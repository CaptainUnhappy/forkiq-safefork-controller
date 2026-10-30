"""Fail on common credential formats/runtime files; print paths, never values."""
import argparse
import json
import re
import subprocess

PATTERN = r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}"
# Upstream's historical test is not a key: the body is literally 'xx'.
# Only this exact invalid fixture is exempt, never a path or whole test file.
INVALID_TEST_PEM = "-----BEGIN " + r"RSA PRIVATE KEY-----\nxx\n-----END RSA PRIVATE KEY-----\n"


def audit(ref):
    commits = subprocess.check_output(["git", "rev-list", ref], text=True).splitlines()
    matches, invalid_fixtures = [], 0
    for commit in commits:
        result = subprocess.run(["git", "grep", "-I", "-l", "-E", "-e", PATTERN, commit, "--", "."],
                                text=True, encoding="utf-8", capture_output=True)
        if result.returncode not in (0, 1):
            raise RuntimeError("History scan failed; no release authorization")
        for entry in result.stdout.splitlines():
            content = subprocess.check_output(["git", "show", entry]).decode("utf-8", errors="strict")
            if entry.endswith(":tests/test_web.py") and INVALID_TEST_PEM in content:
                content = content.replace(INVALID_TEST_PEM, "INVALID_TEST_FIXTURE")
                invalid_fixtures += 1
            if re.search(PATTERN, content):
                matches.append(entry)
    paths = subprocess.check_output(["git", "log", "--format=", "--name-only", ref], text=True).splitlines()
    runtime = sorted({p for p in paths if re.search(r"(?:^|/)(?:\.env(?:\..*)?|app\.pem)$|\.(?:pem|key|db|sqlite3?|bundle)(?:-wal|-shm)?$", p)})
    print(json.dumps({"commits_scanned": len(commits), "credential_pattern_paths": matches,
                      "runtime_paths": runtime, "invalid_test_fixtures_ignored": invalid_fixtures}, ensure_ascii=True))
    return not (matches or runtime)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    raise SystemExit(0 if audit(parser.parse_args().ref) else 1)
