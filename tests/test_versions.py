"""Version comparison labels must match the engine's recorded SHAs and counts."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.versions import annotate, version_verdict

FORK = "a" * 40
UPSTREAM = "b" * 40


def repo(**overrides):
    row = {"fork_sha": FORK, "upstream_sha": UPSTREAM, "last_merged_upstream_sha": None,
           "status": "synced", "upstream_gone": 0, "last_error": None,
           "upstream_ahead": None, "fork_ahead": None}
    row.update(overrides)
    return row


class VersionVerdictTests(unittest.TestCase):
    def test_same_commit_is_reported_as_identical(self):
        verdict = version_verdict(repo(fork_sha=UPSTREAM))
        self.assertEqual(verdict["kind"], "same")
        self.assertEqual(verdict["sha"], UPSTREAM)
        self.assertIn(UPSTREAM, verdict["title"])

    def test_merged_fork_is_reported_as_containing_upstream(self):
        verdict = version_verdict(repo(last_merged_upstream_sha=UPSTREAM))
        self.assertEqual(verdict["kind"], "contains")
        self.assertEqual(verdict["sha"], UPSTREAM)
        self.assertNotIn("落后", verdict["text"])

    def test_synced_status_without_marker_still_reads_as_synced(self):
        verdict = version_verdict(repo(status="synced"))
        self.assertEqual(verdict["kind"], "contains")

    def test_own_commits_are_named_with_their_count(self):
        """本地领先 = 上游提交都已并入，差异只来自 fork 自有提交，无需更新。"""
        verdict = version_verdict(repo(upstream_ahead=0, fork_ahead=15))
        self.assertEqual(verdict["kind"], "contains")
        self.assertIn("本地领先", verdict["text"])
        self.assertIn("15", verdict["text"])
        self.assertIn("15", verdict["title"])
        self.assertIn("无需更新", verdict["title"])
        self.assertNotIn("落后", verdict["text"])

    def test_upstream_commits_missing_locally_are_counted(self):
        verdict = version_verdict(repo(status="behind", upstream_ahead=3, fork_ahead=15))
        self.assertEqual(verdict["kind"], "behind")
        self.assertIn("落后上游 3", verdict["text"])
        self.assertIn("需要更新", verdict["title"])

    def test_counts_are_ignored_while_the_fork_has_diverged(self):
        """历史分叉时先说明原因，不能只报一个领先/落后数字。"""
        verdict = version_verdict(repo(status="diverged", upstream_ahead=2, fork_ahead=4,
                                       last_error="fork 有 4 个独有提交（落后上游 2 个），不会覆盖"))
        self.assertEqual(verdict["kind"], "diverged")
        self.assertIn("分叉", verdict["text"])
        self.assertIn("不会覆盖", verdict["title"])

    def test_behind_fork_points_at_the_upstream_sha(self):
        verdict = version_verdict(repo(status="behind"))
        self.assertEqual(verdict["kind"], "behind")
        self.assertEqual(verdict["sha"], UPSTREAM)
        self.assertIn(UPSTREAM, verdict["title"])

    def test_conflicting_fork_is_not_reported_as_synced(self):
        for status in ("diverged", "blocked"):
            with self.subTest(status=status):
                verdict = version_verdict(repo(status=status))
                self.assertEqual(verdict["kind"], "diverged")
                self.assertEqual(verdict["sha"], UPSTREAM)
                self.assertIn("不是同一版本", verdict["title"])
        for status in ("error", "needs_attention"):
            with self.subTest(status=status):
                verdict = version_verdict(repo(status=status))
                self.assertEqual(verdict["kind"], "unsynced")
                self.assertEqual(verdict["sha"], UPSTREAM)

    def test_failed_check_does_not_borrow_stored_counts(self):
        """检查失败时不能拿上一次的计数当原因说「无需更新」。"""
        verdict = version_verdict(repo(status="error", upstream_ahead=0, fork_ahead=15,
                                       last_error="读取上游分支失败：502 Bad Gateway"))
        self.assertEqual(verdict["kind"], "unsynced")
        self.assertIn("502", verdict["title"])
        self.assertNotIn("无需更新", verdict["title"])

    def test_deleted_upstream_never_claims_synced(self):
        verdict = version_verdict(repo(upstream_gone=1, upstream_sha=UPSTREAM))
        self.assertEqual(verdict["kind"], "gone")
        self.assertEqual(verdict["sha"], "")

    def test_unchecked_repository_stays_pending(self):
        verdict = version_verdict(repo(fork_sha=None, upstream_sha=None, status="pending"))
        self.assertEqual(verdict["kind"], "pending")
        self.assertEqual(verdict["sha"], "")

    def test_annotate_adds_verdict_and_local_sha_tone(self):
        rows = annotate([repo(status="behind"), repo(fork_sha=UPSTREAM),
                         repo(status="diverged")])
        self.assertEqual(rows[0]["verdict"]["kind"], "behind")
        self.assertEqual(rows[0]["fork_tone"], "sha-behind")
        self.assertEqual(rows[1]["verdict"]["kind"], "same")
        self.assertEqual(rows[1]["fork_tone"], "")
        self.assertEqual(rows[2]["verdict"]["kind"], "diverged")
        self.assertEqual(rows[2]["fork_tone"], "sha-unsynced")

    def test_annotate_does_not_mutate_input_rows(self):
        original = repo(status="behind")
        annotate([original])
        self.assertNotIn("verdict", original)


if __name__ == "__main__":
    unittest.main()
