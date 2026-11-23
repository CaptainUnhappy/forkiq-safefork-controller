"""User-facing explanations; raw errors stay intact for diagnosis and history."""
import re


def failure_summary(error, stage="sync"):
    """Keep bounded diagnostic fields, never exception arguments or remote payloads."""
    stages = {"sync", "metadata", "transport", "installation_token", "app_signing", "push", "reconcile"}
    error_stage = getattr(error, "stage", None)
    phase = error_stage if isinstance(error_stage, str) and error_stage in stages else stage if stage in stages else "sync"
    name = type(error).__name__
    name = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "Exception"
    fields = ["阶段 " + phase, "异常 " + name]
    category = getattr(error, "category", None)
    if category in ("network", "certificate"):
        fields.append("类别 " + category)
    status = getattr(error, "status", None)
    if type(status) is int and 100 <= status <= 599:
        fields.append("HTTP " + str(status))
    return "检查或连接失败（" + "；".join(fields) + "）；已停写，请核对凭据、网络与仓库状态后预览。"


def protection_notice(message):
    """Classify known upstream safety guards, never generic failures or raw HTTP payloads."""
    text = (message or "").strip()
    if text.startswith(("不是快进：", "服务器已有历史不能后退或移动标签：", "标签冲突：")):
        return {"label": "历史保护", "summary": "上游与已保存历史不兼容，同步已安全停止。",
                "action": "可能是上游回退、分叉、移动标签，或目标含独有历史。现有历史保留；请核对差异后再决定如何跟踪，不会自动覆盖。"}
    if text.startswith("上游仓库不可访问（HTTP 404）"):
        return {"label": "上游不可用", "summary": "上游当前不可访问；仅凭 HTTP 404 不能确定已删除，也可能转为私有或授权变化。",
                "action": "现有仓库和服务器历史保留，写入已停止。请核对原上游及读取权限，不会自动切换来源。"}
    if text.startswith(("公开目标的 Fork 来源变化；", "源默认分支变化，")):
        return {"label": "来源变化", "summary": "上游来源关系或默认分支变化，同步已安全停止。",
                "action": "原来源配置和现有历史保留。请核对仓库身份；不会自动改用新的上游或默认分支。"}
    if text.startswith("源主分支不存在，"):
        return {"label": "上游分支缺失", "summary": "已登记的上游主分支不可用，同步已安全停止。",
                "action": "现有历史保留。请确认原分支是否删除或改名，不会自动选择替代分支。"}
    if text.startswith(("异常删除：", "目标文件树为空：")):
        return {"label": "删除保护", "summary": "上游变更包含大量删除或空文件树，已触发保护。",
                "action": "现有历史保留，未自动放行删除。请先核对上游改动及完整引用计划。"}
    return None


def error_guidance(message):
    """Explain known failures, including historical GitHub payloads, without guessing."""
    notice = protection_notice(message)
    if notice:
        return notice
    text = (message or "").lower()
    if "只读结果核验" in text or "只读推送结果核验" in text:
        return {"summary": "正在核对上次推送的完整结果，暂不重复推送。",
                "action": "已完成的更新会直接确认；只有确认目标完全未变且为网络错误时，才重新规划并有限重试。可暂停取消自动恢复。"}
    if "运行资源不足" in text:
        return {"summary": "服务器运行资源不足，本次同步已暂停。",
                "action": "请检查容器进程回收、进程上限和内存。处理后先做只读预览，再恢复同步；不会自动重复推送。"}
    if "等待自动重试" in text:
        return {"summary": "本轮网络读取未完成，已安排自动重试。",
                "action": "本轮尚未开始保存或推送，无需反复点击；可在仓库详情查看时间或暂停。"}
    if "object does not exist" in text:
        return {"summary": "GitHub 找不到要同步的提交，本次同步未完成。",
                "action": "同步已暂停。核对仓库身份和完整历史，完成只读预览后再确认恢复；不会强推或自动重试写入。"}
    if "workflow" in text and any(word in text for word in ("permission", "权限", "integration", "refusing")):
        return {"summary": "同步涉及工作流文件，但 GitHub App 缺少 Workflows 写入权限。",
                "action": "核对专用 App 的 Workflows 读写权限及安装批准；目标 Actions 必须持续关闭。完成只读预览后再确认恢复。"}
    if "rate limit" in text or "限流" in text:
        return {"summary": "GitHub API 请求额度受限，检查或同步暂未完成。",
                "action": "已暂停写入。等待 GitHub 解除限流后先做只读预览；反复点击同步不会加快恢复。"}
    if any(word in text for word in ("仓库不可访问", "无法访问此仓库", "仓库已不在 github 上", "在 github 上已不存在")):
        return {"summary": "GitHub App 无法访问此仓库，暂时不能同步。",
                "action": "请确认原仓库仍存在，并检查 App 是否获准访问；仅凭无法访问不能确定仓库已被删除。保持原身份配置，恢复授权后先做只读预览。"}
    if any(word in text for word in ("protected branch", "branch protection", "repository rule", "protected ref")):
        return {"summary": "GitHub 的分支保护或仓库规则阻止了本次同步。",
                "action": "请在 GitHub 检查分支规则与 App 权限；面板不会自动关闭保护或绕过规则。"}
    if any(word in text for word in ("bad credentials", "认证失败", "获取安装令牌失败", "authentication")):
        return {"summary": "GitHub 身份验证失败，暂时无法检查或同步仓库。",
                "action": "请到系统设置检查 GitHub App 的安装与授权；必要时重新连接 GitHub。"}
    if any(word in text for word in ("resource not accessible", "推送被 github 拒绝", "合并被 github 拒绝")):
        return {"summary": "GitHub 拒绝了本次同步，可能涉及访问权限或仓库规则。",
                "action": "请检查 GitHub App 的仓库授权和分支规则；具体原因见技术详情。"}
    if any(word in text for word in ("timed out", "timeout", "connection reset", "connection refused",
                                     "remote end closed", "name resolution", "urlopen error", "network down")):
        return {"summary": "连接 GitHub 失败，本次检查或同步未完成。",
                "action": "已暂停写入，不会自动重试。先检查网络；若推送结果不确定，应核对目标全部引用，再做只读预览。"}
    if re.search(r"(?:\bhttp(?:\s+error)?\s*|->\s*|['\"]status['\"]:\s*['\"]?)(?:500|502|503|504)\b", text):
        return {"summary": "GitHub 服务暂时异常，本次检查或同步未完成。",
                "action": "已暂停写入，不会自动重试。等待服务恢复后先做只读预览，不要通过扩大权限或强推绕过。"}
    return None
