"""
Playwright 浏览器客户端：通过真实 Chromium 会话访问雪球接口。

核心策略:
  1. 主页先过 WAF / JS 挑战
  2. 恢复完整 Cookie Jar，而不是只注入 xq_a_token
  3. 首次访问某类接口前，先暖场对应的 referer 页面
  4. 每次刷新会话后持久化最新浏览器 Cookie
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode

from scrapers.api_endpoints import build_user_timeline_params, build_user_timeline_url
from utils.logger import get_logger

try:
    from core.exceptions import AntiCrawlDetected, CaptchaRequired, RequestFailed
except ImportError:
    class AntiCrawlDetected(Exception):
        """反爬检测异常"""

    class CaptchaRequired(AntiCrawlDetected):
        """触发验证码"""

    class RequestFailed(Exception):
        """普通请求失败"""

        def __init__(self, category: str, message: str, url: str = "", detail: str = ""):
            super().__init__(message)
            self.category = category
            self.url = url
            self.detail = detail or message


logger = logging.getLogger("xueqiu_scraper")


class XueqiuClient:
    """基于 Playwright 的雪球 HTTP 客户端"""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, cookie_manager, rate_limiter, config: dict):
        self.cookie_manager = cookie_manager
        self.rate_limiter = rate_limiter
        self.config = config or {}
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._initialized = False
        self._closed = False
        self._warmed_paths = set()
        self._last_fetch_error = ""
        self._last_failure_meta = {}
        self._slow_request_seconds = float(self.config.get("slow_request_seconds", 15) or 15)
        self._manual_verification_enabled = bool(self.config.get("manual_verification_enabled", True))
        self._manual_verification_timeout_seconds = int(
            self.config.get("manual_verification_timeout_seconds", 300) or 300
        )
        self._manual_verification_mode = str(
            self.config.get("manual_verification_mode", "external") or "external"
        ).strip().lower()
        self._manual_verification_gate_enabled = bool(
            self.config.get("manual_verification_gate_enabled", True)
        )
        self._manual_verification_poll_seconds = float(
            self.config.get("manual_verification_poll_seconds", 2) or 2
        )
        self._manual_verification_grace_seconds = float(
            self.config.get("manual_verification_grace_seconds", 45) or 45
        )
        self._manual_verification_failure_stable_seconds = float(
            self.config.get("manual_verification_failure_stable_seconds", 12) or 12
        )
        self._manual_verification_refresh_cooldown_seconds = float(
            self.config.get("manual_verification_refresh_cooldown_seconds", 45) or 45
        )
        self._manual_verification_auto_refresh = bool(
            self.config.get("manual_verification_auto_refresh", False)
        )
        self._manual_verification_success_checks = int(
            self.config.get("manual_verification_success_checks", 2) or 2
        )
        self._manual_verification_prewarm_pages = max(
            1, int(self.config.get("manual_verification_prewarm_pages", 2) or 2)
        )
        self._manual_verification_resume_probe_count = max(
            0, int(self.config.get("manual_verification_resume_probe_count", 2) or 2)
        )
        self._manual_verification_cleanup_profile_processes = bool(
            self.config.get("manual_verification_cleanup_profile_processes", True)
        )
        self._profile_bootstrap_mode = str(
            self.config.get("profile_bootstrap_mode", "if_missing") or "if_missing"
        ).strip().lower()
        self._session_probe_required = bool(self.config.get("session_probe_required", True))
        self._use_persistent_context = bool(self.config.get("use_persistent_context", False))
        self._browser_engine = str(self.config.get("browser_engine", "playwright") or "playwright").strip().lower()
        self._manual_browser_active = False
        self._verification_events = []
        self._verification_failed_sessions = 0
        self._verification_recovered_sessions = 0
        self._last_verification_event = {}
        self._verification_markers = (
            "访问验证",
            "请按住滑块",
            "拖动到最右边",
            "日志ID",
        )
        self._verification_failure_markers = (
            "验证失败",
            "请刷新重试",
            "刷新重试",
        )
        self._waf_markers = (
            "aliyun_waf",
            "waf_",
            "secverify",
            "captcha",
            "滑块",
        )
        self._login_markers = (
            "登录",
            "注册",
            "手机号登录",
            "扫码登录",
        )
        self._history_unexpected_html_retry = max(
            0, int(self.config.get("history_unexpected_html_retry", 1) or 1)
        )
        self._waf_refresh_cooldown_seconds = float(
            self.config.get("waf_refresh_cooldown_seconds", 60) or 60
        )
        self._last_waf_refresh = 0.0
        self._user_timeline_count_limit_cache = {}

    # ──────── 浏览器生命周期 ────────

    def _browser_context_options(self) -> dict:
        return {
            "user_agent": self.config.get("browser_user_agent", self.DEFAULT_USER_AGENT),
            "viewport": {"width": 1440, "height": 900},
            "locale": self.config.get("browser_locale", "zh-CN"),
            "timezone_id": self.config.get("browser_timezone", "Asia/Shanghai"),
            "color_scheme": "light",
        }

    def _apply_stealth(self):
        if not self._context:
            return

        self._context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'language', {get: () => 'zh-CN'});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
            window.chrome = window.chrome || { runtime: {} };
            """
        )
        self._context.set_extra_http_headers(
            {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            }
        )

    def _restore_session_cookies(self):
        if self._use_persistent_context and self._has_runtime_auth_cookies():
            return
        cookies = []
        if self.cookie_manager:
            cookies = self.cookie_manager.get_browser_cookies()

        if cookies:
            self._context.add_cookies(cookies)
            logger.info(f"已恢复浏览器 Cookie Jar ({len(cookies)} 个)")
            return

        token = self.cookie_manager.get_token() if self.cookie_manager else ""
        if token:
            self._context.add_cookies(
                [
                    {
                        "name": "xq_a_token",
                        "value": token,
                        "domain": ".xueqiu.com",
                        "path": "/",
                    }
                ]
            )
            logger.info("已注入 xq_a_token")

    def _persist_session(self):
        if self.cookie_manager and self._context:
            self.cookie_manager.capture_from_context(self._context)

    def _current_cookie_count(self) -> int:
        count = 0
        if self.cookie_manager:
            try:
                count = len(self.cookie_manager.get_browser_cookies())
            except Exception:
                count = 0
        if count:
            return count
        if self._context:
            try:
                return len(self._context.cookies())
            except Exception:
                return count
        return count

    def _session_profile_dir(self) -> str:
        profile_dir = self.config.get(
            "session_profile_dir",
            os.path.join(os.getcwd(), "data", "xueqiu_session_profile"),
        )
        if not os.path.isabs(profile_dir):
            profile_dir = os.path.join(os.getcwd(), profile_dir)
        os.makedirs(profile_dir, exist_ok=True)
        return profile_dir

    def _manual_verification_profile_dir(self) -> str:
        profile_dir = str(self.config.get("manual_verification_profile_dir", "") or "").strip()
        if not profile_dir:
            profile_dir = self._session_profile_dir()
        if not os.path.isabs(profile_dir):
            profile_dir = os.path.join(os.getcwd(), profile_dir)
        os.makedirs(profile_dir, exist_ok=True)
        return profile_dir

    def _runtime_storage_sidecar_path(self) -> str:
        return os.path.join(self._session_profile_dir(), "codex_runtime_storage.json")

    def _manual_verification_browser_path(self) -> str:
        configured = self.config.get("manual_verification_browser_path", "").strip()
        candidates = [configured] if configured else []
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                shutil.which("google-chrome"),
                shutil.which("chromium"),
                shutil.which("chrome"),
            ]
        )
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        raise AntiCrawlDetected("未找到可用的 Chrome 浏览器，无法进行手动访问验证")

    def _manual_verification_browser_app(self) -> str:
        browser_path = self._manual_verification_browser_path()
        marker = ".app/Contents/"
        if marker in browser_path:
            return browser_path.split(marker, 1)[0] + ".app"
        return browser_path

    def _manual_verification_browser_name(self) -> str:
        app_path = self._manual_verification_browser_app()
        base = os.path.basename(app_path)
        if base.endswith(".app"):
            return base[:-4]
        return base or "Google Chrome"

    def _build_external_verification_command(self, target_url: str) -> list[str]:
        profile_dir = self._manual_verification_profile_dir()
        if sys.platform == "darwin":
            return [
                "open",
                "-Wna",
                self._manual_verification_browser_app(),
                "--args",
                f"--user-data-dir={profile_dir}",
                "--new-window",
                "--lang=zh-CN",
                "--window-size=1440,900",
                target_url,
            ]
        return [
            self._manual_verification_browser_path(),
            f"--user-data-dir={profile_dir}",
            "--new-window",
            "--lang=zh-CN",
            "--window-size=1440,900",
            target_url,
        ]

    def _launch_browser_instance(self, headless: bool):
        if self._browser_engine not in ("playwright", "camoufox"):
            logger.warning(f"未知浏览器后端 {self._browser_engine}，回退到 playwright")
        elif self._browser_engine == "camoufox":
            logger.info("browser_engine=camoufox 已配置，但当前仍使用 Playwright 主线")

        launch_args = [
            "--no-sandbox",
            "--lang=zh-CN",
            "--window-size=1440,900",
        ]
        if headless:
            launch_args.insert(0, "--disable-blink-features=AutomationControlled")
        launch_kwargs = {
            "headless": headless,
            "args": launch_args,
        }
        channel = self.config.get("browser_channel", "")
        if self._use_persistent_context and headless:
            channel = ""
        if channel:
            launch_kwargs["channel"] = channel

        try:
            use_persistent = self._use_persistent_context or (not headless and self._manual_verification_enabled)
            if use_persistent:
                profile_dir = self._session_profile_dir() if self._use_persistent_context else self._manual_verification_profile_dir()
                persistent_kwargs = dict(launch_kwargs)
                persistent_kwargs["ignore_default_args"] = ["--enable-automation"]
                persistent_kwargs.update(self._browser_context_options())
                self._context = self._playwright.chromium.launch_persistent_context(
                    profile_dir,
                    **persistent_kwargs,
                )
                self._browser = self._context.browser
                self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
                self._manual_browser_active = not headless
            else:
                self._browser = self._playwright.chromium.launch(**launch_kwargs)
                self._context = self._browser.new_context(**self._browser_context_options())
                self._page = self._context.new_page()
                self._manual_browser_active = False
        except Exception as e:
            if channel:
                logger.warning(f"启动 {channel} 失败，回退到默认 Chromium: {e}")
                launch_kwargs.pop("channel", None)
                use_persistent = self._use_persistent_context or (not headless and self._manual_verification_enabled)
                if use_persistent:
                    profile_dir = self._session_profile_dir() if self._use_persistent_context else self._manual_verification_profile_dir()
                    persistent_kwargs = dict(launch_kwargs)
                    persistent_kwargs["ignore_default_args"] = ["--enable-automation"]
                    persistent_kwargs.update(self._browser_context_options())
                    self._context = self._playwright.chromium.launch_persistent_context(
                        profile_dir,
                        **persistent_kwargs,
                    )
                    self._browser = self._context.browser
                    self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
                    self._manual_browser_active = not headless
                else:
                    self._browser = self._playwright.chromium.launch(**launch_kwargs)
                    self._context = self._browser.new_context(**self._browser_context_options())
                    self._page = self._context.new_page()
                    self._manual_browser_active = False
            else:
                raise

        self._apply_stealth()
        self._closed = False
        self._warmed_paths.clear()

    def _ensure_live_page(self):
        if self._page:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass

        if self._context:
            try:
                pages = list(self._context.pages)
            except Exception:
                pages = []
            for page in reversed(pages):
                try:
                    if not page.is_closed():
                        self._page = page
                        return page
                except Exception:
                    continue
        return None

    def _close_runtime(self, stop_playwright: bool = False):
        for obj in [self._page, self._context, self._browser]:
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._browser = None
        self._initialized = False
        self._closed = True
        self._manual_browser_active = False
        self._warmed_paths.clear()

        if stop_playwright and self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _body_text(self) -> str:
        page = self._ensure_live_page()
        if not page:
            return None
        try:
            return page.locator("body").inner_text(timeout=3000)
        except Exception:
            return None

    def _text_has_access_verification(self, text: str) -> bool:
        if not text:
            return False
        hits = sum(1 for marker in self._verification_markers if marker in text)
        return hits >= 2 or "访问验证" in text

    def _text_has_verification_failure(self, text: str) -> bool:
        if not text:
            return False
        return bool(self._verification_failure_reason(text))

    def _verification_failure_reason(self, text: str) -> str:
        if not text:
            return ""
        normalized = " ".join(text.split())
        if "验证失败" in normalized and "请刷新重试" in normalized:
            return "验证失败/请刷新重试"
        if "验证失败" in normalized and "刷新重试" in normalized:
            return "验证失败/刷新重试"
        if "验证失败" in normalized:
            return "验证失败"
        if "请刷新重试" in normalized:
            return "请刷新重试"
        if "刷新重试" in normalized:
            return "刷新重试"
        return ""

    def _verification_failure_excerpt(self, text: str, max_chars: int = 120) -> str:
        if not text:
            return ""
        normalized = " ".join(text.split())
        for marker in self._verification_failure_markers:
            idx = normalized.find(marker)
            if idx >= 0:
                start = max(0, idx - 24)
                end = min(len(normalized), idx + max_chars)
                return normalized[start:end]
        return normalized[:max_chars]

    def _extract_verification_log_id(self, text: str) -> str:
        if not text:
            return ""
        match = re.search(r"日志ID[:：]?\s*([A-Za-z0-9_-]+)", text)
        return match.group(1) if match else ""

    def _record_verification_event(
        self,
        state: str,
        *,
        target_url: str = "",
        referer_path: str = "",
        text: str = "",
        note: str = "",
        failure_count: int | None = None,
    ):
        event = {
            "state": state,
            "target_url": target_url,
            "referer_path": referer_path,
            "log_id": self._extract_verification_log_id(text),
            "excerpt": self._verification_failure_excerpt(text),
            "cookie_count": self._current_cookie_count(),
            "failure_count": self._verification_failed_sessions if failure_count is None else failure_count,
            "note": note,
            "created_at": int(time.time() * 1000),
        }
        self._last_verification_event = event
        self._verification_events.append(event)
        logger.warning(
            "验证码事件 [%s] url=%s referer=%s log_id=%s cookies=%s failures=%s note=%s excerpt=%s",
            event["state"],
            event["target_url"] or "-",
            event["referer_path"] or "-",
            event["log_id"] or "-",
            event["cookie_count"],
            event["failure_count"],
            event["note"] or "-",
            event["excerpt"] or "-",
        )

    def _mark_verification_failure(
        self,
        state: str,
        *,
        target_url: str,
        referer_path: str = "",
        text: str = "",
        note: str = "",
    ):
        self._verification_failed_sessions += 1
        self._record_verification_event(
            state,
            target_url=target_url,
            referer_path=referer_path,
            text=text,
            note=note,
            failure_count=self._verification_failed_sessions,
        )

    def _mark_verification_recovered(
        self,
        *,
        target_url: str,
        referer_path: str = "",
        text: str = "",
        note: str = "",
    ):
        self._verification_recovered_sessions += 1
        self._record_verification_event(
            "challenge_recovered",
            target_url=target_url,
            referer_path=referer_path,
            text=text,
            note=note,
        )

    def get_verification_diagnostics(self) -> dict:
        return {
            "failed_sessions": self._verification_failed_sessions,
            "recovered_sessions": self._verification_recovered_sessions,
            "last_event": dict(self._last_verification_event) if self._last_verification_event else {},
            "events": [dict(item) for item in self._verification_events[-10:]],
        }

    def get_last_failure_meta(self) -> dict:
        return dict(self._last_failure_meta) if self._last_failure_meta else {}

    def _set_last_failure_meta(
        self,
        category: str,
        *,
        url: str = "",
        detail: str = "",
        status: int = 0,
        transport: str = "",
        **extra,
    ):
        self._last_failure_meta = {
            "category": category,
            "url": url,
            "detail": detail,
            "status": status,
            "transport": transport,
            "created_at": int(time.time() * 1000),
        }
        self._last_failure_meta.update(extra)

    def _clear_last_failure_meta(self):
        self._last_failure_meta = {}

    def _write_runtime_storage_sidecar(self, storage_state: dict):
        try:
            with open(self._runtime_storage_sidecar_path(), "w", encoding="utf-8") as f:
                json.dump(storage_state or {"local": {}, "session": {}}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"写入运行态存储 sidecar 失败: {e}")

    def _read_runtime_storage_sidecar(self) -> dict:
        path = self._runtime_storage_sidecar_path()
        if not os.path.exists(path):
            return {"local": {}, "session": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return {
                "local": data.get("local") or {},
                "session": data.get("session") or {},
            }
        except Exception as e:
            logger.warning(f"读取运行态存储 sidecar 失败: {e}")
            return {"local": {}, "session": {}}

    def _clear_runtime_storage_sidecar(self):
        try:
            path = self._runtime_storage_sidecar_path()
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _runtime_cookie_names(self) -> set[str]:
        cookies = []
        if self._context:
            try:
                cookies = self._context.cookies(["https://xueqiu.com"])
            except Exception:
                cookies = []
            if not cookies:
                try:
                    cookies = self._context.cookies()
                except Exception:
                    cookies = []
        if not cookies and self.cookie_manager:
            try:
                cookies = self.cookie_manager.get_browser_cookies()
            except Exception:
                cookies = []
        return {str(cookie.get("name", "")).strip() for cookie in cookies if cookie.get("name")}

    def _has_runtime_auth_cookies(self) -> bool:
        cookie_names = self._runtime_cookie_names()
        return "xq_a_token" in cookie_names and "u" in cookie_names

    def _has_login_wall_text(self, text: str | None) -> bool:
        normalized = str(text or "")
        if not normalized:
            return False
        hits = sum(1 for marker in self._login_markers if marker in normalized)
        return hits >= 2 and ("登录" in normalized or "注册" in normalized)

    def _excerpt_text(self, text: str | None, limit: int = 200) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        return normalized[:limit]

    def _current_session_state(self, text: str | None = None) -> str:
        text = text if text is not None else (self._body_text() or "")
        if self._text_has_access_verification(text):
            return "captcha_required"
        has_auth = self._has_runtime_auth_cookies()
        if not has_auth:
            if not text or self._has_login_wall_text(text):
                return "session_expired"
            return "unknown"
        if text and self._has_login_wall_text(text):
            return "unknown"
        return "ok"

    def _recover_unexpected_html(self, *, url: str, referer_path: str | None = None, body: str = "") -> str:
        state = self._current_session_state(body)
        logger.warning(
            "可疑 HTML 恢复: state=%s url=%s excerpt=%s",
            state,
            url,
            self._excerpt_text(body),
        )
        if state == "session_expired":
            return state

        try:
            page = self._ensure_live_page()
            if page:
                page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=15000)
                time.sleep(random.uniform(1.0, 1.8))
                self._ensure_no_access_verification("https://xueqiu.com")
                self._persist_session()
        except Exception as e:
            logger.warning(f"可疑 HTML 恢复时主页探测失败: {e}")

        if referer_path and referer_path != "/":
            try:
                self._warm_referer_path(referer_path)
            except Exception as e:
                logger.warning(f"可疑 HTML 恢复时 referer 预热失败: {e}")

        refreshed_text = self._body_text() or ""
        return self._current_session_state(refreshed_text)

    def _restore_runtime_storage(self, target_url: str, storage_state: dict | None = None):
        storage = storage_state or self._read_runtime_storage_sidecar()
        local_state = storage.get("local") or {}
        session_state = storage.get("session") or {}
        if not local_state and not session_state:
            return
        page = self._ensure_live_page()
        if not page:
            return
        try:
            if not str(page.url or "").startswith("https://xueqiu.com"):
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(0.8, 1.5))
            page.evaluate(
                """
                (storage) => {
                    for (const [key, value] of Object.entries(storage.local || {})) {
                        if (value === null || value === undefined) continue;
                        localStorage.setItem(key, value);
                    }
                    for (const [key, value] of Object.entries(storage.session || {})) {
                        if (value === null || value === undefined) continue;
                        sessionStorage.setItem(key, value);
                    }
                }
                """,
                {"local": local_state, "session": session_state},
            )
        except Exception as e:
            logger.warning(f"恢复运行态存储失败: {e}")

    def _capture_runtime_storage_state(self) -> dict:
        page = self._ensure_live_page()
        if not page:
            return {"local": {}, "session": {}}
        try:
            if not str(page.url or "").startswith("https://xueqiu.com"):
                try:
                    page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(0.8, 1.5))
                except Exception:
                    return {"local": {}, "session": {}}
            return page.evaluate(
                """() => ({
                    local: Object.fromEntries(
                        Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)])
                    ),
                    session: Object.fromEntries(
                        Object.keys(sessionStorage).map((k) => [k, sessionStorage.getItem(k)])
                    ),
                })"""
            )
        except Exception:
            return {"local": {}, "session": {}}

    def _should_auto_refresh_verification(
        self,
        *,
        now: float,
        challenge_opened_at: float,
        failure_since: float | None,
        last_refresh_at: float | None,
    ) -> bool:
        if not self._manual_verification_auto_refresh:
            return False
        if failure_since is None:
            return False
        if now - challenge_opened_at < self._manual_verification_grace_seconds:
            return False
        if now - failure_since < self._manual_verification_failure_stable_seconds:
            return False
        if last_refresh_at is not None and now - last_refresh_at < self._manual_verification_refresh_cooldown_seconds:
            return False
        return True

    def _terminate_manual_profile_processes(self):
        if not self._manual_verification_cleanup_profile_processes:
            return
        profile_dir = self._manual_verification_profile_dir()
        if not profile_dir:
            return
        try:
            subprocess.run(
                ["pkill", "-f", profile_dir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            time.sleep(1)
        except Exception:
            pass

    def _resume_session_probes(self, target_url: str, referer_path: str = ""):
        probe_urls = ["https://xueqiu.com"]
        if referer_path:
            probe_urls.append(f"https://xueqiu.com{referer_path}")
        probe_urls = probe_urls[: max(1, self._manual_verification_resume_probe_count)]
        if not probe_urls:
            return
        for idx, url in enumerate(probe_urls, 1):
            logger.info(f"验证恢复预热 {idx}/{len(probe_urls)}: {url}")
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(1.8, 3.0))
                self._ensure_no_access_verification(url)
            except Exception as e:
                logger.warning(f"验证恢复预热失败 ({url}): {e}")
                raise

    def run_verification_gate(self, referer_path: str | None = None, label: str = "") -> dict:
        if not self._manual_verification_gate_enabled:
            return self.get_verification_diagnostics()

        target_url = "https://xueqiu.com"
        if referer_path:
            target_url = f"https://xueqiu.com{referer_path}"

        logger.info(
            f"历史模式验证门禁开始: {label or referer_path or '/'}"
        )
        self._ensure_browser()
        paths = ["https://xueqiu.com"]
        if referer_path:
            paths.append(target_url)
        paths = paths[: max(1, self._manual_verification_prewarm_pages)]
        for idx, url in enumerate(paths, 1):
            logger.info(f"验证门禁预热 {idx}/{len(paths)}: {url}")
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(1.5, 2.8))
            self._ensure_no_access_verification(url)
            self._persist_session()
        if referer_path:
            self._resume_session_probes(target_url, referer_path=referer_path)
        logger.info("历史模式验证门禁通过，会话已就绪")
        return self.get_verification_diagnostics()

    def _handle_access_verification(self, target_url: str):
        if not self._manual_verification_enabled:
            raise AntiCrawlDetected(f"检测到访问验证: {target_url}")

        if self._manual_verification_mode == "external":
            self._handle_access_verification_external(target_url)
            return

        self._record_verification_event(
            "challenge_shown",
            target_url=target_url,
            text=self._body_text() or "",
            note="已暂停任务，等待人工验证",
        )
        logger.warning("已暂停任务，等待人工验证。检测到访问验证，已切换到可见 Chrome 窗口。")
        self._persist_session()

        if self.config.get("browser_headless", True):
            self._close_runtime(stop_playwright=False)
            self._launch_browser_instance(headless=False)
            self._restore_session_cookies()
            try:
                self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"打开验证页面失败 ({target_url}): {e}")

        try:
            self._page.bring_to_front()
        except Exception:
            pass

        deadline = time.time() + self._manual_verification_timeout_seconds
        challenge_opened_at = time.time()
        failure_since = None
        failure_logged = False
        last_refresh_at = None
        success_streak = 0
        while time.time() < deadline:
            if not self._ensure_live_page():
                logger.warning("验证窗口已关闭，重新打开可见 Chrome 窗口。")
                self._close_runtime(stop_playwright=False)
                self._launch_browser_instance(headless=False)
                self._restore_session_cookies()
                try:
                    self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    logger.warning(f"重新打开验证页面失败 ({target_url}): {e}")
                challenge_opened_at = time.time()
                failure_since = None
                failure_logged = False
                success_streak = 0
                time.sleep(self._manual_verification_poll_seconds)
                continue
            text = self._body_text()
            if text is None:
                time.sleep(min(1, self._manual_verification_poll_seconds))
                continue
            failure_reason = self._verification_failure_reason(text)
            if failure_reason:
                success_streak = 0
                now = time.time()
                if failure_since is None:
                    failure_since = now
                if not failure_logged:
                    excerpt = self._verification_failure_excerpt(text)
                    self._record_verification_event(
                        "challenge_failed",
                        target_url=target_url,
                        text=text,
                        note=f"failure_reason={failure_reason}",
                    )
                    logger.warning(
                        f"访问验证出现失败提示（{failure_reason}），保留当前页面等待人工重试。{excerpt}"
                    )
                    failure_logged = True
                if self._should_auto_refresh_verification(
                    now=now,
                    challenge_opened_at=challenge_opened_at,
                    failure_since=failure_since,
                    last_refresh_at=last_refresh_at,
                ):
                    logger.warning("访问验证失败提示持续存在，自动刷新验证页后重试。")
                    try:
                        self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        challenge_opened_at = time.time()
                        last_refresh_at = challenge_opened_at
                        failure_since = None
                        failure_logged = False
                    except Exception as e:
                        logger.warning(f"刷新验证页面失败 ({target_url}): {e}")
                time.sleep(self._manual_verification_poll_seconds)
                continue
            failure_since = None
            failure_logged = False
            if not self._text_has_access_verification(text):
                success_streak += 1
                if success_streak >= max(1, self._manual_verification_success_checks):
                    self._persist_session()
                    self._warmed_paths.clear()
                    self._closed = False
                    self._initialized = True
                    self._mark_verification_recovered(
                        target_url=target_url,
                        text=text,
                        note="会话恢复完成",
                    )
                    logger.info("访问验证已完成，继续执行。")
                    return
            else:
                success_streak = 0
            time.sleep(self._manual_verification_poll_seconds)

        self._mark_verification_failure(
            "challenge_timeout",
            target_url=target_url,
            note="访问验证未在限定时间内完成",
        )
        raise CaptchaRequired("访问验证未在限定时间内完成")

    def _launch_external_verification_browser(self, target_url: str):
        cmd = self._build_external_verification_command(target_url)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._focus_external_verification_browser()
        return proc

    def _focus_external_verification_browser(self):
        if sys.platform != "darwin":
            return
        app_name = self._manual_verification_browser_name()
        scripts = [
            f'tell application "{app_name}" to activate',
            (
                'tell application "System Events" '
                f'to tell process "{app_name}" to set frontmost to true'
            ),
        ]
        for script in scripts:
            try:
                subprocess.run(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                pass

    def _capture_cookies_from_manual_profile(self, target_url: str, referer_path: str = ""):
        if not self._playwright:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()

        profile_dir = self._manual_verification_profile_dir()
        self._terminate_manual_profile_processes()
        launch_kwargs = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--lang=zh-CN",
                "--window-size=1440,900",
            ],
            "ignore_default_args": ["--enable-automation"],
        }

        persistent_kwargs = dict(launch_kwargs)
        persistent_kwargs.update(self._browser_context_options())
        captured_storage = {"local": {}, "session": {}}
        ctx = self._playwright.chromium.launch_persistent_context(
            profile_dir,
            **persistent_kwargs,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            try:
                text = page.locator("body").inner_text(timeout=5000)
            except Exception:
                text = ""
            if self._text_has_access_verification(text):
                self._mark_verification_failure(
                    "challenge_passed_but_blocked",
                    target_url=target_url,
                    referer_path=referer_path,
                    text=text,
                    note="关闭验证窗口后仍停留在访问验证页",
                )
                raise CaptchaRequired("手动验证完成后仍停留在访问验证页")
            if self.cookie_manager:
                self.cookie_manager.capture_from_context(ctx)
            try:
                captured_storage = page.evaluate(
                    """() => ({
                        local: Object.fromEntries(
                            Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)])
                        ),
                        session: Object.fromEntries(
                            Object.keys(sessionStorage).map((k) => [k, sessionStorage.getItem(k)])
                        ),
                    })"""
                ) or {"local": {}, "session": {}}
            except Exception:
                captured_storage = {"local": {}, "session": {}}
        finally:
            try:
                ctx.close()
            except Exception:
                pass
        return captured_storage

    def _seed_manual_profile_from_cookie_jar(self, target_url: str, storage_state: dict | None = None):
        if self._profile_bootstrap_mode == "never":
            return
        if not self._playwright:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()

        cookies = self.cookie_manager.get_browser_cookies() if self.cookie_manager else []
        if not cookies:
            return

        profile_dir = self._manual_verification_profile_dir()
        launch_kwargs = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--lang=zh-CN",
                "--window-size=1440,900",
            ],
            "ignore_default_args": ["--enable-automation"],
        }

        persistent_kwargs = dict(launch_kwargs)
        persistent_kwargs.update(self._browser_context_options())
        ctx = self._playwright.chromium.launch_persistent_context(
            profile_dir,
            **persistent_kwargs,
        )
        try:
            ctx.add_cookies(cookies)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=30000)
            storage = storage_state or {"local": {}, "session": {}}
            local_state = storage.get("local") or {}
            session_state = storage.get("session") or {}
            if local_state or session_state:
                page.evaluate(
                    """
                    (storage) => {
                        for (const [key, value] of Object.entries(storage.local || {})) {
                            if (value === null || value === undefined) continue;
                            localStorage.setItem(key, value);
                        }
                        for (const [key, value] of Object.entries(storage.session || {})) {
                            if (value === null || value === undefined) continue;
                            sessionStorage.setItem(key, value);
                        }
                    }
                    """,
                    {"local": local_state, "session": session_state},
                )
            time.sleep(random.uniform(1.0, 1.8))
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(0.8, 1.5))
            except Exception:
                pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    def _restore_runtime_after_external_verification(
        self,
        target_url: str,
        referer_path: str = "",
        storage_state: dict | None = None,
    ):
        self._close_runtime(stop_playwright=False)
        self._launch_browser_instance(headless=self.config.get("browser_headless", True))
        self._restore_session_cookies()
        try:
            self._page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"外部验证后恢复主页失败: {e}")
        time.sleep(random.uniform(1.0, 2.0))
        self._restore_runtime_storage(target_url, storage_state=storage_state)
        session_state = self._current_session_state()
        if session_state == "captcha_required":
            self._clear_runtime_storage_sidecar()
            self._mark_verification_failure(
                "challenge_passed_but_blocked",
                target_url=target_url,
                referer_path=referer_path,
                text=self._body_text() or "",
                note="关闭验证窗口后仍停留在访问验证页",
            )
            raise CaptchaRequired("手动验证完成后仍停留在访问验证页")
        if self._session_probe_required and session_state == "session_expired":
            self._clear_runtime_storage_sidecar()
            self._mark_verification_failure(
                "session_expired",
                target_url=target_url,
                referer_path=referer_path,
                text=self._body_text() or "",
                note="关闭验证窗口后仍未恢复登录态",
            )
            raise CaptchaRequired("检测到登录失效，请先登录雪球后重试")
        if self._manual_verification_resume_probe_count > 0:
            self._resume_session_probes(target_url, referer_path=referer_path)
        self._persist_session()
        self._initialized = True
        self._closed = False
        self._clear_runtime_storage_sidecar()

    def probe_user_timeline_access(
        self,
        user_id: str,
        *,
        referer_path: str | None = None,
        count: int = 1,
        page: int = 1,
        timeout_ms: int = 15000,
        log_success: bool = False,
    ) -> dict:
        referer_path = referer_path or f"/u/{user_id}"
        url = build_user_timeline_url()
        params = build_user_timeline_params(user_id, count=max(1, int(count or 1)), page=max(1, int(page or 1)))
        try:
            data = self.get(
                url,
                params=params,
                referer_path=referer_path,
                timeout_ms=timeout_ms,
                max_retries=1,
                transport="page",
            )
            statuses = data.get("statuses") or []
            if log_success:
                logger.info(
                    "用户时间线探测通过: user_id=%s statuses=%s auth_cookies=%s",
                    user_id,
                    len(statuses),
                    sorted(self._runtime_cookie_names()),
                )
            return {
                "ok": True,
                "user_id": str(user_id),
                "count": int(count),
                "page": int(page),
                "status_count": len(statuses),
                "max_page": int(data.get("maxPage", 0) or 0),
                "has_auth_cookies": self._has_runtime_auth_cookies(),
                "auth_cookie_names": sorted(self._runtime_cookie_names()),
            }
        except RequestFailed as e:
            failure = self.get_last_failure_meta()
            return {
                "ok": False,
                "user_id": str(user_id),
                "count": int(count),
                "page": int(page),
                "category": e.category,
                "detail": e.detail,
                "message": str(e),
                "failure_meta": failure,
                "has_auth_cookies": self._has_runtime_auth_cookies(),
                "auth_cookie_names": sorted(self._runtime_cookie_names()),
            }
        except Exception as e:
            failure = self.get_last_failure_meta()
            return {
                "ok": False,
                "user_id": str(user_id),
                "count": int(count),
                "page": int(page),
                "category": "probe_failed",
                "detail": str(e),
                "message": f"{type(e).__name__}: {e}",
                "failure_meta": failure,
                "has_auth_cookies": self._has_runtime_auth_cookies(),
                "auth_cookie_names": sorted(self._runtime_cookie_names()),
            }

    def get_user_timeline_count_limit(self, user_id: str) -> int:
        return int(self._user_timeline_count_limit_cache.get(str(user_id), 0) or 0)

    def set_user_timeline_count_limit(self, user_id: str, limit: int) -> None:
        if int(limit or 0) > 0:
            self._user_timeline_count_limit_cache[str(user_id)] = int(limit)

    def _user_timeline_probe_counts(self, preferred_count: int, probe_candidates=None) -> list[int]:
        configured = probe_candidates or self.config.get("user_timeline_probe_counts", [40, 30, 20])
        values = []
        for raw in [preferred_count, *(configured or [])]:
            try:
                val = int(raw or 0)
            except Exception:
                continue
            if val > 0 and val not in values:
                values.append(val)
        return sorted(values, reverse=True)

    def resolve_user_timeline_count_limit(
        self,
        user_id: str,
        *,
        referer_path: str | None = None,
        preferred_count: int = 20,
        probe_candidates=None,
        page: int = 1,
        timeout_ms: int = 15000,
    ) -> dict:
        referer_path = referer_path or f"/u/{user_id}"
        last_probe = {}
        for count in self._user_timeline_probe_counts(preferred_count, probe_candidates):
            probe = self.probe_user_timeline_access(
                user_id,
                referer_path=referer_path,
                count=count,
                page=page,
                timeout_ms=timeout_ms,
                log_success=True,
            )
            last_probe = probe
            if probe.get("ok"):
                self.set_user_timeline_count_limit(user_id, count)
                probe["resolved_count"] = int(count)
                return probe
            category = str(probe.get("category") or "")
            if category not in {"http_400_10022", "timeline_count_limit"}:
                return probe
        if last_probe:
            last_probe["resolved_count"] = 0
        return last_probe

    def _wait_for_user_timeline_login(
        self,
        user_id: str,
        *,
        screen_name: str = "",
        probe_count: int = 1,
        probe_candidates=None,
        timeout_seconds: int | None = None,
    ) -> dict:
        timeout_seconds = int(timeout_seconds or self._manual_verification_timeout_seconds or 300)
        target_url = f"https://xueqiu.com/u/{user_id}"
        referer_path = f"/u/{user_id}"
        if self.config.get("browser_headless", True):
            self._close_runtime(stop_playwright=False)
            self._launch_browser_instance(headless=False)
            self._restore_session_cookies()
        page = self._ensure_live_page()
        if not page:
            raise RequestFailed("user_timeline_probe_failed", "无法打开用于登录的浏览器窗口", url=target_url)
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"打开用户登录页失败 ({target_url}): {e}")
        try:
            page.bring_to_front()
        except Exception:
            pass

        display_name = f"{screen_name}({user_id})" if screen_name else f"用户 {user_id}"
        logger.warning(
            "用户时间线接口尚未就绪，请在当前爬虫浏览器窗口完成雪球登录/重新登录。"
            "程序会持续探测 user_timeline.json，探测通过后自动继续。目标: %s",
            display_name,
        )
        deadline = time.time() + timeout_seconds
        last_refresh_at = 0.0
        while time.time() < deadline:
            if not self._ensure_live_page():
                raise RequestFailed(
                    "user_timeline_probe_failed",
                    "登录窗口已关闭，无法继续验证用户时间线接口",
                    url=target_url,
                )
            now = time.time()
            if now - last_refresh_at >= 20:
                try:
                    self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                last_refresh_at = now
            time.sleep(max(1.0, min(self._manual_verification_poll_seconds, 3.0)))
            probe = self.probe_user_timeline_access(
                user_id,
                referer_path=referer_path,
                count=probe_count,
                page=1,
                timeout_ms=12000,
            )
            if probe.get("ok"):
                self._persist_session()
                self._warmed_paths.clear()
                try:
                    self._warm_referer_path(referer_path)
                except Exception:
                    pass
                logger.info("用户时间线接口验证通过，继续执行: user_id=%s", user_id)
                return probe
            resolved = self.resolve_user_timeline_count_limit(
                user_id,
                referer_path=referer_path,
                preferred_count=probe_count,
                probe_candidates=probe_candidates,
                page=1,
                timeout_ms=12000,
            )
            if resolved.get("ok"):
                self._persist_session()
                self._warmed_paths.clear()
                try:
                    self._warm_referer_path(referer_path)
                except Exception:
                    pass
                logger.info(
                    "用户时间线接口验证通过，继续执行: user_id=%s resolved_count=%s",
                    user_id,
                    resolved.get("resolved_count"),
                )
                return resolved
        raise RequestFailed(
            "user_timeline_probe_failed",
            f"等待用户时间线接口就绪超时: {user_id}",
            url=target_url,
            detail="等待手动登录后接口验证通过超时",
        )

    def ensure_user_timeline_ready(
        self,
        user_id: str,
        *,
        screen_name: str = "",
        probe_count: int = 1,
        probe_candidates=None,
        max_manual_attempts: int = 1,
    ) -> dict:
        referer_path = f"/u/{user_id}"
        target_url = f"https://xueqiu.com{referer_path}"
        self._ensure_browser()
        try:
            self._warm_referer_path(referer_path)
        except Exception as e:
            logger.warning(f"用户时间线探测前预热失败 ({target_url}): {e}")

        last_probe = self.resolve_user_timeline_count_limit(
            user_id,
            referer_path=referer_path,
            preferred_count=probe_count,
            probe_candidates=probe_candidates,
            page=1,
        )
        if last_probe.get("ok"):
            return last_probe

        for attempt in range(1, max_manual_attempts + 1):
            category = str(last_probe.get("category") or "")
            detail = str(last_probe.get("detail") or last_probe.get("message") or "")
            logger.warning(
                "用户时间线接口未就绪: user_id=%s category=%s detail=%s auth_cookies=%s",
                user_id,
                category or "-",
                detail or "-",
                last_probe.get("auth_cookie_names") or [],
            )
            if not self._manual_verification_enabled:
                break
            logger.warning(
                "开始第 %s/%s 次手动登录验证，目标接口: %s",
                attempt,
                max_manual_attempts,
                target_url,
            )
            last_probe = self._wait_for_user_timeline_login(
                user_id,
                screen_name=screen_name,
                probe_count=probe_count,
                probe_candidates=probe_candidates,
            )
            if last_probe.get("ok"):
                return last_probe

        raise RequestFailed(
            str(last_probe.get("category") or "user_timeline_probe_failed"),
            f"用户时间线接口未就绪: {last_probe.get('message') or last_probe.get('detail') or target_url}",
            url=target_url,
            detail=str(last_probe.get("detail") or last_probe.get("message") or ""),
        )

    def _handle_access_verification_external(self, target_url: str):
        referer_path = ""
        if target_url.startswith("https://xueqiu.com/"):
            referer_path = target_url.replace("https://xueqiu.com", "", 1)
        session_state = self._current_session_state()
        self._record_verification_event(
            "challenge_shown",
            target_url=target_url,
            referer_path=referer_path,
            text="",
            note="已暂停任务，等待人工验证",
        )
        if session_state == "session_expired":
            logger.warning(
                "已暂停任务，等待人工验证。当前登录态已失效；"
                "请在打开的 Chrome 中重新登录后完成验证。"
            )
        else:
            logger.warning(
                "已暂停任务，等待人工验证。程序会继续使用同一登录会话；"
                "若窗口中已登录，只需完成验证，不需要再次输入账号密码。"
            )
        if sys.platform == "darwin":
            logger.warning(
                "验证完成后，请退出这个专用验证 Chrome 实例（Command+Q）；"
                "仅关闭标签页或窗口，程序仍会继续等待。"
            )
        else:
            logger.warning(
                "验证完成后，请关闭这个专用验证 Chrome 实例；"
                "仅关闭标签页可能不会结束等待。"
            )
        logger.warning(
            "若没有看到验证窗口，请检查 Dock 或桌面空间，程序已经尝试将其切到前台。"
        )
        self._persist_session()
        storage_state = self._capture_runtime_storage_state()
        self._write_runtime_storage_sidecar(storage_state)
        self._close_runtime(stop_playwright=False)
        self._terminate_manual_profile_processes()
        if (
            self._profile_bootstrap_mode != "never"
            and not os.listdir(self._manual_verification_profile_dir())
        ):
            self._seed_manual_profile_from_cookie_jar(target_url, storage_state=storage_state)

        proc = self._launch_external_verification_browser(target_url)
        logger.warning(
            "手动验证窗口已启动: pid=%s profile=%s url=%s",
            getattr(proc, "pid", "-"),
            self._manual_verification_profile_dir(),
            target_url,
        )
        deadline = time.time() + self._manual_verification_timeout_seconds
        focus_retry_done = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if not focus_retry_done:
                self._focus_external_verification_browser()
                focus_retry_done = True
            time.sleep(self._manual_verification_poll_seconds)

        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            self._terminate_manual_profile_processes()
            self._mark_verification_failure(
                "challenge_timeout",
                target_url=target_url,
                referer_path=referer_path,
                note="访问验证窗口在限定时间内未关闭",
            )
            self._clear_runtime_storage_sidecar()
            raise CaptchaRequired("访问验证未在限定时间内完成，请关闭手动验证窗口后重试")

        captured_storage = self._capture_cookies_from_manual_profile(target_url, referer_path=referer_path)
        self._restore_runtime_after_external_verification(
            target_url,
            referer_path=referer_path,
            storage_state=captured_storage,
        )
        self._mark_verification_recovered(
            target_url=target_url,
            referer_path=referer_path,
            note="会话有效，继续执行",
        )
        logger.info("访问验证已完成，继续执行。")

    def _ensure_no_access_verification(self, target_url: str):
        text = self._body_text()
        if self._text_has_access_verification(text):
            self._handle_access_verification(target_url)

    def _ensure_browser(self):
        """懒初始化：首次请求时才启动浏览器"""
        if self._initialized and self._browser and self._context and self._ensure_live_page():
            return
        if self._initialized:
            self._close_runtime(stop_playwright=False)

        from playwright.sync_api import sync_playwright

        logger.info("初始化：启动浏览器引擎...")
        if not self._playwright:
            self._playwright = sync_playwright().start()
        self._launch_browser_instance(headless=self.config.get("browser_headless", True))

        logger.info("初始化：访问雪球主页（通过 WAF 挑战）...")
        try:
            self._page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.warning(f"主页 networkidle 超时，改用 domcontentloaded: {e}")
            self._page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(1.5, 2.5))
        self._ensure_no_access_verification("https://xueqiu.com")

        self._restore_session_cookies()

        try:
            self._page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.warning(f"主页二次访问 networkidle 超时，改用 domcontentloaded: {e}")
            self._page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(1.0, 2.0))
        self._ensure_no_access_verification("https://xueqiu.com")
        self._persist_session()

        self._initialized = True
        logger.info("浏览器引擎初始化完成，WAF 挑战已通过")

    def _warm_referer_path(self, referer_path: str | None):
        if not referer_path or referer_path == "/" or not self.config.get("warm_referer_paths", True):
            return
        if referer_path in self._warmed_paths:
            return

        url = f"https://xueqiu.com{referer_path}"
        try:
            logger.info(f"暖场 referer 页面: {referer_path}")
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(1.2, 2.2))
            self._ensure_no_access_verification(url)
            self._persist_session()
            self._warmed_paths.add(referer_path)
        except Exception as e:
            logger.warning(f"暖场 referer 页面失败 ({referer_path}): {e}")

    def _classify_request_failure(
        self,
        *,
        status: int,
        content_type: str,
        body: str,
        error: str,
    ) -> dict:
        normalized_error = (error or "").strip()
        normalized_body = (body or "").strip()
        lower_error = normalized_error.lower()
        lower_body = normalized_body.lower()
        lower_ct = (content_type or "").lower()

        if status == 403:
            return {"category": "http_forbidden", "detail": "HTTP 403"}

        if normalized_error:
            if "err_internet_disconnected" in lower_error or "internet_disconnected" in lower_error:
                return {"category": "transport_failure", "detail": normalized_error}
            if (
                "wait_for_function" in lower_error
                and "timeout" in lower_error
            ) or "request timed out" in lower_error or "aborterror" in lower_error or "aborted" in lower_error:
                return {"category": "transport_timeout", "detail": normalized_error}
            if (
                "no live page" in lower_error
                or "targetclosederror" in lower_error
                or "has been closed" in lower_error
                or "browser has been closed" in lower_error
            ):
                return {"category": "page_dead", "detail": normalized_error}
            return {"category": "transport_failure", "detail": normalized_error}

        if "text/html" in lower_ct or normalized_body.startswith("<"):
            if self._text_has_access_verification(normalized_body):
                return {"category": "captcha_required", "detail": "访问验证页面"}
            if any(marker in lower_body for marker in self._waf_markers):
                return {"category": "explicit_waf", "detail": "WAF/验证码 HTML"}
            return {"category": "unexpected_html", "detail": "非预期 HTML 响应"}

        return {"category": "", "detail": ""}

    def _recover_transport_failure(self, *, reason: str, referer_path: str | None = None):
        logger.warning(f"请求传输恢复: {reason}")
        try:
            self.close()
        except Exception as e:
            logger.warning(f"关闭浏览器失败: {e}")
        time.sleep(random.uniform(2.0, 4.0))
        self._ensure_browser()
        if referer_path:
            self._warm_referer_path(referer_path)

    def _request_via_fetch_page(self, page, full_url: str, timeout_ms: int) -> dict:
        req_key = f"req_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
        try:
            page.evaluate(
                """
                async (config) => {
                    window.__codexFetchResults = window.__codexFetchResults || {};
                    window.__codexFetchResults[config.key] = { done: false, ok: false, status: 0, contentType: '', body: '', error: '' };
                    const ctrl = new AbortController();
                    const timer = setTimeout(() => ctrl.abort(), config.timeoutMs);

                    fetch(config.url, {
                        signal: ctrl.signal,
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json',
                            'X-Requested-With': 'XMLHttpRequest',
                        }
                    })
                    .then(async (resp) => {
                        const text = await resp.text();
                        window.__codexFetchResults[config.key] = {
                            done: true,
                            ok: resp.ok,
                            status: resp.status,
                            contentType: resp.headers.get('content-type') || '',
                            body: text,
                            error: '',
                        };
                    })
                    .catch((err) => {
                        window.__codexFetchResults[config.key] = {
                            done: true,
                            ok: false,
                            status: 0,
                            contentType: '',
                            body: '',
                            error: err && err.message ? err.message : String(err),
                        };
                    })
                    .finally(() => {
                        clearTimeout(timer);
                    });
                    return true;
                }
                """,
                {"url": full_url, "timeoutMs": timeout_ms, "key": req_key},
            )
            page.wait_for_function(
                """
                (config) => {
                    return Boolean(
                        window.__codexFetchResults &&
                        window.__codexFetchResults[config.key] &&
                        window.__codexFetchResults[config.key].done
                    );
                }
                """,
                arg={"key": req_key},
                timeout=timeout_ms,
            )
            return page.evaluate(
                """
                (config) => {
                    const entry = (window.__codexFetchResults || {})[config.key] || null;
                    if (window.__codexFetchResults) {
                        delete window.__codexFetchResults[config.key];
                    }
                    return entry || { done: true, ok: false, status: 0, contentType: '', body: '', error: 'missing result' };
                }
                """,
                {"key": req_key},
            )
        except Exception as e:
            try:
                page.evaluate(
                    """
                    (config) => {
                        if (window.__codexFetchResults) {
                            delete window.__codexFetchResults[config.key];
                        }
                    }
                    """,
                    {"key": req_key},
                )
            except Exception:
                pass
            return {"ok": False, "status": 0, "error": str(e), "body": "", "contentType": ""}

    # ──────── 请求 ────────

    def _build_url(self, url, params):
        if params:
            p = dict(params)
            p["_"] = str(int(time.time() * 1000))
            return f"{url}?{urlencode(p)}"
        return url

    def _request_via_page(
        self,
        full_url: str,
        referer: str,
        timeout_ms: int,
        transport: str = "auto",
    ) -> dict:
        if transport == "navigate":
            temp_page = self._context.new_page()
            try:
                resp = temp_page.goto(
                    full_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                    referer=referer,
                )
                if resp is None:
                    return {"ok": False, "status": 0, "contentType": "", "body": ""}
                return {
                    "ok": resp.ok,
                    "status": resp.status,
                    "contentType": (resp.headers.get("content-type") if hasattr(resp, "headers") else "") or "",
                    "body": resp.text(),
                }
            finally:
                temp_page.close()

        if transport == "isolated_page":
            temp_page = self._context.new_page()
            try:
                try:
                    temp_page.goto(referer, wait_until="domcontentloaded", timeout=min(timeout_ms, 10000))
                    time.sleep(random.uniform(0.8, 1.5))
                except Exception as e:
                    return {"ok": False, "status": 0, "error": f"isolated referer load failed: {e}", "body": "", "contentType": ""}
                return self._request_via_fetch_page(temp_page, full_url, timeout_ms)
            finally:
                try:
                    temp_page.close()
                except Exception:
                    pass

        page = self._ensure_live_page()
        if transport != "page" and not self._manual_browser_active and self._context and getattr(self._context, "request", None):
            resp = self._context.request.get(
                full_url,
                headers={
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer,
                },
                timeout=timeout_ms,
                fail_on_status_code=False,
                max_redirects=2,
            )
            result = {
                "ok": resp.ok,
                "status": resp.status,
                "contentType": (resp.headers.get("content-type") if hasattr(resp, "headers") else "") or "",
                "body": resp.text(),
            }
            body = result.get("body", "")
            content_type = result.get("contentType", "")
            if transport == "request":
                return result
            if not ("text/html" in content_type or body.strip().startswith("<")):
                return result

        if not page:
            return {"ok": False, "status": 0, "error": "no live page", "body": "", "contentType": ""}
        return self._request_via_fetch_page(page, full_url, timeout_ms)

    def get(
        self,
        url: str,
        params: dict = None,
        referer_path: str = None,
        timeout_ms: int | None = None,
        max_retries: int | None = None,
        transport: str = "auto",
    ) -> dict:
        full_url = self._build_url(url, params)
        referer = f"https://xueqiu.com{referer_path}" if referer_path else "https://xueqiu.com"
        timeout_ms = max(1000, int(timeout_ms or self.config.get("request_timeout_ms", 25000) or 25000))

        max_retries = max(1, int(max_retries or self.config.get("max_retries", 3) or 3))
        attempt = 0
        unexpected_html_extra_retries = self._history_unexpected_html_retry
        while True:
            attempt += 1
            self._ensure_browser()
            self._warm_referer_path(referer_path)
            self.rate_limiter.wait()
            request_started = time.time()
            try:
                self._clear_last_failure_meta()
                result = self._request_via_page(
                    full_url,
                    referer,
                    timeout_ms=timeout_ms,
                    transport=transport,
                )
                elapsed = time.time() - request_started
                if elapsed >= self._slow_request_seconds:
                    logger.warning(
                        f"慢请求 {elapsed:.1f}s (尝试 {attempt}/{max_retries}): {full_url}"
                    )
                status = result.get("status", 0)
                body = result.get("body", "")
                content_type = result.get("contentType", "")
                failure = self._classify_request_failure(
                    status=status,
                    content_type=content_type,
                    body=body,
                    error=result.get("error", "") or "",
                )
                category = failure.get("category", "")
                detail = failure.get("detail", "")

                if category == "explicit_waf":
                    self._set_last_failure_meta(category, url=url, detail=detail, status=status, transport=transport)
                    logger.warning(f"WAF 拦截 (尝试 {attempt}/{max_retries}): {url}")
                    self.rate_limiter.on_failure()
                    if attempt < max_retries:
                        self._refresh_waf()
                        wait = random.uniform(10, 20)
                        logger.info(f"等待 {wait:.0f}s 后重试...")
                        time.sleep(wait)
                        continue
                    raise AntiCrawlDetected(f"WAF 反复拦截: {url}")

                if category == "captcha_required":
                    self._set_last_failure_meta(category, url=url, detail=detail, status=status, transport=transport)
                    self._ensure_no_access_verification(referer)
                    if attempt < max_retries:
                        time.sleep(random.uniform(5, 10))
                        continue
                    raise CaptchaRequired(f"访问验证: {url}")

                if category == "http_forbidden":
                    self._set_last_failure_meta(category, url=url, detail=detail, status=status, transport=transport)
                    self.rate_limiter.on_failure()
                    logger.warning(f"HTTP 403 (尝试 {attempt}/{max_retries}): {url}")
                    if attempt < max_retries:
                        self._refresh_waf()
                        time.sleep(random.uniform(12, 24))
                        continue
                    raise AntiCrawlDetected(f"HTTP 403: {url}")

                if category in ("transport_timeout", "page_dead", "transport_failure"):
                    self._set_last_failure_meta(category, url=url, detail=detail, status=status, transport=transport)
                    self.rate_limiter.on_failure()
                    label = {
                        "transport_timeout": "时间线超时",
                        "page_dead": "页面已失活",
                        "transport_failure": "页面内请求失败",
                    }.get(category, "请求失败")
                    logger.warning(
                        f"{label} (尝试 {attempt}/{max_retries}): {url} | {detail}"
                    )
                    if attempt < max_retries:
                        self._recover_transport_failure(
                            reason=f"{category}: {detail}",
                            referer_path=referer_path,
                        )
                        time.sleep(random.uniform(8, 14))
                        continue
                    raise RequestFailed(category, f"{label}: {url} | {detail}", url=url, detail=detail)

                if category == "unexpected_html":
                    self._set_last_failure_meta(
                        category,
                        url=url,
                        detail=detail,
                        status=status,
                        transport=transport,
                        has_auth_cookies=self._has_runtime_auth_cookies(),
                        auth_cookie_names=sorted(self._runtime_cookie_names()),
                        html_excerpt=self._excerpt_text(body),
                    )
                    self.rate_limiter.on_failure()
                    logger.warning(f"可疑 HTML 响应 (尝试 {attempt}/{max_retries}): {url}")
                    can_retry = attempt < max_retries or unexpected_html_extra_retries > 0
                    if can_retry:
                        if attempt >= max_retries:
                            unexpected_html_extra_retries -= 1
                        session_state = self._recover_unexpected_html(
                            url=url,
                            referer_path=referer_path,
                            body=body,
                        )
                        if session_state == "session_expired":
                            self._set_last_failure_meta(
                                "session_expired",
                                url=url,
                                detail="主页/目标页探测确认登录态失效",
                                status=status,
                                transport=transport,
                                has_auth_cookies=self._has_runtime_auth_cookies(),
                                auth_cookie_names=sorted(self._runtime_cookie_names()),
                                html_excerpt=self._excerpt_text(body),
                            )
                            raise RequestFailed(
                                "session_expired",
                                f"检测到登录态失效: {url}",
                                url=url,
                                detail="主页/目标页探测确认登录态失效",
                            )
                        time.sleep(random.uniform(6, 12))
                        continue
                    raise RequestFailed(category, f"非预期 HTML 响应: {url}", url=url, detail=detail)

                looks_like_json = (
                    "json" in str(content_type or "").lower()
                    or str(body or "").lstrip().startswith("{")
                    or str(body or "").lstrip().startswith("[")
                )
                if not result.get("ok") and not looks_like_json:
                    self._set_last_failure_meta("http_error", url=url, detail=body[:200], status=status, transport=transport)
                    raise AntiCrawlDetected(f"HTTP {status}: {url} | {body[:200]}")

                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    if not result.get("ok"):
                        self._set_last_failure_meta("http_error", url=url, detail=body[:200], status=status, transport=transport)
                        raise AntiCrawlDetected(f"HTTP {status}: {url} | {body[:200]}")
                    self._set_last_failure_meta("non_json", url=url, detail=body[:120], status=status, transport=transport)
                    raise AntiCrawlDetected(f"非 JSON 响应: {url}")

                if isinstance(data, dict):
                    ec = data.get("error_code")
                    if ec and ec != 0:
                        ec_str = str(ec)
                        msg = data.get("error_description", "未知错误")
                        self._last_fetch_error = f"{ec}: {msg}"
                        if ec_str == "10022":
                            requested_count = 0
                            if isinstance(params, dict):
                                try:
                                    requested_count = int(params.get("count") or 0)
                                except Exception:
                                    requested_count = 0
                            requested_user_id = ""
                            if isinstance(params, dict):
                                requested_user_id = str(params.get("user_id") or "")
                            known_limit = (
                                self.get_user_timeline_count_limit(requested_user_id)
                                if requested_user_id
                                else 0
                            )
                            if (
                                requested_user_id
                                and requested_count > 0
                                and known_limit > 0
                                and requested_count > known_limit
                                and self._has_runtime_auth_cookies()
                            ):
                                self._set_last_failure_meta(
                                    "timeline_count_limit",
                                    url=url,
                                    detail=f"{msg} | requested_count={requested_count} > known_limit={known_limit}",
                                    status=status,
                                    transport=transport,
                                    has_auth_cookies=self._has_runtime_auth_cookies(),
                                    auth_cookie_names=sorted(self._runtime_cookie_names()),
                                )
                                raise RequestFailed(
                                    "timeline_count_limit",
                                    f"用户时间线 count 超限: requested_count={requested_count}, known_limit={known_limit}",
                                    url=url,
                                    detail=msg,
                                )
                            self._set_last_failure_meta(
                                "http_400_10022",
                                url=url,
                                detail=msg,
                                status=status,
                                transport=transport,
                                has_auth_cookies=self._has_runtime_auth_cookies(),
                                auth_cookie_names=sorted(self._runtime_cookie_names()),
                            )
                            logger.warning(f"用户时间线轻恢复: category=http_400_10022 detail={msg}")
                            can_retry = attempt < max_retries or unexpected_html_extra_retries > 0
                            if can_retry:
                                if attempt >= max_retries:
                                    unexpected_html_extra_retries -= 1
                                session_state = self._recover_unexpected_html(
                                    url=url,
                                    referer_path=referer_path,
                                    body=msg,
                                )
                                if session_state == "session_expired":
                                    self._set_last_failure_meta(
                                        "session_expired",
                                        url=url,
                                        detail="主页/目标页探测确认登录态失效",
                                        status=status,
                                        transport=transport,
                                        has_auth_cookies=self._has_runtime_auth_cookies(),
                                        auth_cookie_names=sorted(self._runtime_cookie_names()),
                                    )
                                    raise RequestFailed(
                                        "session_expired",
                                        f"检测到登录态失效: {url}",
                                        url=url,
                                        detail="主页/目标页探测确认登录态失效",
                                    )
                                time.sleep(random.uniform(4, 8))
                                continue
                            raise RequestFailed(
                                "http_400_10022",
                                f"需要登录或权限不足: {msg}",
                                url=url,
                                detail=msg,
                            )
                        if "login" in str(msg).lower() or ec_str in {"20019", "400016"}:
                            self._set_last_failure_meta(
                                "session_expired",
                                url=url,
                                detail=msg,
                                status=status,
                                transport=transport,
                                has_auth_cookies=self._has_runtime_auth_cookies(),
                                auth_cookie_names=sorted(self._runtime_cookie_names()),
                            )
                            raise RequestFailed(
                                "session_expired",
                                f"需要登录或权限不足: {msg}",
                                url=url,
                                detail=msg,
                            )
                        logger.warning(f"业务错误 {ec}: {msg}")

                self._clear_last_failure_meta()
                self.rate_limiter.on_success()
                return data

            except (AntiCrawlDetected, RequestFailed):
                raise
            except Exception as e:
                elapsed = time.time() - request_started
                logger.error(
                    f"请求异常 (尝试 {attempt}/{max_retries}, {elapsed:.1f}s): {e}"
                )
                if attempt >= max_retries:
                    raise
                if "ERR_INTERNET_DISCONNECTED" in str(e) or "internet_disconnected" in str(e).lower():
                    logger.warning("检测到网络断连，短暂停顿后重试")
                    time.sleep(random.uniform(6, 10))
                else:
                    time.sleep(random.uniform(5, 15))

    def get_raw(self, url: str, params: dict = None) -> dict:
        return self.get(url, params)

    # ──────── 会话刷新 ────────

    def _refresh_waf(self):
        now = time.time()
        if now - self._last_waf_refresh < self._waf_refresh_cooldown_seconds:
            logger.info("WAF 刷新冷却中，跳过重复刷新")
            return
        self._last_waf_refresh = now
        logger.info("重新通过 WAF 挑战...")
        try:
            try:
                self._page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
            except Exception as e:
                logger.warning(f"WAF 刷新 networkidle 超时，改用 domcontentloaded: {e}")
                self._page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2.5, 4.0))
            self._ensure_no_access_verification("https://xueqiu.com")
            self._persist_session()
        except Exception as e:
            logger.warning(f"WAF 刷新失败: {e}")

    def visit_homepage(self):
        self._ensure_browser()

    def verify_cookie(self) -> bool:
        self._ensure_browser()
        try:
            result = self.get(
                "https://xueqiu.com/query/v1/symbol/search/status.json",
                params={"symbol": "SH600519", "count": 1, "sort": "time", "page": 1, "source": "all"},
                referer_path="/S/SH600519",
            )
            return bool(result and result.get("list") is not None)
        except Exception:
            return False

    def refresh_session(self):
        logger = get_logger()
        logger.info("刷新浏览器 session（重置雪球限流计数）...")
        try:
            if self._page and not self._page.is_closed():
                self._page.goto("https://xueqiu.com", timeout=30000, wait_until="networkidle")
                time.sleep(random.uniform(2.0, 3.5))
                self._restore_session_cookies()
                self._page.goto("https://xueqiu.com", timeout=30000, wait_until="domcontentloaded")
                time.sleep(random.uniform(1.5, 2.5))
                self._warmed_paths.clear()
                self._persist_session()
                logger.info("浏览器 session 刷新完成")
        except Exception as e:
            logger.warning(f"刷新 session 异常: {e}")
            try:
                self.close()
                time.sleep(3)
                self._ensure_browser()
                logger.info("浏览器已完全重启")
            except Exception as e2:
                logger.error(f"浏览器重启也失败: {e2}")

    def close(self):
        if self._closed and not self._playwright:
            return

        try:
            self._persist_session()
        except Exception:
            pass

        self._close_runtime(stop_playwright=True)
        logger.info("浏览器引擎已关闭")

    def __del__(self):
        self.close()
