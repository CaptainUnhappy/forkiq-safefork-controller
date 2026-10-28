"""Real local Git transport proves restore bundles and leases without GitHub writes."""
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import backups, restore
from app.db import DB, insert_repository


class LocalGitClient:
    token = "restore-test-secret-never-on-disk"

    def __init__(self, remote):
        self.remote = remote
        self.reads = []

    def installation_token(self):
        return self.token

    def get_ref(self, full_name, branch):
        self.reads.append((full_name, branch))
        if full_name != "me/demo":
            raise RuntimeError("unexpected repository")
        return subprocess.check_output(
            ["git", "--git-dir=" + str(self.remote), "rev-parse", "refs/heads/" + branch],
            stderr=subprocess.DEVNULL, text=True).strip()


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main", str(self.work))
        self.git("-C", str(self.work), "config", "user.name", "Restore Test")
        self.git("-C", str(self.work), "config", "user.email", "restore@example.invalid")
        self.git("-C", str(self.work), "config", "commit.gpgsign", "false")
        (self.work / "saved.txt").write_text("original project content\n")
        self.git("-C", str(self.work), "add", ".")
        self.git("-C", str(self.work), "commit", "-qm", "preserve content")
        self.target = self.git("-C", str(self.work), "rev-parse", "HEAD")
        self.git("-C", str(self.work), "remote", "add", "origin", str(self.remote))
        self.git("-C", str(self.work), "push", "origin", "main")
        self.db = DB(self.root / "forkiq.db")
        self.addCleanup(self.db.close)
        self.repo_id = insert_repository(self.db, owner="me", name="demo", full_name="me/demo",
                                         upstream_full_name="up/demo", node_id="N1")
        self.client = LocalGitClient(self.remote)
        self.commands = []
        self.push_hook = None
        self.real_runner = backups._run_git
        for module in (backups, restore):
            patch = mock.patch.object(module, "_run_git", side_effect=self.local_runner)
            patch.start()
            self.addCleanup(patch.stop)
        self.backup = backups.ensure_backup(self.db, self.client, self.row(), self.target)
        self.assertEqual(self.backup["status"], "ready", self.backup)
        self.assertTrue(backups.verified_backup(self.db, self.backup))
        self.git("-C", str(self.work), "rm", "saved.txt")
        (self.work / "README.md").write_text("emptied upstream\n")
        self.git("-C", str(self.work), "add", ".")
        self.git("-C", str(self.work), "commit", "-qm", "delete project content")
        self.current = self.git("-C", str(self.work), "rev-parse", "HEAD")
        self.git("-C", str(self.work), "push", "origin", "main")
        self.db.run("UPDATE repositories SET fork_sha=?,last_merged_upstream_sha=?,upstream_ahead=1,"
                    "fork_ahead=2,deletion_review='old review',deletion_approval='old approval' WHERE id=?",
                    (self.current, self.current, self.repo_id))
        self.commands.clear()

    def git(self, *args):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("GIT_"):
                del env[key]
        return subprocess.check_output(["git", *args], cwd=self.root, env=env,
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def local_runner(self, args, cwd, *, env=None, capture=False):
        self.commands.append(tuple(args))
        if args[0] == "push" and self.push_hook:
            self.push_hook()
        mapped = [str(self.remote) if item == "https://github.com/me/demo.git" else item for item in args]
        # Only the transport is substituted: production still receives HTTPS argv.
        local_env = dict(env or backups._git_env())
        local_env["GIT_ALLOW_PROTOCOL"] = "file:https"
        result = self.real_runner(["-c", "protocol.file.allow=always", *mapped], cwd,
                                  env=local_env, capture=capture)
        return result

    def row(self):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (self.repo_id,))

    def run_restore(self, **kwargs):
        return restore.rollback_backup(self.db, self.client, self.row(),
                                       kwargs.get("backup_id", self.backup["id"]),
                                       kwargs.get("expected_sha", self.current))

    def assert_paused(self):
        self.assertEqual(self.row()["enabled"], 0)
        self.assertEqual(self.row()["status"], "paused")
        self.assertEqual(self.row()["sync_requested"], 0)

    def assert_no_push(self):
        self.assertFalse([args for args in self.commands if args[0] == "push"])
        self.assertEqual(self.client.get_ref("me/demo", "main"), self.current)

    def rewrite_backup(self, data, sha=None):
        path = backups.backup_path(self.db, self.backup)
        path.write_bytes(data)
        self.db.run("UPDATE repository_backups SET sha256=?,size_bytes=?,fork_sha=? WHERE id=?",
                    (hashlib.sha256(data).hexdigest(), len(data), sha or self.target, self.backup["id"]))
        self.backup = dict(self.db.one("SELECT * FROM repository_backups WHERE id=?", (self.backup["id"],)))

    def test_full_bundle_recovers_deleted_files_and_history_and_keeps_sync_paused(self):
        result = self.run_restore()
        self.assertEqual(result, {"ok": True, "error": "", "sha": self.target})
        self.assertEqual(self.client.get_ref("me/demo", "main"), self.target)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "show", "main:saved.txt"),
                         "original project content")
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "log", "--format=%s", "main"),
                         "preserve content")
        self.assert_paused()
        self.assertEqual(self.row()["fork_sha"], self.target)
        for field in ("last_merged_upstream_sha", "upstream_ahead", "fork_ahead", "deletion_review", "deletion_approval"):
            self.assertIsNone(self.row()[field], field)
        retained = self.db.one("SELECT * FROM repository_backups WHERE full_name='me/demo' AND fork_sha=?",
                               (self.current,))
        self.assertTrue(backups.verified_backup(self.db, retained))
        self.assertIn(self.target, self.db.one("SELECT message FROM events ORDER BY id DESC LIMIT 1")["message"])
        push = [args for args in self.commands if args[0] == "push"]
        self.assertEqual(len(push), 1)
        self.assertIn("--force-with-lease=refs/heads/main:" + self.current, push[0])
        self.assertEqual(push[0][-2:], ("https://github.com/me/demo.git", self.target + ":refs/heads/main"))
        self.assertFalse(list((self.root / "backups").glob(".tmp-restore-*")))

    def test_selected_backup_is_pinned_before_saving_current_head_can_prune_it(self):
        original = restore.ensure_backup
        selected_path = backups.backup_path(self.db, self.backup)

        def pruning_backup(*args):
            self.assert_paused()
            self.assertTrue(list((self.root / "backups").glob(".tmp-restore-*/target.bundle")))
            selected_path.unlink()
            self.db.run("DELETE FROM repository_backups WHERE id=?", (self.backup["id"],))
            return original(*args)

        with mock.patch.object(restore, "ensure_backup", side_effect=pruning_backup):
            self.assertTrue(self.run_restore()["ok"])
        self.assertEqual(self.client.get_ref("me/demo", "main"), self.target)
        self.assert_paused()

    def test_bundle_restores_history_after_remote_rewrite_and_garbage_collection(self):
        self.git("-C", str(self.work), "checkout", "--orphan", "rewritten")
        self.git("-C", str(self.work), "rm", "-rf", ".")
        (self.work / "only.txt").write_text("unrelated replacement history\n")
        self.git("-C", str(self.work), "add", ".")
        self.git("-C", str(self.work), "commit", "-qm", "rewritten history")
        self.current = self.git("-C", str(self.work), "rev-parse", "HEAD")
        self.git("-C", str(self.work), "push", "--force", "origin", "HEAD:refs/heads/main")
        self.git("--git-dir=" + str(self.remote), "reflog", "expire", "--expire=now", "--all")
        self.git("--git-dir=" + str(self.remote), "gc", "--prune=now")
        missing = subprocess.run(["git", "--git-dir=" + str(self.remote), "cat-file", "-e", self.target],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertNotEqual(missing.returncode, 0, "Old history must be absent before testing bundle recovery")
        self.db.run("UPDATE repositories SET fork_sha=? WHERE id=?", (self.current, self.repo_id))
        self.assertTrue(self.run_restore()["ok"])
        self.assertEqual(self.client.get_ref("me/demo", "main"), self.target)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "show", "main:saved.txt"),
                         "original project content")
        self.assert_paused()

    def test_atomic_lease_rejects_a_race_without_overwriting_the_new_remote_commit(self):
        (self.work / "race.txt").write_text("new independent change\n")
        self.git("-C", str(self.work), "add", ".")
        self.git("-C", str(self.work), "commit", "-qm", "concurrent write")
        race = self.git("-C", str(self.work), "rev-parse", "HEAD")

        def race_remote():
            self.git("-C", str(self.work), "push", "origin", "main")

        self.push_hook = race_remote
        result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertIn("分支", result["error"])
        self.assertEqual(self.client.get_ref("me/demo", "main"), race)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "show", "main:race.txt"), "new independent change")
        self.assert_paused()
        self.assertTrue(self.row()["last_error"])
        self.assertEqual(len([args for args in self.commands if args[0] == "push"]), 1)

    def test_expected_head_mismatch_cancels_before_any_push(self):
        result = self.run_restore(expected_sha=self.target)
        self.assertFalse(result["ok"])
        self.assertIn("分支已变化", result["error"])
        self.assert_no_push()
        self.assert_paused()

    def test_backup_from_another_repository_is_never_imported_or_pushed(self):
        self.db.run("UPDATE repository_backups SET full_name='other/project' WHERE id=?", (self.backup["id"],))
        result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertIn("不属于", result["error"])
        self.assertFalse(self.commands)
        self.assert_no_push()
        self.assert_paused()

    def test_readding_the_same_repository_can_restore_its_retained_backup(self):
        previous_id = self.repo_id
        self.db.run("DELETE FROM repositories WHERE id=?", (previous_id,))
        self.repo_id = insert_repository(self.db, owner="me", name="demo", full_name="me/demo",
                                         upstream_full_name="up/demo", node_id="N1")
        self.assertNotEqual(self.repo_id, previous_id)
        self.assertEqual(self.backup["repo_id"], previous_id)
        self.assertTrue(self.run_restore()["ok"])
        self.assertEqual(self.client.get_ref("me/demo", "main"), self.target)
        self.assert_paused()

    def test_missing_unready_and_corrupt_backups_all_fail_before_push(self):
        path = backups.backup_path(self.db, self.backup)
        original = path.read_bytes()
        for case in ("building", "missing", "corrupt"):
            with self.subTest(case=case):
                path.write_bytes(original)
                self.db.run("UPDATE repository_backups SET status=? WHERE id=?",
                            ("building" if case == "building" else "ready", self.backup["id"]))
                if case == "missing":
                    path.unlink()
                elif case == "corrupt":
                    path.write_bytes(b"damaged bundle")
                result = self.run_restore()
                self.assertFalse(result["ok"])
                self.assert_no_push()
                self.assert_paused()

    def test_bundle_without_the_selected_commit_is_rejected_even_with_valid_file_digest(self):
        payload = backups.backup_path(self.db, self.backup).read_bytes()
        self.rewrite_backup(payload, "e" * 40)
        result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertIn("精确保留", result["error"])
        self.assert_no_push()
        self.assert_paused()

    def test_a_hash_valid_but_incremental_bundle_cannot_be_used_for_restore(self):
        incremental = self.root / "incremental.bundle"
        self.git("-C", str(self.work), "bundle", "create", str(incremental), "main", "^" + self.target)
        self.rewrite_backup(incremental.read_bytes(), self.current)
        result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assert_no_push()
        self.assert_paused()

    def test_failure_to_back_up_current_state_prevents_restore(self):
        with mock.patch.object(restore, "ensure_backup", return_value={"status": "error", "last_error": self.client.token}):
            result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertIn("备份当前提交", result["error"])
        self.assert_no_push()
        self.assert_paused()
        self.assertNotIn(self.client.token, result["error"])

    def test_current_head_is_checked_again_after_its_backup(self):
        get_ref = self.client.get_ref
        with mock.patch.object(self.client, "get_ref", side_effect=[self.current, "e" * 40]):
            result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertIn("备份期间已变化", result["error"])
        self.assertEqual(get_ref("me/demo", "main"), self.current)
        self.assert_no_push()
        self.assert_paused()

    def test_unsafe_branch_names_are_rejected_without_remote_writes(self):
        for branch in ("", "main\nother", "main\\other", "main:other", "../main", "main.lock", "a..b", "a b"):
            with self.subTest(branch=branch):
                self.db.run("UPDATE repositories SET branch=? WHERE id=?", (branch, self.repo_id))
                result = self.run_restore()
                self.assertFalse(result["ok"])
                self.assertIn("分支名称无效", result["error"])
                self.assert_no_push()
                self.assert_paused()

    def test_only_the_selected_branch_is_changed(self):
        self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/keep", self.current)
        self.assertTrue(self.run_restore()["ok"])
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/keep"), self.current)
        self.assertEqual(self.client.get_ref("me/demo", "main"), self.target)

    def test_credentials_never_appear_in_arguments_files_or_errors(self):
        self.assertTrue(self.run_restore()["ok"])
        encoded = base64.b64encode(("x-access-token:" + self.client.token).encode()).decode()
        arguments = repr(self.commands)
        self.assertNotIn(self.client.token, arguments)
        self.assertNotIn(encoded, arguments)
        for path in (self.root / "backups").rglob("*"):
            if path.is_file():
                self.assertNotIn(self.client.token.encode(), path.read_bytes(), str(path))
                self.assertNotIn(encoded.encode(), path.read_bytes(), str(path))

    def test_api_exceptions_containing_credentials_are_not_persisted_or_displayed(self):
        with mock.patch.object(self.client, "get_ref", side_effect=RuntimeError("token=" + self.client.token)):
            result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertNotIn(self.client.token, json.dumps(result))
        self.assertNotIn(self.client.token, self.row()["last_error"])
        self.assertNotIn(self.client.token, repr([dict(row) for row in self.db.all("SELECT * FROM events")]))
        self.assert_no_push()
        self.assert_paused()

    def test_timeout_after_actual_push_is_unconfirmed_and_is_never_retried(self):
        original = self.local_runner

        def timeout_after_push(args, cwd, **kwargs):
            result = original(args, cwd, **kwargs)
            if args[0] == "push":
                raise backups.BackupTimeout("Git operation timed out; " + self.client.token)
            return result

        with mock.patch.object(restore, "_run_git", side_effect=timeout_after_push):
            result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertIn("结果未确认", result["error"])
        self.assertIn("不会自动重试", result["error"])
        self.assertEqual(self.client.get_ref("me/demo", "main"), self.target)
        self.assertEqual(self.row()["fork_sha"], self.current)
        self.assert_paused()
        self.assertNotIn(self.client.token, result["error"])
        self.assertEqual(len([args for args in self.commands if args[0] == "push"]), 1)
        self.assertTrue(backups.verified_backup(self.db, self.backup))
        before = self.db.one("SELECT * FROM repository_backups WHERE fork_sha=?", (self.current,))
        self.assertTrue(backups.verified_backup(self.db, before))

    def test_success_is_not_claimed_if_final_head_cannot_be_verified(self):
        with mock.patch.object(self.client, "get_ref", side_effect=[self.current, self.current, "f" * 40]):
            result = self.run_restore()
        self.assertFalse(result["ok"])
        self.assertIn("结果未确认", result["error"])
        self.assertEqual(self.row()["fork_sha"], self.current)
        self.assert_paused()


if __name__ == "__main__":
    unittest.main()
