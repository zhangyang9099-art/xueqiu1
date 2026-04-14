import unittest

from main import _build_comment_backfill_runtime_config
from core.rate_limiter import RateLimiter


class CommentBackfillRuntimeTests(unittest.TestCase):
    def test_comment_backfill_uses_history_like_pacing(self):
        cfg = {
            "scraping": {
                "history_min_request_interval_seconds": 6,
                "history_max_request_interval_seconds": 12,
                "history_burst_rest_count": 30,
                "history_burst_rest_seconds_min": 120,
                "history_burst_rest_seconds_max": 240,
                "session_profile_dir": "data/manual_chrome_profile",
            }
        }

        runtime = _build_comment_backfill_runtime_config(cfg)
        scraping = runtime["scraping"]

        self.assertTrue(scraping["use_persistent_context"])
        self.assertEqual(6.0, scraping["min_request_interval"])
        self.assertEqual(12.0, scraping["max_request_interval"])
        self.assertEqual(30, scraping["burst_rest_count"])
        self.assertFalse(scraping["comment_mode_enabled"])
        self.assertEqual(75.0, scraping["comment_post_budget_seconds"])

    def test_comment_mode_can_be_disabled(self):
        rl = RateLimiter({"comment_mode_enabled": False})
        rl.enter_comment_mode()
        self.assertFalse(rl._in_comment_mode)


if __name__ == "__main__":
    unittest.main()
