#!/usr/bin/env python3
"""
annotate 命令 — LLM 批量标注入口

用法:
  python main.py annotate                    # 标注所有未标注评论
  python main.py annotate --symbol SH600519  # 只标注某只票
  python main.py annotate --days 3           # 只标注最近3天的
  python main.py annotate --dry-run          # 只统计不标注
"""

import argparse
import yaml
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def cmd_annotate(args, config):
    """annotate 命令处理函数"""
    from analysis.llm_annotator import run_annotate

    symbol = args.symbol.upper() if args.symbol else None
    days = args.days
    dry_run = args.dry_run

    run_annotate(config, symbol=symbol, days=days, dry_run=dry_run)


def setup_annotate_parser(subparsers):
    """注册 annotate 子命令"""
    p = subparsers.add_parser("annotate", help="LLM批量标注评论（替代关键词匹配）")
    p.add_argument("--symbol", default=None, help="股票代码（如 SH600519），不指定则标注全部")
    p.add_argument("--days", type=int, default=None, help="只标注最近N天的评论")
    p.add_argument("--dry-run", action="store_true", help="只统计不标注，查看将要处理的批次")
    return p
