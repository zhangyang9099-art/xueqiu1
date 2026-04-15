import os
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock

from core.client import XueqiuClient
from core.cookie_manager import CookieManager
from scrapers.api_endpoints import build_user_timeline_params
from scrapers.user_tracker import UserTracker
from storage.database import Database
from utils.query_progress import parse_user_history_log


class UserTrackingRuntimeTests(unittest.TestCase):
    def test_user_timeline_params_include_count(self):
        params = build_user_timeline_params("1505944393", count=40, page=7)
        self.assertEqual("1505944393", params["user_id"])
        self.assertEqual(40, params["count"])
        self.assertEqual(7, params["page"])

    def test_user_history_cursor_and_last_sync_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "xueqiu.db")
            db = Database({"sqlite_path": db_path, "log_lifecycle": False})
            try:
                db.upsert_tracked_user("1505944393", "雪月霜")
                db.update_user_history_cursor("1505944393", 17)
                db.update_user_last_sync_time("1505944393", 1710001234000)

                cursor = db.get_user_history_cursor("1505944393")
                self.assertEqual(17, cursor["page"])

                rows = db.get_user_time_windows(user_ids=["1505944393"], active_only=False)
                self.assertEqual(1, len(rows))
                self.assertEqual(17, int(rows[0]["history_cursor_page"] or 0))
                self.assertEqual(1710001234000, int(rows[0]["last_sync_time"] or 0))

                db.clear_user_history_cursor("1505944393")
                cursor = db.get_user_history_cursor("1505944393")
                self.assertEqual(0, cursor["page"])
            finally:
                db.close()

    def test_parse_user_history_log_extracts_current_pass_and_remaining(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "user_history_20260409_000000.log"
            log_path.write_text(
                "\n".join(
                    [
                        "[2026-04-09 20:00:00] RUN history pass 1",
                        "  · 用户 1505944393(雪月霜) 历史分段 1: 运行 3 页 (剩余计划 100 页)",
                        "✓ 用户 1505944393: 12条发言 [success]",
                        "[2026-04-09 20:05:00] remaining_incomplete=4",
                    ]
                ),
                encoding="utf-8",
            )
            parsed = parse_user_history_log(log_path)

        self.assertEqual(1, len(parsed["targets"]))
        self.assertEqual("1505944393", parsed["targets"][0]["user_id"])
        self.assertEqual("雪月霜", parsed["targets"][0]["screen_name"])
        self.assertEqual(4, parsed["remaining_incomplete"])
        self.assertEqual(1, len(parsed["pass_durations"]))
        self.assertAlmostEqual(300.0, parsed["pass_durations"][0], delta=0.1)
        self.assertEqual(1, len(parsed["last_completed_results"]))
        self.assertEqual(12, parsed["last_completed_results"][0]["statuses"])

    def test_env_token_overrides_config_token(self):
        original = os.environ.get("XUEQIU_TOKEN")
        os.environ["XUEQIU_TOKEN"] = "env_token_value"
        try:
            mgr = CookieManager({"cookie": {"xq_a_token": "config_token_value"}}, config_path="config.yaml")
            self.assertEqual("env_token_value", mgr.get_token())
        finally:
            if original is None:
                os.environ.pop("XUEQIU_TOKEN", None)
            else:
                os.environ["XUEQIU_TOKEN"] = original

    def test_track_user_probes_user_timeline_before_scraping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "xueqiu.db")
            db = Database({"sqlite_path": db_path, "log_lifecycle": False})
            try:
                db.upsert_tracked_user("1505944393", "雪月霜")
                client = MagicMock()
                client.ensure_user_timeline_ready.return_value = {"ok": True, "resolved_count": 20}
                tracker = UserTracker(client, db, {"max_pages_per_user": 2})
                tracker._fetch_timeline_page = MagicMock(return_value={
                    "statuses": [],
                    "max_page": 1,
                    "newest": 0,
                    "oldest": 0,
                })

                result = tracker.track_user("1505944393", "雪月霜", mode="update")

                self.assertEqual("success", result["status"])
                client.ensure_user_timeline_ready.assert_called_once_with(
                    "1505944393",
                    screen_name="雪月霜",
                    probe_count=tracker.timeline_page_size,
                    probe_candidates=tracker.timeline_probe_counts,
                )
                tracker._fetch_timeline_page.assert_called_once_with(
                    "1505944393",
                    1,
                    mode="update",
                    page_size=20,
                )
            finally:
                db.close()

    def test_resolve_user_timeline_count_limit_uses_highest_successful_probe(self):
        client = XueqiuClient(MagicMock(), MagicMock(), {})
        client.probe_user_timeline_access = MagicMock(side_effect=[
            {"ok": False, "category": "http_400_10022", "count": 40},
            {"ok": False, "category": "http_400_10022", "count": 30},
            {"ok": True, "count": 20, "status_count": 21},
        ])

        result = client.resolve_user_timeline_count_limit(
            "2632831661",
            preferred_count=40,
            probe_candidates=[40, 30, 20],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(20, result["resolved_count"])
        self.assertEqual(20, client.get_user_timeline_count_limit("2632831661"))

    def test_resolve_user_timeline_count_limit_stops_on_non_10022_failure(self):
        client = XueqiuClient(MagicMock(), MagicMock(), {})
        client.probe_user_timeline_access = MagicMock(side_effect=[
            {"ok": False, "category": "unexpected_html", "count": 40},
        ])

        result = client.resolve_user_timeline_count_limit(
            "2632831661",
            preferred_count=40,
            probe_candidates=[40, 30, 20],
        )

        self.assertFalse(result["ok"])
        self.assertEqual("unexpected_html", result["category"])

    def test_get_classifies_known_user_timeline_10022_as_count_limit(self):
        client = XueqiuClient(MagicMock(), MagicMock(), {})
        client._ensure_browser = MagicMock()
        client._warm_referer_path = MagicMock()
        client.rate_limiter.wait = MagicMock()
        client.rate_limiter.on_failure = MagicMock()
        client._has_runtime_auth_cookies = MagicMock(return_value=True)
        client._runtime_cookie_names = MagicMock(return_value={"xq_a_token", "u"})
        client._request_via_page = MagicMock(return_value={
            "ok": False,
            "status": 400,
            "contentType": "application/json;charset=UTF-8",
            "body": json.dumps({"error_code": "10022", "error_description": "用户未登录"}),
        })
        client.set_user_timeline_count_limit("2632831661", 20)

        with self.assertRaises(Exception) as ctx:
            client.get(
                "https://xueqiu.com/v4/statuses/user_timeline.json",
                params={"user_id": "2632831661", "count": 40, "page": 1},
                referer_path="/u/2632831661",
                max_retries=1,
                transport="page",
            )

        self.assertEqual("timeline_count_limit", ctx.exception.category)


if __name__ == "__main__":
    unittest.main()
