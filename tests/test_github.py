"""GitHub response classification at the transport boundary."""
import unittest
from unittest import mock

from app.github import (AuthError, Forbidden, GitHub, MergeConflict, RateLimited,
                        RefNotFastForward, RefObjectNotFound)


class UpdateRefTests(unittest.TestCase):
    def test_ref_validation_errors_are_classified_by_cause_not_status_alone(self):
        cases = [
            ("Object does not exist", RefObjectNotFound),
            ("Update is not a fast forward", RefNotFastForward),
            ("Not a fast-forward update.", RefNotFastForward),
            ("Validation failed", RuntimeError),
            ("Reference update failed", RuntimeError),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                client = GitHub()
                payload = {"message": message, "documentation_url": "https://docs.github.com/rest/git/refs"}
                with mock.patch.object(client, "installation_token", return_value="test-token"), \
                     mock.patch.object(client, "_request", return_value=(422, payload, {})) as request:
                    with self.assertRaises(expected) as caught:
                        client.update_ref("me/demo", "main", "a" * 40)
                self.assertIs(type(caught.exception), expected)
                self.assertIn("422", str(caught.exception))
                self.assertIn("me/demo:main", str(caught.exception))
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("documentation_url", str(caught.exception))
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.args[3], {"sha": "a" * 40, "force": False})

    def test_missing_object_is_not_retried_inside_client_even_with_force(self):
        client = GitHub()
        with mock.patch.object(client, "installation_token", return_value="test-token"), \
             mock.patch.object(client, "_request", return_value=(422, {"message": "Object does not exist"}, {})) as request:
            with self.assertRaises(RefObjectNotFound):
                client.update_ref("me/demo", "main", "a" * 40, force=True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[3], {"sha": "a" * 40, "force": True})

    def test_other_validation_error_preserves_structured_diagnostic_details(self):
        client = GitHub()
        payload = {"message": "Validation failed", "errors": [{"resource": "Reference", "code": "invalid"}]}
        with mock.patch.object(client, "installation_token", return_value="test-token"), \
             mock.patch.object(client, "_request", return_value=(422, payload, {})):
            with self.assertRaises(RuntimeError) as caught:
                client.update_ref("me/demo", "main", "a" * 40)
        self.assertIs(type(caught.exception), RuntimeError)
        self.assertIn('"code": "invalid"', str(caught.exception))


class FileTreeTests(unittest.TestCase):
    commit_sha = "a" * 40
    tree_sha = "b" * 40

    def commit(self):
        return {"sha": self.commit_sha, "tree": {"sha": self.tree_sha}}

    def tree(self, entries=()):
        return {"sha": self.tree_sha, "truncated": False, "tree": list(entries)}

    def entry(self, path, kind="blob", mode="100644"):
        return {"path": path, "type": kind, "mode": mode, "sha": "c" * 40}

    def test_file_paths_reads_the_exact_commit_tree_and_counts_symlinks_and_submodules(self):
        client = GitHub()
        payload = self.tree([
            self.entry("README.md"), self.entry("src", "tree", "040000"),
            self.entry("src/app.py"), self.entry("link", mode="120000"),
            self.entry("vendor", "commit", "160000"),
        ])
        with mock.patch.object(client, "_api", side_effect=[(200, self.commit()), (200, payload)]) as api:
            self.assertEqual(client.file_paths("me/demo", self.commit_sha),
                             {"README.md", "src/app.py", "link", "vendor"})
        self.assertEqual(api.call_args_list, [
            mock.call("GET", f"/repos/me/demo/git/commits/{self.commit_sha}"),
            mock.call("GET", f"/repos/me/demo/git/trees/{self.tree_sha}?recursive=1"),
        ])

    def test_a_complete_empty_tree_is_distinct_from_a_missing_or_partial_tree(self):
        client = GitHub()
        with mock.patch.object(client, "_api", side_effect=[(200, self.commit()), (200, self.tree())]):
            self.assertEqual(client.file_paths("me/demo", self.commit_sha), set())
        for truncated in (True, None, 0, "false"):
            with self.subTest(truncated=truncated):
                payload = self.tree()
                if truncated is None:
                    del payload["truncated"]
                else:
                    payload["truncated"] = truncated
                with mock.patch.object(client, "_api", side_effect=[(200, self.commit()), (200, payload)]):
                    with self.assertRaisesRegex(RuntimeError, "截断|完整"):
                        client.file_paths("me/demo", self.commit_sha)

    def test_malformed_commit_payloads_cannot_be_treated_as_empty_repositories(self):
        client = GitHub()
        cases = (None, [], {}, {"sha": "d" * 40, "tree": {"sha": self.tree_sha}},
                 {"sha": self.commit_sha}, {"sha": self.commit_sha, "tree": []},
                 {"sha": self.commit_sha, "tree": {"sha": "main"}})
        for payload in cases:
            with self.subTest(payload=payload), \
                 mock.patch.object(client, "_api", return_value=(200, payload)) as api:
                with self.assertRaisesRegex(RuntimeError, "文件树检测失败.*me/demo@aaaaaaaaaaaa"):
                    client.file_paths("me/demo", self.commit_sha)
                self.assertEqual(api.call_count, 1)

    def test_malformed_recursive_tree_payloads_fail_closed(self):
        client = GitHub()
        valid = self.tree([self.entry("README.md")])
        cases = [None, [], {}, {**valid, "sha": "d" * 40}, {**valid, "tree": None},
                 {**valid, "tree": {}}, self.tree([None]), self.tree([{}]),
                 self.tree([self.entry("../outside")]), self.tree([self.entry("/outside")]),
                 self.tree([self.entry("src//file")]), self.tree([self.entry("bad\x00name")]),
                 self.tree([self.entry("README.md", "unknown")]),
                 self.tree([self.entry("README.md", [])]),
                 self.tree([{**self.entry("README.md"), "mode": []}]),
                 self.tree([{**self.entry("README.md"), "sha": "main"}]),
                 self.tree([self.entry("README.md"), self.entry("README.md")]),
                 self.tree([self.entry("src/app.py")]),
                 self.tree([self.entry("src"), self.entry("src/app.py")])]
        for index, payload in enumerate(cases):
            with self.subTest(case=index), \
                 mock.patch.object(client, "_api", side_effect=[(200, self.commit()), (200, payload)]):
                with self.assertRaisesRegex(RuntimeError, "文件树检测失败"):
                    client.file_paths("me/demo", self.commit_sha)

    def test_network_failure_has_repository_context_and_typed_auth_errors_are_preserved(self):
        client = GitHub()
        for error in (OSError("connection lost"), RateLimited("rate limit"), AuthError("expired")):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(client, "_api", side_effect=error) as api:
                expected = RuntimeError if isinstance(error, OSError) else type(error)
                with self.assertRaises(expected) as caught:
                    client.file_paths("me/demo", self.commit_sha)
                if isinstance(error, OSError):
                    self.assertIn("me/demo@aaaaaaaaaaaa", str(caught.exception))
                    self.assertIn("connection lost", str(caught.exception))
                else:
                    self.assertIs(caught.exception, error)
                self.assertEqual(api.call_count, 1)

    def test_branch_names_and_short_shas_are_not_accepted_for_detection(self):
        client = GitHub()
        for ref in (None, "main", "abc123", "a" * 40 + "/main", "a" * 39 + "g"):
            with self.subTest(ref=ref), mock.patch.object(client, "_api") as api:
                with self.assertRaises(ValueError):
                    client.file_paths("me/demo", ref)
                api.assert_not_called()


class MergeCommitTests(unittest.TestCase):
    target_sha = "d" * 40

    def test_merge_sources_only_the_reviewed_sha_not_a_moving_upstream_branch(self):
        client = GitHub()
        payload = {"sha": "e" * 40, "message": "merged reviewed target"}
        with mock.patch.object(client, "installation_token", return_value="test-token"), \
             mock.patch.object(client, "_request", return_value=(201, payload, {})) as request:
            result = client.merge_commit("me/demo", "release/main", self.target_sha)
        self.assertEqual(result, {**payload, "merge_type": "merge"})
        self.assertEqual(request.call_args.args[0:2], ("POST", "https://api.github.com/repos/me/demo/merges"))
        self.assertEqual(request.call_args.args[3], {"base": "release/main", "head": self.target_sha})
        self.assertNotIn("merge-upstream", request.call_args.args[1])

    def test_fast_forward_and_already_merged_results_are_identified(self):
        client = GitHub()
        for status, payload, expected in ((201, {"sha": self.target_sha}, "fast-forward"),
                                          (204, None, "none")):
            with self.subTest(status=status), \
                 mock.patch.object(client, "installation_token", return_value="test-token"), \
                 mock.patch.object(client, "_request", return_value=(status, payload, {})):
                self.assertEqual(client.merge_commit("me/demo", "main", self.target_sha)["merge_type"], expected)

    def test_merge_errors_keep_existing_conflict_permission_auth_and_rate_contracts(self):
        cases = ((409, "Merge conflict", MergeConflict),
                 (422, "Validation failed", MergeConflict),
                 (422, "Cannot update workflow file", Forbidden),
                 (403, "Resource not accessible", Forbidden),
                 (403, "API rate limit exceeded", RateLimited),
                 (401, "Bad credentials", AuthError),
                 (401, "API rate limit exceeded", RateLimited),
                 (429, "Too many requests", RateLimited),
                 (404, "Not Found", RuntimeError),
                 (500, "Server error", RuntimeError))
        for status, message, expected in cases:
            with self.subTest(status=status, message=message):
                client = GitHub()
                client._token = "stale-token"
                with mock.patch.object(client, "installation_token", return_value="test-token"), \
                     mock.patch.object(client, "_request", return_value=(status, {"message": message}, {})) as request:
                    with self.assertRaises(expected) as caught:
                        client.merge_commit("me/demo", "main", self.target_sha)
                self.assertIs(type(caught.exception), expected)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(request.call_count, 1)
                if expected is AuthError:
                    self.assertIsNone(client._token)

    def test_success_without_a_valid_result_sha_is_not_reported_as_synced(self):
        client = GitHub()
        for payload in (None, [], {}, {"sha": "short"}):
            with self.subTest(payload=payload), \
                 mock.patch.object(client, "installation_token", return_value="test-token"), \
                 mock.patch.object(client, "_request", return_value=(201, payload, {})):
                with self.assertRaisesRegex(RuntimeError, "valid commit SHA"):
                    client.merge_commit("me/demo", "main", self.target_sha)

    def test_unpinned_targets_are_rejected_before_any_request(self):
        client = GitHub()
        for target in (None, "upstream/main", "d" * 12):
            with self.subTest(target=target), mock.patch.object(client, "_request") as request:
                with self.assertRaises(ValueError):
                    client.merge_commit("me/demo", "main", target)
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
