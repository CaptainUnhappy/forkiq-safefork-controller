"""Build reproducible deployment archives from a committed Git snapshot."""
import argparse
import gzip
import hashlib
import io
import json
import re
import stat
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_PATHS = (
    "app", "deploy", "tests", "requirements.txt", "requirements.lock", "LICENSE",
    "README.md", "SECURITY.md", "PRODUCT.md", "DESIGN.md", "Dockerfile", "compose.yaml",
    "compose.private.example.yaml", ".dockerignore",
)


def build_release(version, output, ref="HEAD", repo=ROOT):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?", version):
        raise ValueError("Use a version such as v1.0.0 or v1.1.0-rc.1")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])

    commit = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    epoch = int(git("show", "-s", "--format=%ct", commit))
    snapshot = git("archive", "--format=tar", commit)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(snapshot), mode="r:") as archive:
        for member in archive:
            if member.name.split("/", 1)[0] not in RELEASE_PATHS:
                continue
            if member.isdir():
                continue
            path = Path(member.name)
            if (any(p in ("__pycache__", "state", "secrets", ".venv") for p in path.parts)
                    or path.suffix.lower() in (".pem", ".key", ".db", ".sqlite", ".sqlite3", ".bundle", ".pyc")
                    or path.name.startswith(".env") or path.name.endswith(("-wal", "-shm"))):
                raise ValueError(f"Runtime/secret file is tracked in release scope: {member.name}")
            if not member.isfile():
                raise ValueError(f"Unsupported release entry: {member.name}")
            mode = 0o755 if member.mode & 0o111 else 0o644
            files[member.name] = (archive.extractfile(member).read(), mode)
    for required in ("app/main.py", "app/safefork.py", "app/gitstore.py", "app/push_guard.py",
                     "requirements.lock", "Dockerfile", "compose.yaml", "LICENSE"):
        if required not in files:
            raise ValueError(f"Missing release file: {required}")
    manifest = {
        "version": version,
        "commit": commit,
        "committed_at": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "files": {name: hashlib.sha256(data).hexdigest()
                  for name, (data, _) in sorted(files.items())},
    }
    files["release.json"] = ((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o644)

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    prefix = f"forkiq-{version}"
    tar_path, zip_path = output / f"{prefix}.tar.gz", output / f"{prefix}.zip"
    # Fixed timestamps and sorted entries make identical commits byte-reproducible.
    with tar_path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, (data, mode) in sorted(files.items()):
                entry = tarfile.TarInfo(f"{prefix}/{name}")
                entry.size, entry.mode, entry.mtime = len(data), mode, epoch
                archive.addfile(entry, io.BytesIO(data))
    stamp = datetime.fromtimestamp(max(315532800, epoch), timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, (data, mode) in sorted(files.items()):
            entry = zipfile.ZipInfo(f"{prefix}/{name}", date_time=stamp)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                                 for path in (tar_path, zip_path)), encoding="ascii")
    return tar_path, zip_path, checksums


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Version tag, for example v1.0.0")
    parser.add_argument("--ref", default="HEAD", help="Git commit or tag to package (default: HEAD)")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    for path in build_release(args.version, args.output, args.ref):
        print(path)


if __name__ == "__main__":
    main()
