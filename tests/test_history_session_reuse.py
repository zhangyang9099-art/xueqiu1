import os
import tempfile
import unittest
from unittest.mock import patch

from core.client import XueqiuClient
from core.rate_limiter import RateLimiter
from scrapers.scrape_cmd import _build_history_runtime_config, _scrape_stocks
from storage.database import Database


class _DummyCookieManager:
    def get_browser_cookies(self):
        return []

    def get_token(self):
        return ""


class _DummyRateLimiter:
    total_requests = 0

    def wait(self):
        return None

    def on_failure(self):
        return None

    def on_success(self):
        return None


class _FakeClient:
    def __init__(self):
        self.rate_limiter = _DummyRateLimiter()
        self.last_failure_meta = {}

    def get_verification_diagnostics(self):
        return {}

    def get_last_failure_meta(self):
        return dict(self.last_failure_meta)


class _FakeStockCommentScraper:
    calls = []
    scripted_results = {}
    call_counts = {}

    def __init__(self, client, db, config):
        self.client = client
        self.db = db
        self.config = config

    def scrape_stock(self, sym, name, mode="update"):
        _FakeStockCommentScraper.calls.append((sym, name, mode, self.client, self.db))
        count = _FakeStockCommentScraper.call_counts.get(sym, 0)
        _FakeStockCommentScraper.call_counts[sym] = count + 1
        scripted = _FakeStockCommentScraper.scripted_results.get(sym, [])
        if count < len(scripted):
            result = dict(scripted[count])
            self.client.last_failure_meta = dict(result.get("failure_meta", {}) or {})
            return result
        self.client.last_failure_meta = {}
        return {
            "symbol": sym,
            "name": name,
            "status": "success",
            "new_posts": 0,
            "new_comments": 0,
        }


class HistorySessionReuseTests(unittest.TestCase):
    def test_manual_profile_defaults_to_session_profile(self):
        client = XueqiuClient(
            _DummyCookieManager(),
            _DummyRateLimiter(),
            {"session_profile_dir": "data/xueqiu_session_profile"},
        )
        self.assertTrue(client._manual_verification_profile_dir().endswith("/data/xueqiu_session_profile"))

    def test_session_state_distinguishes_captcha_and_expired(self):
        client = XueqiuClient(_DummyCookieManager(), _DummyRateLimiter(), {})
        client._runtime_cookie_names = lambda: {"xq_a_token", "u"}
        self.assertEqual("captcha_required", client._current_session_state("访问验证 请按住滑块 日志ID abc"))
        client._runtime_cookie_names = lambda: {"xq_a_token"}
        self.assertEqual("session_expired", client._current_session_state("首页 登录 注册"))
        client._runtime_cookie_names = lambda: {"xq_a_token", "u"}
        self.assertEqual("ok", client._current_session_state("雪球首页"))

    def test_history_runtime_config_enables_persistent_context(self):
        cfg = _build_history_runtime_config({"manual_verification_profile_dir": "data/manual_chrome_profile"})
        self.assertTrue(cfg["use_persistent_context"])
        self.assertEqual("data/manual_chrome_profile", cfg["session_profile_dir"])

    def test_history_runtime_config_carries_adaptive_pacing_settings(self):
        cfg = _build_history_runtime_config(
            {
                "history_adaptive_pacing": True,
                "history_adaptive_fast_min_request_interval_seconds": 6,
                "history_adaptive_fast_max_request_interval_seconds": 9,
                "history_adaptive_slow_min_request_interval_seconds": 8,
                "history_adaptive_slow_max_request_interval_seconds": 14,
                "history_adaptive_success_threshold": 12,
                "history_adaptive_slow_request_count": 18,
            }
        )
        self.assertTrue(cfg["adaptive_pacing_enabled"])
        self.assertEqual(6, cfg["adaptive_fast_min_interval"])
        self.assertEqual(9, cfg["adaptive_fast_max_interval"])
        self.assertEqual(8, cfg["adaptive_slow_min_interval"])
        self.assertEqual(14, cfg["adaptive_slow_max_interval"])
        self.assertEqual(12, cfg["adaptive_success_threshold"])
        self.assertEqual(18, cfg["adaptive_slow_request_count"])

    def test_history_shared_runtime_does_not_construct_new_client_per_chunk(self):
        _FakeStockCommentScraper.calls = []
        _FakeStockCommentScraper.scripted_results = {}
        _FakeStockCommentScraper.call_counts = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "xueqiu.db")
            db_cfg = {"sqlite_path": db_path}
            db = Database(db_cfg)
            try:
                db.upsert_stock("SZ000733", "振华科技")
            finally:
                db.close()

            shared_runtime = {
                "client": _FakeClient(),
                "db": object(),
                "config": {"use_persistent_context": True, "history_reuse_single_client": True},
            }

            with patch("scrapers.scrape_cmd.XueqiuClient", side_effect=AssertionError("unexpected new client")):
                with patch("scrapers.stock_comments.StockCommentScraper", _FakeStockCommentScraper):
                    results = _scrape_stocks(
                        [("SZ000733", "振华科技")],
                        workers=1,
                        cookie_manager=_DummyCookieManager(),
                        scraping_cfg={"history_reuse_single_client": True, "history_chunk_pages": 1},
                        db_cfg=db_cfg,
                        mode="backfill",
                        total_pages=1,
                        shared_runtime=shared_runtime,
                    )

        self.assertEqual(1, len(results))
        self.assertEqual("success", results[0]["status"])
        self.assertEqual(1, len(_FakeStockCommentScraper.calls))

    def test_history_state_db_is_reused_across_chunks(self):
        _FakeStockCommentScraper.calls = []
        _FakeStockCommentScraper.call_counts = {}
        _FakeStockCommentScraper.scripted_results = {
            "SZ000733": [
                {
                    "symbol": "SZ000733",
                    "name": "振华科技",
                    "status": "success",
                    "new_posts": 5,
                    "new_comments": 0,
                },
                {
                    "symbol": "SZ000733",
                    "name": "振华科技",
                    "status": "success",
                    "new_posts": 3,
                    "new_comments": 0,
                },
            ],
        }

        class _FakeStateDb:
            init_count = 0
            close_count = 0
            windows = [
                {"first_post_time": 1000, "history_complete": 0},
                {"first_post_time": 900, "history_complete": 0},
                {"first_post_time": 900, "history_complete": 0},
                {"first_post_time": 800, "history_complete": 1},
            ]

            def __init__(self, config):
                type(self).init_count += 1

            def get_stock_time_windows(self, symbols=None, active_only=False):
                if type(self).windows:
                    return [type(self).windows.pop(0)]
                return [{"first_post_time": 800, "history_complete": 1}]

            def get_stock_history_cursor(self, sym):
                return {"page": 0}

            def close(self):
                type(self).close_count += 1

        shared_runtime = {
            "client": _FakeClient(),
            "db": object(),
            "config": {"use_persistent_context": True, "history_reuse_single_client": True},
        }

        with patch("scrapers.scrape_cmd.Database", _FakeStateDb):
            with patch("scrapers.scrape_cmd.XueqiuClient", side_effect=AssertionError("unexpected new client")):
                with patch("scrapers.stock_comments.StockCommentScraper", _FakeStockCommentScraper):
                    results = _scrape_stocks(
                        [("SZ000733", "振华科技")],
                        workers=1,
                        cookie_manager=_DummyCookieManager(),
                        scraping_cfg={"history_reuse_single_client": True, "history_chunk_pages": 1},
                        db_cfg={"sqlite_path": ":memory:"},
                        mode="backfill",
                        total_pages=2,
                        shared_runtime=shared_runtime,
                    )

        self.assertEqual(1, len(results))
        self.assertEqual("success", results[0]["status"])
        self.assertEqual(1, _FakeStateDb.init_count)
        self.assertEqual(1, _FakeStateDb.close_count)
        self.assertEqual(2, _FakeStockCommentScraper.call_counts["SZ000733"])

    def test_history_transient_failure_retries_current_stock_before_failing_batch(self):
        _FakeStockCommentScraper.calls = []
        _FakeStockCommentScraper.call_counts = {}
        _FakeStockCommentScraper.scripted_results = {
            "SZ000733": [
                {
                    "symbol": "SZ000733",
                    "name": "振华科技",
                    "status": "failed",
                    "new_posts": 0,
                    "new_comments": 0,
                    "error": "非预期 HTML 响应",
                    "error_category": "unexpected_html",
                    "failure_meta": {
                        "category": "unexpected_html",
                        "transport": "page",
                        "has_auth_cookies": True,
                        "html_excerpt": "<html>blocked</html>",
                    },
                    "last_page": 7,
                },
                {
                    "symbol": "SZ000733",
                    "name": "振华科技",
                    "status": "success",
                    "new_posts": 8,
                    "new_comments": 0,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "xueqiu.db")
            db_cfg = {"sqlite_path": db_path}
            db = Database(db_cfg)
            try:
                db.upsert_stock("SZ000733", "振华科技")
            finally:
                db.close()

            shared_runtime = {
                "client": _FakeClient(),
                "db": object(),
                "config": {"use_persistent_context": True, "history_reuse_single_client": True},
            }

            with patch("scrapers.scrape_cmd.XueqiuClient", side_effect=AssertionError("unexpected new client")):
                with patch("scrapers.stock_comments.StockCommentScraper", _FakeStockCommentScraper):
                    results = _scrape_stocks(
                        [("SZ000733", "振华科技")],
                        workers=1,
                        cookie_manager=_DummyCookieManager(),
                        scraping_cfg={"history_reuse_single_client": True, "history_chunk_pages": 1},
                        db_cfg=db_cfg,
                        mode="backfill",
                        total_pages=1,
                        shared_runtime=shared_runtime,
                    )

        self.assertEqual(1, len(results))
        self.assertEqual("success", results[0]["status"])
        self.assertEqual(8, results[0]["new_posts"])
        self.assertEqual(2, _FakeStockCommentScraper.call_counts["SZ000733"])


class AdaptiveRateLimiterTests(unittest.TestCase):
    def test_adaptive_history_pacing_promotes_fast_mode_then_backs_off_on_failure(self):
        rl = RateLimiter(
            {
                "min_request_interval": 7,
                "max_request_interval": 11,
                "adaptive_pacing_enabled": True,
                "adaptive_fast_min_interval": 6,
                "adaptive_fast_max_interval": 9,
                "adaptive_slow_min_interval": 8,
                "adaptive_slow_max_interval": 14,
                "adaptive_success_threshold": 3,
                "adaptive_slow_request_count": 2,
            }
        )

        self.assertEqual((7.0, 11.0), rl._current_wait_bounds())
        rl.on_success()
        rl.on_success()
        rl.on_success()
        self.assertEqual((6.0, 9.0), rl._current_wait_bounds())

        rl.on_failure()
        lo, hi = rl._current_wait_bounds()
        self.assertGreaterEqual(lo, 8.0)
        self.assertGreaterEqual(hi, 14.0)

        rl.on_success()
        rl.on_success()
        lo, hi = rl._current_wait_bounds()
        self.assertGreaterEqual(lo, 7.0)
        self.assertGreaterEqual(hi, 11.0)


if __name__ == "__main__":
    unittest.main()
