"""Version comparison labels for the repository list and detail page.

The panel's ground truth for "is my fork current" is the pair of commit SHAs
recorded for the fork and its upstream, plus the commit counts the engine
observed for them. Every label below is derived from those stored fields, so the
list can never claim a sync the engine did not confirm.

The label answers the one question the panel is asked: are the two sides the
same version, and when they are not, why. "本地领先" means the fork holds
commits upstream does not (its own Actions or local work) while every upstream
commit is already inside it, so nothing needs syncing; "落后上游" means upstream
has commits the fork lacks.
"""


def _count(repo, key):
    """Stored commit count, or None while it has never been probed."""
    try:
        value = repo[key]
    except (KeyError, IndexError):
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _field(repo, key):
    """Row value that may be absent from plain-dict rows in tests."""
    try:
        value = repo[key]
    except (KeyError, IndexError):
        return None
    return value


def version_verdict(repo):
    """Compare the recorded fork head with the upstream head.

    Returns the label shown next to the status badge plus a full-SHA tooltip, so
    the short codes in both version lines can be matched to one comparison.
    """
    fork = repo["fork_sha"] or ""
    upstream = repo["upstream_sha"] or ""
    merged = repo["last_merged_upstream_sha"] or ""
    status = repo["status"]
    upstream_ahead = _count(repo, "upstream_ahead")
    fork_ahead = _count(repo, "fork_ahead")
    if repo["upstream_gone"]:
        return {"kind": "gone", "text": "上游不可用", "sha": "",
                "title": "上游仓库已不可见；fork 与本地历史保留，未做任何改动"}
    if not fork or not upstream:
        return {"kind": "pending", "text": "待检查", "sha": "",
                "title": "尚未记录版本号，等待下一次检查"}
    if fork == upstream:
        return {"kind": "same", "text": "同一版本", "sha": upstream,
                "title": f"本地与上游指向同一提交 {upstream}，是同一个版本"}
    if status in ("diverged", "blocked"):
        reason = (_field(repo, "last_error") or "").strip()
        if reason:
            detail = f"本地与上游不是同一版本：{reason}"
        else:
            detail = "本地与上游不是同一版本：双方各有独立提交，历史已分叉"
        return {"kind": "diverged", "text": "与上游分叉", "sha": upstream,
                "title": f"{detail}（上游为 {upstream}）"}
    if upstream_ahead:
        return {"kind": "behind", "text": f"落后上游 {upstream_ahead} 个提交", "sha": upstream,
                "title": f"本地与上游不是同一版本：上游 {upstream} 有 {upstream_ahead} "
                         "个本地还没有的提交，本地需要更新"}
    if status in ("error", "needs_attention", "missing", "paused"):
        # A failed or paused check is the reason the two sides cannot be called
        # the same version yet; the stored counts may be stale, so they are not
        # quoted as the reason.
        reason = (_field(repo, "last_error") or "").strip() or "检查未完成，等待下一次检查"
        return {"kind": "unsynced", "text": "不一致", "sha": upstream,
                "title": f"本地与上游不是同一版本：{reason}（上游为 {upstream}）"}
    if fork_ahead is not None:
        if not fork_ahead:
            return {"kind": "contains", "text": "已含上游", "sha": upstream,
                    "title": f"本地已包含上游 {upstream}，且没有自有提交"}
        return {"kind": "contains", "text": f"本地领先 {fork_ahead} 个提交", "sha": upstream,
                "title": f"本地与上游不是同一版本：本地已包含上游 {upstream} 的全部提交，"
                         f"另有 {fork_ahead} 个上游没有的自有提交（fork 自身维护或本地提交）；"
                         "上游没有本地缺少的内容，无需更新"}
    if merged == upstream:
        return {"kind": "contains", "text": "已含上游", "sha": upstream,
                "title": f"本地已包含上游提交 {upstream}，另有自有提交（数量待检查），无需更新"}
    if status == "synced":
        return {"kind": "contains", "text": "已同步", "sha": upstream,
                "title": f"引擎已确认同步，上游为 {upstream}"}
    if status in ("behind", "pending"):
        return {"kind": "behind", "text": "落后上游", "sha": upstream,
                "title": f"本地落后上游提交 {upstream}"}
    return {"kind": "unsynced", "text": "不一致", "sha": upstream,
            "title": f"本地与上游不一致，上游为 {upstream}"}


def fork_tone(verdict):
    """Colour for the local SHA: neutral until the fork is known to lag."""
    return {"behind": "sha-behind", "unsynced": "sha-unsynced",
            "diverged": "sha-unsynced"}.get(verdict["kind"], "")


def short_time(value):
    """`2026-10-12T12:29:46+00:00` -> `10-12 12:29`.

    Every timestamp the panel shows is rendered through this filter, so the
    list, the detail page and the event tables read the same. The value is UTC;
    the browser re-renders each one in the viewer's own timezone.
    """
    if not value or "T" not in value:
        return value or "—"
    date, _, clock = value.partition("T")
    return f"{date[5:]} {clock[:5]}"


def annotate(rows):
    """Attach comparison fields to repository rows for template rendering."""
    annotated = []
    for row in rows:
        item = dict(row)
        verdict = version_verdict(item)
        item["verdict"] = verdict
        item["fork_tone"] = fork_tone(verdict)
        annotated.append(item)
    return annotated
