"""Known failures get actionable copy without changing the original evidence."""
import unittest

from app.errors import error_guidance


class ErrorGuidanceTests(unittest.TestCase):
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
