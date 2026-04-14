"""
规律推票引擎

根据 rules/ 下的 YAML 规则，对每只股票检查是否符合推票条件。
所有情绪判断来自 LLM 标注结果（llm_batch_summaries），不使用关键词匹配。
"""

import sqlite3
import os
import yaml
import math
from datetime import datetime, timedelta
from typing import List

from analysis.baseline import get_baseline

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_rules() -> List[dict]:
    """加载所有启用的规则"""
    rules_dir = os.path.join(PROJECT_ROOT, "analysis", "rules")
    all_rules = []

    for filename in ["default_rules.yaml", "user_rules.yaml"]:
        filepath = os.path.join(rules_dir, filename)
        if not os.path.exists(filepath):
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data and "rules" in data:
            for rule_id, rule in data["rules"].items():
                if rule.get("enabled", False):
                    rule["rule_id"] = rule_id
                    all_rules.append(rule)
    return all_rules


def scan_recommendations(conn: sqlite3.Connection,
                         symbols: List[str]) -> List[dict]:
    """对所有股票执行所有规则检查"""
    rules = load_rules()
    recommendations = []

    for symbol in symbols:
        baseline = get_baseline(conn, symbol)

        for rule in rules:
            try:
                rec = _check_rule(conn, symbol, rule, baseline)
                if rec:
                    recommendations.append(rec)
            except Exception:
                pass

    return recommendations


def _check_rule(conn, symbol, rule, baseline):
    """检查某只股票是否符合某条规则。返回推荐 dict 或 None。"""
    conditions = rule.get("conditions", {})

    # 评论量条件
    if baseline and baseline.get("status") == "ok":
        if "comment_ratio_below" in conditions:
            ratio = _get_comment_ratio(conn, symbol, baseline, days=7)
            if ratio is None or ratio >= conditions["comment_ratio_below"]:
                return None
        if "comment_deviation_above" in conditions:
            dev = _get_comment_deviation(conn, symbol, baseline, days=1)
            if dev is None or dev < conditions["comment_deviation_above"]:
                return None

    # 情绪条件（从 LLM 标注读取）
    if "bullish_ratio_above" in conditions:
        ratio = _get_llm_bullish_ratio(conn, symbol, days=3)
        if ratio is None or ratio < conditions["bullish_ratio_above"]:
            return None

    # 情绪反转条件
    if "previous_bearish_ratio_above" in conditions:
        prev_ratio = _get_llm_bearish_ratio(conn, symbol, days=3, offset=3)
        if prev_ratio is None or prev_ratio < conditions["previous_bearish_ratio_above"]:
            return None

    # 价格条件
    if "price_percentile_below" in conditions:
        pct = _get_price_percentile(symbol, lookback=250)
        if pct is None or pct >= conditions["price_percentile_below"]:
            return None

    if "price_percentile_above" in conditions:
        pct = _get_price_percentile(symbol, lookback=30)
        if pct is None or pct < conditions["price_percentile_above"]:
            return None

    if "price_change_below" in conditions:
        ch = _get_price_change(symbol, days=conditions.get("price_change_days", 5))
        if ch is None or ch >= conditions["price_change_below"]:
            return None

    # 成交量条件
    if "volume_ratio_above" in conditions:
        vr = _get_volume_ratio(symbol, days=5)
        if vr is None or vr < conditions["volume_ratio_above"]:
            return None

    return {
        "rule_id": rule["rule_id"],
        "rule_name": rule["name"],
        "symbol": symbol,
        "action": rule.get("action", "关注"),
        "action_detail": rule.get("action_detail", ""),
        "confidence": rule.get("confidence", "low"),
        "historical_note": rule.get("historical_note", ""),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# === 辅助函数 ===

def _get_comment_ratio(conn, symbol, baseline, days):
    """最近N天日均评论 / 基准中位数日均评论"""
    cutoff = (int(datetime.now().timestamp()) - days * 86400) * 1000
    r = conn.execute("""
        SELECT COUNT(*) FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        WHERE p.symbol = ? AND c.created_at >= ?
    """, (symbol, cutoff)).fetchone()
    daily = (r[0] or 0) / max(days, 1)
    median = baseline.get("median_daily_comments", 0)
    return daily / median if median > 0 else None


def _get_comment_deviation(conn, symbol, baseline, days):
    """(当天评论 - 基准均值) / 基准标准差"""
    now_ms = int(datetime.now().timestamp() * 1000)
    cutoff_ms = now_ms - days * 86400000
    r = conn.execute("""
        SELECT COUNT(*) FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        WHERE p.symbol = ? AND c.created_at >= ?
    """, (symbol, cutoff_ms)).fetchone()
    daily = (r[0] or 0) / max(days, 1)
    avg = baseline.get("avg_daily_comments", 0)
    std = baseline.get("std_daily_comments", 1)
    if std <= 0:
        std = max(avg * 0.5, 1)
    return (daily - avg) / std


def _get_llm_bullish_ratio(conn, symbol, days):
    """从 LLM batch summaries 读取看多比例"""
    r = conn.execute("""
        SELECT SUM(bullish_count) AS bull, SUM(bearish_count) AS bear
        FROM llm_batch_summaries
        WHERE symbol = ? AND summary_date >= date('now', ?)
    """, (symbol, f'-{days} days')).fetchone()
    total = (r[0] or 0) + (r[1] or 0)
    return (r[0] or 0) / total if total >= 5 else None


def _get_llm_bearish_ratio(conn, symbol, days, offset=0):
    """读取之前一段时间（offset天前开始）的看空比例"""
    r = conn.execute("""
        SELECT SUM(bullish_count) AS bull, SUM(bearish_count) AS bear
        FROM llm_batch_summaries
        WHERE symbol = ? AND summary_date >= date('now', ?) 
          AND summary_date < date('now', ?)
    """, (symbol, f'-{days + offset} days', f'-{offset} days')).fetchone()
    total = (r[0] or 0) + (r[1] or 0)
    return (r[1] or 0) / total if total >= 5 else None


def _get_price_percentile(symbol, lookback):
    """当前价在 lookback 天价格区间中的百分位"""
    pq = os.path.join(PROJECT_ROOT, "data", "kline", symbol, "daily.parquet")
    if not os.path.exists(pq):
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(pq)
        cutoff = (datetime.now() - timedelta(days=lookback)).strftime("%Y%m%d")
        period = df[df["trade_date"] >= cutoff]
        if len(period) < 10:
            return None
        cur = period.iloc[-1]["close"]
        lo, hi = period["low"].min(), period["high"].max()
        return (cur - lo) / (hi - lo) * 100 if hi != lo else 50
    except Exception:
        return None


def _get_price_change(symbol, days):
    """最近N天的价格变化率"""
    pq = os.path.join(PROJECT_ROOT, "data", "kline", symbol, "daily.parquet")
    if not os.path.exists(pq):
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(pq).sort_values("trade_date").tail(days + 1)
        if len(df) < 2:
            return None
        return (df.iloc[-1]["close"] - df.iloc[0]["close"]) / df.iloc[0]["close"]
    except Exception:
        return None


def _get_volume_ratio(symbol, days):
    """最近N天均量 / 之前30天均量"""
    pq = os.path.join(PROJECT_ROOT, "data", "kline", symbol, "daily.parquet")
    if not os.path.exists(pq):
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(pq).sort_values("trade_date")
        recent = df.tail(days)
        historical = df.iloc[-(days + 30):-days]
        if len(recent) < 2 or len(historical) < 10:
            return None
        return recent["vol"].mean() / historical["vol"].mean()
    except Exception:
        return None
