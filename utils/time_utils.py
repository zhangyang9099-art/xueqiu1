"""
时间工具模块：毫秒时间戳转换、交易日判断、市场阶段识别。

所有时间均为北京时间 (UTC+8)。
"""

from datetime import datetime, timezone, timedelta, date

CST = timezone(timedelta(hours=8))

# 2026 年 A 股休市日（元旦、春节、清明、劳动节、端午、中秋、国庆）
# 注意：每年需要更新。如果日期不在此表中且是工作日，默认视为交易日。
# 来源：国务院办公厅假日安排（2026年版需发布后更新，以下为预估）
HOLIDAYS_2026 = {
    # 元旦
    date(2026, 1, 1), date(2026, 1, 2),
    # 春节（预估：2026年春节为2月17日）
    date(2026, 2, 15), date(2026, 2, 16), date(2026, 2, 17),
    date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21),
    # 清明
    date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
    # 劳动节
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
    date(2026, 5, 4), date(2026, 5, 5),
    # 端午
    date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 2),
    # 中秋 + 国庆
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3),
    date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6),
    date(2026, 10, 7), date(2026, 10, 8),
}

# 2026 年调休上班日（周末但需要上班/交易的日子）
WORKDAYS_2026 = {
    date(2026, 2, 14),  # 春节调休
    date(2026, 2, 22),  # 春节调休
    date(2026, 10, 10), # 国庆调休
}


def ms_to_datetime(ms_timestamp: int) -> datetime | None:
    """毫秒时间戳 → datetime (北京时间)"""
    if not ms_timestamp:
        return None
    try:
        return datetime.fromtimestamp(ms_timestamp / 1000, tz=CST)
    except (OSError, ValueError):
        return None


def ms_to_str(ms_timestamp: int) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'"""
    dt = ms_to_datetime(ms_timestamp)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def ms_to_date_str(ms_timestamp: int) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD'"""
    dt = ms_to_datetime(ms_timestamp)
    return dt.strftime("%Y-%m-%d") if dt else ""


def is_trading_day(d) -> bool:
    """判断某天是否为 A 股交易日"""
    if isinstance(d, datetime):
        d = d.date() if hasattr(d, 'date') else d
    if isinstance(d, date):
        # 调休上班日 → 交易日
        if d in WORKDAYS_2026:
            return True
        # 法定假日 → 非交易日
        if d in HOLIDAYS_2026:
            return False
        # 周末 → 非交易日
        if d.weekday() >= 5:
            return False
        return True
    return False


def get_market_phase(ms_timestamp: int) -> str:
    """
    判断市场阶段。

    Returns:
        'pre_market'  — 交易日开盘前 (00:00 ~ 09:29)
        'in_market'   — 交易日盘中 (09:30 ~ 15:00)
        'post_market' — 交易日盘后 (15:01 ~ 23:59)
        'non_trading' — 非交易日
        'unknown'     — 无法判断
    """
    dt = ms_to_datetime(ms_timestamp)
    if not dt:
        return "unknown"

    if not is_trading_day(dt):
        return "non_trading"

    minutes_of_day = dt.hour * 60 + dt.minute

    if minutes_of_day < 9 * 60 + 30:
        return "pre_market"
    elif minutes_of_day <= 15 * 60:
        return "in_market"
    else:
        return "post_market"


def market_phase_cn(phase: str) -> str:
    """市场阶段英文 → 中文"""
    return {
        "pre_market": "盘前",
        "in_market": "盘中",
        "post_market": "盘后",
        "non_trading": "非交易日",
        "unknown": "未知",
    }.get(phase, phase)
