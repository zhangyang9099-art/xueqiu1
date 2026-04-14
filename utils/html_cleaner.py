"""
HTML 清洗工具：将雪球帖子/评论的 HTML 内容转为纯文本。
"""

import re
from bs4 import BeautifulSoup


def html_to_text(html_content: str) -> str:
    """
    将 HTML 内容转为纯文本。

    处理逻辑：
    1. 用 BeautifulSoup 解析 HTML
    2. 移除 script / style 标签
    3. 提取文本，合并空白
    4. 处理雪球特有的 $股票代码$ 格式

    Args:
        html_content: 原始 HTML 字符串

    Returns:
        清洗后的纯文本
    """
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "lxml")

    # 移除不需要的标签
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # 将 <br> 和块级元素转为换行
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4"]):
        block.insert_before("\n")
        block.insert_after("\n")

    # 提取链接文本（保留 href 中的股票代码信息）
    for a_tag in soup.find_all("a"):
        href = a_tag.get("href", "")
        text = a_tag.get_text(strip=True)
        # 雪球的股票链接格式
        if "/S/" in href:
            symbol = href.split("/S/")[-1].split("?")[0]
            a_tag.replace_with(f"${symbol}$ ")
        elif text:
            a_tag.replace_with(text)

    # 提取文本
    text = soup.get_text()

    # 清理多余空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return text


def extract_stock_mentions(text: str) -> list[str]:
    """
    从文本中提取提到的股票代码。

    支持格式：$SH600519$、$贵州茅台$、SH600519、SZ000858 等

    Args:
        text: 纯文本内容

    Returns:
        股票代码列表
    """
    patterns = [
        r"\$([A-Z]{2}\d{6})\$",         # $SH600519$
        r"\b(S[HZ]\d{6})\b",            # SH600519
        r"\$([^$]{2,10})\$",            # $贵州茅台$ (股票名称)
    ]

    mentions = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        mentions.extend(matches)

    return list(set(mentions))
