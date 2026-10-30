import unittest
from unittest.mock import Mock
from app.github import GitHub, AuthError, RateLimited, NoRedirect


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHub(token="fixture-only")
        self.client._request = Mock()

    def test_no_write_api_or_legacy_mutators(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(RuntimeError):
                self.client._api(method, "/repos/a/b")
        self.client._request.assert_not_called()
        for name in ("update_ref", "merge_commit", "merge_upstream", "set_actions_enabled", "dispatch_workflow", "enable_workflow"):
            self.assertFalse(hasattr(self.client, name), name)

    def test_actions_requires_explicit_boolean(self):
        for payload in ({}, {"enabled": 0}, {"enabled": "false"}):
            self.client._request.return_value = (200, payload, {})
            with self.assertRaises(RuntimeError):
                self.client.actions_enabled("a/b")
        self.client._request.return_value = (200, {"enabled": False}, {})
        self.assertFalse(self.client.actions_enabled("a/b"))

    def test_all_inflight_states_checked(self):
        self.client._request.return_value = (200, {"total_count": 0, "workflow_runs": []}, {})
        self.assertFalse(self.client.has_active_runs("a/b"))
        self.assertEqual(len(self.client._request.call_args_list), 5)
        paths = str(self.client._request.call_args_list)
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            self.assertIn("status=" + status, paths)

    def test_incomplete_or_active_runs_fail_closed(self):
        self.client._request.return_value = (200, {"total_count": 1, "workflow_runs": []}, {})
        self.assertTrue(self.client.has_active_runs("a/b"))
        self.client._request.return_value = (200, {"total_count": 0}, {})
        with self.assertRaises(RuntimeError):
            self.client.has_active_runs("a/b")

    def test_auth_errors_and_redirect_never_choose_fallback(self):
        for status in (301, 302, 401, 403, 404, 500):
            self.client._request.return_value = (status, {"message": "fixture-secret"}, {})
            with self.assertRaises(RuntimeError) as raised:
                self.client.get_repo("a/b")
            self.assertNotIn("fixture-secret", str(raised.exception))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))

    def test_rate_limit_stops(self):
        self.client._request.return_value = (403, {}, {"X-RateLimit-Remaining": "0"})
        with self.assertRaises(RateLimited):
            self.client.get_repo("a/b")

    def test_token_bootstrap_is_only_authorized_post(self):
        client = GitHub("123", "unused", "456")
        client.app_jwt = Mock(return_value="fixture-jwt")
        client._request = Mock(return_value=(201, {"token": "fixture-installation"}, {}))
        self.assertEqual(client.installation_token(), "fixture-installation")
        self.assertEqual(client.installation_token(), "fixture-installation")
        client._request.assert_called_once()
        args = client._request.call_args.args
        self.assertEqual(args[:2], ("POST", "https://api.github.com/app/installations/456/access_tokens"))


if __name__ == "__main__":
    unittest.main()
