"""GitHub App / REST / GraphQL client (urllib only)."""
import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"


class RateLimited(RuntimeError):
    pass


class RefNotFastForward(RuntimeError):
    pass


class RefNotFound(RuntimeError):
    """The requested ref (branch/tag) does not exist in the repository."""


class Forbidden(RuntimeError):
    """403 on a ref update: missing App permission (e.g. workflows) or blocked."""


class MergeConflict(RuntimeError):
    """merge-upstream 409/422: the fork cannot be merged cleanly."""


class DispatchRejected(RuntimeError):
    """workflow dispatch refused (non-permission): missing trigger, disabled, ..."""

    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class AuthError(RuntimeError):
    pass


class GitHub:
    """Talks to GitHub with an App JWT and cached installation tokens.

    Subclass and override `_request` in tests to script responses.
    """

    def __init__(self, app_id=None, pem_path=None, installation_id=None):
        self.app_id = app_id
        self.pem_path = pem_path
        self.installation_id = installation_id
        self._token = None
        self._token_exp = 0

    # -- transport ---------------------------------------------------------
    def _request(self, method, url, headers, body=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = {"message": raw[:500]}
            return e.code, payload, dict(e.headers)

    # -- app auth ----------------------------------------------------------
    def app_jwt(self):
        pem = Path(self.pem_path).read_text()
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)},
                          pem, algorithm="RS256")

    def installation_token(self):
        if self._token and time.time() < self._token_exp:
            return self._token
        if not (self.app_id and self.pem_path and self.installation_id):
            raise AuthError("GitHub App 尚未配置")
        status, payload, _ = self._request(
            "POST", f"{API}/app/installations/{self.installation_id}/access_tokens",
            {"Authorization": f"Bearer {self.app_jwt()}", "Accept": "application/vnd.github+json"})
        if status != 201:
            raise AuthError(f"获取安装令牌失败: {status} {str(payload)[:200]}")
        self._token = payload["token"]
        self._token_exp = time.time() + 3000
        return self._token

    def reset_token(self):
        self._token = None
        self._token_exp = 0

    # -- REST helpers ------------------------------------------------------
    def _api(self, method, path, body=None, ok=(200, 201), timeout=20):
        status, payload, headers = self._request(
            method, API + path,
            {"Authorization": f"token {self.installation_token()}",
             "Accept": "application/vnd.github+json"}, body, timeout)
        if status in (401, 403) and payload and "rate limit" in str(payload.get("message", "")).lower():
            raise RateLimited(str(payload)[:200])
        if status == 401:
            self.reset_token()
            raise AuthError(f"认证失败: {status} {str(payload)[:200]}")
        if status not in ok:
            raise RuntimeError(f"GitHub {method} {path} -> {status}: {str(payload)[:300]}")
        return status, payload

    def installation_repos(self):
        repos, page = [], 1
        while True:
            _, payload = self._api("GET", f"/installation/repositories?per_page=100&page={page}")
            repos.extend(payload["repositories"])
            if len(payload["repositories"]) < 100:
                return repos
            page += 1

    def get_repo(self, full_name):
        _, payload = self._api("GET", f"/repos/{full_name}")
        return payload

    def get_ref(self, full_name, branch):
        status, payload = self._api("GET", f"/repos/{full_name}/git/ref/heads/{branch}",
                                    ok=(200, 404))
        if status == 404:
            raise RefNotFound(f"{full_name} 没有分支 {branch}")
        return payload["object"]["sha"]

    def update_ref(self, full_name, branch, sha, force=False):
        status, payload, _ = self._request(
            "PATCH", f"{API}/repos/{full_name}/git/refs/heads/{branch}",
            {"Authorization": f"token {self.installation_token()}",
             "Accept": "application/vnd.github+json"}, {"sha": sha, "force": bool(force)})
        if status == 422:
            raise RefNotFastForward(str(payload)[:300])
        if status == 403:
            message = str((payload or {}).get("message", ""))
            if "rate limit" in message.lower():
                raise RateLimited(str(payload)[:200])
            raise Forbidden(str(payload)[:300])
        if status not in (200, 201):
            raise RuntimeError(f"update_ref {full_name} -> {status}: {str(payload)[:300]}")
        return payload["object"]["sha"]

    def merge_upstream(self, full_name, branch):
        """Server-side "Sync fork": merge upstream into the fork, keeping its commits."""
        status, payload, _ = self._request(
            "POST", f"{API}/repos/{full_name}/merge-upstream",
            {"Authorization": f"token {self.installation_token()}",
             "Accept": "application/vnd.github+json"}, {"branch": branch})
        if status == 200:
            return payload or {}
        if status == 409:
            raise MergeConflict(str((payload or {}).get("message", payload))[:200])
        if status == 403:
            message = str((payload or {}).get("message", ""))
            if "rate limit" in message.lower():
                raise RateLimited(str(payload)[:200])
            raise Forbidden(str(payload)[:300])
        if status == 422:
            message = str((payload or {}).get("message", payload))
            if "workflow" in message.lower():
                raise Forbidden(message[:300])
            raise MergeConflict(message[:200])
        raise RuntimeError(f"merge_upstream {full_name} -> {status}: {str(payload)[:300]}")

    def compare(self, fork_full, base, head):
        status, payload = self._api(
            "GET", f"/repos/{fork_full}/compare/{base}...{head}", ok=(200, 404))
        if status == 404:
            # "No common ancestor between ...": unrelated histories.
            return None
        return payload

    def set_actions_enabled(self, full_name, enabled):
        self._api("PUT", f"/repos/{full_name}/actions/permissions",
                  {"enabled": bool(enabled)}, ok=(200, 201, 204))

    def actions_enabled(self, full_name):
        _, payload = self._api("GET", f"/repos/{full_name}/actions/permissions")
        return bool(payload.get("enabled"))

    def workflow_permissions(self, full_name):
        _, payload = self._api("GET", f"/repos/{full_name}/actions/permissions/workflow")
        return payload

    def set_workflow_permissions(self, full_name, mode="write"):
        self._api("PUT", f"/repos/{full_name}/actions/permissions/workflow",
                  {"default_workflow_permissions": mode}, ok=(200, 201, 204))

    def list_workflows(self, full_name):
        _, payload = self._api("GET", f"/repos/{full_name}/actions/workflows?per_page=100")
        return payload.get("workflows", [])

    def workflow_state(self, full_name, workflow_id):
        _, payload = self._api("GET", f"/repos/{full_name}/actions/workflows/{workflow_id}")
        return payload.get("state")

    def enable_workflow(self, full_name, workflow_id):
        status, payload, _ = self._request(
            "PUT", f"{API}/repos/{full_name}/actions/workflows/{workflow_id}/enable",
            {"Authorization": f"token {self.installation_token()}",
             "Accept": "application/vnd.github+json"})
        if status in (200, 204):
            return True
        message = str((payload or {}).get("message", payload))
        if status == 403:
            if "rate limit" in message.lower():
                raise RateLimited(str(payload)[:200])
            raise Forbidden(message[:300])
        raise DispatchRejected(f"{status}: {message[:250]}", status=status)

    def get_file_text(self, full_name, path):
        _, payload = self._api("GET", f"/repos/{full_name}/contents/{path}")
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")

    def recent_runs(self, full_name, per_page=30):
        _, payload = self._api("GET", f"/repos/{full_name}/actions/runs?per_page={per_page}")
        return payload.get("workflow_runs", [])

    def latest_workflow_run(self, full_name, workflow_id):
        _, payload = self._api(
            "GET", f"/repos/{full_name}/actions/workflows/{workflow_id}/runs?per_page=1")
        runs = payload.get("workflow_runs", [])
        return runs[0] if runs else None

    def dispatch_workflow(self, full_name, workflow_id, ref):
        """Trigger a workflow_dispatch run (used for fork self-update)."""
        status, payload, _ = self._request(
            "POST", f"{API}/repos/{full_name}/actions/workflows/{workflow_id}/dispatches",
            {"Authorization": f"token {self.installation_token()}",
             "Accept": "application/vnd.github+json"}, {"ref": ref})
        if status == 204:
            return True
        message = str((payload or {}).get("message", payload))
        if status == 403:
            if "rate limit" in message.lower():
                raise RateLimited(str(payload)[:200])
            raise Forbidden(message[:300])
        raise DispatchRejected(f"{status}: {message[:250]}", status=status)

    def app_installations(self):
        status, payload, _ = self._request(
            "GET", f"{API}/app/installations",
            {"Authorization": f"Bearer {self.app_jwt()}", "Accept": "application/vnd.github+json"})
        if status != 200:
            raise AuthError(f"列出安装失败: {status} {str(payload)[:200]}")
        return payload

    # -- GraphQL -----------------------------------------------------------
    def graphql(self, query, variables):
        status, payload, _ = self._request(
            "POST", GRAPHQL,
            {"Authorization": f"token {self.installation_token()}",
             "Accept": "application/vnd.github+json"}, {"query": query, "variables": variables})
        if status != 200:
            raise RuntimeError(f"GraphQL -> {status}: {str(payload)[:300]}")
        data = payload.get("data")
        if data is None:
            raise RuntimeError(f"GraphQL errors: {str(payload.get('errors'))[:300]}")
        # Partial errors (e.g. one node id unresolvable) still return usable
        # data with nulls at the failed positions; callers skip null nodes.
        return data

    # -- manifest ----------------------------------------------------------
    def exchange_manifest(self, code):
        status, payload, _ = self._request(
            "POST", f"{API}/app-manifests/{code}/conversions",
            {"Accept": "application/vnd.github+json"})
        if status not in (200, 201):
            raise AuthError(f"manifest 兑换失败: {status} {str(payload)[:300]}")
        return payload


def write_pem(path, pem):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(pem)
    os.chmod(str(p), 0o600)


def exchange_manifest(code):
    """Exchange a manifest code for App credentials (no auth required)."""
    return GitHub().exchange_manifest(code)
