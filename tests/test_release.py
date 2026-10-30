"""Exercise the published archives against an isolated committed repository."""
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from deploy.package import build_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.sources = {
            "app/main.py": b"# committed application\n",
            "app/static/app.css": b"body { margin: 0; }\n",
            "app/safefork.py": b"# safe policy\n",
            "app/gitstore.py": b"# safe transport\n",
            "app/push_guard.py": b"# trusted pre-push guard\n",
            "deploy/package.py": b"# package builder\n",
            "Dockerfile": b"FROM fixture\n",
            "compose.yaml": b"services: {}\n",
            "requirements.lock": b"fastapi==0.0.0\n",
            "requirements.txt": b"fastapi\n",
            "LICENSE": b"MIT\n",
            "README.md": b"# ForkIQ\n",
            ".github/workflows/release.yml": b"# build metadata, not runtime\n",
        }
        for name, data in self.sources.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git("add", ".")
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-10-28T00:00:00+00:00",
                   GIT_COMMITTER_DATE="2026-10-28T00:00:00+00:00")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], env=env, check=True)
        self.commit = self.git("rev-parse", "HEAD").decode().strip()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args])

    def build(self, version="v1.0.0", directory="dist", ref="HEAD"):
        return build_release(version, self.root / directory, ref=ref, repo=self.repo)

    def test_archives_match_committed_content_and_manifest(self):
        tgz, zipped, checksums = self.build()
        with tarfile.open(tgz) as archive:
            tar_files = {entry.name: archive.extractfile(entry).read() for entry in archive}
        with zipfile.ZipFile(zipped) as archive:
            zip_files = {name: archive.read(name) for name in archive.namelist()}
        self.assertEqual(tar_files, zip_files)
        expected = {"forkiq-v1.0.0/" + name: data for name, data in self.sources.items()
                    if not name.startswith(".github/")}
        self.assertEqual(set(tar_files), {*expected, "forkiq-v1.0.0/release.json"})
        for name, data in expected.items():
            self.assertEqual(tar_files[name], data)
        release = json.loads(tar_files["forkiq-v1.0.0/release.json"])
        self.assertEqual(release["version"], "v1.0.0")
        self.assertEqual(release["commit"], self.commit)
        self.assertEqual(release["files"], {name.removeprefix("forkiq-v1.0.0/"): hashlib.sha256(data).hexdigest()
                                          for name, data in expected.items()})
        self.assertEqual(checksums.read_text(), "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in (tgz, zipped)))

    def test_untracked_runtime_data_and_dirty_edits_are_not_packaged(self):
        (self.repo / "app/main.py").write_bytes(b"# dirty edit must not ship\n")
        for name in ("app/app.pem", "app/runtime.db", "app/__pycache__/main.pyc",
                     "state/forkiq.db", ".env", ".venv/bin/python"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"private runtime fixture")
        _, zipped, _ = self.build()
        with zipfile.ZipFile(zipped) as archive:
            self.assertEqual(archive.read("forkiq-v1.0.0/app/main.py"), self.sources["app/main.py"])
            self.assertFalse(any("private runtime fixture".encode() in archive.read(name)
                                 for name in archive.namelist()))

    def test_archives_are_reproducible_across_output_directories(self):
        first = self.build(directory="first")
        second = self.build(directory="second")
        for a, b in zip(first, second):
            self.assertEqual(a.read_bytes(), b.read_bytes(), a.suffix)

    def test_tracked_secret_file_fails_closed(self):
        (self.repo / "app/app.pem").write_bytes(b"fake-secret-fixture")
        self.git("add", "app/app.pem")
        self.git("commit", "-qm", "bad fixture")
        with self.assertRaisesRegex(ValueError, "Runtime/secret"):
            self.build()

    def test_requested_tag_packages_the_tagged_commit(self):
        self.git("tag", "v1.0.0")
        (self.repo / "app/main.py").write_bytes(b"# later commit\n")
        self.git("add", "app/main.py")
        self.git("commit", "-qm", "later")
        _, zipped, _ = self.build(ref="v1.0.0")
        with zipfile.ZipFile(zipped) as archive:
            self.assertEqual(archive.read("forkiq-v1.0.0/app/main.py"), self.sources["app/main.py"])
            self.assertEqual(json.loads(archive.read("forkiq-v1.0.0/release.json"))["commit"], self.commit)

    def test_invalid_versions_fail_before_creating_files(self):
        for version in ("1.0.0", "v1", "../../escape", "v1.0.0/../escape", "v1.0.0\n"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.build(version)
        self.assertFalse((self.root / "dist").exists())

    def test_prerelease_versions_are_supported(self):
        tgz, zipped, _ = self.build("v1.1.0-rc.1")
        self.assertEqual(tgz.name, "forkiq-v1.1.0-rc.1.tar.gz")
        with zipfile.ZipFile(zipped) as archive:
            self.assertEqual(json.loads(archive.read("forkiq-v1.1.0-rc.1/release.json"))["version"], "v1.1.0-rc.1")

    def test_missing_required_source_fails_before_creating_archives(self):
        self.git("rm", "LICENSE")
        self.git("commit", "-qm", "incomplete fixture")
        with self.assertRaisesRegex(ValueError, "Missing release file: LICENSE"):
            self.build()
        self.assertFalse((self.root / "dist").exists())


if __name__ == "__main__":
    unittest.main()
