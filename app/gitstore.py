"""Isolated full-history Git transport; never checks out managed code."""
import base64
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class SyncBlocked(RuntimeError):
    """Only credential-free, actionable messages cross this interface."""


class TransientGitRead(SyncBlocked):
    """A retryable remote read into scratch storage, never a persistent write."""


def transient_read_error(stderr):
    value = stderr.lower()
    permanent = ("certificate", "authentication", "permission denied", "not found", "access denied",
                 "no space left", "disk quota", "read-only file system", "cannot allocate memory",
                 "out of memory", "resource temporarily unavailable", "cannot create thread",
                 "too many open files", "died of signal", "not a git repository", "bad config",
                 "unknown option", "dubious ownership", "transport 'https' not allowed",
                 "non-fast-forward", "would clobber existing tag")
    if any(marker in value for marker in permanent):
        return False
    if re.search(r"(?:returned error:|response(?: code)?[: ]+|http/[12](?:\.\d+)? )\s*(401|403|404|410|429)\b", value):
        return False
    # Unknown read failures may also be transient. Bounded retries still require
    # a complete valid snapshot and all identity/history checks before any write.
    return True


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

    def run(self, *args, side=None, allowed=(0,), hooks=None, local=False):
        if local and side is not None:
            raise SyncBlocked("本地保存不能附带远端凭据。")
        settings = ("credential.helper=", "core.hooksPath=" + str(hooks or self.path / "no-hooks"),
                    "init.templateDir=", "protocol.allow=never", "protocol.https.allow=always",
                    "http.followRedirects=false", "gc.auto=0", "maintenance.auto=false",
                    "fetch.fsckObjects=true", "transfer.fsckObjects=true", "pack.threads=1",
                    "pack.windowMemory=16m", "pack.deltaCacheSize=8m", "pack.window=5",
                    "core.fsync=committed", "core.deltaBaseCacheLimit=8m")
        command = ["git"]
        for setting in settings:
            command += ["-c", setting]
        env = self.env(side)
        if local:
            # Only controller-owned archive/temporary paths use the file transport.
            env["GIT_ALLOW_PROTOCOL"] = "file"
            command += ["-c", "protocol.file.allow=always"]
        command += list(args)
        operation = args[1] if args[0].startswith("--git-dir=") else args[0]
        # Output never includes stderr, command arguments or credentials in errors.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(command, cwd=self.path, env=env,
                                    stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=errors, start_new_session=True)
            try:
                # Initial full-history downloads may be much larger than later
                # incremental checks. Never extend or retry a remote write.
                timeout = 60 if operation == "ls-remote" else (1800 if args[0] == "fetch" and side else 600)
                code = proc.wait(timeout=timeout)
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
                if operation == "ls-remote" and side:
                    raise TransientGitRead("Git 只读引用检查超时，未重试任何推送。") from None
                message = ("Git 只读拉取超时，未推送；请检查网络与仓库容量后重新预览。"
                           if args[0] == "fetch" and side else
                           "Git 操作超时；若已推送，须核对远端结果后再恢复。")
                raise SyncBlocked(message) from None
            if code not in allowed:
                errors.seek(0)
                if code > 0 and operation in ("ls-remote", "fetch") and side and not local and transient_read_error(errors.read(8192).decode("utf-8", errors="replace")):
                    raise TransientGitRead("Git 远端读取未完成，正在有限重试。")
                stage = {"source": "来源", "target": "目标"}.get(side, "本地")
                raise SyncBlocked("Git 操作失败（" + stage + " " + operation + "，退出码 " + str(code)
                                  + "），请检查权限、引用与磁盘；不会强推。")
            output.seek(0)
            data = output.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise SyncBlocked("Git 返回数据超出安全上限，停止处理不完整结果。")
            return code, data.decode("utf-8", errors="strict")

    def read_remote(self, *args, side):
        if args[0] not in ("ls-remote", "fetch"):
            raise SyncBlocked("只读重试不能用于持久保存或推送。")
        for attempt in range(3):
            try:
                return self.run(*args, side=side)
            except TransientGitRead:
                if attempt == 2:
                    raise SyncBlocked("Git 远端读取（" + args[0] + "）在 3 次尝试后仍不可用，已停写；请检查网络后重新预览。") from None
                time.sleep(2 ** attempt)

    def refs(self, side):
        _, text = self.read_remote("ls-remote", "--refs", self.url(side),
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
        self.read_remote("fetch", "--no-tags", "--no-recurse-submodules", self.url(side),
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

    @staticmethod
    def _linked(path):
        try:
            return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        except FileNotFoundError:
            return False

    def archive_path(self, config):
        target_id = config.get("target_id")
        if type(target_id) is not int or target_id <= 0:
            raise SyncBlocked("持久仓库缺少有效目标 ID。")
        root = self.root.absolute()
        if any(self._linked(item) for item in (root, *root.parents)):
            raise SyncBlocked("持久状态目录不能经过符号链接。")
        parent = root / "repositories"
        path = parent / f"{target_id}.git"
        for item in (root, parent, path):
            if self._linked(item) or (item.exists() and not item.is_dir()):
                raise SyncBlocked("持久仓库路径不是受控目录，已停写。")
        return path

    @staticmethod
    def archive_binding(config):
        return {key: config[key] for key in ("source", "source_id", "target", "target_id", "mode")}

    def archive_refs(self, config):
        path = self.archive_path(config)
        if not path.exists():
            return {}
        binding = path / "safefork-identity.json"
        for item in (binding, path / "config", path / "HEAD", path / "objects", path / "refs"):
            if self._linked(item):
                raise SyncBlocked("持久仓库包含链接路径，已停写。")
        try:
            saved = json.loads(binding.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SyncBlocked("持久仓库缺少有效身份记录，不能接管已有目录。") from None
        if saved != self.archive_binding(config):
            raise SyncBlocked("持久仓库身份与登记不一致，不能复用或覆盖。")
        if (path / "shallow").exists() or (path / "objects/info/alternates").exists():
            raise SyncBlocked("持久仓库不是独立完整历史，已停写。")
        prefix = "--git-dir=" + str(path)
        if self.run(prefix, "rev-parse", "--is-bare-repository", local=True)[1].strip() != "true":
            raise SyncBlocked("持久保存目录不是裸 Git 仓库。")
        self.run(prefix, "fsck", "--connectivity-only", "--no-dangling", local=True)
        _, text = self.run(prefix, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/", "refs/tags/", local=True)
        from .safefork import valid_ref, valid_sha
        refs = {}
        for line in text.splitlines():
            sha, ref = line.split(" ", 1)
            if not valid_sha(sha) or not valid_ref(ref) or ref in refs:
                raise SyncBlocked("持久仓库引用不完整或格式错误。")
            refs[ref] = sha
        return refs

    def seed_archive(self, config):
        """Reuse verified immutable objects without changing the saved archive."""
        if not self.archive_refs(config):
            return
        seed = self.path / "seeded.git"
        self.run("clone", "--local", "--bare", "--no-hardlinks",
                 str(self.archive_path(config)), str(seed), local=True)
        if (seed / "objects/info/alternates").exists() or (seed / "shallow").exists():
            raise SyncBlocked("临时仓库必须具有独立完整历史。")
        self.path = seed

    def archive_plan(self, config, expected):
        """Read-only archive validation; objects are fetched only into the scratch repo."""
        path = self.archive_path(config)
        old = self.archive_refs(config)
        if old:
            self.run("fetch", "--no-tags", "--no-recurse-submodules", path.as_uri(),
                     "refs/heads/*:refs/archive/heads/*", "refs/tags/*:refs/archive/tags/*", local=True)
        for ref, sha in expected.items():
            previous = old.get(ref)
            if previous and previous != sha:
                if ref.startswith("refs/tags/") or not self.is_ancestor(previous, sha):
                    raise SyncBlocked("服务器已有历史不能后退或移动标签：" + ref)
        names = set(old) | set(expected)
        for name in names:
            parts = name.split("/")
            if any("/".join(parts[:i]) in names for i in range(2, len(parts))):
                raise SyncBlocked("持久仓库与拟保存引用存在名称冲突。")
        if self.archive_refs(config) != old:
            raise SyncBlocked("持久仓库在检查期间变化，已停写。")
        return {"path": f"repositories/{config['target_id']}.git", "before": old,
                "local_only": sorted(set(old) - set(expected)), "saved": False}

    def preserve(self, config, expected, archive):
        """Save the complete planned target with ordinary atomic, non-force fetch."""
        self.check_space()
        path = self.archive_path(config)
        if self.archive_refs(config) != archive["before"]:
            raise SyncBlocked("持久仓库在写入前变化，未推送 GitHub。")
        if not path.exists():
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.mkdir(mode=0o700)
            with (path / "safefork-identity.json").open("x", encoding="utf-8") as handle:
                json.dump(self.archive_binding(config), handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            self.run("init", "--bare", str(path), local=True)
            primary = config.get("branch_map", {}).get(config["primary"], config["primary"])
            self.run("--git-dir=" + str(path), "symbolic-ref", "HEAD", "refs/heads/" + primary, local=True)
        changes = {ref: sha for ref, sha in expected.items() if archive["before"].get(ref) != sha}
        if changes:
            self.run("--git-dir=" + str(path), "fetch", "--atomic", "--no-tags", "--no-recurse-submodules",
                     self.path.as_uri(), *[sha + ":" + ref for ref, sha in sorted(changes.items())], local=True)
        wanted = {**archive["before"], **expected}
        if self.archive_refs(config) != wanted:
            raise SyncBlocked("服务器持久保存未完全确认，未推送 GitHub。")
        self.check_space()
        return {**archive, "saved": True, "ref_count": len(wanted)}

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
