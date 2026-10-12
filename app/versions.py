"""Version comparison labels for the repository list and detail page.

The panel's ground truth for "is my fork current" is the pair of commit SHAs
recorded for the fork and its upstream. Every label below is derived from those
stored fields, so the list can never claim a sync the engine did not confirm.
"""

def version_verdict(repo):
    """Compare the recorded fork head with the upstream head.

    Returns the label shown next to the status badge plus a full-SHA tooltip, so
    the short codes in both version lines can be matched to one comparison.
    """
    fork = repo["fork_sha"] or ""
    upstream = repo["upstream_sha"] or ""
    merged = repo["last_merged_upstream_sha"] or ""
    status = repo["status"]
    if repo["upstream_gone"]:
        return {"kind": "gone", "text": "上游不可用", "sha": "",
                "title": "上游仓库已不可见；fork 与本地历史保留，未做任何改动"}
    if not fork or not upstream:
        return {"kind": "pending", "text": "待检查", "sha": "",
                "title": "尚未记录版本号，等待下一次检查"}
    if fork == upstream:
        return {"kind": "same", "text": "同一提交", "sha": upstream,
                "title": f"本地与上游指向同一提交 {upstream}"}
    if merged == upstream:
        return {"kind": "contains", "text": "已含上游", "sha": upstream,
                "title": f"本地已包含上游提交 {upstream}（fork 另有自有提交）"}
    if status == "synced":
        return {"kind": "contains", "text": "已同步", "sha": upstream,
                "title": f"引擎已确认同步，上游为 {upstream}"}
    if status in ("behind", "pending"):
        return {"kind": "behind", "text": "落后上游", "sha": upstream,
                "title": f"本地落后上游提交 {upstream}"}
    return {"kind": "unsynced", "text": "未同步", "sha": upstream,
            "title": f"本地与上游不一致，上游为 {upstream}"}


def fork_tone(verdict):
    """Colour for the local SHA: neutral until the fork is known to lag."""
    return {"behind": "sha-behind", "unsynced": "sha-unsynced"}.get(verdict["kind"], "")


def short_time(value):
    """`2026-10-12T12:29:46+00:00` -> `10-12 12:29`.

    The raw ISO string widened the list enough to push the last column off
    screen once the comparison column was added; the detail page keeps it.
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
        item["checked_at"] = short_time(item.get("last_check_at"))
        annotated.append(item)
    return annotated
