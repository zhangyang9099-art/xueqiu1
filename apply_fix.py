#!/usr/bin/env python3
"""
快速修复脚本 - 自动更新项目以绕过阿里云 WAF

在项目根目录运行：
  python apply_fix.py

它会自动：
1. 备份 core/client.py
2. 写入新的 Playwright 版本
3. 更新 requirements.txt
4. 安装依赖
"""

import os
import shutil
import subprocess
import sys


def main():
    # 检查是否在项目根目录
    if not os.path.exists("core/client.py"):
        print("❌ 请在 xueqiu-scraper 项目根目录下运行此脚本")
        sys.exit(1)

    # 1. 备份
    print("[1/4] 备份原文件...")
    if not os.path.exists("core/client_old_curlffi.py"):
        shutil.copy2("core/client.py", "core/client_old_curlffi.py")
        print("  ✓ core/client.py → core/client_old_curlffi.py")
    else:
        print("  ✓ 备份已存在，跳过")

    # 2. 写入新的 client.py
    print("[2/4] 写入新的 Playwright 客户端...")
    new_client = '''"""
browser_client.py - 使用 Playwright 浏览器内核绕过阿里云 WAF

原理：
  1. 用 Playwright 启动真实 Chromium 内核
  2. 访问雪球主页，浏览器自动执行 WAF 的 JS 挑战
  3. WAF 通过后，浏览器获得合法 cookie
  4. 后续 API 请求都通过浏览器的 page.evaluate(fetch(...)) 发出
"""

import json
import time
import logging
import random
from urllib.parse import urlencode

try:
    from core.exceptions import AntiCrawlDetected
except ImportError:
    class AntiCrawlDetected(Exception):
        """反爬检测异常"""
        pass

logger = logging.getLogger("xueqiu_scraper")


class XueqiuClient:
    """基于 Playwright 的雪球 HTTP 客户端"""

    def __init__(self, cookie_manager, rate_limiter, config: dict):
        self.cookie_manager = cookie_manager
        self.rate_limiter = rate_limiter
        self.config = config
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._initialized = False

    def _ensure_browser(self):
        """懒初始化：首次请求时才启动浏览器"""
        if self._initialized:
            return

        from playwright.sync_api import sync_playwright

        logger.info("初始化：启动浏览器引擎...")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        self._page = self._context.new_page()

        # 访问主页，通过 WAF JS 挑战
        logger.info("初始化：访问雪球主页（通过 WAF 挑战）...")
        self._page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        time.sleep(2)

        # 注入登录 token
        token = self.cookie_manager.get_token()
        if token:
            self._context.add_cookies([{
                "name": "xq_a_token",
                "value": token,
                "domain": ".xueqiu.com",
                "path": "/",
            }])
            logger.info("已注入 xq_a_token")

        # 再访问一次确保 cookie 生效
        self._page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        time.sleep(1)

        self._initialized = True
        logger.info("浏览器引擎初始化完成，WAF 挑战已通过")

    def _build_url(self, url, params):
        """构建完整 URL"""
        if params:
            p = dict(params)
            p["_"] = str(int(time.time() * 1000))
            return f"{url}?{urlencode(p)}"
        return url

    def get(self, url: str, params: dict = None, referer_path: str = None) -> dict:
        """发送 GET 请求并返回 JSON 数据"""
        self._ensure_browser()
        self.rate_limiter.wait()

        full_url = self._build_url(url, params)
        referer = f"https://xueqiu.com{referer_path}" if referer_path else "https://xueqiu.com"

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                result = self._page.evaluate("""
                    async (config) => {
                        try {
                            const resp = await fetch(config.url, {
                                credentials: 'include',
                                headers: {
                                    'Accept': 'application/json',
                                    'X-Requested-With': 'XMLHttpRequest',
                                    'Referer': config.referer,
                                }
                            });
                            const text = await resp.text();
                            return {
                                ok: resp.ok,
                                status: resp.status,
                                contentType: resp.headers.get('content-type') || '',
                                body: text,
                            };
                        } catch(e) {
                            return { ok: false, status: 0, error: e.message, body: '' };
                        }
                    }
                """, {"url": full_url, "referer": referer})

                status = result.get("status", 0)
                body = result.get("body", "")
                content_type = result.get("contentType", "")

                # WAF 拦截检测
                if "text/html" in content_type or body.strip().startswith("<"):
                    if "aliyun_waf" in body or "waf_" in body:
                        logger.warning(f"WAF 拦截 (尝试 {attempt}/{max_retries}): {url}")
                        if attempt < max_retries:
                            self._refresh_waf()
                            wait = random.uniform(5, 15)
                            logger.info(f"等待 {wait:.0f}s 后重试...")
                            time.sleep(wait)
                            continue
                        raise AntiCrawlDetected(f"WAF 反复拦截: {url}")
                    raise AntiCrawlDetected(f"非预期的 HTML 响应: {url}")

                if status == 403:
                    logger.warning(f"HTTP 403 (尝试 {attempt}/{max_retries}): {url}")
                    if attempt < max_retries:
                        self._refresh_waf()
                        time.sleep(random.uniform(10, 30))
                        continue
                    raise AntiCrawlDetected(f"HTTP 403: {url}")

                if not result.get("ok"):
                    raise AntiCrawlDetected(f"HTTP {status}: {url} | {body[:200]}")

                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    raise AntiCrawlDetected(f"非 JSON 响应: {url}")

                # 业务错误检查
                if isinstance(data, dict):
                    ec = data.get("error_code")
                    if ec and ec != 0:
                        msg = data.get("error_description", "未知错误")
                        if "login" in str(msg).lower() or ec in [400016, 20019]:
                            raise AntiCrawlDetected(f"需要登录: {msg}")
                        logger.warning(f"业务错误 {ec}: {msg}")

                return data

            except AntiCrawlDetected:
                raise
            except Exception as e:
                logger.error(f"请求异常 (尝试 {attempt}/{max_retries}): {e}")
                if attempt >= max_retries:
                    raise
                time.sleep(random.uniform(5, 15))

    def get_raw(self, url: str, params: dict = None) -> dict:
        """兼容旧接口"""
        return self.get(url, params)

    def _refresh_waf(self):
        """重新通过 WAF 挑战"""
        logger.info("重新通过 WAF 挑战...")
        try:
            self._page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
            time.sleep(3)
        except Exception as e:
            logger.warning(f"WAF 刷新失败: {e}")

    def visit_homepage(self):
        """兼容旧代码中的主页访问调用"""
        self._ensure_browser()

    def verify_cookie(self) -> bool:
        """验证 Cookie 是否有效"""
        self._ensure_browser()
        try:
            result = self._page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch("https://xueqiu.com/v4/statuses/user_timeline.json?page=1&count=1", {
                            credentials: 'include',
                            headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
                        });
                        return { ok: resp.ok, status: resp.status };
                    } catch(e) {
                        return { ok: false, error: e.message };
                    }
                }
            """)
            return result.get("ok", False)
        except Exception:
            return False

    def close(self):
        """关闭浏览器"""
        for obj in [self._page, self._context, self._browser]:
            if obj:
                try:
                    obj.close()
                except:
                    pass
        if self._playwright:
            try:
                self._playwright.stop()
            except:
                pass
        self._initialized = False
        logger.info("浏览器引擎已关闭")

    def __del__(self):
        self.close()
'''

    with open("core/client.py", "w", encoding="utf-8") as f:
        f.write(new_client)
    print("  ✓ core/client.py 已更新为 Playwright 版本")

    # 3. 更新 requirements.txt
    print("[3/4] 更新 requirements.txt...")
    with open("requirements.txt", "r") as f:
        lines = f.readlines()

    new_lines = []
    has_playwright = False
    for line in lines:
        stripped = line.strip().lower()
        if "curl_cffi" in stripped or "curl-cffi" in stripped:
            new_lines.append(f"# {line.rstrip()}  # 已弃用，被 playwright 替代\n")
        else:
            new_lines.append(line)
        if "playwright" in stripped:
            has_playwright = True

    if not has_playwright:
        new_lines.append("playwright>=1.40.0\n")

    with open("requirements.txt", "w") as f:
        f.writelines(new_lines)
    print("  ✓ requirements.txt 已更新")

    # 4. 安装依赖
    print("[4/4] 安装 Playwright...")
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
    print("  正在下载 Chromium 浏览器内核（约 150MB）...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    print("  ✓ Playwright + Chromium 安装完成")

    print("\n" + "=" * 50)
    print("✓ 修复完成！现在可以运行：")
    print("  python main.py test-cookie")
    print("  python main.py run")
    print("=" * 50)


if __name__ == "__main__":
    main()
