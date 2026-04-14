"""
自动登录模块：打开可视浏览器让用户登录雪球，自动保存完整 Cookie Jar。

流程:
  1. 启动 Playwright Chromium（非无头，用户可见）
  2. 导航到 xueqiu.com
  3. 提示用户在浏览器中登录（扫码或账号密码）
  4. 轮询检测 xq_a_token cookie
  5. 保存完整 Cookie Jar 到 data/browser_cookies.json
  6. 更新 config.yaml 中的 xq_a_token / cookie_file
"""

import os
import time

import yaml

from core.cookie_manager import CookieManager


def auto_login(config_path="config.yaml", timeout=180):
    """
    打开浏览器让用户登录雪球，获取 xq_a_token 与完整 Cookie Jar。

    Args:
        config_path: config.yaml 的路径
        timeout: 等待登录的超时秒数

    Returns:
        获取到的 token 字符串，失败返回 None
    """
    from playwright.sync_api import sync_playwright

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    cookie_manager = CookieManager(config, config_path=config_path)

    print()
    print("正在启动浏览器...")
    print("=" * 50)
    print("  请在弹出的浏览器窗口中登录雪球账号")
    print("  支持：扫码登录 / 手机验证码 / 账号密码")
    print("  登录成功后系统会自动检测并保存完整 Cookie Jar")
    print("=" * 50)
    print()

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    page = context.new_page()

    token = None
    try:
        page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        time.sleep(2)

        try:
            login_btn = (
                page.query_selector('a[href*="login"]')
                or page.query_selector('button:has-text("登录")')
                or page.query_selector('.nav__login__btn')
                or page.query_selector('a:has-text("登录")')
            )
            if login_btn:
                login_btn.click()
                time.sleep(1)
        except Exception:
            pass

        print("等待登录中", end="", flush=True)
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                cookies = context.cookies("https://xueqiu.com")
                cookie_manager.persist_browser_cookies(cookies)
                token = cookie_manager.get_token()
                if token:
                    break
            except Exception:
                pass

            print(".", end="", flush=True)
            time.sleep(2)

        print()

        if not token:
            print("✗ 超时未检测到登录，请重试")
            return None

        cookie_manager.capture_from_context(context)
        cookie_manager.save_token_to_config(token)

        diagnostics = cookie_manager.get_cookie_diagnostics()
        print(f"✓ 获取到 Token: {token[:12]}...{token[-8:]}")
        print(f"✓ 已保存完整 Cookie Jar: {cookie_manager.get_cookie_file()}")
        print(f"✓ 当前 Cookie 数量: {diagnostics['cookie_count']}")
        if diagnostics["missing_required"]:
            print("⚠ 仍缺少关键 Cookie: " + ", ".join(diagnostics["missing_required"]))
        else:
            print("✓ 关键 Cookie 已齐全")

    finally:
        try:
            cookie_manager.capture_from_context(context)
        except Exception:
            pass
        try:
            page.close()
            context.close()
            browser.close()
            pw.stop()
        except Exception:
            pass

    return token
