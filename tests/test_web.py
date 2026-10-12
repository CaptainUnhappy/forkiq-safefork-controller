"""Web layer tests with FastAPI TestClient and a stub GitHub client."""
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import engine
from app.github import RefNotFastForward

TEST_PASSWORD = "correct-horse-8"


class RenderedHTML(HTMLParser):
    """Inspect rendered form ownership and labels without browser dependencies."""
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "parents": tuple(self.stack), "text": ""}
        self.elements.append(element)
        if tag not in self.VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element["text"] += data

    def find(self, tag, **attrs):
        return [element for element in self.elements if element["tag"] == tag
                and all(element["attrs"].get(key) == value for key, value in attrs.items())]

    def inside(self, parent, tag, **attrs):
        return [element for element in self.find(tag, **attrs)
                if any(ancestor is parent for ancestor in element["parents"])]

    def labels_for(self, field):
        return [label for label in self.find("label") if label["text"].strip()
                and (any(ancestor is label for ancestor in field["parents"])
                     or (field["attrs"].get("id")
                         and label["attrs"].get("for") == field["attrs"]["id"]))]


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        import os
        os.environ["FORKIQ_STATE_DIR"] = cls.tmp.name
        os.environ["FORKIQ_PEM_PATH"] = str(Path(cls.tmp.name) / "app.pem")
        from app import main as main_module
        cls.main = main_module
        cls.main.STATE_DIR = Path(cls.tmp.name)
        cls.main.PEM_PATH = Path(cls.tmp.name) / "app.pem"
        cls.main.db = main_module.DB(Path(cls.tmp.name) / "forkiq.db")

    def setUp(self):
        self.main.db.run("DELETE FROM sessions")
        self.main.db.run("DELETE FROM repositories")
        self.main.db.run("DELETE FROM events")
        self.main.db.run("DELETE FROM settings")
        self.main._login_failures.clear()
        self.client = self._test_client()

    @classmethod
    def _test_client(cls):
        from fastapi.testclient import TestClient
        return TestClient(cls.main.app, follow_redirects=False)

    def setup_admin(self):
        self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        self.client.post("/login", data={"password": TEST_PASSWORD})
        session = self.main.db.one("SELECT token FROM sessions ORDER BY created_at DESC LIMIT 1")
        return session["token"]

    def csrf(self, token):
        return self.main.csrf_for(token)

    def rendered_page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response, RenderedHTML(response.text)

    def assert_post_form(self, page, action, token=None):
        forms = page.find("form", action=action)
        self.assertEqual(len(forms), 1, action)
        form = forms[0]
        self.assertEqual(form["attrs"].get("method", "get").lower(), "post", action)
        if token is not None:
            csrf_fields = page.inside(form, "input", name="csrf")
            self.assertEqual(len(csrf_fields), 1, action)
            self.assertEqual(csrf_fields[0]["attrs"].get("type"), "hidden")
            self.assertEqual(csrf_fields[0]["attrs"].get("value"), self.csrf(token))
        return form

    def assert_page_accessibility(self, page):
        self.assertEqual(len(page.find("main", id="main-content")), 1)
        self.assertTrue(page.find("a", href="#main-content"))
        self.assertTrue(any(urlsplit(script["attrs"].get("src", "")).path == "/static/app.js"
                            for script in page.find("script")))

    def test_login_renders_password_only_with_accessible_label(self):
        self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        response, page = self.rendered_page("/login")
        self.assert_page_accessibility(page)
        form = self.assert_post_form(page, "/login")
        fields = page.inside(form, "input")
        self.assertEqual([field["attrs"].get("name") for field in fields], ["password"])
        password = fields[0]
        self.assertEqual(password["attrs"].get("type"), "password")
        self.assertEqual(password["attrs"].get("autocomplete"), "current-password")
        self.assertFalse(password["attrs"].get("value"))
        self.assertTrue(any("管理员密码" in label["text"] for label in page.labels_for(password)))
        self.assertNotIn(TEST_PASSWORD, response.text)

    def test_shell_pages_render_current_navigation_and_preserve_actions(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        routes = [("/", "/", ["/discover"]),
                  ("/repos", "/repos", ["/repos/add"]),
                  (f"/repos/{repo_id}", "/repos",
                   [f"/repos/{repo_id}/{action}" for action in
                    ("sync", "mode", "selfupdate", "pause", "actions", "remove")]),
                  ("/settings", "/settings",
                   ["/settings", "/settings/password", "/settings/disconnect"])]
        for path, active_path, actions in routes:
            with self.subTest(path=path):
                _, page = self.rendered_page(path)
                self.assert_page_accessibility(page)
                current = [link for link in page.find("a", **{"aria-current": "page"})
                           if any(parent["tag"] == "nav" for parent in link["parents"])]
                self.assertEqual([link["attrs"].get("href") for link in current], [active_path])
                self.assert_post_form(page, "/logout")
                for action in actions:
                    self.assert_post_form(page, action, token)

    def test_repository_list_labels_url_and_wraps_table(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo",
                                    status="blocked")
        self.main.db.run("UPDATE repositories SET last_error=? WHERE id=?",
                         ("GitHub denied <unsafe>", repo_id))
        response, page = self.rendered_page("/repos?filter=blocked")
        form = self.assert_post_form(page, "/repos/add")
        fields = page.inside(form, "input", name="url")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["attrs"].get("type"), "url")
        self.assertTrue(page.labels_for(fields[0]))
        self.assertTrue(page.find("a", href=f"/repos/{repo_id}"))
        tables = page.find("table")
        self.assertTrue(tables)
        for table in tables:
            self.assertTrue(any("table-wrap" in parent["attrs"].get("class", "").split()
                                for parent in table["parents"]))
        self.assertIn("GitHub denied &lt;unsafe&gt;", response.text)
        self.assertNotIn("<unsafe>", response.text)

    def test_repository_and_upstream_names_link_to_github(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.run("INSERT INTO events(repo_id, at, kind, message) VALUES(?,?,?,?)",
                         (repo_id, "2026-01-01T00:00:00+00:00", "synced", "me/demo 已与上游一致"))
        # The upstream name is a GitHub link everywhere it is shown.
        for path in ("/repos", f"/repos/{repo_id}"):
            with self.subTest(path=path, target="up/demo"):
                _, page = self.rendered_page(path)
                links = page.find("a", href="https://github.com/up/demo")
                self.assertEqual(len(links), 1, path)
                self.assertEqual(links[0]["attrs"].get("target"), "_blank")
                self.assertIn("noopener", links[0]["attrs"].get("rel", ""))
        # On the overview event log the repository column also links to GitHub.
        _, page = self.rendered_page("/")
        repo_links = page.find("a", href="https://github.com/me/demo")
        self.assertEqual(len(repo_links), 1)
        self.assertEqual(repo_links[0]["attrs"].get("target"), "_blank")
        # The repository list keeps its internal detail link and adds GitHub links.
        _, page = self.rendered_page("/repos")
        self.assertTrue(page.find("a", href=f"/repos/{repo_id}"))
        fork_links = page.find("a", href="https://github.com/me/demo")
        self.assertEqual(len(fork_links), 1)
        self.assertEqual(fork_links[0]["attrs"].get("target"), "_blank")
        # The detail page exposes both names.
        _, page = self.rendered_page(f"/repos/{repo_id}")
        self.assertTrue(page.find("a", href="https://github.com/me/demo"))
        self.assertTrue(page.find("a", href="https://github.com/up/demo"))

    def test_github_links_omit_missing_names(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name=None)
        for path in ("/repos", f"/repos/{repo_id}"):
            with self.subTest(path=path):
                response, page = self.rendered_page(path)
                # The fork itself still links; only the missing upstream is a dash.
                self.assertEqual(len(page.find("a", href="https://github.com/me/demo")), 1)
                self.assertFalse([link for link in page.find("a")
                                  if link["attrs"].get("href") == "https://github.com/None"])
                self.assertIn("—", response.text)

    def test_repository_detail_preserves_paused_actions_and_hidden_enabled(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo",
                                    status="paused")
        self.main.db.run("UPDATE repositories SET enabled=0, actions_disabled=1, "
                         "mode='manual', self_update_interval=1 WHERE id=?", (repo_id,))
        _, page = self.rendered_page(f"/repos/{repo_id}")
        self.assert_post_form(page, f"/repos/{repo_id}/resume", token)
        self.assertFalse(page.find("form", action=f"/repos/{repo_id}/pause"))
        actions_form = self.assert_post_form(page, f"/repos/{repo_id}/actions", token)
        self.assertEqual(page.inside(actions_form, "input", name="enabled")[0]["attrs"]["value"], "1")
        self_update_form = self.assert_post_form(page, f"/repos/{repo_id}/selfupdate", token)
        self.assertIn("关闭自更新", self_update_form["text"])
        remove_form = self.assert_post_form(page, f"/repos/{repo_id}/remove", token)
        self.assertTrue(remove_form["attrs"].get("data-confirm"))

    def test_settings_fields_keep_labels_values_and_notices(self):
        token = self.setup_admin()
        self.main.db.set_setting("check_interval", "90")
        self.main.db.set_setting("auto_discover", "0")
        self.main.db.set_setting("auth_error", "GitHub credentials expired")
        response, page = self.rendered_page("/settings?notice=Saved")
        self.assertIn("Saved", response.text)
        self.assertIn("GitHub credentials expired", response.text)
        form = self.assert_post_form(page, "/settings", token)
        interval = page.inside(form, "input", name="interval")[0]
        self.assertEqual(interval["attrs"].get("value"), "90")
        auto_discover = page.inside(form, "input", name="auto_discover")[0]
        self.assertNotIn("checked", auto_discover["attrs"])
        password_form = self.assert_post_form(page, "/settings/password", token)
        fields = [interval, auto_discover] + page.inside(password_form, "input", type="password")
        self.assertEqual(len(fields), 5)
        for field in fields:
            self.assertTrue(page.labels_for(field), field["attrs"].get("name"))
        self.assertNotIn(TEST_PASSWORD, response.text)

    def test_connection_and_installation_keep_manifest_and_detect_forms(self):
        token = self.setup_admin()
        _, page = self.rendered_page("/github/connect")
        self.assert_page_accessibility(page)
        manifest_forms = page.find("form", id="manifest-form")
        self.assertEqual(len(manifest_forms), 1)
        form = manifest_forms[0]
        state = self.main.db.get_setting("github_manifest_state")
        self.assertEqual(form["attrs"]["action"], f"{self.main.MANIFEST_URL}?state={state}")
        self.assertEqual(form["attrs"].get("method"), "post")
        import json
        payload = json.loads(page.inside(form, "input", name="manifest")[0]["attrs"]["value"])
        self.assertEqual(payload["redirect_url"], "https://fork.llkan.com/api/github/callback")
        self.assertEqual(payload["default_permissions"]["workflows"], "write")
        app_payload = {"id": 123, "slug": "forkiq-test", "pem": "test-pem"}
        with mock.patch.object(self.main, "exchange_manifest", return_value=app_payload), \
             mock.patch.object(self.main, "write_pem"):
            response = self.client.get(f"/api/github/callback?code=test-code&state={state}")
        self.assertEqual(response.status_code, 200)
        installation_page = RenderedHTML(response.text)
        self.assert_page_accessibility(installation_page)
        self.assertTrue(installation_page.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
        self.assert_post_form(installation_page, "/github/detect", token)
        _, connected = self.rendered_page("/github/connect")
        self.assertTrue(connected.find("a", href="/"))

    def test_empty_and_error_pages_remain_actionable(self):
        self.setup_admin()
        self.main.db.run("DELETE FROM events")
        response, _ = self.rendered_page("/")
        self.assertIn("还没有事件", response.text)
        self.assertIn('/github/connect', response.text)
        response, page = self.rendered_page("/repos")
        self.assertIn("还没有受管仓库", response.text)
        self.assert_post_form(page, "/repos/add")
        response, page = self.rendered_page("/repos/99999")
        self.assertIn("未找到", response.text)
        self.assertIn("仓库不在管理列表中", response.text)
        self.assert_page_accessibility(page)
        self.assertTrue(page.find("a", href="/"))

    def test_setup_requires_matching_passwords_and_min_length(self):
        response = self.client.post("/setup", data={"password": "short", "password2": "short"})
        self.assertIn("至少 8 位", response.text)
        response = self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": "other-1234"})
        self.assertIn("不一致", response.text)
        response = self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("setup_done"), "1")

    def test_requires_login_after_setup(self):
        self.setup_admin()
        self.client.cookies.clear()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")

    def test_login_failure_and_rate_limit(self):
        self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        for _ in range(5):
            response = self.client.post("/login", data={"password": "wrong-pass"})
            self.assertIn("不正确", response.text)
        response = self.client.post("/login", data={"password": "wrong-pass"})
        self.assertIn("尝试过多", response.text)
        response = self.client.post("/login", data={"password": TEST_PASSWORD})
        self.assertIn("尝试过多", response.text)

    def test_csrf_missing_is_rejected(self):
        token = self.setup_admin()
        response = self.client.post("/repos/add", data={"url": "https://github.com/a/b"})
        self.assertEqual(response.status_code, 403)

    def test_add_fork_url_validation(self):
        token = self.setup_admin()
        stub = mock.Mock()
        with mock.patch.object(self.main, "client_from_settings", return_value=stub), \
             mock.patch.object(self.main, "add_manual", side_effect=ValueError("不是 fork，无法自动同步")):
            response = self.client.post("/repos/add", data={
                "url": "https://github.com/a/b", "csrf": self.csrf(token)})
        self.assertIn("不是 fork", response.text)

    def test_pause_resume_and_remove(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        response = self.client.post(f"/repos/{repo_id}/pause", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["status"], "paused")
        self.client.post(f"/repos/{repo_id}/resume", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["status"], "pending")
        self.client.post(f"/repos/{repo_id}/sync", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["sync_requested"], 1)
        self.client.post(f"/repos/{repo_id}/remove", data={"csrf": self.csrf(token)})
        self.assertIsNone(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))

    def test_mode_toggle_switches_and_resets_state(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.run("UPDATE repositories SET status='diverged', diverged_at='x', "
                         "diverged_upstream_sha='y' WHERE id=?", (repo_id,))
        response = self.client.post(f"/repos/{repo_id}/mode", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["mode"], "manual")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["diverged_at"])
        self.assertEqual(row["sync_requested"], 1)
        # toggling back returns to mirror mode
        self.client.post(f"/repos/{repo_id}/mode", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["mode"], "mirror")

    def test_selfupdate_toggle_enables_actions_and_permissions(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        stub = mock.Mock()
        stub.actions_enabled.return_value = False
        stub.workflow_permissions.return_value = {"default_workflow_permissions": "read"}
        stub.list_workflows.return_value = [
            {"id": 5, "name": "Update", "state": "disabled_fork",
             "path": ".github/workflows/update.yml"}]
        with mock.patch.object(self.main, "client_from_settings", return_value=stub):
            response = self.client.post(f"/repos/{repo_id}/selfupdate",
                                        data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        stub.set_actions_enabled.assert_called_once_with("me/demo", True)
        stub.set_workflow_permissions.assert_called_once_with("me/demo", "write")
        stub.enable_workflow.assert_called_once_with("me/demo", 5)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["self_update_interval"], 1)
        self.assertEqual(row["sync_requested"], 1)
        # toggle off
        with mock.patch.object(self.main, "client_from_settings", return_value=stub):
            self.client.post(f"/repos/{repo_id}/selfupdate", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertIsNone(row["self_update_interval"])

    def test_selfupdate_reports_permission_failure(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        stub = mock.Mock()
        stub.actions_enabled.return_value = True
        stub.workflow_permissions.return_value = {"default_workflow_permissions": "write"}
        stub.list_workflows.return_value = [
            {"id": 5, "name": "Update", "state": "disabled_fork",
             "path": ".github/workflows/update.yml"}]
        stub.enable_workflow.side_effect = RuntimeError("Resource not accessible by integration")
        with mock.patch.object(self.main, "client_from_settings", return_value=stub):
            response = self.client.post(f"/repos/{repo_id}/selfupdate",
                                        data={"csrf": self.csrf(token)})
        self.assertIn("Actions", response.text)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertIsNone(row["self_update_interval"])
        self.assertIn("开启自更新部分失败", row["self_update"])

    def test_manifest_callback_rejects_bad_state_and_accepts_good_code(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_manifest_state", "state-1")
        # missing code
        response = self.client.get("/api/github/callback")
        self.assertIn("连接失败", response.text)
        # wrong state
        response = self.client.get("/api/github/callback?code=abc&state=wrong")
        self.assertIn("连接失败", response.text)
        # valid exchange writes pem and stores app id
        payload = {"id": 123, "slug": "forkiq-test", "pem": "-----BEGIN RSA PRIVATE KEY-----\nxx\n-----END RSA PRIVATE KEY-----\n"}
        with mock.patch.object(self.main, "exchange_manifest", return_value=payload):
            response = self.client.get("/api/github/callback?code=abc&state=state-1")
        self.assertIn("安装", response.text)
        self.assertEqual(self.main.db.get_setting("github_app_id"), "123")
        pem = Path(self.main.PEM_PATH)
        self.assertTrue(pem.exists())
        self.assertEqual(oct(pem.stat().st_mode)[-3:], "600")

    def test_healthz_is_public(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_detect_installation_stores_id(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_id", "1")
        self.main.db.set_setting("github_pem_path", str(self.main.PEM_PATH))
        stub = mock.Mock()
        stub.app_installations.return_value = [
            {"id": 999, "account": {"login": "zzusec"}}]
        with mock.patch.object(self.main, "app_client_from_settings", return_value=stub):
            response = self.client.post("/github/detect", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("github_installation_id"), "999")

    def test_detect_before_installation_id_exists(self):
        # The detect step runs before an installation id is stored; it must not
        # depend on client_from_settings (which requires one).
        token = self.setup_admin()
        self.main.db.set_setting("github_app_id", "1")
        self.main.db.set_setting("github_pem_path", str(self.main.PEM_PATH))
        stub = mock.Mock()
        stub.app_installations.return_value = [
            {"id": 1000, "account": {"login": "zzusec"}}]
        with mock.patch.object(self.main, "app_client_from_settings", return_value=stub):
            response = self.client.post("/github/detect", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("github_installation_id"), "1000")

    def test_settings_save_clamps_interval(self):
        token = self.setup_admin()
        self.client.post("/settings", data={"csrf": self.csrf(token), "interval": "5",
                                            "auto_discover": "1"})
        self.assertEqual(self.main.db.get_setting("check_interval"), "15")
        self.client.post("/settings", data={"csrf": self.csrf(token), "interval": "9999",
                                            "auto_discover": "0"})
        self.assertEqual(self.main.db.get_setting("check_interval"), "300")
        self.assertEqual(self.main.db.get_setting("auto_discover"), "0")


if __name__ == "__main__":
    unittest.main()
