"""Real credential transport -> durable scheduler, with no external network."""
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from app.db import DB, insert_repository
from app.engine import run_cycle
from app.github import GitHub
from app.gitstore import PushNetworkError
from test_safefork import MemoryStore, config, metadata


class CredentialRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / 'forkiq.db')
        self.addCleanup(self.db.close)
        self.cfg = config()
        self.source, self.target = metadata(self.cfg)
        self.key = insert_repository(self.db, owner='owner', name='project-SafeFork',
                                     full_name=self.cfg['target'], upstream_full_name=self.cfg['source'])
        self.db.run('UPDATE repositories SET config_json=?,enabled=1,review_required=0 WHERE id=?',
                    (json.dumps(self.cfg), self.key))
        self.store = MemoryStore()
        self.client = GitHub('123', 'unused', '456')
        self.client.app_jwt = Mock(return_value='synthetic-fixture')
        self.opener = Mock()
        self.transport = patch('app.github.urllib.request.build_opener', return_value=self.opener)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        # The mocked opener never opens TLS. Avoid retaining a system CA store in
        # every recorded HTTPSHandler argument while running beside the live server.
        self.tls = patch('app.github.ssl.create_default_context', return_value=None)
        self.tls.start()
        self.addCleanup(self.tls.stop)

    def row(self):
        return self.db.one('SELECT * FROM repositories WHERE id=?', (self.key,))

    def cycle(self, at=1000):
        with patch('time.time', return_value=at):
            run_cycle(self.db, self.client, store_type=lambda *args: self.store)

    def success(self, request, **kwargs):
        url = request.full_url
        if url.endswith('/access_tokens'):
            status, payload = 201, {'token': 'synthetic-installation'}
        elif url.endswith('/repos/' + self.cfg['source']):
            status, payload = 200, self.source
        elif url.endswith('/repos/' + self.cfg['target']):
            status, payload = 200, self.target
        elif url.endswith('/actions/permissions'):
            status, payload = 200, {'enabled': False}
        elif '/actions/runs?' in url:
            status, payload = 200, {'total_count': 0, 'workflow_runs': []}
        else:
            raise AssertionError('Unexpected request in fixture')
        response = io.BytesIO(json.dumps(payload).encode())
        response.status, response.headers = status, {}
        return response

    def test_token_network_failure_persists_backoff_then_recovers(self):
        self.opener.open.side_effect = urllib.error.URLError('synthetic-secret-do-not-log')
        self.cycle()
        row = self.row()
        self.assertEqual((row['enabled'], row['review_required'], row['status']), (1, 0, 'retrying'))
        self.assertEqual((row['retry_count'], row['next_retry_at']), (1, 1060))
        self.opener.open.assert_called_once()  # no immediate POST replay
        self.assertIn('installation_token', row['last_error'])
        self.assertNotIn('synthetic-secret', row['last_error'])
        self.assertEqual(self.store.writes, [])
        self.db.close()
        self.cycle(1059)
        self.opener.open.assert_called_once()
        self.opener.open.side_effect = self.success
        self.cycle(1060)
        self.assertEqual((self.row()['status'], self.row()['retry_count'], self.row()['next_retry_at']), ('synced', 0, 0))
        self.assertEqual(len(self.store.writes), 1)

    def test_token_service_failures_use_the_same_bounded_queue(self):
        self.opener.open.side_effect = urllib.error.HTTPError('https://api.github.com', 503, 'synthetic-secret', {}, None)
        at = 1000
        for count, delay in enumerate((60, 300, 900, 1800, 3600), 1):
            self.cycle(at)
            self.assertEqual((self.row()['enabled'], self.row()['retry_count'], self.row()['next_retry_at']), (1, count, at + delay))
            self.assertIn('HTTP 503', self.row()['last_error'])
            at += delay
        self.cycle(at)
        self.assertEqual((self.row()['enabled'], self.row()['review_required'], self.row()['next_retry_at']), (0, 1, 0))
        self.assertIn('耗尽', self.row()['last_error'])
        self.cycle(at + 3600)
        self.assertEqual(self.opener.open.call_count, 6)
        self.assertEqual(self.store.writes, [])

    def test_other_service_statuses_are_retryable_but_auth_and_certificate_are_not(self):
        cases = [(urllib.error.HTTPError('https://api.github.com', code, 'synthetic-secret', {}, None), code in (500, 502, 504), code)
                 for code in (500, 502, 504, 401, 403, 404, 429, 302)]
        cases.append((urllib.error.URLError(ssl.SSLCertVerificationError('synthetic-secret')), False, None))
        for error, retryable, status in cases:
            with self.subTest(status=status, error=type(error).__name__):
                self.db.run("UPDATE repositories SET enabled=1,review_required=0,status='pending',retry_count=0,next_retry_at=0")
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                self.cycle()
                row = self.row()
                self.assertEqual(row['status'], 'retrying' if retryable else 'needs_attention')
                self.assertEqual(row['enabled'], int(retryable))
                self.assertIn('installation_token', row['last_error'])
                if status is not None:
                    self.assertIn('HTTP ' + str(status), row['last_error'])
                self.assertNotIn('上游仓库不可访问', row['last_error'])
                self.assertNotIn('synthetic-secret', row['last_error'])
                self.opener.open.assert_called_once()
                self.assertEqual(self.store.writes, [])

    def test_pause_during_token_backoff_never_reenables(self):
        self.opener.open.side_effect = urllib.error.URLError('temporary')
        self.cycle()
        self.db.run("UPDATE repositories SET enabled=0,status='paused',next_retry_at=0,retry_count=0,control_epoch=control_epoch+1")
        self.cycle(5000)
        self.opener.open.assert_called_once()
        self.assertEqual((self.row()['enabled'], self.row()['status']), (0, 'paused'))

    def test_token_failure_during_result_check_never_replays_push(self):
        real_push = self.store.push
        def response_lost(updates, expected):
            real_push(updates, expected)
            raise PushNetworkError('network')
        self.store.push = Mock(side_effect=response_lost)
        self.opener.open.side_effect = self.success
        self.cycle()
        self.assertEqual(self.row()['status'], 'verifying')
        self.client._token = None
        self.opener.open.side_effect = urllib.error.URLError('synthetic-secret')
        self.cycle(1060)
        self.assertEqual((self.row()['status'], self.row()['enabled'], self.row()['review_required']), ('verifying', 0, 1))
        self.assertIsNotNone(self.row()['push_intent_json'])
        self.assertIn('installation_token', self.row()['last_error'])
        self.assertEqual(self.row()['next_retry_at'], 1360)
        self.store.push.assert_called_once()
        self.opener.open.side_effect = self.success
        self.cycle(1360)
        self.assertEqual(self.row()['status'], 'synced')
        self.store.push.assert_called_once()

    def test_invalid_token_payload_and_signing_failure_stop_with_safe_stage(self):
        response = io.BytesIO(b'{"token":"synthetic secret"}')
        response.status, response.headers = 201, {}
        self.opener.open.return_value = response
        self.cycle()
        self.assertEqual(self.row()['status'], 'needs_attention')
        self.assertIn('installation_token', self.row()['last_error'])
        self.assertNotIn('synthetic secret', self.row()['last_error'])
        self.db.run('UPDATE repositories SET enabled=1,review_required=0')
        self.client.app_jwt.side_effect = FileNotFoundError('synthetic-private-key-path')
        self.cycle()
        self.assertEqual(self.row()['status'], 'needs_attention')
        self.assertIn('app_signing', self.row()['last_error'])
        self.assertNotIn('synthetic-private-key-path', self.row()['last_error'])


if __name__ == '__main__':
    unittest.main()
