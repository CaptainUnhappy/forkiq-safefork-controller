"""Fail-closed deletion review, bound to immutable source and target commits."""
import hashlib
import json
import math


DEFAULT_MAX_FILES = 100
DEFAULT_MAX_PERCENT = 50


def parse_limits(files, percent):
    try:
        if isinstance(files, bool) or (isinstance(files, float) and not files.is_integer()):
            raise ValueError
        count = int(files)
        ratio = float(percent)
        if not 0 <= count <= 1_000_000 or not math.isfinite(ratio) or not 0 <= ratio <= 100:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("删除文件数须为 0–1000000 的整数，比例须为 0–100%") from None
    return count, ratio


def deletion_policy(db, row=None):
    enabled = db.get_setting("deletion_protection", "1")
    count = db.get_setting("deletion_max_files", str(DEFAULT_MAX_FILES))
    percent = db.get_setting("deletion_max_percent", str(DEFAULT_MAX_PERCENT))
    if row is not None:
        if row["deletion_protection"] is not None:
            enabled = str(row["deletion_protection"])
        if row["deletion_max_files"] is not None:
            count = row["deletion_max_files"]
        if row["deletion_max_percent"] is not None:
            percent = row["deletion_max_percent"]
    if enabled not in ("0", "1"):
        raise ValueError("异常删除保护开关配置无效")
    count, percent = parse_limits(count, percent)
    return {"enabled": enabled == "1", "max_files": count, "max_percent": percent}


def load_review(row):
    try:
        review = json.loads(row["deletion_review"] or "null")
        return review if isinstance(review, dict) else None
    except (TypeError, ValueError):
        return None


def review_json(review):
    return json.dumps(review, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def review_token(review):
    return hashlib.sha256(review_json(review).encode()).hexdigest()


def change_identity(row, fork_sha, target_sha):
    return {"fork_sha": fork_sha, "target_sha": target_sha,
            "upstream": row["upstream_full_name"], "branch": row["branch"], "mode": row["mode"]}


def valid_comparison(payload):
    return (isinstance(payload, dict)
            and all(type(payload.get(key)) is int and payload[key] >= 0
                    for key in ("ahead_by", "behind_by")))


def assess_change(client, row, fork_sha, target_sha, policy, previous=None, comparison=None):
    identity = change_identity(row, fork_sha, target_sha)
    if (previous and previous.get("identity") == identity and not previous.get("error")
            and all(key in previous for key in ("current_files", "target_files", "deleted_files"))):
        counts = {key: previous[key] for key in ("current_files", "target_files", "deleted_files")}
    else:
        current = client.file_paths(row["full_name"], fork_sha)
        target = client.file_paths(row["upstream_full_name"], target_sha)
        if row["mode"] == "manual":
            if comparison is None:
                comparison = client.compare(row["full_name"], fork_sha, target_sha)
            if not valid_comparison(comparison):
                raise RuntimeError("提交比较缺失或不完整，不能核实手工合并的删除范围")
            base_sha = ((comparison or {}).get("merge_base_commit") or {}).get("sha")
            if not base_sha:
                raise RuntimeError("无法确定共同祖先，不能核实手工合并的删除范围")
            base = current if base_sha == fork_sha else client.file_paths(row["full_name"], base_sha)
            # Fork-only files survive a three-way merge; do not count them as deletions.
            deleted = current & (base - target)
        else:
            deleted = current - target
        counts = {"current_files": len(current), "target_files": len(target),
                  "deleted_files": len(deleted)}
    ratio = counts["deleted_files"] * 100 / counts["current_files"] if counts["current_files"] else 0
    reasons = []
    if counts["target_files"] == 0:
        reasons.append("上游目标文件树为空")
    if counts["deleted_files"] > policy["max_files"]:
        reasons.append(f"删除文件数超过 {policy['max_files']} 个")
    if ratio > policy["max_percent"]:
        reasons.append(f"删除比例超过 {policy['max_percent']:g}%")
    return {"identity": identity, **counts, "deleted_percent": ratio,
            "policy": policy, "reasons": reasons, "error": ""}
