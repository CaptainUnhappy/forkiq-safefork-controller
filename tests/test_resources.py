import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from app.gitstore import GitStore, SyncBlocked, TransientGitRead, process_capacity


class ResourceTests(unittest.TestCase):
    def test_cgroup_v1_v2_unlimited_and_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertIsNone(process_capacity(root))
            for location in (root, root/'pids'):
                location.mkdir(exist_ok=True)
                (location/'pids.current').write_text('63\n')
                (location/'pids.max').write_text('64\n')
                self.assertEqual(process_capacity(root),dict(current=63,limit=64,available=1))
                (location/'pids.max').write_text('max\n')
                self.assertIsNone(process_capacity(root))
                (location/'pids.current').unlink();(location/'pids.max').unlink()

    def test_present_but_malformed_or_incomplete_budget_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'pids.max').write_text('64')
            with self.assertRaises(SyncBlocked):process_capacity(root)
            (root/'pids.current').write_text('not-an-integer')
            with self.assertRaises(SyncBlocked):process_capacity(root)
            (root/'pids.current').write_text('1');(root/'pids.max').write_text('0')
            with self.assertRaises(SyncBlocked):process_capacity(root)

    def test_exhausted_pid_budget_prevents_new_git_process(self):
        store=GitStore('unused','a/b','c/d');store.path=Path('unused')
        with patch('app.gitstore.process_capacity',return_value=dict(current=60,limit=64,available=4)), \
             patch('app.gitstore.subprocess.Popen') as process, self.assertRaisesRegex(SyncBlocked,'运行资源不足'):
            store.run('fetch',side='source')
        process.assert_not_called()

    def test_spawn_and_git_child_resource_errors_are_safe_and_not_retryable(self):
        with tempfile.TemporaryDirectory() as root:
            store=GitStore(root,'a/b','c/d');store.path=Path(root)
            with patch('app.gitstore.subprocess.Popen',side_effect=OSError(errno.EAGAIN,'fixture-secret')),self.assertRaisesRegex(SyncBlocked,'运行资源不足') as caught:
                store.run('fetch',side='source')
            self.assertNotIn('fixture-secret',str(caught.exception))
            def process(*args,**kwargs):
                kwargs['stderr'].write(b'cannot fork() for commit-graph: Resource temporarily unavailable fixture-secret')
                return Mock(wait=Mock(return_value=48))
            with patch('app.gitstore.subprocess.Popen',side_effect=process),self.assertRaisesRegex(SyncBlocked,'运行资源不足') as caught:
                store.run('fsck','--connectivity-only')
            self.assertNotIsInstance(caught.exception,TransientGitRead)
            self.assertNotIn('fixture-secret',str(caught.exception))


if __name__=='__main__':unittest.main()
