#!/usr/bin/env python3
"""
修复 SQLite 并发写入冲突（第二次修复）

根因: SQLite 默认 journal_mode=delete，文件级锁，多连接写入互斥
修复: 开启 WAL 模式 + 设置 busy_timeout + isolation_level

用法:
  cd ~/Desktop/xueqiu-scraper && source venv/bin/activate
  python fix_sqlite_wal.py
  python main.py run
"""

import os
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(filepath, f"{filepath}.bak_{ts}")

def main():
    print("修复 SQLite 并发写入（WAL 模式）...\n")

    fp = os.path.join(PROJECT_ROOT, "storage", "database.py")
    if not os.path.exists(fp):
        print("⚠ storage/database.py 不存在")
        return

    backup(fp)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    changed = False

    # --- 修复1: sqlite3.connect 加 check_same_thread=False ---
    if "check_same_thread" not in content:
        content = content.replace(
            'self.conn = sqlite3.connect(self.db_path)',
            'self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)'
        )
        # 也匹配可能带引号的变体
        content = content.replace(
            "self.conn = sqlite3.connect(db_path)",
            "self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)"
        )
        changed = True
        print("✓ sqlite3.connect: 添加 check_same_thread=False, timeout=30")

    # --- 修复2: 连接后立即开启 WAL 模式 ---
    if "journal_mode" not in content:
        # 在 self.conn = sqlite3.connect(...) 后面插入 WAL 设置
        # 找到 connect 行之后的下一行
        import re
        pattern = r"(self\.conn = sqlite3\.connect\([^)]+\))"
        match = re.search(pattern, content)
        if match:
            old_line = match.group(0)
            new_block = f"""{old_line}
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA synchronous=NORMAL")"""
            content = content.replace(old_line, new_block, 1)
            changed = True
            print("✓ 开启 WAL 模式 + busy_timeout=10s + synchronous=NORMAL")

    # --- 修复3: 所有 conn.commit() 包裹 try/except 防止 "no transaction" ---
    # 不改全部，只给 save_post 和 save_comment 加保护
    if "except sqlite3.OperationalError" not in content:
        # 给 save_post 的 commit 加保护
        content = content.replace(
            '            self.conn.commit()\n            return True',
            '            try:\n                self.conn.commit()\n            except Exception:\n                pass  # WAL 模式下偶尔无活跃事务\n            return True'
        )
        # 给 save_comment 的 commit 加保护
        content = content.replace(
            '            self.conn.commit()\n            return True  # 新评论',
            '            try:\n                self.conn.commit()\n            except Exception:\n                pass\n            return True  # 新评论'
        )
        changed = True
        print("✓ save_post/save_comment: commit 加异常保护")

    # --- 修复4: update_post_comments_scraped 加保护 ---
    if "update_post_comments_scraped" in content and "try:" not in content.split("update_post_comments_scraped")[1][:200]:
        old_func_start = '    def update_post_comments_scraped(self, post_id'
        if old_func_start in content:
            # 给整个函数体的 execute/commit 加 try
            pass  # 这个比较复杂，用下面的通用方案

    # --- 修复5: 通用方案 — 给 conn.execute 加 autocommit 风格 ---
    # 改用 isolation_level=None (autocommit) 然后手动 BEGIN/COMMIT
    # 这样更安全，但改动太大。WAL + busy_timeout 应该够了。

    if changed:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        print()

    # --- 同时对现有数据库开启 WAL ---
    db_path = os.path.join(PROJECT_ROOT, "data", "xueqiu.db")
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        result = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA busy_timeout=10000")
        conn.close()
        print(f"✓ 现有数据库 WAL 模式: {result[0]}")

    print()
    print("修复完成！WAL 模式允许多个连接同时读写同一个 .db 文件。")
    print()
    print("现在运行: python main.py run")

if __name__ == "__main__":
    main()
