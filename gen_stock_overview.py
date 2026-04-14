"""股票数据总览 — 生成 HTML 报告并在浏览器中打开

CLI 用法:
  python main.py stock-overview              # 全部股票，按帖子数降序
  python main.py stock-overview --symbol SH601020  # 只看某只股票
  python main.py stock-overview --no-open    # 不自动打开浏览器，只生成文件
"""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "xueqiu.db")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "export")


def get_market(sym: str) -> str:
    if sym.startswith("SH"):
        return "A股-沪"
    elif sym.startswith("SZ"):
        return "A股-深"
    elif sym.startswith("BJ"):
        return "北交所"
    elif sym.startswith("0"):
        return "港股"
    elif sym.isalpha() or sym.startswith("CR"):
        return "美股/其他"
    return "其他"


def build_report(db_path: str = DB_PATH, symbol: str = None) -> str:
    """生成 HTML 报告，返回输出文件路径"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # watched_stocks 名字映射
    cur.execute("SELECT symbol, name FROM watched_stocks")
    stocks_info = {r[0]: r[1] for r in cur.fetchall()}

    if symbol:
        symbol = symbol.upper()
        query = """
        SELECT
            p.symbol,
            COUNT(DISTINCT p.id) as post_count,
            COUNT(DISTINCT c.id) as comment_count,
            MIN(SUBSTR(p.created_at_str, 1, 10)) as post_earliest,
            MAX(SUBSTR(p.created_at_str, 1, 10)) as post_latest,
            MIN(SUBSTR(c.created_at_str, 1, 10)) as comment_earliest,
            MAX(SUBSTR(c.created_at_str, 1, 10)) as comment_latest
        FROM posts p
        LEFT JOIN comments c ON c.canonical_post_id = p.id OR c.post_id = p.id
        WHERE p.symbol = ?
        GROUP BY p.symbol
        """
        df = pd.read_sql_query(query, conn, params=(symbol,))
    else:
        query = """
        SELECT
            p.symbol,
            COUNT(DISTINCT p.id) as post_count,
            COUNT(DISTINCT c.id) as comment_count,
            MIN(SUBSTR(p.created_at_str, 1, 10)) as post_earliest,
            MAX(SUBSTR(p.created_at_str, 1, 10)) as post_latest,
            MIN(SUBSTR(c.created_at_str, 1, 10)) as comment_earliest,
            MAX(SUBSTR(c.created_at_str, 1, 10)) as comment_latest
        FROM posts p
        LEFT JOIN comments c ON c.canonical_post_id = p.id OR c.post_id = p.id
        GROUP BY p.symbol
        ORDER BY post_count DESC
        """
        df = pd.read_sql_query(query, conn)

    conn.close()

    if df.empty:
        print(f"未找到数据（symbol={symbol or '全部'}）")
        return ""

    df["stock_name"] = df["symbol"].map(stocks_info).fillna("未知")
    df["market"] = df["symbol"].apply(get_market)

    # 生成行 HTML
    rows_html = ""
    for i, (_, row) in enumerate(df.iterrows()):
        mc = {"A股-沪": "m-a", "A股-深": "m-a", "北交所": "m-bj", "港股": "m-hk"}.get(row["market"], "m-other")
        if row["market"] == "美股/其他":
            mc = "m-us"

        pc = f'{row["post_count"]:,}'
        cc = (f'<span class="zero">{row["comment_count"]}</span>'
              if row["comment_count"] == 0
              else f'<span class="highlight">{row["comment_count"]:,}</span>')

        pe = row["post_earliest"] if pd.notna(row["post_earliest"]) else ""
        pl = row["post_latest"] if pd.notna(row["post_latest"]) else ""
        ce = row["comment_earliest"] if pd.notna(row["comment_earliest"]) else ""
        cl = row["comment_latest"] if pd.notna(row["comment_latest"]) else ""

        rows_html += (
            f'<tr><td>{i + 1}</td><td>{row["symbol"]}</td><td>{row["stock_name"]}</td>'
            f'<td><span class="market-tag {mc}">{row["market"]}</span></td>'
            f'<td class="num">{pc}</td><td class="num">{cc}</td>'
            f'<td>{pe}</td><td>{pl}</td><td>{ce}</td><td>{cl}</td></tr>\n'
        )

    total_p = df["post_count"].sum()
    total_c = df["comment_count"].sum()
    stocks_with_comments = len(df[df["comment_count"] > 0])
    today = datetime.now().strftime("%Y-%m-%d")

    title_suffix = f" — {symbol}" if symbol else ""

    final_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>雪球数据库 - 股票数据总览{title_suffix}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #0f1117; color: #e0e0e0; padding: 24px; }}
  h1 {{ font-size: 20px; margin-bottom: 8px; color: #fff; }}
  .summary {{ color: #888; font-size: 14px; margin-bottom: 16px; }}
  .summary span {{ color: #4fc3f7; font-weight: 600; }}
  .toolbar {{ margin-bottom: 12px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
  .toolbar input {{ background: #1a1d28; border: 1px solid #2a2d38; color: #e0e0e0; padding: 6px 12px; border-radius: 4px; font-size: 13px; width: 240px; outline: none; }}
  .toolbar input:focus {{ border-color: #4fc3f7; }}
  .toolbar button {{ background: #1a2d3d; border: 1px solid #2a4d5d; color: #4fc3f7; padding: 6px 14px; border-radius: 4px; font-size: 13px; cursor: pointer; }}
  .toolbar button:hover {{ background: #2a3d4d; }}
  .toolbar button.active {{ background: #2d4d5d; border-color: #4fc3f7; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead {{ position: sticky; top: 0; z-index: 10; }}
  th {{ background: #1a1d28; color: #aaa; font-weight: 500; text-align: left; padding: 10px 12px; border-bottom: 1px solid #2a2d38; white-space: nowrap; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #1a1d28; white-space: nowrap; }}
  tr:hover {{ background: #1a1d28; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .market-tag {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; }}
  .m-a {{ background: #2d1f1f; color: #f87171; }}
  .m-hk {{ background: #1f2d1f; color: #4ade80; }}
  .m-us {{ background: #1f1f2d; color: #818cf8; }}
  .m-bj {{ background: #2d2d1f; color: #facc15; }}
  .m-other {{ background: #1f2d2d; color: #22d3ee; }}
  .zero {{ color: #555; }}
  .highlight {{ color: #4fc3f7; font-weight: 500; }}
  tfoot td {{ background: #1a1d28; font-weight: 600; color: #fff; border-top: 1px solid #4fc3f7; }}
  .hidden {{ display: none; }}
  .stats {{ display: flex; gap: 24px; margin-bottom: 16px; }}
  .stat-card {{ background: #1a1d28; border: 1px solid #2a2d38; border-radius: 8px; padding: 16px 24px; flex: 1; }}
  .stat-card .label {{ color: #888; font-size: 12px; margin-bottom: 4px; }}
  .stat-card .value {{ color: #fff; font-size: 24px; font-weight: 600; }}
  .stat-card .value.blue {{ color: #4fc3f7; }}
  .stat-card .value.green {{ color: #4ade80; }}
  .stat-card .value.purple {{ color: #a78bfa; }}
</style>
</head>
<body>
<h1>雪球舆情数据库 — 股票数据总览{title_suffix}</h1>
<div class="stats">
  <div class="stat-card"><div class="label">股票数量</div><div class="value">{len(df)}</div></div>
  <div class="stat-card"><div class="label">帖子总数</div><div class="value blue">{total_p:,}</div></div>
  <div class="stat-card"><div class="label">评论总数</div><div class="value green">{total_c:,}</div></div>
  <div class="stat-card"><div class="label">有评论的股票</div><div class="value purple">{stocks_with_comments}</div></div>
</div>
<div class="toolbar">
  <input type="text" id="search" placeholder="搜索股票代码/名称..." oninput="filterTable()">
  <button onclick="filterComments()" id="btn-comments">仅显示有评论</button>
  <button onclick="filterNoComments()" id="btn-nocomments">仅无评论</button>
  <button onclick="resetFilter()">重置</button>
</div>
<table id="data">
<thead><tr>
  <th>#</th><th>股票代码</th><th>股票名</th><th>市场</th><th class="num">帖子数</th><th class="num">评论数</th><th>帖子起始</th><th>帖子截止</th><th>评论起始</th><th>评论截止</th>
</tr></thead>
<tbody id="tbody">
{rows_html}
</tbody>
<tfoot><tr>
  <td></td><td>合计</td><td>{len(df)}只</td><td></td>
  <td class="num">{total_p:,}</td><td class="num">{total_c:,}</td><td colspan="4"></td>
</tr></tfoot>
</table>
<script>
function filterTable() {{
  var q = document.getElementById('search').value.toLowerCase();
  document.getElementById('btn-comments').classList.remove('active');
  document.getElementById('btn-nocomments').classList.remove('active');
  var rows = document.querySelectorAll('#tbody tr');
  rows.forEach(function(r) {{
    r.classList.toggle('hidden', r.textContent.toLowerCase().indexOf(q) === -1);
  }});
}}
function filterComments() {{
  document.getElementById('btn-comments').classList.add('active');
  document.getElementById('btn-nocomments').classList.remove('active');
  document.getElementById('search').value = '';
  var rows = document.querySelectorAll('#tbody tr');
  rows.forEach(function(r) {{
    var cc = parseInt(r.querySelectorAll('td')[5].textContent.replace(/,/g,''));
    r.classList.toggle('hidden', cc === 0);
  }});
}}
function filterNoComments() {{
  document.getElementById('btn-nocomments').classList.add('active');
  document.getElementById('btn-comments').classList.remove('active');
  document.getElementById('search').value = '';
  var rows = document.querySelectorAll('#tbody tr');
  rows.forEach(function(r) {{
    var cc = parseInt(r.querySelectorAll('td')[5].textContent.replace(/,/g,''));
    r.classList.toggle('hidden', cc !== 0);
  }});
}}
function resetFilter() {{
  document.getElementById('search').value = '';
  document.getElementById('btn-comments').classList.remove('active');
  document.getElementById('btn-nocomments').classList.remove('active');
  document.querySelectorAll('#tbody tr').forEach(function(r) {{ r.classList.remove('hidden'); }});
}}
</script>
</body></html>"""

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "stock_overview.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final_html)

    print(f"\n✅ 报告已生成: {output_path} ({os.path.getsize(output_path) // 1024} KB)")
    print(f"   股票: {len(df)}只 | 帖子: {total_p:,} | 评论: {total_c:,} | 有评论: {stocks_with_comments}只")
    return output_path


def open_in_browser(file_path: str):
    """在默认浏览器中打开文件"""
    if sys.platform == "darwin":
        subprocess.run(["open", file_path])
    elif sys.platform == "win32":
        os.startfile(file_path)
    else:
        subprocess.run(["xdg-open", file_path])


def cmd_stock_overview(args, config):
    """CLI 入口: python main.py stock-overview"""
    db_cfg = config.get("database", {})
    db_path = os.path.join(PROJECT_ROOT, db_cfg.get("path", "data/xueqiu.db"))
    db_path = os.path.normpath(db_path)

    symbol = getattr(args, "symbol", None)
    no_open = getattr(args, "no_open", False)

    print("📊 正在生成股票数据总览...")
    output = build_report(db_path=db_path, symbol=symbol)

    if output and not no_open:
        print("🌐 正在打开浏览器...")
        open_in_browser(output)


if __name__ == "__main__":
    # 独立运行: python gen_stock_overview.py [symbol]
    symbol = sys.argv[1] if len(sys.argv) > 1 else None
    output = build_report(symbol=symbol)
    if output:
        open_in_browser(output)
