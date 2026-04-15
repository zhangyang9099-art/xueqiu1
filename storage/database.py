"""
数据库模块 v3 — Phase 5 升级

v3 变更:
  - 新字段: platform_id, fav_count, view_count, depth, parent_comment_id 等
  - 新表: user_profiles, trending_topics, schema_version
  - 视图 discussion_threads 升级含新字段
  - 用户画像自动提取+存储
  - 自动迁移兼容 v2 数据库
"""

import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

from utils.logger import get_logger

logger = get_logger()


class Database:
    """SQLite 数据库管理器 v3。"""

    def __init__(self, config: dict):
        db_path = config.get("sqlite_path", "data/xueqiu.db")
        self._log_lifecycle = bool(config.get("log_lifecycle", True))
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA synchronous=NORMAL")

        self._create_tables()
        self._migrate()
        if self._log_lifecycle:
            logger.info(f"数据库已连接: {db_path}")

    # ──────── 建表 ────────

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS watched_stocks (
                symbol TEXT PRIMARY KEY, name TEXT, sector TEXT,
                last_scrape_time INTEGER DEFAULT 0, oldest_post_time INTEGER DEFAULT 0,
                history_complete INTEGER DEFAULT 0, history_stagnant_runs INTEGER DEFAULT 0,
                history_cursor_page INTEGER DEFAULT 0,
                history_cursor_oldest_time INTEGER DEFAULT 0,
                history_cursor_updated_at INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS tracked_users (
                user_id TEXT PRIMARY KEY, screen_name TEXT,
                last_check_time INTEGER DEFAULT 0, oldest_status_time INTEGER DEFAULT 0,
                last_sync_time INTEGER DEFAULT 0,
                history_complete INTEGER DEFAULT 0, history_stagnant_runs INTEGER DEFAULT 0,
                history_cursor_page INTEGER DEFAULT 0,
                history_cursor_oldest_time INTEGER DEFAULT 0,
                history_cursor_updated_at INTEGER DEFAULT 0,
                runtime_mode TEXT DEFAULT '',
                runtime_state TEXT DEFAULT '',
                runtime_page INTEGER DEFAULT 0,
                runtime_chunk INTEGER DEFAULT 0,
                runtime_total_pages INTEGER DEFAULT 0,
                runtime_started_at INTEGER DEFAULT 0,
                runtime_updated_at INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                note TEXT DEFAULT '', credibility_score REAL,
                created_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY, platform_id TEXT DEFAULT 'xueqiu',
                symbol TEXT, user_id TEXT, user_name TEXT,
                title TEXT DEFAULT '', text_html TEXT DEFAULT '', text_plain TEXT DEFAULT '',
                description TEXT DEFAULT '', created_at INTEGER DEFAULT 0,
                created_at_str TEXT DEFAULT '', market_phase TEXT DEFAULT '',
                thread_root_post_id TEXT DEFAULT '',
                reply_count INTEGER DEFAULT 0, like_count INTEGER DEFAULT 0,
                retweet_count INTEGER DEFAULT 0, fav_count INTEGER DEFAULT 0,
                view_count INTEGER DEFAULT 0, comments_scraped INTEGER DEFAULT 0,
                max_comment_depth INTEGER DEFAULT 1,
                scraped_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY, post_id TEXT,
                platform_id TEXT DEFAULT 'xueqiu',
                user_id TEXT, user_name TEXT,
                text_html TEXT DEFAULT '', text_plain TEXT DEFAULT '',
                created_at INTEGER DEFAULT 0, created_at_str TEXT DEFAULT '',
                market_phase TEXT DEFAULT '',
                like_count INTEGER DEFAULT 0,
                reply_comment_id TEXT DEFAULT '',
                parent_comment_id TEXT, reply_to_user_id TEXT,
                reply_to_user_name TEXT, depth INTEGER DEFAULT 1,
                status_id TEXT DEFAULT '', root_status_id TEXT DEFAULT '',
                retweet_status_id TEXT DEFAULT '', comment_reply_count INTEGER DEFAULT 0,
                canonical_post_id TEXT DEFAULT '',
                parent_post_id TEXT DEFAULT '',
                parent_scope TEXT DEFAULT '',
                scraped_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS comment_memberships (
                post_id TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                relation_scope TEXT DEFAULT 'direct',
                linked_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (post_id, comment_id)
            );

            CREATE TABLE IF NOT EXISTS user_statuses (
                id TEXT PRIMARY KEY, platform_id TEXT DEFAULT 'xueqiu',
                user_id TEXT, user_name TEXT,
                text_html TEXT DEFAULT '', text_plain TEXT DEFAULT '',
                target_symbol TEXT DEFAULT '', target_name TEXT DEFAULT '',
                created_at INTEGER DEFAULT 0, created_at_str TEXT DEFAULT '',
                market_phase TEXT DEFAULT '',
                reply_count INTEGER DEFAULT 0, like_count INTEGER DEFAULT 0,
                retweet_status_id TEXT DEFAULT '',
                parent_status_id TEXT DEFAULT '',
                is_original_post INTEGER DEFAULT 1,
                scraped_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT NOT NULL, platform_id TEXT NOT NULL DEFAULT 'xueqiu',
                screen_name TEXT, profile_image_url TEXT,
                is_default_name INTEGER DEFAULT 0, is_default_avatar INTEGER DEFAULT 0,
                followers_count INTEGER, following_count INTEGER, status_count INTEGER,
                created_at TEXT, verified_type TEXT, description TEXT,
                first_seen_at TEXT, last_updated_at TEXT,
                PRIMARY KEY (user_id, platform_id));

            CREATE TABLE IF NOT EXISTS trending_topics (
                id TEXT NOT NULL, platform_id TEXT NOT NULL DEFAULT 'xueqiu',
                title TEXT, url TEXT, discuss_count INTEGER, followers_count INTEGER,
                rank INTEGER, associated_stocks TEXT,
                captured_at TEXT, captured_date TEXT,
                PRIMARY KEY (id, platform_id, captured_date));

            CREATE TABLE IF NOT EXISTS scrape_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT, target TEXT, status TEXT,
                new_items_count INTEGER DEFAULT 0, duration_seconds REAL,
                error_message TEXT DEFAULT '',
                started_at TEXT, finished_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY, migrated_at TEXT, description TEXT);

            CREATE INDEX IF NOT EXISTS idx_posts_symbol ON posts(symbol);
            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform_id);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
            CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_comment_id);
            CREATE INDEX IF NOT EXISTS idx_comments_depth ON comments(post_id, depth);
            CREATE INDEX IF NOT EXISTS idx_comment_memberships_post ON comment_memberships(post_id);
            CREATE INDEX IF NOT EXISTS idx_comment_memberships_comment ON comment_memberships(comment_id);
            CREATE INDEX IF NOT EXISTS idx_user_statuses_user ON user_statuses(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_statuses_created ON user_statuses(created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_symbol_created ON posts(symbol, created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_user_created ON posts(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_user_gap_created ON posts(user_id, reply_count, comments_scraped, created_at);
            CREATE INDEX IF NOT EXISTS idx_comments_canonical_created ON comments(canonical_post_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_user_statuses_user_created_id ON user_statuses(user_id, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_trending_date ON trending_topics(captured_date, rank);
            CREATE INDEX IF NOT EXISTS idx_user_profiles_name ON user_profiles(screen_name);
        """)

        # 视图
        self.conn.execute("DROP VIEW IF EXISTS discussion_threads")
        self.conn.execute("""CREATE VIEW discussion_threads AS
            SELECT p.id AS thread_id, p.platform_id, p.symbol,
                p.user_id AS author_id, p.user_name AS author, p.title,
                COALESCE(p.text_plain, p.description, '') AS content,
                p.created_at_str AS start_time, p.market_phase,
                p.reply_count AS claimed_comments, p.comments_scraped AS actual_comments,
                p.like_count, p.fav_count, p.view_count, p.retweet_count,
                p.max_comment_depth,
                (SELECT COUNT(DISTINCT c.user_id)
                   FROM comment_memberships m
                   JOIN comments c ON c.id = m.comment_id
                  WHERE m.post_id=p.id) AS participants,
                (SELECT MAX(c.created_at_str)
                   FROM comment_memberships m
                   JOIN comments c ON c.id = m.comment_id
                  WHERE m.post_id=p.id) AS last_comment_time
            FROM posts p""")
        self.conn.commit()

    # ──────── 自动迁移 ────────

    def _migrate(self):
        migrations = [
            ("posts","created_at_str","TEXT DEFAULT ''"),
            ("posts","market_phase","TEXT DEFAULT ''"),
            ("posts","comments_scraped","INTEGER DEFAULT 0"),
            ("posts","platform_id","TEXT DEFAULT 'xueqiu'"),
            ("posts","fav_count","INTEGER DEFAULT 0"),
            ("posts","view_count","INTEGER DEFAULT 0"),
            ("posts","max_comment_depth","INTEGER DEFAULT 1"),
            ("posts","thread_root_post_id","TEXT DEFAULT ''"),
            ("comments","created_at_str","TEXT DEFAULT ''"),
            ("comments","platform_id","TEXT DEFAULT 'xueqiu'"),
            ("comments","market_phase","TEXT DEFAULT ''"),
            ("comments","like_count","INTEGER DEFAULT 0"),
            ("comments","parent_comment_id","TEXT"),
            ("comments","reply_to_user_id","TEXT"),
            ("comments","reply_to_user_name","TEXT"),
            ("comments","depth","INTEGER DEFAULT 1"),
            ("comments","status_id","TEXT DEFAULT ''"),
            ("comments","root_status_id","TEXT DEFAULT ''"),
            ("comments","retweet_status_id","TEXT DEFAULT ''"),
            ("comments","comment_reply_count","INTEGER DEFAULT 0"),
            ("comments","canonical_post_id","TEXT DEFAULT ''"),
            ("comments","parent_post_id","TEXT DEFAULT ''"),
            ("comments","parent_scope","TEXT DEFAULT ''"),
            ("user_statuses","created_at_str","TEXT DEFAULT ''"),
            ("user_statuses","market_phase","TEXT DEFAULT ''"),
            ("user_statuses","platform_id","TEXT DEFAULT 'xueqiu'"),
            ("watched_stocks","sector","TEXT"),
            ("watched_stocks","oldest_post_time","INTEGER DEFAULT 0"),
            ("watched_stocks","history_complete","INTEGER DEFAULT 0"),
            ("watched_stocks","history_stagnant_runs","INTEGER DEFAULT 0"),
            ("watched_stocks","history_cursor_page","INTEGER DEFAULT 0"),
            ("watched_stocks","history_cursor_oldest_time","INTEGER DEFAULT 0"),
            ("watched_stocks","history_cursor_updated_at","INTEGER DEFAULT 0"),
            ("tracked_users","credibility_score","REAL"),
            ("tracked_users","oldest_status_time","INTEGER DEFAULT 0"),
            ("tracked_users","history_complete","INTEGER DEFAULT 0"),
            ("tracked_users","history_stagnant_runs","INTEGER DEFAULT 0"),
            ("tracked_users","history_cursor_page","INTEGER DEFAULT 0"),
            ("tracked_users","history_cursor_oldest_time","INTEGER DEFAULT 0"),
            ("tracked_users","history_cursor_updated_at","INTEGER DEFAULT 0"),
            ("tracked_users","last_sync_time","INTEGER DEFAULT 0"),
            ("tracked_users","runtime_mode","TEXT DEFAULT ''"),
            ("tracked_users","runtime_state","TEXT DEFAULT ''"),
            ("tracked_users","runtime_page","INTEGER DEFAULT 0"),
            ("tracked_users","runtime_chunk","INTEGER DEFAULT 0"),
            ("tracked_users","runtime_total_pages","INTEGER DEFAULT 0"),
            ("tracked_users","runtime_started_at","INTEGER DEFAULT 0"),
            ("tracked_users","runtime_updated_at","INTEGER DEFAULT 0"),
            ("user_statuses","retweet_status_id","TEXT DEFAULT ''"),
            ("user_statuses","parent_status_id","TEXT DEFAULT ''"),
            ("user_statuses","is_original_post","INTEGER DEFAULT 1"),
            ("scrape_logs","duration_seconds","REAL"),
        ]
        for table, column, col_type in migrations:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                logger.info(f"迁移: {table}.{column}")
            except sqlite3.OperationalError:
                pass
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS comment_memberships (
                post_id TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                relation_scope TEXT DEFAULT 'direct',
                linked_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (post_id, comment_id)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_memberships_post ON comment_memberships(post_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_comment_memberships_comment ON comment_memberships(comment_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_thread_root ON posts(thread_root_post_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_status_root ON comments(root_status_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_status_id ON comments(status_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_canonical_post ON comments(canonical_post_id)")
        self._backfill_time_fields()
        self.conn.commit()

    def _backfill_time_fields(self):
        from utils.time_utils import ms_to_str, get_market_phase
        for row in self.conn.execute("SELECT id, created_at FROM posts WHERE created_at_str='' AND created_at>0").fetchall():
            self.conn.execute("UPDATE posts SET created_at_str=?, market_phase=? WHERE id=?",
                              (ms_to_str(row["created_at"]), get_market_phase(row["created_at"]), row["id"]))
        self.conn.execute("""
            INSERT OR IGNORE INTO comment_memberships(post_id, comment_id, relation_scope)
            SELECT post_id, id, 'legacy'
            FROM comments
            WHERE COALESCE(post_id, '') != ''
        """)
        self.conn.execute("""
            UPDATE comments
            SET canonical_post_id = COALESCE(NULLIF(canonical_post_id, ''), post_id, '')
            WHERE COALESCE(canonical_post_id, '') = ''
        """)
        self.conn.execute("""
            UPDATE posts
            SET comments_scraped=(
                SELECT COUNT(*)
                FROM comment_memberships m
                WHERE m.post_id=posts.id
            )
        """)
        for row in self.conn.execute("SELECT id, created_at FROM comments WHERE created_at_str='' AND created_at>0").fetchall():
            self.conn.execute("UPDATE comments SET created_at_str=? WHERE id=?", (ms_to_str(row["created_at"]), row["id"]))
        self.reconcile_comment_parent_links()
        needs_canonical_repair = self.conn.execute(
            "SELECT 1 FROM comments WHERE COALESCE(canonical_post_id, '') = '' LIMIT 1"
        ).fetchone()
        needs_membership_repair = self.conn.execute(
            """
            SELECT 1
            FROM comment_memberships
            GROUP BY comment_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if needs_canonical_repair or needs_membership_repair:
            self.reconcile_comment_canonical_posts(commit=False)
            self.refresh_post_comment_stats(commit=False)
        for row in self.conn.execute("SELECT id, created_at FROM user_statuses WHERE created_at_str='' AND created_at>0").fetchall():
            self.conn.execute("UPDATE user_statuses SET created_at_str=?, market_phase=? WHERE id=?",
                              (ms_to_str(row["created_at"]), get_market_phase(row["created_at"]), row["id"]))

        self.conn.execute("""
            UPDATE watched_stocks
            SET last_scrape_time = COALESCE(
                    NULLIF(last_scrape_time, 0),
                    (SELECT MAX(created_at) FROM posts WHERE posts.symbol = watched_stocks.symbol),
                    0
                ),
                oldest_post_time = COALESCE(
                    NULLIF(oldest_post_time, 0),
                    (SELECT MIN(created_at) FROM posts WHERE posts.symbol = watched_stocks.symbol),
                    0
                )
        """)
        self.conn.execute("""
            UPDATE tracked_users
            SET last_check_time = COALESCE(
                    NULLIF(last_check_time, 0),
                    (SELECT MAX(created_at) FROM user_statuses WHERE user_statuses.user_id = tracked_users.user_id),
                    0
                ),
                screen_name = COALESCE(
                    NULLIF(screen_name, ''),
                    (SELECT user_name FROM user_statuses
                     WHERE user_statuses.user_id = tracked_users.user_id
                       AND COALESCE(user_name, '') != ''
                     ORDER BY created_at DESC LIMIT 1),
                    screen_name
                ),
                oldest_status_time = COALESCE(
                    NULLIF(oldest_status_time, 0),
                    (SELECT MIN(created_at) FROM user_statuses WHERE user_statuses.user_id = tracked_users.user_id),
                    0
                ),
                last_sync_time = COALESCE(
                    NULLIF(last_sync_time, 0),
                    NULLIF(last_check_time, 0),
                    0
                )
        """)

    # ──────── 股票管理 ────────

    def upsert_stock(self, symbol, name, sector=None):
        if sector:
            self.conn.execute(
                """INSERT INTO watched_stocks(symbol,name,sector) VALUES(?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                   name=CASE WHEN excluded.name!='' THEN excluded.name ELSE watched_stocks.name END,
                   sector=COALESCE(excluded.sector, watched_stocks.sector)""",
                (symbol, name, sector),
            )
        else:
            self.conn.execute(
                """INSERT INTO watched_stocks(symbol,name) VALUES(?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                   name=CASE WHEN excluded.name!='' THEN excluded.name ELSE watched_stocks.name END""",
                (symbol, name),
            )
        self.conn.commit()

    def get_watched_stocks(self, active_only=True):
        sql = "SELECT * FROM watched_stocks" + (" WHERE is_active=1" if active_only else "")
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def search_local_stocks(self, query, limit=10):
        q = (query or "").strip()
        if not q:
            return []
        upper = q.upper()
        rows = self.conn.execute(
            """
            SELECT symbol, name,
                   CASE
                       WHEN symbol = ? THEN 0
                       WHEN UPPER(name) = ? THEN 1
                       WHEN symbol LIKE ? THEN 2
                       WHEN name LIKE ? THEN 3
                       ELSE 4
                   END AS rank
            FROM watched_stocks
            WHERE is_active=1
              AND (
                    symbol = ?
                 OR UPPER(name) = ?
                 OR symbol LIKE ?
                 OR name LIKE ?
              )
            ORDER BY rank, symbol
            LIMIT ?
            """,
            (
                upper, upper,
                f"%{upper}%", f"%{q}%",
                upper, upper,
                f"%{upper}%", f"%{q}%",
                limit,
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stock_time_windows(self, symbols=None, active_only=True):
        params = []
        sql = """
            SELECT w.symbol, w.name, w.history_complete, w.history_stagnant_runs,
                   MIN(p.created_at) AS first_post_time,
                   MAX(p.created_at) AS latest_post_time,
                   COUNT(DISTINCT p.id) AS total_posts,
                   COALESCE(SUM(p.comments_scraped), 0) AS total_comments
            FROM watched_stocks w
            LEFT JOIN posts p ON p.symbol = w.symbol
        """
        conditions = []
        if active_only:
            conditions.append("w.is_active=1")
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            conditions.append(f"w.symbol IN ({placeholders})")
            params.extend(symbols)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += """
            GROUP BY w.symbol, w.name, w.history_complete, w.history_stagnant_runs
            ORDER BY w.symbol
        """
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_stock_last_scrape_time(self, symbol):
        r = self.conn.execute("SELECT last_scrape_time FROM watched_stocks WHERE symbol=?", (symbol,)).fetchone()
        return r["last_scrape_time"] if r else 0

    def update_stock_scrape_time(self, symbol, timestamp):
        self.conn.execute("UPDATE watched_stocks SET last_scrape_time=? WHERE symbol=?", (timestamp, symbol))
        self.conn.commit()

    def get_stock_oldest_post_time(self, symbol):
        r = self.conn.execute("SELECT oldest_post_time FROM watched_stocks WHERE symbol=?", (symbol,)).fetchone()
        return r["oldest_post_time"] if r else 0

    def update_stock_oldest_post_time(self, symbol, timestamp):
        self.conn.execute(
            """UPDATE watched_stocks
               SET oldest_post_time = CASE
                   WHEN oldest_post_time IS NULL OR oldest_post_time = 0 THEN ?
                   ELSE MIN(oldest_post_time, ?)
               END
               WHERE symbol=?""",
            (timestamp, timestamp, symbol),
        )
        self.conn.commit()

    def mark_stock_history_complete(self, symbol, complete=True):
        self.conn.execute(
            "UPDATE watched_stocks SET history_complete=? WHERE symbol=?",
            (1 if complete else 0, symbol),
        )
        self.conn.commit()

    def get_stock_history_stagnant_runs(self, symbol):
        r = self.conn.execute(
            "SELECT history_stagnant_runs FROM watched_stocks WHERE symbol=?",
            (symbol,),
        ).fetchone()
        return r["history_stagnant_runs"] if r else 0

    def set_stock_history_stagnant_runs(self, symbol, runs):
        self.conn.execute(
            "UPDATE watched_stocks SET history_stagnant_runs=? WHERE symbol=?",
            (max(0, int(runs)), symbol),
        )
        self.conn.commit()

    def get_stock_history_cursor(self, symbol):
        row = self.conn.execute(
            """SELECT history_cursor_page, history_cursor_oldest_time, history_cursor_updated_at
               FROM watched_stocks WHERE symbol=?""",
            (symbol,),
        ).fetchone()
        if not row:
            return {"page": 0, "oldest_time": 0, "updated_at": 0}
        return {
            "page": row["history_cursor_page"] or 0,
            "oldest_time": row["history_cursor_oldest_time"] or 0,
            "updated_at": row["history_cursor_updated_at"] or 0,
        }

    def update_stock_history_cursor(self, symbol, page, oldest_time=0):
        updated_at = int(time.time() * 1000)
        self.conn.execute(
            """UPDATE watched_stocks
               SET history_cursor_page=?,
                   history_cursor_oldest_time=?,
                   history_cursor_updated_at=?
               WHERE symbol=?""",
            (max(0, int(page)), max(0, int(oldest_time or 0)), updated_at, symbol),
        )
        self.conn.commit()

    def clear_stock_history_cursor(self, symbol):
        self.conn.execute(
            """UPDATE watched_stocks
               SET history_cursor_page=0,
                   history_cursor_oldest_time=0,
                   history_cursor_updated_at=0
               WHERE symbol=?""",
            (symbol,),
        )
        self.conn.commit()

    # ──────── 用户管理 ────────

    def upsert_tracked_user(self, user_id, screen_name, note=""):
        self.conn.execute(
            """INSERT INTO tracked_users(user_id,screen_name,note) VALUES(?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
               screen_name=CASE WHEN excluded.screen_name!='' THEN excluded.screen_name ELSE tracked_users.screen_name END,
               note=CASE WHEN excluded.note!='' THEN excluded.note ELSE tracked_users.note END""",
            (user_id, screen_name, note),
        )
        self.conn.commit()

    def get_tracked_users(self, active_only=True):
        sql = "SELECT * FROM tracked_users" + (" WHERE is_active=1" if active_only else "")
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def search_local_users(self, query, limit=10):
        q = str(query or "").strip()
        if not q:
            return []
        upper = q.upper()
        rows = self.conn.execute(
            """
            WITH local_users AS (
                SELECT user_id, screen_name, note, 1 AS priority
                FROM tracked_users
                WHERE is_active=1
                UNION ALL
                SELECT user_id, screen_name, '' AS note, 2 AS priority
                FROM user_profiles
            )
            SELECT user_id, screen_name, note, MIN(priority) AS priority
            FROM local_users
            WHERE user_id = ?
               OR UPPER(screen_name) = ?
               OR user_id LIKE ?
               OR screen_name LIKE ?
            GROUP BY user_id, screen_name, note
            ORDER BY priority, user_id
            LIMIT ?
            """,
            (q, upper, f"%{q}%", f"%{q}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_user_time_windows(self, user_ids=None, active_only=True):
        params = []
        sql = """
            SELECT t.user_id, t.screen_name, t.note, t.history_complete, t.history_stagnant_runs,
                   t.last_sync_time,
                   t.history_cursor_page, t.history_cursor_oldest_time, t.history_cursor_updated_at,
                   t.runtime_mode, t.runtime_state, t.runtime_page, t.runtime_chunk,
                   t.runtime_total_pages, t.runtime_started_at, t.runtime_updated_at,
                   MIN(u.created_at) AS first_status_time,
                   MAX(u.created_at) AS latest_status_time,
                   COUNT(u.id) AS total_statuses
            FROM tracked_users t
            LEFT JOIN user_statuses u ON u.user_id = t.user_id
        """
        conditions = []
        if active_only:
            conditions.append("t.is_active=1")
        if user_ids:
            placeholders = ",".join("?" for _ in user_ids)
            conditions.append(f"t.user_id IN ({placeholders})")
            params.extend(user_ids)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += """
            GROUP BY t.user_id, t.screen_name, t.note, t.history_complete, t.history_stagnant_runs,
                     t.last_sync_time, t.history_cursor_page, t.history_cursor_oldest_time, t.history_cursor_updated_at,
                     t.runtime_mode, t.runtime_state, t.runtime_page, t.runtime_chunk,
                     t.runtime_total_pages, t.runtime_started_at, t.runtime_updated_at
            ORDER BY t.user_id
        """
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_user_last_check_time(self, user_id):
        r = self.conn.execute("SELECT last_check_time FROM tracked_users WHERE user_id=?", (user_id,)).fetchone()
        return r["last_check_time"] if r else 0

    def update_user_check_time(self, user_id, timestamp):
        self.conn.execute("UPDATE tracked_users SET last_check_time=? WHERE user_id=?", (timestamp, user_id))
        self.conn.commit()

    def get_user_last_sync_time(self, user_id):
        r = self.conn.execute("SELECT last_sync_time FROM tracked_users WHERE user_id=?", (user_id,)).fetchone()
        return r["last_sync_time"] if r else 0

    def update_user_last_sync_time(self, user_id, timestamp):
        self.conn.execute("UPDATE tracked_users SET last_sync_time=? WHERE user_id=?", (timestamp, user_id))
        self.conn.commit()

    def get_user_oldest_status_time(self, user_id):
        r = self.conn.execute("SELECT oldest_status_time FROM tracked_users WHERE user_id=?", (user_id,)).fetchone()
        return r["oldest_status_time"] if r else 0

    def update_user_oldest_status_time(self, user_id, timestamp):
        self.conn.execute(
            """UPDATE tracked_users
               SET oldest_status_time = CASE
                   WHEN oldest_status_time IS NULL OR oldest_status_time = 0 THEN ?
                   ELSE MIN(oldest_status_time, ?)
               END
               WHERE user_id=?""",
            (timestamp, timestamp, user_id),
        )
        self.conn.commit()

    def mark_user_history_complete(self, user_id, complete=True):
        self.conn.execute(
            "UPDATE tracked_users SET history_complete=? WHERE user_id=?",
            (1 if complete else 0, user_id),
        )
        self.conn.commit()

    def get_user_history_stagnant_runs(self, user_id):
        r = self.conn.execute(
            "SELECT history_stagnant_runs FROM tracked_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return r["history_stagnant_runs"] if r else 0

    def set_user_history_stagnant_runs(self, user_id, runs):
        self.conn.execute(
            "UPDATE tracked_users SET history_stagnant_runs=? WHERE user_id=?",
            (max(0, int(runs)), user_id),
        )
        self.conn.commit()

    def get_user_history_cursor(self, user_id):
        row = self.conn.execute(
            """SELECT history_cursor_page, history_cursor_oldest_time, history_cursor_updated_at
               FROM tracked_users WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        if not row:
            return {"page": 0, "oldest_time": 0, "updated_at": 0}
        return {
            "page": row["history_cursor_page"] or 0,
            "oldest_time": row["history_cursor_oldest_time"] or 0,
            "updated_at": row["history_cursor_updated_at"] or 0,
        }

    def update_user_history_cursor(self, user_id, page, oldest_time=0):
        updated_at = int(time.time() * 1000)
        self.conn.execute(
            """UPDATE tracked_users
               SET history_cursor_page=?,
                   history_cursor_oldest_time=?,
                   history_cursor_updated_at=?
               WHERE user_id=?""",
            (max(0, int(page)), max(0, int(oldest_time or 0)), updated_at, user_id),
        )
        self.conn.commit()

    def clear_user_history_cursor(self, user_id):
        self.conn.execute(
            """UPDATE tracked_users
               SET history_cursor_page=0,
                   history_cursor_oldest_time=0,
                   history_cursor_updated_at=0
               WHERE user_id=?""",
            (user_id,),
        )
        self.conn.commit()

    def update_user_runtime_progress(
        self,
        user_id,
        *,
        mode="",
        state="running",
        page=0,
        chunk=0,
        total_pages=0,
        started_at=None,
    ):
        now_ms = int(time.time() * 1000)
        started_at = int(started_at or now_ms)
        self.conn.execute(
            """UPDATE tracked_users
               SET runtime_mode=?,
                   runtime_state=?,
                   runtime_page=?,
                   runtime_chunk=?,
                   runtime_total_pages=?,
                   runtime_started_at=?,
                   runtime_updated_at=?
               WHERE user_id=?""",
            (
                str(mode or ""),
                str(state or "running"),
                max(0, int(page or 0)),
                max(0, int(chunk or 0)),
                max(0, int(total_pages or 0)),
                started_at,
                now_ms,
                user_id,
            ),
        )
        self.conn.commit()

    def clear_user_runtime_progress(self, user_id):
        self.conn.execute(
            """UPDATE tracked_users
               SET runtime_mode='',
                   runtime_state='',
                   runtime_page=0,
                   runtime_chunk=0,
                   runtime_total_pages=0,
                   runtime_started_at=0,
                   runtime_updated_at=0
               WHERE user_id=?""",
            (user_id,),
        )
        self.conn.commit()

    def user_status_exists(self, status_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM user_statuses WHERE id=? LIMIT 1", (status_id,)).fetchone()
        return bool(row)

    # ──────── 用户画像 ────────

    def upsert_user_profile(self, profile: dict, commit=True):
        """从帖子/评论中提取的用户信息自动存入 user_profiles。"""
        uid = str(profile.get("user_id", ""))
        if not uid:
            return
        pid = profile.get("platform_id", "xueqiu")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        name = profile.get("screen_name", "")

        # 判断默认昵称
        is_default = 1 if re.match(r'^(雪球用户\d+|球友\w{4,}|用户\d+)$', name) else 0
        # 判断默认头像
        img = profile.get("profile_image_url", "") or ""
        is_default_avatar = 1 if ("default_avatar" in img or "default-avatar" in img or not img) else 0

        self.conn.execute("""INSERT INTO user_profiles
            (user_id, platform_id, screen_name, profile_image_url,
             is_default_name, is_default_avatar, followers_count, following_count,
             status_count, verified_type, description, first_seen_at, last_updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, platform_id) DO UPDATE SET
             screen_name=excluded.screen_name, profile_image_url=excluded.profile_image_url,
             is_default_name=excluded.is_default_name, is_default_avatar=excluded.is_default_avatar,
             followers_count=COALESCE(excluded.followers_count, user_profiles.followers_count),
             following_count=COALESCE(excluded.following_count, user_profiles.following_count),
             status_count=COALESCE(excluded.status_count, user_profiles.status_count),
             verified_type=COALESCE(excluded.verified_type, user_profiles.verified_type),
             description=COALESCE(excluded.description, user_profiles.description),
             last_updated_at=excluded.last_updated_at""",
            (uid, pid, name, img, is_default, is_default_avatar,
             profile.get("followers_count"), profile.get("following_count"),
             profile.get("status_count"), profile.get("verified_type"),
             profile.get("description"), now, now))
        if commit:
            self.conn.commit()

    # ──────── 帖子写入 ────────

    def save_post(self, symbol, post) -> bool:
        from utils.time_utils import ms_to_str, get_market_phase
        ca = post["created_at"]
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO posts
               (id, platform_id, symbol, user_id, user_name, title, text_html, text_plain,
                description, created_at, reply_count, like_count, retweet_count,
                fav_count, view_count, created_at_str, market_phase)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (post["id"], post.get("platform_id","xueqiu"), symbol,
             post["user_id"], post["user_name"], post["title"],
             post["text_html"], post.get("text_plain",""), post["description"],
             ca, post["reply_count"], post["like_count"], post["retweet_count"],
             post.get("fav_count",0), post.get("view_count",0),
             ms_to_str(ca), get_market_phase(ca)))
        if cur.rowcount == 0:
            self.conn.execute(
                """UPDATE posts SET
                   user_id=?, user_name=?, title=?, text_html=?, text_plain=?,
                   description=?, created_at=?, reply_count=?, like_count=?,
                   retweet_count=?, fav_count=?, view_count=?,
                   created_at_str=?, market_phase=?
                   WHERE id=?""",
                (post["user_id"], post["user_name"], post["title"],
                 post["text_html"], post.get("text_plain", ""), post["description"],
                 ca, post["reply_count"], post["like_count"], post["retweet_count"],
                 post.get("fav_count", 0), post.get("view_count", 0),
                 ms_to_str(ca), get_market_phase(ca), post["id"]))
        self.conn.commit()
        return cur.rowcount > 0

    def save_posts_batch(self, symbol, posts):
        return sum(1 for p in posts if self.save_post(symbol, p))

    # ──────── 评论写入 ────────

    def save_comment(self, post_id, comment, commit=True) -> bool:
        from utils.time_utils import ms_to_str, get_market_phase
        ca = comment["created_at"]
        parent = comment.get("parent_comment_id") or comment.get("reply_comment_id") or ""
        depth = comment.get("depth", 2 if parent else 1)
        status_id = str(comment.get("status_id", "") or "")
        root_status_id = str(comment.get("root_status_id", "") or "")
        retweet_status_id = str(comment.get("retweet_status_id", "") or "")
        comment_reply_count = int(comment.get("comment_reply_count", 0) or 0)
        canonical_post_id = comment.get("canonical_post_id", "") or post_id
        parent_post_id = comment.get("parent_post_id", "") or ""
        parent_scope = comment.get("parent_scope", "") or ("root" if not parent else "")
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO comments
               (id, post_id, platform_id, user_id, user_name, text_html, text_plain,
                created_at, created_at_str, market_phase, like_count,
                reply_comment_id, parent_comment_id,
                reply_to_user_id, reply_to_user_name, depth,
                status_id, root_status_id, retweet_status_id, comment_reply_count,
                canonical_post_id, parent_post_id, parent_scope)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (comment["id"], post_id, comment.get("platform_id","xueqiu"),
             comment["user_id"], comment["user_name"],
             comment["text_html"], comment.get("text_plain",""),
             ca, ms_to_str(ca), get_market_phase(ca),
             comment.get("like_count",0),
             comment.get("reply_comment_id",""), parent,
             comment.get("reply_to_user_id",""), comment.get("reply_to_user_name",""),
             depth, status_id, root_status_id, retweet_status_id, comment_reply_count,
             canonical_post_id, parent_post_id, parent_scope))
        if cur.rowcount == 0:
            self.conn.execute(
                """UPDATE comments SET
                   user_id=CASE WHEN ?!='' THEN ? ELSE user_id END,
                   user_name=CASE WHEN ?!='' THEN ? ELSE user_name END,
                   text_html=CASE WHEN ?!='' THEN ? ELSE text_html END,
                   text_plain=CASE WHEN ?!='' THEN ? ELSE text_plain END,
                   created_at=CASE WHEN ?>0 THEN ? ELSE created_at END,
                   created_at_str=CASE WHEN ?!='' THEN ? ELSE created_at_str END,
                   market_phase=CASE WHEN ?!='' THEN ? ELSE market_phase END,
                   like_count=MAX(like_count, ?),
                   reply_comment_id=CASE WHEN ?!='' THEN ? ELSE reply_comment_id END,
                   parent_comment_id=CASE WHEN ?!='' THEN ? ELSE parent_comment_id END,
                   reply_to_user_id=CASE WHEN ?!='' THEN ? ELSE reply_to_user_id END,
                   reply_to_user_name=CASE WHEN ?!='' THEN ? ELSE reply_to_user_name END,
                   depth=MAX(depth, ?),
                   status_id=CASE WHEN ?!='' THEN ? ELSE status_id END,
                   root_status_id=CASE WHEN ?!='' THEN ? ELSE root_status_id END,
                   retweet_status_id=CASE WHEN ?!='' THEN ? ELSE retweet_status_id END,
                   comment_reply_count=MAX(comment_reply_count, ?),
                   canonical_post_id=CASE WHEN ?!='' THEN ? ELSE canonical_post_id END,
                   parent_post_id=CASE WHEN ?!='' THEN ? ELSE parent_post_id END,
                   parent_scope=CASE WHEN ?!='' THEN ? ELSE parent_scope END
                   WHERE id=?""",
                (comment["user_id"], comment["user_id"],
                 comment["user_name"], comment["user_name"],
                 comment["text_html"], comment["text_html"],
                 comment.get("text_plain", ""), comment.get("text_plain", ""),
                 ca, ca,
                 ms_to_str(ca), ms_to_str(ca),
                 get_market_phase(ca), get_market_phase(ca),
                 comment.get("like_count", 0),
                 comment.get("reply_comment_id", ""), comment.get("reply_comment_id", ""),
                 parent, parent,
                 comment.get("reply_to_user_id", ""), comment.get("reply_to_user_id", ""),
                 comment.get("reply_to_user_name", ""), comment.get("reply_to_user_name", ""),
                 depth,
                 status_id, status_id,
                 root_status_id, root_status_id,
                 retweet_status_id, retweet_status_id,
                 comment_reply_count,
                 canonical_post_id, canonical_post_id,
                 parent_post_id, parent_post_id,
                 parent_scope, parent_scope,
                 comment["id"]))
        if commit:
            self.conn.commit()
        return cur.rowcount > 0

    def link_comment_to_post(self, post_id, comment_id, relation_scope="direct", commit=True):
        self.conn.execute(
            """INSERT INTO comment_memberships(post_id, comment_id, relation_scope)
               VALUES(?,?,?)
               ON CONFLICT(post_id, comment_id) DO UPDATE SET
               relation_scope=CASE
                   WHEN comment_memberships.relation_scope='direct' THEN comment_memberships.relation_scope
                   ELSE excluded.relation_scope
               END,
               linked_at=datetime('now')""",
            (post_id, comment_id, relation_scope),
        )
        if commit:
            self.conn.commit()

    def clear_post_comment_memberships(self, post_id, commit=True):
        self.conn.execute("DELETE FROM comment_memberships WHERE post_id=?", (post_id,))
        if commit:
            self.conn.commit()

    def save_comments_batch(self, post_id, comments):
        return sum(1 for c in comments if self.save_comment(post_id, c))

    # ──────── 评论回填 ────────

    def update_post_comments_scraped(self, post_id, commit=True):
        row = self.conn.execute("SELECT symbol FROM posts WHERE id=?", (post_id,)).fetchone()
        symbol = str(row["symbol"] or "").strip() if row else ""
        if symbol:
            self.reconcile_comment_parent_links(symbol=symbol, commit=False)
            self.reconcile_comment_canonical_posts(symbol=symbol, commit=False)
            self.reconcile_post_thread_links(symbol=symbol, commit=False)
            self.refresh_post_comment_stats(symbol=symbol, commit=False)
        else:
            self.reconcile_comment_parent_links(post_id=post_id, commit=False)
            self.reconcile_comment_canonical_posts(post_id=post_id, commit=False)
            self.reconcile_post_thread_links(post_id=post_id, commit=False)
            self.refresh_post_comment_stats(post_id=post_id, commit=False)
        if commit:
            self.conn.commit()

    def get_post_comment_progress(self, post_id):
        row = self.conn.execute(
            "SELECT reply_count, comments_scraped FROM posts WHERE id=?",
            (post_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "reply_count": row["reply_count"] or 0,
            "comments_scraped": row["comments_scraped"] or 0,
        }

    def get_user_comment_gap_posts(self, user_id, limit=50):
        rows = self.conn.execute(
            """
            SELECT id, symbol, user_name, created_at, reply_count, comments_scraped
            FROM posts
            WHERE user_id=?
              AND COALESCE(reply_count, 0) > COALESCE(comments_scraped, 0)
            ORDER BY created_at DESC,
                     (COALESCE(reply_count, 0) - COALESCE(comments_scraped, 0)) DESC,
                     id DESC
            LIMIT ?
            """,
            (str(user_id), max(1, int(limit or 50))),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_post(self, post_id):
        row = self.conn.execute(
            """SELECT id, symbol, user_name, reply_count, comments_scraped
               FROM posts WHERE id=?""",
            (post_id,),
        ).fetchone()
        return dict(row) if row else None

    def _resolve_known_thread_root(self, post_id: str) -> str:
        current = str(post_id or "").strip()
        if not current:
            return ""

        seen = set()
        while current and current not in seen:
            seen.add(current)
            row = self.conn.execute(
                "SELECT thread_root_post_id FROM posts WHERE id=?",
                (current,),
            ).fetchone()
            if not row:
                return current
            next_root = str(row["thread_root_post_id"] or "").strip()
            if not next_root or next_root == current:
                return current
            current = next_root
        return current

    def set_post_thread_root(self, post_id: str, root_post_id: str, commit=True):
        post_id = str(post_id or "").strip()
        if not post_id:
            return
        row = self.conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row:
            return
        canonical_root = self._resolve_known_thread_root(root_post_id or post_id) or post_id
        self.conn.execute(
            "UPDATE posts SET thread_root_post_id=? WHERE id=?",
            (canonical_root, post_id),
        )
        if commit:
            self.conn.commit()

    def _comment_scope_clause(self, post_id=None, symbol=None, alias="comments"):
        conditions = []
        params = []
        if post_id:
            conditions.append(f"{alias}.post_id=?")
            params.append(post_id)
        elif symbol:
            conditions.append(f"{alias}.post_id IN (SELECT id FROM posts WHERE symbol=?)")
            params.append(symbol)
        return conditions, params

    def _pick_existing_post_id(self, *candidates):
        checked = set()
        for candidate in candidates:
            value = str(candidate or "").strip()
            if not value or value in checked:
                continue
            checked.add(value)
            row = self.conn.execute("SELECT 1 FROM posts WHERE id=?", (value,)).fetchone()
            if row:
                return value
        return ""

    def reconcile_comment_canonical_posts(self, post_id=None, symbol=None, commit=True):
        conditions, params = self._comment_scope_clause(post_id=post_id, symbol=symbol, alias="c")
        sql = """
            SELECT c.id, c.post_id, c.parent_comment_id, c.parent_post_id,
                   c.parent_scope, c.root_status_id, c.canonical_post_id
            FROM comments c
        """
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        scoped_rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        if not scoped_rows:
            return

        row_cache = {row["id"]: row for row in scoped_rows}
        owner_cache = {}

        def fetch_row(comment_id: str):
            cid = str(comment_id or "").strip()
            if not cid:
                return None
            if cid in row_cache:
                return row_cache[cid]
            fetched = self.conn.execute(
                """
                SELECT id, post_id, parent_comment_id, parent_post_id,
                       parent_scope, root_status_id, canonical_post_id
                FROM comments
                WHERE id=?
                """,
                (cid,),
            ).fetchone()
            if not fetched:
                return None
            row_cache[cid] = dict(fetched)
            return row_cache[cid]

        def resolve_owner(comment_id: str, trail=None):
            cid = str(comment_id or "").strip()
            if not cid:
                return ""
            if cid in owner_cache:
                return owner_cache[cid]
            if trail is None:
                trail = set()
            if cid in trail:
                row = fetch_row(cid) or {}
                owner = self._pick_existing_post_id(
                    row.get("canonical_post_id"),
                    row.get("parent_post_id"),
                    row.get("root_status_id"),
                    row.get("post_id"),
                )
                owner_cache[cid] = owner
                return owner

            row = fetch_row(cid)
            if not row:
                return ""

            trail = set(trail)
            trail.add(cid)
            owner = ""
            parent_id = str(row.get("parent_comment_id") or "").strip()
            if parent_id:
                owner = resolve_owner(parent_id, trail)

            if not owner:
                owner = self._pick_existing_post_id(
                    row.get("parent_post_id"),
                    row.get("root_status_id"),
                    row.get("canonical_post_id"),
                    row.get("post_id"),
                )
            owner_cache[cid] = owner
            return owner

        affected_post_ids = set()
        comment_ids = []
        for row in scoped_rows:
            comment_id = row["id"]
            comment_ids.append(comment_id)
            current_owner = str(row.get("canonical_post_id") or row.get("post_id") or "").strip()
            next_owner = resolve_owner(comment_id) or current_owner
            if current_owner:
                affected_post_ids.add(current_owner)
            if next_owner:
                affected_post_ids.add(next_owner)
            for membership in self.conn.execute(
                "SELECT post_id FROM comment_memberships WHERE comment_id=?",
                (comment_id,),
            ).fetchall():
                affected_post_ids.add(str(membership["post_id"] or "").strip())
            self.conn.execute(
                "UPDATE comments SET canonical_post_id=? WHERE id=?",
                (next_owner, comment_id),
            )

        placeholders = ",".join("?" for _ in comment_ids)
        self.conn.execute(
            f"DELETE FROM comment_memberships WHERE comment_id IN ({placeholders})",
            comment_ids,
        )
        for comment_id in comment_ids:
            owner = owner_cache.get(comment_id) or ""
            if not owner:
                row = fetch_row(comment_id) or {}
                owner = self._pick_existing_post_id(row.get("canonical_post_id"), row.get("post_id"))
            if not owner:
                continue
            self.conn.execute(
                """
                INSERT OR REPLACE INTO comment_memberships(post_id, comment_id, relation_scope, linked_at)
                VALUES(?, ?, 'direct', datetime('now'))
                """,
                (owner, comment_id),
            )

        if affected_post_ids:
            self.refresh_post_comment_stats(post_ids=sorted(pid for pid in affected_post_ids if pid), commit=False)
        if commit:
            self.conn.commit()

    def refresh_post_comment_stats(self, post_id=None, symbol=None, post_ids=None, commit=True):
        scoped_post_ids = set(str(pid or "").strip() for pid in (post_ids or []) if str(pid or "").strip())
        if post_id:
            scoped_post_ids.add(str(post_id).strip())
        if symbol:
            for row in self.conn.execute("SELECT id FROM posts WHERE symbol=?", (symbol,)).fetchall():
                scoped_post_ids.add(str(row["id"] or "").strip())
        if not scoped_post_ids:
            for row in self.conn.execute("SELECT id FROM posts").fetchall():
                scoped_post_ids.add(str(row["id"] or "").strip())
        scoped_post_ids = [pid for pid in scoped_post_ids if pid]
        if not scoped_post_ids:
            return

        placeholders = ",".join("?" for _ in scoped_post_ids)
        self.conn.execute(
            f"""
            UPDATE posts
            SET comments_scraped=(
                SELECT COUNT(*)
                FROM comment_memberships m
                WHERE m.post_id=posts.id
            )
            WHERE id IN ({placeholders})
            """,
            scoped_post_ids,
        )
        self.conn.execute(
            f"""
            UPDATE posts
            SET max_comment_depth=COALESCE((
                SELECT MAX(c.depth)
                FROM comment_memberships m
                JOIN comments c ON c.id = m.comment_id
                WHERE m.post_id=posts.id
            ), 1)
            WHERE id IN ({placeholders})
            """,
            scoped_post_ids,
        )
        if commit:
            self.conn.commit()

    def reconcile_post_thread_links(self, post_id=None, symbol=None, commit=True):
        post_sql = """
            SELECT id, created_at, COALESCE(NULLIF(thread_root_post_id, ''), id) AS current_root
            FROM posts
        """
        params = []
        conditions = []
        if post_id:
            conditions.append("id=?")
            params.append(post_id)
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if conditions:
            post_sql += " WHERE " + " AND ".join(conditions)

        scoped_posts = [dict(row) for row in self.conn.execute(post_sql, params).fetchall()]
        if not scoped_posts:
            return

        scoped_ids = [row["id"] for row in scoped_posts]
        placeholders = ",".join("?" for _ in scoped_ids)
        best_root = {row["id"]: str(row["current_root"] or row["id"]) for row in scoped_posts}

        cross_post_rows = self.conn.execute(
            f"""
            SELECT post_id,
                   parent_post_id AS candidate_root,
                   COUNT(*) AS hits
            FROM comments
            WHERE post_id IN ({placeholders})
              AND parent_scope = 'cross_post'
              AND COALESCE(parent_post_id, '') != ''
            GROUP BY post_id, candidate_root
            ORDER BY post_id, hits DESC
            """,
            scoped_ids,
        ).fetchall()
        seen_posts = set()
        for row in cross_post_rows:
            candidate_root = str(row["candidate_root"] or "").strip()
            pid = row["post_id"]
            if not candidate_root or pid in seen_posts:
                continue
            if best_root.get(pid, pid) in ("", pid):
                best_root[pid] = candidate_root
            seen_posts.add(pid)

        updates = {}
        for pid, root in best_root.items():
            updates[pid] = self._resolve_known_thread_root(root or pid) or pid

        status_link_rows = self.conn.execute(
            f"""
            SELECT DISTINCT status_id, root_status_id
            FROM comments
            WHERE post_id IN ({placeholders})
              AND COALESCE(status_id, '') != ''
              AND COALESCE(root_status_id, '') != ''
              AND status_id != root_status_id
            """,
            scoped_ids,
        ).fetchall()
        for row in status_link_rows:
            status_id = str(row["status_id"] or "").strip()
            root_status_id = str(row["root_status_id"] or "").strip()
            if not status_id or not root_status_id:
                continue
            updates[status_id] = self._resolve_known_thread_root(root_status_id) or root_status_id

        anchor_rows = self.conn.execute(
            f"""
            SELECT DISTINCT post_id
            FROM comments
            WHERE post_id IN ({placeholders})
              AND COALESCE(status_id, '') != ''
              AND COALESCE(root_status_id, '') = post_id
              AND status_id != post_id
            """,
            scoped_ids,
        ).fetchall()
        post_created = {row["id"]: int(row["created_at"] or 0) for row in scoped_posts}
        for row in anchor_rows:
            pid = row["post_id"]
            candidate_root = updates.get(pid, pid)
            if candidate_root == pid:
                continue
            root_row = self.conn.execute(
                "SELECT created_at FROM posts WHERE id=?",
                (candidate_root,),
            ).fetchone()
            if root_row and int(root_row["created_at"] or 0) > post_created.get(pid, 0):
                updates[pid] = pid

        for pid, root in list(updates.items()):
            updates[pid] = updates.get(root, root)

        for pid, root in updates.items():
            row = self.conn.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone()
            if not row:
                continue
            self.conn.execute(
                "UPDATE posts SET thread_root_post_id=? WHERE id=?",
                (root or pid, pid),
            )

        if commit:
            self.conn.commit()

    def _load_thread_aggregates(self, symbol=None, days=None):
        sql = """
            SELECT id, symbol, user_name, created_at, reply_count, comments_scraped,
                   max_comment_depth, COALESCE(NULLIF(thread_root_post_id, ''), id) AS thread_id
            FROM posts
        """
        params = []
        conditions = []
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if days:
            cutoff_ms = int((time.time() - days * 86400) * 1000)
            conditions.append("created_at>?")
            params.append(cutoff_ms)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at ASC"

        post_rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        if not post_rows:
            return [], {}

        post_ids = [row["id"] for row in post_rows]
        placeholders = ",".join("?" for _ in post_ids)
        membership_rows = self.conn.execute(
            f"""
            SELECT p.id AS post_id,
                   COALESCE(NULLIF(p.thread_root_post_id, ''), p.id) AS thread_id,
                   m.comment_id,
                   COALESCE(c.parent_scope, '') AS parent_scope,
                   COALESCE(c.parent_post_id, '') AS parent_post_id,
                   COALESCE(c.canonical_post_id, '') AS canonical_post_id
            FROM posts p
            LEFT JOIN comment_memberships m ON m.post_id = p.id
            LEFT JOIN comments c ON c.id = m.comment_id
            WHERE p.id IN ({placeholders})
            """,
            post_ids,
        ).fetchall()

        threads = {}
        for row in post_rows:
            thread_id = row["thread_id"]
            thread = threads.setdefault(
                thread_id,
                {
                    "thread_id": thread_id,
                    "symbol": row["symbol"],
                    "posts": [],
                    "comment_ids": set(),
                    "orphan_comment_ids": set(),
                    "cross_post_comment_ids": set(),
                },
            )
            thread["posts"].append(row)

        for row in membership_rows:
            thread = threads.get(row["thread_id"])
            if not thread:
                continue
            comment_id = str(row["comment_id"] or "").strip()
            if not comment_id:
                continue
            thread["comment_ids"].add(comment_id)
            if row["parent_scope"] == "missing":
                thread["orphan_comment_ids"].add(comment_id)
            elif (
                row["parent_scope"] == "cross_post"
                and str(row["canonical_post_id"] or "").strip() != str(row["parent_post_id"] or "").strip()
            ):
                thread["cross_post_comment_ids"].add(comment_id)

        aggregates = []
        for thread_id, thread in threads.items():
            posts = thread["posts"]
            representative = next((p for p in posts if p["id"] == thread_id), None)
            if representative is None:
                representative = max(
                    posts,
                    key=lambda item: (item.get("reply_count", 0), -item.get("created_at", 0)),
                )
            claimed_comments = max((int(p.get("reply_count", 0) or 0) for p in posts), default=0)
            comments_scraped = len(thread["comment_ids"])
            aggregates.append(
                {
                    "id": representative["id"],
                    "thread_id": thread_id,
                    "symbol": representative["symbol"],
                    "user_name": representative["user_name"],
                    "reply_count": claimed_comments,
                    "comments_scraped": comments_scraped,
                    "gap": max(0, claimed_comments - comments_scraped),
                    "orphan_comments": len(thread["orphan_comment_ids"]),
                    "cross_post_replies": len(thread["cross_post_comment_ids"]),
                    "thread_posts": len(posts),
                    "max_comment_depth": max((int(p.get("max_comment_depth", 1) or 1) for p in posts), default=1),
                }
            )

        return aggregates, {
            "raw_posts": post_rows,
            "threads": threads,
        }

    def get_posts_needing_backfill(self, symbol=None, days=7):
        aggregates, _ = self._load_thread_aggregates(symbol=symbol, days=days)
        rows = [row for row in aggregates if row["gap"] > 0]
        rows.sort(key=lambda item: (item["gap"], item["reply_count"]), reverse=True)
        return rows

    def get_posts_with_orphan_comments(self, symbol=None, days=None):
        aggregates, _ = self._load_thread_aggregates(symbol=symbol, days=days)
        rows = [row for row in aggregates if row["orphan_comments"] > 0]
        rows.sort(key=lambda item: (item["orphan_comments"], item["gap"]), reverse=True)
        return rows

    def get_stock_completeness_report(self, symbol=None, days=None):
        watched_sql = """
            SELECT symbol, name, last_scrape_time, oldest_post_time, history_complete
            FROM watched_stocks
        """
        params = []
        if symbol:
            watched_sql += " WHERE symbol=?"
            params.append(symbol)
        watched_sql += " ORDER BY symbol"
        watched_rows = [dict(row) for row in self.conn.execute(watched_sql, params).fetchall()]

        thread_rows, meta = self._load_thread_aggregates(symbol=symbol, days=days)
        posts_by_symbol = {}
        for row in meta.get("raw_posts", []):
            posts_by_symbol.setdefault(row["symbol"], []).append(row)
        threads_by_symbol = {}
        for row in thread_rows:
            threads_by_symbol.setdefault(row["symbol"], []).append(row)

        reports = []
        for watched in watched_rows:
            symbol_key = watched["symbol"]
            symbol_posts = posts_by_symbol.get(symbol_key, [])
            symbol_threads = threads_by_symbol.get(symbol_key, [])
            reports.append(
                {
                    "symbol": symbol_key,
                    "name": watched["name"],
                    "last_scrape_time": watched["last_scrape_time"],
                    "oldest_post_time": watched["oldest_post_time"],
                    "history_complete": watched["history_complete"],
                    "total_posts": len(symbol_posts),
                    "total_threads": len(symbol_threads),
                    "claimed_comments": sum(row["reply_count"] for row in symbol_threads),
                    "comments_scraped": sum(row["comments_scraped"] for row in symbol_threads),
                    "gap_posts": sum(1 for row in symbol_threads if row["gap"] > 0),
                    "missing_comments": sum(row["gap"] for row in symbol_threads),
                    "max_comment_depth": max([1] + [row["max_comment_depth"] for row in symbol_threads]),
                    "orphan_comments": sum(row["orphan_comments"] for row in symbol_threads),
                    "cross_post_replies": sum(row["cross_post_replies"] for row in symbol_threads),
                }
            )
        return reports

    def reconcile_comment_parent_links(self, post_id=None, symbol=None, commit=True):
        conditions = []
        params = []
        if post_id:
            conditions.append("post_id=?")
            params.append(post_id)
        elif symbol:
            conditions.append("post_id IN (SELECT id FROM posts WHERE symbol=?)")
            params.append(symbol)
        where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        self.conn.execute(
            f"UPDATE comments SET parent_post_id='', parent_scope='root' "
            f"WHERE COALESCE(parent_comment_id, '')='' {('AND ' + ' AND '.join(conditions)) if conditions else ''}",
            params,
        )
        self.conn.execute(
            f"""
            UPDATE comments
            SET parent_post_id=post_id, parent_scope='same_post'
            WHERE COALESCE(parent_comment_id, '')!=''
              AND EXISTS (
                  SELECT 1 FROM comments parent
                  WHERE parent.id = comments.parent_comment_id
                    AND parent.post_id = comments.post_id
              )
              {('AND ' + ' AND '.join(conditions)) if conditions else ''}
            """,
            params,
        )
        self.conn.execute(
            f"""
            UPDATE comments
            SET parent_post_id=(
                    SELECT parent.post_id
                    FROM comments parent
                    WHERE parent.id = comments.parent_comment_id
                    LIMIT 1
                ),
                parent_scope='cross_post'
            WHERE COALESCE(parent_comment_id, '')!=''
              AND NOT EXISTS (
                  SELECT 1 FROM comments parent
                  WHERE parent.id = comments.parent_comment_id
                    AND parent.post_id = comments.post_id
              )
              AND EXISTS (
                  SELECT 1 FROM comments parent
                  WHERE parent.id = comments.parent_comment_id
              )
              {('AND ' + ' AND '.join(conditions)) if conditions else ''}
            """,
            params,
        )
        self.conn.execute(
            f"""
            UPDATE comments
            SET parent_post_id='',
                parent_scope='missing'
            WHERE COALESCE(parent_comment_id, '')!=''
              AND NOT EXISTS (
                  SELECT 1 FROM comments parent
                  WHERE parent.id = comments.parent_comment_id
              )
              {('AND ' + ' AND '.join(conditions)) if conditions else ''}
            """,
            params,
        )
        if commit:
            self.conn.commit()

    def get_user_completeness_report(self, user_id=None):
        sql = """
            SELECT t.user_id, t.screen_name, t.last_check_time, t.oldest_status_time, t.history_complete,
                   COUNT(u.id) AS total_statuses,
                   MIN(u.created_at) AS first_status_time,
                   MAX(u.created_at) AS latest_status_time
            FROM tracked_users t
            LEFT JOIN user_statuses u ON u.user_id = t.user_id
        """
        params = []
        if user_id:
            sql += " WHERE t.user_id=?"
            params.append(user_id)
        sql += " GROUP BY t.user_id, t.screen_name, t.last_check_time, t.oldest_status_time, t.history_complete ORDER BY t.user_id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ──────── 热门话题 ────────

    def save_trending_topic(self, topic: dict) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO trending_topics
               (id, platform_id, title, url, discuss_count, followers_count,
                rank, associated_stocks, captured_at, captured_date)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (topic["id"], topic.get("platform_id","xueqiu"), topic["title"],
             topic.get("url",""), topic.get("discuss_count",0),
             topic.get("followers_count",0), topic.get("rank",0),
             topic.get("associated_stocks","[]"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             datetime.now().strftime("%Y-%m-%d")))
        self.conn.commit()
        return cur.rowcount > 0

    def get_trending_topics(self, date=None, limit=20):
        d = date or datetime.now().strftime("%Y-%m-%d")
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM trending_topics WHERE captured_date=? ORDER BY rank ASC LIMIT ?", (d, limit)).fetchall()]

    # ──────── 用户发言 ────────

    def save_user_status(self, status) -> bool:
        from utils.time_utils import ms_to_str, get_market_phase
        ca = status["created_at"]
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO user_statuses
               (id, platform_id, user_id, user_name, text_html, text_plain,
                target_symbol, target_name, created_at, created_at_str,
                market_phase, reply_count, like_count,
                retweet_status_id, parent_status_id, is_original_post)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (status["id"], status.get("platform_id","xueqiu"),
             status["user_id"], status["user_name"],
             status["text_html"], status.get("text_plain",""),
             status.get("target_symbol",""), status.get("target_name",""),
             ca, ms_to_str(ca), get_market_phase(ca),
             status.get("reply_count",0), status.get("like_count",0),
             status.get("retweet_status_id",""),
             status.get("parent_status_id",""),
             1 if status.get("is_original_post", True) else 0))
        if cur.rowcount == 0:
            self.conn.execute(
                """UPDATE user_statuses SET
                   user_id=?, user_name=?, text_html=?, text_plain=?,
                   target_symbol=?, target_name=?, created_at=?, created_at_str=?,
                   market_phase=?, reply_count=?, like_count=?,
                   retweet_status_id=?, parent_status_id=?, is_original_post=?
                   WHERE id=?""",
                (status["user_id"], status["user_name"],
                 status["text_html"], status.get("text_plain", ""),
                 status.get("target_symbol", ""), status.get("target_name", ""),
                 ca, ms_to_str(ca), get_market_phase(ca),
                 status.get("reply_count", 0), status.get("like_count", 0),
                 status.get("retweet_status_id",""),
                 status.get("parent_status_id",""),
                 1 if status.get("is_original_post", True) else 0,
                 status["id"]))
        self.conn.commit()
        return cur.rowcount > 0

    def save_user_statuses_batch(self, statuses):
        return sum(1 for s in statuses if self.save_user_status(s))

    # ──────── 日志 ────────

    def log_scrape(self, task_type, target, status, new_items_count=0,
                   error_message="", started_at=None, duration_seconds=None):
        self.conn.execute(
            "INSERT INTO scrape_logs (task_type,target,status,new_items_count,duration_seconds,error_message,started_at) VALUES(?,?,?,?,?,?,?)",
            (task_type, target, status, new_items_count, duration_seconds,
             error_message, started_at or datetime.now().isoformat()))
        self.conn.commit()

    # ──────── 统计 ────────

    def get_stats(self):
        stats = {}
        for t in ["posts","comments","user_statuses","scrape_logs"]:
            stats[t] = self.conn.execute(f"SELECT COUNT(*) as c FROM {t}").fetchone()["c"]
        stats["watched_stocks"] = len(self.get_watched_stocks())
        stats["tracked_users"] = len(self.get_tracked_users())
        try: stats["user_profiles"] = self.conn.execute("SELECT COUNT(*) as c FROM user_profiles").fetchone()["c"]
        except: stats["user_profiles"] = 0
        try: stats["trending_topics"] = self.conn.execute("SELECT COUNT(*) as c FROM trending_topics").fetchone()["c"]
        except: stats["trending_topics"] = 0
        return stats

    def get_recent_logs(self, limit=20):
        return [dict(r) for r in self.conn.execute("SELECT * FROM scrape_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def get_daily_summary(self, date=None):
        """生成每日摘要数据。"""
        d = date or datetime.now().strftime("%Y-%m-%d")
        summary = {"date": d, "stocks": []}
        for s in self.get_watched_stocks():
            sym = s["symbol"]
            posts_today = self.conn.execute("SELECT COUNT(*) as c FROM posts WHERE symbol=? AND created_at_str LIKE ?", (sym, f"{d}%")).fetchone()["c"]
            comments_today = self.conn.execute(
                """SELECT COUNT(*) as c
                     FROM comment_memberships m
                     JOIN comments c ON c.id = m.comment_id
                     JOIN posts p ON p.id = m.post_id
                    WHERE p.symbol=? AND c.created_at_str LIKE ?""",
                (sym, f"{d}%"),
            ).fetchone()["c"]
            summary["stocks"].append({"symbol": sym, "name": s.get("name",""), "new_posts": posts_today, "new_comments": comments_today})
        return summary

    def close(self):
        self.conn.close()
        if self._log_lifecycle:
            logger.info("数据库连接已关闭")
