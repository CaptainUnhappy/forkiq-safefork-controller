"""One non-destructive synchronization interface for all heads and raw tags."""
import re
import time
from .gitstore import SyncBlocked

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")


def valid_sha(value):
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def repo_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value) or value.split("/")[1] in (".", ".."):
        raise SyncBlocked("仓库名称必须是 owner/repo，不能包含凭据、查询或路径。")
    return value


def valid_ref(ref):
    if not isinstance(ref, str) or not ref.startswith(("refs/heads/", "refs/tags/")):
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c in "~^:?*[\\" for c in ref):
        return False
    return not (".." in ref or "@{" in ref or ref.endswith(".") or
                any(not p or p.startswith(".") or p.endswith(".lock") for p in ref.split("/")))


def validate_config(config):
    for key in ("source", "target"):
        repo_name(config[key])
    if config["source"].lower() == config["target"].lower():
        raise SyncBlocked("来源与目标不能相同。")
    for key in ("source_id", "target_id"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise SyncBlocked("缺少已核实的仓库 ID。")
    if config["source_id"] == config["target_id"]:
        raise SyncBlocked("来源与目标 ID 不能相同。")
    if config.get("mode") not in ("public-fork", "standalone-private"):
        raise SyncBlocked("不支持的存储模式。")
    if not valid_ref("refs/heads/" + config.get("primary", "")):
        raise SyncBlocked("源主分支无效。")
    mapping = config.get("branch_map", {})
    if not isinstance(mapping, dict):
        raise SyncBlocked("分支映射必须为对象。")
    seen = set()
    for source, target in mapping.items():
        if (not isinstance(target, str) or not valid_ref("refs/heads/" + source)
                or not valid_ref("refs/heads/" + target) or source == target
                or "sync-control" in (source, target) or target in seen or target in mapping):
            raise SyncBlocked("分支映射非法、重复、成链或触及控制分支。")
        seen.add(target)
    preserved = config.get("preserved_tags", {})
    if not isinstance(preserved, dict):
        raise SyncBlocked("保留标签配置无效。")
    for tag, pair in preserved.items():
        if (not valid_ref("refs/tags/" + tag) or not isinstance(pair, list) or len(pair) != 2
                or not all(valid_sha(v) for v in pair) or pair[0] == pair[1]):
            raise SyncBlocked("保留标签必须固定目标和来源的不同原始对象 SHA。")
    if config["mode"] == "standalone-private":
        if config.get("authorized") is not True or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config.get("source_secret", "")):
            raise SyncBlocked("私有备份需独立复制/留存授权及单独来源凭据。")


def validate_identity(config, source, target):
    validate_config(config)
    private = config["mode"] == "standalone-private"
    for side, live in (("source", source), ("target", target)):
        if (live.get("id") != config[side + "_id"] or live.get("full_name") != config[side]
                or live.get("private") is not private
                or live.get("visibility") != ("private" if private else "public")):
            raise SyncBlocked("仓库身份或可见性发生变化；保留原配置，须人工审查。")
    if source.get("default_branch") != config["primary"]:
        raise SyncBlocked("源默认分支变化，须人工审查。")
    if target.get("archived") is not False:
        raise SyncBlocked("目标已归档或状态未知。")
    if private:
        if target.get("fork") is not False or target.get("parent"):
            raise SyncBlocked("私有目标必须是独立私有仓库，不能是原生 Fork。")
    else:
        parent = target.get("parent") or {}
        if (target.get("fork") is not True or parent.get("id") != config["source_id"]
                or parent.get("full_name") != config["source"]):
            raise SyncBlocked("公开目标的 Fork 来源变化；不会选择替代上游。")


def make_plan(config, source, target, store):
    validate_config(config)
    if "refs/heads/" + config["primary"] not in source:
        raise SyncBlocked("源主分支不存在，停止同步。")
    if "refs/heads/sync-control" in source:
        raise SyncBlocked("上游占用 sync-control，不能覆盖控制分支。")
    mapping, preserved = config.get("branch_map", {}), config.get("preserved_tags", {})
    for tag, pair in preserved.items():
        ref = "refs/tags/" + tag
        if [target.get(ref), source.get(ref)] != pair:
            raise SyncBlocked("已审核保留标签的 SHA 变化或引用消失。")
    updates, decisions, destinations = {}, [], set()
    for ref, sha in sorted(source.items()):
        if not valid_ref(ref) or not valid_sha(sha):
            raise SyncBlocked("源引用无效。")
        dest = ref
        if ref.startswith("refs/heads/"):
            branch = ref[len("refs/heads/"):]
            if branch in mapping.values():
                raise SyncBlocked("上游新增分支占用映射目的名，停止同步。")
            dest = "refs/heads/" + mapping.get(branch, branch)
        if dest in destinations:
            raise SyncBlocked("同步目的引用重复。")
        destinations.add(dest)
        old = target.get(dest)
        if old == sha:
            decision = "equal"
        elif ref.startswith("refs/tags/") and old:
            if preserved.get(ref[len("refs/tags/"):]) != [old, sha]:
                raise SyncBlocked("标签冲突：" + ref + "；不会移动已有标签。")
            decision = "preserved"
        elif old:
            if not store.is_ancestor(old, sha):
                raise SyncBlocked("不是快进：" + dest + "；保留目标历史，未写入任何引用。")
            decision = "fast-forward"
        else:
            decision = "create"
        if decision in ("create", "fast-forward"):
            if dest.startswith("refs/heads/"):
                files = store.files(sha)
                if not files:
                    raise SyncBlocked("目标文件树为空：" + dest)
                if old:
                    before = store.files(old)
                    deleted = len(before - files)
                    if deleted > 100 or (before and deleted / len(before) > 0.5):
                        raise SyncBlocked("异常删除：" + dest + "；需要单独审查，不自动放行。")
            updates[dest] = sha
        decisions.append({"source": ref, "target": dest, "old": old, "new": sha, "action": decision})
    # Include preserved target-only refs when checking Git's directory/file namespace.
    names = set(target) | destinations
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:index]) in names for index in range(2, len(parts))):
            raise SyncBlocked("目标引用存在目录/文件名称冲突。")
    return {"updates": updates, "decisions": decisions,
            "target_only": sorted(set(target) - destinations), "source": source, "target": target}


def synchronize(config, store, verify_identity, *, dry_run=True, before_write=lambda: None):
    """verify_identity must enforce live IDs, visibility and disabled/quiescent Actions."""
    validate_config(config)
    verify_identity()
    source, target = store.refs("source"), store.refs("target")
    store.fetch("source", source)
    store.fetch("target", target)
    plan = make_plan(config, source, target, store)
    verify_identity()
    if store.refs("source") != source or store.refs("target") != target:
        raise SyncBlocked("规划期间引用变化，未写入；请重新检查。")
    if dry_run or not plan["updates"]:
        return plan
    verify_identity()
    if store.refs("source") != source or store.refs("target") != target:
        raise SyncBlocked("写入前引用变化，未写入。")
    before_write()
    store.push(plan["updates"], target)
    expected = {**target, **plan["updates"]}
    for attempt in range(3):
        if store.refs("target") == expected:
            return plan
        if attempt < 2:
            time.sleep(1)
    raise SyncBlocked("推送结果未完全确认，已停写；请核对全部引用，不能自动重试。")
