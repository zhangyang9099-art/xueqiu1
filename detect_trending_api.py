#!/usr/bin/env python3
"""
自动抓包脚本 — 找到雪球热门话题 API

用法:
  cd ~/Desktop/xueqiu-scraper && source venv/bin/activate
  python detect_trending_api.py

原理: 用 Playwright 打开雪球首页，拦截所有网络请求，
      找到返回"热门话题"数据的 API，打印完整信息。
"""

import json
import yaml
import time
import sys
import os

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("请先安装 Playwright: pip install playwright && playwright install chromium")
        sys.exit(1)

    # 读取 token
    try:
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        token = cfg["cookie"]["xq_a_token"]
    except Exception as e:
        print(f"无法读取 config.yaml: {e}")
        sys.exit(1)

    print("正在启动浏览器抓包，请稍候...\n")

    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()

        # 拦截所有返回 JSON 的请求
        def on_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            if "json" not in ct and ".json" not in url:
                return
            try:
                body = response.json()
                text = json.dumps(body, ensure_ascii=False)
                # 寻找可能包含热门话题的关键词
                keywords = ["热门", "话题", "hot", "trending", "topic", "event",
                            "热榜", "热议", "热搜", "板块", "异动"]
                url_kw = ["hot", "trend", "topic", "event", "rank", "list"]

                has_content_kw = any(k in text[:3000] for k in keywords)
                has_url_kw = any(k in url.lower() for k in url_kw)

                if has_content_kw or has_url_kw:
                    captured.append({
                        "url": url,
                        "status": response.status,
                        "size": len(text),
                        "preview": text[:2000],
                        "keys": list(body.keys()) if isinstance(body, dict) else "非dict",
                    })
            except Exception:
                pass

        page.on("response", on_response)

        # 访问首页
        page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        time.sleep(2)

        # 注入 token
        context.add_cookies([{"name": "xq_a_token", "value": token,
                              "domain": ".xueqiu.com", "path": "/"}])
        page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
        time.sleep(3)

        # 滚动页面触发更多请求
        page.evaluate("window.scrollTo(0, 800)")
        time.sleep(2)
        page.evaluate("window.scrollTo(0, 1600)")
        time.sleep(2)

        # 尝试点击"热门话题"区域（如果有tab）
        try:
            for selector in ['text=热门话题', 'text=热议话题', '.hot-topic', '[data-type="topic"]']:
                el = page.query_selector(selector)
                if el:
                    el.click()
                    time.sleep(2)
                    break
        except Exception:
            pass

        # 再抓一轮页面上的热门话题文字内容（作为参照）
        print("=" * 60)
        print("  页面上看到的热门话题文字")
        print("=" * 60)
        try:
            # 尝试多种选择器提取热门话题文字
            for sel in ['.hot-topic', '.trending', '.home__hot',
                        '.stock-hot', '.hot-list', 'section']:
                els = page.query_selector_all(sel)
                if els:
                    for el in els[:3]:
                        txt = el.inner_text()[:500]
                        if any(k in txt for k in ["话题", "热门", "异动", "板块"]):
                            print(txt[:300])
                            print("---")
        except Exception:
            pass

        # 兜底: 直接提取页面所有包含"话题"关键词的文本块
        try:
            all_text = page.evaluate("""
                () => {
                    const elements = document.querySelectorAll('a, div, span, li');
                    const results = [];
                    for (const el of elements) {
                        const t = el.innerText?.trim();
                        if (t && t.length > 4 && t.length < 100 &&
                            (t.includes('话题') || t.includes('热门') || t.includes('板块') || t.includes('异动'))) {
                            results.push(t);
                        }
                    }
                    return [...new Set(results)].slice(0, 20);
                }
            """)
            if all_text:
                print("\n包含关键词的文本元素:")
                for t in all_text:
                    print(f"  · {t}")
        except Exception:
            pass

        browser.close()

    # 输出抓到的 API
    print(f"\n{'=' * 60}")
    print(f"  抓到 {len(captured)} 个可能相关的 API 请求")
    print(f"{'=' * 60}")

    if not captured:
        print("\n未抓到热门话题相关 API。可能原因:")
        print("  1. 热门话题是前端渲染的静态内容（非API请求）")
        print("  2. 需要滚动到特定位置才触发加载")
        print("  3. Token 可能已过期")
        print("\n建议: 运行 python main.py test-cookie 检查 Token")
    else:
        # 保存到文件
        os.makedirs("data", exist_ok=True)
        output_file = "data/trending_api_detection.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(captured, f, ensure_ascii=False, indent=2)
        print(f"\n完整数据已保存到: {output_file}")

        for i, api in enumerate(captured, 1):
            print(f"\n--- API #{i} ---")
            print(f"URL: {api['url'][:200]}")
            print(f"状态: {api['status']}")
            print(f"大小: {api['size']} 字符")
            if isinstance(api.get("keys"), list):
                print(f"顶层 Keys: {api['keys']}")
            print(f"数据预览:")
            print(f"{api['preview'][:800]}")
            print()

    print("\n请把以上输出全部复制发给我，我来确认 API 并写热门话题爬虫。")


if __name__ == "__main__":
    main()
