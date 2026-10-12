"""Web layer tests with FastAPI TestClient and a stub GitHub client."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import engine
from app.github import RefNotFastForward

TEST_PASSWORD = "correct-horse-8"


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
