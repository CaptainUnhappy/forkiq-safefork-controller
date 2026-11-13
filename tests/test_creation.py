"""OAuth and provisioning failure boundaries; no real credentials or network."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from unittest.mock import Mock, patch
from app.db import DB
from app import oauth, provision
from app.gitstore import SyncBlocked
from test_safefork import config, metadata, MemoryStore, MAIN, A, B
REAL_PUBLIC_SOURCE = provision.public_source


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.temp.name) / "state.db")
        self.store = oauth.OAuthStore(self.temp.name)
        self.store.configure("test_client_id", "test_secret_" + "x" * 30)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def state(self):
        url = oauth.begin(self.db, self.store, "session", "https://example.test/SafeFork")
        return parse_qs(urlsplit(url).query)

    def test_state_bound_to_session_once_and_pkce_callback_exact(self):
        args = self.state()
        self.assertEqual(args["scope"], ["repo"])
        self.assertEqual(args["code_challenge_method"], ["S256"])
        self.assertEqual(args["redirect_uri"], ["https://example.test/SafeFork/oauth/callback"])
        state = args["state"][0]
        with self.assertRaises(SyncBlocked):
            oauth.consume_state(self.db, state, "another-session")
        verifier = oauth.consume_state(self.db, state, "session")
        challenge = oauth.base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(args["code_challenge"], [challenge])
        with self.assertRaises(SyncBlocked):
            oauth.consume_state(self.db, state, "session")

    def test_expired_and_replaced_state_rejected(self):
        first = self.state()["state"][0]
        second = self.state()["state"][0]
        with self.assertRaises(SyncBlocked):
            oauth.consume_state(self.db, first, "session")
        self.db.run("UPDATE oauth_states SET expires_at=1")
        with self.assertRaises(SyncBlocked):
            oauth.consume_state(self.db, second, "session")

    def test_token_response_requires_json_bearer_and_repo_scope(self):
        good = dict(access_token="gho_" + "x" * 36, token_type="bearer", scope="repo")
        with patch.object(oauth.GitHub, "_request", return_value=(200, good, {})) as request:
            self.assertEqual(oauth.token_exchange({}), good)
            self.assertEqual(request.call_args.args[2], {"Accept": "application/json"})
        for payload in ({**good, "scope": "public_repo"}, {**good, "token_type": "other"}, {"error": "fixture-secret"}):
            with patch.object(oauth.GitHub, "_request", return_value=(200, payload, {})), self.assertRaises(SyncBlocked) as error:
                oauth.token_exchange({})
            self.assertNotIn("fixture-secret", str(error.exception))

    def test_account_mismatch_never_saves_token(self):
        state = self.state()["state"][0]
        with patch.object(oauth, "token_exchange", return_value={"access_token": "fixture-secret"}), \
             patch.object(oauth.ProvisionGitHub, "user", return_value={"id": 1, "login": "wrong"}), \
             patch.object(oauth, "installation_owner", return_value={"id": 2, "login": "owner"}), \
             self.assertRaisesRegex(SyncBlocked, "账号"):
            oauth.complete(self.db, self.store, "session", "fixture-code", state, "https://example.test", Mock(), "123")
        self.assertNotIn("access_token", self.store.read())

    def test_secret_never_in_status_and_disconnect_preserves_app_configuration(self):
        data = self.store.read()
        self.store.write({**data, "access_token": "fixture-secret", "login": "owner"})
        self.assertNotIn("fixture-secret", str(self.store.status()))
        self.store.disconnect()
        self.assertNotIn("access_token", self.store.read())
        self.assertNotEqual(self.store.read()["generation"], data["generation"])
        self.assertTrue(self.store.status()["configured"])
        if os.name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_refresh_rotation_keeps_generation_and_checks_account(self):
        data = {**self.store.read(), "access_token": "old", "refresh_token": "old-refresh",
                "expires_at": 1, "account_id": 1, "login": "owner"}
        self.store.write(data)
        with patch.object(oauth, "token_exchange", return_value=dict(access_token="new", refresh_token="new-refresh", expires_in=28800)), \
             patch.object(oauth.ProvisionGitHub, "user", return_value={"id": 1, "login": "owner"}):
            client, _ = oauth.authorized_client(self.store)
        self.assertEqual(self.store.read()["refresh_token"], "new-refresh")
        self.assertEqual(client.generation, data["generation"])

    def test_initialization_http_status_and_fixed_write_bodies(self):
        client = oauth.ProvisionGitHub("fixture-secret")
        with patch.object(oauth.GitHub, "_request", return_value=(204, None, {})) as request:
            client.isolate("owner/project-SafeFork")
            self.assertEqual(request.call_args.args[0], "PUT")
            self.assertEqual(request.call_args.args[3], {"enabled": False})
            with self.assertRaises(SyncBlocked):
                client.create_fork("upstream/project", "project-SafeFork")
        with patch.object(oauth.GitHub, "_request", return_value=(202, {"id": 22}, {})) as request:
            client.create_fork("upstream/project", "project-SafeFork")
            self.assertEqual(request.call_args.args[3], {"name": "project-SafeFork", "default_branch_only": False})


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.temp.name) / "state.db")
        self.source, self.target = metadata(config())
        self.source.update(size=1)
        self.target.update(default_branch="main")
        self.user = {"id": 7, "login": "owner", "type": "User"}
        self.oauth = Mock(generation="generation")
        self.remote = None
        self.oauth.repo.side_effect = lambda name, missing=False: self.remote
        def create(*_):
            self.remote = self.target.copy()
            return self.remote
        self.oauth.create_fork.side_effect = create
        self.client = Mock()
        self.client.get_repo.side_effect = lambda name: self.source if name == config()["source"] else self.target
        self.client.actions_enabled.return_value = False
        self.client.has_active_runs.return_value = False
        self.store = MemoryStore()
        self.patches = [patch.object(provision, "public_source", return_value=self.source),
                        patch.object(provision, "authorized_client", return_value=(self.oauth, self.user)),
                        patch.object(provision, "installation_owner", return_value=self.user),
                        patch.object(provision, "same_network_fork"),
                        patch.object(provision.OAuthStore, "read", return_value={"generation": "generation"}),
                        patch.object(provision.time, "sleep")]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.db.close()
        self.temp.cleanup()

    def enqueue(self):
        return provision.enqueue(self.db, "https://github.com/upstream/project")

    def run_job(self, **kw):
        provision.run_jobs(self.db, self.client, store_type=lambda *a: self.store, **kw)

    def test_one_click_persists_archive_then_enables_and_duplicate_is_same_job(self):
        job = self.enqueue()
        self.assertEqual(self.enqueue(), job)
        self.run_job()
        row = self.db.one("SELECT * FROM creation_jobs")
        repo = self.db.one("SELECT * FROM repositories")
        self.assertEqual((row["status"], row["phase"], repo["status"], repo["enabled"]), ("complete", "complete", "synced", 1))
        self.assertEqual(self.store.archived[MAIN], B)
        self.assertTrue(json.loads(repo["config_json"])["prune_deleted_branches"])
        self.assertEqual(json.loads(self.db.one("SELECT refs_json FROM sync_baselines")[0]), {MAIN: B})
        self.oauth.create_fork.assert_called_once()
        self.oauth.isolate.assert_called_once_with("owner/project-SafeFork")
        self.assertEqual(self.enqueue(), job)

    def test_name_occupied_never_queues_or_changes_existing_repository(self):
        self.remote = self.target
        with self.assertRaisesRegex(SyncBlocked, "已存在"): self.enqueue()
        self.assertIsNone(self.db.one("SELECT id FROM creation_jobs"))
        self.oauth.isolate.assert_not_called()

    def test_actions_enabled_or_inflight_blocks_all_git_writes(self):
        for enabled, running in ((True, False), (False, True)):
            with self.subTest(enabled=enabled):
                self.db.run("DELETE FROM creation_jobs")
                self.remote = None
                self.enqueue()
                self.client.actions_enabled.return_value = enabled
                self.client.has_active_runs.return_value = running
                self.run_job()
                self.assertEqual(self.db.one("SELECT status FROM creation_jobs")[0], "blocked")
                self.assertEqual(self.store.writes, [])
                self.assertIsNone(self.db.one("SELECT id FROM repositories"))

    def test_uncertain_creation_does_not_repeat_post_on_resume(self):
        job = self.enqueue()
        def uncertain(*_):
            self.remote = self.target
            raise RuntimeError("fixture-secret")
        self.oauth.create_fork.side_effect = uncertain
        self.run_job()
        self.assertNotIn("fixture-secret", self.db.one("SELECT last_error FROM creation_jobs")[0])
        provision.retry(self.db, job)
        self.run_job()
        self.oauth.create_fork.assert_called_once()
        self.oauth.isolate.assert_not_called()
        self.assertEqual(self.db.one("SELECT status FROM creation_jobs")[0], "blocked")

    def test_isolation_retry_reuses_fixed_target(self):
        job = self.enqueue()
        self.oauth.isolate.side_effect = [SyncBlocked("network"), None]
        self.run_job()
        provision.retry(self.db, job)
        self.run_job()
        self.oauth.create_fork.assert_called_once()
        self.assertEqual(self.db.one("SELECT status FROM creation_jobs")[0], "complete")

    def test_source_changes_or_authority_revoked_before_create(self):
        self.enqueue()
        self.source["id"] = 99
        self.run_job()
        self.oauth.create_fork.assert_not_called()
        self.source["id"] = 11
        provision.retry(self.db, 1)
        with patch.object(provision.OAuthStore, "read", return_value={"generation": "changed"}):
            self.run_job()
        self.oauth.create_fork.assert_not_called()

    def test_process_interruption_and_push_failure_are_never_automatically_retried(self):
        job = self.enqueue()
        self.store.push = Mock(side_effect=SyncBlocked("network"))
        self.run_job()
        self.assertEqual(self.db.one("SELECT phase FROM creation_jobs")[0], "sync_intent")
        with self.assertRaisesRegex(SyncBlocked, "预览"): provision.retry(self.db, job)
        self.run_job()
        self.store.push.assert_called_once()
        self.assertEqual(self.store.archived[MAIN], B)
        self.assertEqual(self.db.one("SELECT enabled FROM repositories")[0], 0)

    def test_user_pause_during_initial_write_is_not_overwritten(self):
        self.enqueue()
        original = self.store.preserve
        def pause(cfg, expected, archive):
            result = original(cfg, expected, archive)
            self.db.run("UPDATE repositories SET control_epoch=control_epoch+1,status='paused'")
            return result
        self.store.preserve = pause
        self.run_job()
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.db.one("SELECT status FROM repositories")[0], "paused")

    def test_private_metadata_and_insufficient_capacity_rejected(self):
        with patch.object(provision.GitHub, "get_repo", return_value={**self.source, "private": True}):
            # Exercise real metadata validation rather than the setUp fixture wrapper.
            with self.assertRaises(SyncBlocked): REAL_PUBLIC_SOURCE("upstream/project")
        with patch.object(provision.shutil, "disk_usage", return_value=Mock(free=1024)):
            with self.assertRaisesRegex(SyncBlocked, "容量"): self.enqueue()
        self.oauth.create_fork.assert_not_called()
