#!/usr/bin/env python3
"""
深度研判引擎

在alert_engine / baseline / llm_annotations 的统计结果之上，
再调一次LLM，让它像真正的研究员那样做综合研判。

与 llm_annotator 的区别：
  - annotator: 对单条评论做标注（情绪、论据），输入是单条文本
  - deep_briefing: 对一只票的全部信号做综合研判，输入是统计摘要+代表性评论

与 analyze_cmd 的 3-step chain 的区别：
  - 3-step chain: 通用的全量分析框架，适合深度报告
  - deep_briefing: 聚焦在"发现你不知道的东西"，适合每日简报

每只票一次API调用。只对有异常信号或评论量足够的票做深度研判。
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 个股研判 System Prompt —— 这是最重要的部分
STOCK_SYSTEM_PROMPT = """你是一个为个人A股投资者服务的舆情研究员。

你的工作不是重复数据——用户有SQL，不需要你告诉他"这只票有50条评论"。
你的工作是发现数据背后的含义——用户自己刷雪球花半小时也注意不到的东西。

你的价值标准：
1. 如果一条发现太显而易见（如"讨论量很多"），不值得写
2. 好的发现是"矛盾"——高质量用户和低质量用户方向不同说明什么？
3. 好的发现是"隐含信息"——某评论里暗示的数据或事件
4. 好的发现是"可验证判断"——评论中提到的具体价位/估值/时间节点
5. 好的发现是"行为模式"——某KOL连续3天跟踪同一只票意味着什么
6. 如果评论中有操纵嫌疑（低质量账号扎堆、内容相似），直接指出

你不替用户做买卖决策，只指出值得注意的信息和逻辑链。
输出必须是合法JSON。"""


STOCK_USER_TEMPLATE = """分析 {symbol} {stock_name} 的社区舆情。

【异常告警】
{alerts_text}

【评论统计（最近{days}天）】
- 总评论: {total_comments}条
- 高质量用户(粉丝>1000): {hq_count}人, 看多{hq_bull}人/看空{hq_bear}人
- 低质量用户(粉丝<100): {lq_count}人, 看多{lq_bull}人/看空{lq_bear}人
- LLM标注中被标记为可疑的评论: {flagged_count}条

【叙事主题变化】
{narrative_text}

【K线概要】
{kline_text}

【代表性评论】
{comments_text}

请输出JSON：
{{
  "key_findings": [
    {{
      "finding": "一句话描述",
      "why_matters": "为什么这个发现重要（2句话）",
      "evidence": "具体的数据/评论依据",
      "actionability": "对投资决策意味着什么"
    }}
  ],
  "quality_divergence": {{
    "exists": true/false,
    "description": "高质量用户和低质量用户的观点是否分歧，具体描述"
  }},
  "verifiable_claims": [
    {{
      "claim": "评论中提到的可验证判断（价位/事件/时间）",
      "source_user": "谁说的",
      "how_to_verify": "怎么验证",
      "deadline": "什么时候能验证"
    }}
  ],
  "narrative": {{
    "current": "当前讨论的主叙事",
    "is_shifting": true/false,
    "shift_from_to": "从X叙事变成Y叙事",
    "logic_strength": "强(有数据)/中(有逻辑无数据)/弱(纯想象)"
  }},
  "risk_flags": ["风险信号1", "风险信号2"],
  "bottom_line": "一句话结论：值不值得关注，为什么"
}}"""


# 跨票综合研判
CROSS_SYSTEM_PROMPT = """你是A股舆情研究员，擅长发现跨个股的共同主题和板块级别的趋势。

你只输出跨越个股的发现（单票内部的发现已经在个股研判中说过了）。
如果没有跨票发现，直接说"未发现显著跨票信号"。
输出合法JSON。"""

CROSS_USER_TEMPLATE = """以下是今日多只股票的个股研判摘要：

{stock_summaries}

板块热度数据：
{sector_text}

请找出跨越个股的共同趋势。输出JSON：
{{
  "cross_findings": [
    {{
      "finding": "发现描述",
      "involved_stocks": ["SH601600", "SH601020"],
      "detail": "展开说明",
      "significance": "为什么重要"
    }}
  ],
  "sector_rotation": {{
    "detected": true/false,
    "description": "板块轮动描述",
    "evidence": "基于什么证据"
  }},
  "market_narrative": "当前市场最主流的叙事（一句话）"
}}"""


def build_stock_context(conn: sqlite3.Connection, symbol: str,
                        alerts: list, days: int = 7) -> Optional[dict]:
    """
    收集一只票的全部上下文。
    满足以下条件之一才返回（否则None表示不需要深度研判）：
    1. 该票有触发的异常告警
    2. 最近N天评论>=10条
    3. 有KOL(粉丝>5000)参与
    """
    # 使用 created_at_str 做时间过滤（字符串比较，兼容现有数据）
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    stock_alerts = [a for a in alerts
                    if (a.get("symbol") == symbol or
                        (hasattr(a, "symbol") and a.symbol == symbol))]

    # 评论统计——联合LLM标注结果
    comments = conn.execute("""
        SELECT c.id, c.user_name, c.user_id, c.text_plain,
               c.like_count, c.created_at_str,
               COALESCE(up.followers_count, 0) AS followers,
               la.sentiment, la.sentiment_strength, la.argument_quality,
               la.keywords AS key_claim, la.sarcasm AS is_sarcastic,
               la.manipulation_flag
        FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        LEFT JOIN user_profiles up ON up.user_id = c.user_id
        LEFT JOIN llm_annotations la ON la.source_type = 'comment' AND la.source_id = c.id
        WHERE p.symbol = ? AND c.created_at_str >= ?
        ORDER BY COALESCE(c.like_count, 0) DESC""", (symbol, cutoff)).fetchall()

    comments = [dict(c) for c in comments]
    has_kol = any(c["followers"] >= 5000 for c in comments)

    if not stock_alerts and len(comments) < 10 and not has_kol:
        return None

    # 股票名
    name_row = conn.execute(
        "SELECT name FROM watched_stocks WHERE symbol = ?", (symbol,)
    ).fetchone()
    stock_name = dict(name_row)["name"] if name_row else symbol

    # 用户分层统计
    hq = [c for c in comments if c["followers"] >= 1000]
    lq = [c for c in comments if c["followers"] < 100]
    hq_bull = sum(1 for c in hq if c.get("sentiment") == "bullish")
    hq_bear = sum(1 for c in hq if c.get("sentiment") == "bearish")
    lq_bull = sum(1 for c in lq if c.get("sentiment") == "bullish")
    lq_bear = sum(1 for c in lq if c.get("sentiment") == "bearish")

    flagged = sum(1 for c in comments
                  if c.get("manipulation_flag") and c["manipulation_flag"] != 0
                  and c["manipulation_flag"] != "none")

    # 叙事主题变化
    narrative_rows = conn.execute("""
        SELECT summary_date, narrative_themes, dominant_sentiment, consensus_level
        FROM llm_batch_summaries
        WHERE symbol = ? AND summary_date >= ?
        ORDER BY summary_date DESC LIMIT 5
    """, (symbol, cutoff)).fetchall()

    narrative_history = []
    for row in narrative_rows:
        r = dict(row)
        try:
            themes = json.loads(r["narrative_themes"]) if r["narrative_themes"] else []
        except (json.JSONDecodeError, TypeError):
            themes = []
        narrative_history.append({
            "date": r["summary_date"],
            "themes": themes,
            "sentiment": r["dominant_sentiment"],
            "consensus": r["consensus_level"],
        })

    # K线概要
    kline_text = _get_kline_text(symbol)

    # 选择代表性评论
    representative = _select_representative(comments, max_count=15)

    return {
        "symbol": symbol,
        "stock_name": stock_name,
        "days": days,
        "alerts": stock_alerts,
        "total_comments": len(comments),
        "hq_count": len(hq), "hq_bull": hq_bull, "hq_bear": hq_bear,
        "lq_count": len(lq), "lq_bull": lq_bull, "lq_bear": lq_bear,
        "flagged_count": flagged,
        "narrative_history": narrative_history,
        "kline_text": kline_text,
        "representative": representative,
    }


def _select_representative(comments: list, max_count: int = 15) -> list:
    """
    选择有代表性的评论子集。
    策略：有key_claim的→KOL→看空→被标记的→高赞填充
    """
    selected = []
    ids = set()

    # 第1轮：有实质论据的
    for c in comments:
        if c.get("key_claim") and c["id"] not in ids:
            selected.append(c)
            ids.add(c["id"])
        if len(selected) >= max_count // 3:
            break

    # 第2轮：KOL
    for c in comments:
        if c["followers"] >= 5000 and c["id"] not in ids:
            selected.append(c)
            ids.add(c["id"])
        if len(selected) >= max_count * 2 // 3:
            break

    # 第3轮：看空观点（确保有反面）
    for c in comments:
        if c.get("sentiment") == "bearish" and c["id"] not in ids:
            selected.append(c)
            ids.add(c["id"])
        if len([s for s in selected if s.get("sentiment") == "bearish"]) >= 3:
            break

    # 第4轮：被标记为可疑的
    for c in comments:
        manipulation = c.get("manipulation_flag")
        if manipulation and manipulation != 0 and manipulation != "none" and c["id"] not in ids:
            selected.append(c)
            ids.add(c["id"])
            if len(selected) >= max_count - 2:
                break

    # 第5轮：按赞数填充
    for c in comments:
        if c["id"] not in ids:
            selected.append(c)
            ids.add(c["id"])
        if len(selected) >= max_count:
            break

    return selected


def _format_comments_for_prompt(comments: list) -> str:
    """格式化评论供LLM阅读"""
    lines = []
    for i, c in enumerate(comments, 1):
        tags = []
        if c["followers"] >= 5000:
            tags.append(f"KOL/{c['followers']}粉")
        elif c["followers"] < 100:
            tags.append("低粉")
        if c.get("sentiment"):
            tags.append(str(c["sentiment"]))
        if c.get("argument_quality") and c["argument_quality"] not in ("none", "medium"):
            tags.append(f"论据:{c['argument_quality']}")
        manipulation = c.get("manipulation_flag")
        if manipulation and manipulation != 0 and manipulation != "none":
            tags.append("⚠可疑")
        if c.get("is_sarcastic"):
            tags.append("反讽")

        tags_str = " | ".join(tags) if tags else ""
        text = (c.get("text_plain") or "")[:200].strip()
        if not text:
            continue
        likes = c.get("like_count", 0) or 0
        claim_str = f" → {c['key_claim']}" if c.get("key_claim") else ""

        lines.append(
            f"[{i}] @{c.get('user_name', '?')} [{tags_str}] (👍{likes})\n"
            f"    \"{text}\"{claim_str}"
        )
    return "\n".join(lines) if lines else "无代表性评论"


def _format_alerts_for_prompt(alerts: list) -> str:
    """格式化告警"""
    if not alerts:
        return "无异常告警"
    lines = []
    for a in alerts:
        if isinstance(a, dict):
            lines.append(f"- [{a.get('alert_type','?')}] {a.get('title','')}: {a.get('detail','')}")
        else:
            lines.append(f"- [{a.alert_type}] {a.title}: {a.detail}")
    return "\n".join(lines)


def _format_narrative(history: list) -> str:
    """格式化叙事变化"""
    if not history:
        return "无叙事数据（评论尚未标注或数据不足）"
    lines = []
    for h in history:
        themes_str = ", ".join(h["themes"][:3]) if h["themes"] else "无主题"
        lines.append(f"  {h['date']}: {themes_str} ({h['sentiment']},共识{h['consensus']})")
    return "\n".join(lines)


def _get_kline_text(symbol: str) -> str:
    """获取K线摘要文本"""
    pq_path = os.path.join(PROJECT_ROOT, "data", "kline", symbol, "daily.parquet")
    if not os.path.exists(pq_path):
        return "无K线数据"
    try:
        import pandas as pd
        df = pd.read_parquet(pq_path).sort_values("trade_date")
        recent = df.tail(10)
        if len(recent) < 2:
            return "K线数据不足"

        latest = recent.iloc[-1]
        change_5d = 0
        if len(recent) >= 5:
            change_5d = (recent.iloc[-1]["close"] - recent.iloc[-5]["close"]) / recent.iloc[-5]["close"] * 100

        prices = [f"{r['close']:.2f}" for _, r in recent.tail(5).iterrows()]

        return (f"最新收盘: {latest['close']:.2f}, "
                f"5日涨跌: {change_5d:+.2f}%, "
                f"近5日收盘: {' → '.join(prices)}")
    except Exception:
        return "K线读取失败"


def generate_stock_briefing(llm_client, context: dict) -> Optional[dict]:
    """对单只票生成深度研判"""
    user_prompt = STOCK_USER_TEMPLATE.format(
        symbol=context["symbol"],
        stock_name=context["stock_name"],
        days=context["days"],
        alerts_text=_format_alerts_for_prompt(context["alerts"]),
        total_comments=context["total_comments"],
        hq_count=context["hq_count"],
        hq_bull=context["hq_bull"],
        hq_bear=context["hq_bear"],
        lq_count=context["lq_count"],
        lq_bull=context["lq_bull"],
        lq_bear=context["lq_bear"],
        flagged_count=context["flagged_count"],
        narrative_text=_format_narrative(context["narrative_history"]),
        kline_text=context["kline_text"],
        comments_text=_format_comments_for_prompt(context["representative"]),
    )

    return llm_client.annotate(STOCK_SYSTEM_PROMPT, user_prompt, max_tokens=2000)


def generate_cross_briefing(llm_client, stock_briefings: Dict[str, dict],
                            sector_heat: dict) -> Optional[dict]:
    """跨票综合研判"""
    if len(stock_briefings) < 2:
        return None

    summaries = []
    for symbol, briefing in stock_briefings.items():
        if not briefing:
            continue
        summaries.append({
            "symbol": symbol,
            "bottom_line": briefing.get("bottom_line", ""),
            "narrative": briefing.get("narrative", {}).get("current", ""),
            "findings": [f.get("finding", "") for f in briefing.get("key_findings", [])],
        })

    sector_text = json.dumps(sector_heat, ensure_ascii=False, indent=2) if sector_heat else "无板块数据"

    user_prompt = CROSS_USER_TEMPLATE.format(
        stock_summaries=json.dumps(summaries, ensure_ascii=False, indent=2),
        sector_text=sector_text,
    )

    return llm_client.annotate(CROSS_SYSTEM_PROMPT, user_prompt, max_tokens=1500)


def run_deep_briefing(conn: sqlite3.Connection, profile: dict, config: dict, alerts: list,
                      sector_heat: dict = None) -> dict:
    """
    主入口：生成全部深度研判。

    Returns:
        {
            "stock_briefings": {"SH601600": {...}, ...},
            "cross_stock": {...},
            "usage": {...}
        }
    """
    from analysis.llm_client import get_annotator_client
    from analysis.config_manager import get_api_key, merge_with_system_config

    merged_config = merge_with_system_config(profile, config)

    # 使用briefing_model而非annotate_model
    briefing_model = profile.get("llm", {}).get("briefing_model", "deepseek-chat")
    merged_config["llm"]["model"] = briefing_model

    llm = get_annotator_client(merged_config)
    if not llm:
        print("  ❌ LLM客户端初始化失败")
        return {"stock_briefings": {}, "cross_stock": None, "usage": {}}

    # 确定范围
    scope = profile.get("scope", {})
    exclude = set(scope.get("exclude_symbols", []))
    min_threshold = scope.get("min_comments_threshold", 10)
    max_stocks = profile.get("pipeline", {}).get("max_deep_briefing_stocks", 10)

    days = profile.get("time", {}).get("scan_days", 7)

    if scope.get("mode") == "custom" and scope.get("custom_symbols"):
        symbols = scope["custom_symbols"]
    else:
        symbols = [dict(r)["symbol"] for r in conn.execute(
            "SELECT symbol FROM watched_stocks WHERE is_active = 1"
        ).fetchall()]

    symbols = [s for s in symbols if s not in exclude]

    # 筛选需要深度研判的票
    stock_briefings = {}
    briefing_count = 0
    t_briefing_start = time.time()

    for symbol in symbols:
        if briefing_count >= max_stocks:
            break

        context = build_stock_context(conn, symbol, alerts, days=days)
        if context is None:
            continue

        stock_name = context["stock_name"]
        briefing_count += 1

        # 进度条
        _print_briefing_progress(briefing_count, max_stocks, t_briefing_start,
                                 f"{stock_name}({symbol})...")

        briefing = generate_stock_briefing(llm, context)
        if briefing:
            stock_briefings[symbol] = briefing
            findings_count = len(briefing.get("key_findings", []))
            _print_briefing_done(f"✅ ({findings_count}个发现)")
        else:
            _print_briefing_done("❌")

        time.sleep(1)

    # 跨票研判
    cross_stock = None
    run_cross = profile.get("pipeline", {}).get("run_cross_stock", True)
    if run_cross and len(stock_briefings) >= 2:
        print("  🌐 跨票综合研判...", end=" ", flush=True)
        cross_stock = generate_cross_briefing(llm, stock_briefings, sector_heat)
        print("✅" if cross_stock else "❌")

    briefing_elapsed = time.time() - t_briefing_start
    print(f"  深度研判耗时: {briefing_elapsed:.1f}s")

    return {
        "stock_briefings": stock_briefings,
        "cross_stock": cross_stock,
        "usage": llm.get_cost_summary(),
    }


def _print_briefing_progress(current: int, max_stocks: int, t_start: float, label: str = ""):
    """刷新深度研判进度条（不换行）。"""
    bar_width = 15
    pct = min(current / max_stocks, 1.0) if max_stocks > 0 else 0
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)

    elapsed = time.time() - t_start
    eta_str = ""
    if current > 0:
        avg_per = elapsed / current
        # 估算剩余（假设还有 max_stocks - current 个要处理）
        remaining_approx = (max_stocks - current) * avg_per * 0.7  # 打7折，有些票会被跳过
        eta_str = f" ETA ~{remaining_approx:.0f}s"

    suffix = f" {label}" if label else ""
    sys.stdout.write(
        f"\r   🔬 研判 [{bar}] {current}/{max_stocks} {elapsed:.1f}s{eta_str}{suffix}"
    )
    sys.stdout.flush()


def _print_briefing_done(result: str = ""):
    """在进度条同一行追加结果并换行。"""
    sys.stdout.write(f"{result}\n")
    sys.stdout.flush()
