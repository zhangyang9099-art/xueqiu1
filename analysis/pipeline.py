#!/usr/bin/env python3
"""
统一分析管线

日常只需运行: python main.py analyze-pipeline

自动完成：
  1. 检查/创建配置（首次运行交互式设置）
  2. 标注未标注的评论（LLM annotate）
  3. 更新基准线和KOL评级（如果过期）
  4. 回填信号价格（验证历史信号）
  5. 异常检测（9种+ 板块轮动）
  6. 规律推票（规则引擎）
  7. 高价值内容筛选
  8. 深度研判（LLM综合分析）
  9. 格式化输出到终端
  10. 保存报告文件

整个过程大约3-5分钟（取决于需要标注的评论数量和深度研判的股票数）。
"""

import os
import sqlite3
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def run_pipeline(config: dict, force_config: bool = False,
                 symbol: str = None, skip_annotate: bool = False,
                 skip_briefing: bool = False):
    """
    主管线入口。

    Args:
        config: config.yaml 的字典
        force_config: True=强制重新配置
        symbol: 只分析某只票（None=全部）
        skip_annotate: 跳过LLM标注步骤
        skip_briefing: 跳过深度研判步骤
    """
    t_start = time.time()

    # ══════ Step 0: 配置 ══════
    from analysis.config_manager import ensure_profile, merge_with_system_config
    print("⚙️  检查运行配置...")
    profile = ensure_profile(force_interactive=force_config)
    merged_config = merge_with_system_config(profile, config)

    # ══════ Step 1: 数据库连接 ══════
    db_path = merged_config.get("database", {}).get("sqlite_path", "data/xueqiu.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)

    if not os.path.exists(db_path):
        print(f"❌ 数据库不存在: {db_path}")
        print("   请先运行爬虫: python main.py scrape --all")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    pipeline_cfg = profile.get("pipeline", {})
    time_cfg = profile.get("time", {})
    scan_days = time_cfg.get("scan_days", 7)

    try:
        # ══════ Step 2: 自动标注 ══════
        if not skip_annotate and pipeline_cfg.get("auto_annotate", True):
            from analysis.llm_annotator import count_unannotated, run_annotate
            unannotated = count_unannotated(conn, symbol, days=scan_days)
            if unannotated > 0:
                print(f"\n📝 标注 {unannotated} 条未分析评论...")
                run_annotate(merged_config, symbol=symbol, days=scan_days, conn=conn)
            else:
                print(f"\n📝 评论已全部标注 ✓")
        else:
            print(f"\n📝 跳过标注步骤")

        # ══════ Step 3: 基准线 + KOL ══════
        from analysis.baseline import update_all_baselines, get_baseline, get_today_stats
        update_interval = time_cfg.get("baseline_update_interval", 7)
        baseline_period = time_cfg.get("baseline_period", 30)

        should_update = _check_baseline_freshness(conn, update_interval)
        if should_update and pipeline_cfg.get("auto_baseline", True):
            print(f"\n📊 更新基准线...")
            result = update_all_baselines(conn, baseline_period)
            print(f"   {result['updated']}只更新, {result['skipped']}只跳过")

            if pipeline_cfg.get("auto_kol_update", True):
                print(f"👤 更新KOL评级...")
                try:
                    from analysis.kol_tracker import update_kol_ratings
                    update_kol_ratings(conn, merged_config)
                except Exception as e:
                    print(f"   ⚠ KOL更新失败: {e}")

        # ══════ Step 4: 信号回填 ══════
        try:
            from analysis.signal_ledger import backfill_signal_prices
            backfill_signal_prices(conn)
        except Exception as e:
            print(f"   ⚠ 信号回填失败: {e}")

        # ══════ Step 5: 确定扫描范围 ══════
        scope = profile.get("scope", {})
        if symbol:
            symbols = [symbol.upper()]
        elif scope.get("mode") == "custom" and scope.get("custom_symbols"):
            symbols = scope["custom_symbols"]
        else:
            symbols = [dict(r)["symbol"] for r in conn.execute(
                "SELECT symbol FROM watched_stocks WHERE is_active = 1"
            ).fetchall()]

        exclude = set(scope.get("exclude_symbols", []))
        symbols = [s for s in symbols if s not in exclude]

        # 过滤有数据的
        min_comments = scope.get("min_comments_threshold", 10)
        valid = []
        for sym in symbols:
            cnt = conn.execute("""
                SELECT COUNT(*) FROM comments c
                JOIN comment_memberships m ON m.comment_id = c.id
                JOIN posts p ON p.id = m.post_id
                WHERE p.symbol = ?""", (sym,)).fetchone()[0]
            if cnt >= min_comments:
                valid.append(sym)

        print(f"\n🔍 扫描 {len(valid)} 只股票...")

        # ══════ Step 6: 异常检测 ══════
        from analysis.alert_engine import run_all_detections, save_alerts, Alert

        all_alerts = []
        total_valid = len(valid)
        print(f"   进度条: [0/{total_valid}]", end="", flush=True)
        for i, sym in enumerate(valid, 1):
            baseline = get_baseline(conn, sym)
            today_stats = get_today_stats(conn, sym)

            if today_stats["posts"] == 0 and today_stats["comments"] == 0:
                _print_progress(i, total_valid, t_start, start_step=time.time())
                continue

            if baseline:
                alerts = run_all_detections(conn, sym, today_stats, baseline)
                if alerts:
                    save_alerts(conn, alerts)
                    all_alerts.extend(alerts)

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
                                "data": {},
                                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            })
                    except Exception:
                        pass

            _print_progress(i, total_valid, t_start, start_step=time.time())

        # ══════ Step 7: 板块分析 ══════
        sector_heat = {}
        sector_alerts_list = []
        try:
            from analysis.sector_analysis import compute_sector_heat, detect_sector_migration
            sector_heat = compute_sector_heat(conn, days=scan_days)
            sector_alerts_list = detect_sector_migration(conn)
        except Exception as e:
            print(f"   ⚠ 板块分析失败: {e}")

        # 合并板块告警
        combined_alerts = []
        for a in all_alerts:
            combined_alerts.append(a.to_dict() if isinstance(a, Alert) else a)
        combined_alerts.extend(sector_alerts_list)

        # ══════ Step 8: 推票引擎 ══════
        recommendations = []
        try:
            from analysis.recommend_engine import scan_recommendations
            recommendations = scan_recommendations(conn, valid)
        except Exception as e:
            print(f"   ⚠ 推票引擎失败: {e}")

        # ══════ Step 9: 高价值内容 ══════
        top_content = []
        try:
            from analysis.content_curator import curate_daily_top
            top_content = curate_daily_top(conn, top_n=5, days=1, symbol_filter=symbol)
        except Exception as e:
            print(f"   ⚠ 内容筛选失败: {e}")

        # ══════ Step 10: 深度研判 ══════
        briefing_result = {"stock_briefings": {}, "cross_stock": None, "usage": {}}
        if not skip_briefing and pipeline_cfg.get("run_deep_briefing", True):
            print(f"\n🔬 深度研判...")
            try:
                from analysis.deep_briefing import run_deep_briefing
                briefing_result = run_deep_briefing(
                    conn, profile, config,
                    combined_alerts, sector_heat
                )
            except Exception as e:
                print(f"   ❌ 深度研判失败: {e}")
                import traceback
                traceback.print_exc()

        # ══════ Step 11: 信号准确率 ══════
        signal_scorecard = []
        try:
            from analysis.signal_ledger import get_signal_scorecard
            signal_scorecard = get_signal_scorecard(conn)
        except Exception:
            pass

        # ══════ Step 12: 输出 ══════
        from analysis.output_formatter import format_full_output, save_report_file

        output_text = format_full_output(
            alerts=combined_alerts,
            recommendations=recommendations,
            briefing_result=briefing_result,
            top_content=top_content,
            sector_heat=sector_heat,
            signal_scorecard=signal_scorecard,
            scanned_count=len(valid),
            profile=profile,
            conn=conn,
        )

        # 终端输出
        print("\n")
        print(output_text)

        # 保存文件
        report_path = save_report_file(output_text, profile)
        if report_path:
            print(f"\n📄 报告已保存: {report_path}")

        # 耗时
        elapsed = time.time() - t_start
        print(f"⏱️  总耗时: {elapsed:.1f}秒")

    finally:
        conn.close()


def _check_baseline_freshness(conn, interval_days: int) -> bool:
    """检查基准线是否需要更新"""
    try:
        row = conn.execute("SELECT MAX(computed_at) FROM stock_baselines").fetchone()
        if not row or not row[0]:
            return True
        last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last).days >= interval_days
    except Exception:
        return True


def _print_progress(current: int, total: int, t_start: float,
                    start_step: float = None, label: str = ""):
    """在当前行刷新进度条（不换行），最后一项自动换行。
    
    Args:
        current: 当前序号（1-based）
        total: 总数
        t_start: 管线/步骤开始时间
        start_step: 当前步骤开始时间（用于估算剩余）
        label: 可选后缀标签
    """
    if total <= 0:
        return
    pct = current / total
    bar_width = 20
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)

    elapsed = time.time() - (start_step or t_start)
    eta_str = ""
    if current > 0 and current < total:
        avg_per = elapsed / current
        remaining = (total - current) * avg_per
        eta_str = f" ETA {remaining:.0f}s"

    total_elapsed = time.time() - t_start
    suffix = f" {label}" if label else ""
    tail = "  \n" if current == total else "  "

    sys.stdout.write(
        f"\r   [{bar}] {current}/{total} ({pct:.0%})"
        f" {elapsed:.1f}s{eta_str}{suffix}"
    )
    sys.stdout.flush()


def setup_analyze_pipeline_parser(subparsers):
    """注册 analyze-pipeline 子命令"""
    p = subparsers.add_parser("analyze-pipeline",
                              help="运行每日舆情分析（一键完成全部流程）")
    p.add_argument("--symbol", default=None, help="只分析某只股票")
    p.add_argument("--config", action="store_true", help="重新配置运行参数")
    p.add_argument("--no-annotate", action="store_true", help="跳过LLM标注")
    p.add_argument("--no-briefing", action="store_true", help="跳过深度研判")
    return p


def cmd_analyze_pipeline(args, config):
    """analyze-pipeline 命令处理函数"""
    run_pipeline(
        config=config,
        force_config=args.config,
        symbol=args.symbol.upper() if args.symbol else None,
        skip_annotate=args.no_annotate,
        skip_briefing=args.no_briefing,
    )
