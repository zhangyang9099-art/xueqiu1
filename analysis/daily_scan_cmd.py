#!/usr/bin/env python3
"""
daily-scan 命令 — 每日异常扫描（增强版）

工作流程：
  0. 可选：自动 LLM 标注今日评论
  1. 更新基准线（每周自动）
  2. 更新 KOL 评级（每周自动）
  3. 回填信号价格（验证历史信号）
  4. 异常检测（9种检测 + 板块轮动）
  5. 规律推票（规则引擎）
  6. 高价值内容筛选（TOP-5）
  7. 生成每日简报

用法：
  python main.py daily-scan                    # 全量扫描
  python main.py daily-scan --symbol SH600519  # 单股扫描
  python main.py daily-scan --no-annotate       # 跳过自动标注
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _get_active_symbols(conn: sqlite3.Connection) -> list:
    """获取有近期帖子的所有 symbol"""
    # 注意: posts.created_at 存的是 "秒级时间戳 × 1000"
    cutoff = (int(datetime.now().timestamp()) - 3 * 86400) * 1000
    return [r[0] for r in conn.execute("""
        SELECT DISTINCT p.symbol
        FROM posts p
        WHERE p.created_at >= ?
        ORDER BY p.symbol
    """, (cutoff,)).fetchall()]


def _should_update_weekly(conn: sqlite3.Connection) -> bool:
    """判断是否需要执行每周更新（距上次>=7天）"""
    row = conn.execute("""
        SELECT MAX(computed_at) FROM stock_baselines
    """).fetchone()
    if not row or not row[0]:
        return True
    try:
        last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last).days >= 7
    except ValueError:
        return True


def _generate_full_report(alerts, recommendations, sector_heat,
                          top_content, signal_scorecard, symbols) -> str:
    """生成完整每日简报"""
    lines = [f"# 每日扫描报告\n\n生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    lines.append(f"扫描股票: {len(symbols)} 只\n")

    # 信号准确率
    if signal_scorecard:
        lines.append("## 📊 信号历史准确率\n")
        lines.append("| 信号类型 | 总数 | 正确 | 错误 | 准确率 | 平均10日收益 |")
        lines.append("|---|---|---|---|---|---|")
        for s in signal_scorecard:
            acc = f"{s['accuracy']:.0%}" if s['accuracy'] is not None else "N/A"
            ret = f"{s['avg_return_10d']:.2%}" if s['avg_return_10d'] is not None else "N/A"
            lines.append(f"| {s['signal_type']} | {s['total']} | {s['correct']} | {s['wrong']} | {acc} | {ret} |")
        lines.append("")

    # 板块热度
    if sector_heat:
        lines.append("## 🔥 板块热度\n")
        sorted_sectors = sorted(sector_heat.items(), key=lambda x: x[1]["heat_index"], reverse=True)
        for sector, data in sorted_sectors[:10]:
            icon = "🔥" if data["heat_index"] > 1.5 else ("🌡" if data["heat_index"] > 0.8 else "❄️")
            lines.append(f"- {icon} **{sector}**: 热度 {data['heat_index']} | "
                         f"情绪 {data['dominant_sentiment']} | "
                         f"近7天 {data['total_recent_comments']} 条评论")
        lines.append("")

    # 告警
    if alerts:
        lines.append(f"## 🚨 异常告警 ({len(alerts)} 条)\n")
        for a in alerts:
            severity_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(a.get("severity", "low"), "⚪")
            lines.append(f"- {severity_icon} **{a.get('symbol', '')}** [{a.get('alert_type', '')}] {a.get('title', '')}")
            if a.get("suggestion"):
                lines.append(f"  - 💡 {a['suggestion']}")
        lines.append("")
    else:
        lines.append("## ✅ 无异常告警\n")

    # 推票建议
    if recommendations:
        lines.append(f"## 🎯 规律推票 ({len(recommendations)} 条)\n")
        for r in recommendations:
            conf_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(r.get("confidence", "low"), "⚪")
            lines.append(f"- {conf_icon} **{r['symbol']}** [{r['rule_name']}] → {r['action']}")
            lines.append(f"  - {r.get('action_detail', '')}")
        lines.append("")

    # 高价值内容
    if top_content:
        lines.append("## 💎 今日精选内容 (TOP-5)\n")
        for i, item in enumerate(top_content, 1):
            grade_badge = f"[{item['grade']}]" if item['grade'] != 'C' else ""
            lines.append(f"### {i}. {item['stock_name']} ({item['symbol']}) — {grade_badge} @{item['user_name']}")
            lines.append(f"- ⭐ 评分: {item['score']}/100 | 情绪: {item['sentiment'] or 'N/A'} | 👍 {item['likes']}")
            if item.get('accuracy_rate'):
                lines.append(f"- 历史准确率: {item['accuracy_rate']:.0%}")
            content = (item.get('content') or '')[:200]
            lines.append(f"- > {content}")
            lines.append("")

    return "\n".join(lines)


def run_daily_scan(config: dict, symbol: str = None, skip_annotate: bool = False):
    """执行每日扫描主流程"""
    from analysis.baseline import compute_baselines, save_baseline, get_baseline, get_today_stats, update_all_baselines
    from analysis.alert_engine import run_all_detections, save_alerts

    db_path = config.get("database", {}).get("sqlite_path", "data/xueqiu.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # === Step 0: 可选 — 自动标注 ===
    if not skip_annotate:
        from analysis.llm_annotator import count_unannotated
        unannotated = count_unannotated(conn, symbol, days=1)
        if unannotated > 0:
            print(f"\n📝 发现 {unannotated} 条未标注评论（今日），正在自动标注...")
            from analysis.llm_annotator import run_annotate
            run_annotate(config, symbol=symbol, days=1)
        else:
            print("\n📝 今日评论已全部标注")

    # === Step 1: 更新基准线（每周一次）===
    if _should_update_weekly(conn):
        print("\n📊 更新基准线（每周一次）...")
        if symbol:
            bl = compute_baselines(conn, symbol)
            if bl:
                save_baseline(conn, bl)
                print(f"  ✓ {symbol} 基准线已更新")
        else:
            result = update_all_baselines(conn)
            print(f"  ✓ 基准线更新: {result['updated']} 更新, {result['skipped']} 跳过")
    else:
        print("\n📊 基准线近期已更新，跳过")

    # === Step 2: 更新 KOL 评级（每周一次，同基准线节奏）===
    if _should_update_weekly(conn):
        print("\n👤 更新KOL评级...")
        try:
            from analysis.kol_tracker import update_kol_ratings
            update_kol_ratings(conn, config)
        except Exception as e:
            print(f"  ⚠ KOL评级更新失败: {e}")

    # === Step 3: 回填信号价格 ===
    print("\n📋 回填信号价格...")
    try:
        from analysis.signal_ledger import backfill_signal_prices
        backfill_signal_prices(conn)
    except Exception as e:
        print(f"  ⚠ 信号回填失败: {e}")

    # === Step 4: 确定扫描范围 ===
    if symbol:
        symbols = [symbol]
    else:
        symbols = _get_active_symbols(conn)

    # 过滤有足够数据的
    valid = []
    for sym in symbols:
        cnt = conn.execute("""
            SELECT COUNT(*) FROM comments c
            JOIN comment_memberships m ON m.comment_id = c.id
            JOIN posts p ON p.id = m.post_id
            WHERE p.symbol = ?
        """, (sym,)).fetchone()[0]
        if cnt >= 10:
            valid.append(sym)

    print(f"\n🔍 开始异常扫描 ({len(valid)} 只股票)...")

    # === Step 5: 异常检测 ===
    all_alerts = []
    all_reports = []

    for sym in valid:
        baseline = get_baseline(conn, sym)
        today = get_today_stats(conn, sym)

        if today["posts"] == 0 and today["comments"] == 0:
            continue

        alerts = run_all_detections(conn, sym, today, baseline) if baseline else []

        if alerts:
            save_alerts(conn, alerts)
            # 记录信号
            try:
                from analysis.signal_ledger import record_signal
                for a in alerts:
                    record_signal(conn, {
                        "alert_type": a.alert_type,
                        "symbol": sym,
                        "title": a.title,
                        "severity": a.severity,
                        "suggestion": a.suggestion,
                        "data": a.data if hasattr(a, 'data') else {},
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
            except Exception:
                pass

            all_alerts.extend(alerts)

        # 单股报告
        report_lines = [f"\n{'=' * 50}"]
        report_lines.append(f"  {sym} — {today['date']}")
        report_lines.append(f"{'=' * 50}")
        report_lines.append(f"  帖子: {today['posts']}  评论: {today['comments']}  "
                            f"活跃用户: {today['active_users']}  热度: {today['heat_score']:.0f}")
        if alerts:
            for a in alerts:
                severity_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(a.severity, "⚪")
                report_lines.append(f"  {severity_icon} [{a.alert_type}] {a.title}")
        else:
            report_lines.append(f"  ✅ 无异常")
        all_reports.append("\n".join(report_lines))

    # === Step 6: 板块分析 ===
    sector_heat = {}
    try:
        from analysis.sector_analysis import compute_sector_heat, detect_sector_migration
        sector_heat = compute_sector_heat(conn, days=7)
        sector_alerts = detect_sector_migration(conn)
        if sector_alerts:
            all_alerts.extend(sector_alerts)
            # 板块告警不记录信号（无对应K线）
    except Exception as e:
        print(f"  ⚠ 板块分析失败: {e}")

    # === Step 7: 规律推票 ===
    recommendations = []
    try:
        from analysis.recommend_engine import scan_recommendations
        recommendations = scan_recommendations(conn, valid)
        if recommendations:
            print(f"  🎯 发现 {len(recommendations)} 条推票建议")
    except Exception as e:
        print(f"  ⚠ 推票引擎失败: {e}")

    # === Step 8: 高价值内容筛选 ===
    top_content = []
    try:
        from analysis.content_curator import curate_daily_top
        top_content = curate_daily_top(conn, top_n=5, days=1, symbol_filter=symbol)
        if top_content:
            print(f"  💎 筛选出 {len(top_content)} 条高价值内容")
    except Exception as e:
        print(f"  ⚠ 内容筛选失败: {e}")

    # === Step 9: 信号准确率 ===
    signal_scorecard = []
    try:
        from analysis.signal_ledger import get_signal_scorecard
        signal_scorecard = get_signal_scorecard(conn)
    except Exception:
        pass

    # === 汇总输出 ===
    print(f"\n{'=' * 50}")
    print(f"扫描完成")
    print(f"  扫描股票: {len(valid)}")
    print(f"  发现告警: {len(all_alerts)}")
    print(f"  推票建议: {len(recommendations)}")
    print(f"  精选内容: {len(top_content)}")

    for report in all_reports:
        print(report)

    # 保存完整报告
    output_dir = os.path.join(PROJECT_ROOT, "data", "analysis-reports")
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = os.path.join(output_dir, f"{ts}_daily_scan.md")

    full_report = _generate_full_report(
        all_alerts, recommendations, sector_heat,
        top_content, signal_scorecard, valid
    )

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(full_report)
    print(f"\n📄 完整报告已保存: {report_file}")

    conn.close()


def cmd_daily_scan(args, config):
    """daily-scan 命令处理函数"""
    symbol = args.symbol.upper() if args.symbol else None
    skip_annotate = args.no_annotate
    run_daily_scan(config, symbol=symbol, skip_annotate=skip_annotate)


def setup_daily_scan_parser(subparsers):
    """注册 daily-scan 子命令"""
    p = subparsers.add_parser("daily-scan", help="每日异常扫描（全链路：标注→检测→推票→简报）")
    p.add_argument("--symbol", default=None, help="只扫描某只股票")
    p.add_argument("--no-annotate", action="store_true", help="跳过自动标注步骤")
    return p
