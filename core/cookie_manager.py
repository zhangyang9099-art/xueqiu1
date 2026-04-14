"""
Cookie 管理模块：管理 xq_a_token 与完整浏览器 Cookie Jar。

雪球部分接口对登录态和设备侧 Cookie 较敏感，仅依赖 xq_a_token 不够稳。
本模块统一管理:
  - config.yaml 中的 xq_a_token
  - data/browser_cookies.json 中的完整 Cookie Jar
  - Playwright context 采集到的最新 Cookie 持久化
"""

import json
import os
from typing import Iterable

import yaml

from utils.logger import get_logger
from utils.notifier import Notifier

logger = get_logger()


class CookieExpiredError(Exception):
    """Cookie 已失效。"""


class CookieManager:
    """Cookie 生命周期管理器。"""

    DEFAULT_COOKIE_FILE = "data/browser_cookies.json"
    REQUIRED_COOKIE_NAMES = {
        "xq_a_token",
        "u",
        "device_id",
        "xqat",
        "xq_r_token",
    }

    def __init__(self, config: dict, config_path: str = "config.yaml"):
        self._config = config
        self._config_path = config_path
        cookie_cfg = config.get("cookie", {}) or {}
        self._cookie_file = cookie_cfg.get("cookie_file", self.DEFAULT_COOKIE_FILE)
        self._xq_a_token = cookie_cfg.get("xq_a_token", "")
        self._browser_cookies = []
        self.notifier = Notifier(config)

        self._load_cookie_file()
        self._ensure_token_cookie()

        if not self.is_configured():
            logger.warning("xq_a_token 未配置！请先在 config.yaml 中填入有效的 Cookie。")

    # ──────── 基础读取 ────────

    def _is_xueqiu_cookie(self, cookie: dict) -> bool:
        domain = (cookie.get("domain") or "").lstrip(".").lower()
        return domain.endswith("xueqiu.com")

    def _normalize_cookie(self, cookie: dict) -> dict | None:
        if not isinstance(cookie, dict):
            return None

        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        domain = str(cookie.get("domain", "")).strip()
        path = str(cookie.get("path", "/") or "/")

        if not name or not domain or not self._is_xueqiu_cookie(cookie):
            return None

        normalized = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
        }

        expires = cookie.get("expires")
        if isinstance(expires, (int, float)):
            normalized["expires"] = expires

        for field in ("httpOnly", "secure", "sameSite"):
            if field in cookie:
                normalized[field] = cookie[field]

        return normalized

    def _dedupe_cookies(self, cookies: Iterable[dict]) -> list[dict]:
        deduped = {}
        for raw_cookie in cookies:
            cookie = self._normalize_cookie(raw_cookie)
            if not cookie:
                continue
            key = (cookie["domain"], cookie["path"], cookie["name"])
            deduped[key] = cookie
        return list(deduped.values())

    def _load_cookie_file(self):
        self._browser_cookies = []
        if not self._cookie_file or not os.path.exists(self._cookie_file):
            return

        try:
            with open(self._cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            self._browser_cookies = self._dedupe_cookies(cookies)

            if not self._xq_a_token:
                for cookie in self._browser_cookies:
                    if cookie["name"] == "xq_a_token" and cookie["value"]:
                        self._xq_a_token = cookie["value"]
                        break
        except Exception as e:
            logger.warning(f"加载浏览器 Cookie 文件失败: {e}")
            self._browser_cookies = []

    def _ensure_token_cookie(self):
        if not self._xq_a_token:
            return

        updated = []
        found = False
        for cookie in self._browser_cookies:
            if cookie["name"] == "xq_a_token":
                cookie = dict(cookie)
                cookie["value"] = self._xq_a_token
                found = True
            updated.append(cookie)

        if not found:
            updated.append(
                {
                    "name": "xq_a_token",
                    "value": self._xq_a_token,
                    "domain": ".xueqiu.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "Lax",
                }
            )

        self._browser_cookies = self._dedupe_cookies(updated)

    # ──────── 对外接口 ────────

    def get_token(self) -> str:
        return self._xq_a_token

    def get_cookie_file(self) -> str:
        return self._cookie_file

    def get_browser_cookies(self) -> list[dict]:
        self._ensure_token_cookie()
        return [dict(c) for c in self._browser_cookies]

    def get_cookies(self) -> dict:
        cookies = {}
        for cookie in self.get_browser_cookies():
            cookies[cookie["name"]] = cookie["value"]
        return cookies

    def get_cookie_header(self) -> str:
        parts = [f"{name}={value}" for name, value in self.get_cookies().items() if value]
        return "; ".join(parts)

    def get_cookie_diagnostics(self) -> dict:
        cookies = self.get_cookies()
        available = set(cookies.keys())
        missing = sorted(self.REQUIRED_COOKIE_NAMES - available)
        return {
            "cookie_file": self._cookie_file,
            "cookie_count": len(cookies),
            "has_full_cookie_jar": len(cookies) > 1,
            "missing_required": missing,
            "available_names": sorted(available),
        }

    def is_configured(self) -> bool:
        return bool(
            self._xq_a_token
            and self._xq_a_token != "YOUR_XQ_A_TOKEN_HERE"
        )

    def has_full_cookie_jar(self) -> bool:
        diagnostics = self.get_cookie_diagnostics()
        return diagnostics["has_full_cookie_jar"] and not diagnostics["missing_required"]

    # ──────── 持久化 ────────

    def persist_browser_cookies(self, cookies: Iterable[dict]) -> bool:
        normalized = self._dedupe_cookies(cookies)
        if not normalized:
            return False

        os.makedirs(os.path.dirname(self._cookie_file) or ".", exist_ok=True)
        with open(self._cookie_file, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)

        self._browser_cookies = normalized
        for cookie in normalized:
            if cookie["name"] == "xq_a_token" and cookie["value"]:
                self._xq_a_token = cookie["value"]
                break
        return True

    def capture_from_context(self, context) -> bool:
        if context is None:
            return False
        try:
            cookies = context.cookies(["https://xueqiu.com", "https://open.xueqiu.com"])
            if not cookies:
                cookies = context.cookies()
            return self.persist_browser_cookies(cookies)
        except Exception as e:
            logger.warning(f"持久化浏览器 Cookie 失败: {e}")
            return False

    def save_token_to_config(self, token: str) -> bool:
        if not token:
            return False

        config_path = self._config_path
        if not os.path.isabs(config_path):
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                config_path,
            )
            if not os.path.exists(config_path):
                config_path = self._config_path

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            config_data = {}

        cookie_cfg = config_data.setdefault("cookie", {})
        cookie_cfg["xq_a_token"] = token
        cookie_cfg["cookie_file"] = self._cookie_file

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config_data, f, allow_unicode=True, sort_keys=False)

        self._xq_a_token = token
        self._ensure_token_cookie()
        return True

    # ──────── 校验与告警 ────────

    def validate(self, client=None) -> bool:
        if not self.is_configured():
            logger.error("Cookie 未配置")
            return False

        if client is None:
            logger.info("Cookie 已配置，跳过在线验证（无 client 实例）")
            return True

        probes = [
            (
                "hot_list",
                "https://xueqiu.com/statuses/hot/listV2.json",
                {"a": "1", "count": "1"},
                None,
            ),
            (
                "stock_timeline",
                "https://xueqiu.com/query/v1/symbol/search/status.json",
                {"symbol": "SH600519", "count": 1, "sort": "time", "page": 1, "source": "all"},
                "/S/SH600519",
            ),
        ]

        for probe_name, url, params, referer in probes:
            try:
                resp = client.get(url, params=params, referer_path=referer)
                if not resp:
                    logger.warning(f"Cookie 验证失败: {probe_name} 返回空数据")
                    return False
            except Exception as e:
                logger.error(f"Cookie 验证异常 ({probe_name}): {e}")
                return False

        diagnostics = self.get_cookie_diagnostics()
        if diagnostics["missing_required"]:
            logger.warning(
                "Cookie 已可用，但浏览器 Cookie Jar 不完整: 缺少 %s",
                ",".join(diagnostics["missing_required"]),
            )

        logger.info("Cookie 验证通过 ✓")
        return True

    def on_expired(self):
        logger.error("Cookie 已失效！请更新 config.yaml 或重新运行 python main.py login。")
        self.notifier.notify_cookie_expired()

    def reload_from_config(self) -> bool:
        try:
            if not os.path.exists(self._config_path):
                logger.error(f"配置文件不存在: {self._config_path}")
                return False

            with open(self._config_path, "r", encoding="utf-8") as f:
                new_config = yaml.safe_load(f) or {}

            cookie_cfg = new_config.get("cookie", {}) or {}
            new_token = cookie_cfg.get("xq_a_token", "")
            new_cookie_file = cookie_cfg.get("cookie_file", self._cookie_file)

            changed = False
            if new_cookie_file != self._cookie_file:
                self._cookie_file = new_cookie_file
                changed = True

            if new_token and new_token != self._xq_a_token:
                self._xq_a_token = new_token
                changed = True

            self._load_cookie_file()
            self._ensure_token_cookie()

            if changed:
                logger.info("Cookie 已从配置文件重新加载")
            else:
                logger.info("配置文件中的 Cookie 未变化")
            return changed

        except Exception as e:
            logger.error(f"重新加载配置失败: {e}")
            return False
