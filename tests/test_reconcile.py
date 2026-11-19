import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.db import DB, insert_repository
from app.engine import run_cycle
from app.gitstore import PushNetworkError, RemoteReadUnavailable, SyncBlocked, git_error_category
from test_safefork import A, B, C, MAIN, MemoryStore, config, metadata


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "forkiq.db"); self.addCleanup(self.db.close)
        self.cfg = config(); self.source, self.target = metadata(self.cfg)
        self.client = Mock()
        self.client.get_repo.side_effect = lambda name: self.source if name == self.cfg["source"] else self.target
        self.client.actions_enabled.return_value = False; self.client.has_active_runs.return_value = False
        self.client.installation_token.return_value = "fixture-secret-token"
        self.store = MemoryStore()
        self.key = insert_repository(self.db, owner="owner", name="project-SafeFork", full_name=self.cfg["target"], upstream_full_name=self.cfg["source"])
        self.db.run("UPDATE repositories SET config_json=?,enabled=1,review_required=0 WHERE id=?", (json.dumps(self.cfg), self.key))

    def row(self): return self.db.one("SELECT * FROM repositories WHERE id=?", (self.key,))

    def cycle(self, at=1000):
        with patch("time.time", return_value=at):
            run_cycle(self.db, self.client, store_type=lambda *args: self.store, only_requested=bool(self.row()["push_intent_json"] or self.row()["next_retry_at"]))

    def fail_push(self, error=None, applied=False):
        real = self.store.push
        def fail(updates, expected):
            intent = json.loads(self.row()["push_intent_json"])
            self.assertEqual(intent["plan"]["target"], {MAIN:A})
            self.assertEqual(intent["expected"], {MAIN:B})
            self.assertEqual(intent["epoch"], self.row()["control_epoch"])
            self.assertEqual(self.store.archived, {MAIN:B})
            self.assertNotIn("fixture-secret-token", self.row()["push_intent_json"])
            if applied: real(updates, expected)
            raise error or PushNetworkError("network")
        self.store.push = Mock(side_effect=fail)
        self.cycle()
        return real

    def test_unwritten_network_push_requires_readback_then_fresh_plan(self):
        real = self.fail_push()
        self.assertEqual((self.row()["enabled"], self.row()["review_required"], self.row()["status"]), (0,1,"verifying"))
        self.db.close()  # durable across a new SQLite connection / worker
        self.cycle(1059); self.store.push.assert_called_once()
        self.cycle(1060); self.store.push.assert_called_once()
        self.assertEqual(self.row()["status"], "retrying")
        self.assertEqual(self.row()["retry_count"], 1)
        self.store.push = Mock(side_effect=real)
        self.store.fetch = Mock()
        self.cycle(1060)
        self.assertEqual(self.store.fetch.call_count, 2)
        self.assertEqual(self.row()["status"], "synced")
        self.assertEqual(self.row()["retry_count"], 0)
        self.assertEqual(self.store.state["target"], {MAIN:B})

    def test_applied_response_lost_confirmed_without_repeating_push(self):
        self.fail_push(applied=True)
        self.cycle(1060)
        self.store.push.assert_called_once()
        self.assertEqual((self.row()["enabled"], self.row()["status"]), (1,"synced"))
        self.assertIsNone(self.row()["push_intent_json"])

    def test_crash_rechecks_only_and_never_retries_unclassified_write(self):
        real = self.store.push
        for applied in (False, True):
            self.store = MemoryStore()
            self.db.run("UPDATE repositories SET enabled=1,review_required=0,status='pending',push_intent_json=NULL,next_retry_at=0,retry_count=0")
            def crash(updates, expected):
                if applied: self.store.state["target"].update(updates)
                raise KeyboardInterrupt()
            self.store.push = Mock(side_effect=crash)
            with self.assertRaises(KeyboardInterrupt): self.cycle()
            self.db.close(); self.cycle(1060)
            self.store.push.assert_called_once()
            self.assertEqual(self.row()["status"], "synced" if applied else "needs_attention")

    def test_unknown_or_permission_error_never_grants_repush(self):
        for error in (RuntimeError("fixture-secret-token"), SyncBlocked("permission denied")):
            self.store = MemoryStore()
            self.db.run("UPDATE repositories SET enabled=1,review_required=0,status='pending',push_intent_json=NULL,next_retry_at=0,retry_count=0")
            self.fail_push(error)
            self.assertNotIn("fixture-secret-token", self.row()["last_error"])
            self.cycle(1060); self.store.push.assert_called_once()
            self.assertEqual(self.row()["status"], "needs_attention")

    def test_concurrent_refs_source_or_archive_change_blocks(self):
        for field in ("target", "source", "archive"):
            self.store = MemoryStore()
            self.db.run("UPDATE repositories SET enabled=1,review_required=0,status='pending',push_intent_json=NULL,next_retry_at=0,retry_count=0")
            self.fail_push()
            if field == "archive": self.store.archived[MAIN] = C
            else: self.store.state[field]["refs/tags/concurrent"] = C
            self.cycle(1060)
            self.assertEqual(self.row()["status"], "needs_attention"); self.store.push.assert_called_once()

    def test_pause_and_connection_change_cannot_reenable(self):
        self.fail_push(applied=True)
        refs = self.store.refs
        def pause(side):
            self.db.run("UPDATE repositories SET enabled=0,status='paused',next_retry_at=0,push_intent_json=NULL,control_epoch=control_epoch+1")
            return refs(side)
        self.store.refs = pause; self.cycle(1060)
        self.assertEqual((self.row()["enabled"],self.row()["status"]),(0,"paused"))
        self.store = MemoryStore()
        self.db.run("UPDATE repositories SET enabled=1,review_required=0,status='pending',next_retry_at=0,retry_count=0")
        self.fail_push(applied=True); self.db.set_setting("github_installation_id","different")
        self.cycle(1060)
        self.assertEqual(self.row()["status"],"needs_attention")

    def test_missing_readback_retries_reads_only_and_is_bounded(self):
        self.fail_push()
        self.store.refs = Mock(side_effect=RemoteReadUnavailable("unavailable"))
        at = self.row()["next_retry_at"]
        for _ in range(5):
            self.cycle(at); at = self.row()["next_retry_at"]
        self.store.push.assert_called_once()
        self.assertEqual((self.row()["status"],self.row()["next_retry_at"]),("needs_attention",0))

    def test_repeated_unwritten_network_failures_exhaust_shared_budget(self):
        self.store.push = Mock(side_effect=PushNetworkError("network"))
        self.cycle()
        for _ in range(6):
            at=self.row()["next_retry_at"]
            if not at: break
            self.cycle(at)  # read-only proof
            if self.row()["status"]=="retrying": self.cycle(at)  # fresh complete sync
        self.assertEqual(self.store.push.call_count,6)
        self.assertEqual(self.row()["status"],"needs_attention")

    def test_rollback_or_tag_move_still_passes_full_policy_before_retry(self):
        self.fail_push(); self.cycle(1060)
        self.store.is_ancestor.return_value=False
        self.cycle(1060)
        self.store.push.assert_called_once()
        self.assertEqual(self.row()["status"],"needs_attention")

    def test_push_error_classification_never_returns_payload(self):
        cases = {"fatal: unable to access secret: Failed to connect to github.com": "network",
            "RPC failed; curl 56 OpenSSL SSL_read: Connection reset by peer":"network",
            "gnutls_handshake() failed: The TLS connection was non-properly terminated.":"network",
            "The requested URL returned error: 502":"network",
            "The requested URL returned error: 403":"http_client",
            "Authentication failed secret":"permission", "SSL certificate problem secret":"certificate",
            "GH013 protected branch secret":"rules", "non-fast-forward secret":"ref_conflict",
            "No space left on device secret":"resource", "unexpected secret":"unknown"}
        for error, category in cases.items(): self.assertEqual(git_error_category(error),category)

    def test_additive_migration_preserves_existing_enabled_and_paused_rows(self):
        before=dict(self.row()); self.db.run("ALTER TABLE repositories DROP COLUMN push_intent_json")
        self.db.close()
        reopened=DB(Path(self.tmp.name)/"forkiq.db")
        try:
            self.assertEqual(dict(reopened.one("SELECT * FROM repositories WHERE id=?",(self.key,))),before)
        finally: reopened.close()

if __name__ == "__main__": unittest.main()
