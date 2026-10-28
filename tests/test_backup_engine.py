"""Snapshot gating is independent of deletion policy and approval."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import engine
from app.db import DB, insert_repository, utcnow
from app.protection import load_review, review_token
from test_engine import StubGitHub, node


class BackupEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "forkiq.db")
        self.addCleanup(self.db.close)
        self.repo_id = insert_repository(self.db, owner="me", name="demo", full_name="me/demo",
                                         upstream_full_name="up/demo", node_id="N1")
        self.client = StubGitHub()
        self.sha, self.target = "a" * 40, "b" * 40
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", self.sha, self.target)

    def row(self):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (self.repo_id,))

    def writes(self):
        return [call for call in self.client.calls if call[0] in ("update_ref", "merge_commit")]

    def test_backup_failure_blocks_mirror_force_manual_and_disabled_protection(self):
        for mode, enabled in (("mirror", 1), ("manual", 1), ("mirror", 0), ("manual", 0)):
            with self.subTest(mode=mode, protection=enabled):
                self.db.run("UPDATE repositories SET mode=?,enabled=1,status='pending',deletion_protection=? WHERE id=?",
                            (mode, enabled, self.repo_id))
                self.client.calls.clear()
                self.client.compare_result = {"ahead_by": 1, "behind_by": 0}
                with mock.patch("app.engine.ensure_backup", return_value={"status": "error", "last_error": "disk full"}):
                    status = engine.sync_one(self.db, self.client, self.row(), self.client.nodes["N1"], forced=True)
                self.assertEqual(status, "needs_attention")
                self.assertEqual(self.writes(), [])
                self.assertIn("备份失败", self.row()["last_error"])
                self.assertIsNone(self.row()["deletion_approval"])

    def test_ready_backup_does_not_automatically_allow_empty_upstream(self):
        self.client.file_trees["up/demo", self.target] = set()
        with mock.patch("app.engine.ensure_backup", return_value={"status": "ready"}) as backup:
            self.assertFalse(engine._deletion_guard(self.db, self.client, self.row(), self.sha, self.target))
        backup.assert_called_once()
        self.assertEqual(self.writes(), [])
        self.assertTrue(load_review(self.row())["reasons"])

    def test_backup_failure_cannot_be_bypassed_by_exact_approval(self):
        self.client.file_trees["up/demo", self.target] = set()
        with mock.patch("app.engine.ensure_backup", return_value={"status": "ready"}):
            engine._deletion_guard(self.db, self.client, self.row(), self.sha, self.target)
        token = review_token(load_review(self.row()))
        self.db.run("UPDATE repositories SET deletion_approval=? WHERE id=?", (token, self.repo_id))
        with mock.patch("app.engine.ensure_backup", return_value={"status": "error", "last_error": "bad checksum"}):
            self.assertFalse(engine._deletion_guard(self.db, self.client, self.row(), self.sha, self.target))
        self.assertIsNone(self.row()["deletion_approval"])
        self.assertEqual(self.writes(), [])

    def test_head_or_policy_change_during_backup_never_writes(self):
        for change in ("head", "policy", "pause"):
            with self.subTest(change=change):
                self.db.run("UPDATE repositories SET enabled=1,status='pending',deletion_max_files=NULL WHERE id=?", (self.repo_id,))
                self.client.refs["me/demo"] = self.sha
                def ready(*args):
                    if change == "head":
                        self.client.refs["me/demo"] = "c" * 40
                    elif change == "policy":
                        self.db.run("UPDATE repositories SET deletion_max_files=7 WHERE id=?", (self.repo_id,))
                    else:
                        self.db.run("UPDATE repositories SET enabled=0,status='paused' WHERE id=?", (self.repo_id,))
                    return {"status": "ready"}
                with mock.patch("app.engine.ensure_backup", side_effect=ready):
                    self.assertFalse(engine._deletion_guard(self.db, self.client, self.row(), self.sha, self.target))
                self.assertEqual(self.writes(), [])
                if change == "pause":
                    self.assertEqual(self.row()["status"], "paused")

    def test_initial_baseline_includes_paused_repos_and_is_not_repeated(self):
        self.db.run("UPDATE repositories SET enabled=0,status='paused' WHERE id=?", (self.repo_id,))
        def snapshot(db, client, row, sha):
            db.run("INSERT INTO repository_backups(repo_id,full_name,fork_sha,status,created_at,updated_at) VALUES(?,?,?,'ready',?,?)",
                   (row["id"], row["full_name"], sha, utcnow(), utcnow()))
            return {"status": "ready"}
        with mock.patch("app.engine.ensure_backup", side_effect=snapshot) as backup:
            engine.run_cycle(self.db, self.client)
            engine.run_cycle(self.db, self.client)
        backup.assert_called_once()
        self.assertEqual(self.row()["status"], "paused")
        self.assertEqual(self.writes(), [])

    def test_pending_restore_cannot_be_synced_and_worker_clears_request(self):
        self.db.run("UPDATE repositories SET restore_backup_id=9,restore_expected_sha=?,enabled=0,status='paused' WHERE id=?",
                    (self.sha, self.repo_id))
        with mock.patch("app.engine.ensure_backup") as backup:
            engine.run_cycle(self.db, self.client)
        backup.assert_not_called()
        with mock.patch("app.restore.rollback_backup", return_value={"ok": True}) as restore:
            engine.run_restore_requests(self.db, self.client)
        restore.assert_called_once()
        self.assertIsNone(self.row()["restore_backup_id"])
        self.assertEqual(self.row()["status"], "paused")
