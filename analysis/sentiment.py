"""
散户情绪分析模块（基础版）。

基于关键词匹配的情绪评分，后续可接入 LLM 进行更精准分析。
当前为预留模块，爬取功能稳定后再启用。
"""

# 正面情绪关键词
POSITIVE_WORDS = [
    "看好", "利好", "大涨", "涨停", "牛", "加仓", "满仓", "抄底",
    "起飞", "暴涨", "突破", "新高", "翻倍", "好消息", "超预期",
    "强势", "龙头", "潜力", "低估", "买入", "建仓", "增持",
    "优质", "价值", "成长", "分红", "回购",
]

# 负面情绪关键词
NEGATIVE_WORDS = [
    "看空", "利空", "大跌", "跌停", "熊", "割肉", "清仓", "减仓",
    "暴跌", "崩盘", "破位", "新低", "腰斩", "坏消息", "不及预期",
    "弱势", "垃圾", "高估", "卖出", "出货", "逃跑", "套牢",
    "亏损", "爆雷", "退市", "造假", "暴雷",
]


def score_text(text: str) -> dict:
    """
    对一段文本进行情绪评分。

    Args:
        text: 纯文本内容

    Returns:
        {
            "positive_count": 正面词命中数,
            "negative_count": 负面词命中数,
            "score": 情绪分数（正面 - 负面）,
            "sentiment": "positive" / "negative" / "neutral"
        }
    """
    if not text:
        return {
            "positive_count": 0,
            "negative_count": 0,
            "score": 0,
            "sentiment": "neutral",
        }

    pos = sum(1 for w in POSITIVE_WORDS if w in text)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text)
    score = pos - neg

    if score > 0:
        sentiment = "positive"
    elif score < 0:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    return {
        "positive_count": pos,
        "negative_count": neg,
        "score": score,
        "sentiment": sentiment,
    }
