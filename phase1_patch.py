#!/usr/bin/env python3
"""
阶段一补丁脚本 — 修改 main.py, api_endpoints.py, config.yaml

用法:
  cd ~/Desktop/xueqiu-scraper
  source venv/bin/activate
  python phase1_patch.py

前置条件（先手动完成）:
  1. 已将 time_utils.py 放到 utils/ 目录
  2. 已将 database.py 替换 storage/database.py
  3. 已将 stock_comments.py 替换 scrapers/stock_comments.py
"""

import os
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{filepath}.bak_{ts}"
        shutil.copy2(filepath, bak)
        print(f"  备份: {os.path.basename(filepath)} → {os.path.basename(bak)}")


def patch_file(filepath, replacements):
    """对文件执行一系列字符串替换。"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    changed = False
    for old, new, desc in replacements:
        if old in content:
            content = content.replace(old, new)
            print(f"  ✓ {desc}")
            changed = True
        else:
            print(f"  · 跳过: {desc}（已修改或不匹配）")

    if changed:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)


def append_to_file(filepath, marker, code, desc):
    """在文件末尾（marker 之前或文件末尾）追加代码。"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if code.strip().split('\n')[1].strip() in content:
        print(f"  · 跳过: {desc}（已存在）")
        return

    if marker and marker in content:
        content = content.replace(marker, code + "\n" + marker)
    else:
        content += "\n" + code

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ {desc}")


def patch_config_yaml():
    """修复 config.yaml 参数。"""
    filepath = os.path.join(PROJECT_ROOT, "config.yaml")
    backup(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    changes = [
        ("max_pages_per_stock: 2", "max_pages_per_stock: 50", "帖子页数 2→50"),
        ("max_comment_pages: 2", "max_comment_pages: 20", "评论页数 2→20"),
        ("max_pages_per_user: 2", "max_pages_per_user: 50", "用户页数 2→50"),
    ]

    changed = False
    for old, new, desc in changes:
        if old in content:
            content = content.replace(old, new)
            print(f"  ✓ {desc}")
            changed = True
        else:
            print(f"  · 跳过: {desc}")

    # 添加 comment_backfill_days 配置（如果不存在）
    if "comment_backfill_days" not in content:
        content = content.replace(
            "max_retries: 3",
            "max_retries: 3\n  comment_backfill_days: 7   # 评论回填检查最近N天的帖子"
        )
        print("  ✓ 新增 comment_backfill_days: 7")
        changed = True

    if changed:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    print("  ✓ config.yaml 已更新")


def patch_api_endpoints():
    """修复 api_endpoints.py 翻页判断。"""
    filepath = os.path.join(PROJECT_ROOT, "scrapers", "api_endpoints.py")
    backup(filepath)

    replacements = [
        # 修复帖子列表翻页判断
        (
            "def parse_stock_timeline_response(data: dict) -> tuple:",
            "def parse_stock_timeline_response(data: dict, requested_count: int = 20) -> tuple:",
            "parse_stock_timeline_response 加入 requested_count 参数"
        ),
        (
            "    has_more = len(posts) > 0\n    return posts, has_more",
            "    has_more = len(posts) >= requested_count\n    return posts, has_more",
            "翻页判断: len>0 → len>=count"
        ),
    ]
    patch_file(filepath, replacements)
    print("  ✓ api_endpoints.py 已更新")


def patch_main_py():
    """修改 main.py：添加命令、修复资源释放。"""
    filepath = os.path.join(PROJECT_ROOT, "main.py")
    backup(filepath)

    replacements = [
        # 修复 cmd_run：加 client.close()
        (
            """def cmd_run(args, config):
    \"\"\"立即运行一次。\"\"\"
    components = init_components(config)
    try:
        run_full_scrape(components)
    finally:
        components["db"].close()""",
            """def cmd_run(args, config):
    \"\"\"立即运行一次。\"\"\"
    components = init_components(config)
    try:
        run_full_scrape(components)
    finally:
        components["client"].close()
        components["db"].close()""",
            "cmd_run 加入 client.close()"
        ),
        # 修复 cmd_test_cookie：加 client.close()
        (
            """    if ok:
        print("✓ Cookie 有效")
    else:
        print("✗ Cookie 无效或已失效，请更新 config.yaml")

    components["db"].close()""",
            """    if ok:
        print("✓ Cookie 有效")
    else:
        print("✗ Cookie 无效或已失效，请更新 config.yaml")

    components["client"].close()
    components["db"].close()""",
            "cmd_test_cookie 加入 client.close()"
        ),
    ]

    patch_file(filepath, replacements)

    # 添加 backfill-comments 命令函数
    backfill_func = '''

def cmd_backfill_comments(args, config):
    """回填缺失的评论。"""
    components = init_components(config)
    stock_scraper = components["stock_scraper"]

    symbol = getattr(args, 'symbol', None)
    if symbol:
        symbol = symbol.upper()

    days = getattr(args, 'days', 7) or 7

    print(f"开始评论回填（最近 {days} 天" + (f", 股票 {symbol}" if symbol else ", 全部股票") + "）...")

    try:
        result = stock_scraper.backfill_comments(symbol=symbol, days=days)
        print(f"✓ 回填完成: 处理 {result['total_posts']} 个帖子, 新增 {result['new_comments']} 条评论")
    except CookieExpired:
        print("✗ Cookie 已失效，请更新 config.yaml")
    except Exception as e:
        print(f"✗ 回填失败: {e}")
    finally:
        components["client"].close()
        components["db"].close()

'''

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 添加 backfill 函数（在 cmd_export 之前）
    if "cmd_backfill_comments" not in content:
        content = content.replace(
            "def cmd_export(args, config):",
            backfill_func + "def cmd_export(args, config):"
        )
        print("  ✓ 新增 cmd_backfill_comments 函数")
    else:
        print("  · 跳过: cmd_backfill_comments 已存在")

    # 在 argparse 中添加 backfill-comments 子命令
    backfill_parser = '''
    # backfill-comments
    p = subparsers.add_parser("backfill-comments", help="回填缺失的评论")
    p.add_argument("--symbol", default=None, help="指定股票代码（默认全部）")
    p.add_argument("--days", type=int, default=7, help="回填最近N天（默认7）")

'''
    if "backfill-comments" not in content:
        content = content.replace(
            '    # status\n    subparsers.add_parser("status"',
            backfill_parser + '    # status\n    subparsers.add_parser("status"'
        )
        print("  ✓ 新增 backfill-comments 子命令")
    else:
        print("  · 跳过: backfill-comments 子命令已存在")

    # 在 commands dict 中添加 backfill-comments
    if '"backfill-comments": cmd_backfill_comments' not in content:
        content = content.replace(
            '"export": cmd_export,',
            '"backfill-comments": cmd_backfill_comments,\n        "export": cmd_export,'
        )
        print("  ✓ commands dict 注册 backfill-comments")
    else:
        print("  · 跳过: commands dict 已注册")

    # 修复 cmd_export：加 client.close()
    if 'components["client"].close()' not in content.split("def cmd_export")[1].split("def ")[0] if "def cmd_export" in content else "":
        content = content.replace(
            """    if not any(db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ["posts", "comments", "user_statuses"]):
        print("数据库为空，无数据可导出。请先运行 python main.py run 进行爬取。")

    db.close()""",
            """    if not any(db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ["posts", "comments", "user_statuses"]):
        print("数据库为空，无数据可导出。请先运行 python main.py run 进行爬取。")

    components["client"].close()
    db.close()"""
        )
        print("  ✓ cmd_export 加入 client.close()")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print("  ✓ main.py 已更新")


def verify_files():
    """验证前置文件是否已到位。"""
    checks = [
        ("utils/time_utils.py", "时间工具模块"),
        ("storage/database.py", "数据库模块 v2"),
        ("scrapers/stock_comments.py", "评论爬虫 v2"),
    ]
    all_ok = True
    for path, desc in checks:
        full = os.path.join(PROJECT_ROOT, path)
        if os.path.exists(full):
            # 简单检查是否是新版本
            with open(full, "r", encoding="utf-8") as f:
                content = f.read()
            if "v2" in content or "backfill" in content or "market_phase" in content:
                print(f"  ✓ {path} ({desc})")
            else:
                print(f"  ⚠ {path} 存在但可能是旧版本，请确认已替换")
        else:
            print(f"  ✗ {path} 不存在！请先放置此文件")
            all_ok = False
    return all_ok


def main():
    print("=" * 55)
    print("  雪球爬虫 阶段一 — 补丁安装")
    print("=" * 55)
    print()

    print("[0/4] 检查前置文件...")
    if not verify_files():
        print()
        print("⚠️  请先完成以下步骤:")
        print("  1. cp time_utils.py utils/time_utils.py")
        print("  2. cp database.py storage/database.py")
        print("  3. cp stock_comments.py scrapers/stock_comments.py")
        print("  4. 再次运行 python phase1_patch.py")
        return
    print()

    print("[1/4] 修复 config.yaml...")
    patch_config_yaml()
    print()

    print("[2/4] 修复 api_endpoints.py...")
    patch_api_endpoints()
    print()

    print("[3/4] 修改 main.py...")
    patch_main_py()
    print()

    print("[4/4] 验证数据库迁移...")
    # 数据库迁移在 Database.__init__ 中自动执行
    # 这里只做一次测试初始化
    try:
        import yaml
        with open(os.path.join(PROJECT_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        from storage.database import Database
        db = Database(config.get("database", {}))
        stats = db.get_stats()
        print(f"  ✓ 数据库迁移成功")
        print(f"    帖子: {stats['posts']}, 评论: {stats['comments']}")

        # 检查回填需求
        needs = db.get_posts_needing_backfill(days=9999)
        if needs:
            total_gap = sum(p["gap"] for p in needs)
            print(f"    待回填: {len(needs)} 个帖子, 约 {total_gap} 条评论缺失")
        db.close()
    except Exception as e:
        print(f"  ⚠ 数据库验证出错: {e}")
        print("    （不影响安装，下次运行时会自动迁移）")

    print()
    print("=" * 55)
    print("  阶段一安装完成！")
    print("=" * 55)
    print()
    print("接下来可以:")
    print()
    print("  1. 回填缺失评论:")
    print("     python main.py backfill-comments")
    print("     python main.py backfill-comments --days 30")
    print("     python main.py backfill-comments --symbol SH600519")
    print()
    print("  2. 正常运行（自动回填近7天帖子的新评论）:")
    print("     python main.py run")
    print()
    print("  3. 查看状态:")
    print("     python main.py status")
    print()


if __name__ == "__main__":
    main()
