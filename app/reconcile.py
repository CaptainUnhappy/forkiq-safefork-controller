"""Durable push-result checks. This module never writes Git refs or replays a push."""
import json
import time
from pathlib import Path

from .db import utcnow
from .errors import failure_summary
from .gitstore import GitStore, PushNetworkError, RemoteReadUnavailable, SyncBlocked
from .safefork import validate_config, valid_ref, valid_sha

DELAYS = (60, 300, 900, 1800, 3600)
CONNECTION_KEYS = ("github_app_id", "github_installation_id", "github_pem_path")


def connection(db):
    return [db.get_setting(key) for key in CONNECTION_KEYS]


def _change(db, row, values, message, kind, *, require_connection=None):
    with db.transaction() as conn:
        live = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
        if (not live or any(live[key] != row[key] for key in
                ("config_json", "control_epoch", "push_intent_json"))
                or (live["enabled"], live["review_required"]) != (0, 1)
                or (require_connection is not None and connection(db) != require_connection)):
            return False
        conn.execute("UPDATE repositories SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                     (*values.values(), row["id"]))
        conn.execute("INSERT INTO events(repo_id,at,kind,message) VALUES(?,?,?,?)", (row["id"], utcnow(), kind, message))
        if values.get("status") == "synced":
            config = json.loads(row["config_json"])
            if config.get("prune_deleted_branches"):
                plan = json.loads(values["plan_json"])
                conn.execute("INSERT OR REPLACE INTO sync_baselines VALUES(?,?)", (row["id"], json.dumps(plan["source"], sort_keys=True)))
        return True


def save_intent(db, row, linked, plan, expected, cancelled):
    """Called after archive preservation and guards, immediately before Git push."""
    intent = dict(version=1, config_json=row["config_json"], epoch=row["control_epoch"],
                  connection=list(linked), plan=plan, expected=expected, retry_allowed=False)
    with db.transaction() as conn:
        live = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
        if (cancelled() or not live or (live["enabled"], live["review_required"]) != (0, 1)
                or live["config_json"] != row["config_json"] or live["control_epoch"] != row["control_epoch"]
                or connection(db) != list(linked) or live["push_intent_json"]):
            raise SyncBlocked("推送前授权或配置已变化，未发出推送。")
        conn.execute("UPDATE repositories SET push_intent_json=?,status='verifying',next_retry_at=?,"
                     "last_error='推送结果待核验；中断后仅检查远端，不直接重复推送。' WHERE id=?",
                     (json.dumps(intent, sort_keys=True), time.time() + DELAYS[0], row["id"]))


def queue_check(db, original, linked, error, cancelled):
    """A failed write grants read-only checking, never immediate retry authority."""
    row = db.one("SELECT * FROM repositories WHERE id=?", (original["id"],))
    if (cancelled() or not row or not row["push_intent_json"]
            or row["config_json"] != original["config_json"] or row["control_epoch"] != original["control_epoch"]
            or connection(db) != list(linked)):
        return False
    intent = json.loads(row["push_intent_json"])
    intent["retry_allowed"] = isinstance(error, PushNetworkError) and row["retry_count"] < len(DELAYS)
    count = min(row["retry_count"] + 1, len(DELAYS))
    reason = str(error) if isinstance(error, SyncBlocked) else "目标推送结果未知（unknown）。" + failure_summary(error, "push")
    message = reason + " 已安排只读结果核验；不会直接重复推送。"
    return _change(db, row, dict(push_intent_json=json.dumps(intent, sort_keys=True), status="verifying",
        retry_count=count, next_retry_at=time.time() + DELAYS[count - 1], last_error=message,
        last_check_at=utcnow(), updated_at=utcnow()), message, "retry", require_connection=list(linked))


def _validate_intent(row, intent):
    config = json.loads(row["config_json"])
    validate_config(config)
    if (intent["version"] != 1 or intent["config_json"] != row["config_json"]
            or intent["epoch"] != row["control_epoch"] or type(intent["retry_allowed"]) is not bool):
        raise SyncBlocked("推送记录身份或控制版本变化，保持停写。")
    plan = intent["plan"]
    for refs in (plan["source"], plan["target"], plan["updates"], plan.get("deletes", {}),
                 plan["archive"]["before"], intent["expected"]):
        if not isinstance(refs, dict) or any(not valid_ref(ref) or not valid_sha(sha) for ref, sha in refs.items()):
            raise SyncBlocked("推送记录引用不完整，保持停写。")
    expected = {**plan["target"], **plan["updates"]}
    for ref, sha in plan.get("deletes", {}).items():
        if plan["target"].get(ref) != sha or not ref.startswith("refs/heads/"):
            raise SyncBlocked("推送记录删除快照无效，保持停写。")
        expected.pop(ref, None)
    if expected != intent["expected"] or plan["archive"].get("saved") is not True:
        raise SyncBlocked("推送记录缺少已保存的完整计划，保持停写。")
    return config, plan


def reconcile(db, row, client, store_type=GitStore, cancelled=lambda: False):
    from .engine import source_client, verify_live
    try:
        intent = json.loads(row["push_intent_json"])
        config, plan = _validate_intent(row, intent)
        linked = intent["connection"]
        if connection(db) != linked:
            raise SyncBlocked("GitHub 连接配置变化，取消自动结果核验。")
        source = source_client(config, client)

        def guard():
            if cancelled():
                raise SyncBlocked("控制器正在停止，保留推送记录。")
            verify_live(config, source, client)

        guard()
        source_token = source.installation_token if config["mode"] == "standalone-private" else lambda: ""
        with store_type(Path(db.path).parent, config["source"], config["target"], source_token, client.installation_token) as store:
            store.ref_readers = {
                "source": lambda: source.refs(config["source"], config["source_id"], config["mode"] == "standalone-private"),
                "target": lambda: client.refs(config["target"], config["target_id"], config["mode"] == "standalone-private")}
            actual = store.refs("target")
            if store.refs("source") != plan["source"]:
                raise SyncBlocked("结果核验期间上游引用变化，请重新预览；未重复推送。")
            archive = store.archive_refs(config)
            saved = {**plan["archive"]["before"], **plan["target"], **plan["updates"]}
            after_archive = {k: v for k, v in saved.items() if k not in plan.get("deletes", {})}
            guard()
            if store.refs("target") != actual:
                raise SyncBlocked("结果核验期间目标变化，保持停写。")
            if actual == intent["expected"]:
                if archive != after_archive:
                    raise SyncBlocked("GitHub 已更新，但服务器副本尚未完整确认；请人工预览。")
                stamp = utcnow()
                plan["checked_at"] = stamp
                dest = "refs/heads/" + config["branch_map"].get(config["primary"], config["primary"])
                if cancelled():
                    return
                _change(db, row, dict(enabled=1, review_required=0, status="synced", push_intent_json=None,
                    next_retry_at=0, retry_count=0, last_error=None, plan_json=json.dumps(plan, sort_keys=True),
                    fork_sha=actual.get(dest), upstream_sha=plan["source"].get("refs/heads/" + config["primary"]),
                    last_check_at=stamp, last_success_at=stamp, updated_at=stamp),
                    "完整回读确认上次推送已成功；未重复推送，已恢复自动检查。", "synced", require_connection=linked)
            elif actual == plan["target"] and intent["retry_allowed"]:
                if archive != saved:
                    raise SyncBlocked("服务器副本与推送记录不一致，保持停写。")
                if cancelled():
                    return
                _change(db, row, dict(enabled=1, review_required=0, status="retrying", push_intent_json=None,
                    next_retry_at=time.time(), plan_json=None, last_check_at=utcnow(),
                    last_error="已确认目标完全未变；将重新检查完整历史并生成新计划后重试。"),
                    "网络推送失败后完整回读确认目标未变；已排队重新规划，不重放旧推送。", "retry", require_connection=linked)
            else:
                raise SyncBlocked("推送结果有部分变化、冲突或不具备安全重试条件；保持停写，请预览。")
    except Exception as error:
        if cancelled():
            return
        if isinstance(error, RemoteReadUnavailable) and row["retry_count"] < len(DELAYS):
            count = row["retry_count"] + 1
            message = "只读推送结果核验暂不可用，延迟继续核验；未重复推送。" + str(error)
            _change(db, row, dict(next_retry_at=time.time() + DELAYS[count - 1], retry_count=count,
                last_error=message, last_check_at=utcnow()), message, "retry")
            return
        message = str(error) if isinstance(error, SyncBlocked) else failure_summary(error, "reconcile")
        _change(db, row, dict(enabled=0, review_required=1, status="needs_attention", next_retry_at=0,
            push_intent_json=None, plan_json=None, last_error=message, last_check_at=utcnow()), message, "blocked")
