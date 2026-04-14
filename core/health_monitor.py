"""
健康监控模块 — Token 自检、爬取成功率、每日摘要

功能:
  - Token 有效性定时检查
  - 爬取成功率统计
  - 每日摘要生成
"""

import json
from datetime import datetime
from utils.logger import get_logger

logger = get_logger()


class HealthMonitor:
    """系统健康监控器。"""

    def __init__(self, db, cookie_manager, notifier, config):
        self.db = db
        self.cookie_manager = cookie_manager
        self.notifier = notifier
        self.config = config

    def check_token(self, client=None) -> bool:
        ok = self.cookie_manager.validate(client)
        if not ok:
            logger.warning("Token 健康检查失败")
            self.notifier.notify_cookie_expired()
        return ok

    def get_health_status(self) -> dict:
        stats = self.db.get_stats()
        logs = self.db.get_recent_logs(50)
        success = sum(1 for l in logs if l["status"] == "success")
        total = len(logs)
        stock_reports = self.db.get_stock_completeness_report()
        missing_comments = sum(r.get("missing_comments", 0) for r in stock_reports)
        orphan_comments = sum(r.get("orphan_comments", 0) for r in stock_reports)
        cross_post_replies = sum(r.get("cross_post_replies", 0) for r in stock_reports)
        return {
            "token_configured": self.cookie_manager.is_configured(),
            "success_rate": f"{success}/{total}" if total else "N/A",
            "stats": stats,
            "missing_comments": missing_comments,
            "orphan_comments": orphan_comments,
            "cross_post_replies": cross_post_replies,
            "last_scrape": logs[0] if logs else None,
        }

    def generate_daily_digest(self) -> str:
        """生成每日摘要文本。"""
        summary = self.db.get_daily_summary()
        health = self.get_health_status()

        lines = [
            f"📊 每日摘要 — {summary['date']}",
            f"{'='*40}",
            "",
            "📈 各股票数据:",
        ]
        for s in summary["stocks"]:
            lines.append(f"  {s['symbol']} {s['name']}: 新帖 {s['new_posts']}, 新评论 {s['new_comments']}")

        lines.extend([
            "",
            f"🏥 系统状态:",
            f"  Token: {'有效' if health['token_configured'] else '未配置'}",
            f"  最近成功率: {health['success_rate']}",
            f"  帖子总数: {health['stats']['posts']}",
            f"  评论总数: {health['stats']['comments']}",
            f"  评论缺口: {health['missing_comments']}",
            f"  孤儿评论: {health['orphan_comments']}",
            f"  跨帖回复: {health['cross_post_replies']}",
            f"  用户画像: {health['stats'].get('user_profiles', 0)}",
        ])

        # 热门话题
        try:
            topics = self.db.get_trending_topics(limit=5)
            if topics:
                lines.append("")
                lines.append("🔥 今日热门话题:")
                for t in topics:
                    lines.append(f"  #{t['rank']} {t['title']} (讨论{t.get('discuss_count',0)})")
        except Exception:
            pass

        return "\n".join(lines)
