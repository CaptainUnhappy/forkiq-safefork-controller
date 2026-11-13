"""No network: mirror isolation and read-only recovery boundaries."""
import os
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
from app.github import GitHub
from app.gitstore import GitStore, SyncBlocked, RemoteReadUnavailable, MirrorUnavailable
from test_safefork import A, B, MAIN


class NetworkTests(unittest.TestCase):
    def test_mirror_cannot_receive_tokens_or_run_writes_or_private_reads(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Mock(return_value="secret-source"), Mock(return_value="secret-target")
            store = GitStore(root, "public/source", "owner/target", source, target)
            store.path = Path(root)
            with self.assertRaises(SyncBlocked):store.run("fetch", side="source", mirror=True)
            store.public_source = True
            with self.assertRaises(SyncBlocked):store.run("push", side="source", mirror=True)
            with self.assertRaises(SyncBlocked):store.run("fetch", side="target", mirror=True)
            with patch.dict(os.environ, {"GIT_CONFIG_COUNT":"1", "GIT_CONFIG_VALUE_0":"secret-env", "HTTPS_PROXY":"http://user:secret@proxy"}), \
                 patch("app.gitstore.subprocess.Popen", return_value=Mock(wait=Mock(return_value=0))) as process:
                store.run("fetch", "https://gh-proxy.org/https://github.com/public/source.git", side="source", mirror=True)
            env = process.call_args.kwargs["env"]
            self.assertFalse(any("secret" in v for v in env.values()))
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertIn("http.followRedirects=false", process.call_args.args[0])
            self.assertIn("http.proxy=", process.call_args.args[0])
            source.assert_not_called();target.assert_not_called()

    def test_mirror_snapshot_is_not_authoritative_and_failure_falls_back(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, FORKIQ_PUBLIC_GIT_MIRROR="https://gh-proxy.org"):
            for scenario in ("equal", "stale", "network", "rejected"):
                store = GitStore(root, "public/source", "owner/target")
                store.path = Path(root);store.public_source = True
                store.check_space = Mock();store.run = Mock(return_value=(0, ""))
                store.fetched_refs = Mock(side_effect=lambda side: {MAIN: A if scenario == "stale" and side == "mirror" else B})
                store.read_remote = Mock()
                if scenario == "network":
                    store.read_remote.side_effect = [RemoteReadUnavailable("unavailable"), (0, "")]
                if scenario == "rejected":
                    store.read_remote.side_effect = [MirrorUnavailable("rejected"), (0, "")]
                store.fetch("source", {MAIN: B})
                self.assertEqual(store.read_remote.call_count, 1 if scenario == "equal" else 2)
                if scenario != "equal":
                    last = store.read_remote.call_args
                    self.assertIn("https://github.com/public/source.git", last.args)
                    self.assertNotIn("mirror", last.kwargs)
                    self.assertFalse(any(c.args[:1] == ("update-ref",) and "update refs/source/" in c.kwargs.get("input_text", "") for c in store.run.call_args_list))

    def test_mirror_http_rejection_falls_back_but_local_resource_errors_stop(self):
        with tempfile.TemporaryDirectory() as root:
            store = GitStore(root, "public/source", "owner/target");store.path = Path(root);store.public_source = True
            for stderr, kind in (("The requested URL returned error: 403", MirrorUnavailable),
                                 ("Repository not found", MirrorUnavailable), ("SSL certificate problem", MirrorUnavailable),
                                 ("No space left on device", SyncBlocked), ("cannot allocate memory", SyncBlocked)):
                def process(*args, **kwargs):
                    kwargs["stderr"].write(stderr.encode())
                    return Mock(wait=Mock(return_value=128))
                with patch("app.gitstore.subprocess.Popen", side_effect=process), self.assertRaises(kind) as caught:
                    store.run("fetch", "https://gh-proxy.org/https://github.com/public/source.git", side="source", mirror=True)
                if kind is SyncBlocked:self.assertNotIsInstance(caught.exception, (MirrorUnavailable, RemoteReadUnavailable))

    def test_private_and_target_fetch_ignore_mirror_and_refs_always_official(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, FORKIQ_PUBLIC_GIT_MIRROR="https://gh-proxy.org"):
            store = GitStore(root, "private/source", "owner/target")
            store.path = Path(root);store.check_space = Mock()
            store.run = Mock(return_value=(0, ""));store.fetched_refs = Mock(return_value={MAIN:B})
            store.read_remote = Mock(return_value=(0, B + "\t" + MAIN + "\n"))
            store.fetch("source", {MAIN:B})
            store.public_source = True
            store.fetch("target", {MAIN:B})
            store.refs("source")
            for call in store.read_remote.call_args_list:
                self.assertNotIn("mirror", call.kwargs)
                self.assertFalse(any("gh-proxy" in str(arg) for arg in call.args))

    def test_api_get_network_exhaustion_typed_but_post_and_certificate_not_retryable(self):
        for method, reason, expected, calls in (
                ("GET", TimeoutError(), RemoteReadUnavailable, 4),
                ("POST", TimeoutError(), RuntimeError, 1),
                ("GET", ssl.SSLCertVerificationError(), RuntimeError, 1)):
            opener = Mock();opener.open.side_effect = urllib.error.URLError(reason)
            with patch("app.github.urllib.request.build_opener", return_value=opener), patch("app.github.time.sleep"), self.assertRaises(expected) as caught:
                GitHub(token="fixture")._request(method, "https://api.github.com/test", {})
            self.assertEqual(opener.open.call_count, calls)
            if calls == 1:self.assertNotIsInstance(caught.exception, RemoteReadUnavailable)


if __name__ == "__main__":
    unittest.main()
