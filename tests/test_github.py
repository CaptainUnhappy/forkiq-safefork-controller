import unittest
import ssl
import urllib.error
from unittest.mock import Mock, patch
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


class ReadTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHub(token="fixture-only")
        self.response = Mock(status=200, headers={})
        self.response.read.return_value = b'{"id": 12}'
        self.context = Mock()
        self.context.__enter__ = Mock(return_value=self.response)
        self.context.__exit__ = Mock(return_value=False)
        self.opener = Mock()
        self.build = patch('app.github.urllib.request.build_opener', return_value=self.opener).start()
        self.sleep = patch('app.github.time.sleep').start()
        self.addCleanup(patch.stopall)

    def request(self, method='GET'):
        return self.client._request(method, 'https://api.github.com/repos/a/b',
                                    {'Authorization': 'Bearer fixture-only'})

    def test_read_recovers_from_transient_connection_failure(self):
        self.opener.open.side_effect = [urllib.error.URLError('temporary'), self.context]
        self.assertEqual(self.request()[:2], (200, {'id': 12}))
        self.assertEqual(self.opener.open.call_count, 2)
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertIsNot(requests[0], requests[1])

    def test_read_recovers_from_transient_gateway_error(self):
        self.opener.open.side_effect = [urllib.error.HTTPError('https://api.github.com', 503, '', {}, None), self.context]
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_read_retry_is_bounded_and_does_not_disclose_error(self):
        self.opener.open.side_effect = urllib.error.URLError('fixture-secret')
        with self.assertRaises(RuntimeError) as raised:
            self.request()
        self.assertEqual(self.opener.open.call_count, 4)
        self.assertNotIn('fixture-secret', str(raised.exception))

    def test_permissions_rate_limits_and_redirects_are_not_retried(self):
        for code in (301, 401, 403, 404, 429):
            with self.subTest(code=code):
                self.opener.reset_mock()
                self.opener.open.side_effect = urllib.error.HTTPError('https://api.github.com', code, '', {}, None)
                self.assertEqual(self.request()[0], code)
                self.opener.open.assert_called_once()

    def test_certificate_validation_failure_is_not_retried(self):
        self.opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError('invalid certificate'))
        with self.assertRaises(RuntimeError):
            self.request()
        self.opener.open.assert_called_once()

    def test_credential_post_is_not_retried(self):
        self.opener.open.side_effect = urllib.error.URLError('temporary')
        with self.assertRaises(RuntimeError):
            self.request('POST')
        self.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
