"""Known failures get actionable copy without changing the original evidence."""
import unittest

from app.errors import error_guidance, failure_summary, protection_notice


class ErrorGuidanceTests(unittest.TestCase):
    def test_failure_summary_keeps_safe_diagnostics_not_exception_payload(self):
        error = RuntimeError('private-token-and-key-path')
        error.stage, error.status, error.category = 'installation_token', 403, 'certificate'
        summary = failure_summary(error)
        for value in ('installation_token', 'RuntimeError', 'HTTP 403', 'certificate'):
            self.assertIn(value, summary)
        self.assertNotIn('private-token', summary)
        error.stage, error.status, error.category = ['private-token'], 'private-token', 'private-token'
        summary = failure_summary(error, 'reconcile')
        self.assertIn('reconcile', summary)
        self.assertNotIn('private-token', summary)

    def test_history_and_source_guards_are_warnings(self):
        for message in ("不是快进：refs/heads/main；保留目标历史，未写入任何引用。",
                        "服务器已有历史不能后退或移动标签：refs/heads/main",
                        "标签冲突：refs/tags/v1；不会移动已有标签。",
                        "公开目标的 Fork 来源变化；不会选择替代上游。",
                        "源主分支不存在，停止同步。",
                        "异常删除：refs/heads/main；需要单独审查，不自动放行。",
                        "上游仓库不可访问（HTTP 404）；已停止同步。"):
            with self.subTest(message=message):
                notice = protection_notice(message)
                self.assertTrue(notice["label"])
                self.assertEqual(error_guidance(message), notice)
                self.assertIn("保留", notice["action"])

    def test_unknown_failures_and_target_auth_are_not_protection_warnings(self):
        for message in (None, "", "检查或连接失败；已停写，请核对凭据、网络与仓库状态后预览。",
                        "GitHub 无法读取已登记仓库或权限（HTTP 404）；不会改用其他来源。",
                        "GitHub HTTP 401", "可用磁盘不足 1 GiB，已停止；请先调整容量。",
                        "目标 Actions 仍启用；请先在 GitHub 全局关闭，控制器不会替你启用或关闭。",
                        "unknown failure: 不是快进：refs/heads/main"):
            with self.subTest(message=message):
                self.assertIsNone(protection_notice(message))
        self.assertIn("不能确定", protection_notice("上游仓库不可访问（HTTP 404）；已停止同步。")["summary"])

    def test_historical_payload_and_typed_object_error_share_an_explanation(self):
        old = "强制对齐失败：{'message': 'Object does not exist', 'status': '422'}"
        new = "GitHub HTTP 422: Object does not exist"
        self.assertEqual(error_guidance(old), error_guidance(new))
        self.assertIn("未完成", error_guidance(old)["summary"])
        self.assertIn("不会强推", error_guidance(old)["action"])

    def test_permissions_are_actionable_not_promised_to_self_heal(self):
        for message, expected in (("App 缺 Workflows 权限", "批准"),
                                  ("Resource not accessible by integration", "授权"),
                                  ("protected branch update failed", "不会自动关闭"),
                                  ("Bad credentials", "重新连接")):
            with self.subTest(message=message):
                self.assertIn(expected, error_guidance(message)["action"])

    def test_transient_service_and_network_failures_explain_retries(self):
        for message in ("GitHub GET /repos/me/demo -> 502: Bad Gateway", "HTTP 503",
                        "{'status': '504'}", "<urlopen error timed out>"):
            with self.subTest(message=message):
                self.assertIn("不会自动重试", error_guidance(message)["action"])

    def test_rate_limiting_does_not_encourage_repeated_requests(self):
        self.assertIn("不会加快", error_guidance("API rate limit exceeded")["action"])

    def test_inaccessible_repo_does_not_claim_it_was_deleted(self):
        self.assertIn("不能确定", error_guidance("GitHub App 无法访问此仓库")["action"])

    def test_unknown_or_clear_errors_are_not_replaced_with_a_guessed_cause(self):
        for message in (None, "", "上游与本地分支不一致，请确认后恢复", "unknown failure", "release v1502",
                        "有 500 个独有提交，不会覆盖", "上游没有与 fork 同名的分支 502"):
            self.assertIsNone(error_guidance(message))
