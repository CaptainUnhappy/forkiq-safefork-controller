"""Read-only GitHub metadata plus explicit App credential bootstrap."""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
import jwt

API = "https://api.github.com"


class AuthError(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, app_id=None, pem_path=None, installation_id=None, token=None):
        self.app_id, self.pem_path, self.installation_id = app_id, pem_path, installation_id
        self.source_token = token
        self._token, self._token_exp = None, 0

    def _request(self, method, url, headers, body=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise RuntimeError("GitHub 返回数据超过安全上限。")
                return response.status, json.loads(raw) if raw else None, dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, {}, dict(error.headers)
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("无法连接 GitHub，请检查网络。") from None

    def app_jwt(self):
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)},
                          Path(self.pem_path).read_text(), algorithm="RS256")

    def installation_token(self):
        if self.source_token is not None:
            return self.source_token
        if self._token and time.time() < self._token_exp:
            return self._token
        if not (self.app_id and self.pem_path and self.installation_id):
            raise AuthError("GitHub App 尚未配置。")
        status, payload, _ = self._request("POST", f"{API}/app/installations/{self.installation_id}/access_tokens",
                                         {"Authorization": f"Bearer {self.app_jwt()}"})
        if status != 201:
            raise AuthError(f"获取安装令牌失败（HTTP {status}）。")
        self._token, self._token_exp = payload["token"], time.time() + 3000
        return self._token

    def _api(self, method, path, body=None):
        if method != "GET" or body is not None or not path.startswith("/"):
            raise RuntimeError("SafeFork 元数据客户端只允许 GET；引用写入须经过安全计划。")
        status, payload, headers = self._request(method, API + path,
                                                {"Authorization": f"Bearer {self.installation_token()}"})
        if status == 429 or (status == 403 and headers.get("X-RateLimit-Remaining") == "0"):
            raise RateLimited("GitHub 限流，已停写。")
        if status in (401, 403, 404):
            raise AuthError(f"GitHub 无法读取已登记仓库或权限（HTTP {status}）；不会改用其他来源。")
        if status != 200 or not isinstance(payload, (dict, list)):
            raise RuntimeError(f"GitHub 读取失败（HTTP {status}）。")
        return payload

    def get_repo(self, full_name):
        from .safefork import repo_name
        return self._api("GET", f"/repos/{repo_name(full_name)}")

    def actions_enabled(self, full_name):
        payload = self._api("GET", f"/repos/{full_name}/actions/permissions")
        if type(payload.get("enabled")) is not bool:
            raise RuntimeError("Actions 开关状态不完整，拒绝推送。")
        return payload["enabled"]

    def has_active_runs(self, full_name):
        for state in ("queued", "in_progress", "waiting", "pending", "requested"):
            payload = self._api("GET", f"/repos/{full_name}/actions/runs?status={state}&per_page=1")
            if type(payload.get("total_count")) is not int or not isinstance(payload.get("workflow_runs"), list):
                raise RuntimeError("Actions 运行状态不完整，拒绝推送。")
            if payload["total_count"] or payload["workflow_runs"]:
                return True
        return False

    def app_installations(self):
        status, payload, _ = self._request("GET", f"{API}/app/installations?per_page=100",
                                         {"Authorization": f"Bearer {self.app_jwt()}"})
        if status != 200 or not isinstance(payload, list) or len(payload) >= 100:
            raise AuthError("无法完整读取 GitHub App 安装列表。")
        return payload


def write_pem(path, pem):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(pem)
    p.chmod(0o600)


def exchange_manifest(code):
    if not code.isalnum():
        raise AuthError("授权码无效。")
    status, payload, _ = GitHub()._request("POST", f"{API}/app-manifests/{code}/conversions", {})
    if status not in (200, 201):
        raise AuthError(f"manifest 兑换失败（HTTP {status}）。")
    return payload
