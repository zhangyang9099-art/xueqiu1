#!/usr/bin/env python3
"""
数据查询层 v2 — 为舆情分析准备结构化数据包

v2 变更 (V4优化计划):
  - get_top_threads: N+1查询合并为3次批量SQL
  - get_user_summary: 单次JOIN替代逐用户查询
  - 新增 get_session_distribution: 盘前/盘中/盘后时段聚合
  - 新增 compute_burst_index: 10分钟窗口爆发指数
  - 新增 deduplicate_threads: 转发帖去重
  - 热度分: 原始 + 时间衰减(排序) + 基准线偏离度(展示) 分离设计
  - get_top_threads 增加时间衰减排序和基准线偏离度
"""

import hashlib
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_db_path(config: dict) -> str:
    db_cfg = config.get("database", {})
    path = db_cfg.get("sqlite_path", "data/xueqiu.db")
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_ROOT, path)
    return path


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_data_overview(conn: sqlite3.Connection, symbol: Optional[str], days: int) -> dict:
    """第一层：数据资产盘点"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    if symbol:
        where = "WHERE symbol = ? AND created_at_str >= ?"
        params = (symbol, cutoff)
    else:
        where = "WHERE created_at_str >= ?"
        params = (cutoff,)

    row = conn.execute(f"""
        SELECT
            COUNT(*) AS total_posts,
            SUM(comments_scraped) AS total_comments,
            MIN(created_at_str) AS earliest_post,
            MAX(created_at_str) AS latest_post,
            COUNT(DISTINCT symbol) AS stock_count
        FROM posts {where}
    """, params).fetchone()

    # 评论完备率
    total_claimed = conn.execute(f"""
        SELECT SUM(reply_count) FROM posts {where}
    """, params).fetchone()[0] or 0
    total_scraped = row["total_comments"] or 0
    completion_rate = f"{total_scraped / total_claimed * 100:.1f}%" if total_claimed > 0 else "N/A"

    # K线数据可用性
    kline_status = {}
    if symbol:
        kline_dir = os.path.join(PROJECT_ROOT, "data", "kline", symbol)
        for period in ["daily", "weekly", "monthly"]:
            pq_path = os.path.join(kline_dir, f"{period}.parquet")
            if os.path.exists(pq_path):
                import pandas as pd
                df = pd.read_parquet(pq_path)
                kline_status[period] = f"{len(df)}条, {df['trade_date'].min()}~{df['trade_date'].max()}"
            else:
                kline_status[period] = "无数据"

    return {
        "total_posts": row["total_posts"],
        "total_comments": total_scraped,
        "comment_completion_rate": completion_rate,
        "earliest_post": row["earliest_post"],
        "latest_post": row["latest_post"],
        "stock_count": row["stock_count"],
        "time_range": f"最近{days}天",
        "kline_status": kline_status,
    }


def get_top_threads(conn: sqlite3.Connection, symbol: Optional[str], days: int,
                    top_n: int, min_comment_depth: int) -> list:
    """获取热度TOP-N讨论线程，含帖子内容+评论树+参与用户画像。

    V4优化: N+1查询合并为3次批量SQL（原41次→3次）
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    if symbol:
        symbol_where = "AND dt.symbol = ?"
        symbol_param = symbol
    else:
        symbol_where = ""
        symbol_param = None

    # 查询1: TOP-N线程（含时间衰减热度和基准线偏离度）
    # 衰减热度用于排序，原始热度偏离度用于展示
    baseline_cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    params = [cutoff, baseline_cutoff]
    if symbol_param:
        params.append(symbol_param)

    threads = conn.execute(f"""
        SELECT dt.*,
            -- 原始热度分
            (dt.like_count + COALESCE(dt.actual_comments, 0) * 2
             + COALESCE(dt.retweet_count, 0) * 3 + COALESCE(dt.fav_count, 0) * 1.5)
            AS heat_score,
            -- 时间衰减热度（半衰期7天，用于排序）
            (dt.like_count + COALESCE(dt.actual_comments, 0) * 2
             + COALESCE(dt.retweet_count, 0) * 3 + COALESCE(dt.fav_count, 0) * 1.5)
            * EXP(-0.099 * (julianday('now') - julianday(dt.start_time)))
            AS heat_score_decayed,
            -- 30天基准线（原始热度均值）
            (SELECT AVG(p.like_count + COALESCE(p.comments_scraped, 0) * 2
                        + COALESCE(p.retweet_count, 0) * 3 + COALESCE(p.fav_count, 0) * 1.5)
             FROM posts p
             WHERE p.symbol = dt.symbol AND p.created_at_str >= ?) AS heat_baseline
        FROM discussion_threads dt
        WHERE dt.start_time >= ? {symbol_where}
        ORDER BY heat_score_decayed DESC
        LIMIT ?
    """, params + [top_n]).fetchall()

    thread_list = [dict(t) for t in threads]
    thread_ids = [t["thread_id"] for t in thread_list]
    if not thread_ids:
        return []

    # 计算基准线偏离度（Python端，避免SQL复杂度）
    for t in thread_list:
        baseline = t.get("heat_baseline")
        raw_heat = t.get("heat_score", 0)
        if baseline and baseline > 0:
            t["heat_deviation_pct"] = round((raw_heat - baseline) / baseline * 100, 1)
        else:
            t["heat_deviation_pct"] = None

    # 查询2: 一次性获取所有线程的评论（批量WHERE IN）
    id_placeholders = ",".join("?" * len(thread_ids))
    all_comments = conn.execute(f"""
        SELECT m.post_id, c.user_id, c.user_name, c.text_plain,
               c.created_at_str, c.like_count, c.depth, c.reply_to_user_name
        FROM comment_memberships m
        JOIN comments c ON c.id = m.comment_id
        WHERE m.post_id IN ({id_placeholders})
        ORDER BY m.post_id, c.created_at_str
    """, thread_ids).fetchall()

    # Python侧按post_id分组
    comments_by_thread = defaultdict(list)
    all_user_ids = set()
    for c in all_comments:
        c_dict = dict(c)
        comments_by_thread[c_dict["post_id"]].append(c_dict)
        if c_dict["user_id"]:
            all_user_ids.add(c_dict["user_id"])

    # 查询3: 一次性获取所有相关用户画像
    user_profiles = {}
    if all_user_ids:
        user_placeholders = ",".join("?" * len(all_user_ids))
        profiles = conn.execute(f"""
            SELECT user_id, screen_name, is_default_name, is_default_avatar,
                   followers_count, status_count, verified_type, description
            FROM user_profiles
            WHERE user_id IN ({user_placeholders})
        """, list(all_user_ids)).fetchall()
        user_profiles = {dict(p)["user_id"]: dict(p) for p in profiles}

    # 组装结果
    result = []
    for t in thread_list:
        t["comments"] = comments_by_thread.get(t["thread_id"], [])
        t["user_profiles"] = user_profiles
        t["is_deep_discussion"] = t["max_comment_depth"] >= min_comment_depth
        result.append(t)

    return result


def get_user_summary(conn: sqlite3.Connection, symbol: Optional[str], days: int) -> list:
    """获取发言量TOP用户列表（含画像）。

    V4优化: 单次LEFT JOIN替代逐用户N+1查询
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    if symbol:
        post_where = "WHERE p.symbol = ? AND p.created_at_str >= ?"
        params = (symbol, cutoff)
    else:
        post_where = "WHERE p.created_at_str >= ?"
        params = (cutoff,)

    result = [dict(r) for r in conn.execute(f"""
        SELECT c.user_id, c.user_name,
               COUNT(DISTINCT c.id) AS comment_count,
               COUNT(DISTINCT m.post_id) AS post_count,
               up.is_default_name, up.is_default_avatar,
               up.followers_count, up.status_count,
               up.verified_type, up.description
        FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        LEFT JOIN user_profiles up ON up.user_id = c.user_id
        {post_where}
        GROUP BY c.user_id, c.user_name
        ORDER BY comment_count DESC
        LIMIT 30
    """, params).fetchall()]

    return result


def get_time_distribution(conn: sqlite3.Connection, symbol: Optional[str],
                          days: int) -> list:
    """按日分组的发帖和评论量"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # 帖子时间分布
    if symbol:
        posts = conn.execute("""
            SELECT substr(created_at_str, 1, 10) AS day, COUNT(*) AS post_count
            FROM posts WHERE symbol = ? AND created_at_str >= ?
            GROUP BY day ORDER BY day
        """, (symbol, cutoff)).fetchall()
    else:
        posts = conn.execute("""
            SELECT substr(created_at_str, 1, 10) AS day, COUNT(*) AS post_count
            FROM posts WHERE created_at_str >= ?
            GROUP BY day ORDER BY day
        """, (cutoff,)).fetchall()

    # 评论时间分布（通过 comment_memberships 关联 posts）
    if symbol:
        comments = conn.execute("""
            SELECT substr(c.created_at_str, 1, 10) AS day, COUNT(*) AS comment_count
            FROM comments c
            JOIN comment_memberships m ON m.comment_id = c.id
            JOIN posts p ON p.id = m.post_id
            WHERE p.symbol = ? AND p.created_at_str >= ?
            GROUP BY day ORDER BY day
        """, (symbol, cutoff)).fetchall()
    else:
        comments = conn.execute("""
            SELECT substr(c.created_at_str, 1, 10) AS day, COUNT(*) AS comment_count
            FROM comments c
            JOIN comment_memberships m ON m.comment_id = c.id
            JOIN posts p ON p.id = m.post_id
            WHERE p.created_at_str >= ?
            GROUP BY day ORDER BY day
        """, (cutoff,)).fetchall()

    post_map = {dict(p)["day"]: dict(p)["post_count"] for p in posts}
    comment_map = {dict(c)["day"]: dict(c)["comment_count"] for c in comments}

    all_days = sorted(set(list(post_map.keys()) + list(comment_map.keys())))
    return [{"day": d, "posts": post_map.get(d, 0), "comments": comment_map.get(d, 0)}
            for d in all_days]


def get_session_distribution(conn: sqlite3.Connection, symbol: Optional[str],
                              days: int) -> list:
    """按交易时段聚合评论量（盘前/上午盘/午间/下午盘/盘后/非交易时段）。

    不同时段的评论情绪价值不同：
    - 盘前(8:00-9:29): 预期表达，预测价值高
    - 上午盘/下午盘: 实时反应，噪音大
    - 午间(11:30-12:59): 短暂讨论
    - 盘后(15:00-17:00): 复盘总结，论据质量最高
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    if symbol:
        rows = conn.execute("""
            SELECT CASE
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '08:00' AND '09:29' THEN '盘前'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '09:30' AND '11:29' THEN '上午盘'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '11:30' AND '12:59' THEN '午间'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '13:00' AND '14:59' THEN '下午盘'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '15:00' AND '17:00' THEN '盘后'
                ELSE '非交易时段'
            END AS session,
            COUNT(*) AS count
            FROM comments c
            JOIN comment_memberships m ON m.comment_id = c.id
            JOIN posts p ON p.id = m.post_id
            WHERE p.symbol = ? AND p.created_at_str >= ?
            GROUP BY session
            ORDER BY CASE session
                WHEN '盘前' THEN 1 WHEN '上午盘' THEN 2
                WHEN '午间' THEN 3 WHEN '下午盘' THEN 4
                WHEN '盘后' THEN 5 ELSE 6 END
        """, (symbol, cutoff)).fetchall()
    else:
        rows = conn.execute("""
            SELECT CASE
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '08:00' AND '09:29' THEN '盘前'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '09:30' AND '11:29' THEN '上午盘'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '11:30' AND '12:59' THEN '午间'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '13:00' AND '14:59' THEN '下午盘'
                WHEN substr(c.created_at_str, 12, 5) BETWEEN '15:00' AND '17:00' THEN '盘后'
                ELSE '非交易时段'
            END AS session,
            COUNT(*) AS count
            FROM comments c
            JOIN comment_memberships m ON m.comment_id = c.id
            JOIN posts p ON p.id = m.post_id
            WHERE p.created_at_str >= ?
            GROUP BY session
            ORDER BY CASE session
                WHEN '盘前' THEN 1 WHEN '上午盘' THEN 2
                WHEN '午间' THEN 3 WHEN '下午盘' THEN 4
                WHEN '盘后' THEN 5 ELSE 6 END
        """, (cutoff,)).fetchall()

    return [dict(r) for r in rows]


def compute_burst_index(conn: sqlite3.Connection, thread_id: str) -> dict:
    """计算单个线程的爆发指数。

    检测短时间内大量新账号涌入同向评论的异常模式。
    返回:
      - burst_10min: 10分钟窗口内最大新账号涌入量
      - new_account_ratio: 新账号评论占比（followers<5 或 status_count<10）
      - total_comments: 该线程评论总数
      - abnormal: bool, 新账号占比>40%标记为异常
    """
    # 该线程的评论时间分布
    rows = conn.execute("""
        SELECT c.user_id, c.created_at_str,
               COALESCE(up.followers_count, 999) AS followers_count,
               COALESCE(up.status_count, 999) AS status_count
        FROM comment_memberships m
        JOIN comments c ON c.id = m.comment_id
        LEFT JOIN user_profiles up ON up.user_id = c.user_id
        WHERE m.post_id = ?
        ORDER BY c.created_at_str
    """, (thread_id,)).fetchall()

    if not rows:
        return {"burst_10min": 0, "new_account_ratio": 0, "total_comments": 0, "abnormal": False}

    all_comments = [dict(r) for r in rows]
    total = len(all_comments)

    # 新账号判定：followers<5 或 status_count<10
    new_account_comments = [
        c for c in all_comments
        if (c["followers_count"] or 999) < 5 or (c["status_count"] or 999) < 10
    ]
    new_account_ratio = len(new_account_comments) / total if total > 0 else 0

    # 10分钟窗口爆发检测（评论时间精确到秒）
    burst_10min = 0
    if len(new_account_comments) >= 2:
        # 按时间排序，滑动窗口找最大涌入量
        for i in range(len(new_account_comments)):
            window_start = new_account_comments[i]["created_at_str"]
            if not window_start or len(window_start) < 16:
                continue
            count = 0
            for j in range(i, len(new_account_comments)):
                ts_j = new_account_comments[j]["created_at_str"]
                if not ts_j or len(ts_j) < 16:
                    continue
                try:
                    t_start = datetime.strptime(window_start[:19], "%Y-%m-%d %H:%M:%S")
                    t_j = datetime.strptime(ts_j[:19], "%Y-%m-%d %H:%M:%S")
                    if (t_j - t_start).total_seconds() <= 600:  # 10分钟
                        count += 1
                    else:
                        break
                except ValueError:
                    continue
            burst_10min = max(burst_10min, count)

    return {
        "burst_10min": burst_10min,
        "new_account_ratio": round(new_account_ratio * 100, 1),
        "total_comments": total,
        "abnormal": new_account_ratio > 0.4 or burst_10min >= 5,
    }


def deduplicate_threads(threads: list) -> list:
    """只对转发帖去重，保留热度更高的版本。

    判断依据：content含"转发"或"//@"标记，或thread有retweet_status_id。
    原创帖不做去重（避免误判不同观点）。
    """
    seen = {}
    result = []
    for t in threads:
        content = t.get("content") or ""
        is_retweet = (
            "转发" in content
            or "//@" in content
        )
        if not is_retweet:
            result.append(t)
            continue
        # 转发帖：用去除标题后的内容hash去重
        body = content[30:] if len(content) > 30 else content
        h = hashlib.md5(body[:500].encode("utf-8")).hexdigest()
        if h in seen:
            existing = seen[h]
            if t.get("heat_score_decayed", 0) > existing.get("heat_score_decayed", 0):
                result = [x for x in result if x["thread_id"] != existing["thread_id"]]
                result.append(t)
                seen[h] = t
        else:
            seen[h] = t
            result.append(t)
    return result


def get_kline_data(symbol: str, days: int) -> Optional[list]:
    """从 parquet 文件读取K线数据"""
    kline_dir = os.path.join(PROJECT_ROOT, "data", "kline", symbol)
    pq_path = os.path.join(kline_dir, "daily.parquet")
    if not os.path.exists(pq_path):
        return None

    try:
        import pandas as pd
        df = pd.read_parquet(pq_path)

        # 过滤时间范围
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = df[df["trade_date"] >= cutoff].sort_values("trade_date")

        return df[["trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount"]].to_dict("records")
    except Exception:
        return None


def build_analysis_data(conn: sqlite3.Connection, config: dict,
                        symbol: Optional[str], days: int, top_n: int,
                        min_depth: int) -> dict:
    """构建完整的数据包，供 Prompt 模板使用"""
    analysis_cfg = config.get("analysis", {})
    min_depth = min_depth or analysis_cfg.get("min_comment_depth", 3)

    threads = get_top_threads(conn, symbol, days, top_n, min_depth)
    threads = deduplicate_threads(threads)

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol or "全部",
        "days": days,
        "overview": get_data_overview(conn, symbol, days),
        "top_threads": threads,
        "user_summary": get_user_summary(conn, symbol, days),
        "time_distribution": get_time_distribution(conn, symbol, days),
    }

    # 时段聚合（仅在指定单只股票时）
    if symbol:
        data["session_distribution"] = get_session_distribution(conn, symbol, days)

    # K线数据（仅在指定单只股票时加载）
    if symbol:
        kline = get_kline_data(symbol, days)
        if kline:
            data["kline"] = kline

    return data
