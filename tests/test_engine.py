import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.db import DB, insert_repository
from app.engine import register, run_cycle, source_client, verify_live
from app.gitstore import SyncBlocked
from test_safefork import A, B, MAIN, MemoryStore, config, metadata


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "state" / "forkiq.db")
        self.addCleanup(self.db.close)
        self.cfg = config()
        self.source, self.target = metadata(self.cfg)
        self.client = Mock()
        self.client.get_repo.side_effect = lambda name: self.source if name == self.cfg["source"] else self.target
        self.client.actions_enabled.return_value = False
        self.client.has_active_runs.return_value = False
        self.client.installation_token.return_value = "fixture-target"
        self.store = MemoryStore()

    def add(self, enabled=1, review=0, dry=0):
        repo = insert_repository(self.db, owner="owner", name="project-SafeFork", full_name=self.cfg["target"],
                                 upstream_full_name=self.cfg["source"])
        self.db.run("UPDATE repositories SET config_json=?,enabled=?,review_required=?,dry_run_requested=? WHERE id=?",
                    (json.dumps(self.cfg, sort_keys=True), enabled, review, dry, repo))
        return repo

    def cycle(self, **kwargs):
        run_cycle(self.db, self.client, store_type=lambda *args: self.store, **kwargs)

    def row(self, repo=1):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (repo,))

    def pause(self):
        self.db.run("UPDATE repositories SET enabled=0,status='paused',control_epoch=control_epoch+1")

    def test_register_pins_identity_and_stays_paused(self):
        repo = register(self.db, self.client, self.cfg["source"], self.cfg["target"], "public-fork", {}, {})
        row = self.row(repo)
        self.assertEqual((row["enabled"], row["review_required"]), (0, 1))
        self.assertEqual(json.loads(row["config_json"])["source_id"], 11)
        with self.assertRaises(SyncBlocked):
            register(self.db, self.client, self.cfg["source"], self.cfg["target"], "public-fork", {}, {})

    def test_private_registration_requires_authorization_before_read(self):
        with self.assertRaises(SyncBlocked):
            register(self.db, self.client, "a/b", "c/d", "standalone-private", {}, {})
        self.client.get_repo.assert_not_called()

    def test_paused_repo_never_contacts_remote(self):
        self.add(enabled=0)
        self.cycle()
        self.client.get_repo.assert_not_called()
        self.assertEqual(self.store.writes, [])

    def test_dry_run_clears_review_but_never_enables(self):
        self.add(enabled=0, review=1, dry=1)
        self.cycle()
        row = self.row()
        self.assertEqual((row["enabled"], row["review_required"], row["status"]), (0, 0, "paused"))
        self.assertIsNotNone(row["plan_json"])
        self.assertIsNone(row["last_success_at"])
        self.assertEqual(self.store.writes, [])

    def test_success_requires_persisted_latch_then_reenables(self):
        self.add()
        push = self.store.push
        def inspect(updates, expected):
            row = self.row()
            self.assertEqual((row["enabled"], row["review_required"]), (0, 1))
            push(updates, expected)
        self.store.push = inspect
        self.cycle()
        row = self.row()
        self.assertEqual((row["enabled"], row["review_required"], row["status"]), (1, 0, "synced"))
        self.assertEqual(row["fork_sha"], B)

    def test_actions_enabled_or_in_flight_blocks_all_writes(self):
        for enabled, active in ((True, False), (False, True)):
            self.db.run("DELETE FROM repositories")
            repo = self.add()
            self.client.actions_enabled.return_value = enabled
            self.client.has_active_runs.return_value = active
            self.cycle()
            row = self.row(repo)
            self.assertEqual((row["enabled"], row["review_required"]), (0, 1))
            self.assertEqual(self.store.writes, [])

    def test_removed_source_stays_pinned_and_paused(self):
        self.add()
        self.client.get_repo.side_effect = RuntimeError("fixture-token-must-not-leak")
        self.cycle()
        row = self.row()
        self.assertEqual(row["enabled"], 0)
        self.assertNotIn("fixture-token", row["last_error"])
        self.assertEqual(json.loads(row["config_json"]), self.cfg)

    def test_pause_during_fetch_blocks_push_and_preserves_pause(self):
        self.add()
        self.store.fetch = lambda side, refs: self.pause() if side == "target" else None
        self.cycle()
        self.assertEqual(self.store.writes, [])
        self.assertEqual((self.row()["enabled"], self.row()["status"]), (0, "paused"))

    def test_pause_during_push_is_not_overwritten_by_success(self):
        self.add()
        push = self.store.push
        def pause_after_write(updates, expected):
            push(updates, expected)
            self.pause()
        self.store.push = pause_after_write
        self.cycle()
        self.assertEqual(len(self.store.writes), 1)
        self.assertEqual((self.row()["enabled"], self.row()["status"]), (0, "paused"))
        self.assertEqual(self.row()["review_required"], 1)

    def test_new_preview_is_not_erased_by_old_task(self):
        self.add()
        push = self.store.push
        def newer_request(updates, expected):
            push(updates, expected)
            self.db.run("UPDATE repositories SET enabled=0,status='paused',dry_run_requested=1,control_epoch=control_epoch+1")
        self.store.push = newer_request
        self.cycle()
        self.assertEqual((self.row()["enabled"], self.row()["dry_run_requested"]), (0, 1))

    def test_shutdown_during_fetch_blocks_write(self):
        self.add()
        stopped = []
        self.store.fetch = lambda *args: stopped.append(True)
        self.cycle(cancelled=lambda: bool(stopped))
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.row()["enabled"], 0)

    def test_disconnect_during_fetch_blocks_write(self):
        self.add()
        self.store.fetch = lambda *args: self.db.set_setting("github_installation_id", "changed")
        self.cycle()
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.row()["enabled"], 0)

    def test_crash_after_intent_is_latched_across_restart(self):
        self.add()
        self.store.push = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.cycle()
        self.assertEqual((self.row()["enabled"], self.row()["review_required"]), (0, 1))
        self.store.push.reset_mock()
        self.cycle()
        self.store.push.assert_not_called()

    def test_uncertain_push_stops_without_automatic_retry(self):
        self.add()
        self.store.push = Mock(side_effect=RuntimeError("secret-fixture"))
        self.cycle()
        self.cycle()
        self.store.push.assert_called_once()
        self.assertEqual(self.row()["enabled"], 0)
        self.assertNotIn("secret-fixture", self.row()["last_error"])

    def test_separate_private_credential_is_required(self):
        cfg = config(mode="standalone-private", authorized=True, source_secret="source_read")
        root = Path(self.tmp.name)
        with patch.dict(os.environ, FORKIQ_SOURCE_SECRETS_DIR=str(root)):
            with self.assertRaisesRegex(SyncBlocked, "尚未挂载"):
                source_client(cfg, self.client)
            (root / "source_read").write_text("fixture-target")
            with self.assertRaisesRegex(SyncBlocked, "不能与目标"):
                source_client(cfg, self.client)
            (root / "source_read").write_text("fixture-source")
            source = source_client(cfg, self.client)
            self.assertEqual(source.installation_token(), "fixture-source")
            with self.assertRaises(SyncBlocked):
                source_client({**cfg, "source_secret": "../outside"}, self.client)


class MigrationTests(unittest.TestCase):
    def test_legacy_migration_preserves_copy_and_never_inherits_enable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "forkiq.db"
            from app.db import SCHEMA
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO repositories(owner,name,full_name,created_at,updated_at,enabled,"
                             "sync_requested,restore_backup_id,self_update_interval) VALUES('a','b','a/b','old','old',1,1,8,60)")
                conn.commit()
            db = DB(path)
            row = db.one("SELECT * FROM repositories")
            self.assertEqual((row["enabled"], row["review_required"], row["sync_requested"]), (0, 1, 0))
            self.assertIsNone(row["restore_backup_id"])
            self.assertIsNone(row["self_update_interval"])
            backup = Path(str(path) + ".before-safefork")
            before = backup.read_bytes()
            db.close()
            db = DB(path)
            self.assertEqual(backup.read_bytes(), before)
            self.assertEqual(db.one("SELECT status FROM repositories")["status"], "paused")
            db.close()
            with closing(sqlite3.connect(backup)) as conn:
                self.assertEqual(conn.execute("SELECT enabled FROM repositories").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
