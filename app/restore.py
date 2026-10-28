"""Restore one verified server snapshot with an exact remote-HEAD lease."""
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from .backups import (_NAME, _SHA, _authenticated_git_env, _run_git, backup_path,
                      ensure_backup, verified_backup)
from .db import log_event, utcnow


class RestoreError(RuntimeError):
    """Only explicit, credential-free messages may be returned to the panel."""


def _pin_bundle(source, target, record):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(source, flags)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "rb") as incoming, target.open("xb") as outgoing:
        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
            raise RestoreError("备份不是普通文件，回滚已取消。")
        target.chmod(0o600)
        while chunk := incoming.read(1024 * 1024):
            outgoing.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if size != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
        raise RestoreError("备份完整性校验失败，回滚已取消。")


def rollback_backup(db, client, row, backup_id, expected_sha):
    """Pause first; restore only this repository/branch, never resume or retry."""
    repo_id = row["id"]
    live = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if live is None or live["full_name"] != row["full_name"]:
        return {"ok": False, "error": "仓库已变化或不存在，回滚已取消。", "sha": None}
    db.run("UPDATE repositories SET enabled=0,status='paused',sync_requested=0,"
           "deletion_approval=NULL,last_error=NULL,updated_at=? WHERE id=?", (utcnow(), repo_id))
    stage = "validation"
    target_sha = None
    try:
        full_name, branch = live["full_name"], live["branch"]
        if (not isinstance(full_name, str) or not _NAME.fullmatch(full_name)
                or full_name.split("/", 1)[1] in (".", "..")):
            raise RestoreError("GitHub 仓库名称无效，回滚已取消。")
        if (not isinstance(branch, str) or not branch or "\\" in branch
                or any(ord(char) < 32 or ord(char) == 127 for char in branch)):
            raise RestoreError("仓库分支名称无效，回滚已取消。")
        if row["branch"] != branch:
            raise RestoreError("仓库分支已变化，请重新选择备份并确认。")
        if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
            raise RestoreError("缺少有效的当前提交，请重新打开回滚确认页。")
        expected_sha = expected_sha.lower()
        if isinstance(backup_id, bool) or not re.fullmatch(r"[1-9][0-9]*", str(backup_id)):
            raise RestoreError("备份编号无效，回滚已取消。")
        record = db.one("SELECT * FROM repository_backups WHERE id=?", (int(backup_id),))
        if record is None or record["full_name"] != full_name:
            raise RestoreError("此备份不属于当前仓库，回滚已取消。")
        if not verified_backup(db, record):
            raise RestoreError("备份尚未就绪、已丢失或完整性校验失败，回滚已取消。")
        target_sha = record["fork_sha"]
        source = backup_path(db, record)
        ref = "refs/heads/" + branch
        with tempfile.TemporaryDirectory(prefix=".tmp-restore-", dir=source.parent) as temporary:
            work = Path(temporary)
            work.chmod(0o700)
            pinned = work / "target.bundle"
            # Saving the current HEAD may evict this older backup from retention.
            _pin_bundle(source, pinned, record)
            gitdir = work / "restore.git"
            gitdir.mkdir(mode=0o700)
            try:
                _run_git(["check-ref-format", ref], work)
            except Exception:
                raise RestoreError("仓库分支名称无效，回滚已取消。") from None
            _run_git(["init", "--bare"], gitdir)
            _run_git(["bundle", "verify", str(pinned)], gitdir)
            heads = _run_git(["bundle", "list-heads", str(pinned)], gitdir, capture=True)
            if heads != target_sha + " refs/heads/preserved":
                raise RestoreError("备份未精确保留所选提交，回滚已取消。")
            _run_git(["bundle", "unbundle", str(pinned)], gitdir)
            commit = _run_git(["rev-parse", "--verify", target_sha + "^{commit}"], gitdir, capture=True)
            if commit != target_sha:
                raise RestoreError("备份目标不是有效的提交，回滚已取消。")
            if client.get_ref(full_name, branch) != expected_sha:
                raise RestoreError("GitHub 分支已变化，回滚已取消；请重新检查并确认。")
            stage = "backup"
            current = ensure_backup(db, client, live, expected_sha)
            if (current.get("status") != "ready" or current.get("full_name") != full_name
                    or current.get("fork_sha") != expected_sha or not verified_backup(db, current)):
                raise RestoreError("回滚前无法完整备份当前提交，回滚已取消。")
            if client.get_ref(full_name, branch) != expected_sha:
                raise RestoreError("GitHub 分支在备份期间已变化，回滚已取消；请重新检查并确认。")
            env = _authenticated_git_env(client)
            stage = "push"
            _run_git(["push", "--porcelain", "--no-verify", "--no-follow-tags",
                      "--recurse-submodules=no", f"--force-with-lease={ref}:{expected_sha}",
                      "--", "https://github.com/" + full_name + ".git", target_sha + ":" + ref],
                     gitdir, env=env)
            stage = "verification"
            if client.get_ref(full_name, branch) != target_sha:
                raise RestoreError("回滚结果未确认：GitHub 当前提交与所选备份不一致；"
                                   "已保持暂停，请检查分支，不会自动重试。")
        db.run("UPDATE repositories SET enabled=0,status='paused',fork_sha=?,fork_tag=NULL,"
               "fork_commit_at=NULL,last_merged_upstream_sha=NULL,upstream_ahead=NULL,fork_ahead=NULL,"
               "deletion_review=NULL,deletion_approval=NULL,last_error=NULL,last_check_at=?,updated_at=? "
               "WHERE id=?", (target_sha, utcnow(), utcnow(), repo_id))
        log_event(db, repo_id, "check", f"{full_name} 已回滚至服务器备份提交 {target_sha}；保持暂停同步。")
        return {"ok": True, "error": "", "sha": target_sha}
    except Exception as error:
        if isinstance(error, RestoreError):
            message = str(error)
        elif stage in ("push", "verification"):
            message = ("回滚结果未确认：分支可能已变化、推送被拒或网络超时；"
                       "已保持暂停，请核对 GitHub 分支和权限，不会自动重试。")
        elif stage == "backup":
            message = "回滚前备份或连接检查失败，回滚已取消；已保持暂停。"
        else:
            message = "回滚校验失败，请检查备份、Git 安装、访问权限与存储；已保持暂停。"
        db.run("UPDATE repositories SET enabled=0,status='paused',sync_requested=0,"
               "last_error=?,updated_at=? WHERE id=?", (message, utcnow(), repo_id))
        log_event(db, repo_id, "error", f"{live['full_name']} {message}")
        return {"ok": False, "error": message, "sha": None}
