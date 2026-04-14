"""
高价值内容每日筛选

从当天所有新帖子/评论中，挑出最值得用户阅读的 TOP-N 条。
评分标准：作者可信度 + 互动热度 + 内容深度 + 信息独特性。
"""

import sqlite3
import math
import os
from datetime import datetime, timedelta
from typing import List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def curate_daily_top(conn: sqlite3.Connection,
                     top_n: int = 5,
                     days: int = 1,
                     symbol_filter: str = None) -> List[dict]:
    """
    筛选最近N天最有价值的内容。

    评分公式（满分100）：
      作者分（30分）：A级KOL=30, B=22, C=15, D=8, 无评级=10
      互动分（25分）：按like_count对数打分
      深度分（25分）：argument_quality为high=25, medium=15, low=5
      标注分（20分）：sentiment_strength >= 3 且有明确观点 = 20
    """
    # 注意: comments.created_at 存的是 "秒级时间戳 × 1000"，不是真正的毫秒
    cutoff = (int(datetime.now().timestamp()) - days * 86400) * 1000

    params = [cutoff_ms]
    symbol_clause = ""
    if symbol_filter:
        symbol_clause = " AND p.symbol = ?"
        params.append(symbol_filter)

    rows = conn.execute(f"""
        SELECT c.id AS comment_id, c.user_id, c.user_name, c.text_plain,
               c.like_count, c.created_at, p.symbol,
               COALESCE(up.followers_count, 0) AS followers,
               la.sentiment, la.sentiment_strength, la.argument_quality,
               la.narrative_tag,
               kr.credibility_grade, kr.accuracy_rate, kr.style
        FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        LEFT JOIN user_profiles up ON up.user_id = c.user_id
        LEFT JOIN llm_annotations la ON la.source_type = 'comment' AND la.source_id = c.id
        LEFT JOIN kol_ratings kr ON kr.user_id = c.user_id
        WHERE c.created_at >= ?
          AND length(COALESCE(c.text_plain, '')) > 30
          {symbol_clause}
        ORDER BY c.created_at DESC
    """, params).fetchall()

    # 列顺序: comment_id(0), user_id(1), user_name(2), text_plain(3),
    #         like_count(4), created_at(5), symbol(6),
    #         followers(7), sentiment(8), sentiment_strength(9),
    #         argument_quality(10), narrative_tag(11),
    #         credibility_grade(12), accuracy_rate(13), style(14)
    scored = []
    for r in rows:
        # 作者分
        grade = r[12] or "C"  # credibility_grade
        author_score = {"A": 30, "B": 22, "C": 15, "D": 8, "E": 3}.get(grade, 10)
        followers = r[7] or 0
        if followers >= 10000 and grade == "C":
            author_score = 20

        # 互动分
        likes = r[4] or 0  # like_count
        interaction_score = min(25, int(math.log2(likes + 1) * 5)) if likes > 0 else 0

        # 深度分
        arg_quality = r[10] or "medium"  # argument_quality
        depth_scores = {"high": 25, "medium": 15, "low": 5}
        depth_score = depth_scores.get(arg_quality, 5)

        # 标注分：有明确方向性观点
        sentiment_strength = r[9] or 0
        sentiment = r[8]  # sentiment
        annotation_score = 20 if (sentiment_strength >= 3 and sentiment in ("bullish", "bearish")) else 0

        total = author_score + interaction_score + depth_score + annotation_score

        # 股票名
        name_row = conn.execute(
            "SELECT name FROM watched_stocks WHERE symbol = ?",
            (r[6],)  # symbol
        ).fetchone()

        # 时间格式化
        created_at = r[5] or 0
        dt = datetime.fromtimestamp(created_at / 1000).strftime("%m-%d %H:%M") if created_at > 0 else ""

        scored.append({
            "score": total,
            "symbol": r[6],
            "stock_name": name_row[0] if name_row else r[6],
            "user_name": r[2],
            "followers": followers,
            "grade": grade,
            "style": r[14],
            "content": (r[3] or "")[:300],
            "sentiment": sentiment,
            "argument_quality": arg_quality,
            "narrative_tag": r[11],
            "likes": likes,
            "time": dt,
            "accuracy_rate": r[13],
        })

    # 按分数排序
    scored.sort(key=lambda x: x["score"], reverse=True)

    # 去重：同一用户最多出现2次
    result = []
    user_count = {}
    for item in scored:
        uid = item["user_name"]
        user_count[uid] = user_count.get(uid, 0) + 1
        if user_count[uid] <= 2:
            result.append(item)
        if len(result) >= top_n:
            break

    return result
