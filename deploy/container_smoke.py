"""Credential-free cold-start, read-only filesystem and volume restart checks."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def wait_healthy(origin):
    for _ in range(60):
        try:
            with urllib.request.urlopen(origin + "/healthz", timeout=3) as response:
                if response.status == 200 and json.load(response)["status"] == "ok":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Container worker did not become healthy")


def check(image, memory_mib=128, report=None):
    nonce = uuid.uuid4().hex
    name, volume = "safefork-test-" + nonce, "safefork-state-" + nonce
    container = None
    docker("volume", "create", "--label", "safefork-test=" + nonce, volume)
    try:
        container = docker("run", "-d", "--name", name, "--label", "safefork-test=" + nonce,
                           "--read-only", "--tmpfs", "/tmp:size=64m,mode=1777", "--cap-drop=ALL",
                           "--security-opt=no-new-privileges:true", f"--memory={memory_mib}m", f"--memory-swap={memory_mib}m",
                           "--cpus=1", "--pids-limit=64",
                           "-p", "127.0.0.1::8792", "-v", volume + ":/var/lib/forkiq",
                           "--mount", f"type=bind,source={Path(__file__).resolve().parents[1] / 'tests'},target=/opt/controller-tests,readonly",
                           "-e", "FORKIQ_PUBLIC_URL=https://fixture.example.invalid/SafeFork", image)
        address = docker("port", container, "8792/tcp").splitlines()[0]
        origin = "http://" + address
        wait_healthy(origin)
        assert docker("exec", container, "id", "-u") == "10001"
        info = json.loads(docker("inspect", container))[0]
        assert info["HostConfig"]["ReadonlyRootfs"] is True
        assert info["HostConfig"]["Memory"] == memory_mib * 1024 ** 2
        cookies = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        def request(path, data=None):
            encoded = urllib.parse.urlencode(data).encode() if data is not None else None
            with client.open(origin + "/SafeFork" + path, data=encoded, timeout=5) as response:
                return response.geturl(), response.read().decode()
        _, page = request("/setup")
        assert "设置管理员密码" in page
        fixture_password = "synthetic-test-password-ONLY"
        request("/setup", {"password": fixture_password, "password2": fixture_password})
        _, page = request("/login", {"password": fixture_password})
        assert "概览" in page
        assert 'data-refresh-url="/SafeFork/partials/overview"' in page
        for asset in ("app.css", "app.js", "icons.svg", "favicon.svg"):
            request("/static/" + asset)
        _, page = request("/partials/repos")
        assert "尚未登记仓库" in page
        _, page = request("/create")
        assert "连接 GitHub 创建授权" in page
        _, page = request("/oauth")
        assert "https://fixture.example.invalid/SafeFork/oauth/callback" in page
        print("PASS: subpath cold start, login, static assets and status polling", flush=True)
        # Run actual Git fetch/archive/push tests alongside the live web worker.
        docker("exec", container, "mkdir", "-p", "/var/lib/forkiq/smoke-tmp")
        docker("exec", "-e", "TMPDIR=/var/lib/forkiq/smoke-tmp", "-e", "PYTHONPATH=/opt/forkiq:/opt/controller-tests",
               container, "python", "-m", "unittest", "discover", "-s", "/opt/controller-tests", "-p", "test_safefork.py")
        docker("exec", "-e", "TMPDIR=/var/lib/forkiq/smoke-tmp", "-e", "PYTHONPATH=/opt/forkiq:/opt/controller-tests",
               container, "python", "-m", "unittest", "discover", "-s", "/opt/controller-tests", "-p", "test_creation.py")
        for suite in ("test_engine.py", "test_network.py"):
            docker("exec", "-e", "TMPDIR=/var/lib/forkiq/smoke-tmp", "-e", "PYTHONPATH=/opt/forkiq:/opt/controller-tests",
                   container, "python", "-m", "unittest", "discover", "-s", "/opt/controller-tests", "-p", suite)
        peak = int(docker("exec", container, "python", "-c",
                          "from pathlib import Path; p=Path('/sys/fs/cgroup/memory.peak'); "
                          "p=p if p.exists() else Path('/sys/fs/cgroup/memory/memory.max_usage_in_bytes'); print(p.read_text())"))
        assert peak <= memory_mib * 1024 ** 2
        print(f"PASS: real Git and persistent archive tests; cgroup peak {peak} bytes under {memory_mib} MiB", flush=True)
        # Restart with the same mounted volume and verify the setup and session survive.
        docker("restart", container)
        # An automatically allocated host port may change across restarts.
        restarted_address = docker("port", container, "8792/tcp").splitlines()[0]
        print(f"Test port before/after restart: {address} -> {restarted_address}", flush=True)
        origin = "http://" + restarted_address
        wait_healthy(origin)
        final_url, _ = request("/setup")
        assert not final_url.endswith("/setup")
        _, page = request("/repos")
        assert "登记已有 SafeFork" in page
        settings = docker("exec", container, "python", "-c",
                          "import sqlite3; c=sqlite3.connect('/var/lib/forkiq/forkiq.db'); "
                          "print(c.execute('SELECT count(*) FROM repositories').fetchone()[0]); "
                          "print(c.execute(\"SELECT count(*) FROM settings WHERE key='github_installation_id'\").fetchone()[0])")
        assert settings.splitlines() == ["0", "0"]
        state = json.loads(docker("inspect", "--format", "{{json .State}}", container))
        assert state["OOMKilled"] is False
        if report:
            Path(report).parent.mkdir(parents=True, exist_ok=True)
            Path(report).write_text(json.dumps({"image": image, "memory_limit_mib": memory_mib,
                                               "peak_bytes": peak, "git_tests": "passed", "restart": "passed",
                                               "production_credentials": False}, indent=2) + "\n", encoding="utf-8")
        print(f"PASS: non-root cold start, live worker, read-only root, {memory_mib} MiB limit, volume restart, no GitHub credentials/repos")
    except BaseException:
        if container:
            # This uniquely created container has only synthetic credentials.
            subprocess.run(["docker", "logs", "--tail", "100", container], check=False)
            subprocess.run(["docker", "inspect", "--format", "{{json .State}}", container], check=False)
        raise
    finally:
        if container:
            docker("rm", "-f", container)
        # The exact uniquely created test volume is the only volume ever removed.
        labels = json.loads(docker("volume", "inspect", volume))[0].get("Labels", {})
        if labels.get("safefork-test") == nonce:
            docker("volume", "rm", volume)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="forkiq-safefork-controller:local")
    parser.add_argument("--memory-mib", type=int, choices=range(96, 1025), default=128)
    parser.add_argument("--report")
    args = parser.parse_args()
    check(args.image, args.memory_mib, args.report)
