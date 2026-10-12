"""ForkIQ: GitHub fork sync panel."""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import DB, log_event, utcnow
from .engine import (Worker, add_manual, app_client_from_settings,
                     client_from_settings, discover, parse_fork_url)
from .github import AuthError, exchange_manifest, write_pem

STATE_DIR = Path(os.environ.get("FORKIQ_STATE_DIR", "/var/lib/forkiq"))
PEM_PATH = os.environ.get("FORKIQ_PEM_PATH", "/var/lib/forkiq/app.pem")
BASE = Path(__file__).parent
SESSION_TTL = 7 * 86400
MANIFEST_URL = "https://github.com/settings/apps/new"

app = FastAPI(title="ForkIQ", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def asset_version(*names):
    """Content hash for static assets, so a changed file always busts caches.

    A hand-maintained `?v=` string once stayed constant across two deploys and
    left browsers on stale CSS; deriving it from file contents makes that
    impossible.
    """
    digest = hashlib.sha256()
    for name in names:
        digest.update((BASE / "static" / name).read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = asset_version("app.css", "app.js")


db = DB(STATE_DIR / "forkiq.db")
worker = Worker(STATE_DIR, PEM_PATH)
_login_failures = {}
LOGIN_WINDOW = 60
LOGIN_MAX = 5


# -- auth helpers ----------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, stored):
    try:
        salt_hex, _ = stored.split(":", 1)
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored)
    except (ValueError, AttributeError):
        return False


def csrf_for(token):
    return hmac.new(token.encode(), b"forkiq-csrf", hashlib.sha256).hexdigest()


def current_session(request):
    token = request.cookies.get("forkiq_session")
    if not token:
        return None
    row = db.one("SELECT * FROM sessions WHERE token=?", (token,))
    if not row or row["expires_at"] < time.time():
        return None
    return row


def is_secure(request):
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def render(request, name, **context):
    token = request.cookies.get("forkiq_session", "")
    context.update(csrf=csrf_for(token) if token else "",
                   setup_done=db.get_setting("setup_done") == "1",
                   asset_version=ASSET_VERSION)
    return templates.TemplateResponse(request, name, context)


def require_login(request):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    if not current_session(request):
        return RedirectResponse("/login", status_code=302)
    return None


def check_csrf(request, token):
    session = current_session(request)
    if not session or not hmac.compare_digest(csrf_for(session["token"]), token or ""):
        return JSONResponse({"error": "CSRF 校验失败"}, status_code=403)
    return None


@app.on_event("startup")
def startup():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    worker.start()


# -- public routes ---------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if db.get_setting("setup_done") == "1":
        return RedirectResponse("/", status_code=302)
    return render(request, "setup.html", error=None)


@app.post("/setup", response_class=HTMLResponse)
def setup_submit(request: Request, password: str = Form(""), password2: str = Form("")):
    if db.get_setting("setup_done") == "1":
        return RedirectResponse("/", status_code=302)
    if len(password) < 8:
        return render(request, "setup.html", error="密码至少 8 位")
    if password != password2:
        return render(request, "setup.html", error="两次输入的密码不一致")
    db.set_setting("admin_pass_hash", hash_password(password))
    db.set_setting("setup_done", "1")
    log_event(db, None, "auth", "管理员密码已设置")
    return RedirectResponse("/github/connect", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    return render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form("")):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    failures = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(failures) >= LOGIN_MAX:
        _login_failures[ip] = failures
        return render(request, "login.html", error="尝试过多，请 1 分钟后再试")
    if not verify_password(password, db.get_setting("admin_pass_hash", "")):
        failures.append(now)
        _login_failures[ip] = failures
        return render(request, "login.html", error="密码不正确")
    _login_failures.pop(ip, None)
    token = secrets.token_urlsafe(32)
    db.run("INSERT INTO sessions(token,created_at,expires_at) VALUES(?,?,?)",
           (token, now, now + SESSION_TTL))
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("forkiq_session", token, httponly=True, secure=is_secure(request),
                        samesite="lax", max_age=SESSION_TTL)
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("forkiq_session")
    if token:
        db.run("DELETE FROM sessions WHERE token=?", (token,))
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("forkiq_session")
    return response


# -- GitHub App connection -------------------------------------------------
def manifest_payload():
    state = secrets.token_urlsafe(16)
    db.set_setting("github_manifest_state", state)
    return state, {
        "name": "ForkIQ",
        "url": "https://fork.llkan.com",
        "hook_attributes": {"url": "https://fork.llkan.com/healthz", "active": False},
        "redirect_url": "https://fork.llkan.com/api/github/callback",
        "callback_urls": ["https://fork.llkan.com/api/github/callback"],
        "public": False,
        "request_oauth_on_install": False,
        "setup_on_update": False,
        "default_permissions": {"contents": "write", "administration": "write",
                                "metadata": "read", "workflows": "write",
                                "actions": "write"},
        "default_events": [],
    }


@app.get("/github/connect", response_class=HTMLResponse)
def github_connect(request: Request):
    guard = require_login(request)
    if guard:
        return guard
    state, payload = manifest_payload()
    return render(request, "connect.html", manifest=json.dumps(payload, ensure_ascii=False),
                  manifest_url=MANIFEST_URL, state=state,
                  app_slug=db.get_setting("github_app_slug"),
                  installation_id=db.get_setting("github_installation_id"))


@app.get("/api/github/callback", response_class=HTMLResponse)
def github_callback(request: Request, code: str = "", state: str = ""):
    expected = db.get_setting("github_manifest_state")
    if not code or not expected or state != expected:
        return render(request, "message.html", title="连接失败", body="缺少授权码或会话已过期，请重新发起连接。")
    try:
        payload = exchange_manifest(code)
    except Exception as error:
        return render(request, "message.html", title="连接失败", body=f"兑换 App 凭据失败：{error}")
    db.set_setting("github_app_id", payload["id"])
    db.set_setting("github_app_slug", payload.get("slug", ""))
    write_pem(PEM_PATH, payload["pem"])
    db.set_setting("github_manifest_state", "")
    log_event(db, None, "auth", f"GitHub App 已创建：{payload.get('slug')}（id {payload['id']}）")
    slug = payload.get("slug", "")
    return render(request, "install.html", slug=slug,
                  install_url=f"https://github.com/apps/{slug}/installations/new")


@app.post("/github/detect", response_class=HTMLResponse)
def github_detect(request: Request, csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    client = app_client_from_settings(db)
    if client is None:
        return render(request, "message.html", title="尚未配置", body="请先完成 GitHub App 创建。")
    try:
        installations = client.app_installations()
    except AuthError as error:
        return render(request, "message.html", title="检测失败", body=str(error))
    if not installations:
        return render(request, "message.html", title="尚未安装",
                      body="没有找到安装记录。请先在 GitHub 上完成安装，再回来点击检测。")
    installation = installations[0]
    db.set_setting("github_installation_id", installation["id"])
    account = (installation.get("account") or {}).get("login", "?")
    log_event(db, None, "auth", f"GitHub App 已安装到 {account}（installation {installation['id']}）")
    return RedirectResponse("/", status_code=302)


# -- panel routes ----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    guard = require_login(request)
    if guard:
        return guard
    stats = {row["status"]: row["n"] for row in
             db.all("SELECT status, COUNT(*) AS n FROM repositories GROUP BY status")}
    total = sum(stats.values())
    synced = stats.get("synced", 0)
    behind = stats.get("behind", 0) + stats.get("pending", 0)
    blocked = stats.get("blocked", 0) + stats.get("diverged", 0) + stats.get("needs_attention", 0) + stats.get("error", 0)
    paused = stats.get("paused", 0)
    events = db.all("SELECT e.*, r.full_name FROM events e LEFT JOIN repositories r ON r.id=e.repo_id "
                    "ORDER BY e.id DESC LIMIT 20")
    return render(request, "overview.html", stats=stats, total=total, synced=synced,
                  behind=behind, blocked=blocked, paused=paused, events=events,
                  auth_error=db.get_setting("auth_error", ""),
                  app_connected=bool(db.get_setting("github_installation_id")))


@app.get("/repos", response_class=HTMLResponse)
def repo_list(request: Request, filter: str = ""):
    guard = require_login(request)
    if guard:
        return guard
    query = "SELECT * FROM repositories"
    params = ()
    if filter:
        query += " WHERE status=?"
        params = (filter,)
    query += " ORDER BY status='diverged' DESC, status='blocked' DESC, full_name"
    repos = db.all(query, params)
    return render(request, "repos.html", repos=repos, filter=filter)


@app.get("/repos/{repo_id}", response_class=HTMLResponse)
def repo_detail(request: Request, repo_id: int):
    guard = require_login(request)
    if guard:
        return guard
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return render(request, "message.html", title="未找到", body="仓库不在管理列表中。")
    events = db.all("SELECT * FROM events WHERE repo_id=? ORDER BY id DESC LIMIT 50", (repo_id,))
    return render(request, "repo_detail.html", repo=repo, events=events)


def _repo_action(request, repo_id, csrf, action):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    if action == "pause":
        db.run("UPDATE repositories SET status='paused', enabled=0, updated_at=? WHERE id=?",
               (utcnow(), repo_id))
        log_event(db, repo_id, "blocked", f"{repo['full_name']} 已暂停")
    elif action == "resume":
        db.run("UPDATE repositories SET status='pending', enabled=1, last_error=NULL, updated_at=? WHERE id=?",
               (utcnow(), repo_id))
        log_event(db, repo_id, "check", f"{repo['full_name']} 已恢复")
    elif action == "sync":
        db.run("UPDATE repositories SET sync_requested=1, updated_at=? WHERE id=?", (utcnow(), repo_id))
        worker.request_sync()
    elif action == "mode":
        mode = "manual" if repo["mode"] == "mirror" else "mirror"
        db.run("UPDATE repositories SET mode=?, status='pending', last_error=NULL, "
               "diverged_at=NULL, diverged_upstream_sha=NULL, sync_requested=1, "
               "updated_at=? WHERE id=?", (mode, utcnow(), repo_id))
        label = "手工 fork（保留自有提交，上游更新自动合并）" if mode == "manual" else "镜像（严格跟随上游）"
        log_event(db, repo_id, "check", f"{repo['full_name']} 同步方式切换为{label}")
        worker.request_sync()
    elif action == "selfupdate":
        if repo["self_update_interval"]:
            db.run("UPDATE repositories SET self_update_interval=NULL, self_update=NULL, "
                   "updated_at=? WHERE id=?", (utcnow(), repo_id))
            log_event(db, repo_id, "check", f"{repo['full_name']} 已关闭自更新保活")
        else:
            client = client_from_settings(db)
            if client is None:
                return render(request, "message.html", title="尚未配置",
                              body="请先完成 GitHub App 配置。")
            notes = []
            try:
                if not client.actions_enabled(repo["full_name"]):
                    client.set_actions_enabled(repo["full_name"], True)
                    notes.append("已启用 Actions")
                perms = client.workflow_permissions(repo["full_name"])
                if perms.get("default_workflow_permissions") != "write":
                    client.set_workflow_permissions(repo["full_name"], "write")
                    notes.append("workflow 权限设为 write")
                enabled = []
                for workflow in client.list_workflows(repo["full_name"]):
                    if workflow.get("state") in ("disabled_fork", "disabled_inactivity"):
                        client.enable_workflow(repo["full_name"], workflow["id"])
                        enabled.append(workflow["name"])
                if enabled:
                    notes.append("已启用 " + "、".join(enabled))
            except Exception as error:
                db.run("UPDATE repositories SET self_update=?, updated_at=? WHERE id=?",
                       (f"开启自更新部分失败：{str(error)[:200]}", utcnow(), repo_id))
                return render(request, "message.html", title="开启失败",
                              body=f"GitHub 拒绝：{error}。"
                                   "在 https://github.com/settings/apps/forkiq/permissions "
                                   "给 Actions 加读写权限并批准后可重试。")
            db.run("UPDATE repositories SET self_update_interval=1, self_update=NULL, "
                   "sync_requested=1, updated_at=? WHERE id=?", (utcnow(), repo_id))
            log_event(db, repo_id, "check",
                      f"{repo['full_name']} 已开启自更新保活" +
                      (f"（{'; '.join(notes)}）" if notes else ""))
            worker.request_sync()
    elif action == "remove":
        db.run("DELETE FROM repositories WHERE id=?", (repo_id,))
        log_event(db, None, "check", f"{repo['full_name']} 已移出管理（GitHub 仓库未改动）")
    return RedirectResponse(f"/repos/{repo_id}" if action != "remove" else "/repos", status_code=302)


@app.post("/repos/{repo_id}/{action}")
def repo_action(request: Request, repo_id: int, action: str, csrf: str = Form("")):
    if action not in ("pause", "resume", "sync", "remove", "mode", "selfupdate"):
        return JSONResponse({"error": "不支持的操作"}, status_code=400)
    return _repo_action(request, repo_id, csrf, action)


@app.post("/repos/{repo_id}/actions")
def repo_actions_toggle(request: Request, repo_id: int, enabled: str = Form("0"), csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    client = client_from_settings(db)
    if client is None:
        return render(request, "message.html", title="尚未配置", body="请先完成 GitHub App 配置。")
    want = enabled == "1"
    try:
        client.set_actions_enabled(repo["full_name"], want)
    except Exception as error:
        db.run("UPDATE repositories SET last_error=?, updated_at=? WHERE id=?",
               (f"Actions 开关失败：{error}"[:300], utcnow(), repo_id))
        return render(request, "message.html", title="操作失败", body=f"修改 Actions 失败：{error}")
    db.run("UPDATE repositories SET actions_disabled=?, updated_at=? WHERE id=?",
           (int(not want), utcnow(), repo_id))
    log_event(db, repo_id, "check", f"{repo['full_name']} Actions 已{'启用' if want else '禁用'}")
    return RedirectResponse(f"/repos/{repo_id}", status_code=302)


@app.post("/repos/add", response_class=HTMLResponse)
def repo_add(request: Request, url: str = Form(""), csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    client = client_from_settings(db)
    if client is None:
        return render(request, "message.html", title="尚未配置", body="请先完成 GitHub App 配置。")
    try:
        repo_id = add_manual(db, client, url)
    except ValueError as error:
        repos = db.all("SELECT * FROM repositories ORDER BY full_name")
        return render(request, "repos.html", repos=repos, filter="", add_error=str(error))
    worker.request_sync()
    return RedirectResponse(f"/repos/{repo_id}", status_code=302)


@app.post("/discover")
def discover_now(request: Request, csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    db.set_setting("discover_requested", "1")
    worker.request_sync()
    return RedirectResponse("/repos", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    guard = require_login(request)
    if guard:
        return guard
    return render(request, "settings.html",
                  interval=db.get_setting("check_interval", "30"),
                  auto_discover=db.get_setting("auto_discover", "1") == "1",
                  app_slug=db.get_setting("github_app_slug"),
                  installation_id=db.get_setting("github_installation_id"),
                  auth_error=db.get_setting("auth_error", ""),
                  notice=request.query_params.get("notice", ""))


@app.post("/settings", response_class=HTMLResponse)
def settings_save(request: Request, csrf: str = Form(""),
                  interval: str = Form("30"), auto_discover: str = Form("0")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    try:
        value = max(15, min(300, int(interval)))
    except ValueError:
        value = 30
    db.set_setting("check_interval", value)
    db.set_setting("auto_discover", "1" if auto_discover == "1" else "0")
    return RedirectResponse("/settings?notice=设置已保存", status_code=302)


@app.post("/settings/password", response_class=HTMLResponse)
def settings_password(request: Request, csrf: str = Form(""),
                      current: str = Form(""), password: str = Form(""), password2: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    if not verify_password(current, db.get_setting("admin_pass_hash", "")):
        return RedirectResponse("/settings?notice=当前密码不正确", status_code=302)
    if len(password) < 8 or password != password2:
        return RedirectResponse("/settings?notice=新密码至少 8 位且两次一致", status_code=302)
    db.set_setting("admin_pass_hash", hash_password(password))
    db.run("DELETE FROM sessions")
    log_event(db, None, "auth", "管理员密码已修改")
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("forkiq_session")
    return response


@app.post("/settings/disconnect")
def settings_disconnect(request: Request, csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    db.set_setting("github_installation_id", "")
    db.set_setting("auth_error", "")
    log_event(db, None, "auth", "已断开 GitHub 连接（App 安装本身需在 GitHub 上卸载）")
    return RedirectResponse("/settings?notice=已断开连接", status_code=302)
