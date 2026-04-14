"""
信号事件账本

1. 记录每一个触发的告警/推荐信号
2. 定期（每日）回填已到期信号的后续价格
3. 自动判断信号是否正确
4. 统计各类信号的历史准确率
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def record_signal(conn: sqlite3.Connection, alert: dict):
    """将一个告警记录为信号事件"""
    symbol = alert.get("symbol", "")
    # 跳过板块级别的信号（无K线数据）
    if ":" in str(symbol) or symbol == "全市场":
        return

    price = _get_latest_price(symbol)
    direction = _infer_direction(alert)

    conn.execute("""
        INSERT INTO signal_events
        (signal_type, symbol, signal_date, signal_direction,
         signal_detail, price_at_signal, signal_correct, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (
        alert["alert_type"],
        symbol,
        datetime.now().strftime("%Y-%m-%d"),
        direction,
        alert.get("title", ""),
        price,
        alert.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ))
    conn.commit()


def backfill_signal_prices(conn: sqlite3.Connection):
    """
    回填已到期信号的后续价格。
    在每日扫描中自动调用。
    """
    pending = conn.execute("""
        SELECT id, symbol, signal_date, signal_direction, price_at_signal
        FROM signal_events
        WHERE signal_correct = 'pending'
    """).fetchall()

    updated = 0
    for row in pending:
        signal_date = row[2]
        days_elapsed = (datetime.now() - datetime.strptime(signal_date, "%Y-%m-%d")).days

        if days_elapsed < 5:
            continue

        prices = _get_prices_after(row[1], signal_date, [5, 10, 20])
        if not prices:
            continue

        updates = {}
        for d, p in prices.items():
            if row[4] and row[4] > 0:
                ret = (p - row[4]) / row[4]
                updates[f"price_after_{d}d"] = p
                updates[f"return_{d}d"] = round(ret, 4)

        if not updates:
            continue

        # 判断正确性
        correct = "inconclusive"
        direction = row[3]

        if "return_10d" in updates:
            r10 = updates["return_10d"]
            if direction in ("bearish", "warning"):
                correct = "true" if r10 < -0.03 else ("false" if r10 > 0.05 else "inconclusive")
            elif direction == "bullish":
                correct = "true" if r10 > 0.03 else ("false" if r10 < -0.05 else "inconclusive")
        elif "return_5d" in updates and days_elapsed >= 10:
            r5 = updates["return_5d"]
            if direction in ("bearish", "warning"):
                correct = "true" if r5 < -0.02 else ("false" if r5 > 0.03 else "inconclusive")
            elif direction == "bullish":
                correct = "true" if r5 > 0.02 else ("false" if r5 < -0.03 else "inconclusive")

        set_parts = []
        params = []
        for k, v in updates.items():
            set_parts.append(f"{k} = ?")
            params.append(v)
        set_parts.append("signal_correct = ?")
        params.append(correct)
        set_parts.append("verified_at = ?")
        params.append(datetime.now().strftime("%Y-%m-%d"))
        params.append(row[0])

        conn.execute(
            f"UPDATE signal_events SET {', '.join(set_parts)} WHERE id = ?",
            params
        )
        updated += 1

    if updated > 0:
        conn.commit()
        print(f"  信号回填: {updated}条信号已验证")


def get_signal_scorecard(conn: sqlite3.Connection) -> List[dict]:
    """统计各类信号的历史准确率"""
    rows = conn.execute("""
        SELECT signal_type,
               COUNT(*) AS total,
               SUM(CASE WHEN signal_correct = 'true' THEN 1 ELSE 0 END) AS correct,
               SUM(CASE WHEN signal_correct = 'false' THEN 1 ELSE 0 END) AS wrong,
               SUM(CASE WHEN signal_correct = 'inconclusive' THEN 1 ELSE 0 END) AS inconclusive,
               SUM(CASE WHEN signal_correct = 'pending' THEN 1 ELSE 0 END) AS pending,
               AVG(CASE WHEN return_10d IS NOT NULL THEN return_10d END) AS avg_return_10d
        FROM signal_events
        GROUP BY signal_type
        ORDER BY total DESC
    """).fetchall()

    results = []
    for r in rows:
        verifiable = r[2] + r[3]
        accuracy = r[2] / verifiable if verifiable > 0 else None
        results.append({
            "signal_type": r[0],
            "total": r[1],
            "correct": r[2],
            "wrong": r[3],
            "inconclusive": r[4],
            "pending": r[5],
            "accuracy": round(accuracy, 2) if accuracy is not None else None,
            "avg_return_10d": round(r[6], 4) if r[6] else None,
        })

    return results


# === 辅助函数 ===

def _infer_direction(alert: dict) -> str:
    """从告警类型推断信号方向"""
    type_directions = {
        "volume_spike": "bearish",
        "volume_drop": "neutral",
        "sentiment_extreme": "warning",
        "sentiment_shift": "warning",
        "new_account_influx": "bearish",
        "manipulation_risk": "bearish",
        "kol_activity": "neutral",
        "narrative_drift": "neutral",
        "volume_price_divergence": "warning",
        "sector_heating": "neutral",
        "sector_rotation": "neutral",
    }

    alert_type = alert.get("alert_type", "")
    direction = type_directions.get(alert_type, "neutral")

    if alert_type == "kol_activity":
        data = alert.get("data", {})
        if isinstance(data, dict):
            activities = data.get("activities", [])
            if activities and isinstance(activities[0], dict) and activities[0].get("sentiment"):
                direction = activities[0]["sentiment"]

    if alert_type == "volume_price_divergence":
        data = alert.get("data", {})
        if isinstance(data, dict):
            if data.get("type") == "volume_up_comment_cold":
                direction = "bullish"
            elif data.get("type") == "price_down_sentiment_up":
                direction = "bearish"

    return direction


def _get_latest_price(symbol: str):
    """获取最新收盘价"""
    pq_path = os.path.join(PROJECT_ROOT, "data", "kline", symbol, "daily.parquet")
    if not os.path.exists(pq_path):
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(pq_path).sort_values("trade_date")
        return float(df.iloc[-1]["close"])
    except Exception:
        return None


def _get_prices_after(symbol: str, signal_date: str, days_list: list):
    """获取信号日之后N天的收盘价"""
    pq_path = os.path.join(PROJECT_ROOT, "data", "kline", symbol, "daily.parquet")
    if not os.path.exists(pq_path):
        return {}
    try:
        import pandas as pd
        df = pd.read_parquet(pq_path).sort_values("trade_date")
        date_fmt = signal_date.replace("-", "")

        idx_matches = df[df["trade_date"] >= date_fmt].index
        if len(idx_matches) == 0:
            return {}

        start_idx = idx_matches[0]
        result = {}
        for d in days_list:
            target_idx = start_idx + d
            if target_idx < len(df):
                result[d] = float(df.iloc[target_idx]["close"])

        return result
    except Exception:
        return {}
