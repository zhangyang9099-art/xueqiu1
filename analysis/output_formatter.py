#!/usr/bin/env python3
"""
终端输出格式化器

生成信息密度高、逻辑清晰、重点突出的纯文本输出。
未来可直接发送到Telegram/飞书。

设计原则：
  1. 第一屏就能看到最重要的信息
  2. 用缩进和符号标记层级，不用Markdown语法
  3. 每个发现都带"为什么重要"
  4. 控制总长度——宁可省略低优先级信息也不要刷屏
"""

import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def format_full_output(alerts: list, recommendations: list,
                       briefing_result: dict, top_content: list,
                       sector_heat: dict, signal_scorecard: list,
                       scanned_count: int, profile: dict,
                       conn: sqlite3.Connection) -> str:
    """生成完整的终端输出"""
    width = profile.get("output", {}).get("terminal_width", 80)
    top_n_findings = profile.get("output", {}).get("top_n_findings", 5)
    top_n_content = profile.get("output", {}).get("top_n_content", 3)
    show_cost = profile.get("output", {}).get("show_cost", True)

    lines = []
    sep = "━" * width

    # ====== 头部 ======
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(sep)
    lines.append(f"  舆情研判简报  {now}  覆盖{scanned_count}只")
    lines.append(sep)
    lines.append("")

    # ====== 第一部分：今日必看 ======
    stock_briefings = briefing_result.get("stock_briefings", {})
    if stock_briefings:
        all_findings = []
        for symbol, briefing in stock_briefings.items():
            name = _get_stock_name(conn, symbol)
            for finding in briefing.get("key_findings", []):
                all_findings.append({
                    "symbol": symbol, "name": name,
                    "finding": finding
                })

        if all_findings:
            lines.append(f"🔥 今日必看")
            lines.append("")
            for i, item in enumerate(all_findings[:top_n_findings], 1):
                f = item["finding"]
                lines.append(f"  {i}. {item['name']}({item['symbol']})")
                lines.append(f"     {f.get('finding', '')}")
                if f.get("why_matters"):
                    lines.append(f"     → {f.get('why_matters', '')}")
                if f.get("evidence"):
                    lines.append(f"     证据: {f.get('evidence', '')}")
                if f.get("actionability"):
                    lines.append(f"     ⚡ {f.get('actionability', '')}")
                lines.append("")
            lines.append(sep)
            lines.append("")

    # ====== 第二部分：异常信号 ======
    high_alerts = [a for a in alerts
                   if (a.get("severity") == "high" if isinstance(a, dict)
                       else a.severity == "high")]
    if high_alerts:
        lines.append(f"⚠️  高风险信号 ({len(high_alerts)}条)")
        lines.append("")
        for a in high_alerts:
            title = a.get("title", a.title) if isinstance(a, dict) else a.title
            detail = a.get("detail", a.detail) if isinstance(a, dict) else a.detail
            suggestion = a.get("suggestion", a.suggestion) if isinstance(a, dict) else a.suggestion
            lines.append(f"  🔴 {title}")
            lines.append(f"     {detail}")
            if suggestion:
                lines.append(f"     💡 {suggestion}")
            lines.append("")
        lines.append(sep)
        lines.append("")

    med_low = [a for a in alerts
               if (a.get("severity") != "high" if isinstance(a, dict)
                   else a.severity != "high")]
    if med_low:
        lines.append(f"另有{len(med_low)}条中/低风险信号")
        lines.append("")

    # ====== 第三部分：个股研判详情 ======
    if stock_briefings:
        lines.append(f"🔬 个股研判")
        lines.append("")
        for symbol, briefing in stock_briefings.items():
            name = _get_stock_name(conn, symbol)

            # 底线判断
            bottom = briefing.get("bottom_line", "")
            lines.append(f"  ■ {name}({symbol})")
            if bottom:
                lines.append(f"    结论: {bottom}")

            # 用户分歧
            div = briefing.get("quality_divergence", {})
            if div.get("exists"):
                lines.append(f"    ⚡ 用户分歧: {div.get('description', '')}")

            # 可验证判断
            claims = briefing.get("verifiable_claims", [])
            if claims:
                lines.append(f"    📌 可验证判断:")
                for cl in claims[:2]:
                    lines.append(f"      • {cl.get('claim', '')} (@{cl.get('source_user', '?')})")

            # 叙事
            nar = briefing.get("narrative", {})
            if nar.get("is_shifting"):
                lines.append(f"    🔄 叙事变化: {nar.get('shift_from_to', '')}")

            # 风险
            risks = briefing.get("risk_flags", [])
            if risks:
                for r in risks[:2]:
                    lines.append(f"    ⚠ {r}")

            lines.append("")
        lines.append(sep)
        lines.append("")

    # ====== 第四部分：跨票研判 ======
    cross = briefing_result.get("cross_stock")
    if cross:
        findings = cross.get("cross_findings", [])
        rotation = cross.get("sector_rotation", {})
        narrative = cross.get("market_narrative", "")

        if findings or rotation.get("detected") or narrative:
            lines.append(f"🌐 跨票视角")
            lines.append("")

            for f in findings:
                stocks = ", ".join(f.get("involved_stocks", []))
                lines.append(f"  • {f.get('finding', '')} ({stocks})")
                if f.get("significance"):
                    lines.append(f"    → {f.get('significance', '')}")
                lines.append("")

            if rotation.get("detected"):
                lines.append(f"  🔄 板块轮动: {rotation.get('description', '')}")
                lines.append("")

            if narrative:
                lines.append(f"  📊 市场主叙事: {narrative}")
                lines.append("")

            lines.append(sep)
            lines.append("")

    # ====== 第五部分：推荐阅读 ======
    if top_content:
        lines.append(f"📖 推荐阅读")
        lines.append("")
        for i, item in enumerate(top_content[:top_n_content], 1):
            grade = item.get("grade", "")
            grade_tag = f"[{grade}级]" if grade in ("A", "B") else ""
            acc = f"准确率{item['accuracy_rate']:.0%}" if item.get("accuracy_rate") else ""
            lines.append(f"  {i}. [{item['stock_name']}] @{item['user_name']} {grade_tag}{acc}")
            content = (item.get("content") or "")[:150]
            lines.append(f"     \"{content}\"")
            lines.append(f"     👍{item.get('likes', 0)} | {item.get('argument_quality', '?')}")
            lines.append("")
        lines.append(sep)
        lines.append("")

    # ====== 第六部分：推票信号 ======
    if recommendations:
        lines.append(f"📋 推票信号 ({len(recommendations)})")
        lines.append("")
        for rec in recommendations:
            name = _get_stock_name(conn, rec["symbol"])
            conf = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(rec.get("confidence"), "⚪")
            lines.append(f"  {conf} {name}({rec['symbol']}) — {rec['rule_name']} [{rec['action']}]")
            if rec.get("action_detail"):
                lines.append(f"     {rec['action_detail']}")
        lines.append("")
        lines.append(sep)
        lines.append("")

    # ====== 第七部分：板块热度 ======
    if sector_heat:
        sorted_sectors = sorted(sector_heat.items(),
                                key=lambda x: x[1]["heat_index"], reverse=True)
        lines.append(f"🌡️  板块热度")
        for sector, data in sorted_sectors[:6]:
            icon = "🔥" if data["heat_index"] > 2 else "❄️" if data["heat_index"] < 0.5 else "  "
            lines.append(f"  {icon} {sector}: {data['heat_index']:.1f} ({data['dominant_sentiment']})")
        lines.append("")
        lines.append(sep)
        lines.append("")

    # ====== 第八部分：信号记分卡 ======
    verified = [s for s in signal_scorecard if s.get("accuracy") is not None]
    if verified:
        lines.append(f"📊 信号准确率")
        for s in verified[:5]:
            lines.append(f"  {s['signal_type']}: {s['accuracy']:.0%} ({s['total']}次)")
        lines.append("")

    # ====== 第九部分：成本 ======
    if show_cost:
        usage = briefing_result.get("usage", {})
        if usage.get("total_calls"):
            lines.append(f"💰 API: {usage['total_calls']}次调用, "
                         f"{usage.get('total_tokens', 0)} tokens, "
                         f"{usage.get('model', '?')}")
            lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def save_report_file(output_text: str, profile: dict) -> Optional[str]:
    """保存报告到文件"""
    output_cfg = profile.get("output", {})
    if not output_cfg.get("save_report", True):
        return None

    report_dir = output_cfg.get("report_dir", "data/daily-reports")
    if not os.path.isabs(report_dir):
        report_dir = os.path.join(PROJECT_ROOT, report_dir)
    os.makedirs(report_dir, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(report_dir, f"{today}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(output_text)
    return path


def _get_stock_name(conn, symbol):
    row = conn.execute(
        "SELECT name FROM watched_stocks WHERE symbol = ?", (symbol,)
    ).fetchone()
    return dict(row)["name"] if row else symbol
