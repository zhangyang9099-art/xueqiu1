"""
KOL追踪模块

1. 识别高价值KOL（粉丝数 + 评论量）
2. 从 LLM 标注结果提取KOL的方向性观点
3. 验证观点后续是否正确（对比K线）
4. 计算并更新 KOL 准确率评级
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def identify_kols(conn: sqlite3.Connection,
                  min_followers: int = 5000,
                  min_comments: int = 3) -> List[dict]:
    """识别数据库中的KOL用户"""
    rows = conn.execute("""
        SELECT up.user_id, up.screen_name, up.followers_count,
               up.verified_type, up.description,
               COUNT(DISTINCT c.id) AS comment_count,
               COUNT(DISTINCT p.symbol) AS stock_count
        FROM user_profiles up
        JOIN comments c ON c.user_id = up.user_id
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        WHERE up.followers_count >= ?
        GROUP BY up.user_id
        HAVING comment_count >= ?
        ORDER BY up.followers_count DESC
    """, (min_followers, min_comments)).fetchall()

    return [dict(zip(
        ["user_id", "screen_name", "followers_count", "verified_type",
         "description", "comment_count", "stock_count"],
        r
    )) for r in rows]


def extract_kol_predictions(conn: sqlite3.Connection, user_id: str,
                            days: int = 180) -> List[dict]:
    """
    从 LLM 标注结果提取某 KOL 的方向性观点。
    直接读 llm_annotations 表，不用关键词匹配。
    """
    now_ms = int(datetime.now().timestamp() * 1000)
    cutoff_ms = now_ms - days * 86400000

    rows = conn.execute("""
        SELECT la.sentiment, la.sentiment_strength, la.intent,
               c.created_at, p.symbol, c.text_plain
        FROM llm_annotations la
        JOIN comments c ON la.source_type = 'comment' AND la.source_id = c.id
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        WHERE c.user_id = ? AND c.created_at >= ?
          AND la.sentiment IN ('bullish', 'bearish')
        ORDER BY c.created_at
    """, (user_id, cutoff)).fetchall()

    results = []
    for r in rows:
        dt = datetime.fromtimestamp(r[4] / 1000).strftime("%Y-%m-%d") if r[4] > 0 else None
        results.append({
            "user_id": user_id,
            "symbol": r[5],
            "date": dt,
            "direction": r[0],
            "strength": r[1],
            "intent": r[2],
            "content_preview": (r[6] or "")[:150],
        })

    return results


def verify_predictions(predictions: List[dict],
                       verify_days: int = 10) -> List[dict]:
    """
    验证预测是否正确。
    看多：后续 verify_days 天内最高收盘价 > 预测日+3%
    看空：后续 verify_days 天内最低收盘价 < 预测日-3%
    """
    for pred in predictions:
        pq_path = os.path.join(
            PROJECT_ROOT, "data", "kline", pred["symbol"], "daily.parquet"
        )
        if not os.path.exists(pq_path):
            pred["verified"] = None
            continue

        try:
            import pandas as pd
            df = pd.read_parquet(pq_path).sort_values("trade_date")
            pred_date = pred["date"].replace("-", "")

            idx_matches = df[df["trade_date"] >= pred_date].index
            if len(idx_matches) == 0:
                pred["verified"] = None
                continue

            start_idx = idx_matches[0]
            pred_close = df.loc[start_idx, "close"]
            future = df.iloc[start_idx + 1: start_idx + 1 + verify_days]

            if len(future) < 3:
                pred["verified"] = None
                continue

            if pred["direction"] == "bullish":
                pred["verified"] = (future["close"].max() - pred_close) / pred_close > 0.03
            else:
                pred["verified"] = (pred_close - future["close"].min()) / pred_close > 0.03

            pred["actual_return"] = round(
                (future.iloc[-1]["close"] - pred_close) / pred_close, 4
            )
        except Exception:
            pred["verified"] = None

    return predictions


def update_kol_ratings(conn: sqlite3.Connection, config: dict = None):
    """更新所有KOL的准确率评级。建议每周运行一次。"""
    kols = identify_kols(conn)
    if not kols:
        print("  未识别到KOL用户（需粉丝>=5000且评论>=3条）")
        return

    print(f"  识别到 {len(kols)} 个KOL用户")

    updated = 0
    for kol in kols:
        predictions = extract_kol_predictions(conn, kol["user_id"])
        if not predictions:
            continue

        verified = verify_predictions(predictions)
        verifiable = [p for p in verified if p["verified"] is not None]

        if not verifiable:
            continue

        correct = sum(1 for p in verifiable if p["verified"])
        total = len(verifiable)
        accuracy = correct / total

        returns = [p.get("actual_return", 0) for p in verifiable
                   if p.get("actual_return") is not None]
        avg_return = sum(returns) / len(returns) if returns else 0

        # 信用等级
        if accuracy >= 0.7 and total >= 5:
            grade = "A"
        elif accuracy >= 0.55 and total >= 3:
            grade = "B"
        elif total < 3:
            grade = "C"
        elif accuracy < 0.4:
            grade = "D"
        else:
            grade = "C"

        # 一致性评分（近期准确率 vs 远期准确率的差异）
        recent = verifiable[-max(3, len(verifiable) // 2):]
        recent_acc = sum(1 for p in recent if p["verified"]) / len(recent)
        consistency = round(1.0 - abs(accuracy - recent_acc), 2)

        conn.execute("""
            INSERT INTO kol_ratings 
            (user_id, screen_name, followers_count, credibility_grade,
             total_predictions, correct_predictions, accuracy_rate, avg_return,
             consistency, last_prediction_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                screen_name=excluded.screen_name,
                followers_count=excluded.followers_count,
                credibility_grade=excluded.credibility_grade,
                total_predictions=excluded.total_predictions,
                correct_predictions=excluded.correct_predictions,
                accuracy_rate=excluded.accuracy_rate,
                avg_return=excluded.avg_return,
                consistency=excluded.consistency,
                last_prediction_date=excluded.last_prediction_date,
                updated_at=excluded.updated_at
        """, (
            kol["user_id"], kol["screen_name"], kol["followers_count"],
            grade, total, correct, round(accuracy, 3), round(avg_return, 4),
            consistency,
            predictions[-1]["date"] if predictions else None,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        updated += 1

    conn.commit()
    print(f"  KOL评级更新完成: {updated}个KOL")
