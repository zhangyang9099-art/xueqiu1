import unittest
from unittest.mock import patch

from scrapers.stock_comments import StockCommentScraper


class _DummyClient:
    pass


class _TrackingDb:
    def __init__(self):
        self.days_calls = []

    def get_posts_needing_backfill(self, symbol=None, days=7):
        self.days_calls.append(days)
        return []

    def get_posts_with_orphan_comments(self, symbol=None, days=None):
        return []


class CommentBackfillScopeTests(unittest.TestCase):
    def test_days_zero_means_full_history(self):
        db = _TrackingDb()
        scraper = StockCommentScraper(_DummyClient(), db, {"comment_backfill_days": 7})

        with patch("scrapers.stock_comments.logger"):
            result = scraper.backfill_comments(symbol="SZ000733", days=0)

        self.assertEqual({"total_posts": 0, "new_comments": 0}, result)
        self.assertEqual([None], db.days_calls)


if __name__ == "__main__":
    unittest.main()
