"""
CSV 导出器：生成以 POST/COMMENT 交错的层级 CSV。

每个 POST 行后面紧跟它的 COMMENT 行，
用 type 列区分帖子和评论，post_id 是关联键。
"""

import csv
import os
import time
from datetime import datetime

from utils.time_utils import ms_to_str, market_phase_cn


def export_csv(db, output_dir="data/export", symbol=None, days=None):
    """
    导出层级 CSV。

    Returns:
        生成的文件路径列表
    """
    os.makedirs(output_dir, exist_ok=True)
    files = []

    if symbol:
        stocks = [{"symbol": symbol}]
    else:
        stocks = db.get_watched_stocks(active_only=False)

    for stock in stocks:
        sym = stock["symbol"]
        filepath = _export_stock_csv(db, sym, output_dir, days)
        if filepath:
            files.append(filepath)

    return files


def _export_stock_csv(db, symbol, output_dir, days=None):
    """导出单只股票的层级 CSV。"""
    sql = "SELECT * FROM posts WHERE symbol = ?"
    params = [symbol]

    if days:
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        sql += " AND created_at > ?"
        params.append(cutoff_ms)

    sql += " ORDER BY created_at DESC"
    posts = [dict(row) for row in db.conn.execute(sql, params).fetchall()]

    if not posts:
        return None

    today = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{symbol}_{today}.csv"
    filepath = os.path.join(output_dir, filename)

    headers = [
        "type", "post_id", "comment_id", "time", "market_phase",
        "author", "author_id", "content", "reply_to",
        "stock", "likes", "retweets", "reply_count",
    ]

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for post in posts:
            time_str = post.get("created_at_str") or ms_to_str(post["created_at"])
            phase = market_phase_cn(post.get("market_phase", ""))

            # POST 行
            writer.writerow([
                "POST",
                post["id"],
                "",
                time_str,
                phase,
                post["user_name"],
                post["user_id"],
                (post.get("text_plain") or "")[:500],
                "",
                symbol,
                post.get("like_count", 0),
                post.get("retweet_count", 0),
                post.get("reply_count", 0),
            ])

            # COMMENT 行
            comments = db.conn.execute(
                "SELECT * FROM comments WHERE post_id = ? ORDER BY created_at ASC",
                (post["id"],)
            ).fetchall()

            for c in comments:
                c = dict(c)
                c_time = c.get("created_at_str") or ms_to_str(c["created_at"])
                writer.writerow([
                    "COMMENT",
                    post["id"],
                    c["id"],
                    c_time,
                    "",
                    c["user_name"],
                    c["user_id"],
                    (c.get("text_plain") or "")[:500],
                    c.get("reply_comment_id") or "",
                    "",
                    "",
                    "",
                    "",
                ])

    return filepath
