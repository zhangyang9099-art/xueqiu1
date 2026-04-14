#!/usr/bin/env python3
"""
Prompt模板引擎 v2 — 加载模板、填充数据、注入反馈上下文

v2 变更 (V4优化计划):
  - 集成TokenBudget管理器，按预算分配各section
  - 智能评论截断：高赞 > 楼中楼 > 作者互动 > 时间新
  - 评论增加高赞标记 [+N赞]
  - 用户画像增加description输出和可疑度标记
  - 新增时段聚合格式化
  - 热度分增加偏离度展示
  - 反馈加载保留JSON文件兼容，新增analysis_feedback表查询
"""

import json
import os
from datetime import datetime
from typing import Optional

from .token_budget import TokenBudget

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "analysis", "prompts")
FEEDBACK_DIR = os.path.join(PROJECT_ROOT, "analysis", "feedback")

# 模板 → 文件名映射
LAYER_TEMPLATES = {
    "full": "full-analysis.md",
    "sentiment": "sentiment.md",
    "heat": "heat-propagation.md",
    "manipulation": "manipulation-detect.md",
    "credibility": "user-credibility.md",
    "price": "price-correlation.md",
}


def load_template(layer: str) -> str:
    """加载指定分析层的 Prompt 模板"""
    filename = LAYER_TEMPLATES.get(layer)
    if not filename:
        raise ValueError(f"未知分析层: {layer}，可选: {list(LAYER_TEMPLATES.keys())}")
    path = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"模板文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def smart_select_comments(comments: list, max_count: int, author_id: str = "") -> list:
    """智能选择评论：高赞 > 楼中楼 > 作者互动 > 时间新（已排序）。

    替代原来的 comments[:50] 硬截断。
    """
    if len(comments) <= max_count:
        return comments

    scored = []
    for c in comments:
        score = 0
        score += (c.get("like_count") or 0) * 3       # 高赞优先
        if (c.get("depth") or 1) > 1:
            score += 10                                  # 楼中楼
        if author_id and c.get("user_id") == author_id:
            score += 8                                   # 作者互动
        score += 1                                       # 时间新（列表已按时间排序）
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:max_count]]


def _format_overview(overview: dict) -> str:
    """格式化数据概览为可读文本"""
    lines = [
        f"- 时间范围: {overview['time_range']}",
        f"- 帖子总数: {overview['total_posts']}",
        f"- 评论总数: {overview['total_comments']}",
        f"- 评论完备率: {overview['comment_completion_rate']}",
        f"- 数据跨度: {overview['earliest_post'] or '无'} ~ {overview['latest_post'] or '无'}",
        f"- 涉及股票: {overview['stock_count']}只",
    ]
    if overview.get("kline_status"):
        lines.append("- K线数据:")
        for period, status in overview["kline_status"].items():
            lines.append(f"  - {period}: {status}")
    return "\n".join(lines)


def _format_thread(thread: dict, include_comments: bool = True,
                   max_comments: int = 50) -> str:
    """格式化单个讨论线程为可读文本。

    V4增强:
    - 热度分增加偏离度展示
    - 评论增加高赞标记
    - 使用智能评论截断
    """
    heat = thread.get("heat_score", 0)
    deviation = thread.get("heat_deviation_pct")
    # 偏离度展示
    if deviation is not None and abs(deviation) > 50:
        direction = "远超" if deviation > 0 else "远低于"
        heat_str = f"{heat:.0f}（偏离度{deviation:+.0f}%，{direction}历史均值）"
    elif deviation is not None:
        heat_str = f"{heat:.0f}（偏离度{deviation:+.0f}%）"
    else:
        heat_str = f"{heat:.0f}"

    lines = [
        f"### 线程 #{thread['thread_id'][:8]}  热度分: {heat_str}",
        f"- 作者: {thread['author']} ({thread['author_id']})",
        f"- 时间: {thread['start_time']}",
        f"- 深度讨论: {'是' if thread.get('is_deep_discussion') else '否'} (最大深度{thread.get('max_comment_depth', 1)})",
        f"- 互动: 赞{thread['like_count']} 评{thread.get('actual_comments', 0)} "
        f"转{thread.get('retweet_count', 0)} 藏{thread.get('fav_count', 0)}  参与者{thread.get('participants', 0)}",
        f"- 内容: {(thread.get('content') or '')[:500]}",
    ]

    if include_comments and thread.get("comments"):
        # 智能选择评论
        selected = smart_select_comments(
            thread["comments"], max_comments,
            author_id=thread.get("author_id", "")
        )
        lines.append(f"- 评论树 (智能筛选{len(selected)}/{len(thread['comments'])}条):")
        for c in selected:
            indent = "  " * (c.get("depth", 1) - 1)
            reply_to = f" → @{c.get('reply_to_user_name', '')}" if c.get("reply_to_user_name") else ""
            text = (c.get("text_plain") or "")[:200]
            # 高赞标记：>=3赞显示
            like_badge = f" [+{c['like_count']}赞]" if (c.get("like_count") or 0) >= 3 else ""
            lines.append(f"  {indent}[{c.get('user_name', '?')}{reply_to}]{like_badge} {text}")

    return "\n".join(lines)


def _format_user(user: dict) -> str:
    """格式化用户画像。

    V4增强:
    - 增加description输出（自我介绍）
    - 增加可疑度标记：默认昵称+低粉丝
    """
    flags = []
    if user.get("is_default_name"):
        flags.append("默认昵称")
    if user.get("is_default_avatar"):
        flags.append("默认头像")
    if user.get("verified_type"):
        flags.append(f"认证:{user['verified_type']}")
    # 可疑度标记
    is_suspicious = user.get("is_default_name") and (user.get("followers_count") or 0) < 10
    if is_suspicious:
        flags.append("⚠可疑")
    flag_str = f" [{', '.join(flags)}]" if flags else ""

    base = (
        f"- {user.get('user_name', '?')} ({user.get('user_id', '?')}){flag_str} "
        f"评论{user.get('comment_count', 0)} 帖子{user.get('post_count', 0)} "
        f"粉丝{user.get('followers_count') or '?'} 总发言{user.get('status_count') or '?'}"
    )

    # description（自我介绍，有价值信号）
    desc = (user.get("description") or "").strip()
    if desc:
        base += f"\n  简介: {desc[:80]}"
    return base


def _format_time_dist(dist: list) -> str:
    """格式化时间分布"""
    if not dist:
        return "无数据"
    lines = ["| 日期 | 帖子 | 评论 |", "| --- | ---: | ---: |"]
    for d in dist:
        lines.append(f"| {d['day']} | {d['posts']} | {d['comments']} |")
    return "\n".join(lines)


def _format_session_dist(sessions: list) -> str:
    """格式化时段聚合"""
    if not sessions:
        return "无数据"
    lines = ["| 时段 | 评论数 | 说明 |", "| --- | ---: | --- |"]
    descriptions = {
        "盘前": "预期表达，预测价值高",
        "上午盘": "实时反应，噪音大",
        "午间": "短暂讨论",
        "下午盘": "实时反应，噪音大",
        "盘后": "复盘总结，论据质量最高",
        "非交易时段": "非交易时间",
    }
    for s in sessions:
        desc = descriptions.get(s["session"], "")
        lines.append(f"| {s['session']} | {s['count']} | {desc} |")
    return "\n".join(lines)


def _format_kline(kline: list) -> str:
    """格式化K线数据"""
    if not kline:
        return "无K线数据"
    lines = ["| 日期 | 收盘 | 涨跌幅% | 成交量(手) | 成交额(千元) |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for k in kline[-30:]:  # 最多显示30条
        lines.append(f"| {k['trade_date']} | {k['close']:.2f} | {k['pct_chg']:.2f} | {k['vol']:.0f} | {k['amount']:.0f} |")
    return "\n".join(lines)


def _load_feedback(conn=None, symbol: Optional[str] = None, days: int = 90) -> str:
    """加载历史反馈。

    优先从SQLite analysis_feedback表查询（V4新增），
    保留JSON文件作为降级兼容。
    """
    entries = []
    cutoff = (datetime.now() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")

    # 优先从数据库加载
    if conn:
        try:
            rows = conn.execute("""
                SELECT field, original_value, corrected_value, reason, created_at
                FROM analysis_feedback
                WHERE (symbol = ? OR symbol IS NULL) AND created_at >= ?
                ORDER BY created_at DESC LIMIT 20
            """, (symbol, cutoff)).fetchall()
            for r in rows:
                entries.append({
                    "date": r["created_at"][:10] if r["created_at"] else "?",
                    "symbol": symbol or "?",
                    "original_judgment": f"{r['field']}={r['original_value']}",
                    "user_correction": f"{r['field']}={r['corrected_value']}",
                    "lesson": r["reason"] or "",
                })
        except Exception:
            pass  # 表可能还不存在，降级到文件

    # 降级：从JSON文件加载
    if not entries and os.path.exists(FEEDBACK_DIR):
        for fname in sorted(os.listdir(FEEDBACK_DIR)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(FEEDBACK_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    fb = json.load(f)
                if fb.get("date", "") < cutoff:
                    continue
                if symbol and fb.get("symbol") != symbol:
                    continue
                entries.append(fb)
            except (json.JSONDecodeError, KeyError):
                continue

    if not entries:
        return "无历史反馈记录。"

    lines = ["### 历史反馈经验（最近90天）\n"]
    for fb in entries[-20:]:
        lines.append(
            f"- [{fb.get('date', '?')}] {fb.get('symbol', '?')}: "
            f"原判断「{fb.get('original_judgment', '?')}」→ "
            f"纠正「{fb.get('user_correction', '?')}」— "
            f"经验: {fb.get('lesson', '?')}"
        )
    return "\n".join(lines)


def render_prompt(layer: str, data: dict, symbol: Optional[str] = None,
                  conn=None, max_tokens: int = 12000) -> str:
    """将数据包渲染为完整的分析 Prompt。

    V4增强:
    - 集成TokenBudget，按预算分配各section
    - 新增时段聚合占位符 {{SESSION_DISTRIBUTION}}
    - 反馈加载支持数据库+文件双源
    """
    template = load_template(layer)

    budget = TokenBudget(max_tokens=max_tokens)

    # 元信息（5%）
    meta = (
        f"股票: {data['symbol']} | 时间范围: {data['days']}天 | "
        f"线程数: {len(data.get('top_threads', []))} | "
        f"生成时间: {data['generated_at']}"
    )
    sections = {
        "{{META_INFO}}": budget.consume(meta, "meta"),

        # 概览（5%）
        "{{DATA_OVERVIEW}}": budget.consume(_format_overview(data["overview"]), "overview"),

        # 时段聚合（5%）— 仅在data中有时才渲染
        "{{SESSION_DISTRIBUTION}}": budget.consume(
            _format_session_dist(data.get("session_distribution", [])),
            "session"
        ),

        # K线数据（10%）
        "{{KLINE_DATA}}": budget.consume(
            _format_kline(data.get("kline")),
            "kline"
        ),

        # 用户画像（10%）
        "{{USER_SUMMARY}}": budget.consume(
            "\n".join(_format_user(u) for u in data.get("user_summary", [])),
            "users"
        ),

        # 反馈（5%）
        "{{FEEDBACK_CONTEXT}}": budget.consume(_load_feedback(conn, symbol), "feedback"),
    }

    # 线程（60%）— 最后消耗，获得剩余所有预算
    threads_text = "\n\n".join(
        _format_thread(t) for t in data.get("top_threads", [])
    )
    sections["{{TOP_THREADS}}"] = budget.consume(threads_text, "threads")

    for placeholder, content in sections.items():
        template = template.replace(placeholder, content)

    return template
