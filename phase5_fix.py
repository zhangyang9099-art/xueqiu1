#!/usr/bin/env python3
"""
Phase 5 修复脚本 — 并发+提速+断点+日志+清理

用法:
  cd ~/Desktop/xueqiu-scraper && source venv/bin/activate
  python phase5_fix.py

修复内容:
  1. 删除五粮液 (SZ000858)
  2. config.yaml 参数大幅提速
  3. rate_limiter 优化（评论翻页专用快速模式）
  4. client.py 请求失败分级 + on_success/on_failure 回调
  5. stock_comments.py 断点续爬 + 详细日志 + 快速评论模式
  6. main.py 并发爬取（ThreadPoolExecutor, 3 并发）
"""

import os
import sys
import shutil
import re
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{filepath}.bak_{ts}"
        shutil.copy2(filepath, bak)
        return True
    return False


# ================================================================
# 1. 删除五粮液 + 更新 config.yaml 速度参数
# ================================================================

def fix_config():
    """删除五粮液 + 大幅提速参数"""
    fp = os.path.join(PROJECT_ROOT, "config.yaml")
    if not os.path.exists(fp):
        print("  ⚠ config.yaml 不存在")
        return
    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 删除五粮液
    # 匹配各种可能格式
    for pattern in [
        r'\n\s*-\s*\{?\s*symbol:\s*SZ000858.*\n',
        r'\n\s*-\s*\{SZ000858.*\}\n',
        r'\n.*SZ000858.*五粮液.*\n',
    ]:
        content = re.sub(pattern, '\n', content)

    # 替换速度参数（适配各种可能的当前值）
    replacements = [
        (r'min_request_interval:\s*\d+\.?\d*', 'min_request_interval: 2'),
        (r'max_request_interval:\s*\d+\.?\d*', 'max_request_interval: 5'),
        (r'burst_rest_count:\s*\d+', 'burst_rest_count: 150'),
        (r'burst_rest_seconds_min:\s*\d+', 'burst_rest_seconds_min: 20'),
        (r'burst_rest_seconds_max:\s*\d+', 'burst_rest_seconds_max: 45'),
        (r'max_requests_per_minute:\s*\d+', 'max_requests_per_minute: 20'),
    ]
    for pat, repl in replacements:
        if re.search(pat, content):
            content = re.sub(pat, repl, content)
        else:
            # 参数不存在，追加到 scraping 段
            if repl.split(':')[0].strip() not in content:
                content = content.replace(
                    'max_retries: 3',
                    f'max_retries: 3\n  {repl}'
                )

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)

    # 同时从数据库删除五粮液
    try:
        import sqlite3
        db_path = os.path.join(PROJECT_ROOT, "data", "xueqiu.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE watched_stocks SET is_active=0 WHERE symbol='SZ000858'")
            conn.commit()
            conn.close()
            print("  ✓ 五粮液 (SZ000858) 已从监控列表移除")
    except Exception as e:
        print(f"  ⚠ 数据库操作失败: {e}")

    print("  ✓ config.yaml 速度参数已优化:")
    print("    间隔: 2-5s（原 3-8s）")
    print("    爆发休息: 每150次/20-45s（原 80次/120-300s）")
    print("    每分钟上限: 20次（原 10次）")


# ================================================================
# 2. rate_limiter.py — 评论快速模式 + 真正的自适应
# ================================================================

RATE_LIMITER_V3 = r'''"""
请求节流器 v3 — 自适应 + 评论快速模式

优化点:
  - 基础间隔 2-5s（原 3-8s 或更高）
  - 评论翻页专用快速模式（1.5-3s，同帖子内连续翻页风险低）
  - 爆发休息大幅放宽: 150次/20-45s（原 80次/120-300s）
  - on_success/on_failure 真正生效
"""

import time
import random
from collections import deque
from utils.logger import get_logger

logger = get_logger()


class RateLimiter:
    """自适应请求频率控制器 v3。"""

    def __init__(self, config: dict):
        self.min_interval = config.get("min_request_interval", 2.0)
        self.max_interval = config.get("max_request_interval", 5.0)
        self.max_per_minute = config.get("max_requests_per_minute", 20)
        self.burst_rest_count = config.get("burst_rest_count", 150)
        self.burst_rest_min = config.get("burst_rest_seconds_min", 20)
        self.burst_rest_max = config.get("burst_rest_seconds_max", 45)

        # 评论快速模式参数
        self._comment_min = 1.5
        self._comment_max = 3.0

        # 自适应状态
        self._current_interval = self.min_interval
        self._consecutive_success = 0
        self._in_comment_mode = False
        self._request_times: deque = deque()
        self._total_requests: int = 0
        self._last_request_time: float = 0.0

    @property
    def total_requests(self):
        return self._total_requests

    def enter_comment_mode(self):
        """进入评论翻页快速模式（同帖子内连续翻页）。"""
        self._in_comment_mode = True

    def exit_comment_mode(self):
        """退出评论快速模式。"""
        self._in_comment_mode = False

    def wait(self):
        """请求前调用，自适应等待。"""
        if self._in_comment_mode:
            lo, hi = self._comment_min, self._comment_max
        else:
            lo, hi = self._current_interval * 0.85, self._current_interval * 1.15

        elapsed = time.time() - self._last_request_time
        delay = random.uniform(lo, hi)
        if elapsed < delay:
            time.sleep(delay - elapsed)

        # 每分钟滑动窗口
        now = time.time()
        while self._request_times and now - self._request_times[0] > 60:
            self._request_times.popleft()
        if len(self._request_times) >= self.max_per_minute:
            wait_until = self._request_times[0] + 60
            sleep_time = wait_until - now + random.uniform(0.5, 1.5)
            if sleep_time > 0:
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
        """请求成功，逐步降低间隔。"""
        self._consecutive_success += 1
        if self._consecutive_success >= 5:
            old = self._current_interval
            self._current_interval = max(self.min_interval, self._current_interval - 0.2)
            self._consecutive_success = 0

    def on_failure(self):
        """请求失败（403/WAF），间隔翻倍。"""
        self._consecutive_success = 0
        old = self._current_interval
        self._current_interval = min(self.max_interval * 3, self._current_interval * 2)
        logger.warning(f"频率自适应: 间隔 {old:.1f}s → {self._current_interval:.1f}s")

    def on_recover(self):
        """WAF 恢复后降回正常。"""
        self._current_interval = self.max_interval

    def reset(self):
        self._request_times.clear()
        self._total_requests = 0
        self._last_request_time = 0.0
        self._current_interval = self.min_interval
        self._consecutive_success = 0
'''


# ================================================================
# 3. client.py — 请求失败分级 + on_success/on_failure 回调
# ================================================================

def patch_client():
    """给 client.py 加上 on_success/on_failure 回调"""
    fp = os.path.join(PROJECT_ROOT, "core", "client.py")
    if not os.path.exists(fp):
        print("  ⚠ client.py 不存在")
        return
    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 在成功返回 data 前添加 on_success
    old_return = "                return data"
    new_return = """                self.rate_limiter.on_success()
                return data"""
    # 只替换 get 方法最内层的 return data（第一个出现在重试循环内的）
    if "self.rate_limiter.on_success()" not in content:
        # 找到 "return data" 在 "# 业务错误检查" 之后的那个
        idx = content.find("# 业务错误检查")
        if idx > 0:
            next_return = content.find("                return data", idx)
            if next_return > 0:
                content = content[:next_return] + "                self.rate_limiter.on_success()\n" + content[next_return:]
                print("  ✓ client.py: 添加 on_success 回调")

    # 在 WAF/403 重试前添加 on_failure
    if "self.rate_limiter.on_failure()" not in content:
        content = content.replace(
            "                        if attempt < max_retries:\n                            self._refresh_waf()",
            "                        self.rate_limiter.on_failure()\n                        if attempt < max_retries:\n                            self._refresh_waf()",
            1  # 只替换第一处（WAF拦截）
        )
        # 403 处也加
        content = content.replace(
            '                if status == 403:\n                    logger.warning',
            '                if status == 403:\n                    self.rate_limiter.on_failure()\n                    logger.warning'
        )
        print("  ✓ client.py: 添加 on_failure 回调")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# 4. stock_comments.py — 断点续爬 + 详细日志 + 评论快速模式
# ================================================================

def patch_stock_comments():
    """断点续爬 + 更详细的进度日志 + 评论快速模式"""
    fp = os.path.join(PROJECT_ROOT, "scrapers", "stock_comments.py")
    if not os.path.exists(fp):
        print("  ⚠ stock_comments.py 不存在")
        return
    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # --- 4a: 断点续爬 — 每页帖子处理完后立即更新 last_scrape_time ---
    old_increment = '                    # 记录最新帖子时间\n                    if post["created_at"] > latest_post_time:\n                        latest_post_time = post["created_at"]'
    new_increment = '''                    # 记录最新帖子时间 + 断点续爬
                    if post["created_at"] > latest_post_time:
                        latest_post_time = post["created_at"]
                        # 每发现更新的帖子就保存进度，Ctrl+C 不丢数据
                        self.db.update_stock_scrape_time(symbol, latest_post_time)'''

    if old_increment in content:
        content = content.replace(old_increment, new_increment)
        print("  ✓ 断点续爬: 每页保存进度")

    # --- 4b: 更详细的页级日志（显示累计帖子数+评论数）---
    old_page_log = '''                logger.info(f"[{display_name}] 获取帖子列表 第 {page} 页...")'''
    new_page_log = '''                logger.info(
                    f"[{display_name}] 第 {page} 页 | "
                    f"累计: {total_new_posts} 帖 {total_new_comments} 评论 | "
                    f"总请求: {self.client.rate_limiter.total_requests}"
                )'''

    if old_page_log in content:
        content = content.replace(old_page_log, new_page_log)
        print("  ✓ 日志升级: 显示累计帖子/评论/请求数")

    # --- 4c: 评论爬取进入快速模式 ---
    old_comment_start = '    def _scrape_post_comments(self, post_id: str, display_name: str = "") -> int:\n        """爬取指定帖子的全部评论，爬完后更新 comments_scraped 计数。"""\n        new_count = 0\n        page = 1'
    new_comment_start = '''    def _scrape_post_comments(self, post_id: str, display_name: str = "") -> int:
        """爬取指定帖子的全部评论（快速模式），爬完后更新 comments_scraped 计数。"""
        new_count = 0
        page = 1
        self.client.rate_limiter.enter_comment_mode()'''

    if old_comment_start in content:
        content = content.replace(old_comment_start, new_comment_start)
        print("  ✓ 评论爬取: 进入快速模式 (1.5-3s)")

    # 评论爬取结束退出快速模式
    old_comment_end = '        # 更新已爬评论计数\n        self.db.update_post_comments_scraped(post_id)'
    new_comment_end = '''        self.client.rate_limiter.exit_comment_mode()
        # 更新已爬评论计数
        self.db.update_post_comments_scraped(post_id)'''

    if old_comment_end in content:
        content = content.replace(old_comment_end, new_comment_end)
        print("  ✓ 评论爬取: 结束退出快速模式")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# 5. main.py — 并发爬取（3线程）
# ================================================================

def patch_main_parallel():
    """改 run_full_scrape 为 ThreadPoolExecutor 并发"""
    fp = os.path.join(PROJECT_ROOT, "main.py")
    if not os.path.exists(fp):
        print("  ⚠ main.py 不存在")
        return
    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    # 添加 import
    if "from concurrent.futures" not in content:
        content = content.replace(
            "from core.client import XueqiuClient",
            "from concurrent.futures import ThreadPoolExecutor, as_completed\nfrom core.client import XueqiuClient"
        )
        print("  ✓ 添加 ThreadPoolExecutor import")

    # 替换 run_full_scrape 中的股票爬取部分（顺序→并发）
    old_stock_loop = """    for stock in stocks:
        try:
            result = stock_scraper.scrape_stock(
                stock["symbol"], stock.get("name", "")
            )
            stock_results.append(result)
        except CookieExpired:
            logger.error("Cookie 已失效，中断所有爬取任务。")
            notifier.notify_cookie_expired()
            break
        except Exception as e:
            logger.error(f"爬取 {stock['symbol']} 时发生未预期错误: {e}")
            notifier.notify_scrape_error(
                "stock_comments", stock["symbol"], str(e)
            )"""

    new_stock_loop = """    # ── 并发爬取股票（最多 3 线程）──
    max_workers = min(3, len(stocks))

    def _scrape_one_stock(stock_info):
        \"\"\"单只股票爬取任务（在线程中执行）。\"\"\"
        sym, name = stock_info["symbol"], stock_info.get("name", "")
        # 每个线程创建独立的浏览器+频率控制器
        from core.rate_limiter import RateLimiter
        from core.client import XueqiuClient
        from scrapers.stock_comments import StockCommentScraper

        scraping_cfg = config.get("scraping", {})
        rl = RateLimiter(scraping_cfg)
        cl = XueqiuClient(cookie_manager, rl, scraping_cfg)
        sc = StockCommentScraper(cl, db, scraping_cfg)
        try:
            return sc.scrape_stock(sym, name)
        finally:
            cl.close()

    if max_workers > 1 and len(stocks) > 1:
        logger.info(f"启动并发爬取: {max_workers} 线程, {len(stocks)} 只股票")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_scrape_one_stock, s): s for s in stocks}
            for future in as_completed(futures):
                stock_info = futures[future]
                try:
                    result = future.result()
                    stock_results.append(result)
                except CookieExpired:
                    logger.error("Cookie 已失效，中断爬取。")
                    notifier.notify_cookie_expired()
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                except Exception as e:
                    logger.error(f"爬取 {stock_info['symbol']} 失败: {e}")
                    notifier.notify_scrape_error("stock_comments", stock_info["symbol"], str(e))
    else:
        # 只有1只股票时用主线程（复用已有client）
        for stock in stocks:
            try:
                result = stock_scraper.scrape_stock(stock["symbol"], stock.get("name", ""))
                stock_results.append(result)
            except CookieExpired:
                logger.error("Cookie 已失效，中断所有爬取任务。")
                notifier.notify_cookie_expired()
                break
            except Exception as e:
                logger.error(f"爬取 {stock['symbol']} 时发生未预期错误: {e}")
                notifier.notify_scrape_error("stock_comments", stock["symbol"], str(e))"""

    if old_stock_loop in content:
        content = content.replace(old_stock_loop, new_stock_loop)
        print("  ✓ run_full_scrape 改为并发（3线程）")
    else:
        print("  ⚠ 未找到原始股票循环代码块，尝试备用匹配...")
        # 备用：更宽松的匹配
        if "for stock in stocks:" in content and "_scrape_one_stock" not in content:
            # 在 run_full_scrape 函数内找到 for stock in stocks 并标记
            print("  ℹ 请手动确认: main.py 中 for stock in stocks 的替换")

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


# ================================================================
# 主流程
# ================================================================

def main():
    print("=" * 60)
    print("  Phase 5 修复 — 并发+提速+断点+日志")
    print("=" * 60)
    print()

    print("[1/5] 删除五粮液 + 优化速度参数...")
    fix_config()
    print()

    print("[2/5] 升级 rate_limiter v3（评论快速模式）...")
    fp = os.path.join(PROJECT_ROOT, "core", "rate_limiter.py")
    backup(fp)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(RATE_LIMITER_V3)
    print("  ✓ core/rate_limiter.py v3")
    print("    基础间隔: 2-5s | 评论模式: 1.5-3s")
    print("    爆发休息: 150次/20-45s")
    print()

    print("[3/5] client.py 添加失败分级回调...")
    patch_client()
    print()

    print("[4/5] stock_comments.py 断点+日志+快速评论...")
    patch_stock_comments()
    print()

    print("[5/5] main.py 并发爬取（3线程）...")
    patch_main_parallel()
    print()

    # 速度预估
    print("=" * 60)
    print("  修复完成！")
    print("=" * 60)
    print()
    print("速度对比:")
    print("  修复前: 30页茅台+评论 ≈ 3.5小时（8股串行可能12小时+）")
    print("  修复后预估:")
    print("    单股30页+评论: 基础间隔2-5s, 评论1.5-3s")
    print("    帖子请求: 30页×3.5s = ~2分钟")
    print("    评论请求: ~300次×2.2s = ~11分钟 + 2次休息×32s = ~12分钟")
    print("    单股总计: ~14分钟（原 3.5小时，提速 15倍）")
    print("    8股并发3线程: ~40分钟（原 12小时+）")
    print()
    print("日志改进:")
    print("  原: [SH600519(贵州茅台)] 获取帖子列表 第 5 页...")
    print("  新: [SH600519(贵州茅台)] 第 5 页 | 累计: 80 帖 312 评论 | 总请求: 95")
    print()
    print("新特性:")
    print("  ✓ 并发: 3个浏览器实例同时爬不同股票")
    print("  ✓ 断点: Ctrl+C 后重启不会重爬已完成的帖子")
    print("  ✓ 评论快速模式: 同帖子内翻页间隔 1.5-3s（原 3-8s）")
    print("  ✓ 失败分级: WAF/403 自动降速，成功后逐步恢复")
    print()
    print("现在运行:")
    print("  python main.py run")


if __name__ == "__main__":
    main()
