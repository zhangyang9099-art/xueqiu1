#!/usr/bin/env python3
"""
correct 命令 — 结构化反馈CLI

用法:
  # 逐字段纠正
  python main.py correct --thread 37748230 \
    --field sentiment --original bullish --corrected bearish \
    --reason "用户有反话历史"

  # 交互模式（显示上下文→逐字段确认）
  python main.py correct --thread 37748230 --interactive

  # 也可以不指定thread，直接输入纠正内容
  python main.py correct --symbol SH601020 \
    --field sentiment --original bullish --corrected bearish \
    --reason "实际上是在讽刺"
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _save_feedback_to_db(conn, symbol: str, thread_id: str,
                          field: str, original: str, corrected: str,
                          reason: str, run_id: str = ""):
    """将反馈写入analysis_feedback表"""
    try:
        conn.execute("""
            INSERT INTO analysis_feedback
            (symbol, thread_id, analysis_run_id, field,
             original_value, corrected_value, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, thread_id, run_id, field, original, corrected, reason,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            print("  ⚠ analysis_feedback表尚未创建，请先运行一次analyze命令触发迁移")
        else:
            raise


def _save_feedback_to_file(symbol: str, thread_id: str,
                            field: str, original: str, corrected: str,
                            reason: str):
    """将反馈保存为JSON文件（人工可读备份）"""
    feedback_dir = os.path.join(PROJECT_ROOT, "analysis", "feedback")
    os.makedirs(feedback_dir, exist_ok=True)

    entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "thread_id": thread_id,
        "original_judgment": f"{field}={original}",
        "user_correction": f"{field}={corrected}",
        "lesson": reason,
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{symbol}_{thread_id[:8]}_{field}.json"
    filepath = os.path.join(feedback_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)

    return filepath


def _get_thread_context(conn, thread_id: str) -> dict | None:
    """获取线程上下文用于交互模式"""
    row = conn.execute("""
        SELECT id, symbol, user_name, text_plain, created_at_str
        FROM posts WHERE id = ?
    """, (thread_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def cmd_correct(args, config):
    """correct 命令处理函数"""
    db_path = config.get("database", {}).get("sqlite_path", "data/xueqiu.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)

    if not os.path.exists(db_path):
        print(f"[错误] 数据库不存在: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        symbol = (args.symbol or "").upper() or None
        thread_id = args.thread or ""
        field = args.field or ""
        original = args.original or ""
        corrected = args.corrected or ""
        reason = args.reason or ""

        if args.interactive:
            _interactive_mode(conn, symbol, thread_id)
            return

        # 非交互模式：需要必填参数
        if not field:
            print("[错误] --field 为必填参数（或使用 --interactive 模式）")
            print("可用字段: sentiment, intent, sarcasm, strength, evidence_quality")
            return

        if not thread_id and not symbol:
            print("[错误] 请指定 --thread 或 --symbol")
            return

        # 尝试自动获取symbol
        if not symbol and thread_id:
            row = conn.execute("SELECT symbol FROM posts WHERE id=?", (thread_id,)).fetchone()
            if row:
                symbol = row["symbol"]

        # 保存反馈
        _save_feedback_to_db(conn, symbol or "", thread_id, field, original, corrected, reason)
        filepath = _save_feedback_to_file(symbol or "", thread_id, field, original, corrected, reason)

        print(f"✓ 反馈已记录:")
        print(f"  股票: {symbol or '?'}")
        print(f"  线程: {thread_id or '?'}")
        print(f"  字段: {field}")
        print(f"  原值: {original or '?'}")
        print(f"  纠正: {corrected}")
        print(f"  原因: {reason}")
        print(f"  文件: {filepath}")
        print(f"\n下次 analyze 时将自动加载此反馈作为经验参考。")

    finally:
        conn.close()


def _interactive_mode(conn, symbol: str | None, thread_id: str):
    """交互式反馈模式"""
    # 获取线程上下文
    if thread_id:
        ctx = _get_thread_context(conn, thread_id)
        if not ctx:
            print(f"[错误] 未找到线程: {thread_id}")
            return
        print(f"\n线程上下文:")
        print(f"  ID: {ctx['id']}")
        print(f"  股票: {ctx['symbol']}")
        print(f"  作者: {ctx['user_name']}")
        print(f"  时间: {ctx['created_at_str']}")
        print(f"  内容: {(ctx['text_plain'] or '')[:200]}")
        symbol = ctx["symbol"]

    if not symbol:
        print("[错误] 无法确定股票代码")
        return

    print(f"\n请逐字段纠正分析结果（直接回车跳过）：")

    fields = [
        ("sentiment", "情绪判断 (bullish/bearish/neutral/divided)"),
        ("intent", "言论意图 (genuine/manipulation/contrarian/venting)"),
        ("sarcasm", "反话识别 (true/false)"),
        ("strength", "情绪强度 (1-5)"),
        ("evidence_quality", "论据质量 (high/medium/low)"),
    ]

    corrections = []
    for field_name, desc in fields:
        try:
            val = input(f"  {desc} [跳过]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not val:
            continue

        if "/" in val:
            parts = val.split("/", 1)
            original, corrected = parts[0].strip(), parts[1].strip()
        else:
            original = "?"
            corrected = val

        reason = ""
        try:
            reason = input(f"    原因 [可选]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        corrections.append((field_name, original, corrected, reason))

    if not corrections:
        print("未输入任何纠正。")
        return

    # 批量保存
    for field_name, original, corrected, reason in corrections:
        _save_feedback_to_db(conn, symbol, thread_id, field_name, original, corrected, reason)
        _save_feedback_to_file(symbol, thread_id, field_name, original, corrected, reason)

    print(f"\n✓ 已记录 {len(corrections)} 条纠正:")
    for f, o, c, r in corrections:
        print(f"  {f}: {o} → {c} ({r})")
    print(f"\n下次 analyze {symbol} 时将自动加载。")


def setup_correct_parser(subparsers):
    """注册 correct 子命令的参数解析器"""
    p = subparsers.add_parser("correct", help="纠正分析结果（结构化反馈）")
    p.add_argument("--symbol", default=None, help="股票代码")
    p.add_argument("--thread", default=None, help="帖子/线程ID")
    p.add_argument("--field", default=None,
                   choices=["sentiment", "intent", "sarcasm", "strength", "evidence_quality"],
                   help="要纠正的字段")
    p.add_argument("--original", default=None, help="原始值")
    p.add_argument("--corrected", default=None, help="纠正后的值")
    p.add_argument("--reason", default=None, help="纠正原因")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="交互模式（显示上下文，逐字段确认）")
    return p
