#!/usr/bin/env python3
"""
热门话题爬虫安装脚本

用法:
  cd ~/Desktop/xueqiu-scraper && source venv/bin/activate
  python install_trending.py

创建:
  1. scrapers/trending_scraper.py — 热门话题爬虫
  2. 更新 scrapers/api_endpoints.py — 新增热门话题接口
  3. 更新 main.py — 新增 scrape-trending 命令
"""

import os
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{filepath}.bak_{ts}"
        shutil.copy2(filepath, bak)
        print(f"  备份: {os.path.basename(bak)}")


def write_file(rel_path, content, desc=""):
    full = os.path.join(PROJECT_ROOT, rel_path)
    d = os.path.dirname(full)
    if d:
        os.makedirs(d, exist_ok=True)
    if os.path.exists(full):
        backup(full)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ {rel_path}" + (f" ({desc})" if desc else ""))


# ================================================================
# scrapers/trending_scraper.py
# ================================================================

TRENDING_SCRAPER = r'''"""
雪球热门话题爬虫

已验证 API (2026-03-15):
  URL: https://xueqiu.com/hot_event/list.json?count=10
  响应: {"count": 10, "page": 1, "list": [...]}
  每条: {id, tag, content, status_count, pic, hot}

功能:
  - 抓取雪球首页 10 个热门话题
  - 自动从 content 中提取关联股票代码/名称
  - 按日去重存储到 trending_topics 表
  - 支持查询历史热度趋势
"""

import re
import json
import time
from datetime import datetime

from utils.logger import get_logger

logger = get_logger()

# 热门话题 API
TRENDING_URL = "https://xueqiu.com/hot_event/list.json"


def build_trending_params(count=10):
    """构建热门话题请求参数。"""
    return {
        "count": count,
        "_": int(time.time() * 1000),
    }


def extract_stock_mentions(text):
    """从话题内容中提取提到的股票。"""
    mentions = []

    # 匹配 $SH600519$ 或 $贵州茅台$ 格式
    dollar_matches = re.findall(r'\$([^$]+)\$', text)
    for m in dollar_matches:
        m = m.strip()
        if re.match(r'^[A-Z]{2}\d{6}$', m):
            mentions.append(m)
        elif len(m) >= 2:
            mentions.append(m)

    # 匹配中文股票名+动词（如"兆驰股份涨停"、"郑州煤电2连板"）
    cn_matches = re.findall(r'([\u4e00-\u9fa5]{2,6}(?:股份|集团|科技|电子|光电|化工|矿业|能源|医药))', text)
    mentions.extend(cn_matches)

    return list(set(mentions))


class TrendingScraper:
    """雪球热门话题爬虫。"""

    def __init__(self, client, db, config=None):
        self.client = client
        self.db = db
        self.config = config or {}

    def scrape_trending(self, count=10) -> dict:
        """
        抓取热门话题列表。

        Returns:
            {"new_topics": N, "topics": [...]}
        """
        logger.info("开始抓取雪球热门话题...")
        started = time.time()

        try:
            data = self.client.get(
                TRENDING_URL,
                params=build_trending_params(count),
                referer_path="/",
            )
        except Exception as e:
            logger.error(f"热门话题请求失败: {e}")
            return {"new_topics": 0, "topics": [], "error": str(e)}

        if not data:
            logger.warning("热门话题返回空数据")
            return {"new_topics": 0, "topics": []}

        raw_list = data.get("list", [])
        if not raw_list:
            logger.warning("热门话题列表为空")
            return {"new_topics": 0, "topics": []}

        new_count = 0
        topics = []

        for rank, item in enumerate(raw_list, 1):
            topic_id = str(item.get("id", ""))
            tag = item.get("tag", "").strip("#").strip()
            content = item.get("content", "")
            status_count = item.get("status_count", 0) or 0

            # 提取关联股票
            full_text = f"{tag} {content}"
            stock_mentions = extract_stock_mentions(full_text)

            topic = {
                "id": topic_id,
                "platform_id": "xueqiu",
                "title": tag,
                "url": f"https://xueqiu.com/hot/event/{topic_id}",
                "discuss_count": status_count,
                "followers_count": 0,
                "rank": rank,
                "associated_stocks": json.dumps(stock_mentions, ensure_ascii=False),
            }

            if self.db.save_trending_topic(topic):
                new_count += 1

            topics.append(topic)
            logger.info(f"  #{rank} {tag} (讨论{status_count}) 关联: {stock_mentions}")

        duration = time.time() - started

        # 记录日志
        self.db.log_scrape(
            task_type="trending_topics",
            target="xueqiu_hot_event",
            status="success",
            new_items_count=new_count,
            duration_seconds=round(duration, 1),
        )

        logger.info(f"热门话题抓取完成: {len(topics)} 条, 新增 {new_count} 条 ({duration:.1f}s)")
        return {"new_topics": new_count, "topics": topics}

    def get_trending_summary(self, days=7) -> list:
        """获取最近 N 天的热门话题趋势。"""
        from datetime import timedelta
        summaries = []
        for i in range(days):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            topics = self.db.get_trending_topics(date=d)
            if topics:
                summaries.append({
                    "date": d,
                    "topics": [{"rank": t["rank"], "title": t["title"],
                                "discuss_count": t["discuss_count"]}
                               for t in topics]
                })
        return summaries
'''


# ================================================================
# 更新 api_endpoints.py — 追加热门话题接口定义
# ================================================================

TRENDING_API_BLOCK = '''

# ============================================================
# 热门话题（已验证 2026-03-15）
# ============================================================

def build_trending_url() -> str:
    """热门话题接口 URL。"""
    return "https://xueqiu.com/hot_event/list.json"


def build_trending_params(count: int = 10) -> dict:
    """构建热门话题请求参数。"""
    import time
    return {
        "count": count,
        "_": int(time.time() * 1000),
    }


def parse_trending_response(data: dict) -> list:
    """
    解析热门话题响应。

    Args:
        data: API 返回的 JSON

    Returns:
        话题列表 [{id, tag, content, status_count, pic, hot}, ...]
    """
    if not data:
        return []
    return data.get("list", [])
'''


def patch_api_endpoints():
    """在 api_endpoints.py 末尾追加热门话题接口定义"""
    fp = os.path.join(PROJECT_ROOT, "scrapers", "api_endpoints.py")
    if not os.path.exists(fp):
        print("  ⚠ api_endpoints.py 不存在，跳过")
        return

    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    if "build_trending_url" in content:
        print("  ⏭ api_endpoints.py 已有热门话题接口定义")
        return

    backup(fp)
    content += TRENDING_API_BLOCK

    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    print("  ✓ api_endpoints.py 追加热门话题接口")


# ================================================================
# 更新 main.py — 添加 scrape-trending 命令
# ================================================================

TRENDING_CMD_FUNC = '''

def cmd_scrape_trending(args, config):
    """抓取雪球热门话题。"""
    components = init_components(config)
    from scrapers.trending_scraper import TrendingScraper
    scraper = TrendingScraper(components["client"], components["db"], config)

    result = scraper.scrape_trending()

    print()
    if result.get("topics"):
        print(f"🔥 雪球热门话题 Top {len(result['topics'])}:")
        print("-" * 50)
        for t in result["topics"]:
            stocks = t.get("associated_stocks", "[]")
            if isinstance(stocks, str):
                import json
                try:
                    stocks = json.loads(stocks)
                except Exception:
                    stocks = []
            stocks_str = ", ".join(stocks[:5]) if stocks else ""
            print(f"  #{t['rank']:2d} {t['title']}")
            print(f"      讨论 {t['discuss_count']} | 关联: {stocks_str or '无'}")
        print()
        print(f"新增入库: {result['new_topics']} 条")
    else:
        print("未获取到热门话题数据")
        if result.get("error"):
            print(f"错误: {result['error']}")

    # 显示趋势（如果有历史数据）
    days = getattr(args, 'days', 0) or 0
    if days > 0:
        summaries = scraper.get_trending_summary(days=days)
        if summaries:
            print(f"\\n📈 最近 {days} 天热门话题趋势:")
            for s in summaries:
                print(f"\\n  {s['date']}:")
                for t in s["topics"][:5]:
                    print(f"    #{t['rank']} {t['title']} (讨论{t['discuss_count']})")

    components["client"].close()
    components["db"].close()

'''


def patch_main_py():
    """在 main.py 中添加 scrape-trending 命令"""
    fp = os.path.join(PROJECT_ROOT, "main.py")
    if not os.path.exists(fp):
        print("  ⚠ main.py 不存在，跳过")
        return

    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    changed = False

    # 1. 添加 cmd_scrape_trending 函数
    if "cmd_scrape_trending" not in content:
        # 在 cmd_export 前插入
        content = content.replace(
            "def cmd_export(args, config):",
            TRENDING_CMD_FUNC + "\ndef cmd_export(args, config):"
        )
        print("  ✓ 新增 cmd_scrape_trending 函数")
        changed = True
    else:
        print("  ⏭ cmd_scrape_trending 已存在")

    # 2. 添加 argparse 子命令
    if '"scrape-trending"' not in content:
        # 在 backfill-comments 子命令后面插入
        insert_point = '    # status\n    subparsers.add_parser("status"'
        # 如果有 health 子命令就插在 health 前面
        if '    # health' in content:
            insert_point = '    # health'

        new_parser = '''    # scrape-trending
    p = subparsers.add_parser("scrape-trending", help="抓取雪球热门话题")
    p.add_argument("--days", type=int, default=0, help="同时显示最近N天趋势")

'''
        if insert_point in content:
            content = content.replace(insert_point, new_parser + "    " + insert_point.lstrip())
            print("  ✓ 新增 scrape-trending 子命令")
            changed = True
    else:
        print("  ⏭ scrape-trending 子命令已存在")

    # 3. 注册到 commands dict
    if '"scrape-trending": cmd_scrape_trending' not in content:
        content = content.replace(
            '"export": cmd_export,',
            '"scrape-trending": cmd_scrape_trending,\n        "export": cmd_export,'
        )
        print("  ✓ commands dict 注册 scrape-trending")
        changed = True
    else:
        print("  ⏭ commands dict 已注册")

    if changed:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)


# ================================================================
# 主流程
# ================================================================

def main():
    print("=" * 55)
    print("  热门话题爬虫安装")
    print("=" * 55)
    print()
    print("已确认 API:")
    print("  URL: https://xueqiu.com/hot_event/list.json?count=10")
    print("  字段: id, tag(标题), content(描述), status_count(讨论数)")
    print()

    print("[1/3] 创建 scrapers/trending_scraper.py...")
    write_file("scrapers/trending_scraper.py", TRENDING_SCRAPER, "热门话题爬虫")
    print()

    print("[2/3] 更新 api_endpoints.py...")
    patch_api_endpoints()
    print()

    print("[3/3] 更新 main.py...")
    patch_main_py()
    print()

    # 测试
    print("[验证] 测试导入...")
    try:
        import sys
        sys.path.insert(0, PROJECT_ROOT)
        from scrapers.trending_scraper import TrendingScraper, extract_stock_mentions
        # 测试股票提取
        test_text = "化工板块反复活跃，潞化科技2连板，金正大、兴化股份、恒天海龙涨停"
        mentions = extract_stock_mentions(test_text)
        print(f"  ✓ 导入成功")
        print(f"  ✓ 股票提取测试: '{test_text[:30]}...' → {mentions}")
    except Exception as e:
        print(f"  ⚠ 导入测试出错: {e}")

    print()
    print("=" * 55)
    print("  安装完成！")
    print("=" * 55)
    print()
    print("使用方法:")
    print()
    print("  python main.py scrape-trending            # 抓取当前热门话题")
    print("  python main.py scrape-trending --days 7   # 同时查看7天趋势")
    print()
    print("数据存储在 trending_topics 表中，可通过以下方式查看:")
    print("  python main.py export --format json       # JSON 快照含 trending_snapshot")
    print()


if __name__ == "__main__":
    main()
