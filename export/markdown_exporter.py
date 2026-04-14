"""
Markdown 导出器：生成人类可读的讨论区文档。

按日期分组，帖子为标题，评论缩进排列，楼中楼用 ↪ 标记。
"""

import os
import time
from datetime import datetime
from collections import defaultdict

from utils.time_utils import ms_to_str, ms_to_date_str, market_phase_cn


def export_markdown(db, output_dir="data/export", symbol=None, days=None):
    """
    导出 Markdown 文档。

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
        filepath = _export_stock_md(db, sym, output_dir, days)
        if filepath:
            files.append(filepath)

    return files


def _export_stock_md(db, symbol, output_dir, days=None):
    """导出单只股票的 Markdown。"""
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

    # 按日期分组
    daily = defaultdict(list)
    for post in posts:
        date_str = ms_to_date_str(post["created_at"]) or "未知日期"
        daily[date_str].append(post)

    # 生成 Markdown
    lines = []
    lines.append(f"# {symbol} {stock_name} 讨论区\n")
    lines.append(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"帖子总数: {len(posts)}\n")
    lines.append("")

    for date_str in sorted(daily.keys(), reverse=True):
        day_posts = daily[date_str]
        lines.append(f"## {date_str}\n")

        for post in day_posts:
            time_str = (post.get("created_at_str") or ms_to_str(post["created_at"]))
            time_short = time_str[11:16] if len(time_str) >= 16 else time_str
            phase = market_phase_cn(post.get("market_phase", ""))
            author = post["user_name"] or "匿名"
            likes = post.get("like_count", 0)
            reply_count = post.get("reply_count", 0)

            lines.append(f"### {time_short} {author} [{phase}] [赞{likes} 评{reply_count}]\n")

            content = (post.get("text_plain") or "").strip()
            if content:
                for para in content.split("\n"):
                    para = para.strip()
                    if para:
                        lines.append(f"> {para}\n")
            lines.append("")

            # 评论
            comments = db.conn.execute(
                "SELECT * FROM comments WHERE post_id = ? ORDER BY created_at ASC",
                (post["id"],)
            ).fetchall()

            # 建立评论 ID → 作者名 的映射（用于楼中楼标记）
            comment_author_map = {}
            for c in comments:
                c = dict(c)
                comment_author_map[c["id"]] = c["user_name"]

            for c in comments:
                c = dict(c)
                c_time = (c.get("created_at_str") or ms_to_str(c["created_at"]))
                c_time_short = c_time[11:16] if len(c_time) >= 16 else c_time
                c_author = c["user_name"] or "匿名"
                c_content = (c.get("text_plain") or "").strip()
                reply_to_id = c.get("reply_comment_id") or ""

                if reply_to_id and reply_to_id in comment_author_map:
                    reply_to_name = comment_author_map[reply_to_id]
                    lines.append(f"  ↪ {c_time_short} **{c_author}** 回复 {reply_to_name}:\n")
                else:
                    lines.append(f"💬 {c_time_short} **{c_author}**:\n")

                if c_content:
                    prefix = "  > " if (reply_to_id and reply_to_id in comment_author_map) else "> "
                    for para in c_content.split("\n"):
                        para = para.strip()
                        if para:
                            lines.append(f"{prefix}{para}\n")
                lines.append("")

            lines.append("---\n")
            lines.append("")

    # 写入文件
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{symbol}_{today}.md"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return filepath
