"""Durable public-Fork creation queue, serialized with the existing Git writer."""
import json
import shutil
import time
from pathlib import Path
from .db import utcnow, log_event
from .engine import parse_repo, app_client_from_settings, client_from_settings, verify_live, RETRY_DELAYS, retry_message
from .github import AuthError, RateLimited
from .gitstore import GitStore, SyncBlocked, RemoteReadUnavailable, process_lock
from .oauth import OAuthStore, authorized_client, installation_owner
from .safefork import validate_identity, synchronize

PHASES = {"preflight": "检查来源和容量", "create_intent": "等待 GitHub 创建结果",
          "fork_ready": "关闭目标 Actions", "isolated": "登记服务器同步",
          "registered": "完整保存并首次同步", "sync_intent": "核对首次同步结果", "complete": "已启用自动同步"}


def public_source(name, client):
    meta = client.get_repo(name)
    if (meta.get("full_name") != name or type(meta.get("id")) is not int
            or meta.get("private") is not False or meta.get("visibility") != "public"
            or not meta.get("default_branch") or meta.get("disabled") is True):
        raise SyncBlocked("本入口只支持身份明确、可读取的公开 GitHub 仓库。")
    return meta


def capacity(root, meta):
    # GitHub size is only a conservative admission estimate, not a disk quota.
    size = meta.get("size")
    if type(size) is not int or size < 0:
        raise SyncBlocked("无法核实上游容量，未创建仓库。")
    if shutil.disk_usage(root).free < 1024 ** 3 + max(128 * 1024 ** 2, size * 1024 * 5):
        raise SyncBlocked("服务器剩余容量不足以完整保存此仓库，请先调整容量。")


def same_network_fork(source, owner, api):
    """Public-only listing with authorized metadata quota; no private inventory."""
    network = (source.get("source") or source).get("id")
    for page in range(1, 11):
        rows = api._api("GET", f"/users/{owner}/repos?type=owner&per_page=100&page={page}")
        if not isinstance(rows, list):
            raise SyncBlocked("无法完整检查已有公开 Fork。")
        for row in rows:
            if row.get("private") is False and row.get("fork") is True:
                full = api.get_repo(row["full_name"])
                if full.get("private") is not False or full.get("visibility") != "public":
                    raise SyncBlocked("检查期间公开 Fork 可见性变化，已停止创建。")
                if (full.get("source") or full.get("parent") or {}).get("id") == network:
                    raise SyncBlocked("账号已有同源 Fork：" + full["full_name"] + "；请先决定复用哪个目标。")
        if len(rows) < 100:
            return
    raise SyncBlocked("公开仓库列表超过检查上限，未自动创建。")


def enqueue(db, source):
    source = parse_repo(source)
    client = client_from_settings(db)
    if client is None:
        raise SyncBlocked("请先连接专用 GitHub App。")
    meta = public_source(source, client)
    root = Path(db.path).parent
    oauth, user = authorized_client(OAuthStore(root))
    owner = installation_owner(app_client_from_settings(db), db.get_setting("github_installation_id"))
    if user.get("id") != owner["id"]:
        raise SyncBlocked("授权账号与 App 安装账号不一致。")
    if source.split("/")[0].lower() == owner["login"].lower():
        raise SyncBlocked("GitHub 不能在同一账号 Fork 自己的仓库，请填写其他账号的公开上游。")
    name = source.split("/")[1]
    if not name.endswith("-SafeFork"):
        name += "-SafeFork"
    target = parse_repo(owner["login"] + "/" + name)
    if source.lower() == target.lower():
        raise SyncBlocked("来源和目标不能相同。")
    existing = db.one("SELECT * FROM creation_jobs WHERE source_id=? AND account_id=?", (meta["id"], owner["id"]))
    if existing:
        return existing["id"]
    if db.one("SELECT id FROM repositories WHERE full_name=? COLLATE NOCASE", (target,)) or oauth.repo(target, missing=True):
        raise SyncBlocked("目标名称已存在，请使用登记已有仓库入口；不会覆盖或自动改名。")
    capacity(root, meta)
    with db.transaction() as conn:
        existing = conn.execute("SELECT id FROM creation_jobs WHERE source_id=? AND account_id=?", (meta["id"], owner["id"])).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute("INSERT INTO creation_jobs(source_id,source,target,account_id,primary_branch,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           (meta["id"], source, target, owner["id"], meta["default_branch"], utcnow(), utcnow()))
        return cur.lastrowid


def config_for(job, target_id):
    return dict(source=job["source"], source_id=job["source_id"], target=job["target"], target_id=target_id,
                primary=job["primary_branch"], mode="public-fork", branch_map={}, preserved_tags={},
                prune_deleted_branches=True)


def retry(db, job_id):
    row = db.one("SELECT * FROM creation_jobs WHERE id=?", (job_id,))
    if not row or row["status"] != "blocked":
        raise SyncBlocked("任务没有等待恢复，请刷新查看。")
    if row["phase"] == "sync_intent":
        raise SyncBlocked("首次保存或推送结果待核对，请进入仓库详情运行预览，再确认恢复同步。")
    db.run("UPDATE creation_jobs SET status='queued',last_error=NULL,retry_count=0,next_retry_at=0,updated_at=? WHERE id=? AND status='blocked'", (utcnow(), job_id))


def run_jobs(db, client, cancelled=lambda: False, store_type=GitStore):
    root = Path(db.path).parent
    with process_lock(root / "writer.lock"):
        # A process exit never silently repeats a write. Explicit recovery is required.
        db.run("UPDATE creation_jobs SET status='blocked',last_error='任务中断，请重新核对后继续。' WHERE status='working'")
        row = db.one("SELECT * FROM creation_jobs WHERE status='queued' OR (status='retrying' AND next_retry_at<=?) ORDER BY id LIMIT 1", (time.time(),))
        if not row or cancelled():
            return
        job = dict(row)
        connection = tuple(db.get_setting(k) for k in ("github_app_id", "github_installation_id", "github_pem_path"))
        db.run("UPDATE creation_jobs SET status='working',last_error=NULL,updated_at=? WHERE id=?", (utcnow(), job["id"]))
        def phase(value, **fields):
            job.update(phase=value, **fields)
            fields.update(phase=value, updated_at=utcnow())
            db.run("UPDATE creation_jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), job["id"]))
        try:
            oauth, user = authorized_client(OAuthStore(root))
            def check_authority():
                current = tuple(db.get_setting(k) for k in ("github_app_id", "github_installation_id", "github_pem_path"))
                if (cancelled() or current != connection
                        or OAuthStore(root).read().get("generation") != oauth.generation):
                    raise SyncBlocked("创建授权或 App 连接已变化，任务停止。")
            owner = installation_owner(app_client_from_settings(db), db.get_setting("github_installation_id"))
            if user.get("id") != job["account_id"] or owner["id"] != job["account_id"] or user.get("login") != job["target"].split("/")[0]:
                raise SyncBlocked("任务的 GitHub 账号发生变化。")
            source = public_source(job["source"], client)
            if source["id"] != job["source_id"] or source["default_branch"] != job["primary_branch"]:
                raise SyncBlocked("创建期间来源身份或默认分支变化，已停写。")
            capacity(root, source)
            if job["phase"] == "preflight":
                if oauth.repo(job["target"], missing=True):
                    raise SyncBlocked("目标名称已被占用，未覆盖现有仓库。")
                same_network_fork(source, user["login"], client)
                check_authority()
                phase("create_intent")
                target = oauth.create_fork(job["source"], job["target"].split("/")[1])
                if not isinstance(target, dict) or type(target.get("id")) is not int or target.get("full_name") != job["target"]:
                    raise SyncBlocked("GitHub 返回了其他目标（可能已有同源 Fork），请核对；未接管或修改。")
                phase("create_intent", target_id=target["id"])
            # Bounded polling only; never repeat the create request after an uncertain response.
            target = None
            for attempt in range(7):
                target = oauth.repo(job["target"], missing=True)
                if target:
                    break
                if attempt == 6 or cancelled():
                    raise SyncBlocked("GitHub 创建尚未确认；稍后继续检查，不会重复创建。")
                time.sleep(5)
            if job["target_id"] is None:
                # A lost POST response cannot safely distinguish a concurrently-created repository.
                raise SyncBlocked("创建响应中断，目标 ID 尚未固定；请核对后通过登记已有仓库接管。")
            cfg = config_for(job, job["target_id"])
            # Default selection can be corrected only during this fixed-ID initialization.
            validate_identity({**cfg, "prune_deleted_branches": False}, source, target)
            if job["phase"] == "create_intent":
                phase("fork_ready")
            if job["phase"] == "fork_ready":
                check_authority()
                oauth.isolate(job["target"])
                verify_live({**cfg, "prune_deleted_branches": False}, client, client)
                if target.get("default_branch") != cfg["primary"]:
                    check_authority()
                    oauth.default_branch(job["target"], cfg["primary"])
                if client.get_repo(job["target"]).get("default_branch") != cfg["primary"]:
                    raise SyncBlocked("目标默认分支未确认，已停写。")
                phase("isolated")
            if job["phase"] == "isolated":
                verify_live(cfg, client, client)
                with db.transaction() as conn:
                    if conn.execute("SELECT id FROM repositories WHERE full_name=? COLLATE NOCASE", (job["target"],)).fetchone():
                        raise SyncBlocked("目标已由其他登记操作接管，任务停止。")
                    stamp = utcnow()
                    owner_name, name = job["target"].split("/")
                    cur = conn.execute("INSERT INTO repositories(owner,name,full_name,upstream_full_name,branch,mode,status,config_json,created_at,updated_at) VALUES(?,?,?,?,?,'safefork','pending',?,?,?)",
                                       (owner_name, name, job["target"], job["source"], cfg["primary"], json.dumps(cfg, sort_keys=True), stamp, stamp))
                    job["repo_id"] = cur.lastrowid
                    conn.execute("UPDATE creation_jobs SET repo_id=?,phase='registered',updated_at=? WHERE id=?", (job["repo_id"], stamp, job["id"]))
                job["phase"] = "registered"
            if job["phase"] != "registered":
                raise SyncBlocked("首次写入需要人工核对，请进入仓库详情预览。")
            repo = db.one("SELECT * FROM repositories WHERE id=?", (job["repo_id"],))
            if not repo or repo["control_epoch"] != 0 or json.loads(repo["config_json"]) != cfg:
                raise SyncBlocked("首次任务已被暂停或配置发生变化。")
            def guard():
                check_authority()
                verify_live(cfg, client, client)
                if client.get_repo(cfg["target"]).get("default_branch") != cfg["primary"]:
                    raise SyncBlocked("目标默认分支发生变化。")
            def before_write():
                check_authority()
                with db.transaction() as conn:
                    live = conn.execute("SELECT * FROM repositories WHERE id=?", (job["repo_id"],)).fetchone()
                    current = tuple(db.get_setting(k) for k in ("github_app_id", "github_installation_id", "github_pem_path"))
                    if cancelled() or not live or live["control_epoch"] != 0 or live["config_json"] != repo["config_json"] or current != connection:
                        raise SyncBlocked("首次同步已暂停或授权配置变化，停止写入。")
                    conn.execute("UPDATE creation_jobs SET phase='sync_intent',updated_at=? WHERE id=?", (utcnow(), job["id"]))
                    conn.execute("UPDATE repositories SET review_required=1,enabled=0,status='needs_attention',last_error='首次保存或推送未确认，请核对后恢复。' WHERE id=?", (job["repo_id"],))
            with store_type(root, cfg["source"], cfg["target"], lambda: "", client.installation_token) as store:
                store.ref_readers = {"source":lambda:client.refs(cfg["source"],cfg["source_id"],False),
                                     "target":lambda:client.refs(cfg["target"],cfg["target_id"],False)}
                plan = synchronize(cfg, store, guard, dry_run=False, before_write=before_write)
            stamp = utcnow()
            plan["checked_at"] = stamp
            check_authority()
            with db.transaction() as conn:
                changed = conn.execute("UPDATE repositories SET enabled=1,review_required=0,status='synced',last_error=NULL,plan_json=?,upstream_sha=?,fork_sha=?,last_check_at=?,last_success_at=?,updated_at=? WHERE id=? AND control_epoch=0 AND config_json=?",
                                       (json.dumps(plan, sort_keys=True), plan["source"]["refs/heads/" + cfg["primary"]], plan["source"]["refs/heads/" + cfg["primary"]], stamp, stamp, stamp, job["repo_id"], repo["config_json"]))
                if changed.rowcount != 1 or cancelled():
                    raise SyncBlocked("任务已暂停，首次结果需重新核对。")
                conn.execute("INSERT OR REPLACE INTO sync_baselines VALUES(?,?)", (job["repo_id"], json.dumps(plan["source"], sort_keys=True)))
                conn.execute("UPDATE creation_jobs SET status='complete',phase='complete',last_error=NULL,retry_count=0,next_retry_at=0,updated_at=? WHERE id=?", (stamp, job["id"]))
            log_event(db, job["repo_id"], "synced", "公开 SafeFork 已创建，完整保存与引用核对通过，已启用自动同步。")
        except Exception as error:
            message = str(error) if isinstance(error, (SyncBlocked, AuthError, RateLimited)) else "创建检查未完成，请核对授权、网络和容量后继续；未自动删除仓库。"
            if isinstance(error, RemoteReadUnavailable) and not cancelled():
                with db.transaction() as conn:
                    live = conn.execute("SELECT * FROM creation_jobs WHERE id=?", (job["id"],)).fetchone()
                    repo = conn.execute("SELECT * FROM repositories WHERE id=?", (job.get("repo_id"),)).fetchone()
                    current = tuple(db.get_setting(k) for k in ("github_app_id", "github_installation_id", "github_pem_path"))
                    if (live["phase"] in ("preflight", "registered") and current == connection
                            and (not job.get("repo_id") or (repo and repo["control_epoch"] == 0))
                            and live["retry_count"] < len(RETRY_DELAYS)):
                        count = live["retry_count"]
                        message = retry_message(error, count + 1)
                        when = time.time() + RETRY_DELAYS[count]
                        conn.execute("UPDATE creation_jobs SET status='retrying',last_error=?,retry_count=?,next_retry_at=?,updated_at=? WHERE id=?",
                                     (message, count + 1, when, utcnow(), job["id"]))
                        if repo:
                            conn.execute("UPDATE repositories SET status='retrying',last_error=?,last_check_at=? WHERE id=? AND control_epoch=0", (message, utcnow(), job["repo_id"]))
                        return
            db.run("UPDATE creation_jobs SET status='blocked',next_retry_at=0,last_error=?,updated_at=? WHERE id=?", (message, utcnow(), job["id"]))
            if job.get("repo_id"):
                db.run("UPDATE repositories SET enabled=0,review_required=1,status='needs_attention',last_error=?,last_check_at=? WHERE id=? AND control_epoch=0",
                       (message, utcnow(), job["repo_id"]))
