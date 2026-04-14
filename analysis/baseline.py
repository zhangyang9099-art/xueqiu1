#!/usr/bin/env python3
"""
基准线系统 — 为异常检测提供比较基础

核心概念：
  每只股票维护一组基准指标（日均发帖量、日均评论量、平均热度、情绪分布等）。
  异常检测引擎将当日数据与基准线对比，发现偏离。

基准线数据来源：
  1. 历史统计（从 posts/comments 表计算过去 N 天的均值）
  2. LLM 标注结果（从 llm_batch_summaries 表获取情绪分布）

更新策略：
  - 每次运行 daily-scan 时自动更新
  - 只在有足够历史数据时计算（至少7天）

用法：
  内部模块，由 daily-scan 调用，不直接暴露命令
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def compute_baselines(conn: sqlite3.Connection, symbol: str,
                      period_days: int = 30) -> Optional[dict]:
    """计算单只股票的基准线。

    Returns:
        基准线 dict，数据不足时返回 None
    """
    cutoff = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%d")

    # 基础统计：帖子和评论量
    row = conn.execute("""
        SELECT
            COUNT(DISTINCT p.id) AS total_posts,
            COUNT(DISTINCT CASE WHEN p.created_at_str >= ? THEN p.id END) AS recent_posts,
            COUNT(DISTINCT c.id) AS total_comments,
            COUNT(DISTINCT CASE WHEN c.created_at_str >= ? THEN c.id END) AS recent_comments,
            COUNT(DISTINCT CASE WHEN p.created_at_str >= ? THEN p.user_id END) AS recent_active_users,
            MIN(substr(p.created_at_str, 1, 10)) AS earliest_date,
            MAX(substr(p.created_at_str, 1, 10)) AS latest_date
        FROM posts p
        LEFT JOIN comment_memberships m ON m.post_id = p.id
        LEFT JOIN comments c ON c.id = m.comment_id
        WHERE p.symbol = ?
    """, (cutoff, cutoff, cutoff, symbol)).fetchone()

    r = dict(row)
    total_posts = r["total_posts"] or 0
    total_comments = r["total_comments"] or 0

    if total_posts < 5:
        return None  # 数据太少，不计算基准线

    # 计算时间跨度（天数）
    earliest = r["earliest_date"]
    latest = r["latest_date"]
    if not earliest or not latest:
        return None

    try:
        span_days = max(1, (datetime.strptime(latest, "%Y-%m-%d") -
                            datetime.strptime(earliest, "%Y-%m-%d")).days)
    except ValueError:
        span_days = max(1, period_days)

    # 平均热度分
    heat_row = conn.execute("""
        SELECT
            AVG(like_count + COALESCE(comments_scraped, 0) * 2
                + COALESCE(retweet_count, 0) * 3 + COALESCE(fav_count, 0) * 1.5)
            AS avg_heat
        FROM posts
        WHERE symbol = ? AND created_at_str >= ?
    """, (symbol, cutoff)).fetchone()

    avg_heat = dict(heat_row)["avg_heat"] or 0

    # LLM 标注结果的情绪分布
    sentiment_row = conn.execute("""
        SELECT
            AVG(CASE WHEN sentiment = 'bullish' THEN 1.0 ELSE 0.0 END) AS pct_bullish,
            AVG(CASE WHEN sentiment = 'bearish' THEN 1.0 ELSE 0.0 END) AS pct_bearish,
            AVG(CASE WHEN sentiment = 'neutral' THEN 1.0 ELSE 0.0 END) AS pct_neutral,
            AVG(CASE WHEN sentiment = 'mixed' THEN 1.0 ELSE 0.0 END) AS pct_mixed,
            AVG(sentiment_strength) AS avg_sentiment_strength,
            COUNT(*) AS annotation_count
        FROM llm_annotations
        WHERE symbol = ? AND annotated_at >= ?
    """, (symbol, cutoff)).fetchone()

    sr = dict(sentiment_row)

    # 如果有标注数据，用标注的情绪；否则中性
    if (sr["annotation_count"] or 0) > 10:
        pct_bullish = round((sr["pct_bullish"] or 0) * 100, 1)
        pct_bearish = round((sr["pct_bearish"] or 0) * 100, 1)
        avg_sentiment = round((sr["pct_bullish"] or 0) - (sr["pct_bearish"] or 0), 3)
    else:
        pct_bullish = 0
        pct_bearish = 0
        avg_sentiment = 0

    baseline = {
        "symbol": symbol,
        "period_days": period_days,
        "avg_daily_posts": round(total_posts / span_days, 1),
        "avg_daily_comments": round(total_comments / span_days, 1),
        "avg_heat_score": round(avg_heat, 1),
        "avg_sentiment": avg_sentiment,
        "pct_bullish": pct_bullish,
        "pct_bearish": pct_bearish,
        "typical_active_users": r["recent_active_users"] or 0,
        "total_posts_in_period": r["recent_posts"] or 0,
        "total_comments_in_period": r["recent_comments"] or 0,
        "data_quality": "high" if (sr["annotation_count"] or 0) > 50 else "medium" if (sr["annotation_count"] or 0) > 10 else "low",
    }

    return baseline


def save_baseline(conn: sqlite3.Connection, baseline: dict):
    """保存基准线到 stock_baselines 表"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        INSERT OR REPLACE INTO stock_baselines
        (symbol, avg_daily_posts, avg_daily_comments, avg_heat_score,
         avg_sentiment, pct_bullish, pct_bearish,
         typical_active_users, baseline_json, computed_at, period_days)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        baseline["symbol"],
        baseline["avg_daily_posts"],
        baseline["avg_daily_comments"],
        baseline["avg_heat_score"],
        baseline["avg_sentiment"],
        baseline["pct_bullish"],
        baseline["pct_bearish"],
        baseline["typical_active_users"],
        json.dumps(baseline, ensure_ascii=False),
        now_str,
        baseline["period_days"],
    ))
    conn.commit()


def get_baseline(conn: sqlite3.Connection, symbol: str) -> Optional[dict]:
    """读取已保存的基准线"""
    row = conn.execute(
        "SELECT baseline_json FROM stock_baselines WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row and row["baseline_json"]:
        try:
            return json.loads(row["baseline_json"])
        except json.JSONDecodeError:
            return None
    return None


def update_all_baselines(conn: sqlite3.Connection, period_days: int = 30) -> dict:
    """更新所有活跃股票的基准线。

    Returns:
        {"updated": 3, "skipped": 2, "errors": []}
    """
    # 获取有帖子的所有 symbol
    symbols = [dict(r)["symbol"] for r in conn.execute("""
        SELECT DISTINCT symbol FROM posts
        WHERE created_at_str >= date('now', '-60 days')
    """).fetchall()]

    result = {"updated": 0, "skipped": 0, "errors": []}

    for symbol in symbols:
        try:
            baseline = compute_baselines(conn, symbol, period_days)
            if baseline:
                save_baseline(conn, baseline)
                result["updated"] += 1
            else:
                result["skipped"] += 1
        except Exception as e:
            result["errors"].append(f"{symbol}: {e}")

    return result


def get_today_stats(conn: sqlite3.Connection, symbol: str) -> dict:
    """获取某只股票今日的统计数据（用于与基准线对比）。

    Returns:
        {"posts": 5, "comments": 23, "heat_score": 45.2, "active_users": 12, ...}
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # 今日帖子和评论
    row = conn.execute("""
        SELECT
            COUNT(DISTINCT p.id) AS posts,
            COUNT(DISTINCT c.id) AS comments,
            COUNT(DISTINCT c.user_id) AS active_users,
            SUM(p.like_count + COALESCE(p.comments_scraped, 0) * 2
                + COALESCE(p.retweet_count, 0) * 3 + COALESCE(p.fav_count, 0) * 1.5)
            AS total_heat
        FROM posts p
        LEFT JOIN comment_memberships m ON m.post_id = p.id
        LEFT JOIN comments c ON c.id = m.comment_id
        WHERE p.symbol = ? AND p.created_at_str = ?
    """, (symbol, today)).fetchone()

    r = dict(row)

    # 今日 LLM 标注情绪
    sent_row = conn.execute("""
        SELECT
            SUM(CASE WHEN sentiment = 'bullish' THEN 1 ELSE 0 END) AS bullish,
            SUM(CASE WHEN sentiment = 'bearish' THEN 1 ELSE 0 END) AS bearish,
            SUM(CASE WHEN sentiment = 'neutral' THEN 1 ELSE 0 END) AS neutral,
            SUM(CASE WHEN sentiment = 'mixed' THEN 1 ELSE 0 END) AS mixed,
            AVG(sentiment_strength) AS avg_strength,
            COUNT(*) AS annotated
        FROM llm_annotations
        WHERE symbol = ? AND annotated_at >= ?
    """, (symbol, today)).fetchone()

    sr = dict(sent_row)
    total_annotated = sr["annotated"] or 0

    if total_annotated > 0:
        pct_bullish = round(sr["bullish"] / total_annotated * 100, 1)
        pct_bearish = round(sr["bearish"] / total_annotated * 100, 1)
    else:
        pct_bullish = pct_bearish = 0

    return {
        "date": today,
        "symbol": symbol,
        "posts": r["posts"] or 0,
        "comments": r["comments"] or 0,
        "active_users": r["active_users"] or 0,
        "heat_score": round(r["total_heat"] or 0, 1),
        "bullish_count": sr["bullish"] or 0,
        "bearish_count": sr["bearish"] or 0,
        "neutral_count": sr["neutral"] or 0,
        "mixed_count": sr["mixed"] or 0,
        "pct_bullish": pct_bullish,
        "pct_bearish": pct_bearish,
        "avg_strength": round(sr["avg_strength"] or 0, 1),
        "annotated_count": total_annotated,
    }
