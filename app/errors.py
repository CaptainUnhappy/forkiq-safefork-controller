"""User-facing explanations; raw errors stay intact for diagnosis and history."""
import re


def error_guidance(message):
    """Explain known failures, including historical GitHub payloads, without guessing."""
    text = (message or "").lower()
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
