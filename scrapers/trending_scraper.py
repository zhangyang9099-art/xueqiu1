"""
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
