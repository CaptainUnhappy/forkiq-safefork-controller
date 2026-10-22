"""User-facing explanations; raw errors stay intact for diagnosis and history."""
import re


def error_guidance(message):
    """Explain known failures, including historical GitHub payloads, without guessing."""
    text = (message or "").lower()
    if "object does not exist" in text:
        return {"summary": "GitHub 找不到要同步的提交，本次同步未完成。",
                "action": "镜像模式会重新读取上游提交并重试一次；若仍失败，约 10 分钟后自动再试，也可在仓库详情点「立即同步」。"}
    if "workflow" in text and any(word in text for word in ("permission", "权限", "integration", "refusing")):
        return {"summary": "同步涉及工作流文件，但 GitHub App 缺少 Workflows 写入权限。",
                "action": "请在 GitHub App 设置中授予 Workflows 读写权限，并在安装页面批准新增权限；完成后会自动重试。"}
    if "rate limit" in text or "限流" in text:
        return {"summary": "GitHub API 请求额度受限，检查或同步暂未完成。",
                "action": "后台会继续按周期尝试；需等待 GitHub 解除限流，反复点击同步不会加快恢复。"}
    if any(word in text for word in ("仓库不可访问", "无法访问此仓库", "仓库已不在 github 上", "在 github 上已不存在")):
        return {"summary": "GitHub App 无法访问此仓库，暂时不能同步。",
                "action": "请确认仓库仍存在，并检查 App 是否获准访问；仅凭无法访问不能确定仓库已被删除。恢复授权后可点「发现新 fork」重新检查。"}
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
                "action": "后台会自动重试；仓库同步失败通常约 10 分钟后再试，持续失败时请检查服务器网络。"}
    if re.search(r"(?:\bhttp(?:\s+error)?\s*|->\s*|['\"]status['\"]:\s*['\"]?)(?:500|502|503|504)\b", text):
        return {"summary": "GitHub 服务暂时异常，本次检查或同步未完成。",
                "action": "后台会自动重试；仓库同步失败通常约 10 分钟后再试，无需修改仓库或权限。"}
    return None
