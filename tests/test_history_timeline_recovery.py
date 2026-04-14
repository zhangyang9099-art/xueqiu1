import unittest
from unittest.mock import MagicMock

from core.client import XueqiuClient
from core.exceptions import RequestFailed
from scrapers.stock_comments import StockCommentScraper


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


class _DummyDb:
    def __init__(self, cursor=None):
        self._cursor = cursor or {"page": 0, "oldest_time": 0, "updated_at": 0}

    def get_stock_history_cursor(self, symbol):
        return dict(self._cursor)


class TimelineFailureClassificationTests(unittest.TestCase):
    def make_client(self):
        return XueqiuClient(_DummyCookieManager(), _DummyRateLimiter(), {})

    def test_wait_for_function_timeout_is_transport_timeout(self):
        client = self.make_client()
        info = client._classify_request_failure(
            status=0,
            content_type="",
            body="",
            error="Page.wait_for_function: Timeout 30000ms exceeded.",
        )
        self.assertEqual("transport_timeout", info["category"])

    def test_html_waf_is_explicit_waf(self):
        client = self.make_client()
        info = client._classify_request_failure(
            status=200,
            content_type="text/html",
            body="<html>aliyun_waf waf_abc</html>",
            error="",
        )
        self.assertEqual("explicit_waf", info["category"])

    def test_session_state_with_auth_cookies_and_login_text_is_not_expired(self):
        client = self.make_client()
        client._runtime_cookie_names = lambda: {"xq_a_token", "u"}
        self.assertEqual("unknown", client._current_session_state("首页 登录 注册"))

    def test_transport_timeout_does_not_refresh_waf(self):
        client = self.make_client()
        client._ensure_browser = MagicMock()
        client._warm_referer_path = MagicMock()
        client._request_via_page = MagicMock(
            side_effect=[
                {
                    "ok": False,
                    "status": 0,
                    "contentType": "",
                    "body": "",
                    "error": "Page.wait_for_function: Timeout 30000ms exceeded.",
                },
                {
                    "ok": False,
                    "status": 0,
                    "contentType": "",
                    "body": "",
                    "error": "Page.wait_for_function: Timeout 30000ms exceeded.",
                },
            ]
        )
        client._refresh_waf = MagicMock()
        client._recover_transport_failure = MagicMock()

        with self.assertRaises(RequestFailed) as ctx:
            client.get(
                "https://xueqiu.com/query/v1/symbol/search/status.json",
                referer_path="/S/SH603345",
                max_retries=2,
                timeout_ms=1000,
            )

        self.assertEqual("transport_timeout", ctx.exception.category)
        client._refresh_waf.assert_not_called()
        client._recover_transport_failure.assert_called_once()


class HistoryCursorResolutionTests(unittest.TestCase):
    def make_scraper(self, cursor):
        client = MagicMock()
        db = _DummyDb(cursor=cursor)
        return StockCommentScraper(client, db, {"history_cursor_enabled": True})

    def test_resolve_history_start_page_prefers_persisted_cursor(self):
        scraper = self.make_scraper({"page": 7, "oldest_time": 100, "updated_at": 1})
        scraper._fetch_timeline_page = MagicMock(
            return_value={
                "posts": [{"id": "1"}],
                "oldest": 80,
                "newest": 95,
                "max_page": 120,
                "has_more": True,
            }
        )
        scraper._locate_history_start_page = MagicMock()

        page, page_cache, max_page = scraper._resolve_history_start_page(
            "SZ000001",
            90,
            10,
            "测试股票",
        )

        self.assertEqual(7, page)
        self.assertIn(7, page_cache)
        self.assertEqual(120, max_page)
        scraper._locate_history_start_page.assert_not_called()

    def test_resolve_history_start_page_keeps_cursor_when_cursor_page_is_still_usable(self):
        scraper = self.make_scraper({"page": 7, "oldest_time": 100, "updated_at": 1})
        scraper._fetch_timeline_page = MagicMock(
            return_value={
                "posts": [{"id": "1"}],
                "oldest": 120,
                "newest": 150,
                "max_page": 120,
                "has_more": True,
            }
        )
        scraper._locate_history_start_page = MagicMock(return_value=(11, {11: {"posts": []}}, 120))

        page, page_cache, max_page = scraper._resolve_history_start_page(
            "SZ000001",
            90,
            10,
            "测试股票",
        )

        self.assertEqual(7, page)
        self.assertIn(7, page_cache)
        self.assertEqual(120, max_page)
        scraper._locate_history_start_page.assert_not_called()


class HistoryTransportFallbackTests(unittest.TestCase):
    def test_history_mode_defaults_to_page_transport(self):
        scraper = StockCommentScraper(MagicMock(), _DummyDb(), {})
        self.assertEqual("page", scraper._timeline_transport_for_mode("backfill"))

    def test_fetch_timeline_page_falls_back_to_isolated_page_after_transport_failure(self):
        client = MagicMock()
        client.get.side_effect = [
            RequestFailed("transport_timeout", "boom"),
            {"list": [{"id": "1", "created_at": 1}], "maxPage": 10, "page": 3},
        ]
        scraper = StockCommentScraper(
            client,
            _DummyDb(),
            {
                "history_timeline_transport": "page",
                "history_timeline_fallback_transport": "isolated_page",
            },
        )

        info = scraper._fetch_timeline_page("SZ000001", 3, 10, mode="backfill")

        self.assertEqual(2, client.get.call_count)
        self.assertEqual("page", client.get.call_args_list[0].kwargs["transport"])
        self.assertEqual("isolated_page", client.get.call_args_list[1].kwargs["transport"])
        self.assertEqual(1, len(info["posts"]))


if __name__ == "__main__":
    unittest.main()
