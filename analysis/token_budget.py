#!/usr/bin/env python3
"""
Token预算管理器 — 控制Prompt的数据包大小

核心逻辑：
  - 中文约 1.5 字符/token，英文约 4 字符/token
  - 按优先级分配预算：元信息5% > 概览5% > 线程60% > 用户10% > K线10% > 时段5% > 反馈5%
  - 超预算时按比例截断并标记
"""


class TokenBudget:
    """管理Prompt中各section的token预算。"""

    # 默认预算分配比例（section名 → 百分比）
    DEFAULT_ALLOCATION = {
        "meta": 0.05,
        "overview": 0.05,
        "threads": 0.60,
        "users": 0.10,
        "kline": 0.10,
        "session": 0.05,
        "feedback": 0.05,
    }

    def __init__(self, max_tokens: int = 12000):
        self.max_tokens = max_tokens
        self.used = 0
        self.truncated_sections: list[str] = []
        self.section_tokens: dict[str, int] = {}

    def estimate(self, text: str) -> int:
        """估算文本的token数。中文~1.5 char/token, 英文~4 char/token"""
        cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        en_chars = len(text) - cn_chars
        return int(cn_chars / 1.5 + en_chars / 4) if en_chars > 0 else int(cn_chars / 1.5)

    def remaining(self) -> int:
        """剩余可用token数"""
        return max(0, self.max_tokens - self.used)

    def allocate(self, section: str, ratio: float | None = None) -> int:
        """获取指定section的预算token数"""
        ratio = ratio or self.DEFAULT_ALLOCATION.get(section, 0.05)
        return int(self.max_tokens * ratio)

    def consume(self, text: str, section: str) -> str:
        """消耗预算。如果超预算则按比例截断文本。

        Args:
            text: 待处理的文本
            section: section名称（用于日志和分配比例）

        Returns:
            处理后的文本（可能被截断）
        """
        tokens = self.estimate(text)
        self.section_tokens[section] = tokens

        if self.used + tokens <= self.max_tokens:
            self.used += tokens
            return text

        # 超预算：按比例截断
        budget_left = self.remaining()
        if budget_left <= 0:
            self.truncated_sections.append(section)
            return ""

        ratio = budget_left / tokens
        # 至少保留10%
        ratio = max(0.1, ratio)
        truncated_len = max(1, int(len(text) * ratio))
        truncated_text = text[:truncated_len]
        actual_tokens = self.estimate(truncated_text)

        self.truncated_sections.append(section)
        self.used += actual_tokens
        truncated_text += f"\n[...因token预算截断，原{tokens}tokens压缩至{actual_tokens}tokens]"
        return truncated_text

    def summary(self) -> str:
        """输出预算使用摘要"""
        lines = [f"Token预算: 已用{self.used}/{self.max_tokens}"]
        if self.truncated_sections:
            lines.append(f"被截断的区块: {', '.join(self.truncated_sections)}")
        for section, tokens in sorted(self.section_tokens.items(), key=lambda x: -x[1]):
            pct = tokens / self.max_tokens * 100
            lines.append(f"  {section}: {tokens}tokens ({pct:.1f}%)")
        return "\n".join(lines)
