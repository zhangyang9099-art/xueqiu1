"""
JSON 导出器：生成以 DiscussionThread 为核心的嵌套 JSON 快照。

输出文件: data/snapshots/{SYMBOL}_{DATE}.json
这是面向 AI 分析的主力格式。
"""

import json
import os
from datetime import datetime

from utils.time_utils import ms_to_str, ms_to_datetime, market_phase_cn


def export_json(db, output_dir="data/snapshots", symbol=None, days=None):
    """
    导出 JSON 快照。

    Args:
        db: Database 实例
        output_dir: 输出目录
        symbol: 限定股票（None=全部）
        days: 限定最近 N 天（None=全部）

    Returns:
        生成的文件路径列表
    """
    os.makedirs(output_dir, exist_ok=True)
    files = []

    # 获取要导出的股票列表
    if symbol:
        stocks = [{"symbol": symbol}]
    else:
        stocks = db.get_watched_stocks(active_only=False)

    for stock in stocks:
        sym = stock["symbol"]
        filepath = _export_stock_json(db, sym, output_dir, days)
        if filepath:
            files.append(filepath)

    return files


def _export_stock_json(db, symbol, output_dir, days=None):
    """导出单只股票的 JSON 快照。"""
    import time

    # 构建查询条件
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

    # 获取股票名称
    stock_row = db.conn.execute(
        "SELECT name FROM watched_stocks WHERE symbol = ?", (symbol,)
    ).fetchone()
    stock_name = stock_row["name"] if stock_row else ""

    # 构建线程列表
    threads = []
    total_comments = 0

    for post in posts:
        # 获取该帖子的所有评论
        comments_rows = db.conn.execute(
            "SELECT * FROM comments WHERE post_id = ? ORDER BY created_at ASC",
            (post["id"],)
        ).fetchall()
        comments = [dict(c) for c in comments_rows]
        total_comments += len(comments)

        # 计算讨论时长
        duration_hours = 0
        if comments:
            last_comment_time = max(c["created_at"] for c in comments)
            if post["created_at"] and last_comment_time:
                duration_hours = round(
                    (last_comment_time - post["created_at"]) / 3600000, 1
                )

        # 计算参与者
        participant_ids = set(c["user_id"] for c in comments if c["user_id"])

        thread = {
            "id": post["id"],
            "author": post["user_name"],
            "author_id": post["user_id"],
            "time": post.get("created_at_str") or ms_to_str(post["created_at"]),
            "market_phase": post.get("market_phase") or "",
            "market_phase_cn": market_phase_cn(post.get("market_phase", "")),
            "title": post.get("title") or "",
            "content": post.get("text_plain") or "",
            "content_html": post.get("text_html") or "",
            "likes": post.get("like_count", 0),
            "retweets": post.get("retweet_count", 0),
            "comments_count": len(comments),
            "claimed_comments": post.get("reply_count", 0),
            "participants": len(participant_ids),
            "discussion_duration_hours": duration_hours,
            "comments": _build_comment_tree(comments),
        }
        threads.append(thread)

    # 日期范围
    dates = [ms_to_str(p["created_at"])[:10] for p in posts if p["created_at"]]
    date_range = f"{min(dates)} ~ {max(dates)}" if dates else ""

    snapshot = {
        "meta": {
            "stock": symbol,
            "stock_name": stock_name,
            "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_threads": len(threads),
            "total_comments": total_comments,
            "date_range": date_range,
        },
        "threads": threads,
    }

    # 写入文件
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{symbol}_{today}.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return filepath



def _build_comment_tree(comments: list) -> list:
    """将平铺的评论列表构建为树形结构。"""
    from utils.time_utils import ms_to_str
    # 先建索引
    by_id = {}
    for c in comments:
        node = {
            "id": c["id"],
            "author": c["user_name"],
            "author_id": c["user_id"],
            "time": c.get("created_at_str") or ms_to_str(c["created_at"]),
            "content": c.get("text_plain") or "",
            "likes": c.get("like_count", 0) if isinstance(c.get("like_count"), int) else 0,
            "depth": c.get("depth", 1) if isinstance(c.get("depth"), int) else 1,
            "reply_to": c.get("parent_comment_id") or c.get("reply_comment_id") or None,
            "reply_to_user": c.get("reply_to_user_name") or None,
            "children": [],
        }
        by_id[c["id"]] = node

    # 构建树
    roots = []
    for cid, node in by_id.items():
        parent_id = node["reply_to"]
        if parent_id and str(parent_id) in by_id:
            by_id[str(parent_id)]["children"].append(node)
        else:
            roots.append(node)

    return roots
