"""Credential-free cold-start, read-only filesystem and volume restart checks."""
import argparse
import http.cookiejar
import json
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


def check(image):
    nonce = uuid.uuid4().hex
    name, volume = "safefork-test-" + nonce, "safefork-state-" + nonce
    container = None
    docker("volume", "create", "--label", "safefork-test=" + nonce, volume)
    try:
        container = docker("run", "-d", "--name", name, "--label", "safefork-test=" + nonce,
                           "--read-only", "--tmpfs", "/tmp:size=64m,mode=1777", "--cap-drop=ALL",
                           "--security-opt=no-new-privileges:true", "--memory=512m", "--cpus=1", "--pids-limit=64",
                           "-p", "127.0.0.1::8792", "-v", volume + ":/var/lib/forkiq",
                           "-e", "FORKIQ_PUBLIC_URL=https://fixture.example.invalid", image)
        address = docker("port", container, "8792/tcp").splitlines()[0]
        origin = "http://" + address
        wait_healthy(origin)
        assert docker("exec", container, "id", "-u") == "10001"
        info = json.loads(docker("inspect", container))[0]
        assert info["HostConfig"]["ReadonlyRootfs"] is True
        assert info["HostConfig"]["Memory"] == 512 * 1024 ** 2
        cookies = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        def request(path, data=None):
            encoded = urllib.parse.urlencode(data).encode() if data is not None else None
            with client.open(origin + path, data=encoded, timeout=5) as response:
                return response.geturl(), response.read().decode()
        _, page = request("/setup")
        assert "设置管理员密码" in page
        fixture_password = "synthetic-test-password-ONLY"
        request("/setup", {"password": fixture_password, "password2": fixture_password})
        _, page = request("/login", {"password": fixture_password})
        assert "概览" in page
        print("PASS: cold start and login", flush=True)
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
        print("PASS: non-root cold start, live worker, read-only root, 512 MiB limit, volume restart, no GitHub credentials/repos")
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
    check(parser.parse_args().image)
