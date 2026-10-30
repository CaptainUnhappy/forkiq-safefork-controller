"""Serialized SafeFork worker. No discovery, merge, rollback or Actions writes."""
import json
import os
import re
import threading
import time
from pathlib import Path
from .db import DB, insert_repository, log_event, utcnow
from .github import GitHub
from .gitstore import GitStore, SyncBlocked, process_lock
from .safefork import repo_name, synchronize, validate_config, validate_identity


def client_from_settings(db):
    values = [db.get_setting(key) for key in ("github_app_id", "github_pem_path", "github_installation_id")]
    return GitHub(*values) if all(values) else None


def app_client_from_settings(db):
    app_id, pem = db.get_setting("github_app_id"), db.get_setting("github_pem_path")
    return GitHub(app_id=app_id, pem_path=pem) if app_id and pem else None


def parse_repo(value):
    value = value.strip()
    if value.startswith("https://github.com/"):
        value = value[len("https://github.com/"):].rstrip("/")
    return repo_name(value)


def source_client(config, target_client):
    if config["mode"] == "public-fork":
        return target_client
    name = config.get("source_secret", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise SyncBlocked("来源凭据名称无效。")
    root = Path(os.environ.get("FORKIQ_SOURCE_SECRETS_DIR", "/run/secrets")).resolve()
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise SyncBlocked("来源凭据路径无效。")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise SyncBlocked("独立来源凭据尚未挂载或无法读取。") from None
    if not token or len(token) > 16384 or any(c.isspace() for c in token):
        raise SyncBlocked("独立来源凭据格式无效。")
    if token == target_client.installation_token():
        raise SyncBlocked("私有来源凭据不能与目标写入凭据相同。")
    return GitHub(token=token)


def register(db, client, source, target, mode, branch_map, preserved_tags,
             authorized=False, source_secret=""):
    source, target = parse_repo(source), parse_repo(target)
    if mode not in ("public-fork", "standalone-private"):
        raise SyncBlocked("不支持的存储模式。")
    if mode == "standalone-private" and authorized is not True:
        raise SyncBlocked("私有备份需先取得独立复制与留存授权。")
    if db.one("SELECT id FROM repositories WHERE full_name=?", (target,)):
        raise SyncBlocked("目标已登记；不会覆盖已有来源或映射。")
    config = {"source": source, "target": target, "mode": mode, "branch_map": branch_map,
              "preserved_tags": preserved_tags, "authorized": authorized, "source_secret": source_secret}
    target_meta = client.get_repo(target)
    source_meta = source_client(config, client).get_repo(source)
    config.update(source_id=source_meta["id"], target_id=target_meta["id"], primary=source_meta["default_branch"])
    validate_identity(config, source_meta, target_meta)
    owner, name = target.split("/")
    repo_id = insert_repository(db, owner=owner, name=name, full_name=target,
                                upstream_full_name=source, branch=config["primary"],
                                node_id=target_meta.get("node_id"), status="paused")
    db.run("UPDATE repositories SET config_json=?,mode='safefork',enabled=0,review_required=1 WHERE id=?",
           (json.dumps(config, sort_keys=True), repo_id))
    log_event(db, repo_id, "check", "已固定来源和目标身份；保持暂停，请先运行只读预览。")
    return repo_id


def verify_live(config, source, target):
    validate_identity(config, source.get_repo(config["source"]), target.get_repo(config["target"]))
    if target.actions_enabled(config["target"]):
        raise SyncBlocked("目标 Actions 仍启用；请先在 GitHub 全局关闭，控制器不会替你启用或关闭。")
    if target.has_active_runs(config["target"]):
        raise SyncBlocked("目标仍有在途 Actions；等待全部结束后再预览。")


def run_cycle(db, client, store_type=GitStore, cancelled=lambda: False):
    root = Path(db.path).parent
    with process_lock(root / "writer.lock"):
        for original in db.all("SELECT * FROM repositories ORDER BY id"):
            if cancelled():
                return
            row = dict(original)
            dry = bool(row["dry_run_requested"])
            if not dry and (not row["enabled"] or row["review_required"]):
                continue
            db.run("UPDATE repositories SET dry_run_requested=0,sync_requested=0 WHERE id=?", (row["id"],))
            try:
                config = json.loads(row["config_json"] or "null")
                if not isinstance(config, dict):
                    raise SyncBlocked("旧版仓库需显式重新登记安全配置，不能直接恢复。")
                validate_config(config)
                source = source_client(config, client)
                connection = tuple(db.get_setting(k) for k in ("github_app_id", "github_installation_id", "github_pem_path"))

                def guard():
                    verify_live(config, source, client)

                def before_write():
                    # One transaction serializes this gate with UI changes. Once it
                    # commits, the push is in flight; pause cannot recall Git traffic.
                    with db.transaction() as conn:
                        live = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
                        current = tuple(db.get_setting(k) for k in ("github_app_id", "github_installation_id", "github_pem_path"))
                        if (cancelled() or not live or not live["enabled"] or live["review_required"]
                                or live["config_json"] != row["config_json"] or current != connection
                                or live["control_epoch"] != row["control_epoch"]):
                            raise SyncBlocked("同步已暂停、断开或配置变化，停止写入。")
                        # Persist intent before the write. A crash must not auto-retry it.
                        conn.execute("UPDATE repositories SET enabled=0,review_required=1,status='needs_attention',"
                                     "last_error='推送尚未确认；中断后先预览核对。',updated_at=? WHERE id=?",
                                     (utcnow(), row["id"]))

                source_token = (source.installation_token if config["mode"] == "standalone-private" else lambda: "")
                with store_type(root, config["source"], config["target"], source_token,
                                client.installation_token) as store:
                    plan = synchronize(config, store, guard, dry_run=dry, before_write=before_write)
                primary = "refs/heads/" + config["primary"]
                dest = "refs/heads/" + config["branch_map"].get(config["primary"], config["primary"])
                target_refs = plan["target"] if dry else {**plan["target"], **plan["updates"]}
                enabled = int(not dry and bool(row["enabled"]))
                stamp = utcnow()
                plan["checked_at"] = stamp
                db.run("UPDATE repositories SET plan_json=?,fork_sha=?,upstream_sha=?,last_check_at=?,"
                       "last_success_at=?,last_error=NULL,review_required=0,enabled=?,status=?,updated_at=? "
                       "WHERE id=? AND control_epoch=? AND config_json=?",
                       (json.dumps(plan, sort_keys=True), target_refs.get(dest), plan["source"].get(primary), stamp,
                        row["last_success_at"] if dry else stamp, enabled, "synced" if enabled else "paused", stamp,
                        row["id"], row["control_epoch"], row["config_json"]))
                if dry or plan["updates"]:
                    log_event(db, row["id"], "check" if dry else "synced",
                              f"{'只读预览' if dry else '同步已核对'}：{len(plan['updates'])} 个安全更新，"
                              f"保留 {len(plan['target_only'])} 个目标独有引用。")
            except Exception as error:
                message = str(error) if isinstance(error, SyncBlocked) else "检查或连接失败；已停写，请核对凭据、网络与仓库状态后预览。"
                db.run("UPDATE repositories SET enabled=0,review_required=1,status='needs_attention',"
                       "plan_json=NULL,last_error=?,last_check_at=?,updated_at=? WHERE id=? AND control_epoch=?",
                       (message, utcnow(), utcnow(), row["id"], row["control_epoch"]))
                log_event(db, row["id"], "blocked", message)


class Worker(threading.Thread):
    def __init__(self, state_dir, pem_path):
        super().__init__(daemon=True, name="safefork-worker")
        self.state_dir, self.pem_path = Path(state_dir), pem_path
        self.wake, self.stop_flag = threading.Event(), threading.Event()
        self.last_tick, self.busy_since = 0.0, 0.0

    def request_sync(self):
        self.wake.set()

    def run(self):
        db = DB(self.state_dir / "forkiq.db")
        db.set_setting("github_pem_path", self.pem_path)
        due = 0.0
        try:
            while not self.stop_flag.is_set():
                self.last_tick = time.monotonic()
                requested = db.one("SELECT id FROM repositories WHERE dry_run_requested=1 OR sync_requested=1 LIMIT 1")
                if requested or time.monotonic() >= due:
                    client = client_from_settings(db)
                    self.busy_since = time.monotonic()
                    try:
                        if client:
                            run_cycle(db, client, cancelled=self.stop_flag.is_set)
                    except Exception:
                        log_event(db, None, "error", "同步检查未完成；请检查 worker 锁和存储。")
                    finally:
                        self.busy_since = 0.0
                    due = time.monotonic() + max(60, min(86400, int(db.get_setting("check_interval", "3600"))))
                self.wake.wait(5)
                self.wake.clear()
        finally:
            db.close()
