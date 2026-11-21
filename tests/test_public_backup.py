"""Independent public backups retain identity, anonymity and write recovery gates."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.db import DB
from app.engine import register, run_cycle, source_client
from app.gitstore import PushNetworkError, SyncBlocked
from app.safefork import validate_config, validate_identity
from test_safefork import A, B, MAIN, MemoryStore, config, metadata


def public_backup():
    cfg = config(mode="standalone-public")
    source, target = metadata(cfg)
    target.update(fork=False)
    target.pop("parent", None)
    return cfg, source, target


class PublicBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "forkiq.db")
        self.addCleanup(self.db.close)
        self.cfg, self.source, self.target = public_backup()
        self.client = Mock()
        self.client.get_repo.side_effect = lambda name: self.source if name == self.cfg["source"] else self.target
        self.client.actions_enabled.return_value = False
        self.client.has_active_runs.return_value = False
        self.client.installation_token.return_value = "fixture-target-only"
        self.store = MemoryStore()

    def add(self):
        return register(self.db, self.client, self.cfg["source"], self.cfg["target"], "standalone-public", {}, {})

    def cycle(self, at=1000):
        def store_factory(root, source, target, source_token, target_token):
            self.assertEqual(source_token(), "")
            self.assertEqual(target_token(), "fixture-target-only")
            return self.store
        with patch("time.time", return_value=at):
            run_cycle(self.db, self.client, store_type=store_factory)

    def test_identity_rejects_fork_network_and_changed_visibility_or_ids(self):
        validate_identity(self.cfg, self.source, self.target)
        for side in ("source", "target"):
            for field, bad in (("id", 999), ("full_name", "changed/name"), ("private", True), ("visibility", "private")):
                with self.subTest(side=side, field=field):
                    src, dst = copy.deepcopy(self.source), copy.deepcopy(self.target)
                    (src if side == "source" else dst)[field] = bad
                    with self.assertRaises(SyncBlocked): validate_identity(self.cfg, src, dst)
        for change in ({"fork": True}, {"fork": None}, {"parent": {"id": 11}}, {"source": {"id": 11}}):
            with self.subTest(change=change), self.assertRaises(SyncBlocked):
                validate_identity(self.cfg, self.source, {**self.target, **change})
        with self.assertRaises(SyncBlocked):
            validate_config({**self.cfg, "prune_deleted_branches": True})

    def test_registration_is_paused_and_public_reads_do_not_open_source_secret(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read a source secret")):
            self.assertIs(source_client(self.cfg, self.client), self.client)
            key = self.add()
        row = self.db.one("SELECT * FROM repositories WHERE id=?", (key,))
        self.assertEqual((row["enabled"], row["review_required"], row["status"]), (0, 1, "paused"))
        self.assertEqual(json.loads(row["config_json"])["mode"], "standalone-public")
        with self.assertRaises(SyncBlocked): self.add()

    def test_unknown_mode_fails_before_remote_reads_or_credentials(self):
        with self.assertRaises(SyncBlocked):
            register(self.db, self.client, "a/b", "c/d", "invalid", {}, {})
        with self.assertRaises(SyncBlocked): source_client({"mode": "invalid"}, self.client)
        self.client.get_repo.assert_not_called()
        self.client.installation_token.assert_not_called()

    def test_preview_and_sync_use_public_refs_and_keep_actions_gate(self):
        self.add()
        self.db.run("UPDATE repositories SET dry_run_requested=1")
        self.cycle()
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.db.one("SELECT review_required FROM repositories")[0], 0)
        self.db.run("UPDATE repositories SET enabled=1")
        self.cycle()
        self.assertEqual(self.db.one("SELECT status FROM repositories")[0], "synced")
        self.assertTrue(self.store.public_source)
        self.store.ref_readers["source"]()
        self.store.ref_readers["target"]()
        self.client.refs.assert_any_call(self.cfg["source"], 11, False)
        self.client.refs.assert_any_call(self.cfg["target"], 22, False)
        self.client.actions_enabled.return_value = True
        writes = list(self.store.writes)
        self.cycle()
        self.assertEqual(self.db.one("SELECT status FROM repositories")[0], "needs_attention")
        self.assertEqual(self.store.writes, writes)

    def test_response_loss_reconciles_without_another_push(self):
        self.add()
        self.db.run("UPDATE repositories SET enabled=1,review_required=0")
        push = self.store.push
        def lost_response(updates, expected):
            push(updates, expected)
            raise PushNetworkError("network")
        self.store.push = Mock(side_effect=lost_response)
        self.cycle()
        self.assertEqual(self.db.one("SELECT status FROM repositories")[0], "verifying")
        self.cycle(1060)
        self.store.push.assert_called_once()
        self.assertEqual(self.db.one("SELECT status FROM repositories")[0], "synced")
        self.assertEqual(self.store.state["target"], {MAIN: B})
        self.assertEqual(self.store.archived, {MAIN: B})

