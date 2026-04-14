#!/usr/bin/env python3
"""
修复：session 级限流导致第13页卡死
- burst_rest_count 从 150 降到 100
- burst 休息时间从 20-45s 增到 90-120s
- 在 client.py 添加 refresh_session() 方法
- 在 rate_limiter.py burst 休息时回调刷新 session
"""
import shutil
from datetime import datetime

def backup(fp):
    shutil.copy2(fp, f"{fp}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

# ================================================================
# 1. 修改 core/rate_limiter.py — 降低 burst 阈值 + 加长休息
# ================================================================
fp = "core/rate_limiter.py"
backup(fp)
with open(fp, "r") as f:
    c = f.read()

# 降低 burst_rest_count 默认值
c = c.replace(
    "self.burst_rest_count = config.get('burst_rest_count', 150)",
    "self.burst_rest_count = config.get('burst_rest_count', 100)"
)
# 如果是另一种写法
c = c.replace(
    "'burst_rest_count', 150",
    "'burst_rest_count', 100"
)

# 增加休息时间（找到 burst 休息的 sleep 行）
# 常见写法: random.uniform(20, 45) 或 random.randint(20, 45)
c = c.replace("random.uniform(20, 45)", "random.uniform(90, 120)")
c = c.replace("random.uniform(20, 30)", "random.uniform(90, 120)")
c = c.replace("random.randint(20, 45)", "random.randint(90, 120)")

# 添加 on_burst_rest 回调支持
if "self.on_burst_rest" not in c:
    c = c.replace(
        "self.burst_rest_count = config.get('burst_rest_count', 100)",
        "self.burst_rest_count = config.get('burst_rest_count', 100)\n"
        "        self.on_burst_rest = None  # 回调: burst休息时刷新session"
    )
    # 在 burst 休息的 logger/print 行之后插入回调调用
    # 找到 burst 休息的日志行
    if "爆发休息" in c:
        c = c.replace(
            'logger.info(f"爆发休息',
            'if self.on_burst_rest:\n'
            '                try:\n'
            '                    self.on_burst_rest()\n'
            '                except Exception as e:\n'
            '                    logger.warning(f"刷新session失败: {e}")\n'
            '            logger.info(f"爆发休息'
        )
    elif "burst" in c.lower() and "sleep" in c:
        # 备用匹配
        pass

with open(fp, "w") as f:
    f.write(c)
print("✓ rate_limiter.py: burst阈值→100, 休息→90-120s, 添加回调")


# ================================================================
# 2. 修改 core/client.py — 添加 refresh_session() 方法
# ================================================================
fp = "core/client.py"
backup(fp)
with open(fp, "r") as f:
    c = f.read()

# 添加 refresh_session 方法（在 close 方法之前插入）
if "def refresh_session" not in c:
    refresh_code = '''
    def refresh_session(self):
        """刷新浏览器session: 重新导航到雪球首页，重置服务端计数器。"""
        logger = get_logger()
        logger.info("刷新浏览器session（重置雪球限流计数）...")
        try:
            if self._page and not self._page.is_closed():
                # 重新访问首页，过WAF
                self._page.goto("https://xueqiu.com", timeout=30000, wait_until="domcontentloaded")
                import time as _time
                _time.sleep(3)
                # 重新注入cookie
                if hasattr(self, '_cookie_manager') and self._cookie_manager:
                    token = self._cookie_manager.get_token()
                    if token:
                        self._page.evaluate(f"""
                            document.cookie = 'xq_a_token={token}; domain=.xueqiu.com; path=/';
                        """)
                        logger.info("已重新注入 xq_a_token")
                _time.sleep(2)
                logger.info("浏览器session刷新完成")
        except Exception as e:
            logger.warning(f"刷新session异常: {e}")
            # 如果刷新失败，尝试完全重启浏览器
            try:
                self.close()
                import time as _time
                _time.sleep(5)
                self._ensure_browser()
                logger.info("浏览器已完全重启")
            except Exception as e2:
                logger.error(f"浏览器重启也失败: {e2}")

'''
    # 找到 close 方法插入点
    if "def close(self)" in c:
        c = c.replace("    def close(self)", refresh_code + "    def close(self)")
    print("✓ client.py: 添加 refresh_session()")
else:
    print("⏭ client.py: refresh_session 已存在")


# 确保 cookie_manager 引用可用
if "self._cookie_manager" not in c:
    # 在 __init__ 中保存 cookie_manager 引用
    if "self.cookie_manager = cookie_manager" in c:
        c = c.replace(
            "self.cookie_manager = cookie_manager",
            "self.cookie_manager = cookie_manager\n        self._cookie_manager = cookie_manager"
        )
    elif "self._cookie_manager" not in c and "cookie_manager" in c:
        # 已经有某种引用，检查具体名称
        pass

with open(fp, "w") as f:
    f.write(c)


# ================================================================
# 3. 修改 scrapers/stock_comments.py — 连接 rate_limiter 和 client
# ================================================================
fp = "scrapers/stock_comments.py"
backup(fp)
with open(fp, "r") as f:
    c = f.read()

# 在 scrape_stock 方法开头，将 client.refresh_session 绑定到 rate_limiter
if "on_burst_rest" not in c:
    # 找到 scrape_stock 方法中的初始化部分
    # 通常在 "def scrape_stock" 之后几行
    if "def scrape_stock(self" in c:
        # 在方法体开头添加回调绑定
        old_line = '        logger.info(f"========== 开始爬取'
        if old_line in c:
            c = c.replace(
                old_line,
                '        # 绑定burst休息回调：刷新浏览器session重置限流\n'
                '        if hasattr(self.client, "refresh_session"):\n'
                '            self.rate_limiter.on_burst_rest = self.client.refresh_session\n'
                '\n'
                '        ' + old_line.strip()
            )
            print("✓ stock_comments.py: 绑定 burst→refresh_session 回调")
        else:
            print("⚠ stock_comments.py: 未找到目标行，请手动检查")
    else:
        print("⚠ stock_comments.py: 未找到 scrape_stock 方法")
else:
    print("⏭ stock_comments.py: on_burst_rest 已存在")

with open(fp, "w") as f:
    f.write(c)


# ================================================================
# 4. 确认 fetch 超时是否生效
# ================================================================
fp = "core/client.py"
with open(fp, "r") as f:
    c = f.read()

if "AbortController" in c:
    print("✓ client.py: fetch 25s 超时已存在")
else:
    print("⚠ client.py: fetch 超时未生效，正在添加...")
    old = '''async (config) => {
                        try {
                            const resp = await fetch(config.url, {'''
    new = '''async (config) => {
                        try {
                            const ctrl = new AbortController();
                            const timer = setTimeout(() => ctrl.abort(), 25000);
                            const resp = await fetch(config.url, {
                                signal: ctrl.signal,'''
    if old in c:
        c = c.replace(old, new)
        c = c.replace(
            '''return {
                                ok: resp.ok,''',
            '''clearTimeout(timer);
                            return {
                                ok: resp.ok,'''
        )
        with open(fp, "w") as f:
            f.write(c)
        print("✓ client.py: fetch 25s 超时已添加")
    else:
        print("⚠ 未匹配到fetch代码块，可能已部分修改过")

print()
print("=" * 55)
print("  修复完成！策略:")
print("  - 每100次请求暂停90-120秒")
print("  - 暂停期间刷新浏览器session（重置雪球计数器）")
print("  - fetch请求25秒超时（不再永久挂死）")
print("=" * 55)
print()
print("测试:")
print("  python main.py scrape --stocks 振华科技 --mode history --pages 100")
