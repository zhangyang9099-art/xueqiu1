"""
HTTP 客户端：封装所有与雪球服务器的网络通信。

核心反爬策略：
  - 使用 curl_cffi 模拟真实 Chrome 浏览器 TLS 指纹
  - 自动注入 Cookie（xq_a_token）
  - 正确设置 Referer 和其他请求头
  - 集成 RateLimiter 控制请求频率
  - 自动重试（指数退避 + 随机抖动）
  - 检测反爬响应（403/400/验证码），触发相应处理
"""

import time
import random
from typing import Optional

from curl_cffi import requests as curl_requests

from core.rate_limiter import RateLimiter
from core.cookie_manager import CookieManager
from core.exceptions import (
    AntiCrawlDetected,
    CookieExpired,
    CaptchaRequired,
    MaxRetryExceeded,
)
from utils.logger import get_logger

logger = get_logger()


class XueqiuClient:
    """雪球 HTTP 客户端。"""

    BASE_URL = "https://xueqiu.com"
    STOCK_API_BASE = "https://stock.xueqiu.com"

    def __init__(
        self,
        cookie_manager: CookieManager,
        rate_limiter: RateLimiter,
        config: dict,
    ):
        """
        Args:
            cookie_manager: Cookie 管理器
            rate_limiter: 请求节流器
            config: scraping 段的配置字典
        """
        self.cookie_manager = cookie_manager
        self.rate_limiter = rate_limiter
        self.max_retries = config.get("max_retries", 3)

        # 使用 curl_cffi 创建 session，模拟 Chrome 浏览器指纹
        self.session = curl_requests.Session(impersonate="chrome120")

        # 首次访问雪球主页以获取基础 Cookie（如 xq_is_login 等）
        self._initialized = False

    def _ensure_initialized(self):
        """
        首次使用时访问雪球主页，让 session 获取必要的基础 Cookie。
        雪球需要先访问主页才能正常使用 API 接口。
        """
        if self._initialized:
            return

        try:
            logger.info("初始化：访问雪球主页获取基础 Cookie...")
            self.session.get(
                self.BASE_URL,
                headers=self._build_headers(),
                timeout=30,
            )
            self._initialized = True
            logger.info("雪球主页访问完成，基础 Cookie 已获取")
        except Exception as e:
            logger.warning(f"访问雪球主页失败（非致命）: {e}")
            self._initialized = True  # 即使失败也标记，避免反复重试

    def _build_headers(self, referer_path: str = "/") -> dict:
        """
        构建请求头。

        Args:
            referer_path: Referer 路径后缀

        Returns:
            请求头字典
        """
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"{self.BASE_URL}{referer_path}",
            "Origin": self.BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
        }

    def _inject_cookies(self):
        """将 xq_a_token 注入到 session 的 Cookie 中。"""
        cookies = self.cookie_manager.get_cookies()
        for key, value in cookies.items():
            self.session.cookies.set(key, value, domain=".xueqiu.com")

    def _check_response(self, resp) -> dict:
        """
        检查响应是否正常，检测反爬信号。

        Args:
            resp: curl_cffi 的 Response 对象

        Returns:
            解析后的 JSON 字典

        Raises:
            AntiCrawlDetected: 403 等反爬响应
            CookieExpired: 需要登录
            CaptchaRequired: 触发验证码
        """
        # HTTP 状态码检查
        if resp.status_code == 403:
            raise AntiCrawlDetected(
                f"HTTP 403 Forbidden - 可能被 IP 封禁 (URL: {resp.url})"
            )

        if resp.status_code == 400:
            raise CookieExpired(
                f"HTTP 400 Bad Request - Cookie 可能已失效 (URL: {resp.url})"
            )

        if resp.status_code == 401:
            raise CookieExpired(
                f"HTTP 401 Unauthorized - 需要登录 (URL: {resp.url})"
            )

        # 内容检查
        text = resp.text
        if not text:
            raise AntiCrawlDetected("空响应体 - 可能被拦截")

        # 检查验证码
        captcha_signals = ["验证码", "captcha", "geetest", "gt.js"]
        if any(signal in text.lower() for signal in captcha_signals):
            raise CaptchaRequired("检测到验证码页面")

        # 检查登录要求
        login_signals = ['"error_code":"400016"', "请先登录", "login_required"]
        if any(signal in text for signal in login_signals):
            raise CookieExpired("API 返回需要登录的错误")

        # 尝试解析 JSON
        try:
            data = resp.json()
        except Exception:
            # 如果不是 JSON，可能是 HTML 错误页面
            if "<html" in text.lower()[:200]:
                raise AntiCrawlDetected("返回了 HTML 页面而非 JSON - 可能被重定向")
            raise

        # 检查 API 层面的错误
        if isinstance(data, dict):
            error_code = data.get("error_code")
            error_desc = data.get("error_description", "")
            if error_code and str(error_code) != "0":
                if "登录" in error_desc or "login" in error_desc.lower():
                    raise CookieExpired(f"API 错误: {error_code} - {error_desc}")
                logger.warning(f"API 返回错误: {error_code} - {error_desc}")

        return data

    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        referer_path: str = "/",
    ) -> Optional[dict]:
        """
        发送 GET 请求，自动处理反爬、重试、频率控制。

        Args:
            url: 请求 URL
            params: 查询参数
            referer_path: Referer 路径

        Returns:
            解析后的 JSON 字典，失败返回 None

        Raises:
            MaxRetryExceeded: 超过最大重试次数
            CookieExpired: Cookie 失效（会同时触发告警）
        """
        self._ensure_initialized()
        self._inject_cookies()

        last_exception = None

        for attempt in range(self.max_retries):
            # 请求前等待（频率控制）
            self.rate_limiter.wait()

            headers = self._build_headers(referer_path)

            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=30,
                )

                data = self._check_response(resp)
                return data

            except CookieExpired as e:
                logger.error(f"Cookie 失效: {e}")
                self.cookie_manager.on_expired()
                raise

            except CaptchaRequired as e:
                logger.error(f"触发验证码: {e}")
                # 验证码情况下等待较长时间
                wait_time = 300 + random.uniform(60, 180)
                logger.warning(f"触发验证码，等待 {wait_time:.0f}s 后重试...")
                time.sleep(wait_time)
                last_exception = e

            except AntiCrawlDetected as e:
                logger.warning(f"反爬检测 (尝试 {attempt + 1}/{self.max_retries}): {e}")
                wait_time = (2 ** attempt) * 15 + random.uniform(10, 30)
                logger.info(f"等待 {wait_time:.0f}s 后重试...")
                time.sleep(wait_time)
                last_exception = e

            except Exception as e:
                logger.warning(
                    f"请求异常 (尝试 {attempt + 1}/{self.max_retries}): {type(e).__name__}: {e}"
                )
                wait_time = (2 ** attempt) * 5 + random.uniform(3, 8)
                time.sleep(wait_time)
                last_exception = e

        raise MaxRetryExceeded(
            f"超过最大重试次数 ({self.max_retries}): {last_exception}"
        )

    def get_raw(
        self,
        url: str,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        发送原始 GET 请求（不经过 RateLimiter，用于 Cookie 验证等场景）。

        Args:
            url: 请求 URL
            params: 查询参数

        Returns:
            解析后的 JSON 字典
        """
        self._ensure_initialized()
        self._inject_cookies()

        headers = self._build_headers()

        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            return resp.json()
        except Exception as e:
            logger.error(f"原始请求失败: {e}")
            return None
