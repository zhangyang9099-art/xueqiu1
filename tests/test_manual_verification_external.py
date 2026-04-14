import unittest
from unittest.mock import patch

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


class _FakePlaywright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class ManualVerificationExternalTests(unittest.TestCase):
    def test_external_mode_is_default(self):
        client = XueqiuClient(_DummyCookieManager(), _DummyRateLimiter(), {})
        self.assertEqual("external", client._manual_verification_mode)

    def test_manual_profile_dir_is_absolute(self):
        client = XueqiuClient(
            _DummyCookieManager(),
            _DummyRateLimiter(),
            {"manual_verification_profile_dir": "data/manual_chrome_profile"},
        )
        profile_dir = client._manual_verification_profile_dir()
        self.assertTrue(profile_dir.endswith("/data/manual_chrome_profile"))
        self.assertTrue(profile_dir.startswith("/"))

    @patch("core.client.sys.platform", "darwin")
    def test_external_command_uses_system_chrome_instance(self):
        client = XueqiuClient(
            _DummyCookieManager(),
            _DummyRateLimiter(),
            {"manual_verification_profile_dir": "data/manual_chrome_profile"},
        )
        client._manual_verification_browser_app = lambda: "/Applications/Google Chrome.app"
        cmd = client._build_external_verification_command("https://xueqiu.com/S/SH603345")
        self.assertEqual("open", cmd[0])
        self.assertIn("-Wna", cmd)
        self.assertIn("/Applications/Google Chrome.app", cmd)
        self.assertIn("--args", cmd)
        joined = " ".join(cmd)
        self.assertNotIn("remote-debugging-pipe", joined)
        self.assertNotIn("--enable-automation", joined)

    def test_close_stops_playwright_even_if_runtime_already_marked_closed(self):
        client = XueqiuClient(_DummyCookieManager(), _DummyRateLimiter(), {})
        fake = _FakePlaywright()
        client._closed = True
        client._playwright = fake
        client.close()
        self.assertTrue(fake.stopped)
        self.assertIsNone(client._playwright)


if __name__ == "__main__":
    unittest.main()
