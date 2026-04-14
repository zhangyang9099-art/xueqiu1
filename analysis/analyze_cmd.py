#!/usr/bin/env python3
"""
analyze 命令 v2 — 舆情分析入口

v2 变更 (V4优化计划):
  - 新增 --output auto 模式（自动调用LLM API执行三步Chain）
  - 新增 --resume-from 参数（Chain中断恢复）
  - 缓存中间结果到 data/analysis-cache/
  - 集成TokenBudget管理
  - 集成analysis_feedback表反馈加载

用法:
  python main.py analyze                             # 全部活跃股票，最近7天，完整分析
  python main.py analyze --symbol SH600519           # 指定股票
  python main.py analyze --symbol SH600519 --output auto   # 自动执行LLM分析
  python main.py analyze --symbol SH600519 --output report  # 保存数据包
  python main.py analyze --symbol SH600519 --resume-from step2  # 从step2恢复
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

CACHE_DIR = os.path.join(PROJECT_ROOT, "data", "analysis-cache")


def _cache_step(symbol: str, step: str, data):
    """缓存Chain中间结果"""
    cache_dir = os.path.join(CACHE_DIR, symbol or "ALL")
    os.makedirs(cache_dir, exist_ok=True)
    filepath = os.path.join(cache_dir, f"{step}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return filepath


def _load_cached_step(symbol: str, step: str):
    """加载缓存的中间结果"""
    filepath = os.path.join(CACHE_DIR, symbol or "ALL", f"{step}.json")
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def _render_chain_step(step_num: int, data: dict, symbol: str,
                       step1_result=None, step2_result=None) -> str:
    """渲染Chain某一步的Prompt"""
    from analysis.prompt_engine import _format_overview, _format_user, _format_kline
    from analysis.prompt_engine import _format_session_dist, _format_time_dist
    from analysis.prompt_engine import _load_feedback
    from analysis.token_budget import TokenBudget
    from analysis.schemas import compress_step1_for_step2

    PROMPTS_DIR = os.path.join(PROJECT_ROOT, "analysis", "prompts")

    if step_num == 1:
        template_path = os.path.join(PROMPTS_DIR, "step1-extract.md")
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()

        budget = TokenBudget(max_tokens=12000)
        sections = {
            "{{TOP_THREADS}}": budget.consume(
                "\n\n".join(_format_thread_for_chain(t) for t in data.get("top_threads", [])),
                "threads"
            ),
            "{{USER_SUMMARY}}": budget.consume(
                "\n".join(_format_user(u) for u in data.get("user_summary", [])),
                "users"
            ),
            "{{FEEDBACK_CONTEXT}}": budget.consume("无历史反馈记录。", "feedback"),
        }
        for k, v in sections.items():
            template = template.replace(k, v)
        return template

    elif step_num == 2:
        template_path = os.path.join(PROMPTS_DIR, "step2-judge.md")
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()

        compressed = compress_step1_for_step2(step1_result)
        sections = {
            "{{STEP1_COMPRESSED}}": json.dumps(compressed, ensure_ascii=False, indent=2),
            "{{KLINE_DATA}}": _format_kline(data.get("kline")),
            "{{SESSION_DISTRIBUTION}}": _format_session_dist(data.get("session_distribution", [])),
            "{{TIME_DISTRIBUTION}}": _format_time_dist(data.get("time_distribution", [])),
        }
        for k, v in sections.items():
            template = template.replace(k, v)
        return template

    elif step_num == 3:
        template_path = os.path.join(PROMPTS_DIR, "step3-report.md")
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()

        meta = (
            f"股票: {data['symbol']} | 时间范围: {data['days']}天 | "
            f"生成时间: {data['generated_at']}"
        )
        sections = {
            "{{META_INFO}}": meta,
            "{{STEP1_RESULT}}": json.dumps(step1_result, ensure_ascii=False, indent=2),
            "{{STEP2_RESULT}}": json.dumps(step2_result, ensure_ascii=False, indent=2),
            "{{DATA_OVERVIEW}}": _format_overview(data["overview"]),
            "{{USER_SUMMARY}}": "\n".join(_format_user(u) for u in data.get("user_summary", [])),
            "{{FEEDBACK_CONTEXT}}": "无历史反馈记录。",
        }
        for k, v in sections.items():
            template = template.replace(k, v)
        return template


def _format_thread_for_chain(thread: dict) -> str:
    """为Chain Step1格式化线程（简化版，不包含TokenBudget）"""
    heat = thread.get("heat_score", 0)
    deviation = thread.get("heat_deviation_pct")
    if deviation is not None and abs(deviation) > 50:
        heat_str = f"{heat:.0f}（偏离度{deviation:+.0f}%）"
    else:
        heat_str = f"{heat:.0f}"

    lines = [
        f"### 线程 #{thread['thread_id'][:8]}  热度: {heat_str}",
        f"- 作者: {thread['author']} ({thread['author_id']})",
        f"- 时间: {thread['start_time']}",
        f"- 互动: 赞{thread['like_count']} 评{thread.get('actual_comments', 0)} "
        f"参与者{thread.get('participants', 0)}",
        f"- 内容: {(thread.get('content') or '')[:500]}",
    ]

    if thread.get("comments"):
        from analysis.prompt_engine import smart_select_comments
        selected = smart_select_comments(
            thread["comments"], 50,
            author_id=thread.get("author_id", "")
        )
        lines.append(f"- 评论({len(selected)}/{len(thread['comments'])}条):")
        for c in selected:
            indent = "  " * (c.get("depth", 1) - 1)
            reply_to = f" → @{c.get('reply_to_user_name', '')}" if c.get("reply_to_user_name") else ""
            text = (c.get("text_plain") or "")[:200]
            like_badge = f" [+{c['like_count']}赞]" if (c.get("like_count") or 0) >= 3 else ""
            lines.append(f"  {indent}[{c.get('user_name', '?')}{reply_to}]{like_badge} {text}")

    return "\n".join(lines)


def _run_chain(data: dict, symbol: str, conn, resume_from: str = None):
    """执行三步Chain，支持中断恢复和降级输出"""
    from analysis.llm_client import get_llm_client, LLMError, TokenBudgetExceeded
    from analysis.schemas import validate_step_output, STEP1_SCHEMA, STEP2_SCHEMA

    llm = get_llm_client(conn) if False else get_llm_client({})
    if llm is None:
        # 尝试从config.yaml读取
        import yaml
        config_path = os.path.join(PROJECT_ROOT, "config.yaml")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f)
            llm = get_llm_client(cfg)
        if llm is None:
            print("[错误] 未配置LLM API。请在 config.yaml 中添加 llm 配置段：")
            print("""
llm:
  provider: "deepseek"
  api_key: "your-api-key"
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  max_tokens: 8000
  timeout_seconds: 120
""")
            return None

    sym_key = symbol or "ALL"
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Step 1: 感知层
    if resume_from != "step2" and resume_from != "step3":
        print("\n📌 Step 1/3: 感知层（提取情绪、意图、反话）...")
        step1_prompt = _render_chain_step(1, data, symbol)
        try:
            step1_result = llm.call(step1_prompt, "step1", expect_json=True)
            ok, errors = validate_step_output(step1_result, STEP1_SCHEMA)
            if not ok:
                print(f"  ⚠ Step1校验失败: {errors}")
                print("  正在重试...")
                step1_result = llm.call(
                    f"上次输出校验失败: {errors}\n请修正JSON格式。", "step1", expect_json=True
                )
            cache_path = _cache_step(sym_key, "step1", step1_result)
            print(f"  ✓ Step1完成，已缓存: {cache_path}")
            print(f"    整体情绪: {step1_result.get('overall_sentiment', {})}")
        except (LLMError, TokenBudgetExceeded) as e:
            print(f"  ✗ Step1失败: {e}")
            return None
    else:
        print("\n📌 跳过Step1，从缓存加载...")
        step1_result = _load_cached_step(sym_key, "step1")
        if not step1_result:
            print("[错误] 未找到step1缓存结果，请从头运行")
            return None
        print(f"  ✓ 已加载step1缓存")

    # Step 2: 判断层
    if resume_from != "step3":
        print("\n📌 Step 2/3: 判断层（舆情-价格联动）...")
        step2_prompt = _render_chain_step(2, data, symbol,
                                          step1_result=step1_result)
        try:
            step2_result = llm.call(step2_prompt, "step2", expect_json=True)
            ok, errors = validate_step_output(step2_result, STEP2_SCHEMA)
            if not ok:
                print(f"  ⚠ Step2校验警告: {errors}")
            cache_path = _cache_step(sym_key, "step2", step2_result)
            print(f"  ✓ Step2完成，已缓存: {cache_path}")
            risk = step2_result.get("manipulation_risk_score", "?")
            alignment = step2_result.get("price_sentiment_alignment", "?")
            print(f"    操纵风险: {risk}/100, 情绪对齐: {alignment}")
        except (LLMError, TokenBudgetExceeded) as e:
            print(f"  ⚠ Step2失败: {e}")
            print("  降级为 Step1-only 报告...")
            return _render_degraded_report(step1_result, data)
    else:
        print("\n📌 跳过Step2，从缓存加载...")
        step2_result = _load_cached_step(sym_key, "step2")
        if not step2_result:
            print("  ⚠ 未找到step2缓存，降级为Step1-only报告")
            return _render_degraded_report(step1_result, data)
        print(f"  ✓ 已加载step2缓存")

    # Step 3: 输出层
    print("\n📌 Step 3/3: 输出层（生成最终报告）...")
    step3_prompt = _render_chain_step(3, data, symbol,
                                      step1_result=step1_result,
                                      step2_result=step2_result)
    try:
        final_report = llm.call_streaming(step3_prompt, "step3")
        return final_report
    except (LLMError, TokenBudgetExceeded) as e:
        print(f"  ⚠ Step3失败: {e}")
        print("  直接从Step1+Step2数据组装报告...")
        return _render_from_structured(step1_result, step2_result, data)


def _render_degraded_report(step1_result: dict, data: dict) -> str:
    """降级报告：只有Step1结果时直接组装"""
    overall = step1_result.get("overall_sentiment", {})
    lines = [
        f"# {data['symbol']} 舆情分析报告（降级版 — 仅情绪提取）",
        f"## 生成时间: {data['generated_at']}",
        "",
        "## 整体情绪",
        f"- 情绪: {overall.get('label', '?')}",
        f"- 强度: {overall.get('strength', '?')}",
        f"- 置信度: {overall.get('confidence', '?')}",
        "",
        "## 线程分析",
    ]
    for t in step1_result.get("threads", []):
        lines.append(f"- **{t['thread_id'][:8]}**: {t.get('sentiment', '?')} "
                     f"(强度{t.get('strength', '?')}, 意图{t.get('intent', '?')}, "
                     f"反话{t.get('sarcasm', False)})")
        if t.get("key_argument"):
            lines.append(f"  - 论据: {t['key_argument']}")
        if t.get("suspicious_users"):
            lines.append(f"  - 可疑用户: {', '.join(t['suspicious_users'][:5])}")

    lines.append("")
    lines.append("⚠ 注意：此为降级报告，Step2（联动分析）和Step3（完整报告）未能完成。")
    lines.append("可使用 --resume-from step2 重新尝试。")
    return "\n".join(lines)


def _render_from_structured(step1_result: dict, step2_result: dict,
                            data: dict) -> str:
    """从结构化数据直接组装报告（Step3失败时降级）"""
    overall = step1_result.get("overall_sentiment", {})
    lines = [
        f"# {data['symbol']} 舆情分析报告",
        f"## 生成时间: {data['generated_at']}",
        "",
        "## 执行摘要",
        f"- 整体情绪: {overall.get('label', '?')} (强度{overall.get('strength', '?')})",
        f"- 置信度: {overall.get('confidence', '?')}",
        f"- 操纵风险: {step2_result.get('manipulation_risk_score', '?')}/100",
        f"- 情绪-价格对齐: {step2_result.get('price_sentiment_alignment', '?')}",
        "",
        "## Step2摘要",
        step2_result.get("summary", "无"),
    ]
    return "\n".join(lines)


def cmd_analyze(args, config):
    """analyze 命令处理函数"""
    from analysis.data_query import build_analysis_data, get_db_path
    from analysis.prompt_engine import render_prompt

    analysis_cfg = config.get("analysis", {})
    db_path = get_db_path(config)

    # 解析参数
    symbol = args.symbol.upper() if args.symbol else None
    days = args.days or analysis_cfg.get("default_days", 7)
    layer = args.layer or "full"
    top_n = args.top_n or analysis_cfg.get("default_top_n", 20)
    output = args.output or "prompt"
    output_dir = os.path.join(PROJECT_ROOT, analysis_cfg.get("output_dir", "data/analysis-reports"))
    min_depth = analysis_cfg.get("min_comment_depth", 3)
    resume_from = getattr(args, "resume_from", None)

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    # 连接数据库（auto模式需要写权限用于反馈表）
    if not os.path.exists(db_path):
        print(f"[错误] 数据库不存在: {db_path}")
        print("请先运行爬取命令收集数据。")
        return

    if output == "auto":
        conn = sqlite3.connect(db_path)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # 构建数据包
        print(f"正在准备分析数据...")
        print(f"  股票: {symbol or '全部活跃股票'}")
        print(f"  时间范围: 最近{days}天")
        print(f"  分析层级: {layer}")
        print(f"  热度TOP: {top_n}线程")

        data = build_analysis_data(conn, config, symbol, days, top_n, min_depth)

        overview = data["overview"]
        print(f"\n数据概览:")
        print(f"  帖子: {overview['total_posts']}  评论: {overview['total_comments']}")
        print(f"  完备率: {overview['comment_completion_rate']}")
        print(f"  时间跨度: {overview['earliest_post'] or '无'} ~ {overview['latest_post'] or '无'}")
        print(f"  涉及股票: {overview['stock_count']}只")

        if overview["total_posts"] == 0:
            print("\n⚠ 指定时间范围内无数据，请扩大 --days 范围或检查数据库。")
            return

        if output == "auto":
            # ──── Auto模式：执行三步Chain ────
            if layer != "full":
                print(f"\n⚠ auto模式目前仅支持full层级，已自动切换。")

            report = _run_chain(data, symbol, conn, resume_from)

            if report:
                # 保存报告
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                sym_part = (symbol or "ALL").replace("/", "-")
                filename = f"{ts}_{sym_part}_auto.md"
                filepath = os.path.join(output_dir, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(report)
                print(f"\n✓ 分析报告已保存: {filepath}")
            else:
                print("\n✗ 分析未能完成。")

        elif output == "prompt":
            # 渲染 Prompt（传入conn用于从analysis_feedback表加载反馈）
            prompt = render_prompt(layer, data, symbol, conn=conn)

            print("\n" + "=" * 60)
            print("  复制以下内容到对话中即可开始分析")
            print("=" * 60 + "\n")
            print(prompt)
            print("\n" + "=" * 60)
            print(f"提示: python main.py analyze --symbol {symbol or 'SH600519'} --output auto 自动执行分析")

        else:
            # report 模式
            prompt = render_prompt(layer, data, symbol, conn=conn)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            sym_part = (symbol or "ALL").replace("/", "-")
            filename = f"{ts}_{sym_part}_{layer}.md"
            filepath = os.path.join(output_dir, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(prompt)
            print(f"\n✓ 分析数据包已保存: {filepath}")
            print(f"  请将此文件内容提供给 LLM Agent 执行分析。")

        conn.close()

    except Exception as e:
        print(f"[错误] 分析失败: {e}")
        import traceback
        traceback.print_exc()
        conn.close()


def setup_analyze_parser(subparsers):
    """注册 analyze 子命令的参数解析器"""
    p = subparsers.add_parser("analyze", help="舆情分析（准备数据 + 生成分析Prompt）")
    p.add_argument("--symbol", default=None, help="股票代码（如 SH600519），不指定则分析全部")
    p.add_argument("--days", type=int, default=None, help="分析最近N天的数据（默认7）")
    p.add_argument("--layer", default=None,
                   choices=["full", "sentiment", "heat", "manipulation", "credibility", "price"],
                   help="分析层级（默认 full 完整六层分析）")
    p.add_argument("--top-n", type=int, default=None, help="取热度最高的N个线程（默认20）")
    p.add_argument("--output", default=None,
                   choices=["prompt", "report", "auto"],
                   help="输出方式: prompt=终端输出, report=保存文件, auto=自动LLM分析（默认prompt）")
    p.add_argument("--resume-from", default=None,
                   choices=["step2", "step3"],
                   help="从缓存的指定步骤恢复Chain执行")
    return p
