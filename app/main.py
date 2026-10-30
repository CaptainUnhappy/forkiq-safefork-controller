"""ForkIQ SafeFork controller: authenticated management, one safe write path."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .db import DB, log_event, utcnow
from .engine import Worker, app_client_from_settings, client_from_settings, register
from .github import exchange_manifest, write_pem
from .gitstore import SyncBlocked
from .errors import error_guidance
from .versions import annotate, short_time

STATE_DIR = Path(os.environ.get("FORKIQ_STATE_DIR", "/var/lib/forkiq"))
PEM_PATH = os.environ.get("FORKIQ_PEM_PATH", str(STATE_DIR / "app.pem"))
BASE = Path(__file__).parent
SESSION_TTL = 7 * 86400
MANIFEST_URL = "https://github.com/settings/apps/new"
app = FastAPI(title="ForkIQ SafeFork", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["short_time"] = short_time
templates.env.filters["error_guidance"] = error_guidance
ASSET_VERSION = hashlib.sha256((BASE / "static/app.css").read_bytes() +
                               (BASE / "static/app.js").read_bytes()).hexdigest()[:10]
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
db = DB(STATE_DIR / "forkiq.db")
worker = Worker(STATE_DIR, PEM_PATH)
_login_failures = {}
STATUS_GROUPS = {"synced": ("synced",), "waiting": ("pending", "behind"),
                 "attention": ("blocked", "diverged", "needs_attention", "error", "missing"), "paused": ("paused",)}


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, stored):
    try:
        salt, _ = stored.split(":", 1)
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except (ValueError, AttributeError):
        return False


def csrf_for(token):
    return hmac.new(token.encode(), b"forkiq-csrf", hashlib.sha256).hexdigest()


def current_session(request):
    token = request.cookies.get("forkiq_session")
    row = db.one("SELECT * FROM sessions WHERE token=?", (token,)) if token else None
    return row if row and row["expires_at"] > time.time() else None


def render(request, name, **context):
    token = request.cookies.get("forkiq_session", "")
    context.update(csrf=csrf_for(token) if token else "", setup_done=db.get_setting("setup_done") == "1",
                   asset_version=ASSET_VERSION)
    return templates.TemplateResponse(request, name, context)


def require_login(request):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    if not current_session(request):
        return RedirectResponse("/login", status_code=302)


def check_csrf(request, token):
    session = current_session(request)
    if not session or not hmac.compare_digest(csrf_for(session["token"]), token or ""):
        return JSONResponse({"error": "CSRF 校验失败"}, status_code=403)


def form_guard(request, csrf):
    return require_login(request) or check_csrf(request, csrf)


@app.on_event("startup")
def startup():
    db.set_setting("github_pem_path", PEM_PATH)
    worker.start()


@app.on_event("shutdown")
def shutdown():
    worker.stop_flag.set()
    worker.wake.set()
    worker.join(timeout=2)
    db.close()


@app.get("/healthz")
def healthz():
    now = time.monotonic()
    fresh = (now - worker.busy_since < 3600) if worker.busy_since else (now - worker.last_tick < 30)
    ok = worker.is_alive() and fresh
    return JSONResponse({"status": "ok" if ok else "worker_unavailable", "busy": bool(worker.busy_since)},
                        status_code=200 if ok else 503)


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if db.get_setting("setup_done") == "1":
        return RedirectResponse("/", status_code=302)
    return render(request, "setup.html", error=None)


@app.post("/setup", response_class=HTMLResponse)
def setup_submit(request: Request, password: str = Form(""), password2: str = Form("")):
    if db.get_setting("setup_done") == "1":
        return RedirectResponse("/", status_code=302)
    if len(password) < 12 or len(password) > 1024:
        return render(request, "setup.html", error="密码需为 12–1024 位")
    if password != password2:
        return render(request, "setup.html", error="两次输入的密码不一致")
    hashed = hash_password(password)
    with db.transaction() as conn:
        # A second concurrent setup request must not replace the first password.
        if db.get_setting("setup_done") == "1":
            return JSONResponse({"error": "初始化已完成，请登录。"}, status_code=409)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('admin_pass_hash',?)", (hashed,))
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('setup_done','1')")
    log_event(db, None, "auth", "管理员密码已设置")
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form("")):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    failures = [t for t in _login_failures.get(ip, []) if now - t < 60]
    if len(failures) >= 5:
        _login_failures[ip] = failures
        return render(request, "login.html", error="尝试过多，请 1 分钟后再试")
    if len(password) > 1024 or not verify_password(password, db.get_setting("admin_pass_hash", "")):
        _login_failures[ip] = failures + [now]
        return render(request, "login.html", error="密码不正确")
    _login_failures.pop(ip, None)
    token = secrets.token_urlsafe(32)
    db.run("INSERT INTO sessions(token,created_at,expires_at) VALUES(?,?,?)", (token, now, now + SESSION_TTL))
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("forkiq_session", token, httponly=True,
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                        samesite="lax", max_age=SESSION_TTL)
    return response


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    denied = form_guard(request, csrf)
    if denied:
        return denied
    db.run("DELETE FROM sessions WHERE token=?", (request.cookies.get("forkiq_session"),))
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("forkiq_session")
    return response


def public_url():
    value = os.environ.get("FORKIQ_PUBLIC_URL", "")
    try:
        parsed = urlsplit(value)
        if (any(c.isspace() or ord(c) < 32 for c in value) or parsed.scheme != "https"
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or "?" in value or "#" in value
                or "\\" in value or "%" in parsed.netloc or parsed.netloc.endswith(":")):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("FORKIQ_PUBLIC_URL 必须是本实例的 HTTPS 根地址，不含账号或子路径。") from None
    return f"{parsed.scheme}://{parsed.netloc}"


def manifest_payload():
    base = public_url()
    state = secrets.token_urlsafe(32)
    db.set_setting("github_manifest_state", state)
    return state, {"name": "ForkIQ SafeFork", "url": base,
                   "hook_attributes": {"url": base + "/healthz", "active": False},
                   "redirect_url": base + "/api/github/callback", "callback_urls": [base + "/api/github/callback"],
                   "public": False, "request_oauth_on_install": False, "setup_on_update": False,
                   "default_permissions": {"contents": "write", "workflows": "write", "metadata": "read",
                                           "administration": "read", "actions": "read"}, "default_events": []}


def github_connection_page(request, detect_error=""):
    slug = db.get_setting("github_app_slug")
    state, payload, error = "", {}, ""
    if not slug:
        try:
            state, payload = manifest_payload()
        except ValueError as failure:
            error = str(failure)
    return render(request, "connect.html", manifest=json.dumps(payload, ensure_ascii=False),
                  manifest_url=MANIFEST_URL, state=state, app_slug=slug,
                  installation_id=db.get_setting("github_installation_id"), auth_error="",
                  config_error=error, detect_error=detect_error)


@app.get("/github/connect", response_class=HTMLResponse)
def github_connect(request: Request):
    return require_login(request) or github_connection_page(request)


@app.get("/api/github/callback", response_class=HTMLResponse)
def github_callback(request: Request, code: str = "", state: str = ""):
    denied = require_login(request)
    if denied:
        return denied
    expected = db.get_setting("github_manifest_state")
    if not code or not expected or not hmac.compare_digest(state, expected):
        return JSONResponse({"error": "授权状态失效，请重新连接。"}, status_code=400)
    db.set_setting("github_manifest_state", "")
    try:
        payload = exchange_manifest(code)
        write_pem(PEM_PATH, payload["pem"])
        db.set_setting("github_app_id", payload["id"])
        db.set_setting("github_app_slug", payload["slug"])
    except Exception:
        return JSONResponse({"error": "App 连接未完成，请在 GitHub 核对后重试。"}, status_code=409)
    return render(request, "install.html", slug=payload["slug"],
                  install_url=f"https://github.com/apps/{payload['slug']}/installations/new")


@app.post("/github/detect")
def github_detect(request: Request, csrf: str = Form("")):
    denied = form_guard(request, csrf)
    if denied:
        return denied
    client = app_client_from_settings(db)
    try:
        installations = client.app_installations() if client else []
        if len(installations) != 1:
            raise ValueError
    except Exception:
        return github_connection_page(request, "请确保此专用 App 只有一个获批安装；不能自动选择账号。")
    db.set_setting("github_installation_id", installations[0]["id"])
    return RedirectResponse("/repos", status_code=302)


def _rows(filter=""):
    if not filter:
        return db.all("SELECT * FROM repositories ORDER BY full_name")
    statuses = STATUS_GROUPS.get(filter, (filter,))
    return db.all("SELECT * FROM repositories WHERE status IN (" + ",".join("?" for _ in statuses) + ") ORDER BY full_name", statuses)


def _revision(rows):
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()[:16]


def _repo_list_context(filter=""):
    rows = _rows(filter)
    return {"repos": annotate(rows), "filter": filter, "revision": _revision(rows), "auto_discover": False}


def _overview_context():
    rows = _rows()
    counts = {key: sum(r["status"] in states for r in rows) for key, states in STATUS_GROUPS.items()}
    events = db.all("SELECT e.*,r.full_name FROM events e LEFT JOIN repositories r ON r.id=e.repo_id ORDER BY e.id DESC LIMIT 20")
    return {"total": len(rows), "synced": counts["synced"], "behind": counts["waiting"],
            "blocked": counts["attention"], "paused": counts["paused"], "events": events,
            "auth_error": "", "app_connected": bool(db.get_setting("github_installation_id")),
            "revision": _revision(rows) + _revision(events)}


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    return require_login(request) or render(request, "overview.html", **_overview_context())


@app.get("/repos", response_class=HTMLResponse)
def repo_list(request: Request, filter: str = ""):
    return require_login(request) or render(request, "repos.html", **_repo_list_context(filter), notice=request.query_params.get("notice", ""))


@app.get("/partials/repos")
def partial_repos(request: Request, filter: str = ""):
    if not current_session(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    context = _repo_list_context(filter)
    if request.query_params.get("v") == context["revision"]:
        return Response(status_code=204)
    return render(request, "partials/repo_table.html", **context)


@app.get("/partials/overview")
def partial_overview(request: Request):
    if not current_session(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    context = _overview_context()
    if request.query_params.get("v") == context["revision"]:
        return Response(status_code=204)
    return render(request, "partials/overview_regions.html", **context)


def parse_pairs(value, fields):
    result = {}
    for line in value.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != fields or parts[0] in result:
            raise SyncBlocked("映射或保留标签行格式错误，或名称重复。")
        result[parts[0]] = parts[1] if fields == 2 else parts[1:]
    return result


@app.post("/repos/add")
def repo_add(request: Request, csrf: str = Form(""), source: str = Form(""), target: str = Form(""),
             mode: str = Form("public-fork"), branch_map: str = Form(""), preserved_tags: str = Form(""),
             authorized: str = Form(""), source_secret: str = Form("")):
    denied = form_guard(request, csrf)
    if denied:
        return denied
    client = client_from_settings(db)
    if not client:
        return JSONResponse({"error": "请先连接专用 GitHub App。"}, status_code=409)
    try:
        repo_id = register(db, client, source, target, mode, parse_pairs(branch_map, 2),
                           parse_pairs(preserved_tags, 3), authorized == "1", source_secret)
    except Exception as error:
        message = str(error) if isinstance(error, SyncBlocked) else "无法核实仓库，请检查身份、权限和输入；未加入同步。"
        return render(request, "repos.html", **_repo_list_context(), add_error=message)
    return RedirectResponse(f"/repos/{repo_id}", status_code=302)


def review_token(plan):
    return hashlib.sha256((plan or "").encode()).hexdigest()


@app.get("/repos/{repo_id}", response_class=HTMLResponse)
def repo_detail(request: Request, repo_id: int):
    denied = require_login(request)
    if denied:
        return denied
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    return render(request, "repo_detail.html", repo=repo,
                  config=json.loads(repo["config_json"] or "null"), plan=json.loads(repo["plan_json"] or "null"),
                  review=review_token(repo["plan_json"]),
                  events=db.all("SELECT * FROM events WHERE repo_id=? ORDER BY id DESC LIMIT 50", (repo_id,)),
                  notice=request.query_params.get("notice", ""))


@app.post("/repos/{repo_id}/{action}")
def repo_action(request: Request, repo_id: int, action: str, csrf: str = Form(""), review: str = Form("")):
    denied = form_guard(request, csrf)
    if denied:
        return denied
    row = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not row:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    if action == "pause":
        db.run("UPDATE repositories SET enabled=0,status='paused',sync_requested=0,control_epoch=control_epoch+1 WHERE id=?", (repo_id,))
    elif action == "preview":
        db.run("UPDATE repositories SET enabled=0,status='paused',dry_run_requested=1,control_epoch=control_epoch+1 WHERE id=?", (repo_id,))
    elif action == "resume":
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_check_at"])).total_seconds()
        except (TypeError, ValueError):
            age = 9999
        if (row["review_required"] or not row["plan_json"] or not 0 <= age <= 600
                or not hmac.compare_digest(review_token(row["plan_json"]), review)):
            return JSONResponse({"error": "请先完成最新只读预览，在 10 分钟内确认计划。"}, status_code=409)
        changed = db.run("UPDATE repositories SET enabled=1,status='pending',sync_requested=1,control_epoch=control_epoch+1 "
                         "WHERE id=? AND control_epoch=? AND plan_json=? AND review_required=0",
                         (repo_id, row["control_epoch"], row["plan_json"]))
        if changed.rowcount != 1:
            return JSONResponse({"error": "计划或状态已变化，请刷新并重新预览。"}, status_code=409)
    elif action == "sync" and row["enabled"] and not row["review_required"]:
        db.run("UPDATE repositories SET sync_requested=1 WHERE id=?", (repo_id,))
    else:
        return JSONResponse({"error": "不支持该操作；SafeFork 不提供强推、合并、回滚或 Actions 开关。"}, status_code=400)
    log_event(db, repo_id, "check", {"pause": "已暂停", "preview": "只读预览已排队，保持暂停",
                                    "resume": "已确认计划并启用安全同步", "sync": "安全同步已排队"}[action])
    worker.request_sync()
    return RedirectResponse(f"/repos/{repo_id}", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return require_login(request) or render(request, "settings.html",
        interval_minutes=f'{int(db.get_setting("check_interval", "3600")) / 60:g}',
        app_slug=db.get_setting("github_app_slug"), installation_id=db.get_setting("github_installation_id"),
        auth_error="", notice=request.query_params.get("notice", ""))


@app.post("/settings")
def settings_save(request: Request, csrf: str = Form(""), interval_minutes: str = Form("")):
    denied = form_guard(request, csrf)
    if denied:
        return denied
    try:
        value = float(interval_minutes)
        if not math.isfinite(value) or not 1 <= value <= 1440:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "检查间隔需为 1–1440 分钟。"}, status_code=400)
    db.set_setting("check_interval", round(value * 60))
    return RedirectResponse("/settings?notice=设置已保存", status_code=302)


@app.post("/settings/password")
def settings_password(request: Request, csrf: str = Form(""), current: str = Form(""),
                      password: str = Form(""), password2: str = Form("")):
    denied = form_guard(request, csrf)
    if denied:
        return denied
    if not verify_password(current, db.get_setting("admin_pass_hash", "")) or not 12 <= len(password) <= 1024 or password != password2:
        return RedirectResponse("/settings?notice=当前密码不正确或新密码不符合要求", status_code=302)
    db.set_setting("admin_pass_hash", hash_password(password))
    db.run("DELETE FROM sessions")
    return RedirectResponse("/login", status_code=302)


@app.post("/settings/disconnect")
def settings_disconnect(request: Request, csrf: str = Form("")):
    denied = form_guard(request, csrf)
    if denied:
        return denied
    db.set_setting("github_installation_id", "")
    db.run("UPDATE repositories SET enabled=0,review_required=1,status='paused',control_epoch=control_epoch+1")
    return RedirectResponse("/settings?notice=已断开，所有仓库保持暂停", status_code=302)
