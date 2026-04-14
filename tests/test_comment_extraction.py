import unittest

from scrapers.api_endpoints import (
    build_stock_timeline_params,
    extract_comment_fields,
    parse_comments_v3_response,
    parse_stock_timeline_response,
)


class CommentExtractionTests(unittest.TestCase):
    def test_extracts_nested_reply_chain_depths(self):
        raw_comment = {
            "id": 3,
            "user": {"id": 300, "screen_name": "child"},
            "text": "reply level 3",
            "created_at": 3000,
            "in_reply_to_comment_id": 2,
            "reply_comment": {
                "id": 2,
                "user": {"id": 200, "screen_name": "parent"},
                "text": "reply level 2",
                "created_at": 2000,
                "in_reply_to_comment_id": 1,
                "reply_comment": {
                    "id": 1,
                    "user": {"id": 100, "screen_name": "root"},
                    "text": "root",
                    "created_at": 1000,
                    "in_reply_to_comment_id": None,
                    "child_comments": [],
                },
                "child_comments": [],
            },
            "child_comments": [],
        }

        comments = {item["id"]: item for item in extract_comment_fields(raw_comment)}
        self.assertEqual({"3"}, set(comments.keys()))
        self.assertEqual(3, comments["3"]["depth"])
        self.assertEqual("2", comments["3"]["parent_comment_id"])
        self.assertEqual("parent", comments["3"]["reply_to_user_name"])

    def test_extracts_inline_child_comments(self):
        raw_comment = {
            "id": 10,
            "user": {"id": 10, "screen_name": "top"},
            "text": "top",
            "created_at": 1000,
            "child_comments": [
                {
                    "id": 11,
                    "user": {"id": 11, "screen_name": "child"},
                    "text": "child",
                    "created_at": 1100,
                    "child_comments": [],
                }
            ],
        }

        comments = {item["id"]: item for item in extract_comment_fields(raw_comment)}
        self.assertEqual(2, len(comments))
        self.assertEqual("10", comments["11"]["parent_comment_id"])
        self.assertEqual(2, comments["11"]["depth"])
        self.assertEqual("top", comments["11"]["reply_to_user_name"])

    def test_extracts_v3_thread_fields_and_child_reply_count(self):
        raw_comment = {
            "id": 10,
            "user": {"id": 10, "screen_name": "top"},
            "text": "top",
            "created_at": 1000,
            "statusId": 12345,
            "root_in_reply_to_status_id": 99999,
            "retweet_status_id": 77777,
            "comment_reply_count": 8,
            "child_comments": [
                {
                    "id": 11,
                    "user": {"id": 11, "screen_name": "child"},
                    "text": "child",
                    "created_at": 1100,
                    "in_reply_to_comment_id": 10,
                    "child_comments": [],
                }
            ],
        }

        comments = {item["id"]: item for item in extract_comment_fields(raw_comment)}
        self.assertEqual("12345", comments["10"]["status_id"])
        self.assertEqual("99999", comments["10"]["root_status_id"])
        self.assertEqual("77777", comments["10"]["retweet_status_id"])
        self.assertEqual(8, comments["10"]["comment_reply_count"])
        self.assertEqual("10", comments["11"]["parent_comment_id"])

    def test_parse_comments_v3_response(self):
        parsed = parse_comments_v3_response(
            {
                "comments": [{"id": 1}],
                "next_max_id": 123,
                "status_reply_count": 110,
                "comment_tl_count": 2,
                "has_filtered": True,
            }
        )
        self.assertEqual([{"id": 1}], parsed["comments"])
        self.assertEqual(123, parsed["next_max_id"])
        self.assertEqual(110, parsed["status_reply_count"])
        self.assertEqual(2, parsed["comment_tl_count"])
        self.assertTrue(parsed["has_filtered"])

    def test_stock_timeline_has_more_when_page_is_full(self):
        posts = [{"id": 1}, {"id": 2}]
        parsed_posts, has_more, max_page = parse_stock_timeline_response(
            {"list": posts},
            requested_count=2,
        )
        self.assertEqual(posts, parsed_posts)
        self.assertTrue(has_more)
        self.assertEqual(0, max_page)

        _, has_more, _ = parse_stock_timeline_response({"list": posts[:1]}, requested_count=2)
        self.assertFalse(has_more)

    def test_stock_timeline_prefers_max_page_metadata(self):
        posts = [{"id": 1}]
        _, has_more, max_page = parse_stock_timeline_response(
            {"list": posts, "page": 2, "maxPage": 100},
            requested_count=10,
        )
        self.assertTrue(has_more)
        self.assertEqual(100, max_page)

        _, has_more, max_page = parse_stock_timeline_response(
            {"list": posts, "page": 100, "maxPage": 100},
            requested_count=10,
        )
        self.assertFalse(has_more)
        self.assertEqual(100, max_page)

    def test_stock_timeline_params_match_web_defaults(self):
        params = build_stock_timeline_params("SZ000733")
        self.assertEqual(10, params["count"])
        self.assertEqual("user", params["source"])
        self.assertEqual("time", params["sort"])
        self.assertEqual(0, params["comment"])
        self.assertEqual(0, params["hl"])
        self.assertEqual("", params["q"])
        self.assertEqual(11, params["type"])


if __name__ == "__main__":
    unittest.main()
