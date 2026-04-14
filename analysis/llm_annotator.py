#!/usr/bin/env python3
"""
LLM 批量标注引擎 v1

替代关键词匹配的 sentiment.py，用 LLM 对评论做结构化分析。

工作流程：
  1. 查找所有未标注的评论（llm_annotations 表中没有对应记录的）
  2. 按 (symbol, date) 分组
  3. 每组构造一个 LLM prompt，一次性分析该组所有评论
  4. 解析 LLM 返回的 JSON，逐条写入 llm_annotations
  5. 批次摘要写入 llm_batch_summaries

成本控制：
  - 每组最多30条评论（超过的截断，优先保留高互动的）
  - 每条评论文本最多截取前200字
  - 使用 response_format=json_object 保证 JSON 输出
  - 预估成本：43只票 × 5条/天 ≈ 43次API调用/天 ≈ ¥0.3/天

用法：
  python main.py annotate                # 标注所有未标注评论
  python main.py annotate --symbol SH600519  # 只标注某只票
  python main.py annotate --days 3           # 只标注最近3天的
  python main.py annotate --dry-run          # 只统计不标注
"""

import json
import os
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# LLM Prompt 模板
# ============================================================

SYSTEM_PROMPT = """你是一个专业的A股舆情分析师。你的任务是分析雪球平台上的股票评论，判断每条评论的情绪方向、论证类型和可信度。

重要规则：
1. 判断情绪方向时要注意反讽和阴阳怪气。A股散户经常用反话，比如"这票真是太好了，好到我都亏了50%"实际上是bearish。
2. "加仓"、"满仓"、"梭哈"这些词在亏损语境下可能是绝望而非看多。要看上下文。
3. 纯粹的数据搬运（如"今日收盘价xx"）标记为neutral。
4. 如果评论太短（如"哈哈"、"顶"）无法判断，标记为neutral+低强度。
5. argument_type说明：
   - fundamental：提到营收/利润/估值/行业数据等
   - technical：提到K线/均线/形态/量价等
   - narrative：讲故事/行业趋势/赛道/政策方向等
   - emotion：纯情绪表达，无实质论据
   - news：转述消息/公告/传闻
   - none：无法归类

你必须严格输出JSON格式。"""


def build_batch_prompt(symbol: str, stock_name: str, date: str,
                       comments: List[dict]) -> str:
    """构造一个批次的 user prompt。"""
    comment_lines = []
    for c in comments:
        follower_tag = ""
        followers = c.get("followers", 0) or 0
        if followers >= 10000:
            follower_tag = f"（{followers}粉丝，大V）"
        elif followers >= 1000:
            follower_tag = f"（{followers}粉丝）"

        like_tag = f" [+{c['likes']}赞]" if c.get("likes", 0) > 0 else ""
        comment_lines.append(
            f"[{c['index']}] {c['user_name']}{follower_tag}: "
            f"\"{c['text']}\"{like_tag}"
        )

    comments_text = "\n".join(comment_lines)

    return f"""以下是雪球平台上关于 {symbol} {stock_name} 的 {len(comments)} 条评论，发布日期：{date}。

请对每条评论进行分析，并给出整体总结。

评论列表：
{comments_text}

请以JSON格式输出，格式如下：
{{"comments": [
    {{
      "index": 1,
      "sentiment": "bullish 或 bearish 或 neutral 或 mixed",
      "strength": 1到5的整数（1=轻微, 5=强烈）,
      "argument_type": "fundamental 或 technical 或 narrative 或 emotion 或 news 或 none",
      "key_claim": "一句话概括核心主张，无实质内容则为null",
      "is_sarcastic": false,
      "intent": "express 或 guide 或 reverse_indicator",
      "manipulation_flag": "none 或 template 或 coordinated"
    }}
  ],
  "batch_summary": {{
    "bullish_count": 0,
    "bearish_count": 0,
    "neutral_count": 0,
    "mixed_count": 0,
    "dominant_sentiment": "bullish 或 bearish 或 neutral 或 mixed",
    "consensus_level": 0.5,
    "narrative_themes": ["主题1", "主题2"],
    "notable_arguments": ["值得注意的论据1"],
    "manipulation_risk": "low 或 medium 或 high",
    "overall_quality": "high 或 medium 或 low"
  }}
}}

字段说明：
- intent: express=真实表达, guide=引导洗盘/散户, reverse_indicator=反向指标
- manipulation_flag: none=正常, template=模板化评论, coordinated=疑似协调行为
- consensus_level: 0.5=看多看空各半, 1.0=完全一致, 计算=max(bull,bear)/(bull+bear)
- strength: 1=轻微提及, 2=一般观点, 3=明确表态, 4=强烈情绪, 5=极端言论（含辱骂/威胁等）"""


# ============================================================
# 数据查询
# ============================================================

def get_unannotated_comments(conn: sqlite3.Connection,
                             symbol: Optional[str] = None,
                             days: Optional[int] = None,
                             limit_per_batch: int = 30) -> Dict[str, List[dict]]:
    """查找所有未标注的评论，按 (symbol, date) 分组。

    Returns:
        {"SH600519|2026-03-30": [{comment_id, user_name, text_plain, ...}, ...], ...}
    """
    conditions = ["la.id IS NULL"]
    params = []

    if symbol:
        conditions.append("p.symbol = ?")
        params.append(symbol)

    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conditions.append("c.created_at_str >= ?")
        params.append(cutoff)

    # 只标注有实际文本内容的评论
    conditions.append("COALESCE(c.text_plain, '') != ''")

    where_clause = " AND ".join(conditions)

    rows = conn.execute(f"""
        SELECT c.id AS comment_id,
               c.user_id, c.user_name, c.text_plain,
               c.like_count, c.created_at_str,
               p.symbol,
               COALESCE(up.followers_count, 0) AS followers
        FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        LEFT JOIN user_profiles up ON up.user_id = c.user_id
        LEFT JOIN llm_annotations la ON la.source_type = 'comment' AND la.source_id = c.id
        WHERE {where_clause}
        ORDER BY p.symbol, substr(c.created_at_str, 1, 10),
                 COALESCE(c.like_count, 0) DESC
    """, params).fetchall()

    # 按 (symbol, date) 分组
    groups = defaultdict(list)
    for row in rows:
        date_str = (row["created_at_str"] or "")[:10]
        if not date_str:
            continue
        key = f"{row['symbol']}|{date_str}"
        groups[key].append(dict(row))

    # 每组限制最多 limit_per_batch 条（保留高互动的）
    for key in groups:
        if len(groups[key]) > limit_per_batch:
            groups[key] = groups[key][:limit_per_batch]

    return dict(groups)


def get_stock_name(conn: sqlite3.Connection, symbol: str) -> str:
    """从 watched_stocks 获取股票名"""
    row = conn.execute(
        "SELECT name FROM watched_stocks WHERE symbol = ?", (symbol,)
    ).fetchone()
    return dict(row)["name"] if row else symbol


def count_unannotated(conn: sqlite3.Connection,
                      symbol: Optional[str] = None,
                      days: Optional[int] = None) -> int:
    """统计未标注的评论数量"""
    conditions = ["la.id IS NULL", "COALESCE(c.text_plain, '') != ''"]
    params = []

    if symbol:
        conditions.append("p.symbol = ?")
        params.append(symbol)
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conditions.append("c.created_at_str >= ?")
        params.append(cutoff)

    where_clause = " AND ".join(conditions)
    row = conn.execute(f"""
        SELECT COUNT(*) AS cnt
        FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        LEFT JOIN llm_annotations la ON la.source_type = 'comment' AND la.source_id = c.id
        WHERE {where_clause}
    """, params).fetchone()
    return dict(row)["cnt"]


# ============================================================
# 标注执行
# ============================================================

def annotate_batch(llm_client, conn_write: sqlite3.Connection,
                   symbol: str, date_str: str,
                   comments: List[dict],
                   stock_name: str) -> bool:
    """对一个 (symbol, date) 批次调用 LLM 标注。

    Returns:
        True=成功, False=失败
    """
    batch_id = str(uuid.uuid4())[:8]

    # 构造 prompt 中的评论列表
    prompt_comments = []
    for i, c in enumerate(comments):
        text = (c["text_plain"] or "")[:200].strip()
        if not text:
            continue
        prompt_comments.append({
            "index": i + 1,
            "user_name": c["user_name"] or "匿名",
            "followers": c.get("followers", 0),
            "text": text,
            "likes": c.get("like_count", 0) or 0,
        })

    if not prompt_comments:
        return True  # 无有效评论，跳过

    # 调 LLM
    user_prompt = build_batch_prompt(symbol, stock_name, date_str, prompt_comments)
    result = llm_client.annotate(SYSTEM_PROMPT, user_prompt, max_tokens=3000)

    if not result:
        return False

    # 解析并存储逐条标注
    llm_comments = result.get("comments", [])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 建立 index → original comment 映射
    # prompt_comments 中的 index 从 1 开始
    index_to_comment = {}
    for c in prompt_comments:
        index_to_comment[c["index"]] = c

    inserted = 0
    for llm_c in llm_comments:
        idx = llm_c.get("index", 0)
        if idx not in index_to_comment:
            continue

        # 找到原始 comment（在 comments 列表中按 index 对应）
        orig_idx = idx - 1
        if orig_idx < 0 or orig_idx >= len(comments):
            continue

        original = comments[orig_idx]

        try:
            conn_write.execute("""
                INSERT OR IGNORE INTO llm_annotations
                (source_type, source_id, symbol,
                 sentiment, sentiment_strength,
                 intent, sarcasm, manipulation_flag,
                 argument_quality, keywords, narrative_tag,
                 annotated_at, batch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "comment",
                original["comment_id"],
                symbol,
                llm_c.get("sentiment", "neutral"),
                llm_c.get("strength", 0),
                llm_c.get("intent", "express"),
                1 if llm_c.get("is_sarcastic") else 0,
                llm_c.get("manipulation_flag", "none"),
                llm_c.get("argument_type", "none"),
                llm_c.get("key_claim"),
                None,  # narrative_tag 留给后续板块分析
                now_str,
                batch_id,
            ))
            inserted += 1
        except Exception:
            pass  # 静默处理，避免冲散进度条

    # 存储批次摘要
    summary = result.get("batch_summary", {})
    try:
        conn_write.execute("""
            INSERT OR REPLACE INTO llm_batch_summaries
            (symbol, summary_date,
             post_count, comment_count,
             bullish_count, bearish_count, neutral_count, mixed_count,
             dominant_sentiment, consensus_level,
             narrative_themes, notable_arguments,
             manipulation_risk, overall_quality,
             model_used, batch_id, annotated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            date_str,
            0,  # post_count 后续可补充
            len(prompt_comments),
            summary.get("bullish_count", 0),
            summary.get("bearish_count", 0),
            summary.get("neutral_count", 0),
            summary.get("mixed_count", 0),
            summary.get("dominant_sentiment"),
            summary.get("consensus_level", 0.5),
            json.dumps(summary.get("narrative_themes", []), ensure_ascii=False),
            json.dumps(summary.get("notable_arguments", []), ensure_ascii=False),
            summary.get("manipulation_risk", "low"),
            summary.get("overall_quality", "medium"),
            llm_client.model,
            batch_id,
            now_str,
        ))
    except Exception:
        pass  # 静默处理

    # 不在此处 commit，由调用方统一控制事务
    return True


# ============================================================
# 主流程
# ============================================================

def run_annotate(config: dict, symbol: Optional[str] = None,
                 days: Optional[int] = None, dry_run: bool = False,
                 conn: sqlite3.Connection = None):
    """执行批量标注主流程。

    Args:
        conn: 外部传入的数据库连接（pipeline复用时共享连接，避免锁冲突）。
              若为None则自行创建。
    """
    from analysis.llm_client import get_annotator_client

    owns_conn = conn is None
    if owns_conn:
        db_path = config.get("database", {}).get("sqlite_path", "data/xueqiu.db")
        if not os.path.isabs(db_path):
            db_path = os.path.join(PROJECT_ROOT, db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

    # 初始化 LLM 客户端
    llm = get_annotator_client(config)
    if not llm and not dry_run:
        print("[错误] 未配置 LLM API。请在 config.yaml 的 llm 段设置 api_key。")
        if owns_conn:
            conn.close()
        return

    # 查找未标注评论
    print("正在查找未标注的评论...")
    total_unannotated = count_unannotated(conn, symbol, days)
    print(f"  未标注评论总数: {total_unannotated}")

    if total_unannotated == 0:
        print("所有评论已标注完毕 ✓")
        if owns_conn:
            conn.close()
        return

    groups = get_unannotated_comments(conn, symbol, days)
    batch_count = len(groups)
    print(f"  分组数量: {batch_count} 个 (symbol|date)")

    if dry_run:
        print("\n[干跑模式] 以下是将要标注的批次：")
        for key, items in sorted(groups.items()):
            sym, date = key.split("|", 1)
            name = get_stock_name(conn, sym)
            print(f"  {sym} {name} {date}: {len(items)} 条评论")
        print(f"\n合计 {batch_count} 个批次, 约 {batch_count} 次 API 调用")
        if owns_conn:
            conn.close()
        return

    # 逐批标注
    success_count = 0
    fail_count = 0
    t_annotate_start = time.time()

    print(f"\n开始批量标注 ({batch_count} 个批次)...")
    for i, (key, comments) in enumerate(sorted(groups.items()), 1):
        parts = key.split("|", 1)
        sym = parts[0]
        date_str = parts[1] if len(parts) > 1 else ""
        stock_name = get_stock_name(conn, sym)

        # 进度条
        _print_annotate_progress(i, batch_count, t_annotate_start,
                                 f"{sym} {stock_name} {date_str} ({len(comments)}条)")

        ok = annotate_batch(llm, conn, sym, date_str, comments, stock_name)
        if ok:
            success_count += 1
        else:
            fail_count += 1

        # 批次间间隔1秒，避免限流
        if i < batch_count:
            time.sleep(1)

    # 统一提交所有标注结果
    try:
        conn.commit()
    except Exception as e:
        print(f"\n  ⚠ 批量提交失败: {e}")

    # 汇总
    elapsed = time.time() - t_annotate_start
    cost = llm.get_cost_summary()
    print(f"\n{'=' * 50}")
    print(f"标注完成: {success_count} 成功, {fail_count} 失败 ({elapsed:.1f}s)")
    print(f"API调用: {cost['total_calls']} 次")
    print(f"Token用量: {cost['total_input_tokens']} input + {cost['total_output_tokens']} output = {cost['total_tokens']} total")
    print(f"模型: {cost['provider']}/{cost['model']}")
    print(f"{'=' * 50}")

    if owns_conn:
        conn.close()


def _print_annotate_progress(current: int, total: int, t_start: float, label: str = ""):
    """在当前行刷新标注进度条（不换行），最后一项自动换行。"""
    if total <= 0:
        return
    pct = current / total
    bar_width = 20
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)

    elapsed = time.time() - t_start
    eta_str = ""
    if current > 0 and current < total:
        avg_per = elapsed / current
        remaining = (total - current) * avg_per
        eta_str = f" ETA {remaining:.0f}s"

    suffix = f" {label}" if label else ""
    tail = "  \n" if current == total else "  "

    sys.stdout.write(
        f"\r   [{bar}] {current}/{total} ({pct:.0%})"
        f" {elapsed:.1f}s{eta_str}{suffix}"
    )
    sys.stdout.flush()
