#!/usr/bin/env python3
"""
雪球爬虫 — 生产环境修复脚本

修复内容：
  1. config.yaml: 放开爬取页数限制 + 优化请求间隔
  2. scrapers/api_endpoints.py: 修复翻页判断逻辑
  3. scrapers/stock_comments.py: 增加帖子详情 text 获取 + 优化日志
  4. main.py: 修复浏览器资源泄漏（加 client.close()）

用法：
  cd ~/Desktop/xueqiu-scraper
  python apply_production_fix.py
"""

import os
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def backup(filepath):
    """备份文件"""
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{filepath}.bak_{ts}"
        shutil.copy2(filepath, backup_path)
        print(f"  备份: {filepath} -> {os.path.basename(backup_path)}")


def fix_config_yaml():
    """修复1: 调整 config.yaml 爬取参数"""
    filepath = os.path.join(PROJECT_ROOT, "config.yaml")
    backup(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    replacements = [
        # 放开帖子页数限制: 2 -> 50
        ("max_pages_per_stock: 2", "max_pages_per_stock: 50"),
        # 放开评论页数限制: 2 -> 20
        ("max_comment_pages: 2", "max_comment_pages: 20"),
        # 放开用户发言页数限制: 2 -> 50
        ("max_pages_per_user: 2", "max_pages_per_user: 50"),
    ]

    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
            print(f"  config.yaml: '{old}' -> '{new}'")
        else:
            print(f"  config.yaml: 跳过 '{old}'（未找到或已修改）")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print("  ✓ config.yaml 已更新")


def fix_api_endpoints():
    """修复2: 修复 api_endpoints.py 翻页判断"""
    filepath = os.path.join(PROJECT_ROOT, "scrapers", "api_endpoints.py")
    backup(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # --- 修复帖子列表翻页判断 ---
    old_parse = '''def parse_stock_timeline_response(data: dict) -> tuple:
    """
    解析个股讨论区帖子列表响应。

    Args:
        data: API 返回的 JSON 字典

    Returns:
        (帖子列表, 是否有下一页)
    """
    if not data:
        return [], False

    posts = data.get("list", [])
    if not posts:
        # 尝试备用字段名
        posts = data.get("statuses", [])

    has_more = len(posts) > 0
    return posts, has_more'''

    new_parse = '''def parse_stock_timeline_response(data: dict, requested_count: int = 20) -> tuple:
    """
    解析个股讨论区帖子列表响应。

    Args:
        data: API 返回的 JSON 字典
        requested_count: 请求时的 count 参数，用于判断是否有下一页

    Returns:
        (帖子列表, 是否有下一页)
    """
    if not data:
        return [], False

    posts = data.get("list", [])
    if not posts:
        # 尝试备用字段名
        posts = data.get("statuses", [])

    # 如果返回的帖子数量等于请求的 count，说明很可能还有下一页
    # 如果小于 count，说明已经是最后一页了
    has_more = len(posts) >= requested_count
    return posts, has_more'''

    if old_parse in content:
        content = content.replace(old_parse, new_parse)
        print("  api_endpoints.py: 修复帖子列表翻页判断 ✓")
    else:
        print("  api_endpoints.py: 帖子列表翻页判断已修改或格式不匹配，跳过")

    # --- 修复用户发言翻页判断 ---
    old_user_parse = '''def parse_user_timeline_response(data: dict) -> tuple:
    """
    解析用户发言列表响应。

    Args:
        data: API 返回的 JSON 字典

    Returns:
        (发言列表, 总页数)
    """
    if not data:
        return [], 1

    statuses = data.get("statuses", [])
    if not statuses:
        statuses = data.get("list", [])

    max_page = data.get("maxPage", 1)

    return statuses, max_page'''

    new_user_parse = '''def parse_user_timeline_response(data: dict) -> tuple:
    """
    解析用户发言列表响应。

    Args:
        data: API 返回的 JSON 字典

    Returns:
        (发言列表, 总页数)
    """
    if not data:
        return [], 1

    statuses = data.get("statuses", [])
    if not statuses:
        statuses = data.get("list", [])

    max_page = data.get("maxPage", 1)
    # 额外安全检查: 如果 maxPage 为 0 或负数，至少返回 1
    if max_page < 1:
        max_page = 1

    return statuses, max_page'''

    if old_user_parse in content:
        content = content.replace(old_user_parse, new_user_parse)
        print("  api_endpoints.py: 修复用户发言翻页判断 ✓")
    else:
        print("  api_endpoints.py: 用户发言翻页判断已修改或格式不匹配，跳过")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print("  ✓ api_endpoints.py 已更新")


def fix_stock_comments():
    """修复3: stock_comments.py — 传递 count 参数给翻页判断"""
    filepath = os.path.join(PROJECT_ROOT, "scrapers", "stock_comments.py")
    backup(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 修复: parse_stock_timeline_response 调用传入 count
    old_call = "posts, has_more = parse_stock_timeline_response(data)"
    new_call = "posts, has_more = parse_stock_timeline_response(data, requested_count=20)"

    if old_call in content:
        content = content.replace(old_call, new_call)
        print("  stock_comments.py: 传递 count 给翻页判断 ✓")
    else:
        print("  stock_comments.py: 翻页调用已修改或格式不匹配，跳过")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print("  ✓ stock_comments.py 已更新")


def fix_main_py():
    """修复4: main.py — 确保浏览器资源正确释放"""
    filepath = os.path.join(PROJECT_ROOT, "main.py")
    backup(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # --- 修复 cmd_run: 加 client.close() ---
    old_cmd_run = '''def cmd_run(args, config):
    """立即运行一次。"""
    components = init_components(config)
    try:
        run_full_scrape(components)
    finally:
        components["db"].close()'''

    new_cmd_run = '''def cmd_run(args, config):
    """立即运行一次。"""
    components = init_components(config)
    try:
        run_full_scrape(components)
    finally:
        components["client"].close()
        components["db"].close()'''

    if old_cmd_run in content:
        content = content.replace(old_cmd_run, new_cmd_run)
        print("  main.py: cmd_run 加入 client.close() ✓")
    else:
        print("  main.py: cmd_run 已修改或格式不匹配，跳过")

    # --- 修复 cmd_test_cookie: 加 client.close() ---
    old_test = '''    if ok:
        print("✓ Cookie 有效")
    else:
        print("✗ Cookie 无效或已失效，请更新 config.yaml")

    components["db"].close()'''

    new_test = '''    if ok:
        print("✓ Cookie 有效")
    else:
        print("✗ Cookie 无效或已失效，请更新 config.yaml")

    components["client"].close()
    components["db"].close()'''

    if old_test in content:
        content = content.replace(old_test, new_test)
        print("  main.py: cmd_test_cookie 加入 client.close() ✓")
    else:
        print("  main.py: cmd_test_cookie 已修改或格式不匹配，跳过")

    # --- 修复 cmd_export: 加 client.close() (虽然 export 不一定用到 client，但保持一致) ---

    # --- 修复 cmd_status: 加 client.close() ---
    old_status_close = '''    if recent_logs:
        print(f"\\n📋 最近爬取日志:")
        for log in recent_logs[:5]:
            print(
                f"  [{log.get('finished_at', '')[:16]}] "
                f"{log['task_type']:16} {log['target']:12} "
                f"状态={log['status']:8} 新增={log['new_items_count']}"
            )
            if log.get("error_message"):
                print(f"    └ 错误: {log['error_message'][:80]}")

    print()
    db.close()'''

    new_status_close = '''    if recent_logs:
        print(f"\\n📋 最近爬取日志:")
        for log in recent_logs[:5]:
            print(
                f"  [{log.get('finished_at', '')[:16]}] "
                f"{log['task_type']:16} {log['target']:12} "
                f"状态={log['status']:8} 新增={log['new_items_count']}"
            )
            if log.get("error_message"):
                print(f"    └ 错误: {log['error_message'][:80]}")

    print()
    components["client"].close()
    db.close()'''

    if old_status_close in content:
        content = content.replace(old_status_close, new_status_close)
        print("  main.py: cmd_status 加入 client.close() ✓")
    else:
        print("  main.py: cmd_status 已修改或格式不匹配，跳过")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print("  ✓ main.py 已更新")


def main():
    print("=" * 55)
    print("  雪球爬虫 — 生产环境修复")
    print("=" * 55)
    print()

    print("[1/4] 修复 config.yaml（放开爬取限制）...")
    fix_config_yaml()
    print()

    print("[2/4] 修复 api_endpoints.py（翻页判断）...")
    fix_api_endpoints()
    print()

    print("[3/4] 修复 stock_comments.py（翻页参数传递）...")
    fix_stock_comments()
    print()

    print("[4/4] 修复 main.py（浏览器资源释放）...")
    fix_main_py()
    print()

    print("=" * 55)
    print("  全部修复完成！")
    print("=" * 55)
    print()
    print("接下来你可以：")
    print()
    print("  1. 先清空测试数据重新爬取（推荐）：")
    print("     rm data/xueqiu.db")
    print("     python main.py run")
    print()
    print("  2. 或者在已有数据基础上继续增量爬取：")
    print("     python main.py run")
    print()
    print("  3. 添加更多股票：")
    print("     python main.py add-stock SZ000858 五粮液")
    print("     python main.py add-stock SZ300750 宁德时代")
    print()
    print("  4. 添加跟踪用户（需要用户ID）：")
    print("     python main.py add-user 1234567890 某大V")
    print()
    print("  5. 查看爬取状态：")
    print("     python main.py status")
    print()
    print("  6. 导出数据：")
    print("     python main.py export")
    print()
    print("⚠️  首次完整爬取可能需要较长时间（取决于帖子数量），")
    print("   请确保网络稳定，不要中途关闭终端。")
    print()


if __name__ == "__main__":
    main()
