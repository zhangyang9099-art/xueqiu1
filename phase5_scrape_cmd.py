#!/usr/bin/env python3
"""
Phase 5 补丁 — 新增 scrape 命令 + 修复多线程/WAF/日志

新增命令（运行时传参，不改代码）:

  # 增量更新（默认模式）— 只爬新内容 + 回填旧帖新评论
  python main.py scrape --stocks 振华科技 西藏矿业 迪安诊断
  python main.py scrape --users 罗洄头
  python main.py scrape --stocks 振华科技 --users 罗洄头

  # 历史回溯 — 忽略上次爬取时间，往前爬 N 页
  python main.py scrape --stocks 振华科技 --mode history --pages 100

  # 所有已监控的股票+用户
  python main.py scrape --all

  # 控制并发数（默认2）
  python main.py scrape --stocks 振华科技 西藏矿业 --workers 2

修复:
  1. 多线程: 主线程预初始化 Playwright 防死锁
  2. WAF评论重试: HTML响应不再直接跳过，而是暂停+刷新+重试
  3. 终端日志: 所有 print 带 flush=True
  4. 并发改为2线程（用户要求）

用法:
  cd ~/Desktop/xueqiu-scraper && source venv/bin/activate
  python phase5_scrape_cmd.py
"""

import os
import sys
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(filepath, f"{filepath}.bak_{ts}")

# ================================================================
# 1. 修复 client.py — HTML响应也重试（不只是WAF关键词）
# ================================================================

def fix_client_waf_retry():
    """让评论接口返回HTML时也触发WAF重试而非直接报错"""
    fp = os.path.join(PROJECT_ROOT, "core", "client.py")
    if not os.path.exists(fp):
        print("  ⚠ core/client.py 不存在")
        return
    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 把"非预期HTML直接报错"改成"也走WAF重试流程"
    old = '                    raise AntiCrawlDetected(f"非预期的 HTML 响应: {url}")'
    new = '''                    # 非WAF关键词的HTML也可能是限流，走重试流程
                    logger.warning(f"HTML 响应 (尝试 {attempt}/{max_retries}): {url}")
                    self.rate_limiter.on_failure()
                    if attempt < max_retries:
                        self._refresh_waf()
                        wait = random.uniform(8, 20)
                        logger.info(f"等待 {wait:.0f}s 后重试...")
                        time.sleep(wait)
                        continue
                    raise AntiCrawlDetected(f"非预期的 HTML 响应: {url}")'''

    if old in content:
        content = content.replace(old, new)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        print("  ✓ client.py: HTML响应改为重试（不再直接报错）")
    else:
        print("  ⏭ client.py: 已修改或不匹配")


# ================================================================
# 2. 修改 main.py — 添加 scrape 命令
# ================================================================

SCRAPE_CMD_CODE = r'''

# ================================================================
# scrape 命令 — 运行时指定股票/用户/模式（面向自动化调用）
# ================================================================

def cmd_scrape(args, config):
    """
    灵活爬取命令 — 运行时通过参数指定目标和模式。

    两种模式:
      update (默认): 增量更新，只爬新帖子 + 回填旧帖新评论
      history:       历史回溯，忽略上次时间，往前爬 N 页

    用法:
      python main.py scrape --stocks 振华科技 西藏矿业
      python main.py scrape --stocks 振华科技 --mode history --pages 100
      python main.py scrape --users 罗洄头
      python main.py scrape --all
      python main.py scrape --stocks 振华科技 西藏矿业 --workers 2
    """
    import sys
    sys.stdout.reconfigure(line_buffering=True)

    mode = getattr(args, 'mode', 'update') or 'update'
    pages = getattr(args, 'pages', 50) or 50
    workers = getattr(args, 'workers', 2) or 2
    stock_queries = getattr(args, 'stocks', None) or []
    user_queries = getattr(args, 'users', None) or []
    scrape_all = getattr(args, 'all', False)

    if not stock_queries and not user_queries and not scrape_all:
        print("请指定爬取目标。用法:", flush=True)
        print("  python main.py scrape --stocks 振华科技 西藏矿业", flush=True)
        print("  python main.py scrape --users 罗洄头", flush=True)
        print("  python main.py scrape --all", flush=True)
        return

    # 初始化组件（轻量，不启动浏览器）
    scraping_cfg = config.get("scraping", {})
    db_cfg = config.get("database", {})
    setup_logger(config)

    from core.cookie_manager import CookieManager
    from storage.database import Database

    cookie_manager = CookieManager(config, config_path="config.yaml")
    db = Database(db_cfg)

    if not cookie_manager.is_configured():
        print("✗ Cookie 未配置，请先运行: python main.py login", flush=True)
        db.close()
        return

    # ── 解析股票目标 ──
    stock_targets = []

    if scrape_all:
        # 爬所有已监控的
        sync_config_to_db(config, db)
        for s in db.get_watched_stocks():
            stock_targets.append((s["symbol"], s.get("name", "")))
        user_queries = user_queries or []
        if not user_queries:
            for u in db.get_tracked_users():
                user_queries.append(u["user_id"])

    if stock_queries:
        from utils.stock_resolver import resolve_stock
        # 先启动一个临时 client 做股票搜索
        from core.rate_limiter import RateLimiter
        from core.client import XueqiuClient
        tmp_rl = RateLimiter(scraping_cfg)
        tmp_cl = XueqiuClient(cookie_manager, tmp_rl, scraping_cfg)
        try:
            for q in stock_queries:
                q = q.strip()
                if not q:
                    continue
                candidates = resolve_stock(q, client=tmp_cl)
                if not candidates:
                    print(f"  ⚠ 未找到股票: {q}", flush=True)
                    continue
                sym, name, _ = candidates[0]
                # 确保在数据库中
                db.upsert_stock(sym, name)
                stock_targets.append((sym, name))
                print(f"  ✓ {sym} {name}", flush=True)
        finally:
            tmp_cl.close()

    # ── 解析用户目标 ──
    user_targets = []
    if user_queries:
        from utils.stock_resolver import resolve_stock  # noqa
        from core.rate_limiter import RateLimiter
        from core.client import XueqiuClient
        tmp_rl = RateLimiter(scraping_cfg)
        tmp_cl = XueqiuClient(cookie_manager, tmp_rl, scraping_cfg)
        try:
            for q in user_queries:
                q = q.strip()
                if not q:
                    continue
                if q.isdigit():
                    user_targets.append((q, ""))
                    db.upsert_tracked_user(q, "")
                else:
                    from utils.user_resolver import search_xueqiu_user
                    users = search_xueqiu_user(tmp_cl, q)
                    if users:
                        u = users[0]
                        user_targets.append((u["id"], u["name"]))
                        db.upsert_tracked_user(u["id"], u["name"])
                        print(f"  ✓ 用户 {u['name']} (ID: {u['id']})", flush=True)
                    else:
                        print(f"  ⚠ 未找到用户: {q}", flush=True)
        finally:
            tmp_cl.close()

    if not stock_targets and not user_targets:
        print("没有有效的爬取目标", flush=True)
        db.close()
        return

    # ── 模式处理 ──
    if mode == "history":
        # 历史模式: 重置 last_scrape_time，设置页数
        scraping_cfg["max_pages_per_stock"] = pages
        for sym, name in stock_targets:
            db.conn.execute("UPDATE watched_stocks SET last_scrape_time=NULL WHERE symbol=?", (sym,))
            db.conn.commit()
            print(f"  ↻ {sym}({name}) 已重置爬取记录，将爬取 {pages} 页", flush=True)
    # update 模式不需要特殊处理，增量逻辑已内置

    print(flush=True)
    print("=" * 55, flush=True)
    print(f"  爬取任务", flush=True)
    print(f"  模式: {'增量更新' if mode == 'update' else f'历史回溯({pages}页)'}", flush=True)
    print(f"  股票: {len(stock_targets)} 只  用户: {len(user_targets)} 人", flush=True)
    print(f"  并发: {workers} 线程", flush=True)
    print("=" * 55, flush=True)

    # ── 主线程预初始化 Playwright（防多线程死锁）──
    from core.rate_limiter import RateLimiter
    from core.client import XueqiuClient

    print("\n初始化浏览器引擎...", flush=True)
    pre_rl = RateLimiter(scraping_cfg)
    pre_cl = XueqiuClient(cookie_manager, pre_rl, scraping_cfg)
    try:
        pre_cl._ensure_browser()
        print("  ✓ 浏览器引擎就绪，Cookie 有效", flush=True)
    except Exception as e:
        print(f"  ✗ 初始化失败: {e}", flush=True)
        db.close()
        return
    finally:
        pre_cl.close()

    # ── 并发爬取股票 ──
    import time as _time
    started = _time.time()
    stock_results = []

    if stock_targets:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from scrapers.stock_comments import StockCommentScraper

        actual_workers = min(workers, len(stock_targets))

        def _crawl_stock(sym, name):
            t0 = _time.time()
            print(f"\n>>> [{sym}({name})] 开始爬取...", flush=True)
            rl = RateLimiter(scraping_cfg)
            thread_db = Database(db_cfg)
            cl = XueqiuClient(cookie_manager, rl, scraping_cfg)
            sc = StockCommentScraper(cl, thread_db, scraping_cfg)
            try:
                result = sc.scrape_stock(sym, name)
                np = result.get("new_posts", 0) or 0
                nc = result.get("new_comments", 0) or 0
                elapsed = (_time.time() - t0) / 60
                print(f"\n✓ [{sym}({name})] 完成: {np}帖 {nc}评论 {elapsed:.1f}分钟", flush=True)
                return result
            except Exception as e:
                elapsed = (_time.time() - t0) / 60
                print(f"\n✗ [{sym}({name})] 失败: {e} ({elapsed:.1f}分钟)", flush=True)
                return {"symbol": sym, "name": name, "status": "failed",
                        "new_posts": 0, "new_comments": 0, "error": str(e)}
            finally:
                cl.close()
                thread_db.close()

        if actual_workers > 1 and len(stock_targets) > 1:
            print(f"\n启动 {actual_workers} 线程并发爬取 {len(stock_targets)} 只股票...", flush=True)
            with ThreadPoolExecutor(max_workers=actual_workers) as executor:
                futures = {executor.submit(_crawl_stock, s, n): (s, n) for s, n in stock_targets}
                for future in as_completed(futures):
                    try:
                        stock_results.append(future.result())
                    except Exception as e:
                        sym, name = futures[future]
                        print(f"✗ {sym}({name}) 线程异常: {e}", flush=True)
                        stock_results.append({"symbol": sym, "name": name, "status": "error",
                                              "new_posts": 0, "new_comments": 0})
        else:
            # 单股直接跑
            for sym, name in stock_targets:
                stock_results.append(_crawl_stock(sym, name))

    # ── 用户跟踪 ──
    user_results = []
    if user_targets:
        from scrapers.user_tracker import UserTracker
        rl = RateLimiter(scraping_cfg)
        cl = XueqiuClient(cookie_manager, rl, scraping_cfg)
        ut = UserTracker(cl, db, scraping_cfg)
        try:
            for uid, uname in user_targets:
                print(f"\n>>> 跟踪用户 {uid}({uname})...", flush=True)
                try:
                    result = ut.track_user(uid, uname)
                    user_results.append(result)
                    ns = result.get("new_statuses", 0)
                    print(f"✓ 用户 {uid}({uname}): {ns} 条新发言", flush=True)
                except Exception as e:
                    print(f"✗ 用户 {uid}({uname}) 失败: {e}", flush=True)
                    user_results.append({"user_id": uid, "status": "failed", "new_statuses": 0})
        finally:
            cl.close()

    # ── 汇总 ──
    elapsed = (_time.time() - started) / 60
    total_posts = sum(r.get("new_posts", 0) for r in stock_results)
    total_comments = sum(r.get("new_comments", 0) for r in stock_results)
    total_statuses = sum(r.get("new_statuses", 0) for r in user_results)

    print(f"\n{'='*55}", flush=True)
    print(f"  完成（{elapsed:.1f} 分钟）", flush=True)
    print("=" * 55, flush=True)
    for r in stock_results:
        sym = r.get("symbol", "?")
        name = r.get("name", "")
        np = r.get("new_posts", 0)
        nc = r.get("new_comments", 0)
        st = r.get("status", "?")
        print(f"  {sym}({name}): {np}帖 {nc}评论 [{st}]", flush=True)
    for r in user_results:
        uid = r.get("user_id", "?")
        ns = r.get("new_statuses", 0)
        st = r.get("status", "?")
        print(f"  用户 {uid}: {ns}条发言 [{st}]", flush=True)
    print(f"\n  合计: {total_posts}帖 {total_comments}评论 {total_statuses}条发言", flush=True)
    print(f"  耗时: {elapsed:.1f}分钟", flush=True)

    db.close()

'''


def patch_main_py():
    """在 main.py 中添加 scrape 命令"""
    fp = os.path.join(PROJECT_ROOT, "main.py")
    if not os.path.exists(fp):
        print("  ⚠ main.py 不存在")
        return
    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    changed = False

    # 1. 添加 cmd_scrape 函数（在 cmd_export 前）
    if "cmd_scrape" not in content:
        content = content.replace(
            "\ndef cmd_export(args, config):",
            SCRAPE_CMD_CODE + "\ndef cmd_export(args, config):"
        )
        print("  ✓ 新增 cmd_scrape 函数")
        changed = True
    else:
        print("  ⏭ cmd_scrape 已存在")

    # 2. 添加 argparse 子命令
    if '"scrape"' not in content or 'add_parser("scrape"' not in content:
        # 在 backfill-comments 前面插入
        scrape_parser = '''    # scrape — 灵活爬取（运行时指定目标和模式）
    p = subparsers.add_parser("scrape", help="爬取指定股票/用户（支持增量和历史两种模式）")
    p.add_argument("--stocks", nargs="+", default=[], help="股票名称或代码（可多个，空格分隔）")
    p.add_argument("--users", nargs="+", default=[], help="用户名或ID（可多个，空格分隔）")
    p.add_argument("--all", action="store_true", help="爬取所有已监控的股票和用户")
    p.add_argument("--mode", choices=["update", "history"], default="update",
                   help="update=增量更新(默认), history=历史回溯")
    p.add_argument("--pages", type=int, default=50, help="历史模式下爬取页数（默认50）")
    p.add_argument("--workers", type=int, default=2, help="并发线程数（默认2）")

'''
        if '    # backfill-comments' in content:
            content = content.replace(
                '    # backfill-comments',
                scrape_parser + '    # backfill-comments'
            )
        elif '    # scrape-trending' in content:
            content = content.replace(
                '    # scrape-trending',
                scrape_parser + '    # scrape-trending'
            )
        print("  ✓ 新增 scrape 子命令")
        changed = True

    # 3. 注册到 commands dict
    if '"scrape": cmd_scrape' not in content:
        content = content.replace(
            '"run": cmd_run,',
            '"run": cmd_run,\n        "scrape": cmd_scrape,'
        )
        print("  ✓ commands dict 注册 scrape")
        changed = True

    if changed:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
    print("  ✓ main.py 已更新")


# ================================================================
# 3. 清理临时文件
# ================================================================

def cleanup_temp_files():
    """删除之前的临时 deep_crawl.py"""
    fp = os.path.join(PROJECT_ROOT, "deep_crawl.py")
    if os.path.exists(fp):
        os.remove(fp)
        print("  ✓ 已删除临时文件 deep_crawl.py")
    else:
        print("  ⏭ deep_crawl.py 不存在")


# ================================================================
# 4. 修复 stock_comments.py 重复的用户画像提取
# ================================================================

def fix_duplicate_user_profile():
    """stock_comments.py 中有两处重复的 upsert_user_profile 调用"""
    fp = os.path.join(PROJECT_ROOT, "scrapers", "stock_comments.py")
    if not os.path.exists(fp):
        return
    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 修复帖子保存处的重复
    dup_post = '''                    # 自动提取用户画像
                    if post.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(post["_user_profile"])
                        except Exception:
                            pass

                    # 自动提取用户画像
                    if post.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(post["_user_profile"])
                        except Exception:
                            pass'''
    single_post = '''                    # 自动提取用户画像
                    if post.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(post["_user_profile"])
                        except Exception:
                            pass'''
    if dup_post in content:
        content = content.replace(dup_post, single_post)
        print("  ✓ 修复帖子处重复的用户画像提取")

    # 修复评论保存处的重复
    dup_comment = '''                    if comment.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(comment["_user_profile"])
                        except Exception:
                            pass
                    if comment.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(comment["_user_profile"])
                        except Exception:
                            pass'''
    single_comment = '''                    if comment.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(comment["_user_profile"])
                        except Exception:
                            pass'''
    if dup_comment in content:
        content = content.replace(dup_comment, single_comment)
        print("  ✓ 修复评论处重复的用户画像提取")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# 主流程
# ================================================================

def main():
    print("=" * 60)
    print("  Phase 5 补丁 — scrape 命令 + 修复")
    print("=" * 60)
    print()

    print("[1/4] 修复 client.py WAF 评论重试...")
    fix_client_waf_retry()
    print()

    print("[2/4] 修复 stock_comments.py 重复代码...")
    fix_duplicate_user_profile()
    print()

    print("[3/4] 添加 scrape 命令到 main.py...")
    patch_main_py()
    print()

    print("[4/4] 清理临时文件...")
    cleanup_temp_files()
    print()

    print("=" * 60)
    print("  安装完成！")
    print("=" * 60)
    print()
    print("新命令 scrape — 运行时指定目标，不改代码：")
    print()
    print("  ━━ 增量更新（默认）━━")
    print("  python main.py scrape --stocks 振华科技 西藏矿业 迪安诊断")
    print("  python main.py scrape --users 罗洄头")
    print("  python main.py scrape --stocks 振华科技 --users 罗洄头")
    print("  python main.py scrape --all")
    print()
    print("  ━━ 历史回溯（往前爬N页）━━")
    print("  python main.py scrape --stocks 振华科技 --mode history --pages 100")
    print("  python main.py scrape --stocks 西藏矿业 迪安诊断 --mode history --pages 50")
    print()
    print("  ━━ 并发控制 ━━")
    print("  python main.py scrape --stocks 振华科技 西藏矿业 --workers 2  (默认2)")
    print()
    print("  ━━ 原有命令仍可用 ━━")
    print("  python main.py run          # 爬所有已监控股票（等同 scrape --all）")
    print("  python main.py add-stock 茅台   # 先添加到监控列表")
    print()
    print("提示:")
    print("  - scrape 会自动解析股票名称（茅台→SH600519）")
    print("  - scrape 会自动搜索用户名（罗洄头→对应ID）")
    print("  - 不在监控列表中的股票/用户会自动添加")
    print("  - history 模式会重置该股票的爬取记录再爬")
    print("  - update 模式只爬新内容，速度快")


if __name__ == "__main__":
    main()
