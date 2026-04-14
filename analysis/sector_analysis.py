"""
板块热度分析

计算每个板块的热度指数，检测板块间的热度迁移。
板块分类来自 analysis/rules/sector_mapping.yaml。
"""

import sqlite3
import os
import yaml
from datetime import datetime, timedelta
from typing import Dict, List

from analysis.baseline import get_baseline

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_sector_mapping() -> Dict[str, List[str]]:
    """加载板块分类映射"""
    path = os.path.join(PROJECT_ROOT, "analysis", "rules", "sector_mapping.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sectors", {})


def compute_sector_heat(conn: sqlite3.Connection,
                        days: int = 7) -> Dict[str, dict]:
    """
    计算每个板块的热度指数。

    热度指数 = 该板块所有票的(最近N天日均评论 / 基准日均评论)的加权平均
    权重 = 该票在板块内的评论量占比
    """
    mapping = load_sector_mapping()
    if not mapping:
        return {}

    # 注意: comments.created_at 存的是 "秒级时间戳 × 1000"，不是真正的毫秒
    # 所以 cutoff 也要用 秒×1000 格式
    cutoff = (int(datetime.now().timestamp()) - days * 86400) * 1000
    results = {}

    for sector, symbols in mapping.items():
        stocks = []
        total_weight = 0
        weighted_ratio_sum = 0
        total_comments = 0
        sector_bull = 0
        sector_bear = 0

        for symbol in symbols:
            baseline = get_baseline(conn, symbol)
            if not baseline:
                continue

            # 最近N天评论数（用毫秒时间戳）
            result = conn.execute("""
                SELECT COUNT(*) AS cnt FROM comments c
                JOIN comment_memberships m ON m.comment_id = c.id
                JOIN posts p ON p.id = m.post_id
                WHERE p.symbol = ? AND c.created_at >= ?
            """, (symbol, cutoff)).fetchone()

            cnt = result[0] or 0
            recent_daily = cnt / max(days, 1)
            avg = baseline.get("avg_daily_comments", 0)
            ratio = recent_daily / avg if avg > 0 else 1.0

            # 情绪统计（从标注结果）
            sent = conn.execute("""
                SELECT SUM(bullish_count) AS bull, SUM(bearish_count) AS bear
                FROM llm_batch_summaries
                WHERE symbol = ? AND summary_date >= date('now', ?)
            """, (symbol, f'-{days} days')).fetchone()

            sector_bull += sent[0] or 0
            sector_bear += sent[1] or 0

            # 股票名
            name_row = conn.execute(
                "SELECT name FROM watched_stocks WHERE symbol = ?", (symbol,)
            ).fetchone()
            stock_name = name_row[0] if name_row else symbol

            weight = cnt if cnt > 0 else 1
            stocks.append({
                "symbol": symbol,
                "name": stock_name,
                "ratio": round(ratio, 1),
                "recent_comments": cnt
            })

            weighted_ratio_sum += ratio * weight
            total_weight += weight
            total_comments += cnt

        if total_weight == 0:
            continue

        heat_index = weighted_ratio_sum / total_weight
        dominant = "bullish" if sector_bull > sector_bear else (
            "bearish" if sector_bear > sector_bull else "neutral"
        )

        results[sector] = {
            "heat_index": round(heat_index, 2),
            "stocks": sorted(stocks, key=lambda s: s["ratio"], reverse=True),
            "total_recent_comments": total_comments,
            "dominant_sentiment": dominant,
        }

    return results


def detect_sector_migration(conn: sqlite3.Connection) -> List[dict]:
    """
    检测板块间的热度迁移。

    比较本周(7天) vs 上周(14天)：
    - 热度翻倍以上 → 急剧升温
    - 下降50%以上 → 急剧降温
    - 同时有升有降 → 疑似轮动
    """
    current = compute_sector_heat(conn, days=7)
    previous = compute_sector_heat(conn, days=14)

    alerts = []
    heating = []
    cooling = []

    for sector in current:
        curr_heat = current[sector]["heat_index"]
        prev_heat = previous.get(sector, {}).get("heat_index", 1.0)

        if prev_heat > 0:
            change = (curr_heat - prev_heat) / prev_heat
        else:
            change = 0

        if change > 1.0:
            heating.append((sector, curr_heat, change))
        elif change < -0.5:
            cooling.append((sector, curr_heat, change))

    for sector, heat, change in heating:
        alerts.append({
            "alert_type": "sector_heating",
            "symbol": f"板块:{sector}",
            "severity": "medium",
            "title": f"板块「{sector}」讨论度急剧升温（+{change:.0%}）",
            "detail": (
                f"本周热度指数{heat:.1f}，较上周大幅上升。\n"
                f"热门个股: {', '.join(s['name'] for s in current[sector]['stocks'][:3])}"
            ),
            "data": {"sector": sector, "heat": heat, "change": round(change, 2)},
            "suggestion": f"板块「{sector}」正在吸引大量关注，可能有板块性催化剂。",
        })

    if heating and cooling:
        hot_names = [s for s, _, _ in heating]
        cold_names = [s for s, _, _ in cooling]
        alerts.append({
            "alert_type": "sector_rotation",
            "symbol": "全市场",
            "severity": "medium",
            "title": f"疑似板块轮动: {'/'.join(cold_names)} → {'/'.join(hot_names)}",
            "detail": (
                f"升温板块: {', '.join(f'{s}(+{c:.0%})' for s, _, c in heating)}\n"
                f"降温板块: {', '.join(f'{s}({c:.0%})' for s, _, c in cooling)}"
            ),
            "data": {"heating": hot_names, "cooling": cold_names},
            "suggestion": "资金关注可能正在从降温板块向升温板块迁移。",
        })

    return alerts
