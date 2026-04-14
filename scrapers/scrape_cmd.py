"""
scrape 命令 — 运行时指定股票/用户/模式（面向自动化调用）

增强点:
  - 支持名称/代码混输，并在歧义时给出候选确认
  - 每次运行前展示当前数据库时间窗口
  - 提供交互式爬取清单，可临时增删股票/KOL
"""

import os
import sys
import time
from datetime import datetime

from utils.logger import setup_logger
from utils.run_reporter import write_scrape_report
from core.cookie_manager import CookieManager
from core.rate_limiter import RateLimiter
from core.client import XueqiuClient
from core.exceptions import CaptchaRequired
from storage.database import Database

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fmt_ts(value) -> str:
    """格式化毫秒时间戳为可读字符串。"""
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return str(value)[:16]


def _build_history_runtime_config(scraping_cfg):
    cfg = dict(scraping_cfg)
    cfg["use_persistent_context"] = True
    cfg["history_reuse_single_client"] = bool(cfg.get("history_reuse_single_client", True))
    cfg["profile_bootstrap_mode"] = str(cfg.get("profile_bootstrap_mode", "if_missing") or "if_missing")
    cfg["session_probe_required"] = bool(cfg.get("session_probe_required", True))
    if not str(cfg.get("session_profile_dir", "") or "").strip():
        manual_profile = str(cfg.get("manual_verification_profile_dir", "") or "").strip()
        if manual_profile:
            cfg["session_profile_dir"] = manual_profile
    cfg["min_request_interval"] = float(
        cfg.get("history_min_request_interval_seconds", 6) or 6
    )
    cfg["max_request_interval"] = float(
        cfg.get("history_max_request_interval_seconds", 12) or 12
    )
    cfg["burst_rest_count"] = int(cfg.get("history_burst_rest_count", 30) or 30)
    cfg["burst_rest_seconds_min"] = int(
        cfg.get("history_burst_rest_seconds_min", 120) or 120
    )
    cfg["burst_rest_seconds_max"] = int(
        cfg.get("history_burst_rest_seconds_max", 240) or 240
    )
    cfg["adaptive_pacing_enabled"] = bool(cfg.get("history_adaptive_pacing", False))
    cfg["adaptive_fast_min_interval"] = float(
        cfg.get("history_adaptive_fast_min_request_interval_seconds", 6) or 6
    )
    cfg["adaptive_fast_max_interval"] = float(
        cfg.get("history_adaptive_fast_max_request_interval_seconds", 9) or 9
    )
    cfg["adaptive_slow_min_interval"] = float(
        cfg.get("history_adaptive_slow_min_request_interval_seconds", 8) or 8
    )
    cfg["adaptive_slow_max_interval"] = float(
        cfg.get("history_adaptive_slow_max_request_interval_seconds", 14) or 14
    )
    cfg["adaptive_success_threshold"] = int(
        cfg.get("history_adaptive_success_threshold", 20) or 20
    )
    cfg["adaptive_slow_request_count"] = int(
        cfg.get("history_adaptive_slow_request_count", 20) or 20
    )
    return cfg


def cmd_scrape(args, config):
    sys.stdout.reconfigure(line_buffering=True)

    mode = getattr(args, "mode", "update") or "update"
    mode = "backfill" if mode == "history" else mode
    pages = getattr(args, "pages", 50) or 50
    workers = getattr(args, "workers", 2) or 2
    scrape_all = getattr(args, "all", False)
    auto_confirm = getattr(args, "yes", False)
    no_preflight = getattr(args, "no_preflight", False)
    history_child = getattr(args, "history_child", False)

    stock_queries = [q.strip() for q in (getattr(args, "stocks", None) or []) if q.strip()]
    user_queries = [str(q).strip() for q in (getattr(args, "users", None) or []) if str(q).strip()]

    if not stock_queries and not user_queries and not scrape_all:
        print("请指定爬取目标:", flush=True)
        print("  python main.py scrape --stocks 振华科技 西藏矿业", flush=True)
        print("  python main.py scrape --users 罗洄头", flush=True)
        print("  python main.py scrape --all", flush=True)
        return

    scraping_cfg = dict(config.get("scraping", {}))
    db_cfg = config.get("database", {})
    setup_logger(config)

    cookie_manager = CookieManager(config, config_path="config.yaml")
    db = Database(db_cfg)
    history_runtime = None
    stock_results = []
    user_results = []
    started = time.time()
    report_status = "failed"
    report_error = ""

    try:
        if not cookie_manager.is_configured():
            print("✗ Cookie 未配置，请先运行: python main.py login", flush=True)
            report_error = "Cookie 未配置"
            return

        resolve_client = XueqiuClient(cookie_manager, RateLimiter(scraping_cfg), scraping_cfg)
        try:
            stock_targets = _resolve_stock_targets(
                stock_queries, scrape_all, config, db, resolve_client, auto_confirm
            )
            user_targets = _resolve_user_targets(
                user_queries, scrape_all, db, resolve_client, auto_confirm
            )
        finally:
            resolve_client.close()

        if not stock_targets and not user_targets:
            print("没有有效的爬取目标", flush=True)
            return

        _print_database_windows(db)

        if not no_preflight:
            stock_targets, user_targets, cancelled = _interactive_checklist(
                stock_targets, user_targets, db, cookie_manager, scraping_cfg, config, auto_confirm
            )
            if cancelled:
                print("已取消本次爬取", flush=True)
                return

        if not stock_targets and not user_targets:
            print("清单为空，已取消本次爬取", flush=True)
            return

        if mode == "backfill":
            scraping_cfg["max_pages_per_stock"] = pages
            scraping_cfg["max_pages_per_user"] = pages
            for item in stock_targets:
                print(
                    f"  ↻ {item['symbol']}({item.get('name', '')}) 将以最老帖子为边界向更早历史补 {pages} 页",
                    flush=True,
                )
            for item in user_targets:
                print(
                    f"  ↻ 用户 {item['user_id']}({item.get('screen_name', '')}) 将以最老发言为边界向更早历史补 {pages} 页",
                    flush=True,
                )

        mode_cn = "增量更新" if mode == "update" else f"历史补全({pages}页)"
        print(flush=True)
        print("=" * 55, flush=True)
        print(
            f"  模式: {mode_cn} | 股票: {len(stock_targets)} | "
            f"用户: {len(user_targets)} | 线程: {workers}",
            flush=True,
        )
        print("=" * 55, flush=True)

        if mode == "backfill":
            if scraping_cfg.get("history_reuse_single_client", True):
                history_cfg = _build_history_runtime_config(scraping_cfg)
                history_runtime = {
                    "client": XueqiuClient(cookie_manager, RateLimiter(history_cfg), history_cfg),
                    "db": db,
                    "config": history_cfg,
                }
            if not _run_history_verification_gate(
                stock_targets,
                user_targets,
                cookie_manager,
                scraping_cfg,
                gate_client=history_runtime["client"] if history_runtime else None,
            ):
                return
        else:
            print("\n初始化浏览器引擎...", flush=True)
            pre_cl = XueqiuClient(cookie_manager, RateLimiter(scraping_cfg), scraping_cfg)
            try:
                pre_cl._ensure_browser()
                print("  ✓ 浏览器引擎就绪", flush=True)
            except Exception as e:
                print(f"  ✗ 初始化失败: {e}", flush=True)
                return
            finally:
                pre_cl.close()

        stock_pairs = [(item["symbol"], item.get("name", "")) for item in stock_targets]
        user_pairs = [(item["user_id"], item.get("screen_name", "")) for item in user_targets]

        stock_results = _scrape_stocks(
            stock_pairs, workers, cookie_manager, scraping_cfg, db_cfg, mode, total_pages=pages,
            shared_runtime=history_runtime,
        )
        user_results = _scrape_users(
            user_pairs, cookie_manager, scraping_cfg, db_cfg, mode, total_pages=pages,
            shared_runtime=history_runtime,
        )

        elapsed = (time.time() - started) / 60
        tp = sum(r.get("new_posts", 0) for r in stock_results)
        tc = sum(r.get("new_comments", 0) for r in stock_results)
        ts = sum(r.get("new_statuses", 0) for r in user_results)

        print(f"\n{'=' * 55}", flush=True)
        print(f"  完成 | {tp}帖 {tc}评论 {ts}条发言 | {elapsed:.1f}分钟", flush=True)
        for r in stock_results:
            label = r.get("name") or r.get("symbol", "?")
            print(
                f"    {label}: "
                f"{r.get('new_posts', 0)}帖 {r.get('new_comments', 0)}评论 [{r.get('status', '')}]",
                flush=True,
            )
        for r in user_results:
            print(
                f"    用户{r.get('user_id', '?')}({r.get('screen_name', '')}): "
                f"{r.get('new_statuses', 0)}发言 [{r.get('status', '')}]",
                flush=True,
            )
        print("=" * 55, flush=True)
        report_status = "success"
    finally:
        try:
            paths = write_scrape_report(
                PROJECT_ROOT,
                db,
                mode=mode,
                pages=pages,
                workers=workers,
                stock_targets=stock_targets if 'stock_targets' in locals() else [],
                user_targets=user_targets if 'user_targets' in locals() else [],
                stock_results=stock_results,
                user_results=user_results,
                started_at=started,
                status=report_status,
                error=report_error,
            )
            print(f"\n运行摘要已写入: {paths['latest_md']}", flush=True)
        except Exception as report_exc:
            print(f"\n⚠ 写运行摘要失败: {report_exc}", flush=True)
        if history_runtime and history_runtime.get("client"):
            history_runtime["client"].close()
        db.close()


def _resolve_stock_targets(stock_queries, scrape_all, config, db, client, auto_confirm=False):
    stock_targets = []
    seen = set()

    if scrape_all:
        from main import sync_config_to_db
        sync_config_to_db(config, db)
        for item in db.get_watched_stocks():
            payload = {"symbol": item["symbol"], "name": item.get("name", "")}
            if payload["symbol"] not in seen:
                seen.add(payload["symbol"])
                stock_targets.append(payload)

    for query in stock_queries:
        resolved = _resolve_one_stock(query, db, client, auto_confirm=auto_confirm)
        if not resolved:
            continue
        db.upsert_stock(resolved["symbol"], resolved.get("name", ""))
        if resolved["symbol"] not in seen:
            seen.add(resolved["symbol"])
            stock_targets.append(resolved)
            print(f"  ✓ {resolved['symbol']} {resolved.get('name', '')}", flush=True)

    return stock_targets


def _resolve_user_targets(user_queries, scrape_all, db, client, auto_confirm=False):
    user_targets = []
    seen = set()

    if scrape_all and not user_queries:
        for item in db.get_tracked_users():
            payload = {"user_id": item["user_id"], "screen_name": item.get("screen_name", "")}
            if payload["user_id"] not in seen:
                seen.add(payload["user_id"])
                user_targets.append(payload)

    for query in user_queries:
        resolved = _resolve_one_user(query, db, client, auto_confirm=auto_confirm)
        if not resolved:
            continue
        db.upsert_tracked_user(resolved["user_id"], resolved.get("screen_name", ""))
        if resolved["user_id"] not in seen:
            seen.add(resolved["user_id"])
            user_targets.append(resolved)
            print(
                f"  ✓ 用户 {resolved.get('screen_name', '')} (ID: {resolved['user_id']})",
                flush=True,
            )

    return user_targets


def _resolve_one_stock(query, db, client, auto_confirm=False):
    from utils.stock_resolver import resolve_stock, format_candidates

    if not query:
        return None

    local = db.search_local_stocks(query)
    if len(local) == 1:
        return {"symbol": local[0]["symbol"], "name": local[0].get("name", "")}

    candidates = []
    seen = set()
    for item in local:
        tup = (item["symbol"], item.get("name", ""), "local")
        if tup[0] not in seen:
            seen.add(tup[0])
            candidates.append(tup)

    for item in resolve_stock(query, client=client):
        if item[0] not in seen:
            seen.add(item[0])
            candidates.append(item)

    if not candidates:
        print(f"  ⚠ 未找到股票: {query}", flush=True)
        return None

    if len(candidates) == 1 or candidates[0][2] in ("exact", "alias", "code", "local"):
        symbol, name, _ = candidates[0]
        return {"symbol": symbol, "name": name}

    print(f"\n股票“{query}”存在多个候选:\n{format_candidates(candidates)}", flush=True)
    idx = _prompt_choice(
        len(candidates),
        "请输入序号确认（回车选第1个，s跳过）: ",
        auto_confirm=auto_confirm,
    )
    if idx is None:
        return None
    symbol, name, _ = candidates[idx]
    return {"symbol": symbol, "name": name}


def _resolve_one_user(query, db, client, auto_confirm=False):
    from utils.user_resolver import search_xueqiu_user, format_user_candidates

    if not query:
        return None

    if query.isdigit():
        local = db.search_local_users(query, limit=1)
        return {
            "user_id": query,
            "screen_name": local[0].get("screen_name", "") if local else "",
        }

    local = db.search_local_users(query)
    if len(local) == 1:
        return {"user_id": local[0]["user_id"], "screen_name": local[0].get("screen_name", "")}

    seen = set()
    candidates = []
    for item in local:
        uid = item["user_id"]
        if uid not in seen:
            seen.add(uid)
            candidates.append(
                {
                    "id": uid,
                    "name": item.get("screen_name", ""),
                    "description": item.get("note", ""),
                    "followers_count": 0,
                    "_source": "local",
                }
            )

    for item in search_xueqiu_user(client, query):
        if item["id"] not in seen:
            seen.add(item["id"])
            candidates.append(item)

    if not candidates:
        print(f"  ⚠ 未找到用户: {query}", flush=True)
        return None

    if len(candidates) == 1 or candidates[0].get("_source") == "local":
        item = candidates[0]
        return {"user_id": item["id"], "screen_name": item.get("name", "")}

    print(f"\n用户“{query}”存在多个候选:\n{format_user_candidates(candidates)}", flush=True)
    idx = _prompt_choice(
        len(candidates),
        "请输入序号确认（回车选第1个，s跳过）: ",
        auto_confirm=auto_confirm,
    )
    if idx is None:
        return None
    item = candidates[idx]
    return {"user_id": item["id"], "screen_name": item.get("name", "")}


def _prompt_choice(total, prompt, auto_confirm=False):
    if auto_confirm or not sys.stdin.isatty():
        return 0
    try:
        choice = input(prompt).strip().lower()
    except EOFError:
        return 0
    if choice in ("s", "skip", "q", "quit"):
        return None
    if not choice:
        return 0
    try:
        idx = int(choice) - 1
    except ValueError:
        return 0
    return idx if 0 <= idx < total else 0


def _print_database_windows(db):
    print("\n当前数据库时间窗口", flush=True)
    print("-" * 55, flush=True)

    stocks = db.get_stock_time_windows(active_only=True)
    if stocks:
        print("股票:", flush=True)
        for item in stocks:
            print("  " + _format_stock_window(item), flush=True)
    else:
        print("股票: 暂无", flush=True)

    users = db.get_user_time_windows(active_only=True)
    if users:
        print("KOL:", flush=True)
        for item in users:
            print("  " + _format_user_window(item), flush=True)
    else:
        print("KOL: 暂无", flush=True)


def _interactive_checklist(stock_targets, user_targets, db, cookie_manager, scraping_cfg, config, auto_confirm=False):
    if auto_confirm or not sys.stdin.isatty():
        _print_checklist(stock_targets, user_targets, db)
        return stock_targets, user_targets, False

    while True:
        _print_checklist(stock_targets, user_targets, db)
        print("可操作: +s 股票名/+u 用户名  |  -s 代码/名称  |  -u ID/名称  |  ls 重新显示  |  run 开始  |  q 取消", flush=True)
        try:
            command = input("清单编辑> ").strip()
        except EOFError:
            return stock_targets, user_targets, False

        if not command or command.lower() in ("run", "go", "start"):
            return stock_targets, user_targets, False
        if command.lower() in ("q", "quit", "exit"):
            return stock_targets, user_targets, True
        if command.lower() in ("ls", "list"):
            continue

        prefix, _, payload = command.partition(" ")
        payload = payload.strip()

        if prefix == "+s" and payload:
            client = XueqiuClient(cookie_manager, RateLimiter(scraping_cfg), scraping_cfg)
            try:
                resolved = _resolve_one_stock(payload, db, client, auto_confirm=False)
            finally:
                client.close()
            if resolved and not any(x["symbol"] == resolved["symbol"] for x in stock_targets):
                stock_targets.append(resolved)
                db.upsert_stock(resolved["symbol"], resolved.get("name", ""))
            continue

        if prefix == "+u" and payload:
            client = XueqiuClient(cookie_manager, RateLimiter(scraping_cfg), scraping_cfg)
            try:
                resolved = _resolve_one_user(payload, db, client, auto_confirm=False)
            finally:
                client.close()
            if resolved and not any(x["user_id"] == resolved["user_id"] for x in user_targets):
                user_targets.append(resolved)
                db.upsert_tracked_user(resolved["user_id"], resolved.get("screen_name", ""))
            continue

        if prefix == "-s" and payload:
            stock_targets = [
                item for item in stock_targets
                if payload.upper() not in (item["symbol"].upper(), item.get("name", ""))
            ]
            continue

        if prefix == "-u" and payload:
            user_targets = [
                item for item in user_targets
                if payload not in (item["user_id"], item.get("screen_name", ""))
            ]
            continue

        print("未识别的命令，请重新输入。", flush=True)


def _print_checklist(stock_targets, user_targets, db):
    print("\n爬取清单", flush=True)
    print("-" * 55, flush=True)

    stock_windows = {
        item["symbol"]: item
        for item in db.get_stock_time_windows(symbols=[x["symbol"] for x in stock_targets], active_only=False)
    } if stock_targets else {}
    user_windows = {
        item["user_id"]: item
        for item in db.get_user_time_windows(user_ids=[x["user_id"] for x in user_targets], active_only=False)
    } if user_targets else {}

    if stock_targets:
        print("股票:", flush=True)
        for idx, item in enumerate(stock_targets, 1):
            window = stock_windows.get(item["symbol"], {})
            print(f"  {idx}. {item['symbol']} {item.get('name', '')} | {_format_stock_window(window, compact=True)}", flush=True)
    else:
        print("股票: 空", flush=True)

    if user_targets:
        print("KOL:", flush=True)
        for idx, item in enumerate(user_targets, 1):
            window = user_windows.get(item["user_id"], {})
            print(f"  {idx}. {item['user_id']} {item.get('screen_name', '')} | {_format_user_window(window, compact=True)}", flush=True)
    else:
        print("KOL: 空", flush=True)


def _format_stock_window(item, compact=False):
    symbol = item.get("symbol", "")
    name = item.get("name", "")
    first_post = _fmt_ms(item.get("first_post_time"))
    latest_post = _fmt_ms(item.get("latest_post_time"))
    total_posts = item.get("total_posts", 0) or 0
    total_comments = item.get("total_comments", 0) or 0
    complete = "已触底" if item.get("history_complete") else "未触底"
    stagnant = item.get("history_stagnant_runs", 0) or 0
    if compact:
        return f"{first_post or '无数据'} ~ {latest_post or '无数据'} | 帖{total_posts} 评{total_comments} | {complete}/停滞{stagnant}"
    return f"{symbol} {name} | {first_post or '无数据'} ~ {latest_post or '无数据'} | 帖{total_posts} 评{total_comments} | {complete}/停滞{stagnant}"


def _format_user_window(item, compact=False):
    user_id = item.get("user_id", "")
    name = item.get("screen_name", "")
    first_status = _fmt_ms(item.get("first_status_time"))
    latest_status = _fmt_ms(item.get("latest_status_time"))
    total = item.get("total_statuses", 0) or 0
    complete = "已触底" if item.get("history_complete") else "未触底"
    stagnant = item.get("history_stagnant_runs", 0) or 0
    if compact:
        return f"{first_status or '无数据'} ~ {latest_status or '无数据'} | 发言{total} | {complete}/停滞{stagnant}"
    return f"{user_id} {name} | {first_status or '无数据'} ~ {latest_status or '无数据'} | 发言{total} | {complete}/停滞{stagnant}"


def _fmt_ms(value):
    if not value:
        return ""
    return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")


def _history_gate_target(stock_targets, user_targets):
    if stock_targets:
        symbol, name = stock_targets[0]["symbol"], stock_targets[0].get("name", "")
        return {
            "label": f"{symbol}({name})" if name else symbol,
            "referer_path": f"/S/{symbol}",
        }
    if user_targets:
        user_id, screen_name = user_targets[0]["user_id"], user_targets[0].get("screen_name", "")
        return {
            "label": f"{user_id}({screen_name})" if screen_name else user_id,
            "referer_path": f"/u/{user_id}",
        }
    return {"label": "xueqiu_home", "referer_path": "/"}


def _format_verification_event(diag):
    event = (diag or {}).get("last_event", {}) or {}
    state = event.get("state", "") or "unknown"
    log_id = event.get("log_id", "") or "-"
    note = event.get("note", "") or "-"
    return f"state={state} log_id={log_id} note={note}"


def _run_history_verification_gate(stock_targets, user_targets, cookie_manager, scraping_cfg, gate_client=None):
    if not scraping_cfg.get("manual_verification_gate_enabled", True):
        return True

    target = _history_gate_target(stock_targets, user_targets)
    max_failures = max(1, int(scraping_cfg.get("manual_verification_max_failures_per_run", 2) or 2))
    cooldown_minutes = max(0, int(scraping_cfg.get("manual_verification_cooldown_minutes", 30) or 30))

    print("\n历史模式前置验证门禁...", flush=True)
    failure_streak = 0
    owns_client = False
    if gate_client is None:
        gate_client = XueqiuClient(cookie_manager, RateLimiter(scraping_cfg), scraping_cfg)
        owns_client = True
    try:
        while failure_streak < max_failures:
            try:
                gate_client.run_verification_gate(
                    referer_path=target["referer_path"],
                    label=target["label"],
                )
                print(
                    f"  ✓ 历史模式验证门禁通过，会话已就绪（目标: {target['label']}）",
                    flush=True,
                )
                return True
            except CaptchaRequired as e:
                failure_streak += 1
                diag = gate_client.get_verification_diagnostics()
                print(
                    f"  ⚠ 验证门禁失败 {failure_streak}/{max_failures}: {e} | "
                    f"{_format_verification_event(diag)}",
                    flush=True,
                )
                if failure_streak >= max_failures:
                    print(
                        f"  ✋ 已冷却退出，不再继续打验证码。请等待约 {cooldown_minutes} 分钟后重试。",
                        flush=True,
                    )
                    return False
                print("  · 请关闭验证窗口后重试，程序将重新打开验证门禁", flush=True)
                time.sleep(1)
            except Exception as e:
                print(f"  ✗ 验证门禁异常: {e}", flush=True)
                return False
    finally:
        if owns_client:
            gate_client.close()
    return False


def _scrape_stocks(stock_targets, workers, cookie_manager, scraping_cfg, db_cfg, mode, total_pages=None, shared_runtime=None):
    if not stock_targets:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from scrapers.stock_comments import StockCommentScraper

    shared_client = shared_runtime.get("client") if shared_runtime else None
    shared_db = shared_runtime.get("db") if shared_runtime else None
    shared_cfg = dict(shared_runtime.get("config", {})) if shared_runtime else {}

    actual_workers = min(workers, len(stock_targets))
    if mode in ("history", "backfill") and actual_workers > 1:
        print("\n历史补全模式改为单线程执行，避免 Playwright 并发请求挂起。", flush=True)
        actual_workers = 1
    if mode in ("history", "backfill") and shared_client:
        print("\n历史补全模式将继续使用同一登录会话，不再按股票/分段重建浏览器。", flush=True)
    results = []
    history_chunk_pages = max(1, int(scraping_cfg.get("user_history_chunk_pages", 3) or 3))
    history_page_timeout_streak_limit = max(
        1,
        int(scraping_cfg.get("history_page_timeout_streak_limit", 2) or 2),
    )
    state_db = None
    if mode in ("history", "backfill"):
        state_db = Database({**db_cfg, "log_lifecycle": False})

    def _apply_history_rate_profile(chunk_cfg):
        if mode not in ("history", "backfill"):
            return chunk_cfg
        chunk_cfg["min_request_interval"] = float(
            chunk_cfg.get("history_min_request_interval_seconds", 6) or 6
        )
        chunk_cfg["max_request_interval"] = float(
            chunk_cfg.get("history_max_request_interval_seconds", 12) or 12
        )
        chunk_cfg["burst_rest_count"] = int(chunk_cfg.get("history_burst_rest_count", 30) or 30)
        chunk_cfg["burst_rest_seconds_min"] = int(
            chunk_cfg.get("history_burst_rest_seconds_min", 120) or 120
        )
        chunk_cfg["burst_rest_seconds_max"] = int(
            chunk_cfg.get("history_burst_rest_seconds_max", 240) or 240
        )
        chunk_cfg["adaptive_pacing_enabled"] = bool(
            chunk_cfg.get("history_adaptive_pacing", False)
        )
        chunk_cfg["adaptive_fast_min_interval"] = float(
            chunk_cfg.get("history_adaptive_fast_min_request_interval_seconds", 6) or 6
        )
        chunk_cfg["adaptive_fast_max_interval"] = float(
            chunk_cfg.get("history_adaptive_fast_max_request_interval_seconds", 9) or 9
        )
        chunk_cfg["adaptive_slow_min_interval"] = float(
            chunk_cfg.get("history_adaptive_slow_min_request_interval_seconds", 8) or 8
        )
        chunk_cfg["adaptive_slow_max_interval"] = float(
            chunk_cfg.get("history_adaptive_slow_max_request_interval_seconds", 14) or 14
        )
        chunk_cfg["adaptive_success_threshold"] = int(
            chunk_cfg.get("history_adaptive_success_threshold", 20) or 20
        )
        chunk_cfg["adaptive_slow_request_count"] = int(
            chunk_cfg.get("history_adaptive_slow_request_count", 20) or 20
        )
        return chunk_cfg

    def _run_stock_chunk(sym, name, max_pages_for_run):
        chunk_cfg = dict(shared_cfg or scraping_cfg)
        chunk_cfg["max_pages_per_stock"] = max_pages_for_run
        chunk_cfg = _apply_history_rate_profile(chunk_cfg)
        owns_runtime = False
        if shared_client and mode in ("history", "backfill"):
            cl = shared_client
            thread_db = shared_db
        else:
            rl = RateLimiter(chunk_cfg)
            thread_db = Database(db_cfg)
            cl = XueqiuClient(cookie_manager, rl, chunk_cfg)
            owns_runtime = True
        sc = StockCommentScraper(cl, thread_db, chunk_cfg)
        try:
            result = sc.scrape_stock(sym, name, mode=mode)
            result["verification"] = cl.get_verification_diagnostics()
            result["failure_meta"] = cl.get_last_failure_meta()
            return result
        except Exception as e:
            return {
                "symbol": sym,
                "name": name,
                "status": "failed",
                "new_posts": 0,
                "new_comments": 0,
                "error": str(e),
                "verification": cl.get_verification_diagnostics(),
                "failure_meta": cl.get_last_failure_meta(),
            }
        finally:
            if owns_runtime:
                cl.close()
                thread_db.close()

    def _read_stock_window(sym):
        if state_db is None:
            db = Database({**db_cfg, "log_lifecycle": False})
            try:
                rows = db.get_stock_time_windows(symbols=[sym], active_only=False)
                return rows[0] if rows else {}
            finally:
                db.close()
        rows = state_db.get_stock_time_windows(symbols=[sym], active_only=False)
        return rows[0] if rows else {}

    def _read_stock_cursor(sym):
        if state_db is None:
            db = Database({**db_cfg, "log_lifecycle": False})
            try:
                return db.get_stock_history_cursor(sym)
            finally:
                db.close()
        return state_db.get_stock_history_cursor(sym)

    def _format_history_failure(sym, name, failure_meta, failed_page):
        label = name or sym
        excerpt = str(failure_meta.get("html_excerpt", "") or "")[:200]
        auth_state = "?"
        if "has_auth_cookies" in failure_meta:
            auth_state = "yes" if failure_meta.get("has_auth_cookies") else "no"
        return (
            f"  ⚠ [{label}] 历史页 {failed_page} "
            f"category={failure_meta.get('category', '') or '-'} "
            f"transport={failure_meta.get('transport', '') or '-'} "
            f"auth={auth_state} "
            f"excerpt={excerpt}"
        )

    def _crawl_one(sym, name):
        t0 = time.time()
        label = name or sym
        print(f"\n>>> [{label}] 开始爬取...", flush=True)

        if mode in ("history", "backfill"):
            remaining_pages = max(1, int(total_pages or scraping_cfg.get("max_pages_per_stock", 50) or 50))
            aggregate = {
                "symbol": sym,
                "name": name,
                "status": "success",
                "new_posts": 0,
                "new_comments": 0,
                "history_interruptions": {},
                "failure_meta": {},
            }
            chunk_no = 0
            verification_failure_streak = 0
            history_page_streaks = {}
            max_verification_failures = max(
                1,
                int(scraping_cfg.get("manual_verification_max_failures_per_run", 2) or 2),
            )
            cooldown_minutes = max(
                0,
                int(scraping_cfg.get("manual_verification_cooldown_minutes", 30) or 30),
            )
            transient_categories = {
                "transport_timeout",
                "page_dead",
                "transport_failure",
                "unexpected_html",
            }
            while remaining_pages > 0:
                chunk_no += 1
                chunk_pages = min(history_chunk_pages, remaining_pages)
                before = _read_stock_window(sym)
                before_oldest = before.get("first_post_time") or 0
                print(
                    f"  · [{sym}({name})] 历史分段 {chunk_no}: 运行 {chunk_pages} 页 "
                    f"(剩余计划 {remaining_pages} 页)",
                    flush=True,
                )
                result = _run_stock_chunk(sym, name, chunk_pages)
                aggregate["new_posts"] += result.get("new_posts", 0) or 0
                aggregate["new_comments"] += result.get("new_comments", 0) or 0
                failure_meta = result.get("failure_meta", {}) or {}
                error_category = result.get("error_category") or failure_meta.get("category", "")
                if error_category:
                    aggregate["failure_meta"] = dict(failure_meta)
                    interruptions = aggregate.setdefault("history_interruptions", {})
                    interruptions[error_category] = interruptions.get(error_category, 0) + 1
                verification = result.get("verification", {}) or {}
                failed_sessions = int(verification.get("failed_sessions", 0) or 0)
                recovered_sessions = int(verification.get("recovered_sessions", 0) or 0)
                if failed_sessions:
                    verification_failure_streak += failed_sessions
                    print(
                        f"  ⚠ [{label}] 验证失败 {verification_failure_streak}/{max_verification_failures} | "
                        f"{_format_verification_event(verification)}",
                        flush=True,
                    )
                    if verification_failure_streak >= max_verification_failures:
                        aggregate["status"] = "cooldown"
                        aggregate["error"] = (
                            f"已冷却退出，请等待约 {cooldown_minutes} 分钟后重试"
                        )
                        print(
                            f"  ✋ [{label}] 已冷却退出，不再继续打验证码。"
                            f"请等待约 {cooldown_minutes} 分钟后重试。",
                            flush=True,
                        )
                        break
                    print(
                        f"  · [{label}] 验证失败后保留 frontier，准备重试当前分段",
                        flush=True,
                    )
                    continue
                if error_category == "session_expired":
                    aggregate["status"] = "blocked"
                    aggregate["error"] = result.get("error", "") or "检测到登录态失效"
                    failed_page = int(result.get("last_page") or _read_stock_cursor(sym).get("page") or 1)
                    print(_format_history_failure(sym, name, failure_meta, failed_page), flush=True)
                    break
                if error_category in transient_categories:
                    failed_page = int(result.get("last_page") or _read_stock_cursor(sym).get("page") or 1)
                    history_page_streaks[failed_page] = history_page_streaks.get(failed_page, 0) + 1
                    print(
                        f"  ⚠ [{label}] {error_category}，当前历史页 {failed_page} "
                        f"连续失败 {history_page_streaks[failed_page]}/{history_page_timeout_streak_limit}",
                        flush=True,
                    )
                    print(_format_history_failure(sym, name, failure_meta, failed_page), flush=True)
                    if history_page_streaks[failed_page] >= history_page_timeout_streak_limit:
                        aggregate["status"] = "deferred"
                        aggregate["error"] = f"{error_category}: page={failed_page}，已移到批尾重试"
                        aggregate["defer_retry"] = True
                        aggregate["defer_page"] = failed_page
                        break
                    print(
                        f"  · [{label}] 保留历史游标，放慢节奏后重试当前分段",
                        flush=True,
                    )
                    time.sleep(5)
                    continue
                if recovered_sessions or result.get("status") in ("success", "partial"):
                    verification_failure_streak = 0
                if result.get("status") not in ("success", "partial"):
                    aggregate["status"] = result.get("status", "failed")
                    aggregate["error"] = result.get("error", "")
                    break

                after = _read_stock_window(sym)
                after_oldest = after.get("first_post_time") or 0
                if after.get("history_complete"):
                    break
                if after_oldest == before_oldest:
                    break
                remaining_pages -= chunk_pages

            elapsed = (time.time() - t0) / 60
            marker = "✓" if aggregate.get("status") in ("success", "partial") else "✗"
            summary = (
                f"{aggregate.get('new_posts', 0)}帖 "
                f"{aggregate.get('new_comments', 0)}评论 {elapsed:.1f}分钟"
            )
            if aggregate.get("error"):
                summary += f" | {aggregate['error']}"
            print(f"\n{marker} [{label}] {summary}", flush=True)
            return aggregate

        result = _run_stock_chunk(sym, name, total_pages or scraping_cfg.get("max_pages_per_stock", 50))
        np = result.get("new_posts", 0) or 0
        nc = result.get("new_comments", 0) or 0
        elapsed = (time.time() - t0) / 60
        if result.get("status") in ("success", "partial"):
            print(f"\n✓ [{label}] {np}帖 {nc}评论 {elapsed:.1f}分钟", flush=True)
        else:
            print(f"\n✗ [{label}] {result.get('error', result.get('status', 'failed'))} ({elapsed:.1f}分钟)", flush=True)
        return result

    try:
        if actual_workers > 1 and len(stock_targets) > 1:
            print(f"\n启动 {actual_workers} 线程并发...", flush=True)
            with ThreadPoolExecutor(max_workers=actual_workers) as executor:
                futures = {executor.submit(_crawl_one, s, n): (s, n) for s, n in stock_targets}
                for future in as_completed(futures):
                    sym, name = futures[future]
                    try:
                        results.append(future.result())
                    except Exception:
                        results.append(
                            {"symbol": sym, "name": name, "status": "error", "new_posts": 0, "new_comments": 0}
                        )
        else:
            if mode in ("history", "backfill"):
                queue = [(sym, name, 0) for sym, name in stock_targets]
                rollups = {}
                while queue:
                    sym, name, round_no = queue.pop(0)
                    result = _crawl_one(sym, name)
                    rollup = rollups.setdefault(
                        sym,
                        {
                            "symbol": sym,
                            "name": name,
                            "status": "success",
                            "new_posts": 0,
                            "new_comments": 0,
                        },
                    )
                    rollup["new_posts"] += result.get("new_posts", 0) or 0
                    rollup["new_comments"] += result.get("new_comments", 0) or 0
                    if result.get("defer_retry") and round_no < 1:
                        print(
                            f"  · [{name or sym}] 当前历史页反复超时，先处理后续股票，稍后回到批尾重试",
                            flush=True,
                        )
                        queue.append((sym, name, round_no + 1))
                        continue
                    rollup["status"] = result.get("status", "failed")
                    if result.get("error"):
                        rollup["error"] = result.get("error", "")
                    results.append(rollup)
            else:
                for sym, name in stock_targets:
                    results.append(_crawl_one(sym, name))
        return results
    finally:
        if state_db is not None:
            state_db.close()


def _scrape_users(user_targets, cookie_manager, scraping_cfg, db_cfg, mode, total_pages=None, shared_runtime=None):
    if not user_targets:
        return []

    from scrapers.user_tracker import UserTracker

    results = []
    history_chunk_pages = max(1, int(scraping_cfg.get("history_chunk_pages", 12) or 12))
    shared_client = shared_runtime.get("client") if shared_runtime else None
    shared_db = shared_runtime.get("db") if shared_runtime else None
    shared_cfg = dict(shared_runtime.get("config", {})) if shared_runtime else {}
    state_db = None
    if mode in ("history", "backfill"):
        state_db = Database({**db_cfg, "log_lifecycle": False})

    def _run_user_chunk(uid, uname, max_pages_for_run):
        chunk_cfg = dict(shared_cfg or scraping_cfg)
        chunk_cfg["max_pages_per_user"] = max_pages_for_run
        owns_runtime = False
        if shared_client and mode in ("history", "backfill"):
            db = shared_db
            cl = shared_client
        else:
            db = Database(db_cfg)
            cl = XueqiuClient(cookie_manager, RateLimiter(chunk_cfg), chunk_cfg)
            owns_runtime = True
        ut = UserTracker(cl, db, chunk_cfg)
        try:
            result = ut.track_user(uid, uname, mode=mode)
            result["verification"] = cl.get_verification_diagnostics()
            result["failure_meta"] = cl.get_last_failure_meta()
            return result
        except Exception as e:
            return {
                "user_id": uid,
                "screen_name": uname,
                "status": "failed",
                "new_statuses": 0,
                "new_comments": 0,
                "error": str(e),
                "verification": cl.get_verification_diagnostics(),
                "failure_meta": cl.get_last_failure_meta(),
            }
        finally:
            if owns_runtime:
                cl.close()
                db.close()

    def _read_user_cursor(uid):
        if state_db is None:
            db = Database({**db_cfg, "log_lifecycle": False})
            try:
                return db.get_user_history_cursor(uid)
            finally:
                db.close()
        return state_db.get_user_history_cursor(uid)

    def _read_user_window(uid):
        if state_db is None:
            db = Database({**db_cfg, "log_lifecycle": False})
            try:
                rows = db.get_user_time_windows(user_ids=[uid], active_only=False)
                return rows[0] if rows else {}
            finally:
                db.close()
        rows = state_db.get_user_time_windows(user_ids=[uid], active_only=False)
        return rows[0] if rows else {}

    try:
        user_queue = [(uid, uname, 0) for uid, uname in user_targets]
        processed_users = {}
        while user_queue:
            uid, uname, round_no = user_queue.pop(0)
            try:
                if mode in ("history", "backfill"):
                    remaining_pages = max(1, int(total_pages or scraping_cfg.get("max_pages_per_user", 50) or 50))
                    aggregate = {
                        "user_id": uid, "screen_name": uname, "status": "success",
                        "new_statuses": 0, "new_comments": 0,
                        "history_interruptions": {}, "failure_meta": {},
                    }
                    chunk_no = 0
                    verification_failure_streak = 0
                    max_verification_failures = max(
                        1, int(scraping_cfg.get("manual_verification_max_failures_per_run", 2) or 2),
                    )
                    cooldown_minutes = max(
                        0, int(scraping_cfg.get("manual_verification_cooldown_minutes", 30) or 30),
                    )
                    history_page_timeout_streak_limit = max(
                        1, int(scraping_cfg.get("history_page_timeout_streak_limit", 2) or 2),
                    )
                    transient_categories = {
                        "transport_timeout", "page_dead", "transport_failure", "unexpected_html", "http_400_10022",
                    }
                    history_page_streaks = {}
                    print(f"\n>>> [{uname or uid}] 历史补全开始...", flush=True)
                    while remaining_pages > 0:
                        chunk_no += 1
                        chunk_pages = min(history_chunk_pages, remaining_pages)
                        before = _read_user_window(uid)
                        before_oldest = before.get("first_status_time") or 0
                        print(
                            f"  · 用户 {uid}({uname}) 历史分段 {chunk_no}: 运行 {chunk_pages} 页 "
                            f"(剩余计划 {remaining_pages} 页)",
                            flush=True,
                        )
                        result = _run_user_chunk(uid, uname, chunk_pages)
                        chunk_new_s = result.get("new_statuses", 0) or 0
                        chunk_new_c = result.get("new_comments", 0) or 0
                        aggregate["new_statuses"] += chunk_new_s
                        aggregate["new_comments"] += chunk_new_c
                        failure_meta = result.get("failure_meta", {}) or {}
                        error_category = result.get("error_category") or failure_meta.get("category", "")
                        if error_category:
                            aggregate["failure_meta"] = dict(failure_meta)
                            interruptions = aggregate.setdefault("history_interruptions", {})
                            interruptions[error_category] = interruptions.get(error_category, 0) + 1
                        verification = result.get("verification", {}) or {}
                        failed_sessions = int(verification.get("failed_sessions", 0) or 0)
                        recovered_sessions = int(verification.get("recovered_sessions", 0) or 0)
                        if failed_sessions:
                            verification_failure_streak += failed_sessions
                            print(
                                f"  ⚠ 用户 {uid}({uname}) 验证失败 {verification_failure_streak}/{max_verification_failures} | "
                                f"{_format_verification_event(verification)}",
                                flush=True,
                            )
                            if verification_failure_streak >= max_verification_failures:
                                aggregate["status"] = "cooldown"
                                aggregate["error"] = f"已冷却退出，请等待约 {cooldown_minutes} 分钟后重试"
                                print(
                                    f"  ✋ 用户 {uid}({uname}) 已冷却退出",
                                    flush=True,
                                )
                                break
                            print(f"  · 用户 {uid}({uname}) 验证失败后保留 frontier，准备重试", flush=True)
                            continue
                        if error_category == "session_expired":
                            aggregate["status"] = "blocked"
                            aggregate["error"] = result.get("error", "") or "检测到登录态失效"
                            failed_page = int(result.get("last_page") or _read_user_cursor(uid).get("page") or 1)
                            print(
                                f"  ⚠ 用户 {uid}({uname}) 登录态失效 @ page={failed_page}",
                                flush=True,
                            )
                            break
                        if error_category in transient_categories:
                            failed_page = int(result.get("last_page") or _read_user_cursor(uid).get("page") or 1)
                            history_page_streaks[failed_page] = history_page_streaks.get(failed_page, 0) + 1
                            print(
                                f"  ⚠ 用户 {uid}({uname}) {error_category} @ page={failed_page} "
                                f"连续失败 {history_page_streaks[failed_page]}/{history_page_timeout_streak_limit}",
                                flush=True,
                            )
                            if history_page_streaks[failed_page] >= history_page_timeout_streak_limit:
                                aggregate["status"] = "deferred"
                                aggregate["error"] = f"{error_category}: page={failed_page}，已移到批尾重试"
                                aggregate["defer_retry"] = True
                                aggregate["defer_page"] = failed_page
                                print(
                                    f"  · 用户 {uid}({uname}) 当前页进入 deferred，先处理后续用户，批尾再试",
                                    flush=True,
                                )
                                break
                            print(f"  · 用户 {uid}({uname}) 保留游标，放慢节奏后重试", flush=True)
                            time.sleep(5)
                            continue
                        if recovered_sessions or result.get("status") in ("success", "partial"):
                            verification_failure_streak = 0
                        if result.get("status") not in ("success", "partial"):
                            aggregate["status"] = result.get("status", "failed")
                            aggregate["error"] = result.get("error", "")
                            break
                        after = _read_user_window(uid)
                        if after.get("history_complete"):
                            after_oldest = _fmt_ts(after.get("first_status_time"))
                            print(
                                f"  ✓ 用户 {uid}({uname}) 历史已触底 | "
                                f"最早 {after_oldest or '?'} | "
                                f"分段{chunk_no}共+{chunk_new_s}发言+{chunk_new_c}评论",
                                flush=True,
                            )
                            break
                        if (after.get("first_status_time") or 0) == before_oldest:
                            print(
                                f"  · 用户 {uid}({uname}) 历史未推进 (停滞)，结束分段 | "
                                f"分段{chunk_no}共+{chunk_new_s}发言+{chunk_new_c}评论",
                                flush=True,
                            )
                            break
                        remaining_pages -= chunk_pages
                        after_oldest = _fmt_ts(after.get("first_status_time"))
                        print(
                            f"  · 用户 {uid}({uname}) 分段{chunk_no}完成 | "
                            f"+{chunk_new_s}发言 +{chunk_new_c}评论 | "
                            f"累计 {aggregate['new_statuses']}发言 {aggregate['new_comments']}评论 | "
                            f"最早 {after_oldest or '?'} | "
                            f"剩余计划 {remaining_pages} 页",
                            flush=True,
                        )
                    rollup = processed_users.setdefault(
                        uid,
                        {"user_id": uid, "screen_name": uname, "status": "success", "new_statuses": 0, "new_comments": 0},
                    )
                    rollup["new_statuses"] += aggregate.get("new_statuses", 0)
                    rollup["new_comments"] += aggregate.get("new_comments", 0)
                    if aggregate.get("defer_retry") and round_no < 1:
                        print(
                            f"  · 用户 {uid}({uname}) 当前历史页恢复未果，移到批尾重试",
                            flush=True,
                        )
                        user_queue.append((uid, uname, round_no + 1))
                        continue
                    rollup["status"] = aggregate.get("status", "failed")
                    if aggregate.get("error"):
                        rollup["error"] = aggregate.get("error", "")
                    results.append(rollup)
                    marker = "✓" if rollup.get("status") in ("success", "partial") else "✗"
                    suffix = f" | {rollup['error']}" if rollup.get("error") else ""
                    print(
                        f"{marker} 用户 {uid}({uname}): {rollup.get('new_statuses', 0)}发言 {rollup.get('new_comments', 0)}评论{suffix}",
                        flush=True,
                    )
                else:
                    result = _run_user_chunk(uid, uname, total_pages or scraping_cfg.get("max_pages_per_user", 50))
                    results.append(result)
                    marker = "✓" if result.get("status") in ("success", "partial") else "✗"
                    print(
                        f"{marker} 用户 {uid}({uname}): "
                        f"{result.get('new_statuses', 0)}发言 {result.get('new_comments', 0)}评论",
                        flush=True,
                    )
            except Exception as e:
                print(f"✗ 用户 {uid}: {e}", flush=True)
                results.append({"user_id": uid, "screen_name": uname, "status": "failed", "new_statuses": 0, "new_comments": 0})
        return results
    finally:
        if state_db is not None:
            state_db.close()


def _orchestrate_history_targets(stock_targets, user_targets, scraping_cfg, db_cfg, total_pages):
    history_chunk_pages = max(1, int(scraping_cfg.get("history_chunk_pages", 10) or 10))
    chunk_timeout = max(60, int(scraping_cfg.get("history_chunk_timeout_seconds", 180) or 180))
    state_db = Database({**db_cfg, "log_lifecycle": False})

    def _stock_window(sym):
        rows = state_db.get_stock_time_windows(symbols=[sym], active_only=False)
        return rows[0] if rows else {}

    def _stock_completeness(sym):
        rows = state_db.get_stock_completeness_report(symbol=sym)
        return rows[0] if rows else {}

    def _user_window(uid):
        rows = state_db.get_user_time_windows(user_ids=[uid], active_only=False)
        return rows[0] if rows else {}

    def _run_child(kind, identifier, pages_for_chunk):
        cmd = [sys.executable, "main.py", "scrape", "--mode", "history", "--pages", str(pages_for_chunk), "--yes", "--no-preflight", "--history-child"]
        if kind == "stock":
            cmd.extend(["--stocks", identifier])
        else:
            cmd.extend(["--users", identifier])
        return subprocess.run(cmd, timeout=chunk_timeout, check=False)

    try:
        stock_results = []
        for sym, name in stock_targets:
            remaining = max(1, int(total_pages or 1))
            aggregate = {"symbol": sym, "name": name, "status": "success", "new_posts": 0, "new_comments": 0}
            chunk_no = 0
            while remaining > 0:
                chunk_no += 1
                chunk_pages = min(history_chunk_pages, remaining)
                before = _stock_window(sym)
                before_oldest = before.get("first_post_time") or 0
                rc = None
                timed_out = False
                current_pages = chunk_pages
                while True:
                    try:
                        rc = _run_child("stock", sym, current_pages).returncode
                        break
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        after_timeout = _stock_window(sym)
                        progressed = (
                            (after_timeout.get("first_post_time") or 0) != before_oldest
                            or (after_timeout.get("total_posts", 0) or 0) > (before.get("total_posts", 0) or 0)
                        )
                        if current_pages > 1:
                            next_pages = max(1, current_pages // 2)
                            print(
                                f"⚠ [{sym}({name})] 历史分段 {chunk_no} 超时，缩小为 {next_pages} 页重试",
                                flush=True,
                            )
                            current_pages = next_pages
                            if progressed:
                                remaining = max(0, remaining - 1)
                                before = after_timeout
                                before_oldest = before.get("first_post_time") or 0
                            continue
                        print(f"⚠ [{sym}({name})] 历史分段 {chunk_no} 1页仍超时，跳过该分段", flush=True)
                        break
                after = _stock_window(sym)
                aggregate["new_posts"] = max(0, (after.get("total_posts", 0) or 0) - (before.get("total_posts", 0) or 0)) + aggregate["new_posts"]
                aggregate["new_comments"] = max(0, (after.get("total_comments", 0) or 0) - (before.get("total_comments", 0) or 0)) + aggregate["new_comments"]
                after_oldest = after.get("first_post_time") or 0
                if rc not in (None, 0) and not timed_out:
                    aggregate["status"] = "failed"
                    break
                if after.get("history_complete"):
                    break
                if after_oldest == before_oldest:
                    if timed_out:
                        aggregate["status"] = "partial"
                    break
                remaining -= current_pages if rc == 0 else 1
            completeness = _stock_completeness(sym)
            print(
                f"  · [{sym}({name})] 已覆盖区间完整性检查: "
                f"缺口评论 {completeness.get('missing_comments', 0) or 0} / "
                f"缺口帖子 {completeness.get('gap_posts', 0) or 0}",
                flush=True,
            )
            stock_results.append(aggregate)

        user_results = []
        for uid, uname in user_targets:
            remaining = max(1, int(total_pages or 1))
            aggregate = {"user_id": uid, "screen_name": uname, "status": "success", "new_statuses": 0}
            chunk_no = 0
            while remaining > 0:
                chunk_no += 1
                chunk_pages = min(history_chunk_pages, remaining)
                before = _user_window(uid)
                before_oldest = before.get("first_status_time") or 0
                try:
                    rc = _run_child("user", uid, chunk_pages).returncode
                except subprocess.TimeoutExpired:
                    print(f"✗ 用户 {uid}({uname}) 历史分段 {chunk_no} 超时，已终止该分段", flush=True)
                    aggregate["status"] = "failed"
                    break
                after = _user_window(uid)
                aggregate["new_statuses"] = max(0, (after.get("total_statuses", 0) or 0) - (before.get("total_statuses", 0) or 0)) + aggregate["new_statuses"]
                after_oldest = after.get("first_status_time") or 0
                if rc != 0:
                    aggregate["status"] = "failed"
                    break
                if after.get("history_complete"):
                    break
                if after_oldest == before_oldest:
                    break
                remaining -= chunk_pages
            user_results.append(aggregate)

        return stock_results, user_results
    finally:
        state_db.close()
