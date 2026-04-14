#!/usr/bin/env python3
"""
雪球 WAF 绕过测试脚本
使用 Playwright（真实浏览器内核）绕过阿里云 WAF 的 JS 挑战

用法：
  pip install playwright
  playwright install chromium
  python test_playwright.py
"""

import json
import yaml
import time
import sys


def test_with_playwright():
    """用 Playwright 打开雪球，通过 WAF 挑战，然后请求 API"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 请先安装 Playwright:")
        print("   pip install playwright")
        print("   playwright install chromium")
        sys.exit(1)

    # 读取 config 中的 token
    try:
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        token = cfg["cookie"]["xq_a_token"]
        print(f"✓ 读取到 xq_a_token: {token[:20]}...")
    except Exception as e:
        print(f"❌ 无法读取 config.yaml: {e}")
        sys.exit(1)

    with sync_playwright() as p:
        # 启动浏览器（headless=False 可以看到浏览器操作，调试时用）
        print("\n[1] 启动浏览器...")
        browser = p.chromium.launch(
            headless=True,  # 改为 False 可看到浏览器窗口，方便调试
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()

        # 第一步：访问主页，让浏览器自动执行 WAF JS 挑战
        print("[2] 访问雪球主页（等待 WAF JS 挑战完成）...")
        page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        time.sleep(2)  # 额外等待 WAF cookie 写入

        # 注入 xq_a_token
        print("[3] 注入 xq_a_token...")
        context.add_cookies([{
            "name": "xq_a_token",
            "value": token,
            "domain": ".xueqiu.com",
            "path": "/",
        }])

        # 打印当前所有 cookie
        cookies = context.cookies()
        print(f"[4] 当前 Cookie 数量: {len(cookies)}")
        for c in cookies:
            if "xueqiu" in c.get("domain", ""):
                print(f"    {c['name']}: {c['value'][:40]}...")

        # 第二步：访问个股讨论页，触发讨论区相关的 API 请求
        print("\n[5] 访问贵州茅台讨论页...")
        
        # 用 route 拦截所有 API 请求，记录下来
        api_calls = []
        
        def handle_response(response):
            url = response.url
            # 只关注可能是帖子/评论的 API
            keywords = ["timeline", "status", "comment", "post", "stock", "query"]
            if any(kw in url.lower() for kw in keywords) and "json" in url.lower():
                try:
                    body = response.json()
                    api_calls.append({
                        "url": url.split("?")[0],
                        "full_url": url,
                        "status": response.status,
                        "keys": list(body.keys()) if isinstance(body, dict) else "NOT_DICT",
                        "data_preview": json.dumps(body, ensure_ascii=False)[:800],
                    })
                except:
                    api_calls.append({
                        "url": url.split("?")[0],
                        "full_url": url,
                        "status": response.status,
                        "keys": "PARSE_ERROR",
                    })

        page.on("response", handle_response)

        page.goto("https://xueqiu.com/S/SH600519", wait_until="networkidle", timeout=30000)
        time.sleep(3)

        # 滚动页面触发更多请求
        print("[6] 滚动页面加载更多帖子...")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)

        # 展示抓到的 API 请求
        print(f"\n{'='*60}")
        print(f"捕获到 {len(api_calls)} 个 API 请求:")
        print(f"{'='*60}")
        
        for i, call in enumerate(api_calls):
            print(f"\n--- API #{i+1} ---")
            print(f"URL: {call['url']}")
            print(f"状态: {call['status']}")
            print(f"Keys: {call['keys']}")
            if "data_preview" in call:
                print(f"数据预览: {call['data_preview'][:500]}")

        # 第三步：尝试直接用浏览器的 fetch 去请求 API
        print(f"\n{'='*60}")
        print("直接用浏览器 fetch 请求 API:")
        print(f"{'='*60}")

        test_urls = [
            ("statuses/stock_timeline", 
             "https://xueqiu.com/statuses/stock_timeline.json?symbol_id=SH600519&count=5&page=1"),
            ("query/v1/search/status",
             "https://xueqiu.com/query/v1/symbol/search/status.json?symbol=SH600519&count=5&sort=time&page=1&source=all"),
            ("v5/stock/timeline",
             "https://stock.xueqiu.com/v5/stock/timeline.json?symbol_id=SH600519&count=5&page=1"),
        ]

        for name, url in test_urls:
            print(f"\n[{name}]")
            try:
                result = page.evaluate(f"""
                    async () => {{
                        try {{
                            const resp = await fetch("{url}", {{
                                credentials: 'include',
                                headers: {{
                                    'Accept': 'application/json',
                                    'X-Requested-With': 'XMLHttpRequest',
                                }}
                            }});
                            const text = await resp.text();
                            return {{
                                status: resp.status,
                                contentType: resp.headers.get('content-type'),
                                body: text.substring(0, 1000),
                            }};
                        }} catch(e) {{
                            return {{ error: e.message }};
                        }}
                    }}
                """)
                print(f"  HTTP {result.get('status')}")
                print(f"  Content-Type: {result.get('contentType')}")
                body = result.get("body", "")
                if body.startswith("{"):
                    try:
                        d = json.loads(body if len(body) < 1000 else body)
                        print(f"  Keys: {list(d.keys()) if isinstance(d, dict) else 'NOT DICT'}")
                        print(f"  预览: {json.dumps(d, ensure_ascii=False)[:500]}")
                    except:
                        print(f"  body(截断): {body[:300]}")
                else:
                    print(f"  非JSON: {body[:200]}")
            except Exception as e:
                print(f"  异常: {e}")

        # 第四步：提取可复用的 cookie
        print(f"\n{'='*60}")
        print("可复用的完整 Cookie 字符串:")
        print(f"{'='*60}")
        
        final_cookies = context.cookies()
        xueqiu_cookies = [c for c in final_cookies if "xueqiu" in c.get("domain", "")]
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in xueqiu_cookies])
        print(cookie_str[:500])
        
        # 保存 cookie 到文件
        with open("data/browser_cookies.json", "w") as f:
            json.dump(xueqiu_cookies, f, ensure_ascii=False, indent=2)
        print("\n✓ Cookie 已保存到 data/browser_cookies.json")

        browser.close()
        print("\n✓ 测试完成")


if __name__ == "__main__":
    test_with_playwright()
