import json
import os
from datetime import datetime


def _ensure_report_dir(project_root: str) -> str:
    report_dir = os.path.join(project_root, "data", "run_reports")
    os.makedirs(report_dir, exist_ok=True)
    return report_dir


def _fmt_ms(value):
    try:
        value = int(value or 0)
    except Exception:
        value = 0
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_tag(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "run"
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)


def _stock_window_rows(db, symbols):
    rows = db.get_stock_time_windows(symbols=symbols, active_only=False)
    by_symbol = {row["symbol"]: row for row in rows}
    result = []
    for symbol in symbols:
        row = by_symbol.get(symbol, {"symbol": symbol, "name": ""})
        result.append(
            {
                "symbol": row.get("symbol", symbol),
                "name": row.get("name", ""),
                "total_posts": int(row.get("total_posts", 0) or 0),
                "total_comments": int(row.get("total_comments", 0) or 0),
                "first_post_time": _fmt_ms(row.get("first_post_time")),
                "latest_post_time": _fmt_ms(row.get("latest_post_time")),
                "history_complete": bool(row.get("history_complete")),
                "history_stagnant_runs": int(row.get("history_stagnant_runs", 0) or 0),
            }
        )
    return result


def _user_window_rows(db, user_ids):
    rows = db.get_user_time_windows(user_ids=user_ids, active_only=False)
    by_user = {row["user_id"]: row for row in rows}
    result = []
    for user_id in user_ids:
        row = by_user.get(user_id, {"user_id": user_id, "screen_name": ""})
        result.append(
            {
                "user_id": row.get("user_id", user_id),
                "screen_name": row.get("screen_name", ""),
                "total_statuses": int(row.get("total_statuses", 0) or 0),
                "first_status_time": _fmt_ms(row.get("first_status_time")),
                "latest_status_time": _fmt_ms(row.get("latest_status_time")),
                "history_complete": bool(row.get("history_complete")),
                "history_stagnant_runs": int(row.get("history_stagnant_runs", 0) or 0),
            }
        )
    return result


def _sum_history_interruptions(stock_results):
    summary = {}
    for item in stock_results or []:
        for key, value in (item.get("history_interruptions", {}) or {}).items():
            summary[key] = summary.get(key, 0) + int(value or 0)
    return summary


def _write_report_files(project_root: str, report: dict, stem: str):
    report_dir = _ensure_report_dir(project_root)
    timestamp = report["finished_at"].replace(":", "").replace("-", "").replace(" ", "_")
    base_name = f"{timestamp}_{_sanitize_tag(stem)}"
    json_path = os.path.join(report_dir, f"{base_name}.json")
    md_path = os.path.join(report_dir, f"{base_name}.md")
    latest_json = os.path.join(report_dir, "latest_run.json")
    latest_md = os.path.join(report_dir, "latest_run.md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(latest_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md = render_markdown_report(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(latest_md, "w", encoding="utf-8") as f:
        f.write(md)

    return {
        "json_path": json_path,
        "md_path": md_path,
        "latest_json": latest_json,
        "latest_md": latest_md,
    }


def render_markdown_report(report: dict) -> str:
    lines = []
    lines.append(f"# 运行结果摘要")
    lines.append("")
    lines.append(f"- 命令: {report.get('command', '')}")
    lines.append(f"- 状态: {report.get('status', '')}")
    lines.append(f"- 开始: {report.get('started_at', '')}")
    lines.append(f"- 结束: {report.get('finished_at', '')}")
    lines.append(f"- 耗时分钟: {report.get('elapsed_minutes', 0):.1f}")

    totals = report.get("totals", {})
    if totals:
        lines.append(
            f"- 汇总: {totals.get('new_posts', 0)}帖 {totals.get('new_comments', 0)}评论 {totals.get('new_statuses', 0)}条发言"
        )

    scrape = report.get("scrape", {})
    if scrape:
        lines.append("")
        lines.append("## 爬取结果")
        for item in scrape.get("stocks", []):
            lines.append(
                f"- {item.get('name') or item.get('symbol')}: {item.get('new_posts', 0)}帖 {item.get('new_comments', 0)}评论 [{item.get('status', '')}]"
            )
            interruptions = item.get("history_interruptions", {}) or {}
            if interruptions:
                summary = ", ".join(
                    f"{key}={value}" for key, value in sorted(interruptions.items())
                )
                lines.append(f"  历史中断: {summary}")
        for item in scrape.get("users", []):
            lines.append(
                f"- 用户 {item.get('screen_name') or item.get('user_id')}: {item.get('new_statuses', 0)}条发言 [{item.get('status', '')}]"
            )

    interruption_summary = report.get("history_interruption_summary", {}) or {}
    if interruption_summary:
        lines.append("")
        lines.append("## 历史中断原因统计")
        for key, value in sorted(interruption_summary.items()):
            lines.append(f"- {key}: {value}")

    backfill = report.get("comment_backfill", {})
    if backfill:
        lines.append("")
        lines.append("## 评论回填")
        lines.append(
            f"- 范围: {backfill.get('scope', '')}"
        )
        lines.append(
            f"- 结果: 处理 {backfill.get('total_posts', 0)} 帖, 新增 {backfill.get('new_comments', 0)} 评论"
        )

    stocks = report.get("stock_windows", [])
    if stocks:
        lines.append("")
        lines.append("## 股票库状态")
        for item in stocks:
            lines.append(
                f"- {item.get('name') or item.get('symbol')}: {item.get('first_post_time') or '无数据'} ~ {item.get('latest_post_time') or '无数据'} | 帖{item.get('total_posts', 0)} 评{item.get('total_comments', 0)} | {'已触底' if item.get('history_complete') else '未触底'}/停滞{item.get('history_stagnant_runs', 0)}"
            )

    users = report.get("user_windows", [])
    if users:
        lines.append("")
        lines.append("## KOL库状态")
        for item in users:
            lines.append(
                f"- {item.get('screen_name') or item.get('user_id')}: {item.get('first_status_time') or '无数据'} ~ {item.get('latest_status_time') or '无数据'} | 发言{item.get('total_statuses', 0)} | {'已触底' if item.get('history_complete') else '未触底'}/停滞{item.get('history_stagnant_runs', 0)}"
            )

    return "\n".join(lines) + "\n"


def write_scrape_report(project_root: str, db, *, mode: str, pages: int, workers: int,
                        stock_targets: list, user_targets: list, stock_results: list,
                        user_results: list, started_at: float, status: str = "success",
                        error: str = ""):
    finished_at = datetime.now()
    report = {
        "type": "scrape",
        "command": "scrape",
        "status": status,
        "mode": mode,
        "pages": int(pages or 0),
        "workers": int(workers or 0),
        "started_at": datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_minutes": max(0.0, (finished_at.timestamp() - started_at) / 60.0),
        "error": error,
        "targets": {
            "stocks": stock_targets,
            "users": user_targets,
        },
        "scrape": {
            "stocks": stock_results,
            "users": user_results,
        },
        "totals": {
            "new_posts": sum(item.get("new_posts", 0) or 0 for item in stock_results),
            "new_comments": sum(item.get("new_comments", 0) or 0 for item in stock_results),
            "new_statuses": sum(item.get("new_statuses", 0) or 0 for item in user_results),
        },
        "history_interruption_summary": _sum_history_interruptions(stock_results),
        "stock_windows": _stock_window_rows(db, [item["symbol"] for item in stock_targets]),
        "user_windows": _user_window_rows(db, [item["user_id"] for item in user_targets]),
    }
    return _write_report_files(project_root, report, f"scrape_{mode}")


def write_comment_backfill_report(project_root: str, db, *, symbol: str, days, max_posts,
                                  post_id: str, result: dict, started_at: float,
                                  status: str = "success", error: str = ""):
    finished_at = datetime.now()
    scope = "全历史" if days in (None, 0) else f"最近{days}天"
    symbols = [symbol] if symbol else [row["symbol"] for row in db.get_watched_stocks(active_only=False)]
    report = {
        "type": "comment_backfill",
        "command": "backfill-comments",
        "status": status,
        "started_at": datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_minutes": max(0.0, (finished_at.timestamp() - started_at) / 60.0),
        "error": error,
        "comment_backfill": {
            "symbol": symbol or "",
            "days": days,
            "max_posts": max_posts,
            "post_id": post_id or "",
            "scope": scope,
            "total_posts": int(result.get("total_posts", 0) or 0),
            "new_comments": int(result.get("new_comments", 0) or 0),
        },
        "totals": {
            "new_posts": 0,
            "new_comments": int(result.get("new_comments", 0) or 0),
            "new_statuses": 0,
        },
        "stock_windows": _stock_window_rows(db, symbols),
        "user_windows": [],
    }
    return _write_report_files(project_root, report, "comment_backfill")
