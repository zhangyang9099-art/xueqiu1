#!/usr/bin/env python3
"""
雪球评论区爬虫 — 主程序入口

核心命令:
  python main.py scrape --stocks 振华科技 西藏矿业   # 运行时指定目标
  python main.py scrape --stocks 振华科技 --mode history --pages 100
  python main.py scrape --all                         # 所有已监控的
  python main.py run                                  # 等同 scrape --all

管理命令:
  python main.py login                    # 浏览器登录获取 Token
  python main.py add-stock 茅台           # 添加监控股票
  python main.py add-user 罗洄头          # 添加跟踪用户
  python main.py status                   # 系统状态
  python main.py export --format json     # 导出数据
"""

import os
import sys
import argparse
import sqlite3
import json
import time
from copy import deepcopy
from datetime import datetime

import yaml

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
USER_SYNC_STATE_PATH = os.path.join(PROJECT_ROOT, "data", "run_reports", "user_sync_state.json")
sys.path.insert(0, PROJECT_ROOT)

from utils.logger import setup_logger, get_logger
from utils.notifier import Notifier
from utils.run_reporter import write_comment_backfill_report
from utils.query_progress import (
    format_duration,
    get_comment_backfill_overview,
    get_history_queue_overview,
    get_user_scrape_overview,
)
from core.rate_limiter import RateLimiter
from core.cookie_manager import CookieManager
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.client import XueqiuClient
from core.exceptions import CookieExpired
from storage.database import Database
from scrapers.stock_comments import StockCommentScraper
from scrapers.user_tracker import UserTracker


# ================================================================
# 配置与初始化
# ================================================================

def load_config(config_path: str = "config.yaml") -> dict:
    path = os.path.join(PROJECT_ROOT, config_path)
    if not os.path.exists(path):
        print(f"[错误] 配置文件不存在: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_components(config: dict):
    scraping_cfg = config.get("scraping", {})
    db_cfg = config.get("database", {})
    logger = setup_logger(config)
    cookie_manager = CookieManager(config, config_path="config.yaml")
    rate_limiter = RateLimiter(scraping_cfg)
    client = XueqiuClient(cookie_manager, rate_limiter, scraping_cfg)
    db = Database(db_cfg)
    notifier = Notifier(config)
    stock_scraper = StockCommentScraper(client, db, scraping_cfg)
    user_tracker = UserTracker(client, db, scraping_cfg)
    return {
        "config": config, "logger": logger,
        "cookie_manager": cookie_manager, "rate_limiter": rate_limiter,
        "client": client, "db": db, "notifier": notifier,
        "stock_scraper": stock_scraper, "user_tracker": user_tracker,
    }


def _build_comment_backfill_runtime_config(config: dict) -> dict:
    runtime = deepcopy(config)
    scraping_cfg = runtime.setdefault("scraping", {})

    history_min = float(scraping_cfg.get("history_min_request_interval_seconds", 6) or 6)
    history_max = float(scraping_cfg.get("history_max_request_interval_seconds", 12) or 12)
    history_burst_count = int(scraping_cfg.get("history_burst_rest_count", 30) or 30)
    history_burst_min = int(scraping_cfg.get("history_burst_rest_seconds_min", 120) or 120)
    history_burst_max = int(scraping_cfg.get("history_burst_rest_seconds_max", 240) or 240)

    scraping_cfg["use_persistent_context"] = True
    scraping_cfg["min_request_interval"] = float(
        scraping_cfg.get("comment_backfill_min_request_interval_seconds", history_min) or history_min
    )
    scraping_cfg["max_request_interval"] = float(
        scraping_cfg.get("comment_backfill_max_request_interval_seconds", history_max) or history_max
    )
    scraping_cfg["burst_rest_count"] = int(
        scraping_cfg.get("comment_backfill_burst_rest_count", history_burst_count) or history_burst_count
    )
    scraping_cfg["burst_rest_seconds_min"] = int(
        scraping_cfg.get("comment_backfill_burst_rest_seconds_min", history_burst_min) or history_burst_min
    )
    scraping_cfg["burst_rest_seconds_max"] = int(
        scraping_cfg.get("comment_backfill_burst_rest_seconds_max", history_burst_max) or history_burst_max
    )
    scraping_cfg["comment_mode_enabled"] = bool(
        scraping_cfg.get("comment_backfill_comment_mode_enabled", False)
    )
    scraping_cfg["comment_mode_min_interval"] = float(
        scraping_cfg.get("comment_backfill_comment_mode_min_interval_seconds", scraping_cfg["min_request_interval"])
        or scraping_cfg["min_request_interval"]
    )
    scraping_cfg["comment_mode_max_interval"] = float(
        scraping_cfg.get("comment_backfill_comment_mode_max_interval_seconds", scraping_cfg["max_request_interval"])
        or scraping_cfg["max_request_interval"]
    )
    scraping_cfg["comment_post_budget_seconds"] = float(
        scraping_cfg.get("comment_backfill_post_budget_seconds", 75) or 75
    )
    return runtime


def _build_user_sync_runtime_config(config: dict) -> dict:
    runtime = deepcopy(config)
    scraping_cfg = runtime.setdefault("scraping", {})

    history_min = float(scraping_cfg.get("history_min_request_interval_seconds", 6) or 6)
    history_max = float(scraping_cfg.get("history_max_request_interval_seconds", 12) or 12)
    history_burst_count = int(scraping_cfg.get("history_burst_rest_count", 30) or 30)
    history_burst_min = int(scraping_cfg.get("history_burst_rest_seconds_min", 120) or 120)
    history_burst_max = int(scraping_cfg.get("history_burst_rest_seconds_max", 240) or 240)

    scraping_cfg["use_persistent_context"] = True
    # 用户时间线接口对真实登录态和真实浏览器环境更敏感；
    # 这里强制改为非 headless，并尽量复用系统 Chrome。
    scraping_cfg["browser_headless"] = False
    scraping_cfg["browser_channel"] = scraping_cfg.get("browser_channel", "chrome") or "chrome"
    # 用户/KOL 模式节奏与评论回填稳态对齐，避免继续使用全局 2-5s 的激进档位。
    scraping_cfg["min_request_interval"] = float(
        scraping_cfg.get("user_sync_min_request_interval_seconds",
                         scraping_cfg.get("comment_backfill_min_request_interval_seconds", history_min))
        or scraping_cfg.get("comment_backfill_min_request_interval_seconds", history_min)
    )
    scraping_cfg["max_request_interval"] = float(
        scraping_cfg.get("user_sync_max_request_interval_seconds",
                         scraping_cfg.get("comment_backfill_max_request_interval_seconds", history_max))
        or scraping_cfg.get("comment_backfill_max_request_interval_seconds", history_max)
    )
    scraping_cfg["burst_rest_count"] = int(
        scraping_cfg.get("user_sync_burst_rest_count",
                         scraping_cfg.get("comment_backfill_burst_rest_count", history_burst_count))
        or scraping_cfg.get("comment_backfill_burst_rest_count", history_burst_count)
    )
    scraping_cfg["burst_rest_seconds_min"] = int(
        scraping_cfg.get("user_sync_burst_rest_seconds_min",
                         scraping_cfg.get("comment_backfill_burst_rest_seconds_min", history_burst_min))
        or scraping_cfg.get("comment_backfill_burst_rest_seconds_min", history_burst_min)
    )
    scraping_cfg["burst_rest_seconds_max"] = int(
        scraping_cfg.get("user_sync_burst_rest_seconds_max",
                         scraping_cfg.get("comment_backfill_burst_rest_seconds_max", history_burst_max))
        or scraping_cfg.get("comment_backfill_burst_rest_seconds_max", history_burst_max)
    )
    scraping_cfg["comment_mode_enabled"] = bool(
        scraping_cfg.get("comment_backfill_comment_mode_enabled", False)
    )
    scraping_cfg["comment_mode_min_interval"] = float(
        scraping_cfg.get(
            "comment_backfill_comment_mode_min_interval_seconds",
            scraping_cfg["min_request_interval"],
        ) or scraping_cfg["min_request_interval"]
    )
    scraping_cfg["comment_mode_max_interval"] = float(
        scraping_cfg.get(
            "comment_backfill_comment_mode_max_interval_seconds",
            scraping_cfg["max_request_interval"],
        ) or scraping_cfg["max_request_interval"]
    )
    return runtime


def sync_config_to_db(config: dict, db: Database):
    for stock in config.get("stocks", []):
        db.upsert_stock(stock["symbol"], stock.get("name", ""))
    for user in config.get("tracked_users", []):
        db.upsert_tracked_user(
            user["user_id"], user.get("screen_name", ""), user.get("note", ""),
        )


def normalize_stock_symbol(query: str, db: Database = None, client=None) -> str:
    """把用户输入的股票名/裸代码规范成库内 symbol。"""
    if not query:
        return query

    raw = query.strip()
    upper = raw.upper()

    if db:
        row = db.conn.execute(
            """
            SELECT symbol
            FROM watched_stocks
            WHERE symbol = ?
               OR symbol LIKE ?
               OR name = ?
            ORDER BY CASE
                WHEN symbol = ? THEN 0
                WHEN name = ? THEN 1
                WHEN symbol LIKE ? THEN 2
                ELSE 3
            END
            LIMIT 1
            """,
            (upper, f"%{upper}", raw, upper, raw, f"%{upper}"),
        ).fetchone()
        if row and row["symbol"]:
            return row["symbol"]

    if upper.isdigit() and len(upper) == 6:
        prefix = "SH" if upper[0] in ("5", "6", "9") else "SZ"
        return prefix + upper

    from utils.stock_resolver import resolve_stock

    candidates = resolve_stock(raw, client=client)
    if candidates:
        return candidates[0][0]

    return upper


# ================================================================
# run 命令（爬所有已监控，保留向后兼容）
# ================================================================

def run_full_scrape(components: dict):
    logger = get_logger()
    config = components["config"]
    db = components["db"]
    cookie_manager = components["cookie_manager"]
    stock_scraper = components["stock_scraper"]
    user_tracker = components["user_tracker"]
    notifier = components["notifier"]

    logger.info("=" * 60)
    logger.info("开始执行完整爬取任务")
    logger.info("=" * 60)

    if not cookie_manager.is_configured():
        logger.error("Cookie 未配置。")
        return

    sync_config_to_db(config, db)

    # 并发爬取股票
    stocks = db.get_watched_stocks()
    stock_results = []
    max_workers = min(2, len(stocks))

    def _scrape_one_stock(stock_info):
        sym, name = stock_info["symbol"], stock_info.get("name", "")
        scraping_cfg = config.get("scraping", {})
        rl = RateLimiter(scraping_cfg)
        cl = XueqiuClient(cookie_manager, rl, scraping_cfg)
        sc = StockCommentScraper(cl, db, scraping_cfg)
        try:
            return sc.scrape_stock(sym, name)
        finally:
            cl.close()

    if max_workers > 1 and len(stocks) > 1:
        # 主线程预初始化 Playwright 防死锁
        pre_cl = XueqiuClient(cookie_manager, RateLimiter(config.get("scraping", {})), config.get("scraping", {}))
        try:
            pre_cl._ensure_browser()
        finally:
            pre_cl.close()

        logger.info(f"启动并发爬取: {max_workers} 线程, {len(stocks)} 只股票")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_scrape_one_stock, s): s for s in stocks}
            for future in as_completed(futures):
                stock_info = futures[future]
                try:
                    stock_results.append(future.result())
                except CookieExpired:
                    logger.error("Cookie 已失效，中断爬取。")
                    notifier.notify_cookie_expired()
                    break
                except Exception as e:
                    logger.error(f"爬取 {stock_info['symbol']} 失败: {e}")
    else:
        for stock in stocks:
            try:
                result = stock_scraper.scrape_stock(stock["symbol"], stock.get("name", ""))
                stock_results.append(result)
            except CookieExpired:
                logger.error("Cookie 已失效。")
                notifier.notify_cookie_expired()
                break
            except Exception as e:
                logger.error(f"爬取 {stock['symbol']} 失败: {e}")

    # 用户跟踪
    tracked = db.get_tracked_users()
    user_results = []
    for user in tracked:
        try:
            result = user_tracker.track_user(user["user_id"], user.get("screen_name", ""))
            user_results.append(result)
        except CookieExpired:
            logger.error("Cookie 已失效。")
            notifier.notify_cookie_expired()
            break
        except Exception as e:
            logger.error(f"跟踪用户 {user['user_id']} 失败: {e}")

    # 汇总
    total_posts = sum(r.get("new_posts", 0) for r in stock_results)
    total_comments = sum(r.get("new_comments", 0) for r in stock_results)
    total_statuses = sum(r.get("new_statuses", 0) for r in user_results)
    logger.info(f"合计: {total_posts}帖 {total_comments}评论 {total_statuses}发言")


# ================================================================
# 各命令处理函数
# ================================================================

def cmd_login(args, config):
    from core.auto_login import auto_login
    token = auto_login(config_path="config.yaml")
    if token:
        print("\n现在可以运行 python main.py test-cookie 验证")


def cmd_run(args, config):
    components = init_components(config)
    try:
        run_full_scrape(components)
    finally:
        components["client"].close()
        components["db"].close()


def cmd_schedule(args, config):
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        print("[错误] 请先安装 apscheduler: pip install apscheduler")
        sys.exit(1)

    components = init_components(config)
    logger = get_logger()
    scraping_cfg = config.get("scraping", {})
    hour = scraping_cfg.get("schedule_hour", 3)
    minute = scraping_cfg.get("schedule_minute", 0)

    scheduler = BlockingScheduler()

    def scheduled_job():
        try:
            new_config = load_config()
            components["config"] = new_config
            sync_config_to_db(new_config, components["db"])
        except Exception as e:
            logger.warning(f"重新加载配置失败: {e}")
        try:
            run_full_scrape(components)
        except Exception as e:
            logger.error(f"定时任务执行失败: {e}")

    scheduler.add_job(scheduled_job, trigger="cron", hour=hour, minute=minute)
    logger.info(f"定时调度已启动，每天 {hour:02d}:{minute:02d} 执行")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("定时调度已停止")
    finally:
        components["db"].close()


def cmd_add_stock(args, config):
    from utils.stock_resolver import resolve_stock, format_candidates
    components = init_components(config)
    db, client = components["db"], components["client"]
    query = args.symbol
    name_arg = args.name or ""

    if (query.upper().startswith("SH") or query.upper().startswith("SZ")) and len(query) == 8:
        db.upsert_stock(query.upper(), name_arg)
        print(f"✓ 已添加: {query.upper()} {name_arg}")
        components["client"].close(); db.close()
        return

    candidates = resolve_stock(query, client=client)
    if not candidates:
        print(f"未找到: {query}")
        components["client"].close(); db.close()
        return

    if len(candidates) == 1 or candidates[0][2] in ("exact", "alias"):
        symbol, name, _ = candidates[0]
        db.upsert_stock(symbol, name)
        print(f"✓ 已添加: {symbol} {name}")
    else:
        print(f"找到多个匹配:\n{format_candidates(candidates)}\n")
        try:
            choice = input("请输入序号（回车选第1个）: ").strip()
            idx = int(choice) - 1 if choice else 0
            if 0 <= idx < len(candidates):
                symbol, name, _ = candidates[idx]
                db.upsert_stock(symbol, name)
                print(f"✓ 已添加: {symbol} {name}")
        except (ValueError, EOFError):
            symbol, name, _ = candidates[0]
            db.upsert_stock(symbol, name)
            print(f"✓ 已添加: {symbol} {name}")

    components["client"].close(); db.close()


def cmd_add_user(args, config):
    components = init_components(config)
    db, client = components["db"], components["client"]
    query = args.user_id
    name_arg = args.name or ""

    if query.isdigit():
        db.upsert_tracked_user(query, name_arg, args.note or "")
        print(f"✓ 已添加: {query} {name_arg}")
        components["client"].close(); db.close()
        return

    print(f"正在搜索用户: {query}...")
    from utils.user_resolver import search_xueqiu_user, format_user_candidates
    users = search_xueqiu_user(client, query)
    if not users:
        print(f"未找到: {query}")
        components["client"].close(); db.close()
        return

    print(format_user_candidates(users) + "\n")
    try:
        choice = input("请输入序号（回车选第1个）: ").strip()
        idx = int(choice) - 1 if choice else 0
        if 0 <= idx < len(users):
            u = users[idx]
            db.upsert_tracked_user(u["id"], u["name"], args.note or "")
            print(f"✓ 已添加: {u['id']} {u['name']}")
    except (ValueError, EOFError):
        u = users[0]
        db.upsert_tracked_user(u["id"], u["name"], args.note or "")
        print(f"✓ 已添加: {u['id']} {u['name']}")

    components["client"].close(); db.close()


def cmd_remove_stock(args, config):
    components = init_components(config)
    db = components["db"]
    db.conn.execute("UPDATE watched_stocks SET is_active=0 WHERE symbol=?", (args.symbol.upper(),))
    db.conn.commit()
    print(f"✓ 已停止监控: {args.symbol.upper()}")
    db.close()


def cmd_remove_user(args, config):
    components = init_components(config)
    db = components["db"]
    db.conn.execute("UPDATE tracked_users SET is_active=0 WHERE user_id=?", (args.user_id,))
    db.conn.commit()
    print(f"✓ 已停止跟踪: {args.user_id}")
    db.close()


def cmd_status(args, config):
    db_path = os.path.join(PROJECT_ROOT, config.get("database", {}).get("path", "data/xueqiu.db"))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    stats = {}
    for table in ["posts", "comments", "user_statuses", "scrape_logs"]:
        stats[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    stats["watched_stocks"] = conn.execute(
        "SELECT COUNT(*) AS c FROM watched_stocks WHERE is_active=1"
    ).fetchone()["c"]
    stats["tracked_users"] = conn.execute(
        "SELECT COUNT(*) AS c FROM tracked_users WHERE is_active=1"
    ).fetchone()["c"]
    try:
        stats["user_profiles"] = conn.execute("SELECT COUNT(*) AS c FROM user_profiles").fetchone()["c"]
    except sqlite3.OperationalError:
        stats["user_profiles"] = 0
    try:
        stats["trending_topics"] = conn.execute("SELECT COUNT(*) AS c FROM trending_topics").fetchone()["c"]
    except sqlite3.OperationalError:
        stats["trending_topics"] = 0

    stocks = [dict(r) for r in conn.execute(
        """
        SELECT w.symbol, w.name, w.history_complete, w.history_stagnant_runs,
               MIN(p.created_at) AS first_post_time,
               MAX(p.created_at) AS latest_post_time,
               COUNT(DISTINCT p.id) AS total_posts,
               COALESCE(SUM(p.comments_scraped), 0) AS total_comments
        FROM watched_stocks w
        LEFT JOIN posts p ON p.symbol = w.symbol
        WHERE w.is_active=1
        GROUP BY w.symbol, w.name, w.history_complete, w.history_stagnant_runs
        ORDER BY w.symbol
        """
    ).fetchall()]
    users = [dict(r) for r in conn.execute(
        """
        SELECT t.user_id, t.screen_name, t.note, t.history_complete, t.history_stagnant_runs,
               t.history_cursor_page, t.last_check_time, t.last_sync_time,
               MIN(u.created_at) AS first_status_time,
               MAX(u.created_at) AS latest_status_time,
               COUNT(u.id) AS total_statuses,
               COALESCE(SUM(u.reply_count), 0) AS total_replies
        FROM tracked_users t
        LEFT JOIN user_statuses u ON u.user_id = t.user_id
        WHERE t.is_active=1
        GROUP BY t.user_id, t.screen_name, t.note, t.history_complete, t.history_stagnant_runs,
                 t.history_cursor_page, t.last_check_time, t.last_sync_time
        ORDER BY t.user_id
        """
    ).fetchall()]
    recent_logs = [dict(r) for r in conn.execute(
        "SELECT * FROM scrape_logs ORDER BY id DESC LIMIT 10"
    ).fetchall()]

    print("\n" + "=" * 50)
    print("  雪球爬虫系统状态")
    print("=" * 50)
    print(f"\n📊 数据统计:")
    print(f"  帖子: {stats['posts']}  评论: {stats['comments']}  "
          f"用户发言: {stats['user_statuses']}  日志: {stats['scrape_logs']}")

    print(f"\n📈 监控股票 ({len(stocks)}):")
    for s in stocks:
        lt = ""
        ot = ""
        if s.get("latest_post_time"):
            lt = datetime.fromtimestamp(s["latest_post_time"] / 1000).strftime("%Y-%m-%d %H:%M")
        if s.get("first_post_time"):
            ot = datetime.fromtimestamp(s["first_post_time"] / 1000).strftime("%Y-%m-%d %H:%M")
        history_tag = "已触底" if s.get("history_complete") else "未触底"
        print(
            f"  {s['symbol']:>10} {s.get('name', ''):10} "
            f"范围: {ot or '无数据'} ~ {lt or '无数据'} "
            f"帖{int(s.get('total_posts', 0) or 0)} 评{int(s.get('total_comments', 0) or 0)} "
            f"{history_tag}/停滞{int(s.get('history_stagnant_runs', 0) or 0)}"
        )

    print(f"\n👤 跟踪用户 ({len(users)}):")
    for u in users:
        lt = ""
        ot = ""
        if u.get("latest_status_time"):
            lt = datetime.fromtimestamp(u["latest_status_time"] / 1000).strftime("%Y-%m-%d %H:%M")
        if u.get("first_status_time"):
            ot = datetime.fromtimestamp(u["first_status_time"] / 1000).strftime("%Y-%m-%d %H:%M")
        history_tag = "已触底" if u.get("history_complete") else "未触底"
        cursor = int(u.get("history_cursor_page", 0) or 0)
        last_sync = ""
        if u.get("last_sync_time"):
            last_sync = datetime.fromtimestamp(u["last_sync_time"] / 1000).strftime("%m-%d %H:%M")
        print(
            f"  {u['user_id']:>14} {u.get('screen_name', ''):10} "
            f"范围: {ot or '无数据'} ~ {lt or '无数据'} "
            f"发言{int(u.get('total_statuses', 0) or 0)} 评{int(u.get('total_replies', 0) or 0)} "
            f"{history_tag}/停滞{int(u.get('history_stagnant_runs', 0) or 0)}"
            f"/cursor={cursor}"
            f"{'/同步' + last_sync if last_sync else ''}"
        )

    if recent_logs:
        print(f"\n📋 最近日志:")
        for log in recent_logs[:5]:
            print(
                f"  [{log.get('finished_at', '')[:16]}] "
                f"{log['task_type']:16} {log['target']:12} "
                f"状态={log['status']:8} 新增={log['new_items_count']}"
            )
            if log.get("error_message"):
                print(f"    └ {log['error_message'][:80]}")

    comment_progress = get_comment_backfill_overview(completed_limit=3)
    history_progress = get_history_queue_overview()
    user_progress = get_user_scrape_overview()

    print(f"\n⏱ 运行进展:")
    if comment_progress.get("exists") and comment_progress.get("active_entry"):
        print(
            f"  评论回填: {comment_progress.get('active_name') or comment_progress.get('active_symbol')} "
            f"| 当前剩余 {format_duration(comment_progress['eta'].get('current_remaining_seconds'))} "
            f"| 队列剩余 {format_duration(comment_progress['eta'].get('queue_remaining_seconds'))}"
        )
    else:
        print("  评论回填: 当前无活动队列")
    if history_progress.get("exists") and history_progress.get("current_batch_info"):
        current_name = history_progress["current_batch_info"].get("current_stock_name") or "未知"
        current_page = history_progress["current_batch_info"].get("current_page") or 0
        print(
            f"  历史批跑: {current_name} 第{current_page}页 "
            f"| 当前批剩余 {format_duration(history_progress['eta'].get('current_batch_remaining_seconds'))} "
            f"| 队列剩余 {format_duration(history_progress['eta'].get('queue_remaining_seconds'))}"
        )
    else:
        print("  历史批跑: 当前无活动队列")
    if user_progress.get("active_row"):
        active_row = user_progress["active_row"]
        print(
            f"  用户模式: {active_row.get('screen_name') or active_row.get('user_id')} "
            f"第{int(active_row.get('runtime_page') or 0)}页 "
            f"| 当前剩余 {format_duration(user_progress['eta'].get('current_remaining_seconds'))} "
            f"| 队列剩余 {format_duration(user_progress['eta'].get('queue_remaining_seconds'))}"
        )
    else:
        print("  用户模式: 当前无活动队列")
    print()
    conn.close()


def cmd_test_cookie(args, config):
    components = init_components(config)
    client = components["client"]
    cm = components["cookie_manager"]
    if not cm.is_configured():
        print("✗ Cookie 未配置")
        components["client"].close()
        components["db"].close()
        return
    print("正在验证 Cookie...")
    ok = cm.validate(client)
    diagnostics = cm.get_cookie_diagnostics()
    print("✓ Cookie 有效" if ok else "✗ Cookie 无效或已失效")
    print(f"Cookie 文件: {diagnostics['cookie_file']}")
    print(f"Cookie 数量: {diagnostics['cookie_count']}")
    if diagnostics["missing_required"]:
        print("缺少关键 Cookie: " + ", ".join(diagnostics["missing_required"]))
    else:
        print("关键 Cookie 完整")
    components["client"].close(); components["db"].close()


def _write_user_sync_state(payload: dict):
    os.makedirs(os.path.dirname(USER_SYNC_STATE_PATH), exist_ok=True)
    with open(USER_SYNC_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _clear_user_sync_state():
    try:
        if os.path.exists(USER_SYNC_STATE_PATH):
            os.remove(USER_SYNC_STATE_PATH)
    except OSError:
        pass


def cmd_backfill_comments(args, config):
    runtime_config = _build_comment_backfill_runtime_config(config)
    components = init_components(runtime_config)
    scraper = components["stock_scraper"]
    symbol = normalize_stock_symbol(args.symbol, db=components["db"], client=components["client"]) if args.symbol else None
    days = args.days
    max_posts = getattr(args, "max_posts", None)
    post_id = getattr(args, "post_id", None)
    scope = "全历史" if days in (None, 0) else f"最近 {days} 天"
    started_at = datetime.now().timestamp()
    result = {"total_posts": 0, "new_comments": 0}
    report_status = "failed"
    report_error = ""
    print(f"开始评论回填（{scope}" + (f", {symbol}" if symbol else ", 全部") + "）...")
    try:
        if post_id:
            result = scraper.backfill_one_post(post_id=post_id, symbol=symbol)
        else:
            result = scraper.backfill_comments(symbol=symbol, days=days, max_posts=max_posts)
        print(f"✓ 回填完成: {result['total_posts']}帖, 新增 {result['new_comments']}评论")
        report_status = "success"
    except CookieExpired:
        print("✗ Cookie 已失效")
        report_error = "Cookie 已失效"
    except Exception as e:
        print(f"✗ 回填失败: {e}")
        report_error = str(e)
    finally:
        try:
            paths = write_comment_backfill_report(
                PROJECT_ROOT,
                components["db"],
                symbol=symbol,
                days=days,
                max_posts=max_posts,
                post_id=post_id,
                result=result,
                started_at=started_at,
                status=report_status,
                error=report_error,
            )
            print(f"运行摘要已写入: {paths['latest_md']}")
        except Exception as report_exc:
            print(f"⚠ 写运行摘要失败: {report_exc}")
        components["client"].close(); components["db"].close()


def cmd_audit_completeness(args, config):
    components = init_components(config)
    db = components["db"]
    scraper = components["stock_scraper"]
    symbol = args.symbol.upper() if args.symbol else None
    user_id = args.user_id if getattr(args, "user_id", None) else None
    days = args.days

    try:
        stock_reports = db.get_stock_completeness_report(symbol=symbol, days=days)
        user_reports = db.get_user_completeness_report(user_id=user_id)

        print("\n" + "=" * 60)
        print("  完整性审计")
        print("=" * 60)

        if stock_reports:
            print("\n📈 股票评论完整性:")
            for report in stock_reports:
                last_scrape = ""
                oldest = ""
                if report.get("last_scrape_time"):
                    last_scrape = datetime.fromtimestamp(report["last_scrape_time"] / 1000).strftime("%Y-%m-%d %H:%M")
                if report.get("oldest_post_time"):
                    oldest = datetime.fromtimestamp(report["oldest_post_time"] / 1000).strftime("%Y-%m-%d")
                print(
                    f"  {report['symbol']} {report.get('name', '')}: "
                    f"帖子{report['total_posts']} 声称评论{report['claimed_comments']} 已抓{report['comments_scraped']} "
                    f"缺口{report['missing_comments']} 缺口帖子{report['gap_posts']} 孤儿评论{report['orphan_comments']} "
                    f"跨帖回复{report.get('cross_post_replies', 0)} "
                    f"深度{report['max_comment_depth']} 最新{last_scrape or '未知'} 最老{oldest or '未知'} "
                    f"{'已触底' if report.get('history_complete') else '未触底'}"
                )
        else:
            print("\n📈 股票评论完整性: 无匹配股票")

        if user_reports:
            print("\n👤 用户时间线完整性:")
            for report in user_reports:
                latest = ""
                oldest = ""
                if report.get("latest_status_time"):
                    latest = datetime.fromtimestamp(report["latest_status_time"] / 1000).strftime("%Y-%m-%d %H:%M")
                if report.get("first_status_time"):
                    oldest = datetime.fromtimestamp(report["first_status_time"] / 1000).strftime("%Y-%m-%d %H:%M")
                print(
                    f"  {report['user_id']} {report.get('screen_name', '')}: "
                    f"发言{report['total_statuses']} 最新{latest or '未知'} 最老{oldest or '未知'} "
                    f"{'已触底' if report.get('history_complete') else '未触底'}"
                )

        if getattr(args, "fix_comments", False):
            result = scraper.backfill_comments(symbol=symbol, days=days)
            print(f"\n🔧 自动修复评论缺口: 处理 {result['total_posts']} 帖, 新增 {result['new_comments']} 评论")
    finally:
        components["client"].close()
        db.close()


def cmd_health(args, config):
    components = init_components(config)
    from core.health_monitor import HealthMonitor
    monitor = HealthMonitor(
        components["db"], components["cookie_manager"],
        components["notifier"], config)
    status = monitor.get_health_status()
    print("\n" + "=" * 50)
    print("  系统健康状态")
    print("=" * 50)
    print(f"  Token: {'已配置' if status['token_configured'] else '未配置'}")
    print(f"  成功率: {status['success_rate']}")
    print(f"  帖子: {status['stats']['posts']}  评论: {status['stats']['comments']}")
    print(
        f"  评论缺口: {status.get('missing_comments', 0)}  "
        f"孤儿评论: {status.get('orphan_comments', 0)}  "
        f"跨帖回复: {status.get('cross_post_replies', 0)}"
    )
    print(f"  用户画像: {status['stats'].get('user_profiles', 0)}  "
          f"热门话题: {status['stats'].get('trending_topics', 0)}")
    if status["last_scrape"]:
        ls = status["last_scrape"]
        print(f"  最后爬取: {ls.get('finished_at', '')} {ls['task_type']} {ls['status']}")
    print()
    components["client"].close(); components["db"].close()


def cmd_daily_digest(args, config):
    components = init_components(config)
    from core.health_monitor import HealthMonitor
    monitor = HealthMonitor(
        components["db"], components["cookie_manager"],
        components["notifier"], config)
    print(monitor.generate_daily_digest())
    components["client"].close(); components["db"].close()


def cmd_scrape_trending(args, config):
    components = init_components(config)
    from scrapers.trending_scraper import TrendingScraper
    scraper = TrendingScraper(components["client"], components["db"], config)
    result = scraper.scrape_trending()

    print()
    if result.get("topics"):
        print(f"🔥 雪球热门话题 Top {len(result['topics'])}:")
        print("-" * 50)
        for t in result["topics"]:
            stocks = t.get("associated_stocks", "[]")
            if isinstance(stocks, str):
                import json
                try:
                    stocks = json.loads(stocks)
                except Exception:
                    stocks = []
            stocks_str = ", ".join(stocks[:5]) if stocks else ""
            print(f"  #{t['rank']:2d} {t['title']}")
            print(f"      讨论 {t['discuss_count']} | 关联: {stocks_str or '无'}")
        print(f"\n新增入库: {result['new_topics']} 条")
    else:
        print("未获取到热门话题数据")

    days = getattr(args, "days", 0) or 0
    if days > 0:
        summaries = scraper.get_trending_summary(days=days)
        if summaries:
            print(f"\n📈 最近 {days} 天趋势:")
            for s in summaries:
                print(f"\n  {s['date']}:")
                for t in s["topics"][:5]:
                    print(f"    #{t['rank']} {t['title']} (讨论{t['discuss_count']})")

    components["client"].close(); components["db"].close()


def cmd_export(args, config):
    components = init_components(config)
    db = components["db"]
    fmt = getattr(args, "format", "all") or "all"
    symbol = args.symbol.upper() if args.symbol else None
    days = args.days

    print(f"导出" + (f" {symbol}" if symbol else " 全部") +
          (f" 最近{days}天" if days else "") + "...")

    try:
        if fmt in ("all", "json"):
            from export.json_exporter import export_json
            for f in export_json(db, symbol=symbol, days=days):
                print(f"✓ JSON: {f}")
        if fmt in ("all", "csv"):
            from export.csv_exporter import export_csv
            for f in export_csv(db, symbol=symbol, days=days):
                print(f"✓ CSV:  {f}")
        if fmt in ("all", "md", "markdown"):
            from export.markdown_exporter import export_markdown
            for f in export_markdown(db, symbol=symbol, days=days):
                print(f"✓ MD:   {f}")
    except Exception as e:
        print(f"✗ 导出失败: {e}")
        import traceback
        traceback.print_exc()

    components["client"].close(); db.close()


def cmd_progress(args, config):
    print("\n" + "=" * 50)
    print("  实时进展")
    print("=" * 50)
    print()
    command_comment = argparse.Namespace(log="", completed_limit=6)
    from utils.query_progress import (
        command_comment_backfill_status,
        command_history_queue_status,
        command_user_scrape_status,
    )
    command_comment_backfill_status(command_comment)
    print()
    command_history = argparse.Namespace(manifest="")
    command_history_queue_status(command_history)
    print()
    command_user = argparse.Namespace(user_ids=[])
    command_user_scrape_status(command_user)
    print()


def cmd_user_scrape_status(args, config):
    """代理到 query_progress.command_user_scrape_status。"""
    from utils.query_progress import command_user_scrape_status
    return command_user_scrape_status(args)


def cmd_sync_users(args, config):
    """用户/KOL 同步：支持历史触底后再增量，或仅跑每日增量。"""
    from scrapers.scrape_cmd import cmd_scrape
    from scrapers.scrape_cmd import _resolve_user_targets

    runtime_config = _build_user_sync_runtime_config(config)
    scraping_cfg = runtime_config.get("scraping", {})
    db_cfg = config.get("database", {})
    db = Database(db_cfg)

    user_queries = [str(q).strip() for q in (getattr(args, "users", None) or []) if str(q).strip()]
    ensure_history = bool(getattr(args, "ensure_history", False))
    history_pages = max(1, int(getattr(args, "pages", 50) or 50))
    history_round_limit = max(1, int(getattr(args, "history_rounds", 12) or 12))
    state_started_at = int(time.time() * 1000)

    tracked = db.get_tracked_users(active_only=True)
    if not tracked and not user_queries:
        print("没有跟踪用户，请先运行: python main.py add-user 用户名")
        db.close()
        return

    tracked_map = {u["user_id"]: u for u in tracked}
    target_info = []
    if user_queries:
        cookie_manager = CookieManager(config, config_path="config.yaml")
        resolve_client = XueqiuClient(cookie_manager, RateLimiter(scraping_cfg), scraping_cfg)
        try:
            resolved = _resolve_user_targets(user_queries, False, db, resolve_client, auto_confirm=True)
        finally:
            resolve_client.close()
        for item in resolved:
            uid = item["user_id"]
            uname = item.get("screen_name", "")
            if uid not in tracked_map:
                db.upsert_tracked_user(uid, uname)
                tracked_map[uid] = {"user_id": uid, "screen_name": uname, "history_complete": 0}
            target_info.append({"user_id": uid, "screen_name": uname})
    else:
        target_info = [{"user_id": u["user_id"], "screen_name": u.get("screen_name", "")} for u in tracked]

    target_ids = [item["user_id"] for item in target_info]

    def _refresh_tracked():
        current = Database({**db_cfg, "log_lifecycle": False})
        try:
            return {u["user_id"]: u for u in current.get_tracked_users(active_only=True)}
        finally:
            current.close()

    def _run_phase(user_ids, phase_mode, pages_for_phase, phase_name, round_no=0):
        if not user_ids:
            return
        _write_user_sync_state(
            {
                "active": True,
                "phase": phase_name,
                "ensure_history": ensure_history,
                "started_at": state_started_at,
                "updated_at": int(time.time() * 1000),
                "history_pages": history_pages,
                "target_user_ids": user_ids,
                "current_round": round_no,
                "current_user_id": "",
            }
        )
        phase_args = argparse.Namespace(
            stocks=[],
            users=user_ids,
            all=False,
            mode=phase_mode,
            pages=pages_for_phase,
            workers=1,
            yes=True,
            no_preflight=True,
            history_child=False,
        )
        cmd_scrape(phase_args, runtime_config)

    try:
        tracked_map = _refresh_tracked()
        history_users = [uid for uid in target_ids if not tracked_map.get(uid, {}).get("history_complete")]
        update_users = [uid for uid in target_ids if tracked_map.get(uid, {}).get("history_complete")]

        print(f"\n两阶段同步计划:")
        print(f"  历史补全: {len(history_users)} 个用户")
        print(f"  增量更新: {len(update_users)} 个用户")

        if ensure_history:
            remaining_history = history_users[:]
            round_no = 0
            while remaining_history and round_no < history_round_limit:
                round_no += 1
                print(f"\n{'=' * 55}")
                print(f"  Phase 1: 历史补全第 {round_no} 轮 ({len(remaining_history)} 用户)")
                print(f"{'=' * 55}")
                _write_user_sync_state(
                    {
                        "active": True,
                        "phase": "history",
                        "ensure_history": True,
                        "started_at": state_started_at,
                        "updated_at": int(time.time() * 1000),
                        "history_pages": history_pages,
                        "target_user_ids": remaining_history,
                        "current_round": round_no,
                        "current_user_id": "",
                    }
                )
                _run_phase(remaining_history, "backfill", history_pages, "history", round_no=round_no)
                tracked_map = _refresh_tracked()
                next_remaining = [uid for uid in remaining_history if not tracked_map.get(uid, {}).get("history_complete")]
                print(f"  本轮结束后仍未触底: {len(next_remaining)} 个用户")
                remaining_history = next_remaining
            history_users = remaining_history
            if history_users:
                print(f"  ⚠ 达到历史轮次上限后仍有 {len(history_users)} 个用户未触底，下次继续")
            tracked_map = _refresh_tracked()
            update_users = [uid for uid in target_ids if tracked_map.get(uid, {}).get("history_complete")]
        else:
            history_users = [uid for uid in target_ids if not tracked_map.get(uid, {}).get("history_complete")]
            if history_users:
                print(f"\n  · 跳过 {len(history_users)} 个未触底用户；如需先补全历史，请加 --ensure-history")

        stale_update_users = []
        tracked_map = _refresh_tracked()
        for uid in update_users:
            last_sync_time = int(tracked_map.get(uid, {}).get("last_sync_time", 0) or 0)
            if ensure_history or not last_sync_time:
                stale_update_users.append(uid)
                continue
            last_dt = datetime.fromtimestamp(last_sync_time / 1000)
            if last_dt.date() != datetime.now().date():
                stale_update_users.append(uid)

        if stale_update_users:
            print(f"\n{'=' * 55}")
            print(f"  Phase 2: 增量更新 ({len(stale_update_users)} 用户)")
            print(f"{'=' * 55}")
            _run_phase(stale_update_users, "update", 50, "update", round_no=0)
        else:
            print("\n  · 今天已无需要增量同步的用户")

        tracked_map = _refresh_tracked()
        completed_now = sum(1 for uid in target_ids if tracked_map.get(uid, {}).get("history_complete"))
        synced_today = 0
        for uid in target_ids:
            last_sync_time = int(tracked_map.get(uid, {}).get("last_sync_time", 0) or 0)
            if last_sync_time:
                try:
                    if datetime.fromtimestamp(last_sync_time / 1000).date() == datetime.now().date():
                        synced_today += 1
                except (ValueError, OSError):
                    pass

        print(f"\n{'=' * 55}")
        print(f"  同步完成")
        print(f"  已触底: {completed_now}/{len(target_ids)}")
        print(f"  今日已同步: {synced_today}/{len(target_ids)}")
        print(f"{'=' * 55}")
        print(f"\n✓ 同步完成")
    finally:
        db.close()
        _clear_user_sync_state()


# ================================================================
# argparse + 命令路由
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="雪球评论区爬虫系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py scrape --stocks 振华科技 西藏矿业          增量爬取指定股票
  python main.py scrape --stocks 振华科技 --mode backfill --pages 100
  python main.py scrape --users 罗洄头                      跟踪指定用户
  python main.py scrape --all                               爬取所有已监控
  python main.py run                                        等同 scrape --all
  python main.py add-stock 茅台                             添加监控股票
  python main.py status                                     查看系统状态
        """,
    )

    sub = parser.add_subparsers(dest="command", help="可用命令")

    # scrape — 核心命令
    p = sub.add_parser("scrape", help="爬取指定股票/用户（支持增量与历史补全）")
    p.add_argument("--stocks", nargs="+", default=[], help="股票名称或代码（空格分隔）")
    p.add_argument("--users", nargs="+", default=[], help="用户名或ID（空格分隔）")
    p.add_argument("--all", action="store_true", help="爬取所有已监控的股票和用户")
    p.add_argument("--mode", choices=["update", "backfill", "history"], default="update",
                   help="update=增量(默认), backfill/history=向更早历史补全")
    p.add_argument("--pages", type=int, default=50, help="历史补全模式扫描页数（默认50）")
    p.add_argument("--workers", type=int, default=2, help="并发线程数（默认2）")
    p.add_argument("--yes", action="store_true", help="自动确认候选与清单，不进入交互编辑")
    p.add_argument("--no-preflight", action="store_true", help="跳过运行前数据库窗口和清单编辑")
    p.add_argument("--history-child", action="store_true", help=argparse.SUPPRESS)

    # 其他命令
    sub.add_parser("login", help="浏览器登录获取Token")
    sub.add_parser("run", help="爬取所有已监控（等同 scrape --all）")
    sub.add_parser("schedule", help="启动定时调度")

    p = sub.add_parser("add-stock", help="添加监控股票")
    p.add_argument("symbol", help="股票名称或代码")
    p.add_argument("name", nargs="?", default="", help="股票名称")

    p = sub.add_parser("add-user", help="添加跟踪用户")
    p.add_argument("user_id", help="用户名或ID")
    p.add_argument("name", nargs="?", default="", help="昵称")
    p.add_argument("--note", default="", help="备注")

    p = sub.add_parser("remove-stock", help="移除监控股票")
    p.add_argument("symbol", help="股票代码")

    p = sub.add_parser("remove-user", help="移除跟踪用户")
    p.add_argument("user_id", help="用户ID")

    p = sub.add_parser("sync-users", help="智能同步所有跟踪用户（自动判断历史补全或增量）")
    p.add_argument("--users", nargs="+", default=[], help="指定用户（默认全部）")
    p.add_argument("--pages", type=int, default=50, help="历史补全页数（默认50）")
    p.add_argument("--ensure-history", action="store_true", help="先循环历史补全直到触底，再做增量同步")
    p.add_argument("--history-rounds", type=int, default=12, help="历史补全最多循环轮数（默认12）")
    p.add_argument("--yes", action="store_true", help="自动确认，不进入交互")
    p.add_argument("--no-preflight", action="store_true", help="跳过运行前检查")

    p = sub.add_parser("backfill-comments", help="回填缺失评论")
    p.add_argument("--symbol", default=None, help="指定股票")
    p.add_argument("--days", type=int, default=None, help="回填天数；0 表示全历史")
    p.add_argument("--max-posts", type=int, default=None, help="单次最多处理多少个缺口帖子")
    p.add_argument("--post-id", default=None, help="仅回填单个帖子评论")

    p = sub.add_parser("audit-completeness", help="检查评论/用户时间线完整性")
    p.add_argument("--symbol", default=None, help="指定股票")
    p.add_argument("--user-id", default=None, help="指定用户 ID")
    p.add_argument("--days", type=int, default=None, help="仅审计最近 N 天帖子")
    p.add_argument("--fix-comments", action="store_true", help="审计后自动回填评论缺口")

    p = sub.add_parser("scrape-trending", help="抓取热门话题")
    p.add_argument("--days", type=int, default=0, help="显示最近N天趋势")

    sub.add_parser("health", help="系统健康状态")
    sub.add_parser("daily-digest", help="每日摘要")
    sub.add_parser("status", help="系统状态")
    sub.add_parser("progress", help="查看实时进展与预计剩余时间")
    sub.add_parser("test-cookie", help="测试Cookie")
    p = sub.add_parser("user-scrape-status", help="查看用户跟踪爬取详细状态")
    p.add_argument("--user-ids", nargs="+", default=[], help="指定用户 ID（默认全部活跃用户）")

    p = sub.add_parser("export", help="导出数据（JSON/CSV/Markdown）")
    p.add_argument("--format", choices=["all", "json", "csv", "md"], default="all")
    p.add_argument("--symbol", default=None, help="指定股票")
    p.add_argument("--days", type=int, default=None, help="最近N天")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config()

    # scrape 命令从独立模块导入
    from scrapers.scrape_cmd import cmd_scrape

    commands = {
        "scrape": cmd_scrape,
        "login": cmd_login,
        "run": cmd_run,
        "schedule": cmd_schedule,
        "add-stock": cmd_add_stock,
        "add-user": cmd_add_user,
        "remove-stock": cmd_remove_stock,
        "remove-user": cmd_remove_user,
        "sync-users": cmd_sync_users,
        "status": cmd_status,
        "progress": cmd_progress,
        "test-cookie": cmd_test_cookie,
        "backfill-comments": cmd_backfill_comments,
        "audit-completeness": cmd_audit_completeness,
        "health": cmd_health,
        "daily-digest": cmd_daily_digest,
        "scrape-trending": cmd_scrape_trending,
        "export": cmd_export,
        "user-scrape-status": cmd_user_scrape_status,
    }

    handler = commands.get(args.command)
    if not handler:
        parser.print_help()
        return

    try:
        handler(args, config)
    except KeyboardInterrupt:
        print("\n已收到中断信号，正在退出...")
        sys.exit(130)


if __name__ == "__main__":
    main()
