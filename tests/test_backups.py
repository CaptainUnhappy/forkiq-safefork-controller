"""Real Git recovery plus fail-closed server backup and retention contracts."""
import base64
import errno
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import backups
from app.db import DB, insert_repository
from app.github import AuthError


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.db = DB(self.state / "forkiq.db")
        self.addCleanup(self.db.close)
        self.repo_id = insert_repository(self.db, owner="me", name="demo", full_name="me/demo",
                                         upstream_full_name="up/demo")
        self.row = self.db.one("SELECT * FROM repositories WHERE id=?", (self.repo_id,))
        self.source = self.base / "source"
        self.source.mkdir()
        self.git("init", "--initial-branch=main", cwd=self.source)
        (self.source / "keep.txt").write_text("first\n")
        (self.source / "recover.txt").write_text("irreplaceable source code\n")
        self.commits = [self.commit("first")]
        (self.source / "keep.txt").write_text("second\n")
        self.commits.append(self.commit("second"))
        (self.source / "recover.txt").unlink()
        self.commits.append(self.commit("delete upstream content"))
        self.client = mock.Mock()
        self.token = "ghs_TEST_SECRET_MUST_NOT_PERSIST"
        self.client.installation_token.return_value = self.token
        patcher = mock.patch("app.backups._fetch_repository", side_effect=self.fetch_local)
        self.fetcher = patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, cwd):
        env = backups._git_env()
        env["GIT_ALLOW_PROTOCOL"] = "file"
        result = subprocess.run(
            ["git", "-c", "user.name=ForkIQ test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             "-c", "init.templateDir=", "-c", "protocol.file.allow=always", *args],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, timeout=30)
        return result.stdout.decode().strip()

    def commit(self, message):
        self.git("add", "-A", cwd=self.source)
        self.git("commit", "-m", message, cwd=self.source)
        return self.git("rev-parse", "HEAD", cwd=self.source)

    def fetch_local(self, client, full_name, sha, target_gitdir):
        self.git("fetch", "--no-tags", "--no-recurse-submodules", str(self.source), sha,
                 cwd=target_gitdir)

    def backup(self, sha=None, row=None):
        return backups.ensure_backup(self.db, self.client, row or self.row, sha or self.commits[1])

    def assert_ready(self, record):
        self.assertEqual(record["status"], "ready", record.get("last_error"))
        self.assertTrue(backups.verified_backup(self.db, record))
        return backups.backup_path(self.db, record)

    def records(self, full_name="me/demo"):
        return self.db.all("SELECT * FROM repository_backups WHERE full_name=? ORDER BY id", (full_name,))

    def more_commits(self, count=2):
        for number in range(count):
            (self.source / "keep.txt").write_text(f"new version {number}\n")
            self.commits.append(self.commit(f"new version {number}"))

    def test_self_contained_bundle_restores_deleted_files_after_source_history_is_gone(self):
        record = self.backup(self.commits[1])
        bundle = self.assert_ready(record)
        self.git("checkout", "--orphan", "rewritten", cwd=self.source)
        self.git("rm", "-rf", ".", cwd=self.source)
        (self.source / "empty-replacement").write_text("rewritten history\n")
        self.commit("replace all history")
        shutil.rmtree(self.source)
        restored = self.base / "restored"
        self.git("clone", "--branch", "preserved", str(bundle), str(restored), cwd=self.base)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=restored), self.commits[1])
        self.assertEqual(self.git("rev-list", "--count", "HEAD", cwd=restored), "2")
        self.assertEqual((restored / "recover.txt").read_text(), "irreplaceable source code\n")
        self.assertEqual(self.git("show", self.commits[0] + ":keep.txt", cwd=restored), "first")
        self.git("fsck", "--full", "--strict", cwd=restored)

    def test_ready_snapshot_is_deduplicated_and_verified_without_fetch(self):
        first = self.backup()
        second = self.backup(self.commits[1].upper())
        self.assert_ready(second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.fetcher.call_count, 1)
        self.assertEqual(len(self.records()), 1)

    def test_missing_ready_file_is_rebuilt_using_same_record(self):
        first = self.backup()
        self.assert_ready(first).unlink()
        second = self.backup()
        self.assert_ready(second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.fetcher.call_count, 2)

    def test_corrupt_ready_file_is_rebuilt_using_same_record(self):
        first = self.backup()
        path = self.assert_ready(first)
        path.write_bytes(b"x" * first["size_bytes"])
        self.assertFalse(backups.verified_backup(self.db, first))
        second = self.backup()
        self.assert_ready(second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.fetcher.call_count, 2)

    def test_only_latest_three_successful_snapshots_per_repository_are_retained(self):
        self.more_commits()
        snapshots = [self.backup(sha) for sha in self.commits]
        # Same-second creation timestamps are ordered by the monotonic row id.
        records = self.records()
        self.assertEqual([record["id"] for record in records], [record["id"] for record in snapshots[-3:]])
        for record in records:
            self.assert_ready(dict(record))
        for record in snapshots[:-3]:
            self.assertFalse(backups.backup_path(self.db, record).exists())
        self.assertEqual(len(list((self.state / "backups").glob("*.bundle"))), 3)

    def test_failed_fourth_snapshot_does_not_prune_any_good_backups(self):
        self.more_commits(1)
        snapshots = [self.backup(sha) for sha in self.commits[:3]]
        self.fetcher.side_effect = RuntimeError("network lost")
        failed = self.backup(self.commits[3])
        self.assertEqual(failed["status"], "error")
        self.assertEqual(len([row for row in self.records() if row["status"] == "ready"]), 3)
        for record in snapshots:
            self.assert_ready(record)

    def test_retention_does_not_delete_another_repository(self):
        other_id = insert_repository(self.db, owner="me", name="other", full_name="me/other",
                                     upstream_full_name="up/other")
        other = self.db.one("SELECT * FROM repositories WHERE id=?", (other_id,))
        other_record = self.backup(row=other)
        self.more_commits(1)
        for sha in self.commits:
            self.assert_ready(self.backup(sha))
        self.assert_ready(other_record)
        self.assertEqual(len(self.records("me/other")), 1)
        self.assertEqual(len(self.records()), 3)

    def test_failed_snapshot_retries_only_after_backoff(self):
        self.fetcher.side_effect = RuntimeError("offline")
        first = self.backup()
        self.assertEqual(first["status"], "error")
        self.assertEqual(self.backup()["id"], first["id"])
        self.assertEqual(self.fetcher.call_count, 1)
        stale = (datetime.now(timezone.utc) - timedelta(seconds=backups.RETRY_SECONDS + 5)).isoformat()
        self.db.run("UPDATE repository_backups SET updated_at=? WHERE id=?", (stale, first["id"]))
        self.fetcher.side_effect = self.fetch_local
        recovered = self.backup()
        self.assert_ready(recovered)
        self.assertEqual(first["id"], recovered["id"])
        self.assertEqual(self.fetcher.call_count, 2)

    def test_rebuilt_old_record_is_kept_even_with_newer_ids_in_the_same_second(self):
        self.more_commits(1)
        self.fetcher.side_effect = RuntimeError("offline")
        old = self.backup(self.commits[0])
        self.fetcher.side_effect = self.fetch_local
        newer = [self.backup(sha) for sha in self.commits[1:]]
        self.db.run("UPDATE repository_backups SET created_at=?,updated_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", old["id"]))
        # All successes share a timestamp, so the old id must be explicitly kept.
        fixed_time = "2026-10-28T12:00:00+00:00"
        self.db.run("UPDATE repository_backups SET created_at=? WHERE status='ready'", (fixed_time,))
        with mock.patch("app.backups.utcnow", return_value=fixed_time):
            rebuilt = self.backup(self.commits[0])
        self.assert_ready(rebuilt)
        self.assertEqual(rebuilt["id"], old["id"])
        self.assertEqual(rebuilt["created_at"], fixed_time)
        self.assertEqual(len(self.records()), 3)
        self.assertFalse(backups.backup_path(self.db, newer[0]).exists())
        for record in newer[1:]:
            self.assert_ready(record)

    def test_backup_is_rechecked_after_retention_before_allowing_sync(self):
        def invalidate(db, name, newest_id):
            record = db.one("SELECT * FROM repository_backups WHERE id=?", (newest_id,))
            backups.backup_path(db, record).unlink()

        with mock.patch("app.backups._prune", side_effect=invalidate):
            result = self.backup()
        self.assertEqual(result["status"], "error")
        self.assertIn("完整性", result["last_error"])

    def test_disk_space_failure_is_visible_and_does_not_prune(self):
        good = self.backup(self.commits[0])
        with mock.patch("app.backups.shutil.disk_usage", return_value=mock.Mock(free=0)):
            failed = self.backup(self.commits[1])
        self.assertEqual(failed["status"], "error")
        self.assertIn("256 MiB", failed["last_error"])
        self.assert_ready(good)
        self.assertEqual(self.fetcher.call_count, 1)

    def test_disk_full_and_authentication_exceptions_never_expose_secrets(self):
        encoded = base64.b64encode(("x-access-token:" + self.token).encode()).decode()
        with self.subTest("disk full"):
            self.fetcher.side_effect = OSError(errno.ENOSPC, self.token + encoded)
            failed = self.backup(self.commits[0])
            self.assertEqual(failed["status"], "error")
            self.assertIn("磁盘空间", failed["last_error"])
        with self.subTest("authorization"):
            self.client.installation_token.side_effect = AuthError(self.token + encoded)
            self.fetcher.side_effect = lambda *args: backups._authenticated_git_env(args[0])
            failed = self.backup(self.commits[1])
            self.assertEqual(failed["status"], "error")
            self.assertIn("授权失败", failed["last_error"])
        for path in self.state.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(self.token.encode(), content)
                self.assertNotIn(encoded.encode(), content)

    def test_temporary_artifacts_are_cleaned_after_success_and_failure(self):
        self.assert_ready(self.backup())
        self.fetcher.side_effect = RuntimeError("failure")
        self.assertEqual(self.backup(self.commits[2])["status"], "error")
        self.assertFalse(list((self.state / "backups").glob(".tmp-*")))

    def test_files_and_directories_have_restrictive_permissions(self):
        path = self.assert_ready(self.backup())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((path.parent / ".lock").stat().st_mode), 0o600)

    def test_path_rejects_invalid_ids_and_partial_or_invalid_commits(self):
        self.assert_ready(self.backup())
        for value in (0, -1, True, "../outside", "1/../2", "1.0"):
            with self.subTest(value=value), self.assertRaises(backups.BackupError):
                backups.backup_path(self.db, {"id": value, "fork_sha": self.commits[0]})
        for sha in ("abc123", "a" * 39, "g" * 40, self.commits[0] + "\n"):
            with self.subTest(sha=sha), self.assertRaises(backups.BackupError):
                backups.backup_path(self.db, {"id": 1, "fork_sha": sha})

    def test_invalid_repository_names_and_commits_do_not_run_git(self):
        for name in ("../demo", "me/..", "https://github.com/me/demo", "me/demo;rm", "me/a/b"):
            with self.subTest(name=name):
                failed = self.backup(row={"id": 1, "full_name": name})
                self.assertEqual(failed["status"], "error")
        self.assertEqual(self.backup("a" * 39)["status"], "error")
        self.fetcher.assert_not_called()
        self.assertFalse(self.records())

    def test_root_and_target_symlinks_are_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        root = self.state / "backups"
        root.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.backup()["status"], "error")
        self.assertFalse(list(outside.iterdir()))
        root.unlink()
        record = self.backup()
        path = self.assert_ready(record)
        path.unlink()
        valuable = outside / "valuable"
        valuable.write_text("do not touch")
        path.symlink_to(valuable)
        self.assertFalse(backups.verified_backup(self.db, record))
        failed = self.backup()
        self.assertEqual(failed["status"], "error")
        self.assertEqual(valuable.read_text(), "do not touch")

    def test_integrity_checks_require_ready_regular_file_correct_size_and_hash(self):
        record = self.backup()
        self.assert_ready(record)
        for changes in ({"status": "error"}, {"size_bytes": record["size_bytes"] + 1},
                        {"sha256": "0" * 64}):
            with self.subTest(changes=changes):
                self.assertFalse(backups.verified_backup(self.db, {**record, **changes}))
        path = backups.backup_path(self.db, record)
        path.unlink()
        path.mkdir()
        self.assertFalse(backups.verified_backup(self.db, record))

    def test_removing_managed_repository_preserves_backup_record_and_bundle(self):
        record = self.backup()
        self.db.run("DELETE FROM repositories WHERE id=?", (self.repo_id,))
        persisted = dict(self.db.one("SELECT * FROM repository_backups WHERE id=?", (record["id"],)))
        self.assert_ready(persisted)

    def test_incorrect_fetched_commit_cannot_be_marked_ready(self):
        self.fetcher.side_effect = lambda client, name, sha, target: self.fetch_local(
            client, name, self.commits[0], target)
        failed = self.backup(self.commits[2])
        self.assertEqual(failed["status"], "error")
        self.assertFalse(list((self.state / "backups").glob("*.bundle")))

    def test_git_timeout_is_visible_and_never_marks_backup_ready(self):
        self.fetcher.side_effect = backups.BackupTimeout("Git operation timed out; its result must be checked.")
        failed = self.backup()
        self.assertEqual(failed["status"], "error")
        self.assertIn("timed out", failed["last_error"])
        self.assertFalse(list((self.state / "backups").glob("*.bundle")))


class GitTransportTests(unittest.TestCase):
    def test_fetch_token_is_only_in_environment_and_inherited_git_controls_are_scrubbed(self):
        client = mock.Mock()
        token = "ghs_SUPER_SECRET"
        client.installation_token.return_value = token
        # Use the original function even though other tests patch its public seam.
        with mock.patch.dict(os.environ, {"GIT_DIR": "/evil", "GIT_TRACE": "1",
                                          "GIT_CONFIG_PARAMETERS": "evil",
                                          "GIT_CONFIG_COUNT": "9"}), \
                mock.patch("app.backups._run_git") as run:
            backups._fetch_repository(client, "me/demo", "a" * 40, Path("/temporary.git"))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["fetch", "--no-tags", "--no-recurse-submodules",
                                  "https://github.com/me/demo.git", "a" * 40])
        self.assertNotIn(token, str(args))
        env = kwargs["env"]
        encoded = base64.b64encode(("x-access-token:" + token).encode()).decode()
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Basic " + encoded)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        for key in ("GIT_DIR", "GIT_TRACE", "GIT_CONFIG_PARAMETERS"):
            self.assertNotIn(key, env)

    def test_runner_restricts_memory_protocols_hooks_and_output(self):
        process = mock.Mock()
        process.wait.return_value = 0
        with mock.patch("app.backups.subprocess.Popen", return_value=process) as popen:
            backups._run_git(["init", "--bare"], Path("/temporary.git"))
        argv = popen.call_args.args[0]
        for setting in ("pack.threads=1", "pack.windowMemory=16m", "pack.deltaCacheSize=8m",
                        "credential.helper=", "core.hooksPath=/dev/null", "protocol.allow=never",
                        "protocol.https.allow=always", "http.followRedirects=false"):
            self.assertIn(setting, argv)
        options = popen.call_args.kwargs
        self.assertIs(options["stderr"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.DEVNULL)
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertTrue(options["start_new_session"])
        process.wait.assert_called_once_with(timeout=backups.COMMAND_TIMEOUT)

    def test_runner_timeout_kills_fetch_process_group_and_raises_safe_timeout(self):
        process = mock.Mock(pid=123456)
        process.wait.side_effect = [subprocess.TimeoutExpired("SECRET", 600), 0]
        with mock.patch("app.backups.subprocess.Popen", return_value=process), \
                mock.patch("app.backups.os.killpg") as kill:
            with self.assertRaises(backups.BackupTimeout) as raised:
                backups._run_git(["fetch", "https://github.com/me/demo.git", "a" * 40], Path("/tmp"))
        self.assertNotIn("SECRET", str(raised.exception))
        kill.assert_called_once_with(123456, backups.signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
