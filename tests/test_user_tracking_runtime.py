import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from storage.database import Database
from scrapers.user_tracker import UserTracker
from utils.query_progress import parse_user_history_log


class UserTrackingRuntimeTests(unittest.TestCase):
    def test_update_mode_does_not_stop_when_page_contains_new_and_old_mixed(self):
        tracker = UserTracker(
            client=SimpleNamespace(),
            db=SimpleNamespace(),
            config={},
        )
        self.assertFalse(tracker._should_stop_update_after_page(5, True))
        self.assertTrue(tracker._should_stop_update_after_page(0, True))
        self.assertFalse(tracker._should_stop_update_after_page(0, False))

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


if __name__ == "__main__":
    unittest.main()
