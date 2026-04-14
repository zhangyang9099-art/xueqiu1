"""批量K线数据下载脚本 - 从数据库评论时间段驱动
按每只股票的评论时间段下载K线，跳过已有数据，带限流控制
"""

import json
import sqlite3
import subprocess
import time
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import os
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path("/Users/zhangyang/Desktop/xueqiu-scraper")
DB_PATH = PROJECT_ROOT / "data/xueqiu.db"
KLINE_BASE = PROJECT_ROOT / "data/kline"
API_URL = "https://www.codebuddy.cn/v2/tool/financedata"

# 限流控制：每只股票之间间隔(秒)
INTERVAL_BETWEEN_STOCKS = 3
# API连续调用间隔(秒)
INTERVAL_BETWEEN_CALLS = 1


def get_stocks_with_comments():
    """从数据库获取有评论的股票列表"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute("""
        SELECT 
            p.symbol as db_symbol,
            COUNT(DISTINCT c.id) as comment_count,
            MIN(SUBSTR(c.created_at_str, 1, 10)) as earliest,
            MAX(SUBSTR(c.created_at_str, 1, 10)) as latest
        FROM posts p 
        JOIN comments c ON c.parent_post_id = p.id 
        WHERE c.created_at_str IS NOT NULL AND c.created_at_str != '' AND LENGTH(c.created_at_str) >= 10 
        GROUP BY p.symbol 
        HAVING comment_count > 0 
        ORDER BY comment_count DESC
    """).fetchall()
    conn.close()
    
    stocks = []
    for row in rows:
        db_symbol = row[0]
        comment_count = row[1]
        earliest = row[2]
        latest = row[3]
        
        # 转换API格式
        if db_symbol.startswith("SH"):
            api_code = db_symbol[2:] + ".SH"
        elif db_symbol.startswith("SZ"):
            api_code = db_symbol[2:] + ".SZ"
        else:
            # 港股/美股，跳过
            continue
        
        stocks.append({
            "db_symbol": db_symbol,
            "api_code": api_code,
            "comment_count": comment_count,
            "earliest_comment": earliest,
            "latest_comment": latest,
        })
    
    return stocks


def has_existing_data(db_symbol):
    """检查是否已有K线数据"""
    kline_dir = KLINE_BASE / db_symbol
    if kline_dir.exists() and (kline_dir / "daily.parquet").exists():
        return True
    return False


def convert_date_to_api_format(date_str):
    """YYYY-MM-DD → YYYYMMDD"""
    return date_str.replace("-", "")


def compute_date_range(earliest_comment, latest_comment):
    """根据评论时间段计算K线下载范围，前后各扩展一个月"""
    try:
        start = datetime.strptime(earliest_comment, "%Y-%m-%d") - timedelta(days=30)
        end = datetime.strptime(latest_comment, "%Y-%m-%d") + timedelta(days=15)
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    except:
        # 默认回退到最近2年
        today = datetime.now()
        start = (today - timedelta(days=730)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        return start, end


def fetch_data(api_code, api_name, start_date, end_date):
    """调用finance-data API获取数据"""
    result = subprocess.run([
        "curl", "-s", "-X", "POST",
        API_URL,
        "-H", "Content-Type: application/json",
        "-d", json.dumps({
            "api_name": api_name,
            "params": {
                "ts_code": api_code,
                "start_date": start_date,
                "end_date": end_date
            },
            "fields": ""
        })
    ], capture_output=True, text=True, timeout=30)
    
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise Exception(f"API响应解析失败: {result.stdout[:200]}")
    
    if data["code"] != 0:
        raise Exception(f"API error ({data.get('code')}): {data.get('msg', 'unknown')}")
    
    fields = data["data"]["fields"]
    items = data["data"]["items"]
    if not items:
        return pd.DataFrame()
    
    return pd.DataFrame(items, columns=fields)


def standardize(df, db_symbol, period):
    """标准化字段"""
    if df.empty:
        return df
    df = df.copy()
    if "ts_code" in df.columns:
        df = df.rename(columns={"ts_code": "symbol"})
    df["symbol"] = db_symbol
    df["period"] = period
    df["amplitude"] = ((df["high"] - df["low"]) / df["pre_close"] * 100).round(4)
    cols = ["symbol", "trade_date", "open", "high", "low", "close",
            "pre_close", "change", "pct_chg", "vol", "amount", "amplitude", "period"]
    # 只保留存在的列
    cols = [c for c in cols if c in df.columns]
    return df[cols].sort_values("trade_date").reset_index(drop=True)


def aggregate_quarterly(monthly_df, db_symbol):
    """从月K聚合季K"""
    if monthly_df.empty:
        return monthly_df
    monthly_df = monthly_df.copy()
    monthly_df["year"] = monthly_df["trade_date"].str[:4].astype(int)
    monthly_df["month"] = monthly_df["trade_date"].str[4:6].astype(int)
    monthly_df["quarter"] = ((monthly_df["month"] - 1) // 3) + 1
    
    records = []
    for (year, quarter), group in monthly_df.groupby(["year", "quarter"]):
        group = group.sort_values("trade_date")
        records.append({
            "symbol": db_symbol,
            "trade_date": f"{year}{quarter:02d}01",
            "open": group.iloc[0]["open"],
            "high": group["high"].max(),
            "low": group["low"].min(),
            "close": group.iloc[-1]["close"],
            "pre_close": group.iloc[0]["pre_close"],
            "change": group["change"].sum(),
            "pct_chg": round(((group.iloc[-1]["close"] / group.iloc[0]["pre_close"]) - 1) * 100, 4),
            "vol": group["vol"].sum(),
            "amount": group["amount"].sum(),
            "amplitude": round(((group["high"].max() - group["low"].min()) / group.iloc[0]["pre_close"]) * 100, 4),
            "period": "quarterly"
        })
    return pd.DataFrame(records).sort_values("trade_date").reset_index(drop=True)


def save_parquet(df, path):
    """保存Parquet"""
    df.to_parquet(path, index=False, engine="pyarrow")
    size_kb = os.path.getsize(path) / 1024
    return f"{path.name} ({size_kb:.1f}KB, {len(df)}条)"


def save_to_sqlite(db_symbol, df, db_path, table_name):
    """写入SQLite"""
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute(f"DELETE FROM {table_name} WHERE symbol = ?", (db_symbol,))
    df.to_sql(table_name, conn, if_exists="append", index=False)
    count = conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE symbol = ?", (db_symbol,)).fetchone()[0]
    conn.close()
    return count


def create_sqlite_tables(db_path):
    """确保K线表存在"""
    conn = sqlite3.connect(db_path, timeout=30)
    cursor = conn.cursor()
    for table in ["kline_daily", "kline_weekly", "kline_monthly", "kline_quarterly"]:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                symbol TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
                close REAL, pre_close REAL, change REAL, pct_chg REAL,
                vol REAL, amount REAL, amplitude REAL, period TEXT
            )
        """)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_sym ON {table}(symbol)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_dt ON {table}(trade_date)")
    conn.commit()
    conn.close()


def download_one_stock(stock_info):
    """下载单只股票的K线数据"""
    db_symbol = stock_info["db_symbol"]
    api_code = stock_info["api_code"]
    
    # 检查已有数据
    if has_existing_data(db_symbol):
        return None, "已有数据，跳过"
    
    kline_dir = KLINE_BASE / db_symbol
    kline_dir.mkdir(parents=True, exist_ok=True)
    
    # 计算日期范围
    start_date, end_date = compute_date_range(
        stock_info["earliest_comment"],
        stock_info["latest_comment"]
    )
    
    # 拉取日K
    try:
        daily_df = fetch_data(api_code, "daily", start_date, end_date)
    except Exception as e:
        return None, f"日K拉取失败: {e}"
    time.sleep(INTERVAL_BETWEEN_CALLS)
    
    # 拉取周K
    try:
        weekly_df = fetch_data(api_code, "weekly", start_date, end_date)
    except Exception as e:
        return None, f"周K拉取失败: {e}"
    time.sleep(INTERVAL_BETWEEN_CALLS)
    
    # 拉取月K
    try:
        monthly_df = fetch_data(api_code, "monthly", start_date, end_date)
    except Exception as e:
        return None, f"月K拉取失败: {e}"
    
    if daily_df.empty and weekly_df.empty and monthly_df.empty:
        return None, "API返回空数据"
    
    # 标准化
    daily_std = standardize(daily_df, db_symbol, "daily")
    weekly_std = standardize(weekly_df, db_symbol, "weekly")
    monthly_std = standardize(monthly_df, db_symbol, "monthly")
    
    # 聚合季K
    quarterly_std = aggregate_quarterly(monthly_std, db_symbol)
    
    # 保存Parquet
    pq_results = []
    if not daily_std.empty:
        save_parquet(daily_std, kline_dir / "daily.parquet")
        pq_results.append(f"日K:{len(daily_std)}")
    if not weekly_std.empty:
        save_parquet(weekly_std, kline_dir / "weekly.parquet")
        pq_results.append(f"周K:{len(weekly_std)}")
    if not monthly_std.empty:
        save_parquet(monthly_std, kline_dir / "monthly.parquet")
        pq_results.append(f"月K:{len(monthly_std)}")
    if not quarterly_std.empty:
        save_parquet(quarterly_std, kline_dir / "quarterly.parquet")
        pq_results.append(f"季K:{len(quarterly_std)}")
    
    # 保存SQLite
    db_results = []
    if not daily_std.empty:
        db_results.append(f"日K:{save_to_sqlite(db_symbol, daily_std, DB_PATH, 'kline_daily')}")
    if not weekly_std.empty:
        db_results.append(f"周K:{save_to_sqlite(db_symbol, weekly_std, DB_PATH, 'kline_weekly')}")
    if not monthly_std.empty:
        db_results.append(f"月K:{save_to_sqlite(db_symbol, monthly_std, DB_PATH, 'kline_monthly')}")
    if not quarterly_std.empty:
        db_results.append(f"季K:{save_to_sqlite(db_symbol, quarterly_std, DB_PATH, 'kline_quarterly')}")
    
    date_range = ""
    if not daily_std.empty:
        date_range = f"{daily_std['trade_date'].iloc[0]}~{daily_std['trade_date'].iloc[-1]}"
    
    return {
        "db_symbol": db_symbol,
        "api_code": api_code,
        "date_range": date_range,
        "pq": ", ".join(pq_results),
        "db": ", ".join(db_results),
    }, "成功"


def main():
    print("=" * 70)
    print("批量K线下载 - 基于数据库评论时间段")
    print("=" * 70)
    
    # 获取有评论的股票
    stocks = get_stocks_with_comments()
    print(f"\n📊 数据库中有评论的A股: {len(stocks)} 只")
    
    # 过滤
    to_download = []
    skipped = []
    for s in stocks:
        if has_existing_data(s["db_symbol"]):
            skipped.append(s)
        else:
            to_download.append(s)
    
    print(f"⏭️ 已有数据跳过: {len(skipped)} 只")
    print(f"📥 需要下载: {len(to_download)} 只")
    
    if not to_download:
        print("\n✅ 所有股票K线数据已存在，无需下载")
        return
    
    # 列出待下载
    print(f"\n{'序号':>4} {'代码':<12} {'评论数':>6} {'评论时间段':<25} {'K线范围'}")
    print("-" * 70)
    for i, s in enumerate(to_download, 1):
        start, end = compute_date_range(s["earliest_comment"], s["latest_comment"])
        print(f"{i:>4} {s['api_code']:<12} {s['comment_count']:>6} "
              f"{s['earliest_comment']}~{s['latest_comment']:<14} {start}~{end}")
    
    # 确认
    print(f"\n即将下载 {len(to_download)} 只股票的K线数据...")
    print(f"每只股票间隔 {INTERVAL_BETWEEN_STOCKS} 秒（防限流）")
    
    # 创建表
    create_sqlite_tables(DB_PATH)
    
    # 逐只下载
    results = []
    errors = []
    for i, stock in enumerate(to_download, 1):
        print(f"\n[{i}/{len(to_download)}] {stock['api_code']} ({stock['db_symbol']}) - 评论 {stock['comment_count']} 条")
        
        result, status = download_one_stock(stock)
        time.sleep(INTERVAL_BETWEEN_STOCKS)
        
        if status == "成功":
            print(f"  ✅ {result['date_range']} | Parquet: {result['pq']} | DB: {result['db']}")
            results.append(result)
        else:
            print(f"  ❌ {status}")
            errors.append({"symbol": stock["api_code"], "reason": status})
    
    # 汇总
    print(f"\n{'=' * 70}")
    print(f"📊 下载完成")
    print(f"{'=' * 70}")
    print(f"  ✅ 成功: {len(results)} 只")
    if results:
        print(f"  失败: {len(errors)} 只")
        for e in errors:
            print(f"    ❌ {e['symbol']}: {e['reason']}")
    
    # 跳过的
    if skipped:
        print(f"\n  ⏭️ 已有数据跳过: {len(skipped)} 只")
        for s in skipped:
            print(f"    {s['api_code']}")
    
    # 统计
    total_daily = sum(int(r['pq'].split('日K:')[1].split(',')[0]) if '日K:' in r['pq'] else 0 for r in results)
    print(f"\n  总计日K数据: {total_daily} 条")
    print(f"  存储目录: {KLINE_BASE}/")


if __name__ == "__main__":
    main()
