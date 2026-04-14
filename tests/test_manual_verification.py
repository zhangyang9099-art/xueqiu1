import unittest

from core.client import XueqiuClient


class _DummyCookieManager:
    def get_browser_cookies(self):
        return []

    def get_token(self):
        return ""


class _DummyRateLimiter:
    def wait(self):
        return None

    def on_failure(self):
        return None

    def on_success(self):
        return None


class ManualVerificationTests(unittest.TestCase):
    def make_client(self, **overrides):
        config = {
            "manual_verification_enabled": True,
            "manual_verification_auto_refresh": False,
            "manual_verification_grace_seconds": 45,
            "manual_verification_failure_stable_seconds": 12,
            "manual_verification_refresh_cooldown_seconds": 45,
        }
        config.update(overrides)
        return XueqiuClient(_DummyCookieManager(), _DummyRateLimiter(), config)

    def test_generic_retry_word_does_not_count_as_failure(self):
        client = self.make_client()
        self.assertFalse(client._text_has_verification_failure("访问验证 请按住滑块后重试一次"))
        self.assertFalse(client._text_has_verification_failure("请重试滑块"))

    def test_explicit_failure_prompt_is_detected(self):
        client = self.make_client()
        reason = client._verification_failure_reason("访问验证 验证失败 请刷新重试")
        self.assertEqual("验证失败/请刷新重试", reason)
        self.assertTrue(client._text_has_verification_failure("访问验证 验证失败 请刷新重试"))

    def test_extracts_verification_log_id(self):
        client = self.make_client()
        self.assertEqual(
            "abc123xyz",
            client._extract_verification_log_id("访问验证 日志ID: abc123xyz 请按住滑块"),
        )

    def test_auto_refresh_requires_grace_and_persistent_failure(self):
        client = self.make_client(manual_verification_auto_refresh=True)
        self.assertFalse(
            client._should_auto_refresh_verification(
                now=8,
                challenge_opened_at=0,
                failure_since=6,
                last_refresh_at=None,
            )
        )
        self.assertFalse(
            client._should_auto_refresh_verification(
                now=50,
                challenge_opened_at=0,
                failure_since=45,
                last_refresh_at=None,
            )
        )
        self.assertTrue(
            client._should_auto_refresh_verification(
                now=70,
                challenge_opened_at=0,
                failure_since=50,
                last_refresh_at=None,
            )
        )
        self.assertFalse(
            client._should_auto_refresh_verification(
                now=80,
                challenge_opened_at=0,
                failure_since=50,
                last_refresh_at=60,
            )
        )


if __name__ == "__main__":
    unittest.main()
