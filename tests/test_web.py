import atexit
import gc
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from html.parser import HTMLParser
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from app.db import DB, insert_repository, utcnow
from test_safefork import config

_import_state = tempfile.TemporaryDirectory()
with patch.dict(os.environ, FORKIQ_STATE_DIR=_import_state.name):
    from app import main
main.db.close()
atexit.register(_import_state.cleanup)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.temp.name) / "db.sqlite")
        self.worker = Mock(last_tick=time.monotonic(), busy_since=0)
        self.worker.is_alive.return_value = True
        self.patches = [patch.object(main, "db", self.db), patch.object(main, "worker", self.worker)]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app)
        self.client.__enter__()
        main._login_failures.clear()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db.close()
        gc.collect()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def login(self):
        self.db.set_setting("setup_done", "1")
        self.db.set_setting("admin_pass_hash", main.hash_password("test-password-123"))
        response = self.client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        return main.csrf_for(self.client.cookies.get("forkiq_session"))

    def repo(self):
        repo = insert_repository(self.db, owner="owner", name="project-SafeFork", full_name="owner/project-SafeFork",
                                 upstream_full_name="upstream/project", status="paused")
        self.db.run("UPDATE repositories SET config_json=? WHERE id=?", (json.dumps(config()), repo))
        return repo

    def reviewed(self, repo):
        plan = json.dumps(dict(checked_at=utcnow(), decisions=[], target_only=[], updates={}), sort_keys=True)
        self.db.run("UPDATE repositories SET plan_json=?,last_check_at=?,review_required=0 WHERE id=?", (plan, utcnow(), repo))
        return main.review_token(plan)

    def test_unauthenticated_does_not_expose_repos_or_mutate(self):
        response = self.client.post("/repos/1/preview", data={}, follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (302, "/setup"))
        self.assertEqual(self.client.get("/partials/repos").status_code, 401)

    def test_setup_minimum_and_one_time(self):
        response = self.client.post("/setup", data={"password": "short", "password2": "short"})
        self.assertIn("12", response.text)
        self.assertIsNone(self.db.get_setting("setup_done"))
        response = self.client.post("/setup", data={"password": "test-password-123", "password2": "test-password-123"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login")
        before = self.db.get_setting("admin_pass_hash")
        self.client.post("/setup", data={"password": "other-password-123", "password2": "other-password-123"})
        self.assertEqual(self.db.get_setting("admin_pass_hash"), before)

    def test_login_throttled_and_cookie_not_javascript_accessible(self):
        self.login()
        self.client.cookies.clear()
        for _ in range(5):
            self.client.post("/login", data={"password": "wrong"})
        self.assertIn("尝试过多", self.client.post("/login", data={"password": "test-password-123"}).text)
        main._login_failures.clear()
        response = self.client.post("/login", data={"password": "test-password-123"}, follow_redirects=False)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])

    def test_concurrent_setup_cannot_replace_first_password(self):
        first_hash = main.hash_password("first-password-123")
        own_hash = main.hash_password("second-password-123")
        def first_request_finished(password):
            self.db.set_setting("admin_pass_hash", first_hash)
            self.db.set_setting("setup_done", "1")
            return own_hash
        with patch.object(main, "hash_password", side_effect=first_request_finished):
            response = self.client.post("/setup", data={"password": "second-password-123", "password2": "second-password-123"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db.get_setting("admin_pass_hash"), first_hash)

    def test_all_writes_require_csrf(self):
        self.login()
        repo = self.repo()
        for path in (f"/repos/{repo}/pause", f"/repos/{repo}/preview", f"/repos/{repo}/resume",
                     "/repos/add", "/settings", "/settings/password", "/settings/disconnect", "/github/detect", "/logout",
                     "/create", "/create/1/retry", "/oauth/configure", "/oauth/authorize", "/oauth/disconnect"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 403)

    def test_creation_pages_require_login_and_secrets_are_never_echoed(self):
        for url in ("/create", "/oauth", "/create/1"):
            self.assertEqual(self.client.get(url, follow_redirects=False).status_code, 302)
        self.assertEqual(self.client.get("/partials/creation/1").status_code, 401)
        csrf = self.login()
        self.client.post("/oauth/configure", data={"csrf": csrf, "client_id": "test_client_id",
                                                 "client_secret": "fixture-secret-" + "a"*30}, follow_redirects=False)
        with patch.dict(os.environ, FORKIQ_PUBLIC_URL="https://example.test/SafeFork"):
            page = self.client.get("/oauth")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("fixture-secret", page.text)
        self.assertIn("https://example.test/SafeFork/oauth/callback", page.text)
        self.assertEqual(page.headers["referrer-policy"], "no-referrer")
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("新建 SafeFork", self.client.get("/repos").text)

    def test_creation_submit_queues_once_and_redirects_to_progress(self):
        csrf = self.login()
        with patch.object(main.provision, "enqueue", return_value=3) as enqueue:
            response = self.client.post("/create", data={"csrf": csrf, "source": "upstream/project"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/create/3")
        enqueue.assert_called_once_with(self.db, "upstream/project")
        self.worker.request_sync.assert_called_once()

    def test_preview_pauses_and_queues_only_read(self):
        csrf = self.login()
        repo = self.repo()
        response = self.client.post(f"/repos/{repo}/preview", data={"csrf": csrf}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        row = self.db.one("SELECT * FROM repositories")
        self.assertEqual((row["enabled"], row["dry_run_requested"], row["control_epoch"]), (0, 1, 1))

    def test_resume_requires_recent_matching_preview(self):
        csrf = self.login()
        repo = self.repo()
        path = f"/repos/{repo}/resume"
        self.assertEqual(self.client.post(path, data={"csrf": csrf}).status_code, 409)
        review = self.reviewed(repo)
        self.assertEqual(self.client.post(path, data={"csrf": csrf, "review": "wrong"}).status_code, 409)
        self.db.run("UPDATE repositories SET last_check_at='2001-01-01T00:00:00+00:00'")
        self.assertEqual(self.client.post(path, data={"csrf": csrf, "review": review}).status_code, 409)
        review = self.reviewed(repo)
        response = self.client.post(path, data={"csrf": csrf, "review": review}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        row = self.db.one("SELECT * FROM repositories")
        self.assertEqual((row["enabled"], row["sync_requested"]), (1, 1))

    def test_disabled_dangerous_routes_are_not_available(self):
        csrf = self.login()
        repo = self.repo()
        for action in ("mode", "selfupdate", "actions", "remove", "restore", "confirm-deletion"):
            with self.subTest(action=action):
                response = self.client.post(f"/repos/{repo}/{action}", data={"csrf": csrf})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(f"/repos/{repo}/backups/1/restore", data={"csrf": csrf}).status_code, 404)
        self.assertEqual(self.client.post("/repos/discover", data={"csrf": csrf}).status_code, 405)

    def test_pause_and_disconnect_invalidate_old_tasks(self):
        csrf = self.login()
        repo = self.repo()
        self.client.post(f"/repos/{repo}/pause", data={"csrf": csrf})
        self.client.post("/settings/disconnect", data={"csrf": csrf})
        row = self.db.one("SELECT * FROM repositories")
        self.assertEqual((row["enabled"], row["review_required"], row["control_epoch"]), (0, 1, 2))

    def test_pages_render_escape_errors_and_remove_unsafe_controls(self):
        self.login()
        repo = self.repo()
        self.db.run("UPDATE repositories SET last_error='<script>bad()</script>'")
        for path in ("/", "/repos", f"/repos/{repo}", "/settings", "/github/connect"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<script>bad()", response.text)
                self.assertNotIn('action="/repos/discover"', response.text)
                self.assertNotIn(f'action="/repos/{repo}/restore"', response.text)
        self.assertIn("&lt;script&gt;", self.client.get(f"/repos/{repo}").text)

    def test_incremental_refresh_revision(self):
        self.login()
        repo = self.repo()
        revision = main._repo_list_context()["revision"]
        self.assertEqual(self.client.get("/partials/repos?v=" + revision).status_code, 204)
        self.db.run("UPDATE repositories SET status='needs_attention' WHERE id=?", (repo,))
        self.assertEqual(self.client.get("/partials/repos?v=" + revision).status_code, 200)

    def test_worker_health_reports_death_and_stale_busy_state(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.worker.is_alive.return_value = False
        self.assertEqual(self.client.get("/healthz").status_code, 503)
        self.worker.is_alive.return_value = True
        self.worker.busy_since = time.monotonic() - 3601
        self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_manifest_no_actions_or_administration_write(self):
        with patch.dict(os.environ, FORKIQ_PUBLIC_URL="https://controller.example.invalid"):
            _, manifest = main.manifest_payload()
        self.assertEqual(manifest["default_permissions"], {"contents": "write", "workflows": "write", "metadata": "read",
                                                          "administration": "read", "actions": "read"})
        self.assertFalse(manifest["hook_attributes"]["active"])
        self.assertEqual(manifest["default_events"], [])

    def test_invalid_origin_and_manifest_callback_rejected(self):
        self.login()
        for value in ("http://example.invalid", "https://user:pass@example.invalid", "https://x.invalid?q=1",
                      "https://x.invalid//SafeFork", "https://x.invalid/a/../b", "https://x.invalid/%2fother",
                      "https://x.invalid/a\\b", "https://x.invalid/a/./b"):
            with patch.dict(os.environ, FORKIQ_PUBLIC_URL=value), self.assertRaises(ValueError):
                main.public_url()
        with patch.object(main, "exchange_manifest") as exchange:
            self.assertEqual(self.client.get("/api/github/callback?code=abc&state=wrong").status_code, 400)
            exchange.assert_not_called()

    def test_password_change_revokes_sessions(self):
        csrf = self.login()
        response = self.client.post("/settings/password", data={"csrf": csrf, "current": "test-password-123",
                                    "password": "changed-password-123", "password2": "changed-password-123"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM sessions")["n"], 0)

    def test_subpath_manifest(self):
        for address in ("https://controller.example.invalid/SafeFork", "https://controller.example.invalid/SafeFork/"):
            with patch.dict(os.environ, FORKIQ_PUBLIC_URL=address):
                _, manifest = main.manifest_payload()
            self.assertEqual(manifest["url"], address.rstrip("/"))
            self.assertEqual(manifest["redirect_url"], address.rstrip("/") + "/api/github/callback")
            self.assertEqual(manifest["callback_urls"], [manifest["redirect_url"]])

    def test_subpath_pages_assets_forms_polling_and_session_isolation(self):
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.urls = []
            def handle_starttag(self, tag, attrs):
                self.urls.extend(value for key, value in attrs if key in ("href", "src", "action", "data-refresh-url") and value)

        prefix = "/SafeFork"
        self.db.set_setting("setup_done", "1")
        self.db.set_setting("admin_pass_hash", main.hash_password("test-password-123"))
        # A root-deployment cookie must not shadow the new path-scoped session.
        self.client.cookies.set("forkiq_session", "stale-root-session")
        with patch.object(main.app, "root_path", prefix):
            response = self.client.get(prefix + "/repos", follow_redirects=False)
            self.assertEqual(response.headers["location"], prefix + "/login")
            response = self.client.post(prefix + "/login", data={"password": "test-password-123"}, follow_redirects=False)
            self.assertEqual(response.headers["location"], prefix + "/")
            self.assertIn("Path=/SafeFork/", response.headers["set-cookie"])
            token = next(iter(response.cookies.values()))
            csrf = main.csrf_for(token)
            repo = self.repo()
            self.reviewed(repo)
            for path in ("/", "/repos", f"/repos/{repo}", "/settings", "/github/connect", "/login", "/partials/repos", "/partials/overview"):
                response = self.client.get(prefix + path)
                self.assertEqual(response.status_code, 200, path)
                parser = Links()
                parser.feed(response.text)
                for url in parser.urls:
                    if url.startswith("/"):
                        self.assertTrue(url.startswith(prefix + "/"), (path, url))
                self.assertNotIn('href="/static/', response.text)
            for asset in ("app.css", "app.js", "icons.svg", "favicon.svg"):
                self.assertEqual(self.client.get(prefix + "/static/" + asset).status_code, 200)
            self.assertEqual(self.client.post(prefix + "/settings", data={"interval_minutes": "60"}).status_code, 403)
            response = self.client.post(prefix + "/settings", data={"csrf": csrf, "interval_minutes": "60"}, follow_redirects=False)
            self.assertTrue(response.headers["location"].startswith(prefix + "/settings?"))
            revision = main._repo_list_context()["revision"]
            self.assertEqual(self.client.get(prefix + "/partials/repos?v=" + revision).status_code, 204)
            response = self.client.post(prefix + "/logout", data={"csrf": csrf}, follow_redirects=False)
            self.assertEqual(response.headers["location"], prefix + "/login")
            self.assertIn("Path=/SafeFork/", response.headers["set-cookie"])
            self.assertEqual(self.client.get(prefix + "/partials/repos").status_code, 401)

    def test_subpath_auth_callback_and_forwarded_prefix_header(self):
        self.login()
        response = self.client.get("/repos", headers={"x-forwarded-prefix": "//attacker.invalid"})
        self.assertNotIn("attacker.invalid", response.text)
        with patch.object(main.app, "root_path", "/SafeFork"), patch.object(main, "exchange_manifest") as exchange:
            response = self.client.get("/api/github/callback?code=invalid&state=wrong", follow_redirects=False)
            self.assertEqual(response.headers["location"], "/SafeFork/login")
            exchange.assert_not_called()

    def test_empty_state_explains_connection_is_not_registration(self):
        self.login()
        self.db.set_setting("github_installation_id", "123")
        for path in ("/", "/repos", "/partials/overview", "/partials/repos"):
            response = self.client.get(path)
            self.assertIn("尚未登记仓库", response.text)
            self.assertIn("GitHub Actions", response.text)
            self.assertNotIn("连接 GitHub 后会自动开始检查", response.text)


if __name__ == "__main__":
    unittest.main()
