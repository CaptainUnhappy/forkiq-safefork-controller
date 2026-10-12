"""Version comparison labels must match the engine's recorded SHAs."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.versions import annotate, version_verdict

FORK = "a" * 40
UPSTREAM = "b" * 40


def repo(**overrides):
    row = {"fork_sha": FORK, "upstream_sha": UPSTREAM, "last_merged_upstream_sha": None,
           "status": "synced", "upstream_gone": 0}
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

    def test_synced_status_without_marker_still_reads_as_synced(self):
        verdict = version_verdict(repo(status="synced"))
        self.assertEqual(verdict["kind"], "contains")

    def test_behind_fork_points_at_the_upstream_sha(self):
        verdict = version_verdict(repo(status="behind"))
        self.assertEqual(verdict["kind"], "behind")
        self.assertEqual(verdict["sha"], UPSTREAM)
        self.assertIn(UPSTREAM, verdict["title"])

    def test_conflicting_fork_is_not_reported_as_synced(self):
        for status in ("diverged", "blocked", "error", "needs_attention"):
            with self.subTest(status=status):
                verdict = version_verdict(repo(status=status))
                self.assertEqual(verdict["kind"], "unsynced")
                self.assertEqual(verdict["sha"], UPSTREAM)

    def test_deleted_upstream_never_claims_synced(self):
        verdict = version_verdict(repo(upstream_gone=1, upstream_sha=UPSTREAM))
        self.assertEqual(verdict["kind"], "gone")
        self.assertEqual(verdict["sha"], "")

    def test_unchecked_repository_stays_pending(self):
        verdict = version_verdict(repo(fork_sha=None, upstream_sha=None, status="pending"))
        self.assertEqual(verdict["kind"], "pending")
        self.assertEqual(verdict["sha"], "")

    def test_annotate_adds_verdict_and_local_sha_tone(self):
        rows = annotate([repo(status="behind"), repo(fork_sha=UPSTREAM)])
        self.assertEqual(rows[0]["verdict"]["kind"], "behind")
        self.assertEqual(rows[0]["fork_tone"], "sha-behind")
        self.assertEqual(rows[1]["verdict"]["kind"], "same")
        self.assertEqual(rows[1]["fork_tone"], "")

    def test_annotate_does_not_mutate_input_rows(self):
        original = repo(status="behind")
        annotate([original])
        self.assertNotIn("verdict", original)


if __name__ == "__main__":
    unittest.main()
