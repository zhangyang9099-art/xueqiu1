"""华钰矿业K线数据入库脚本 - 单次执行
从API JSON数据生成Parquet文件 + SQLite表 + 季K聚合 + 数据验证
"""

import json
import sqlite3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import os
from pathlib import Path

SYMBOL_DB = "SH601020"
SYMBOL_API = "601020.SH"
NAME = "华钰矿业"
DB_PATH = "/Users/zhangyang/Desktop/xueqiu-scraper/data/xueqiu.db"
KLINE_DIR = Path("/Users/zhangyang/Desktop/xueqiu-scraper/data/kline/SH601020")


def fetch_data(api_name, start_date="20240330", end_date="20260330"):
    """调用finance-data API获取数据"""
    import subprocess
    result = subprocess.run([
        "curl", "-s", "-X", "POST",
        "https://www.codebuddy.cn/v2/tool/financedata",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({
            "api_name": api_name,
            "params": {
                "ts_code": SYMBOL_API,
                "start_date": start_date,
                "end_date": end_date
            },
            "fields": ""
        })
    ], capture_output=True, text=True)
    data = json.loads(result.stdout)
    if data["code"] != 0:
        raise Exception(f"API error: {data['msg']}")
    fields = data["data"]["fields"]
    items = data["data"]["items"]
    df = pd.DataFrame(items, columns=fields)
    return df


def standardize_daily(df, period):
    """标准化字段：重命名ts_code→symbol, 添加period和amplitude"""
    df = df.copy()
    if "ts_code" in df.columns:
        df = df.rename(columns={"ts_code": "symbol"})
    df["symbol"] = SYMBOL_DB
    df["period"] = period
    # 计算振幅
    df["amplitude"] = ((df["high"] - df["low"]) / df["pre_close"] * 100).round(4)
    # 字段顺序
    cols = ["symbol", "trade_date", "open", "high", "low", "close",
            "pre_close", "change", "pct_chg", "vol", "amount", "amplitude", "period"]
    return df[cols].sort_values("trade_date").reset_index(drop=True)


def standardize_weekly_monthly(df, period):
    """标准化周K/月K字段（字段顺序不同）"""
    df = df.copy()
    if "ts_code" in df.columns:
        df = df.rename(columns={"ts_code": "symbol"})
    df["symbol"] = SYMBOL_DB
    df["period"] = period
    df["amplitude"] = ((df["high"] - df["low"]) / df["pre_close"] * 100).round(4)
    cols = ["symbol", "trade_date", "open", "high", "low", "close",
            "pre_close", "change", "pct_chg", "vol", "amount", "amplitude", "period"]
    return df[cols].sort_values("trade_date").reset_index(drop=True)


def aggregate_quarterly(monthly_df):
    """从月K聚合季K（按自然季度）"""
    monthly_df = monthly_df.copy()
    monthly_df["year"] = monthly_df["trade_date"].str[:4].astype(int)
    monthly_df["month"] = monthly_df["trade_date"].str[4:6].astype(int)
    monthly_df["quarter"] = ((monthly_df["month"] - 1) // 3) + 1

    quarterly_groups = monthly_df.groupby(["year", "quarter"])

    records = []
    for (year, quarter), group in quarterly_groups:
        group = group.sort_values("trade_date")
        record = {
            "symbol": SYMBOL_DB,
            "trade_date": f"{year}0{quarter}01" if quarter < 10 else f"{year}{quarter}01",  # 季度标记
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
        }
        records.append(record)

    return pd.DataFrame(records).sort_values("trade_date").reset_index(drop=True)


def save_parquet(df, path):
    """保存为Parquet"""
    df.to_parquet(path, index=False, engine="pyarrow")
    size_kb = os.path.getsize(path) / 1024
    print(f"  ✅ Parquet: {path} ({size_kb:.1f} KB, {len(df)} rows)")


def save_to_sqlite(df, db_path, table_name):
    """写入SQLite"""
    conn = get_db_conn(db_path, timeout=60)
    # 删除已有数据（避免重复）
    conn.execute(f"DELETE FROM {table_name} WHERE symbol = ?", (SYMBOL_DB,))
    df.to_sql(table_name, conn, if_exists="append", index=False)
    count = conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE symbol = ?", (SYMBOL_DB,)).fetchone()[0]
    conn.close()
    print(f"  ✅ SQLite {table_name}: {count} rows for {SYMBOL_DB}")


def validate(df, period):
    """数据完整性验证"""
    issues = []
    # 检查缺失值
    null_counts = df.isnull().sum()
    null_cols = null_counts[null_counts > 0]
    if len(null_cols) > 0:
        issues.append(f"缺失值: {dict(null_cols)}")
    
    # 检查日期连续性（仅日K）
    if period == "daily" and len(df) > 1:
        dates = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        # 不做严格交易日连续检查（有节假日），只检查有无重复
        dupes = df["trade_date"].duplicated().sum()
        if dupes > 0:
            issues.append(f"重复日期: {dupes}条")
    
    # 检查价格合理性
    if (df["close"] <= 0).any():
        issues.append("存在收盘价<=0的记录")
    if (df["vol"] < 0).any():
        issues.append("存在成交量<0的记录")
    
    if issues:
        print(f"  ⚠️ 验证问题:")
        for issue in issues:
            print(f"     - {issue}")
    else:
        print(f"  ✅ 验证通过 ({period}: {len(df)} rows, {df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]})")
    return len(issues) == 0


def get_db_conn(db_path, timeout=30):
    """获取SQLite连接，带超时重试"""
    return sqlite3.connect(db_path, timeout=timeout)


def create_sqlite_tables(db_path):
    """创建K线表（如不存在）"""
    conn = get_db_conn(db_path)
    cursor = conn.cursor()
    for table in ["kline_daily", "kline_weekly", "kline_monthly", "kline_quarterly"]:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                symbol TEXT,
                trade_date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pre_close REAL,
                change REAL,
                pct_chg REAL,
                vol REAL,
                amount REAL,
                amplitude REAL,
                period TEXT
            )
        """)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_symbol ON {table}(symbol)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table}(trade_date)")
    conn.commit()
    conn.close()
    print("✅ SQLite表结构已就绪")


def main():
    print(f"=" * 60)
    print(f"华钰矿业 ({SYMBOL_DB}) K线数据下载")
    print(f"=" * 60)
    
    # 1. 创建目录和表
    KLINE_DIR.mkdir(parents=True, exist_ok=True)
    create_sqlite_tables(DB_PATH)
    
    # 2. 拉取数据
    print("\n📥 拉取数据...")
    daily_df = fetch_data("daily")
    weekly_df = fetch_data("weekly")
    monthly_df = fetch_data("monthly")
    
    # 3. 标准化
    print("\n🔄 标准化字段...")
    daily_std = standardize_daily(daily_df, "daily")
    weekly_std = standardize_weekly_monthly(weekly_df, "weekly")
    monthly_std = standardize_weekly_monthly(monthly_df, "monthly")
    quarterly_std = aggregate_quarterly(monthly_std)
    
    # 4. 保存Parquet
    print("\n💾 保存Parquet...")
    save_parquet(daily_std, KLINE_DIR / "daily.parquet")
    save_parquet(weekly_std, KLINE_DIR / "weekly.parquet")
    save_parquet(monthly_std, KLINE_DIR / "monthly.parquet")
    save_parquet(quarterly_std, KLINE_DIR / "quarterly.parquet")
    
    # 5. 保存SQLite
    print("\n💾 写入SQLite...")
    save_to_sqlite(daily_std, DB_PATH, "kline_daily")
    save_to_sqlite(weekly_std, DB_PATH, "kline_weekly")
    save_to_sqlite(monthly_std, DB_PATH, "kline_monthly")
    save_to_sqlite(quarterly_std, DB_PATH, "kline_quarterly")
    
    # 6. 验证
    print("\n🔍 数据验证...")
    all_ok = True
    all_ok &= validate(daily_std, "daily")
    all_ok &= validate(weekly_std, "weekly")
    all_ok &= validate(monthly_std, "monthly")
    all_ok &= validate(quarterly_std, "quarterly")
    
    # 7. 摘要
    print(f"\n{'=' * 60}")
    print(f"📊 完成摘要 - {NAME} ({SYMBOL_DB})")
    print(f"{'=' * 60}")
    print(f"  日K:  {len(daily_std)} 条  ({daily_std['trade_date'].iloc[0]} ~ {daily_std['trade_date'].iloc[-1]})")
    print(f"  周K:  {len(weekly_std)} 条")
    print(f"  月K:  {len(monthly_std)} 条")
    print(f"  季K:  {len(quarterly_std)} 条 (从月K聚合)")
    print(f"  存储: {KLINE_DIR}/")
    print(f"  数据库: {DB_PATH} (kline_daily/weekly/monthly/quarterly)")
    
    # 显示最新价格
    latest = daily_std.iloc[-1]
    print(f"\n  最新日K ({latest['trade_date']}): "
          f"收盘 {latest['close']}, 涨跌幅 {latest['pct_chg']:.2f}%, "
          f"振幅 {latest['amplitude']:.2f}%, 成交量 {latest['vol']:.0f}手")
    
    print(f"\n{'✅ 全部完成!' if all_ok else '⚠️ 有验证问题'}")


if __name__ == "__main__":
    main()
