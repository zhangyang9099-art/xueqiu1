#!/usr/bin/env python3
"""
常用进展查询工具。

用途：
1. 直接查看评论回填队列实时进展
2. 直接查看数据库评论总数
3. 直接查看指定股票的帖子/评论/缺口统计
4. 直接导出自选股 pages 覆盖情况

示例：
  python utils/query_progress.py db-comments
  python utils/query_progress.py comment-backfill-status
  python utils/query_progress.py symbol-stats --symbols 00100 01945 002268
  python utils/query_progress.py watchlist-pages
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.database import Database

DB_PATH = ROOT / "data" / "xueqiu.db"
OVERNIGHT_DIR = ROOT / "data" / "overnight_runs"
RUN_REPORT_DIR = ROOT / "data" / "run_reports"
USER_SYNC_STATE_PATH = RUN_REPORT_DIR / "user_sync_state.json"
DEFAULT_WATCHLIST = Path("/Users/zhangyang/Desktop/自选股.csv")


@dataclass
class SymbolStats:
    symbol: str
    name: str
    posts: int
    comments: int
    missing_comments: int
    gap_posts: int
    cursor_page: int
    history_complete: int
    history_stagnant_runs: int


def connect_db() -> sqlite3.Connection:
    try:
        migrator = Database({"sqlite_path": str(DB_PATH), "log_lifecycle": False})
        migrator.close()
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower():
            raise
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def latest_log(prefix: str) -> Path | None:
    files = sorted(OVERNIGHT_DIR.glob(f"{prefix}*.log"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def latest_batch_log() -> Path | None:
    files = sorted(OVERNIGHT_DIR.glob("batch_*.log"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def parse_ts_from_bracket(line: str) -> datetime | None:
    try:
        raw = line.split("]", 1)[0].lstrip("[")
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "未知"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def active_process_count_contains(pattern: str) -> list[str]:
    return active_process_lines([pattern])


def active_process_lines(patterns: Iterable[str]) -> list[str]:
    try:
        proc = subprocess.run(
            ["ps", "-Ao", "pid,etime,command"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (PermissionError, OSError, subprocess.CalledProcessError):
        return []
    lines: list[str] = []
    for raw in proc.stdout.splitlines():
        if all(pattern in raw for pattern in patterns):
            lines.append(raw.strip())
    return lines


def normalize_symbol_candidates(raw: str) -> list[str]:
    raw = raw.strip()
    upper = raw.upper()
    candidates = [upper]
    if upper.isdigit() and len(upper) == 6:
        if upper[0] in {"5", "6", "9"}:
            candidates.append(f"SH{upper}")
        candidates.append(f"SZ{upper}")
    return list(dict.fromkeys(candidates))


def resolve_symbol_name(conn: sqlite3.Connection, raw: str) -> tuple[str, str]:
    candidates = normalize_symbol_candidates(raw)
    placeholders = ",".join("?" for _ in candidates)
    row = conn.execute(
        f"""
        SELECT symbol, COALESCE(name, symbol) AS name
        FROM watched_stocks
        WHERE symbol IN ({placeholders})
           OR symbol LIKE ?
        ORDER BY CASE
            WHEN symbol = ? THEN 0
            WHEN symbol = ? THEN 1
            ELSE 2
        END
        LIMIT 1
        """,
        [*candidates, f"%{raw.strip().upper()}", candidates[0], candidates[-1]],
    ).fetchone()
    if row:
        name = row["name"] or watchlist_name_map().get(raw.strip(), "") or row["symbol"]
        return row["symbol"], name
    fallback = watchlist_name_map().get(raw.strip(), "") or raw.strip().upper()
    return raw.strip().upper(), fallback


def watchlist_name_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not DEFAULT_WATCHLIST.exists():
        return mapping
    try:
        with DEFAULT_WATCHLIST.open("r", encoding="utf-16", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                name = (row.get("名称") or row.get("股票名称") or row.get("name") or "").strip()
                code = (row.get("代码") or row.get("股票代码") or row.get("symbol") or "").strip().lstrip("'")
                if name and code:
                    mapping[code] = name
    except OSError:
        return mapping
    return mapping


def fetch_symbol_stats(conn: sqlite3.Connection, raw_symbols: list[str]) -> list[SymbolStats]:
    resolved = [resolve_symbol_name(conn, raw) for raw in raw_symbols]
    symbols = [symbol for symbol, _ in resolved]
    if not symbols:
        return []

    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""
        WITH post_counts AS (
            SELECT
                symbol,
                COUNT(*) AS posts,
                SUM(CASE WHEN reply_count > comments_scraped THEN reply_count - comments_scraped ELSE 0 END) AS missing_comments,
                SUM(CASE WHEN reply_count > comments_scraped THEN 1 ELSE 0 END) AS gap_posts
            FROM posts
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        ),
        comment_counts AS (
            SELECT
                p.symbol AS symbol,
                COUNT(DISTINCT c.id) AS comments
            FROM comments c
            JOIN posts p ON p.id = c.post_id
            WHERE p.symbol IN ({placeholders})
            GROUP BY p.symbol
        )
        SELECT
            ws.symbol,
            COALESCE(ws.name, ws.symbol) AS name,
            COALESCE(pc.posts, 0) AS posts,
            COALESCE(cc.comments, 0) AS comments,
            COALESCE(pc.missing_comments, 0) AS missing_comments,
            COALESCE(pc.gap_posts, 0) AS gap_posts,
            COALESCE(ws.history_cursor_page, 0) AS cursor_page,
            COALESCE(ws.history_complete, 0) AS history_complete,
            COALESCE(ws.history_stagnant_runs, 0) AS history_stagnant_runs
        FROM watched_stocks ws
        LEFT JOIN post_counts pc ON pc.symbol = ws.symbol
        LEFT JOIN comment_counts cc ON cc.symbol = ws.symbol
        WHERE ws.symbol IN ({placeholders})
        ORDER BY ws.symbol
        """,
        [*symbols, *symbols, *symbols],
    ).fetchall()

    mapping = {row["symbol"]: row for row in rows}
    stats: list[SymbolStats] = []
    for symbol, fallback_name in resolved:
        row = mapping.get(symbol)
        if row is None:
            stats.append(
                SymbolStats(
                    symbol=symbol,
                    name=fallback_name,
                    posts=0,
                    comments=0,
                    missing_comments=0,
                    gap_posts=0,
                    cursor_page=0,
                    history_complete=0,
                    history_stagnant_runs=0,
                )
            )
            continue
        stats.append(
            SymbolStats(
                symbol=row["symbol"],
                name=row["name"] or fallback_name or row["symbol"],
                posts=int(row["posts"]),
                comments=int(row["comments"]),
                missing_comments=int(row["missing_comments"]),
                gap_posts=int(row["gap_posts"]),
                cursor_page=int(row["cursor_page"]),
                history_complete=int(row["history_complete"]),
                history_stagnant_runs=int(row["history_stagnant_runs"]),
            )
        )
    return stats


def parse_comment_backfill_log(log_path: Path) -> dict:
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    entries: list[dict] = []
    current: dict | None = None
    last_progress: dict | None = None

    for line in lines:
        if "] START " in line:
            code = line.split("] START ", 1)[1].strip()
            current = {
                "code": code,
                "started_line": line,
                "started_at": parse_ts_from_bracket(line),
                "completed": False,
            }
            entries.append(current)
            continue
        if current and "评论回填完成: 处理 " in line:
            tail = line.split("评论回填完成: 处理 ", 1)[1]
            processed = int(tail.split(" 帖", 1)[0])
            new_comments = int(tail.split("新增 ", 1)[1].split(" 条评论", 1)[0])
            current["processed_posts"] = processed
            current["new_comments"] = new_comments
            continue
        if current and "] END " in line:
            current["completed"] = True
            current["end_line"] = line
            current["ended_at"] = parse_ts_from_bracket(line)
            if current.get("started_at") and current.get("ended_at"):
                current["duration_seconds"] = int(
                    (current["ended_at"] - current["started_at"]).total_seconds()
                )
            current = None
            continue

        if "[INFO] xueqiu_scraper - [" in line and "] 帖子 " in line and ": 差 " in line:
            try:
                progress_part = line.split(" - [", 1)[1].split("] 帖子", 1)[0]
                cur, total = progress_part.split("/", 1)
                post_part = line.split("] 帖子 ", 1)[1]
                post_id = post_part.split(" ", 1)[0]
                last_progress = {
                    "current": int(cur),
                    "total": int(total),
                    "post_id": post_id,
                    "line": line,
                }
            except (ValueError, IndexError):
                pass

    active = None
    for entry in reversed(entries):
        if not entry.get("completed"):
            active = entry
            break

    return {
        "entries": entries,
        "active": active,
        "last_progress": last_progress,
    }


def estimate_comment_backfill_eta(
    parsed: dict,
    active_process_lines_raw: list[str],
) -> dict:
    entries = parsed["entries"]
    active = parsed["active"]
    last_progress = parsed["last_progress"]

    completed = [e for e in entries if e.get("completed") and e.get("duration_seconds")]
    avg_symbol_seconds = (
        sum(int(e["duration_seconds"]) for e in completed) / len(completed) if completed else None
    )

    current_remaining_seconds = None
    if active and active.get("started_at") and last_progress and last_progress.get("current", 0) > 0:
        elapsed = max(1, int((datetime.now() - active["started_at"]).total_seconds()))
        per_post = elapsed / max(1, last_progress["current"])
        remaining_posts = max(0, int(last_progress["total"]) - int(last_progress["current"]))
        current_remaining_seconds = per_post * remaining_posts

    queue_symbols: list[str] = []
    queue_line = next((line for line in active_process_lines_raw if "/bin/zsh -c for s in " in line), "")
    if " for s in " in queue_line and "; do " in queue_line:
        try:
            tail = queue_line.split(" for s in ", 1)[1]
            seq = tail.split("; do ", 1)[0]
            queue_symbols = [token for token in seq.split() if token]
        except (IndexError, ValueError):
            queue_symbols = []

    remaining_symbols_after_active = 0
    if active and queue_symbols:
        try:
            idx = queue_symbols.index(active["code"])
            remaining_symbols_after_active = max(0, len(queue_symbols) - idx - 1)
        except ValueError:
            remaining_symbols_after_active = 0

    queue_remaining_seconds = None
    if current_remaining_seconds is not None and avg_symbol_seconds is not None:
        queue_remaining_seconds = current_remaining_seconds + remaining_symbols_after_active * avg_symbol_seconds
    elif current_remaining_seconds is not None:
        queue_remaining_seconds = current_remaining_seconds

    return {
        "avg_symbol_seconds": avg_symbol_seconds,
        "current_remaining_seconds": current_remaining_seconds,
        "queue_remaining_seconds": queue_remaining_seconds,
        "remaining_symbols_after_active": remaining_symbols_after_active,
    }


def latest_history_manifest_path() -> Path:
    return OVERNIGHT_DIR / "queue_manifest.json"


def load_history_manifest(manifest_path: Path | None = None) -> dict:
    path = manifest_path or latest_history_manifest_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_history_batch_log(log_path: Path) -> dict:
    text = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    completed_stocks: list[dict] = []
    current_page = None
    current_stock_name = None
    current_symbol = None
    batch_started_at = None
    batch_finished = False
    for line in text:
        if line.startswith("[") and "批次开始:" in line and batch_started_at is None:
            batch_started_at = parse_ts_from_bracket(line)
        if line.startswith("[") and "批次结束:" in line:
            batch_finished = True
        if line.startswith("✓ [") and "分钟" in line:
            try:
                body = line.split("✓ [", 1)[1]
                name = body.split("]", 1)[0]
                rest = body.split("] ", 1)[1]
                new_posts = int(rest.split("帖", 1)[0])
                duration_minutes = float(rest.rsplit(" ", 1)[1].replace("分钟", ""))
                completed_stocks.append(
                    {
                        "name": name,
                        "new_posts": new_posts,
                        "duration_seconds": int(duration_minutes * 60),
                    }
                )
            except (ValueError, IndexError):
                pass
        if "[INFO] xueqiu_scraper - [" in line and "] 第 " in line and " 页 | 累计:" in line:
            try:
                bracket = line.split("[INFO] xueqiu_scraper - [", 1)[1]
                sym_name = bracket.split("]", 1)[0]
                if "(" in sym_name and ")" in sym_name:
                    current_symbol = sym_name.split("(", 1)[0]
                    current_stock_name = sym_name.split("(", 1)[1].rsplit(")", 1)[0]
                current_page = int(line.split("] 第 ", 1)[1].split(" 页", 1)[0])
            except (ValueError, IndexError):
                pass
    return {
        "completed_stocks": completed_stocks,
        "current_page": current_page,
        "current_stock_name": current_stock_name,
        "current_symbol": current_symbol,
        "batch_started_at": batch_started_at,
        "batch_finished": batch_finished,
    }


def estimate_history_queue_eta(manifest: dict, current_batch_info: dict | None = None) -> dict:
    batches = manifest.get("batches", [])
    completed_batches = [b for b in batches if b.get("returncode") == 0]
    batch_seconds: list[int] = []
    stock_seconds: list[int] = []
    for batch in completed_batches:
        try:
            started = datetime.strptime(batch["started_at"], "%Y%m%d_%H%M%S")
            finished = datetime.strptime(batch["finished_at"], "%Y-%m-%d %H:%M:%S")
            duration = int((finished - started).total_seconds())
            batch_seconds.append(duration)
            count = max(1, len(batch.get("stocks", [])))
            stock_seconds.append(int(duration / count))
        except (KeyError, ValueError):
            continue
    avg_batch_seconds = sum(batch_seconds) / len(batch_seconds) if batch_seconds else None
    avg_stock_seconds = sum(stock_seconds) / len(stock_seconds) if stock_seconds else None

    current_batch_remaining_seconds = None
    current_stock_remaining_seconds = None
    remaining_batches_after_current = 0
    if current_batch_info and avg_stock_seconds is not None:
        current_page = current_batch_info.get("current_page")
        current_batch = current_batch_info.get("current_batch")
        pages_total = int(manifest.get("pages", 100) or 100)
        if current_page:
            avg_page_seconds = avg_stock_seconds / max(1, pages_total)
            current_stock_remaining_seconds = max(0, pages_total - current_page) * avg_page_seconds
        if current_batch:
            completed_in_batch = len(current_batch_info.get("completed_stocks", []))
            total_in_batch = len(current_batch.get("stocks", []))
            current_batch_remaining_seconds = (
                (current_stock_remaining_seconds or avg_stock_seconds)
                + max(0, total_in_batch - completed_in_batch - 1) * avg_stock_seconds
            )
            remaining_batches_after_current = max(0, len(batches) - current_batch.get("batch_no", 0))

    queue_remaining_seconds = None
    if avg_batch_seconds is not None:
        queue_remaining_seconds = remaining_batches_after_current * avg_batch_seconds
        if current_batch_remaining_seconds is not None:
            queue_remaining_seconds += current_batch_remaining_seconds

    return {
        "avg_batch_seconds": avg_batch_seconds,
        "avg_stock_seconds": avg_stock_seconds,
        "current_stock_remaining_seconds": current_stock_remaining_seconds,
        "current_batch_remaining_seconds": current_batch_remaining_seconds,
        "queue_remaining_seconds": queue_remaining_seconds,
        "remaining_batches_after_current": remaining_batches_after_current,
    }


def get_comment_backfill_overview(log: str = "", completed_limit: int = 6) -> dict:
    log_path = Path(log) if log else latest_log("comment_backfill")
    if not log_path or not log_path.exists():
        return {"exists": False}

    parsed = parse_comment_backfill_log(log_path)
    entries = parsed["entries"]
    active = parsed["active"]
    last_progress = parsed["last_progress"]
    active_ps = active_process_lines(["main.py backfill-comments"])
    all_active_ps = active_process_lines(["comment_backfill_rerun_20260323_104358"])
    eta = estimate_comment_backfill_eta(parsed, all_active_ps)

    conn = connect_db()
    total_comments = int(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0])

    completed_codes: list[str] = []
    for entry in entries:
        if entry.get("completed"):
            completed_codes.append(entry["code"])
    stat_codes = completed_codes[-completed_limit :]
    if active:
        stat_codes.append(active["code"])
    stats_list = fetch_symbol_stats(conn, stat_codes)
    stats = {item.symbol: item for item in stats_list}
    name_map = {item.symbol: item.name for item in stats_list}
    resolved_active_symbol = None
    resolved_active_name = None
    if active:
        resolved_active_symbol, resolved_active_name = resolve_symbol_name(conn, active["code"])
    conn.close()

    completed_items = []
    for code in completed_codes[-completed_limit:]:
        symbol = next(
            (item.symbol for item in stats_list if item.symbol in normalize_symbol_candidates(code)),
            code,
        )
        name = name_map.get(symbol, code)
        entry = next((e for e in entries if e["code"] == code and e.get("completed")), None)
        stat = stats.get(symbol)
        if entry:
            completed_items.append({"code": code, "symbol": symbol, "name": name, "entry": entry, "stat": stat})
    active_stat = stats.get(resolved_active_symbol) if resolved_active_symbol else None
    return {
        "exists": True,
        "log_path": str(log_path),
        "total_comments": total_comments,
        "active_ps": active_ps,
        "completed_items": completed_items,
        "active_symbol": resolved_active_symbol,
        "active_name": resolved_active_name,
        "active_entry": active,
        "active_stat": active_stat,
        "last_progress": last_progress,
        "eta": eta,
    }


def get_history_queue_overview(manifest_path: str = "") -> dict:
    path = Path(manifest_path) if manifest_path else latest_history_manifest_path()
    manifest = load_history_manifest(path)
    if not manifest:
        return {"exists": False}

    queue_ps = active_process_lines(["watchlist_queue_runner.py"])
    scrape_ps = active_process_lines(["main.py scrape"])
    batches = manifest.get("batches", [])
    completed_batches = len([b for b in batches if b.get("returncode") == 0])
    pending_count = int(manifest.get("pending_count", 0) or 0)
    processed_count = sum(len(b.get("stocks", [])) for b in batches)

    current_batch = None
    current_batch_log = None
    current_batch_info = None
    latest_log_path = latest_batch_log()
    if (queue_ps or scrape_ps) and latest_log_path and latest_log_path.exists():
        info = parse_history_batch_log(latest_log_path)
        if not info.get("batch_finished"):
            current_batch_log = latest_log_path
            current_batch_info = info
            batch_no = None
            try:
                batch_no = int(latest_log_path.name.split("_", 2)[1])
            except (IndexError, ValueError):
                batch_no = None
            if batch_no is not None:
                current_batch = next((b for b in batches if b.get("batch_no") == batch_no), None)

    eta = estimate_history_queue_eta(
        manifest,
        {"current_batch": current_batch, **(current_batch_info or {})} if current_batch_info else None,
    )

    return {
        "exists": True,
        "manifest_path": str(path),
        "manifest": manifest,
        "queue_ps": queue_ps,
        "scrape_ps": scrape_ps,
        "completed_batches": completed_batches,
        "processed_count": processed_count,
        "pending_count": pending_count,
        "current_batch": current_batch,
        "current_batch_log": str(current_batch_log) if current_batch_log else "",
        "current_batch_info": current_batch_info,
        "eta": eta,
    }


def parse_user_history_log(log_path: Path) -> dict:
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    targets: list[dict] = []
    target_map: dict[str, dict] = {}
    pass_durations: list[float] = []
    last_completed_results: list[dict] = []
    remaining_incomplete = 0
    current_pass_started_at = None

    for line in lines:
        ts = parse_ts_from_bracket(line)
        if "RUN history pass " in line:
            current_pass_started_at = ts
            continue
        if "remaining_incomplete=" in line:
            try:
                remaining_incomplete = int(line.rsplit("remaining_incomplete=", 1)[1].strip())
            except ValueError:
                pass
            if current_pass_started_at and ts:
                pass_durations.append(max(0.0, (ts - current_pass_started_at).total_seconds()))
                current_pass_started_at = None
            continue
        if "· 用户 " in line and " 历史分段 " in line:
            try:
                body = line.split("· 用户 ", 1)[1]
                uid = body.split("(", 1)[0].strip()
                screen_name = body.split("(", 1)[1].split(")", 1)[0].strip()
                if uid not in target_map:
                    entry = {"user_id": uid, "screen_name": screen_name}
                    target_map[uid] = entry
                    targets.append(entry)
            except (IndexError, ValueError):
                pass
            continue
        if line.startswith("✓ 用户 "):
            try:
                body = line.split("✓ 用户 ", 1)[1]
                if "(" in body and ")" in body:
                    uid = body.split("(", 1)[0].strip()
                    screen_name = body.split("(", 1)[1].split(")", 1)[0].strip()
                    tail = body.split("): ", 1)[1]
                else:
                    uid = body.split(":", 1)[0].strip()
                    screen_name = ""
                    tail = body.split(": ", 1)[1]
                statuses = int(tail.split("发言", 1)[0].replace("条", "").strip())
                comments = 0
                if "评论" in tail and "发言 " in tail:
                    comments = int(tail.split("发言 ", 1)[1].split("评论", 1)[0])
                result = {
                    "user_id": uid,
                    "screen_name": screen_name,
                    "statuses": statuses,
                    "comments": comments,
                }
                last_completed_results.append(result)
                if uid not in target_map:
                    target_map[uid] = {"user_id": uid, "screen_name": screen_name}
                    targets.append(target_map[uid])
            except (IndexError, ValueError):
                pass

    return {
        "targets": targets,
        "remaining_incomplete": remaining_incomplete,
        "pass_durations": pass_durations,
        "last_completed_results": last_completed_results,
    }


def load_user_sync_state() -> dict:
    if not USER_SYNC_STATE_PATH.exists():
        return {}
    try:
        return json.loads(USER_SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _is_same_day_ms(value_ms: int | None) -> bool:
    if not value_ms:
        return False
    try:
        dt = datetime.fromtimestamp(int(value_ms) / 1000)
    except (ValueError, OSError, TypeError):
        return False
    return dt.date() == datetime.now().date()


def get_user_scrape_overview(user_ids: list[str] | None = None) -> dict:
    conn = connect_db()
    params: list[str] = []
    where = "WHERE t.is_active=1"
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        where += f" AND t.user_id IN ({placeholders})"
        params.extend(user_ids)

    rows = conn.execute(
        f"""
        WITH base_users AS (
            SELECT
                t.user_id, t.screen_name, t.note,
                t.history_complete, t.history_stagnant_runs,
                t.history_cursor_page, t.history_cursor_oldest_time, t.history_cursor_updated_at,
                t.last_check_time, t.oldest_status_time, t.last_sync_time,
                t.runtime_mode, t.runtime_state, t.runtime_page, t.runtime_chunk,
                t.runtime_total_pages, t.runtime_started_at, t.runtime_updated_at
            FROM tracked_users t
            {where}
        ),
        user_agg AS (
            SELECT
                u.user_id,
                MIN(u.created_at) AS first_status_time,
                MAX(u.created_at) AS latest_status_time,
                COUNT(u.id) AS total_statuses,
                COALESCE(SUM(u.reply_count), 0) AS total_replies,
                COALESCE(SUM(u.like_count), 0) AS total_likes
            FROM user_statuses u
            JOIN base_users b ON b.user_id = u.user_id
            GROUP BY u.user_id
        ),
        user_posts AS (
            SELECT DISTINCT us.user_id, p.id AS post_id
            FROM user_statuses us
            JOIN base_users b ON b.user_id = us.user_id
            JOIN posts p
              ON p.id = us.id
              OR p.id = us.retweet_status_id
              OR p.id = us.parent_status_id
        ),
        user_post_gap AS (
            SELECT
                up.user_id,
                COALESCE(SUM(CASE WHEN p.reply_count > p.comments_scraped THEN p.reply_count - p.comments_scraped ELSE 0 END), 0) AS missing_comments,
                COALESCE(SUM(CASE WHEN p.reply_count > p.comments_scraped THEN 1 ELSE 0 END), 0) AS gap_posts
            FROM user_posts up
            JOIN posts p ON p.id = up.post_id
            GROUP BY up.user_id
        ),
        user_comment_counts AS (
            SELECT
                up.user_id,
                COUNT(DISTINCT cm.comment_id) AS actual_comments
            FROM user_posts up
            JOIN comment_memberships cm ON cm.post_id = up.post_id
            GROUP BY up.user_id
        ),
        user_log_stats AS (
            SELECT
                target AS user_id,
                COUNT(*) AS run_count,
                AVG(duration_seconds) AS avg_duration_sec
            FROM scrape_logs
            WHERE task_type = 'user_track'
              AND status IN ('success', 'partial')
              AND duration_seconds > 0
            GROUP BY target
        ),
        latest_failures AS (
            SELECT l1.target AS user_id, l1.error_message AS latest_error_message
            FROM scrape_logs l1
            JOIN (
                SELECT target, MAX(id) AS max_id
                FROM scrape_logs
                WHERE task_type = 'user_track'
                  AND status IN ('failed', 'deferred', 'partial')
                GROUP BY target
            ) lf ON lf.target = l1.target AND lf.max_id = l1.id
        )
        SELECT
            b.*,
            COALESCE(a.first_status_time, 0) AS first_status_time,
            COALESCE(a.latest_status_time, 0) AS latest_status_time,
            COALESCE(a.total_statuses, 0) AS total_statuses,
            COALESCE(a.total_replies, 0) AS total_replies,
            COALESCE(a.total_likes, 0) AS total_likes,
            COALESCE(cc.actual_comments, 0) AS actual_comments,
            COALESCE(pg.missing_comments, 0) AS missing_comments,
            COALESCE(pg.gap_posts, 0) AS gap_posts,
            COALESCE(ls.run_count, 0) AS run_count,
            COALESCE(ls.avg_duration_sec, 0) AS avg_duration_sec,
            COALESCE(lf.latest_error_message, '') AS latest_error_message
        FROM base_users b
        LEFT JOIN user_agg a ON a.user_id = b.user_id
        LEFT JOIN user_comment_counts cc ON cc.user_id = b.user_id
        LEFT JOIN user_post_gap pg ON pg.user_id = b.user_id
        LEFT JOIN user_log_stats ls ON ls.user_id = b.user_id
        LEFT JOIN latest_failures lf ON lf.user_id = b.user_id
        ORDER BY b.user_id
        """,
        params,
    ).fetchall()
    active_ps = active_process_lines(["main.py sync-users"])
    legacy_user_ps = active_process_lines(["main.py scrape", "--users"])
    state = load_user_sync_state()

    avg_user_seconds_values = [float(r["avg_duration_sec"] or 0) for r in rows if float(r["avg_duration_sec"] or 0) > 0]
    avg_user_seconds = (
        sum(avg_user_seconds_values) / len(avg_user_seconds_values)
        if avg_user_seconds_values else None
    )

    data_rows = [dict(r) for r in rows]
    active_row = next(
        (r for r in data_rows if str(r.get("runtime_state") or "") in {"running", "deferred"}),
        None,
    )
    if not active_row and state.get("current_user_id"):
        active_row = next((r for r in data_rows if r["user_id"] == state.get("current_user_id")), None)

    remaining_users = 0
    current_remaining_seconds = None
    queue_remaining_seconds = None
    if active_row:
        total_pages = int(active_row.get("runtime_total_pages") or 0)
        current_page = int(active_row.get("runtime_page") or 0)
        started_at = int(active_row.get("runtime_started_at") or 0)
        if started_at > 0 and current_page > 0:
            elapsed = max(1, int(datetime.now().timestamp() - (started_at / 1000)))
            if total_pages > 0:
                per_page = elapsed / max(1, current_page)
                current_remaining_seconds = max(0, total_pages - current_page) * per_page
            elif avg_user_seconds is not None:
                current_remaining_seconds = max(0.0, avg_user_seconds - elapsed)
        elif avg_user_seconds is not None:
            current_remaining_seconds = avg_user_seconds

    target_user_ids = [str(uid) for uid in state.get("target_user_ids", []) if str(uid).strip()]
    current_user_id = str(state.get("current_user_id") or (active_row.get("user_id") if active_row else "") or "")
    if target_user_ids and current_user_id:
        try:
            idx = target_user_ids.index(current_user_id)
            remaining_users = max(0, len(target_user_ids) - idx - 1)
        except ValueError:
            remaining_users = max(0, len(target_user_ids) - 1)
    else:
        if state.get("phase") == "history":
            remaining_users = sum(1 for r in data_rows if not int(r.get("history_complete") or 0))
        elif state.get("phase") == "update":
            remaining_users = sum(1 for r in data_rows if int(r.get("history_complete") or 0) and not _is_same_day_ms(r.get("last_sync_time")))

    if avg_user_seconds is not None:
        queue_remaining_seconds = (current_remaining_seconds or avg_user_seconds) + remaining_users * avg_user_seconds

    total_statuses_all = sum(int(r["total_statuses"] or 0) for r in data_rows)
    total_comments_all = sum(int(r["actual_comments"] or 0) for r in data_rows)
    conn.close()

    return {
        "rows": data_rows,
        "active_ps": active_ps,
        "legacy_user_ps": legacy_user_ps,
        "state": state,
        "active_row": active_row,
        "eta": {
            "avg_user_seconds": avg_user_seconds,
            "current_remaining_seconds": current_remaining_seconds,
            "queue_remaining_seconds": queue_remaining_seconds,
            "remaining_users": remaining_users,
        },
        "summary": {
            "user_count": len(data_rows),
            "completed": sum(1 for r in data_rows if int(r.get("history_complete") or 0)),
            "unsynced_today": sum(
                1 for r in data_rows
                if int(r.get("history_complete") or 0) and not _is_same_day_ms(r.get("last_sync_time"))
            ),
            "incomplete": sum(1 for r in data_rows if not int(r.get("history_complete") or 0)),
            "total_statuses": total_statuses_all,
            "total_comments": total_comments_all,
        },
    }


def command_db_comments(_: argparse.Namespace) -> int:
    conn = connect_db()
    total = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    conn.close()
    print(total)
    return 0


def command_symbol_stats(args: argparse.Namespace) -> int:
    conn = connect_db()
    stats = fetch_symbol_stats(conn, args.symbols)
    conn.close()
    for item in stats:
        print(
            f"{item.name} ({item.symbol}) | 帖子 {item.posts} | 评论 {item.comments} | "
            f"缺口评论 {item.missing_comments} | 缺口帖子 {item.gap_posts} | "
            f"cursor_page {item.cursor_page} | 触底 {item.history_complete} | 停滞 {item.history_stagnant_runs}"
        )
    return 0


def command_comment_backfill_status(args: argparse.Namespace) -> int:
    overview = get_comment_backfill_overview(log=args.log, completed_limit=args.completed_limit)
    if not overview.get("exists"):
        print("未找到评论回填日志")
        return 1

    print("评论回填队列状态")
    print(f"日志文件: {overview['log_path']}")
    print(f"数据库评论总数: {overview['total_comments']}")
    print(f"活跃进程数: {len(overview['active_ps'])}")
    for line in overview["active_ps"][:3]:
        print(f"  {line}")
    if overview["eta"]["avg_symbol_seconds"] is not None:
        print(f"已完成股票平均耗时: {format_duration(overview['eta']['avg_symbol_seconds'])}")

    if overview["completed_items"]:
        print("\n已完成")
        for item in overview["completed_items"]:
            entry = item["entry"]
            stat = item["stat"]
            print(
                f"- {item['name']} ({item['symbol']})"
                f": 处理 {entry.get('processed_posts', 0)} 帖, 新增 {entry.get('new_comments', 0)} 评论"
            )
            if stat:
                print(
                    f"  当前库内: 帖子 {stat.posts}, 评论 {stat.comments}, "
                    f"剩余缺口评论 {stat.missing_comments}, 剩余缺口帖子 {stat.gap_posts}"
                )

    if overview["active_entry"]:
        print("\n进行中")
        print(f"- {overview['active_name']} ({overview['active_symbol']})")
        if overview["last_progress"]:
            print(
                f"  当前进度: {overview['last_progress']['current']} / {overview['last_progress']['total']} 帖, "
                f"当前帖子 {overview['last_progress']['post_id']}"
            )
        if overview["eta"]["current_remaining_seconds"] is not None:
            print(f"  当前股票预计剩余: {format_duration(overview['eta']['current_remaining_seconds'])}")
        if overview["eta"]["queue_remaining_seconds"] is not None:
            print(
                f"  整条队列预计剩余: {format_duration(overview['eta']['queue_remaining_seconds'])}"
                f"（后面还有 {overview['eta']['remaining_symbols_after_active']} 只）"
            )
        active_entry = overview["active_entry"]
        if active_entry and "new_comments" in active_entry:
            print(f"  当前累计新增: {active_entry['new_comments']} 评论")
        stat = overview["active_stat"]
        if stat:
            print(
                f"  当前库内: 帖子 {stat.posts}, 评论 {stat.comments}, "
                f"剩余缺口评论 {stat.missing_comments}, 剩余缺口帖子 {stat.gap_posts}"
            )
    else:
        print("\n当前没有未完成的评论回填任务")

    return 0


def load_watchlist(csv_path: Path) -> list[tuple[int, str, str]]:
    items: list[tuple[int, str, str]] = []
    with csv_path.open("r", encoding="utf-16", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for idx, row in enumerate(reader, start=1):
            name = (row.get("名称") or row.get("股票名称") or row.get("name") or "").strip()
            code = (row.get("代码") or row.get("股票代码") or row.get("symbol") or "").strip().lstrip("'")
            if name:
                items.append((idx, name, code))
    return items


def command_watchlist_pages(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    items = load_watchlist(csv_path)
    conn = connect_db()
    rows: list[dict] = []
    missing: list[dict] = []
    for index, name, code in items:
        symbol, resolved_name = resolve_symbol_name(conn, code or name)
        row = conn.execute(
            """
            SELECT
                symbol,
                COALESCE(name, symbol) AS name,
                history_cursor_page,
                history_complete,
                history_stagnant_runs,
                oldest_post_time
            FROM watched_stocks
            WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            missing.append({"index": index, "name": name, "code": code})
            continue
        post_count = conn.execute("SELECT COUNT(*) FROM posts WHERE symbol = ?", (symbol,)).fetchone()[0]
        covered_pages = int((post_count + 9) // 10)
        rows.append(
            {
                "index": index,
                "name": resolved_name,
                "symbol": symbol,
                "covered_pages": covered_pages,
                "cursor_page": int(row["history_cursor_page"] or 0),
                "history_complete": int(row["history_complete"] or 0),
                "history_stagnant_runs": int(row["history_stagnant_runs"] or 0),
            }
        )
    conn.close()

    history_overview = get_history_queue_overview()
    avg_stock_seconds = history_overview.get("eta", {}).get("avg_stock_seconds")
    avg_page_seconds = (avg_stock_seconds / 100.0) if avg_stock_seconds else None
    remaining_pages_total = 0
    for row in rows:
        covered = max(int(row["covered_pages"]), int(row["cursor_page"]))
        row["remaining_pages_to_100"] = max(0, 100 - covered)
        row["estimated_remaining_seconds"] = (
            row["remaining_pages_to_100"] * avg_page_seconds if avg_page_seconds is not None else None
        )
        remaining_pages_total += row["remaining_pages_to_100"]
    overall_eta_seconds = remaining_pages_total * avg_page_seconds if avg_page_seconds is not None else None

    if args.output_md:
        out = Path(args.output_md)
        lines = [
            "# 自选股 Pages 覆盖情况",
            "",
            f"- 总数: {len(items)}",
            f"- 已匹配: {len(rows)}",
            f"- 未匹配: {len(missing)}",
            f"- 剩余页数合计: {remaining_pages_total}",
            f"- 预计剩余时间: {format_duration(overall_eta_seconds)}",
            "",
            "| 序号 | 名称 | Symbol | covered_pages | cursor_page | 剩余页数 | 预计剩余 | 触底 | 停滞 |",
            "|---:|---|---|---:|---:|---:|---|---:|---:|",
        ]
        for row in rows:
            lines.append(
                f"| {row['index']} | {row['name']} | {row['symbol']} | "
                f"{row['covered_pages']} | {row['cursor_page']} | "
                f"{row['remaining_pages_to_100']} | {format_duration(row['estimated_remaining_seconds'])} | "
                f"{row['history_complete']} | {row['history_stagnant_runs']} |"
            )
        if missing:
            lines.extend(["", "## 未匹配", ""])
            for row in missing:
                lines.append(f"- {row['index']}. {row['name']} ({row['code']})")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.output_csv:
        out = Path(args.output_csv)
        with out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "index",
                    "name",
                    "symbol",
                    "covered_pages",
                    "cursor_page",
                    "remaining_pages_to_100",
                    "estimated_remaining_seconds",
                    "history_complete",
                    "history_stagnant_runs",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)

    print(f"总数: {len(items)}")
    print(f"已匹配: {len(rows)}")
    print(f"未匹配: {len(missing)}")
    print(f"剩余页数合计: {remaining_pages_total}")
    print(f"预计剩余时间: {format_duration(overall_eta_seconds)}")
    print("Top 20:")
    for row in rows[:20]:
        print(
            f"- {row['index']}. {row['name']} ({row['symbol']}) | "
            f"covered_pages {row['covered_pages']} | cursor_page {row['cursor_page']} | "
            f"剩余页数 {row['remaining_pages_to_100']} | 预计剩余 {format_duration(row['estimated_remaining_seconds'])} | "
            f"触底 {row['history_complete']} | 停滞 {row['history_stagnant_runs']}"
        )
    return 0


def command_history_queue_status(args: argparse.Namespace) -> int:
    overview = get_history_queue_overview(manifest_path=args.manifest)
    if not overview.get("exists"):
        print("未找到历史批跑队列清单")
        return 1

    manifest = overview["manifest"]
    print("历史批跑队列状态")
    print(f"清单文件: {overview['manifest_path']}")
    print(f"待处理股票总数: {manifest.get('pending_count', 0)}")
    print(f"已完成批次: {overview['completed_batches']}")
    print(f"已处理股票数: {overview['processed_count']}")
    print(f"活跃队列进程数: {len(overview['queue_ps'])}")
    for line in overview["queue_ps"][:2]:
        print(f"  {line}")
    print(f"活跃 scrape 进程数: {len(overview['scrape_ps'])}")
    for line in overview["scrape_ps"][:2]:
        print(f"  {line}")
    if overview["eta"]["avg_batch_seconds"] is not None:
        print(f"历史批次平均耗时: {format_duration(overview['eta']['avg_batch_seconds'])}")
    if overview["eta"]["avg_stock_seconds"] is not None:
        print(f"历史单股平均耗时: {format_duration(overview['eta']['avg_stock_seconds'])}")

    current_batch = overview["current_batch"]
    current_batch_info = overview["current_batch_info"]
    if current_batch and current_batch_info:
        print("\n进行中")
        print(f"- 第 {current_batch['batch_no']} 批")
        names = "、".join(item["name"] for item in current_batch["stocks"])
        print(f"  批次股票: {names}")
        if current_batch_info.get("current_stock_name"):
            print(
                f"  当前股票: {current_batch_info['current_stock_name']} "
                f"(第 {current_batch_info.get('current_page', 0)} 页)"
            )
        print(f"  当前批已完成股票: {len(current_batch_info.get('completed_stocks', []))}")
        if overview["eta"]["current_stock_remaining_seconds"] is not None:
            print(f"  当前股票预计剩余: {format_duration(overview['eta']['current_stock_remaining_seconds'])}")
        if overview["eta"]["current_batch_remaining_seconds"] is not None:
            print(f"  当前批预计剩余: {format_duration(overview['eta']['current_batch_remaining_seconds'])}")
        if overview["eta"]["queue_remaining_seconds"] is not None:
            print(
                f"  整条历史队列预计剩余: {format_duration(overview['eta']['queue_remaining_seconds'])}"
                f"（后面还有 {overview['eta']['remaining_batches_after_current']} 批）"
            )
        print(f"  日志文件: {overview['current_batch_log']}")
    else:
        print("\n当前没有正在运行的历史批跑队列")
        if overview["eta"]["avg_batch_seconds"] is not None:
            print("如后续继续按当前平均速度跑，可按平均批次耗时估算。")
    return 0


def command_user_scrape_status(args: argparse.Namespace) -> int:
    """查看所有跟踪用户的爬取状态（发言数、评论数、时间范围、cursor、ETA）。"""
    user_ids = [str(uid).strip() for uid in (getattr(args, "user_ids", None) or []) if str(uid).strip()]
    overview = get_user_scrape_overview(user_ids=user_ids or None)
    rows = overview["rows"]
    if not rows:
        print("没有活跃的跟踪用户")
        return 0
    print("\n用户/KOL 爬取状态")
    print("=" * 110)
    print(f"活跃 sync-users 进程数: {len(overview['active_ps'])}")
    for line in overview["active_ps"][:2]:
        print(f"  {line}")
    if overview["eta"]["avg_user_seconds"] is not None:
        print(f"平均单用户耗时: {format_duration(overview['eta']['avg_user_seconds'])}")
    if overview["eta"]["current_remaining_seconds"] is not None:
        print(f"当前用户预计剩余: {format_duration(overview['eta']['current_remaining_seconds'])}")
    if overview["eta"]["queue_remaining_seconds"] is not None:
        print(
            f"整体预计剩余: {format_duration(overview['eta']['queue_remaining_seconds'])} "
            f"(后面还有 {overview['eta']['remaining_users']} 个用户)"
        )

    active_row = overview["active_row"]
    if active_row:
        print("\n进行中")
        print(
            f"- {(active_row.get('screen_name') or active_row['user_id'])} "
            f"({active_row['user_id']}) | 模式 {active_row.get('runtime_mode') or overview['state'].get('phase') or '-'} "
            f"| page={int(active_row.get('runtime_page') or 0)} "
            f"| chunk={int(active_row.get('runtime_chunk') or 0)} "
            f"| cursor={int(active_row.get('history_cursor_page') or 0)} "
            f"| state={active_row.get('runtime_state') or '-'}"
        )

    print("\n" + "=" * 110)
    print(f"{'用户':<18} {'发言':>6} {'评论':>6} {'缺口评':>6} {'缺口帖':>6} {'时间范围':<28} {'状态':<14} {'cursor':<8} {'同步':<10}")
    print("=" * 110)

    for row in rows:
        uid = row["user_id"]
        name = (row["screen_name"] or uid)[:16]
        first_ts = _fmt_ts(row["first_status_time"])
        latest_ts = _fmt_ts(row["latest_status_time"])
        time_range = f"{first_ts} ~ {latest_ts}" if first_ts and latest_ts else (first_ts or "无数据")
        cursor_page = int(row["history_cursor_page"] or 0)
        runtime_state = str(row.get("runtime_state") or "")
        if runtime_state:
            status_tag = f"运行中 p{int(row.get('runtime_page') or 0)}"
        elif int(row["history_complete"] or 0):
            status_tag = "✓已触底"
        elif cursor_page > 0:
            status_tag = f"补全中 p{cursor_page}"
        else:
            status_tag = "未开始"
        sync_tag = "今日已同步" if _is_same_day_ms(row.get("last_sync_time")) else (
            _fmt_ts(row.get("last_sync_time"))[5:] if row.get("last_sync_time") else "待同步"
        )
        print(
            f"{name:<18} {int(row['total_statuses'] or 0):>6} {int(row['actual_comments'] or 0):>6} "
            f"{int(row['missing_comments'] or 0):>6} {int(row['gap_posts'] or 0):>6} "
            f"{time_range:<28} {status_tag:<14} "
            f"{('page=' + str(cursor_page)) if cursor_page else '-':<8} {sync_tag:<10}"
        )
        detail_parts = []
        if int(row.get("history_stagnant_runs") or 0) > 0:
            detail_parts.append(f"停滞{int(row['history_stagnant_runs'])}次")
        if int(row.get("total_replies") or 0) > 0:
            detail_parts.append(f"互动{int(row['total_replies'])}")
        if int(row.get("total_likes") or 0) > 0:
            detail_parts.append(f"赞{int(row['total_likes'])}")
        if row.get("note"):
            detail_parts.append(f"[{row['note']}]")
        if detail_parts:
            print(f"{'':>18} {' '.join(detail_parts)}")
        latest_error = str(row.get("latest_error_message") or "").strip()
        if latest_error:
            print(f"{'':>18} 最近异常: {latest_error[:120]}")

    print("=" * 110)
    summary = overview["summary"]
    print(
        f"\n汇总: {summary['user_count']} 个用户 | "
        f"{summary['completed']} 已触底 | {summary['incomplete']} 未触底 | "
        f"{summary['unsynced_today']} 待同步到今天 | "
        f"{summary['total_statuses']} 发言 | {summary['total_comments']} 评论"
    )
    return 0


def _fmt_ts(value) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return str(value)[:16]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="常用进展查询工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("db-comments", help="查询数据库评论总数")
    p.set_defaults(func=command_db_comments)

    p = sub.add_parser("symbol-stats", help="查询指定股票统计")
    p.add_argument("--symbols", nargs="+", required=True)
    p.set_defaults(func=command_symbol_stats)

    p = sub.add_parser("comment-backfill-status", help="查询评论回填队列状态")
    p.add_argument("--log", default="", help="指定日志文件")
    p.add_argument("--completed-limit", type=int, default=8, help="显示最近已完成几只")
    p.set_defaults(func=command_comment_backfill_status)

    p = sub.add_parser("history-queue-status", help="查询历史批跑队列状态")
    p.add_argument("--manifest", default="", help="指定 queue_manifest.json")
    p.set_defaults(func=command_history_queue_status)

    p = sub.add_parser("watchlist-pages", help="导出自选股 pages 覆盖情况")
    p.add_argument("--csv", default=str(DEFAULT_WATCHLIST))
    p.add_argument("--output-md", default="")
    p.add_argument("--output-csv", default="")
    p.set_defaults(func=command_watchlist_pages)

    p = sub.add_parser("user-scrape-status", help="查看用户跟踪爬取状态")
    p.add_argument("--user-ids", nargs="+", default=[], help="指定用户 ID（默认全部活跃用户）")
    p.set_defaults(func=command_user_scrape_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
