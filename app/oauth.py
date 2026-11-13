"""User OAuth for public-Fork provisioning; never a Git transport credential."""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from .github import GitHub, API
from .gitstore import SyncBlocked
from .safefork import repo_name

_lock = threading.RLock()
TOKEN_URL = "https://github.com/login/oauth/access_token"
CALLBACK = "/oauth/callback"


class OAuthStore:
    def __init__(self, root):
        self.path = Path(root) / "oauth-credentials.json"

    def read(self):
        if self.path.is_symlink():
            raise SyncBlocked("OAuth 凭据路径不安全。")
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > 65536:
            raise SyncBlocked("OAuth 凭据文件无效。")
        if os.name != "nt" and self.path.stat().st_mode & 0o077:
            raise SyncBlocked("OAuth 凭据文件权限必须为 0600。")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (OSError, ValueError):
            raise SyncBlocked("OAuth 配置无法读取，请重新配置。") from None

    def write(self, data):
        with _lock:
            # Random, exclusive sibling avoids following a pre-existing temporary link.
            tmp = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(data, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, self.path)
                self.path.chmod(0o600)
            finally:
                if tmp.exists():
                    tmp.unlink()

    def configure(self, client_id, secret):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{10,100}", client_id) or not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", secret):
            raise SyncBlocked("Client ID 或 Client Secret 格式不正确。")
        self.write({"client_id": client_id, "client_secret": secret, "generation": secrets.token_hex(16)})

    def status(self):
        data = self.read()
        return {"configured": bool(data.get("client_id") and data.get("client_secret")),
                "connected": bool(data.get("access_token")), "login": data.get("login", "")}

    def disconnect(self):
        with _lock:
            data = self.read()
            self.write({**{k: data[k] for k in ("client_id", "client_secret") if k in data}, "generation": secrets.token_hex(16)})


def begin(db, store, session, base):
    data = store.read()
    if not data.get("client_id") or not data.get("client_secret"):
        raise SyncBlocked("请先配置用于创建仓库的 OAuth 应用。")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    session_hash = hashlib.sha256(session.encode()).hexdigest()
    with db.transaction() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at<? OR session_hash=?", (time.time(), session_hash))
        conn.execute("INSERT INTO oauth_states VALUES(?,?,?,?)",
                     (hashlib.sha256(state.encode()).hexdigest(), session_hash, verifier, time.time() + 600))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return "https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": data["client_id"], "redirect_uri": base + CALLBACK, "scope": "repo",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def consume_state(db, state, session):
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,100}", state):
        raise SyncBlocked("授权状态无效或已过期，请重新授权。")
    with db.transaction() as conn:
        hashed = hashlib.sha256(state.encode()).hexdigest()
        row = conn.execute("SELECT * FROM oauth_states WHERE state_hash=?", (hashed,)).fetchone()
        if (not row or row["expires_at"] < time.time()
                or row["session_hash"] != hashlib.sha256(session.encode()).hexdigest()):
            raise SyncBlocked("授权状态无效或已过期，请重新授权。")
        conn.execute("DELETE FROM oauth_states WHERE state_hash=?", (hashed,))
        return row["verifier"]


def token_exchange(body):
    status, payload, _ = GitHub()._request("POST", TOKEN_URL, {"Accept": "application/json"}, body)
    if (status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str)
            or not re.fullmatch(r"[A-Za-z0-9_\-]{20,16384}", payload["access_token"])
            or "repo" not in re.split(r"[ ,]+", payload.get("scope", ""))
            or payload.get("token_type", "").lower() != "bearer"):
        raise SyncBlocked("GitHub 授权未完成或缺少 repo 权限，请重新授权。")
    return payload


def token_fields(payload):
    result = {"access_token": payload["access_token"], "expires_at": 0}
    if payload.get("expires_in"):
        result["expires_at"] = time.time() + int(payload["expires_in"])
    if payload.get("refresh_token"):
        result["refresh_token"] = payload["refresh_token"]
    return result


def installation_owner(app, installation_id):
    if app is None:
        raise SyncBlocked("请先连接专用 GitHub App。")
    matches = [i for i in app.app_installations() if str(i.get("id")) == str(installation_id)]
    if len(matches) != 1 or matches[0].get("repository_selection") != "all" or matches[0].get("suspended_at"):
        raise SyncBlocked("创建功能需要现有 GitHub App 安装覆盖新目标（All repositories），且未被停用。")
    account = matches[0].get("account", {})
    if account.get("type") != "User" or type(account.get("id")) is not int:
        raise SyncBlocked("本入口仅为已连接的个人账号创建公开 Fork。")
    repo_name(account.get("login", "") + "/check")
    return account


def complete(db, store, session, code, state, base, app, installation_id):
    verifier = consume_state(db, state, session)
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", code):
        raise SyncBlocked("授权码无效，请重新授权。")
    with _lock:
        data = store.read()
        payload = token_exchange({"client_id": data["client_id"], "client_secret": data["client_secret"],
                                  "code": code, "code_verifier": verifier, "redirect_uri": base + CALLBACK})
        user = ProvisionGitHub(payload["access_token"]).user()
        owner = installation_owner(app, installation_id)
        if user.get("id") != owner["id"] or user.get("login") != owner["login"]:
            raise SyncBlocked("授权账号与 SafeFork App 的安装账号不一致，请使用相同账号。")
        store.write({"client_id": data["client_id"], "client_secret": data["client_secret"],
                     **token_fields(payload), "account_id": owner["id"], "login": owner["login"],
                     "generation": secrets.token_hex(16)})


def authorized_client(store):
    with _lock:
        data = store.read()
        if not data.get("access_token"):
            raise SyncBlocked("请先在设置页完成 GitHub 网页授权。")
        if data.get("expires_at") and data["expires_at"] < time.time() + 120:
            if not data.get("refresh_token"):
                raise SyncBlocked("GitHub 授权已过期，请重新授权。")
            payload = token_exchange({"client_id": data["client_id"], "client_secret": data["client_secret"],
                                      "grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
            data = {**data, **token_fields(payload)}
            store.write(data)
        client = ProvisionGitHub(data["access_token"])
        client.generation = data.get("generation")
        user = client.user()
        if user.get("id") != data.get("account_id") or user.get("login") != data.get("login"):
            raise SyncBlocked("GitHub 授权账号发生变化。")
        return client, user


class ProvisionGitHub:
    """Only fixed, reviewed initialization endpoints; not used by GitStore."""
    def __init__(self, token):
        self.token = token

    def _call(self, method, path, body=None, missing=False, expected=200):
        status, payload, _ = GitHub()._request(method, API + path, {"Authorization": "Bearer " + self.token}, body)
        if status == 404 and missing and method == "GET":
            return None
        if status != expected:
            raise SyncBlocked(f"GitHub 初始化操作未确认（HTTP {status}），请核对授权与任务阶段。")
        return payload

    def user(self):
        return self._call("GET", "/user")

    def repo(self, name, missing=False):
        return self._call("GET", "/repos/" + repo_name(name), missing=missing)

    def create_fork(self, source, name):
        repo_name("owner/" + name)
        return self._call("POST", "/repos/" + repo_name(source) + "/forks", {"name": name, "default_branch_only": False}, expected=202)

    def isolate(self, target):
        self._call("PUT", "/repos/" + repo_name(target) + "/actions/permissions", {"enabled": False}, expected=204)

    def default_branch(self, target, branch):
        from .safefork import valid_ref
        if not valid_ref("refs/heads/" + branch):
            raise SyncBlocked("默认分支名称无效。")
        self._call("PATCH", "/repos/" + repo_name(target), {"default_branch": branch})
