"""Isolated full-history Git transport; never checks out managed code."""
import base64
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


class SyncBlocked(RuntimeError):
    """Only credential-free, actionable messages cross this interface."""


@contextmanager
def process_lock(path):
    """One writer per state directory, including separate application processes."""
    with open(path, "a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SyncBlocked("另一个任务正在检查或同步，请稍后重试。") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class GitStore:
    def __init__(self, root, source, target, source_token=lambda: "", target_token=lambda: ""):
        self.root = Path(root)
        self.source, self.target = source, target
        self.tokens = {"source": source_token, "target": target_token}
        self.temporary = None

    def __enter__(self):
        self.check_space()
        self.temporary = tempfile.TemporaryDirectory(prefix="sync-", dir=self.root)
        self.path = Path(self.temporary.name)
        self.path.chmod(0o700)
        try:
            self.run("init", "--bare")
        except BaseException:
            self.temporary.cleanup()
            raise
        return self

    def __exit__(self, *exc):
        if self.temporary:
            self.temporary.cleanup()

    def check_space(self):
        if shutil.disk_usage(self.root).free < 1024 ** 3:
            raise SyncBlocked("可用磁盘不足 1 GiB，已停止；请先调整容量。")

    def url(self, side):
        return "https://github.com/" + getattr(self, side) + ".git"

    def env(self, side=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1",
                   GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https",
                   GIT_PROTOCOL_FROM_USER="0")
        token = self.tokens[side]() if side else ""
        if token:
            if not isinstance(token, str) or len(token) > 16384 or any(c.isspace() for c in token):
                raise SyncBlocked("凭据格式无效。")
            auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0="Authorization: Basic " + auth)
        return env

    def run(self, *args, side=None, allowed=(0,), hooks=None):
        settings = ("credential.helper=", "core.hooksPath=" + str(hooks or self.path / "no-hooks"),
                    "init.templateDir=", "protocol.allow=never", "protocol.https.allow=always",
                    "http.followRedirects=false", "gc.auto=0", "maintenance.auto=false",
                    "fetch.fsckObjects=true", "transfer.fsckObjects=true", "pack.threads=1",
                    "pack.windowMemory=16m", "pack.deltaCacheSize=8m", "pack.window=5")
        command = ["git"]
        for setting in settings:
            command += ["-c", setting]
        command += list(args)
        # Output never includes stderr, command arguments or credentials in errors.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(command, cwd=self.path, env=self.env(side),
                                    stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait()
                raise SyncBlocked("Git 操作超时；若已推送，须核对远端结果后再恢复。") from None
            if code not in allowed:
                raise SyncBlocked("Git 操作失败（" + args[0] + "），请检查权限、引用与磁盘；不会强推。")
            output.seek(0)
            data = output.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise SyncBlocked("Git 返回数据超出安全上限，停止处理不完整结果。")
            return code, data.decode("utf-8", errors="strict")

    def refs(self, side):
        _, text = self.run("ls-remote", "--refs", self.url(side),
                           "refs/heads/*", "refs/tags/*", side=side)
        result = {}
        for line in text.splitlines():
            sha, ref = line.split("\t", 1)
            from .safefork import valid_ref, valid_sha
            if not valid_sha(sha) or not valid_ref(ref) or ref in result:
                raise SyncBlocked("远端引用列表无效或重复。")
            result[ref] = sha
        return result

    def fetch(self, side, snapshot):
        self.check_space()
        if not snapshot:
            return
        self.run("fetch", "--no-tags", "--no-recurse-submodules", self.url(side),
                 f"refs/heads/*:refs/{side}/heads/*", f"refs/tags/*:refs/{side}/tags/*", side=side)
        _, text = self.run("for-each-ref", "--format=%(objectname) %(refname)", f"refs/{side}/")
        actual = {}
        for line in text.splitlines():
            sha, ref = line.split(" ", 1)
            actual[ref.replace(f"refs/{side}/", "refs/", 1)] = sha
        if actual != snapshot or (self.path / "shallow").exists():
            raise SyncBlocked("拉取期间引用变化或历史不完整，未写入目标。")
        self.run("fsck", "--connectivity-only", "--no-dangling")
        self.check_space()

    def is_ancestor(self, old, new):
        code, _ = self.run("merge-base", "--is-ancestor", old, new, allowed=(0, 1))
        return code == 0

    def files(self, sha):
        _, value = self.run("ls-tree", "-r", "--name-only", "-z", sha)
        return set(value.rstrip("\0").split("\0")) if value else set()

    def push(self, updates, expected):
        """Ordinary atomic push plus a trusted hook pinning advertised old OIDs."""
        self.check_space()
        hooks = self.path / "trusted-hooks"
        hooks.mkdir()
        guard = hooks / "expected.json"
        guard.write_text(json.dumps({ref: [expected.get(ref, "0" * 40), sha]
                                    for ref, sha in updates.items()}), encoding="utf-8")
        # This is our own reviewed hook, never a hook fetched from upstream.
        script = Path(__file__).with_name("push_guard.py").resolve()
        command = " ".join(shlex.quote(str(p).replace("\\", "/"))
                           for p in (sys.executable, script, guard))
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\nexec " + command + "\n", encoding="utf-8", newline="\n")
        hook.chmod(0o700)
        self.run("push", "--atomic", "--porcelain", "--no-follow-tags", "--recurse-submodules=no",
                 "--", self.url("target"), *[sha + ":" + ref for ref, sha in sorted(updates.items())],
                 side="target", hooks=hooks)
