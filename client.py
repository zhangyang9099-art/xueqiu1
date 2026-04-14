"""
browser_client.py - 使用 Playwright 浏览器内核绕过阿里云 WAF

原理：
  1. 用 Playwright 启动真实 Chromium 内核
  2. 访问雪球主页，浏览器自动执行 WAF 的 JS 挑战
  3. WAF 通过后，浏览器获得合法 cookie
  4. 后续所有 API 请求都通过浏览器的 page.evaluate(fetch(...)) 发出
     ——浏览器自带完整的 cookie、TLS、指纹，WAF 不会拦截

替换说明：
  这个文件替换原来的 core/client.py
  对外暴露的接口保持不变：XueqiuClient(cookie_manager, rate_limiter, config)
  - .get(url, params, referer_path) -> dict
  - .close()
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

        # 再访问一次主页确保 cookie 生效
        self._page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        time.sleep(1)

        self._initialized = True
        logger.info("浏览器引擎初始化完成，WAF 挑战已通过 ✓")

    def get(self, url: str, params: dict = None, referer_path: str = None) -> dict:
        """
        发送 GET 请求并返回 JSON 数据

        通过浏览器内置的 fetch() 发请求，自动携带所有 cookie 和合法指纹
        """
        self._ensure_browser()
        self.rate_limiter.wait()

        # 构建完整 URL
        if params:
            # 添加时间戳防缓存
            params["_"] = str(int(time.time() * 1000))
            full_url = f"{url}?{urlencode(params)}"
        else:
            full_url = url

        referer = f"https://xueqiu.com{referer_path}" if referer_path else "https://xueqiu.com"

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # 通过浏览器的 fetch 发请求
                result = self._page.evaluate(f"""
                    async () => {{
                        try {{
                            const resp = await fetch("{full_url}", {{
                                credentials: 'include',
                                headers: {{
                                    'Accept': 'application/json',
                                    'X-Requested-With': 'XMLHttpRequest',
                                    'Referer': '{referer}',
                                }}
                            }});
                            const text = await resp.text();
                            return {{
                                ok: resp.ok,
                                status: resp.status,
                                contentType: resp.headers.get('content-type') || '',
                                body: text,
                            }};
                        }} catch(e) {{
                            return {{ ok: false, status: 0, error: e.message, body: '' }};
                        }}
                    }}
                """)

                status = result.get("status", 0)
                body = result.get("body", "")
                content_type = result.get("contentType", "")

                # 检查是否被 WAF 拦截（返回了 HTML 而不是 JSON）
                if "text/html" in content_type or body.strip().startswith("<"):
                    if "aliyun_waf" in body or "waf_" in body:
                        logger.warning(
                            f"WAF 拦截 (尝试 {attempt}/{max_retries}): {url}"
                        )
                        if attempt < max_retries:
                            # 重新通过 WAF 挑战
                            self._refresh_waf()
                            wait = random.uniform(5, 15)
                            logger.info(f"等待 {wait:.0f}s 后重试...")
                            time.sleep(wait)
                            continue
                        raise AntiCrawlDetected(f"WAF 反复拦截: {url}")
                    else:
                        logger.warning(f"返回了 HTML 但非 WAF 页面: {url}")
                        raise AntiCrawlDetected(f"非预期的 HTML 响应: {url}")

                if status == 403:
                    logger.warning(f"HTTP 403 (尝试 {attempt}/{max_retries}): {url}")
                    if attempt < max_retries:
                        self._refresh_waf()
                        wait = random.uniform(10, 30)
                        time.sleep(wait)
                        continue
                    raise AntiCrawlDetected(f"HTTP 403: {url}")

                if status == 400:
                    logger.warning(f"HTTP 400 Bad Request: {url}")
                    raise AntiCrawlDetected(f"HTTP 400: {url}")

                if not result.get("ok"):
                    raise AntiCrawlDetected(
                        f"HTTP {status}: {url} | {body[:200]}"
                    )

                # 解析 JSON
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    logger.error(f"JSON 解析失败: {body[:200]}")
                    raise AntiCrawlDetected(f"非 JSON 响应: {url}")

                # 检查雪球业务层错误
                if isinstance(data, dict):
                    error_code = data.get("error_code")
                    if error_code and error_code != 0:
                        error_msg = data.get("error_description", "未知错误")
                        if "login" in str(error_msg).lower() or error_code in [
                            400016, 20019
                        ]:
                            raise AntiCrawlDetected(f"需要登录: {error_msg}")
                        logger.warning(f"业务错误 {error_code}: {error_msg}")

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
        """重新访问主页通过 WAF 挑战"""
        logger.info("重新通过 WAF 挑战...")
        try:
            self._page.goto(
                "https://xueqiu.com", wait_until="networkidle", timeout=30000
            )
            time.sleep(3)
        except Exception as e:
            logger.warning(f"WAF 刷新失败: {e}")

    def verify_cookie(self) -> bool:
        """验证 Cookie 是否有效"""
        self._ensure_browser()
        try:
            result = self._page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch("https://xueqiu.com/v4/statuses/user_timeline.json?page=1&count=1", {
                            credentials: 'include',
                            headers: {
                                'Accept': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest',
                            }
                        });
                        const data = await resp.json();
                        return { ok: resp.ok, hasData: !!data };
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
        if self._page:
            try:
                self._page.close()
            except:
                pass
        if self._context:
            try:
                self._context.close()
            except:
                pass
        if self._browser:
            try:
                self._browser.close()
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
