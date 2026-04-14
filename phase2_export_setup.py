#!/usr/bin/env python3
"""
阶段二安装脚本 — 导出重构

创建 export/ 模块（JSON快照 + 层级CSV + Markdown），
修改 main.py 的 export 命令支持新格式和参数。

用法:
  cd ~/Desktop/xueqiu-scraper
  source venv/bin/activate
  python phase2_export_setup.py
"""

import os
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{filepath}.bak_{ts}"
        shutil.copy2(filepath, bak)
        print(f"  备份: {os.path.basename(bak)}")


# ================================================================
# export/__init__.py
# ================================================================

EXPORT_INIT = '''"""导出模块：将数据库数据以 DiscussionThread 为单元导出。"""
'''


# ================================================================
# export/json_exporter.py
# ================================================================

JSON_EXPORTER = r'''"""
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
            "comments": [
                {
                    "id": c["id"],
                    "author": c["user_name"],
                    "author_id": c["user_id"],
                    "time": c.get("created_at_str") or ms_to_str(c["created_at"]),
                    "content": c.get("text_plain") or "",
                    "reply_to": c.get("reply_comment_id") or None,
                }
                for c in comments
            ],
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
'''


# ================================================================
# export/csv_exporter.py
# ================================================================

CSV_EXPORTER = r'''"""
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
'''


# ================================================================
# export/markdown_exporter.py
# ================================================================

MARKDOWN_EXPORTER = r'''"""
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
'''


# ================================================================
# main.py export 命令重写
# ================================================================

NEW_EXPORT_FUNC = '''
def cmd_export(args, config):
    """导出数据（JSON快照 + 层级CSV + Markdown）。"""
    components = init_components(config)
    db = components["db"]

    fmt = getattr(args, 'format', 'all') or 'all'
    symbol = getattr(args, 'symbol', None)
    days = getattr(args, 'days', None)

    if symbol:
        symbol = symbol.upper()

    print(f"开始导出" + (f" {symbol}" if symbol else " 全部股票") +
          (f" 最近{days}天" if days else " 全部数据") + "...")
    print()

    try:
        if fmt in ('all', 'json'):
            from export.json_exporter import export_json
            files = export_json(db, symbol=symbol, days=days)
            for f in files:
                print(f"✓ JSON 快照: {f}")

        if fmt in ('all', 'csv'):
            from export.csv_exporter import export_csv
            files = export_csv(db, symbol=symbol, days=days)
            for f in files:
                print(f"✓ 层级 CSV:  {f}")

        if fmt in ('all', 'md', 'markdown'):
            from export.markdown_exporter import export_markdown
            files = export_markdown(db, symbol=symbol, days=days)
            for f in files:
                print(f"✓ Markdown:  {f}")

        if fmt == 'all':
            print()
            print("提示: JSON 快照在 data/snapshots/ 目录")
            print("      CSV 和 Markdown 在 data/export/ 目录")

    except Exception as e:
        print(f"✗ 导出失败: {e}")
        import traceback
        traceback.print_exc()

    components["client"].close()
    db.close()
'''


def create_export_module():
    """创建 export/ 目录和所有文件。"""
    export_dir = os.path.join(PROJECT_ROOT, "export")
    os.makedirs(export_dir, exist_ok=True)

    files = {
        "__init__.py": EXPORT_INIT,
        "json_exporter.py": JSON_EXPORTER,
        "csv_exporter.py": CSV_EXPORTER,
        "markdown_exporter.py": MARKDOWN_EXPORTER,
    }

    for filename, content in files.items():
        filepath = os.path.join(export_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  ✓ export/{filename}")

    # 创建 snapshots 目录
    os.makedirs(os.path.join(PROJECT_ROOT, "data", "snapshots"), exist_ok=True)
    print("  ✓ data/snapshots/ 目录")


def patch_main_py():
    """修改 main.py 的 export 命令。"""
    filepath = os.path.join(PROJECT_ROOT, "main.py")
    backup(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 找到旧的 cmd_export 函数并替换
    if "def cmd_export(args, config):" in content:
        # 找到函数开头和下一个函数开头之间的内容
        start = content.index("def cmd_export(args, config):")
        # 找到下一个顶层 def（不缩进的）
        rest = content[start + 1:]
        next_def = rest.find("\ndef ")
        if next_def == -1:
            next_def = rest.find("\n\ndef ")
        if next_def == -1:
            # 可能是最后一个函数之前的 main()
            next_def = rest.find("\ndef main():")

        if next_def != -1:
            end = start + 1 + next_def
            old_func = content[start:end]
            content = content[:start] + NEW_EXPORT_FUNC.strip() + "\n\n" + content[end:]
            print("  ✓ 替换 cmd_export 函数")
        else:
            print("  ⚠ 找不到 cmd_export 函数边界，跳过替换")
    else:
        print("  ⚠ 找不到 cmd_export 函数")

    # 添加 export 子命令的参数（--format, --symbol, --days）
    old_export_parser = '    subparsers.add_parser("export", help="导出数据为 CSV")'
    new_export_parser = '''    # export
    p = subparsers.add_parser("export", help="导出数据（JSON/CSV/Markdown）")
    p.add_argument("--format", choices=["all", "json", "csv", "md"], default="all",
                   help="导出格式（默认all=全部）")
    p.add_argument("--symbol", default=None, help="指定股票代码")
    p.add_argument("--days", type=int, default=None, help="只导出最近N天")'''

    if old_export_parser in content:
        content = content.replace(old_export_parser, new_export_parser)
        print("  ✓ 更新 export 子命令参数")
    elif '--format' not in content:
        # 尝试其他格式
        alt = 'subparsers.add_parser("export"'
        if alt in content:
            idx = content.index(alt)
            line_end = content.index("\n", idx)
            old_line = content[idx:line_end]
            content = content[:idx] + new_export_parser.strip() + content[line_end:]
            print("  ✓ 更新 export 子命令参数（备选匹配）")
        else:
            print("  · 跳过: export 子命令参数已更新")
    else:
        print("  · 跳过: export 子命令参数已存在")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print("  ✓ main.py 已更新")


def test_export():
    """测试导出功能。"""
    try:
        import yaml
        with open(os.path.join(PROJECT_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        from storage.database import Database
        db = Database(config.get("database", {}))

        stats = db.get_stats()
        print(f"  数据库: {stats['posts']} 帖子, {stats['comments']} 评论")

        if stats['posts'] > 0:
            from export.json_exporter import export_json
            files = export_json(db, symbol="SH600519", days=3)
            if files:
                fsize = os.path.getsize(files[0])
                print(f"  ✓ JSON 测试导出成功: {files[0]} ({fsize:,} 字节)")

                # 简单验证内容
                import json
                with open(files[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                threads = data.get("threads", [])
                total_c = sum(len(t.get("comments", [])) for t in threads)
                print(f"    线程数: {len(threads)}, 评论数: {total_c}")
            else:
                print("  · 最近3天无数据，跳过测试")
        else:
            print("  · 数据库为空，跳过测试")

        db.close()
    except Exception as e:
        print(f"  ⚠ 测试出错: {e}")
        import traceback
        traceback.print_exc()


def main():
    print("=" * 55)
    print("  雪球爬虫 阶段二 — 导出重构")
    print("=" * 55)
    print()

    print("[1/3] 创建 export/ 模块...")
    create_export_module()
    print()

    print("[2/3] 修改 main.py export 命令...")
    patch_main_py()
    print()

    print("[3/3] 测试导出...")
    test_export()
    print()

    print("=" * 55)
    print("  阶段二安装完成！")
    print("=" * 55)
    print()
    print("导出命令用法:")
    print()
    print("  python main.py export                           # 全部格式全部数据")
    print("  python main.py export --format json             # 只导出 JSON")
    print("  python main.py export --format csv              # 只导出 CSV")
    print("  python main.py export --format md               # 只导出 Markdown")
    print("  python main.py export --symbol SH600519         # 指定股票")
    print("  python main.py export --days 7                  # 最近7天")
    print("  python main.py export --format json --days 3    # 组合")
    print()
    print("输出位置:")
    print("  JSON 快照: data/snapshots/")
    print("  CSV/MD:    data/export/")


if __name__ == "__main__":
    main()
