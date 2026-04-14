import os
import tempfile
import unittest

from storage.database import Database


class CommentParentReconcileTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database({"sqlite_path": self.db_path})
        self.db.upsert_stock("SZ000733", "振华科技")
        self.db.conn.execute(
            "INSERT INTO posts(id, symbol, user_id, user_name, created_at) VALUES(?,?,?,?,?)",
            ("post_a", "SZ000733", "u1", "root_author", 1000),
        )
        self.db.conn.execute(
            "INSERT INTO posts(id, symbol, user_id, user_name, created_at) VALUES(?,?,?,?,?)",
            ("post_b", "SZ000733", "u2", "child_author", 2000),
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_reconcile_marks_same_post_cross_post_and_missing(self):
        self.db.save_comment(
            "post_a",
            {
                "id": "root_1",
                "user_id": "u1",
                "user_name": "root",
                "text_html": "root",
                "created_at": 1000,
                "parent_comment_id": "",
                "reply_comment_id": "",
                "depth": 1,
            },
        )
        self.db.link_comment_to_post("post_a", "root_1")
        self.db.save_comment(
            "post_b",
            {
                "id": "same_parent",
                "user_id": "u2",
                "user_name": "same_parent",
                "text_html": "same parent",
                "created_at": 2000,
                "parent_comment_id": "",
                "reply_comment_id": "",
                "depth": 1,
            },
        )
        self.db.link_comment_to_post("post_b", "same_parent")
        self.db.save_comment(
            "post_b",
            {
                "id": "same_child",
                "user_id": "u3",
                "user_name": "same_child",
                "text_html": "same child",
                "created_at": 2100,
                "parent_comment_id": "same_parent",
                "reply_comment_id": "same_parent",
                "depth": 2,
            },
        )
        self.db.link_comment_to_post("post_b", "same_child")
        self.db.save_comment(
            "post_b",
            {
                "id": "cross_child",
                "user_id": "u4",
                "user_name": "cross_child",
                "text_html": "cross child",
                "created_at": 2200,
                "parent_comment_id": "root_1",
                "reply_comment_id": "root_1",
                "depth": 2,
            },
        )
        self.db.link_comment_to_post("post_b", "cross_child")
        self.db.save_comment(
            "post_b",
            {
                "id": "missing_child",
                "user_id": "u5",
                "user_name": "missing_child",
                "text_html": "missing child",
                "created_at": 2300,
                "parent_comment_id": "ghost",
                "reply_comment_id": "ghost",
                "depth": 2,
            },
        )
        self.db.link_comment_to_post("post_b", "missing_child")

        self.db.reconcile_comment_parent_links(symbol="SZ000733")
        self.db.update_post_comments_scraped("post_a")
        self.db.update_post_comments_scraped("post_b")

        rows = {
            row["id"]: dict(row)
            for row in self.db.conn.execute(
                "SELECT id, canonical_post_id, parent_post_id, parent_scope FROM comments"
            ).fetchall()
        }
        self.assertEqual("root", rows["root_1"]["parent_scope"])
        self.assertEqual("same_post", rows["same_child"]["parent_scope"])
        self.assertEqual("post_b", rows["same_child"]["parent_post_id"])
        self.assertEqual("cross_post", rows["cross_child"]["parent_scope"])
        self.assertEqual("post_a", rows["cross_child"]["parent_post_id"])
        self.assertEqual("post_a", rows["cross_child"]["canonical_post_id"])
        self.assertEqual("missing", rows["missing_child"]["parent_scope"])

        report = self.db.get_stock_completeness_report(symbol="SZ000733")[0]
        self.assertEqual(1, report["orphan_comments"])
        self.assertEqual(0, report["cross_post_replies"])
        self.assertEqual(5, report["comments_scraped"])

    def test_completeness_and_backfill_are_thread_aware(self):
        self.db.conn.execute("UPDATE posts SET reply_count=5 WHERE id='post_a'")
        self.db.conn.execute("UPDATE posts SET reply_count=4 WHERE id='post_b'")
        self.db.conn.commit()

        self.db.save_comment(
            "post_a",
            {
                "id": "thread_root",
                "user_id": "u1",
                "user_name": "root",
                "text_html": "root",
                "created_at": 1000,
                "parent_comment_id": "",
                "reply_comment_id": "",
                "depth": 1,
                "status_id": "post_b",
                "root_status_id": "post_a",
            },
        )
        self.db.link_comment_to_post("post_a", "thread_root")

        self.db.save_comment(
            "post_a",
            {
                "id": "shared_reply",
                "user_id": "u2",
                "user_name": "shared",
                "text_html": "shared",
                "created_at": 1100,
                "parent_comment_id": "",
                "reply_comment_id": "",
                "depth": 1,
            },
        )
        self.db.link_comment_to_post("post_a", "shared_reply")
        self.db.link_comment_to_post("post_b", "shared_reply")

        self.db.save_comment(
            "post_b",
            {
                "id": "only_b",
                "user_id": "u3",
                "user_name": "only_b",
                "text_html": "only_b",
                "created_at": 1200,
                "parent_comment_id": "",
                "reply_comment_id": "",
                "depth": 1,
            },
        )
        self.db.link_comment_to_post("post_b", "only_b")

        self.db.update_post_comments_scraped("post_a")
        self.db.update_post_comments_scraped("post_b")

        thread_root = self.db.conn.execute(
            "SELECT thread_root_post_id FROM posts WHERE id='post_b'"
        ).fetchone()
        self.assertEqual("post_a", thread_root["thread_root_post_id"])

        report = self.db.get_stock_completeness_report(symbol="SZ000733")[0]
        self.assertEqual(2, report["total_posts"])
        self.assertEqual(1, report["total_threads"])
        self.assertEqual(5, report["claimed_comments"])
        self.assertEqual(3, report["comments_scraped"])
        self.assertEqual(2, report["missing_comments"])

        memberships = self.db.conn.execute(
            """
            SELECT post_id
            FROM comment_memberships
            WHERE comment_id='shared_reply'
            ORDER BY post_id
            """
        ).fetchall()
        self.assertEqual(["post_a"], [row["post_id"] for row in memberships])

        targets = self.db.get_posts_needing_backfill(symbol="SZ000733", days=None)
        self.assertEqual(1, len(targets))
        self.assertEqual("post_a", targets[0]["id"])
        self.assertEqual("post_a", targets[0]["thread_id"])
        self.assertEqual(2, targets[0]["gap"])


if __name__ == "__main__":
    unittest.main()
