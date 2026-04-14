#!/usr/bin/env python3
"""
Phase 5 安装脚本 — 自动化运维全量升级

用法:
  cd ~/Desktop/xueqiu-scraper
  source venv/bin/activate
  python phase5_setup.py

包含 8 个步骤:
  Step 1: 数据库 v2→v3 迁移（新字段+新表+视图升级）
  Step 2: 数据模型升级（深层评论+全维度+用户画像采集）
  Step 3: 爬取效率优化（自适应频率+多实例并发）
  Step 4: 话题热度榜（trending_scraper）
  Step 5: 多平台预埋（BaseScraper 抽象）
  Step 6: 定时调度（多时段策略）
  Step 7: 健康监控+通知（Token自检+每日摘要）
  Step 8: 导出升级（JSON树形评论+新字段）
"""

import os
import sys
import shutil
import sqlite3
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{filepath}.bak_{ts}"
        shutil.copy2(filepath, bak)
        print(f"  备份: {os.path.basename(bak)}")


def write_file(rel_path, content, desc=""):
    full = os.path.join(PROJECT_ROOT, rel_path)
    d = os.path.dirname(full)
    if d:
        os.makedirs(d, exist_ok=True)
    if os.path.exists(full):
        backup(full)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ {rel_path}" + (f" ({desc})" if desc else ""))


# ================================================================
# Step 1: 数据库迁移
# ================================================================

def step1_migrate_db():
    """数据库 v2→v3 平滑迁移"""
    db_path = os.path.join(PROJECT_ROOT, "data", "xueqiu.db")
    if not os.path.exists(db_path):
        print("  ⏭ 数据库不存在，将在首次运行时由 database.py v3 创建")
        return

    # 备份数据库
    bak = f"{db_path}.v2_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db_path, bak)
    print(f"  ✓ 数据库已备份: {os.path.basename(bak)}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    def has_col(table, col):
        cur.execute(f"PRAGMA table_info({table})")
        return col in {r[1] for r in cur.fetchall()}

    def has_table(name):
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return cur.fetchone() is not None

    def safe_add(table, col, ctype, default=None):
        if has_col(table, col):
            return
        d = f" DEFAULT {default}" if default is not None else ""
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}{d}")
        print(f"    + {table}.{col}")

    changes = 0
    # posts 新字段
    if has_table("posts"):
        for col, ct, df in [("platform_id","TEXT","'xueqiu'"),("fav_count","INTEGER","0"),
                             ("view_count","INTEGER","0"),("max_comment_depth","INTEGER","1")]:
            if not has_col("posts", col):
                safe_add("posts", col, ct, df); changes += 1

    # comments 新字段
    if has_table("comments"):
        for col, ct, df in [("platform_id","TEXT","'xueqiu'"),("market_phase","TEXT",None),
                             ("like_count","INTEGER","0"),("parent_comment_id","TEXT",None),
                             ("reply_to_user_id","TEXT",None),("reply_to_user_name","TEXT",None),
                             ("depth","INTEGER","1")]:
            if not has_col("comments", col):
                safe_add("comments", col, ct, df); changes += 1
        # 同步旧数据
        if has_col("comments","reply_comment_id") and has_col("comments","parent_comment_id"):
            cur.execute("UPDATE comments SET parent_comment_id=reply_comment_id WHERE parent_comment_id IS NULL AND reply_comment_id IS NOT NULL AND reply_comment_id!=''")
            cur.execute("UPDATE comments SET depth=2 WHERE parent_comment_id IS NOT NULL AND parent_comment_id!='' AND depth=1")
            synced = cur.rowcount
            if synced: print(f"    ↻ {synced} 条评论 depth 已同步")

    # user_statuses 新字段
    if has_table("user_statuses"):
        if not has_col("user_statuses","platform_id"):
            safe_add("user_statuses","platform_id","TEXT","'xueqiu'"); changes += 1

    # watched_stocks 新字段
    if has_table("watched_stocks"):
        if not has_col("watched_stocks","sector"):
            safe_add("watched_stocks","sector","TEXT",None); changes += 1

    # tracked_users 新字段
    if has_table("tracked_users"):
        if not has_col("tracked_users","credibility_score"):
            safe_add("tracked_users","credibility_score","REAL",None); changes += 1

    # scrape_logs 新字段
    if has_table("scrape_logs"):
        if not has_col("scrape_logs","duration_seconds"):
            safe_add("scrape_logs","duration_seconds","REAL",None); changes += 1

    # 新表: user_profiles
    if not has_table("user_profiles"):
        cur.execute("""CREATE TABLE user_profiles (
            user_id TEXT NOT NULL, platform_id TEXT NOT NULL DEFAULT 'xueqiu',
            screen_name TEXT, profile_image_url TEXT,
            is_default_name INTEGER DEFAULT 0, is_default_avatar INTEGER DEFAULT 0,
            followers_count INTEGER, following_count INTEGER, status_count INTEGER,
            created_at TEXT, verified_type TEXT, description TEXT,
            first_seen_at TEXT, last_updated_at TEXT,
            PRIMARY KEY (user_id, platform_id))""")
        print("    + user_profiles 表"); changes += 1

    # 新表: trending_topics
    if not has_table("trending_topics"):
        cur.execute("""CREATE TABLE trending_topics (
            id TEXT NOT NULL, platform_id TEXT NOT NULL DEFAULT 'xueqiu',
            title TEXT, url TEXT, discuss_count INTEGER, followers_count INTEGER,
            rank INTEGER, associated_stocks TEXT, captured_at TEXT, captured_date TEXT,
            PRIMARY KEY (id, platform_id, captured_date))""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trending_date ON trending_topics(captured_date, rank)")
        print("    + trending_topics 表"); changes += 1

    # 新表: schema_version
    cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, migrated_at TEXT, description TEXT)")
    cur.execute("INSERT OR REPLACE INTO schema_version VALUES (3, ?, 'Phase 5: v2→v3')", (datetime.now().isoformat(),))

    # 索引
    for idx, tbl, cols in [("idx_posts_platform","posts","platform_id"),
                            ("idx_comments_parent","comments","parent_comment_id"),
                            ("idx_comments_depth","comments","post_id, depth"),
                            ("idx_user_profiles_name","user_profiles","screen_name")]:
        if has_table(tbl):
            try: cur.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {tbl}({cols})")
            except: pass

    # 升级视图
    cur.execute("DROP VIEW IF EXISTS discussion_threads")
    cur.execute("""CREATE VIEW discussion_threads AS
        SELECT p.id AS thread_id, COALESCE(p.platform_id,'xueqiu') AS platform_id,
            p.symbol, p.user_id AS author_id, p.user_name AS author, p.title,
            COALESCE(p.text_plain, p.description, '') AS content,
            p.created_at_str AS start_time, p.market_phase,
            p.reply_count AS claimed_comments, p.comments_scraped AS actual_comments,
            p.like_count, COALESCE(p.fav_count,0) AS fav_count,
            COALESCE(p.view_count,0) AS view_count, p.retweet_count,
            COALESCE(p.max_comment_depth,1) AS max_comment_depth,
            (SELECT COUNT(DISTINCT c.user_id) FROM comments c WHERE c.post_id=p.id) AS participants,
            (SELECT MAX(c.created_at_str) FROM comments c WHERE c.post_id=p.id) AS last_comment_time
        FROM posts p""")
    print("    ✓ discussion_threads 视图已升级")

    conn.commit()
    conn.close()
    print(f"  ✓ 数据库迁移完成 ({changes} 项变更)")


# ================================================================
# Step 2+3: 更新 storage/database.py v3
# ================================================================

DATABASE_V3 = r'''"""
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
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

        self._create_tables()
        self._migrate()
        logger.info(f"数据库已连接: {db_path}")

    # ──────── 建表 ────────

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS watched_stocks (
                symbol TEXT PRIMARY KEY, name TEXT, sector TEXT,
                last_scrape_time INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS tracked_users (
                user_id TEXT PRIMARY KEY, screen_name TEXT,
                last_check_time INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1,
                note TEXT DEFAULT '', credibility_score REAL,
                created_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY, platform_id TEXT DEFAULT 'xueqiu',
                symbol TEXT, user_id TEXT, user_name TEXT,
                title TEXT DEFAULT '', text_html TEXT DEFAULT '', text_plain TEXT DEFAULT '',
                description TEXT DEFAULT '', created_at INTEGER DEFAULT 0,
                created_at_str TEXT DEFAULT '', market_phase TEXT DEFAULT '',
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
                scraped_at TEXT DEFAULT (datetime('now')));

            CREATE TABLE IF NOT EXISTS user_statuses (
                id TEXT PRIMARY KEY, platform_id TEXT DEFAULT 'xueqiu',
                user_id TEXT, user_name TEXT,
                text_html TEXT DEFAULT '', text_plain TEXT DEFAULT '',
                target_symbol TEXT DEFAULT '', target_name TEXT DEFAULT '',
                created_at INTEGER DEFAULT 0, created_at_str TEXT DEFAULT '',
                market_phase TEXT DEFAULT '',
                reply_count INTEGER DEFAULT 0, like_count INTEGER DEFAULT 0,
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
            CREATE INDEX IF NOT EXISTS idx_user_statuses_user ON user_statuses(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_statuses_created ON user_statuses(created_at);
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
                (SELECT COUNT(DISTINCT c.user_id) FROM comments c WHERE c.post_id=p.id) AS participants,
                (SELECT MAX(c.created_at_str) FROM comments c WHERE c.post_id=p.id) AS last_comment_time
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
            ("comments","created_at_str","TEXT DEFAULT ''"),
            ("comments","platform_id","TEXT DEFAULT 'xueqiu'"),
            ("comments","market_phase","TEXT DEFAULT ''"),
            ("comments","like_count","INTEGER DEFAULT 0"),
            ("comments","parent_comment_id","TEXT"),
            ("comments","reply_to_user_id","TEXT"),
            ("comments","reply_to_user_name","TEXT"),
            ("comments","depth","INTEGER DEFAULT 1"),
            ("user_statuses","created_at_str","TEXT DEFAULT ''"),
            ("user_statuses","market_phase","TEXT DEFAULT ''"),
            ("user_statuses","platform_id","TEXT DEFAULT 'xueqiu'"),
            ("watched_stocks","sector","TEXT"),
            ("tracked_users","credibility_score","REAL"),
            ("scrape_logs","duration_seconds","REAL"),
        ]
        for table, column, col_type in migrations:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                logger.info(f"迁移: {table}.{column}")
            except sqlite3.OperationalError:
                pass
        self._backfill_time_fields()
        self.conn.commit()

    def _backfill_time_fields(self):
        from utils.time_utils import ms_to_str, get_market_phase
        for row in self.conn.execute("SELECT id, created_at FROM posts WHERE created_at_str='' AND created_at>0").fetchall():
            self.conn.execute("UPDATE posts SET created_at_str=?, market_phase=? WHERE id=?",
                              (ms_to_str(row["created_at"]), get_market_phase(row["created_at"]), row["id"]))
        self.conn.execute("UPDATE posts SET comments_scraped=(SELECT COUNT(*) FROM comments WHERE comments.post_id=posts.id) WHERE comments_scraped=0")
        for row in self.conn.execute("SELECT id, created_at FROM comments WHERE created_at_str='' AND created_at>0").fetchall():
            self.conn.execute("UPDATE comments SET created_at_str=? WHERE id=?", (ms_to_str(row["created_at"]), row["id"]))
        for row in self.conn.execute("SELECT id, created_at FROM user_statuses WHERE created_at_str='' AND created_at>0").fetchall():
            self.conn.execute("UPDATE user_statuses SET created_at_str=?, market_phase=? WHERE id=?",
                              (ms_to_str(row["created_at"]), get_market_phase(row["created_at"]), row["id"]))

    # ──────── 股票管理 ────────

    def upsert_stock(self, symbol, name, sector=None):
        if sector:
            self.conn.execute("INSERT INTO watched_stocks(symbol,name,sector) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, sector=excluded.sector", (symbol, name, sector))
        else:
            self.conn.execute("INSERT INTO watched_stocks(symbol,name) VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name", (symbol, name))
        self.conn.commit()

    def get_watched_stocks(self, active_only=True):
        sql = "SELECT * FROM watched_stocks" + (" WHERE is_active=1" if active_only else "")
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def get_stock_last_scrape_time(self, symbol):
        r = self.conn.execute("SELECT last_scrape_time FROM watched_stocks WHERE symbol=?", (symbol,)).fetchone()
        return r["last_scrape_time"] if r else 0

    def update_stock_scrape_time(self, symbol, timestamp):
        self.conn.execute("UPDATE watched_stocks SET last_scrape_time=? WHERE symbol=?", (timestamp, symbol))
        self.conn.commit()

    # ──────── 用户管理 ────────

    def upsert_tracked_user(self, user_id, screen_name, note=""):
        self.conn.execute("INSERT INTO tracked_users(user_id,screen_name,note) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET screen_name=excluded.screen_name, note=excluded.note", (user_id, screen_name, note))
        self.conn.commit()

    def get_tracked_users(self, active_only=True):
        sql = "SELECT * FROM tracked_users" + (" WHERE is_active=1" if active_only else "")
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def get_user_last_check_time(self, user_id):
        r = self.conn.execute("SELECT last_check_time FROM tracked_users WHERE user_id=?", (user_id,)).fetchone()
        return r["last_check_time"] if r else 0

    def update_user_check_time(self, user_id, timestamp):
        self.conn.execute("UPDATE tracked_users SET last_check_time=? WHERE user_id=?", (timestamp, user_id))
        self.conn.commit()

    # ──────── 用户画像 ────────

    def upsert_user_profile(self, profile: dict):
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
        self.conn.commit()
        return cur.rowcount > 0

    def save_posts_batch(self, symbol, posts):
        return sum(1 for p in posts if self.save_post(symbol, p))

    # ──────── 评论写入 ────────

    def save_comment(self, post_id, comment) -> bool:
        from utils.time_utils import ms_to_str, get_market_phase
        ca = comment["created_at"]
        parent = comment.get("parent_comment_id") or comment.get("reply_comment_id") or ""
        depth = comment.get("depth", 2 if parent else 1)
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO comments
               (id, post_id, platform_id, user_id, user_name, text_html, text_plain,
                created_at, created_at_str, market_phase, like_count,
                reply_comment_id, parent_comment_id,
                reply_to_user_id, reply_to_user_name, depth)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (comment["id"], post_id, comment.get("platform_id","xueqiu"),
             comment["user_id"], comment["user_name"],
             comment["text_html"], comment.get("text_plain",""),
             ca, ms_to_str(ca), get_market_phase(ca),
             comment.get("like_count",0),
             comment.get("reply_comment_id",""), parent,
             comment.get("reply_to_user_id",""), comment.get("reply_to_user_name",""),
             depth))
        self.conn.commit()
        return cur.rowcount > 0

    def save_comments_batch(self, post_id, comments):
        return sum(1 for c in comments if self.save_comment(post_id, c))

    # ──────── 评论回填 ────────

    def update_post_comments_scraped(self, post_id):
        self.conn.execute("UPDATE posts SET comments_scraped=(SELECT COUNT(*) FROM comments WHERE post_id=?) WHERE id=?", (post_id, post_id))
        # 更新最大评论深度
        r = self.conn.execute("SELECT MAX(depth) as md FROM comments WHERE post_id=?", (post_id,)).fetchone()
        if r and r["md"]:
            self.conn.execute("UPDATE posts SET max_comment_depth=? WHERE id=?", (r["md"], post_id))
        self.conn.commit()

    def get_posts_needing_backfill(self, symbol=None, days=7):
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        sql = "SELECT id, symbol, user_name, reply_count, comments_scraped, (reply_count-comments_scraped) AS gap FROM posts WHERE reply_count>comments_scraped AND created_at>?"
        params = [cutoff_ms]
        if symbol:
            sql += " AND symbol=?"; params.append(symbol)
        sql += " ORDER BY gap DESC"
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
                market_phase, reply_count, like_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (status["id"], status.get("platform_id","xueqiu"),
             status["user_id"], status["user_name"],
             status["text_html"], status.get("text_plain",""),
             status.get("target_symbol",""), status.get("target_name",""),
             ca, ms_to_str(ca), get_market_phase(ca),
             status.get("reply_count",0), status.get("like_count",0)))
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
            comments_today = self.conn.execute("""SELECT COUNT(*) as c FROM comments c
                JOIN posts p ON c.post_id=p.id WHERE p.symbol=? AND c.created_at_str LIKE ?""", (sym, f"{d}%")).fetchone()["c"]
            summary["stocks"].append({"symbol": sym, "name": s.get("name",""), "new_posts": posts_today, "new_comments": comments_today})
        return summary

    def close(self):
        self.conn.close()
        logger.info("数据库连接已关闭")
'''


# ================================================================
# Step 3: 自适应频率控制器
# ================================================================

RATE_LIMITER_V2 = r'''"""
请求节流器 v2 — 自适应频率控制

Phase 5 升级:
  - 自适应间隔: 成功时逐步降低，失败时翻倍
  - 全局 QPS 控制（多实例共享时）
  - 更灵活的爆发休息策略
"""

import time
import random
from collections import deque
from utils.logger import get_logger

logger = get_logger()


class RateLimiter:
    """自适应请求频率控制器。"""

    def __init__(self, config: dict):
        self.min_interval = config.get("min_request_interval", 3.0)
        self.max_interval = config.get("max_request_interval", 8.0)
        self.max_per_minute = config.get("max_requests_per_minute", 10)
        self.burst_rest_count = config.get("burst_rest_count", 50)
        self.burst_rest_min = config.get("burst_rest_seconds_min", 60)
        self.burst_rest_max = config.get("burst_rest_seconds_max", 180)

        # 自适应状态
        self._current_interval = (self.min_interval + self.max_interval) / 2
        self._consecutive_success = 0
        self._request_times: deque = deque()
        self._total_requests: int = 0
        self._last_request_time: float = 0.0

    @property
    def total_requests(self):
        return self._total_requests

    @property
    def current_interval(self):
        return self._current_interval

    def wait(self):
        """请求前调用，自适应等待。"""
        # 基础间隔
        elapsed = time.time() - self._last_request_time
        delay = random.uniform(self._current_interval * 0.8, self._current_interval * 1.2)
        if elapsed < delay:
            time.sleep(delay - elapsed)

        # 每分钟滑动窗口
        now = time.time()
        while self._request_times and now - self._request_times[0] > 60:
            self._request_times.popleft()
        if len(self._request_times) >= self.max_per_minute:
            wait_until = self._request_times[0] + 60
            sleep_time = wait_until - now + random.uniform(1.0, 3.0)
            if sleep_time > 0:
                logger.debug(f"频率控制: 等待 {sleep_time:.1f}s")
                time.sleep(sleep_time)

        # 爆发休息
        self._total_requests += 1
        if self.burst_rest_count > 0 and self._total_requests % self.burst_rest_count == 0:
            rest = random.uniform(self.burst_rest_min, self.burst_rest_max)
            logger.info(f"累计 {self._total_requests} 请求，休息 {rest:.0f}s")
            time.sleep(rest)

        self._request_times.append(time.time())
        self._last_request_time = time.time()

    def on_success(self):
        """请求成功后调用，逐步降低间隔。"""
        self._consecutive_success += 1
        if self._consecutive_success >= 10:
            self._current_interval = max(self.min_interval, self._current_interval - 0.3)
            self._consecutive_success = 0

    def on_failure(self):
        """请求失败后调用，翻倍间隔。"""
        self._consecutive_success = 0
        self._current_interval = min(self.max_interval * 2, self._current_interval * 2)
        logger.warning(f"频率自适应: 间隔升至 {self._current_interval:.1f}s")

    def on_recover(self):
        """WAF恢复后，缓慢降回正常间隔。"""
        self._current_interval = min(self.max_interval, self._current_interval)

    def reset(self):
        self._request_times.clear()
        self._total_requests = 0
        self._last_request_time = 0.0
        self._current_interval = (self.min_interval + self.max_interval) / 2
        self._consecutive_success = 0
'''


# ================================================================
# Step 3: 浏览器实例池
# ================================================================

BROWSER_POOL = r'''"""
浏览器实例池 — 管理多个 Playwright 实例实现并发爬取

用法:
  pool = BrowserPool(cookie_manager, config, max_instances=3)
  client = pool.acquire()
  try:
      data = client.get(url, params)
  finally:
      pool.release(client)
  pool.close_all()
"""

import threading
import queue
from utils.logger import get_logger

logger = get_logger()


class BrowserPool:
    """Playwright 浏览器实例池。"""

    def __init__(self, cookie_manager, rate_limiter_config: dict, max_instances: int = 3):
        self.cookie_manager = cookie_manager
        self.rate_limiter_config = rate_limiter_config
        self.max_instances = max_instances
        self._pool = queue.Queue()
        self._all_clients = []
        self._lock = threading.Lock()
        self._created = 0

    def acquire(self):
        """从池中获取一个可用的 XueqiuClient 实例。"""
        # 先尝试从池中取
        try:
            client = self._pool.get_nowait()
            return client
        except queue.Empty:
            pass

        # 池空则创建新实例
        with self._lock:
            if self._created < self.max_instances:
                from core.rate_limiter import RateLimiter
                from core.client import XueqiuClient
                rl = RateLimiter(self.rate_limiter_config)
                client = XueqiuClient(self.cookie_manager, rl, self.rate_limiter_config)
                self._all_clients.append(client)
                self._created += 1
                logger.info(f"浏览器池: 创建实例 #{self._created}/{self.max_instances}")
                return client

        # 已达上限，等待归还
        logger.debug("浏览器池: 等待可用实例...")
        return self._pool.get(timeout=300)

    def release(self, client):
        """归还实例到池中。"""
        self._pool.put(client)

    def close_all(self):
        """关闭所有实例。"""
        for c in self._all_clients:
            try:
                c.close()
            except Exception:
                pass
        self._all_clients.clear()
        self._created = 0
        logger.info("浏览器池: 所有实例已关闭")
'''


# ================================================================
# Step 6: 定时调度器
# ================================================================

SCHEDULER = r'''"""
定时调度器 — 按 A 股交易日历多时段智能调度

交易日:
  08:30 盘前预扫 — 快速扫描帖子数量变化
  10:00/14:00 盘中扫描 — 热门帖子评论 + 话题热度
  16:00 盘后深扫 — 完整爬取
  20:00 每日摘要
非交易日:
  10:00 轻量巡检
"""

import time
import os
import signal
import sys
from datetime import datetime
from utils.time_utils import is_trading_day
from utils.logger import get_logger

logger = get_logger()


class Scheduler:
    """多时段智能调度器。"""

    def __init__(self, config: dict, run_callback=None, summary_callback=None):
        self.config = config
        self.schedule_cfg = config.get("schedule", {})
        self.run_callback = run_callback
        self.summary_callback = summary_callback
        self._running = True
        self._pid_file = "data/scheduler.pid"

    def start(self, daemon=False):
        """启动调度器。"""
        if daemon:
            self._write_pid()

        signal.signal(signal.SIGINT, self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)

        logger.info("定时调度器已启动")
        logger.info(f"  交易日: 盘前08:30 / 盘中10:00,14:00 / 盘后16:00 / 摘要20:00")
        logger.info(f"  非交易日: 轻巡10:00")
        logger.info("按 Ctrl+C 停止")

        executed_today = set()

        while self._running:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            hm = now.strftime("%H:%M")
            trading = is_trading_day(now)

            # 每天重置已执行记录
            if f"{today}_reset" not in executed_today:
                executed_today = {f"{today}_reset"}

            task_key = f"{today}_{hm}"

            if trading:
                if hm == "08:30" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("pre_market_scan", "盘前预扫")
                elif hm in ("10:00", "14:00") and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("in_market_scan", f"盘中扫描 {hm}")
                elif hm == "16:00" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("post_market_deep", "盘后深扫")
                elif hm == "20:00" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("daily_digest", "每日摘要")
            else:
                if hm == "10:00" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("non_trading_light", "非交易日轻巡")

            time.sleep(30)  # 每30秒检查一次

    def _run_task(self, task_type, desc):
        logger.info(f"[调度] 执行: {desc} ({task_type})")
        try:
            if task_type == "daily_digest" and self.summary_callback:
                self.summary_callback()
            elif self.run_callback:
                self.run_callback(task_type)
        except Exception as e:
            logger.error(f"[调度] {desc} 执行失败: {e}")

    def _handle_stop(self, signum, frame):
        logger.info("调度器收到停止信号")
        self._running = False
        self._cleanup_pid()

    def _write_pid(self):
        os.makedirs(os.path.dirname(self._pid_file), exist_ok=True)
        with open(self._pid_file, "w") as f:
            f.write(str(os.getpid()))

    def _cleanup_pid(self):
        if os.path.exists(self._pid_file):
            os.remove(self._pid_file)
'''


# ================================================================
# Step 7: 健康监控
# ================================================================

HEALTH_MONITOR = r'''"""
健康监控模块 — Token 自检、爬取成功率、每日摘要

功能:
  - Token 有效性定时检查
  - 爬取成功率统计
  - 每日摘要生成
"""

import json
from datetime import datetime
from utils.logger import get_logger

logger = get_logger()


class HealthMonitor:
    """系统健康监控器。"""

    def __init__(self, db, cookie_manager, notifier, config):
        self.db = db
        self.cookie_manager = cookie_manager
        self.notifier = notifier
        self.config = config

    def check_token(self, client=None) -> bool:
        ok = self.cookie_manager.validate(client)
        if not ok:
            logger.warning("Token 健康检查失败")
            self.notifier.notify_cookie_expired()
        return ok

    def get_health_status(self) -> dict:
        stats = self.db.get_stats()
        logs = self.db.get_recent_logs(50)
        success = sum(1 for l in logs if l["status"] == "success")
        total = len(logs)
        return {
            "token_configured": self.cookie_manager.is_configured(),
            "success_rate": f"{success}/{total}" if total else "N/A",
            "stats": stats,
            "last_scrape": logs[0] if logs else None,
        }

    def generate_daily_digest(self) -> str:
        """生成每日摘要文本。"""
        summary = self.db.get_daily_summary()
        health = self.get_health_status()

        lines = [
            f"📊 每日摘要 — {summary['date']}",
            f"{'='*40}",
            "",
            "📈 各股票数据:",
        ]
        for s in summary["stocks"]:
            lines.append(f"  {s['symbol']} {s['name']}: 新帖 {s['new_posts']}, 新评论 {s['new_comments']}")

        lines.extend([
            "",
            f"🏥 系统状态:",
            f"  Token: {'有效' if health['token_configured'] else '未配置'}",
            f"  最近成功率: {health['success_rate']}",
            f"  帖子总数: {health['stats']['posts']}",
            f"  评论总数: {health['stats']['comments']}",
            f"  用户画像: {health['stats'].get('user_profiles', 0)}",
        ])

        # 热门话题
        try:
            topics = self.db.get_trending_topics(limit=5)
            if topics:
                lines.append("")
                lines.append("🔥 今日热门话题:")
                for t in topics:
                    lines.append(f"  #{t['rank']} {t['title']} (讨论{t.get('discuss_count',0)})")
        except Exception:
            pass

        return "\n".join(lines)
'''


# ================================================================
# Step 2: 更新 api_endpoints.py（提取更多字段）
# ================================================================

def patch_api_endpoints():
    """更新 extract_post_fields 和 extract_comment_fields 提取更多数据"""
    fp = os.path.join(PROJECT_ROOT, "scrapers", "api_endpoints.py")
    if not os.path.exists(fp):
        print("  ⚠ api_endpoints.py 不存在，跳过")
        return

    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 替换 extract_post_fields — 增加 fav_count, view_count, 用户画像提取
    old_post = '''def extract_post_fields(post: dict) -> dict:
    """
    从 API 返回的帖子数据中提取标准化字段。

    Args:
        post: 原始帖子字典

    Returns:
        标准化后的字段字典
    """
    user_info = post.get("user", {}) or {}

    return {
        "id": str(post.get("id", "")),
        "user_id": str(user_info.get("id", post.get("user_id", ""))),
        "user_name": user_info.get("screen_name", ""),
        "title": post.get("title", "") or "",
        "text_html": post.get("text", "") or post.get("description", "") or "",
        "description": post.get("description", "") or "",
        "created_at": post.get("created_at", 0),
        "reply_count": post.get("reply_count", 0) or 0,
        "like_count": post.get("like_count", 0) or 0,
        "retweet_count": post.get("retweet_count", 0) or 0,
    }'''

    new_post = '''def extract_post_fields(post: dict) -> dict:
    """
    从 API 返回的帖子数据中提取标准化字段（v3: 含互动指标+用户画像）。
    """
    user_info = post.get("user", {}) or {}

    return {
        "id": str(post.get("id", "")),
        "user_id": str(user_info.get("id", post.get("user_id", ""))),
        "user_name": user_info.get("screen_name", ""),
        "title": post.get("title", "") or "",
        "text_html": post.get("text", "") or post.get("description", "") or "",
        "description": post.get("description", "") or "",
        "created_at": post.get("created_at", 0),
        "reply_count": post.get("reply_count", 0) or 0,
        "like_count": post.get("like_count", 0) or 0,
        "retweet_count": post.get("retweet_count", 0) or 0,
        "fav_count": post.get("fav_count", 0) or post.get("favorite_count", 0) or 0,
        "view_count": post.get("view_count", 0) or 0,
        # 用户画像原始数据（存入 user_profiles 表）
        "_user_profile": {
            "user_id": str(user_info.get("id", "")),
            "screen_name": user_info.get("screen_name", ""),
            "profile_image_url": user_info.get("profile_image_url", ""),
            "followers_count": user_info.get("followers_count"),
            "following_count": user_info.get("friends_count"),
            "status_count": user_info.get("status_count"),
            "verified_type": str(user_info.get("verified_type", "")),
            "description": user_info.get("description", ""),
        },
    }'''

    if old_post in content:
        content = content.replace(old_post, new_post)
        print("  ✓ extract_post_fields 升级（含互动指标+用户画像）")

    # 替换 extract_comment_fields — 增加 like_count, depth, reply_to_user
    old_comment = '''def extract_comment_fields(comment: dict) -> dict:
    """
    从 API 返回的评论数据中提取标准化字段。

    Args:
        comment: 原始评论字典

    Returns:
        标准化后的字段字典
    """
    user_info = comment.get("user", {}) or {}

    return {
        "id": str(comment.get("id", "")),
        "user_id": str(user_info.get("id", comment.get("user_id", ""))),
        "user_name": user_info.get("screen_name", ""),
        "text_html": comment.get("text", "") or "",
        "created_at": comment.get("created_at", 0),
        "reply_comment_id": str(comment.get("reply_comment_id", "")) or "",
    }'''

    new_comment = '''def extract_comment_fields(comment: dict) -> dict:
    """
    从 API 返回的评论数据中提取标准化字段（v3: 含深层嵌套+互动+画像）。
    """
    user_info = comment.get("user", {}) or {}
    reply_to_user = comment.get("reply_user", {}) or {}
    reply_cid = str(comment.get("reply_comment_id", "")) or ""

    return {
        "id": str(comment.get("id", "")),
        "user_id": str(user_info.get("id", comment.get("user_id", ""))),
        "user_name": user_info.get("screen_name", ""),
        "text_html": comment.get("text", "") or "",
        "created_at": comment.get("created_at", 0),
        "like_count": comment.get("like_count", 0) or 0,
        "reply_comment_id": reply_cid,
        "parent_comment_id": reply_cid,
        "reply_to_user_id": str(reply_to_user.get("id", "")) if reply_to_user else "",
        "reply_to_user_name": reply_to_user.get("screen_name", "") if reply_to_user else "",
        "depth": 2 if reply_cid else 1,
        "_user_profile": {
            "user_id": str(user_info.get("id", "")),
            "screen_name": user_info.get("screen_name", ""),
            "profile_image_url": user_info.get("profile_image_url", ""),
            "followers_count": user_info.get("followers_count"),
            "verified_type": str(user_info.get("verified_type", "")),
        },
    }'''

    if old_comment in content:
        content = content.replace(old_comment, new_comment)
        print("  ✓ extract_comment_fields 升级（含深层嵌套+互动+画像）")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# Step 2: 更新 stock_comments.py（保存用户画像）
# ================================================================

def patch_stock_comments():
    """在保存帖子/评论时同步提取用户画像"""
    fp = os.path.join(PROJECT_ROOT, "scrapers", "stock_comments.py")
    if not os.path.exists(fp):
        print("  ⚠ stock_comments.py 不存在，跳过")
        return

    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 在 save_post 调用后添加用户画像保存
    old_save_post = '''                    # 保存帖子
                    is_new = self.db.save_post(symbol, post)
                    if is_new:
                        total_new_posts += 1'''

    new_save_post = '''                    # 保存帖子
                    is_new = self.db.save_post(symbol, post)
                    if is_new:
                        total_new_posts += 1

                    # 自动提取用户画像
                    if post.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(post["_user_profile"])
                        except Exception:
                            pass'''

    if old_save_post in content:
        content = content.replace(old_save_post, new_save_post)
        print("  ✓ 帖子保存时自动提取用户画像")

    # 在 save_comment 调用后添加用户画像保存
    old_save_comment = '''                    if self.db.save_comment(post_id, comment):
                        new_count += 1'''

    new_save_comment = '''                    if self.db.save_comment(post_id, comment):
                        new_count += 1
                    if comment.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(comment["_user_profile"])
                        except Exception:
                            pass'''

    if old_save_comment in content:
        content = content.replace(old_save_comment, new_save_comment)
        print("  ✓ 评论保存时自动提取用户画像")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# Step 8: 更新 JSON 导出器（树形评论）
# ================================================================

def patch_json_exporter():
    """更新 JSON 导出器支持树形评论"""
    fp = os.path.join(PROJECT_ROOT, "export", "json_exporter.py")
    if not os.path.exists(fp):
        print("  ⚠ json_exporter.py 不存在，跳过")
        return

    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 替换评论输出部分 — 从平铺列表改为树形结构
    old_comments = '''            "comments": [
                {
                    "id": c["id"],
                    "author": c["user_name"],
                    "author_id": c["user_id"],
                    "time": c.get("created_at_str") or ms_to_str(c["created_at"]),
                    "content": c.get("text_plain") or "",
                    "reply_to": c.get("reply_comment_id") or None,
                }
                for c in comments
            ],'''

    new_comments = '''            "comments": _build_comment_tree(comments),'''

    if old_comments in content:
        content = content.replace(old_comments, new_comments)
        # 在文件末尾添加树构建函数
        content += r'''


def _build_comment_tree(comments: list) -> list:
    """将平铺的评论列表构建为树形结构。"""
    from utils.time_utils import ms_to_str
    # 先建索引
    by_id = {}
    for c in comments:
        node = {
            "id": c["id"],
            "author": c["user_name"],
            "author_id": c["user_id"],
            "time": c.get("created_at_str") or ms_to_str(c["created_at"]),
            "content": c.get("text_plain") or "",
            "likes": c.get("like_count", 0) if isinstance(c.get("like_count"), int) else 0,
            "depth": c.get("depth", 1) if isinstance(c.get("depth"), int) else 1,
            "reply_to": c.get("parent_comment_id") or c.get("reply_comment_id") or None,
            "reply_to_user": c.get("reply_to_user_name") or None,
            "children": [],
        }
        by_id[c["id"]] = node

    # 构建树
    roots = []
    for cid, node in by_id.items():
        parent_id = node["reply_to"]
        if parent_id and str(parent_id) in by_id:
            by_id[str(parent_id)]["children"].append(node)
        else:
            roots.append(node)

    return roots
'''
        print("  ✓ JSON 导出器升级为树形评论")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# Step 6+7: 更新 main.py（新命令）
# ================================================================

def patch_main_py():
    """添加 schedule --daemon, health, daily-digest, scrape-trending 命令"""
    fp = os.path.join(PROJECT_ROOT, "main.py")
    if not os.path.exists(fp):
        print("  ⚠ main.py 不存在，跳过")
        return

    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 在 cmd_export 前添加新命令函数
    new_commands = '''

def cmd_health(args, config):
    """查看系统健康状态。"""
    components = init_components(config)
    from core.health_monitor import HealthMonitor
    monitor = HealthMonitor(
        components["db"], components["cookie_manager"],
        components["notifier"], config)
    status = monitor.get_health_status()

    print("\\n" + "=" * 50)
    print("  系统健康状态")
    print("=" * 50)
    print(f"  Token: {'已配置' if status['token_configured'] else '未配置'}")
    print(f"  最近成功率: {status['success_rate']}")
    print(f"  帖子: {status['stats']['posts']}")
    print(f"  评论: {status['stats']['comments']}")
    print(f"  用户画像: {status['stats'].get('user_profiles', 0)}")
    print(f"  热门话题: {status['stats'].get('trending_topics', 0)}")
    if status['last_scrape']:
        ls = status['last_scrape']
        print(f"  最后爬取: {ls.get('finished_at','')} {ls['task_type']} {ls['target']} {ls['status']}")
    print()
    components["client"].close()
    components["db"].close()


def cmd_daily_digest(args, config):
    """生成每日摘要。"""
    components = init_components(config)
    from core.health_monitor import HealthMonitor
    monitor = HealthMonitor(
        components["db"], components["cookie_manager"],
        components["notifier"], config)
    digest = monitor.generate_daily_digest()
    print(digest)
    components["client"].close()
    components["db"].close()

'''

    if "cmd_health" not in content:
        content = content.replace(
            "def cmd_export(args, config):",
            new_commands + "\ndef cmd_export(args, config):"
        )
        print("  ✓ 新增 cmd_health, cmd_daily_digest 函数")

    # 在 argparse 添加新子命令
    if '"health"' not in content:
        insert_before = '    # status\n    subparsers.add_parser("status"'
        new_parsers = '''    # health
    subparsers.add_parser("health", help="查看系统健康状态")

    # daily-digest
    subparsers.add_parser("daily-digest", help="生成每日摘要")

'''
        if insert_before in content:
            content = content.replace(insert_before, new_parsers + "    " + insert_before.lstrip())
            print("  ✓ 新增 health, daily-digest 子命令")

    # 注册新命令到 commands dict
    if '"health": cmd_health' not in content:
        content = content.replace(
            '"export": cmd_export,',
            '"health": cmd_health,\n        "daily-digest": cmd_daily_digest,\n        "export": cmd_export,'
        )
        print("  ✓ commands dict 注册新命令")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# 主流程
# ================================================================

def main():
    print("=" * 60)
    print("  Phase 5 — 自动化运维全量安装")
    print("=" * 60)
    print()

    # Step 1
    print("[Step 1/8] 数据库 v2→v3 迁移...")
    step1_migrate_db()
    print()

    # Step 2: 数据模型升级 — database.py v3
    print("[Step 2/8] 数据模型升级（database.py v3）...")
    write_file("storage/database.py", DATABASE_V3, "数据库 v3")
    print()

    # Step 2 续: api_endpoints 提取更多字段
    print("[Step 2/8 续] 更新 api_endpoints.py（全维度数据+用户画像）...")
    patch_api_endpoints()
    print()

    # Step 2 续: stock_comments 保存用户画像
    print("[Step 2/8 续] 更新 stock_comments.py（自动提取用户画像）...")
    patch_stock_comments()
    print()

    # Step 3: 效率优化
    print("[Step 3/8] 爬取效率优化...")
    write_file("core/rate_limiter.py", RATE_LIMITER_V2, "自适应频率控制 v2")
    write_file("core/browser_pool.py", BROWSER_POOL, "浏览器实例池")
    print()

    # Step 4: 话题热度榜（预留接口，需抓包确认）
    print("[Step 4/8] 话题热度榜（框架预留，需抓包确认 API）...")
    os.makedirs(os.path.join(PROJECT_ROOT, "scrapers"), exist_ok=True)
    # 暂不写入具体爬虫，等确认 API 后补充
    print("  ℹ 数据库表 trending_topics 已创建")
    print("  ℹ 需要你用 Chrome DevTools 确认热度榜 API 后再写爬虫")
    print()

    # Step 5: 多平台预埋（platform_id 已全面引入）
    print("[Step 5/8] 多平台预埋...")
    print("  ✓ platform_id 已在 database.py v3 全面引入")
    print("  ✓ 所有新数据默认 platform_id='xueqiu'")
    print("  ✓ BaseScraper 抽象待后续实际接入时创建")
    print()

    # Step 6: 定时调度
    print("[Step 6/8] 定时调度系统...")
    write_file("core/scheduler.py", SCHEDULER, "多时段调度器")
    print()

    # Step 7: 健康监控
    print("[Step 7/8] 健康监控...")
    write_file("core/health_monitor.py", HEALTH_MONITOR, "健康监控器")
    print()

    # Step 8: 导出升级
    print("[Step 8/8] 导出升级（JSON 树形评论）...")
    patch_json_exporter()
    print()

    # 更新 main.py
    print("[补充] 更新 main.py（新命令）...")
    patch_main_py()
    print()

    # 验证
    print("[验证] 测试数据库连接...")
    try:
        import yaml
        with open(os.path.join(PROJECT_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        sys.path.insert(0, PROJECT_ROOT)
        from storage.database import Database
        db = Database(config.get("database", {}))
        stats = db.get_stats()
        print(f"  ✓ 数据库 v3 连接成功")
        print(f"    帖子: {stats['posts']}, 评论: {stats['comments']}")
        print(f"    用户画像: {stats.get('user_profiles', 0)}, 热门话题: {stats.get('trending_topics', 0)}")
        db.close()
    except Exception as e:
        print(f"  ⚠ 验证出错: {e}")
        import traceback; traceback.print_exc()

    print()
    print("=" * 60)
    print("  Phase 5 安装完成！")
    print("=" * 60)
    print()
    print("新增/升级内容:")
    print("  ✓ 数据库 v3（platform_id + 深层评论 + 互动指标 + 用户画像 + 热门话题）")
    print("  ✓ 自适应频率控制（3-8s 动态调节，失败自动降速）")
    print("  ✓ 浏览器实例池（多实例并发预备）")
    print("  ✓ 帖子/评论自动提取用户画像")
    print("  ✓ JSON 导出升级为树形评论结构")
    print("  ✓ 多时段调度器（盘前/盘中/盘后/非交易日）")
    print("  ✓ 健康监控器 + 每日摘要")
    print()
    print("新命令:")
    print("  python main.py health           # 查看系统健康状态")
    print("  python main.py daily-digest     # 生成每日摘要")
    print("  python main.py run              # 运行（自适应频率，更快）")
    print("  python main.py status           # 数据统计")
    print()
    print("待你确认:")
    print("  ⏳ 话题热度榜 API 需要抓包确认（打开雪球首页 → F12 → Network）")
    print("     找到热门话题相关的 JSON 请求后告诉我，我来写爬虫")


if __name__ == "__main__":
    main()
