#!/usr/bin/env python3
"""
可执行JSON Schema + 校验器 — 用于Chain中间数据格式校验

设计原则:
  - 自定义轻量格式，非标准JSON Schema（太重）
  - enum做大小写容忍（LLM常输出 "Bullish" 而非 "bullish"）
  - 非 required 字段缺失不报错
  - 递归校验list/dict嵌套结构
"""

from typing import Any


# ──────── Step 1: 感知层 Schema ────────

STEP1_SCHEMA = {
    "threads": {
        "_type": "list",
        "_item": {
            "thread_id": {"type": "str", "required": True},
            "sentiment": {
                "type": "enum",
                "values": ["bullish", "bearish", "neutral", "divided"],
                "required": True,
            },
            "strength": {"type": "int", "min": 1, "max": 5, "required": True},
            "intent": {
                "type": "enum",
                "values": ["genuine", "manipulation", "contrarian", "venting"],
            },
            "sarcasm": {"type": "bool", "default": False},
            "key_argument": {"type": "str", "max_length": 300},
            "evidence_quality": {
                "type": "enum",
                "values": ["high", "medium", "low"],
            },
            "suspicious_users": {
                "type": "list",
                "_item": {"type": "str"},
            },
        },
    },
    "overall_sentiment": {
        "label": {"type": "str", "required": True},
        "strength": {"type": "int", "min": 1, "max": 5},
        "confidence": {
            "type": "enum",
            "values": ["high", "medium", "low"],
        },
    },
}


# ──────── Step 2: 判断层 Schema ────────

STEP2_SCHEMA = {
    "price_sentiment_alignment": {
        "type": "enum",
        "values": ["aligned", "diverged_bullish", "diverged_bearish", "no_kline_data"],
    },
    "sentiment_leading": {
        "type": "enum",
        "values": ["yes_leading", "no_lagging", "unclear"],
    },
    "lead_days": {"type": "int", "min": 0, "max": 30},
    "manipulation_risk_score": {"type": "int", "min": 0, "max": 100},
    "manipulation_signals": {
        "type": "list",
        "_item": {"type": "str"},
    },
    "key_price_events": {
        "type": "list",
        "_item": {
            "date": {"type": "str"},
            "event": {"type": "str"},
            "sentiment_at_time": {"type": "str"},
        },
    },
    "session_analysis": {
        "type": "dict",
        "_item": {
            "session": {"type": "str"},
            "dominant_sentiment": {"type": "str"},
            "note": {"type": "str"},
        },
    },
    "summary": {"type": "str", "max_length": 500},
}


# ──────── 校验函数 ────────

def validate_step_output(data: Any, schema: dict) -> tuple[bool, list[str]]:
    """递归校验LLM输出是否符合预期schema。

    Args:
        data: LLM输出的数据（通常已json.loads）
        schema: 可执行的schema定义

    Returns:
        (是否通过, 错误列表)
    """
    errors: list[str] = []

    def _validate(obj: Any, spec: Any, path: str = ""):
        """递归校验"""
        if spec is None or obj is None:
            return

        # list类型
        if isinstance(spec, dict) and spec.get("_type") == "list":
            if not isinstance(obj, list):
                errors.append(f"{path}: 期望list，实际{type(obj).__name__}")
                return
            item_spec = spec.get("_item", {})
            for i, item in enumerate(obj):
                _validate(item, item_spec, f"{path}[{i}]")
            return

        # dict类型（可以是嵌套schema或带_type的dict）
        if isinstance(spec, dict):
            # 检查_type字段是否匹配
            spec_type = spec.get("_type")
            if spec_type == "dict":
                if not isinstance(obj, dict):
                    errors.append(f"{path}: 期望dict，实际{type(obj).__name__}")
                    return
                item_spec = spec.get("_item", {})
                for key, val in obj.items():
                    _validate(val, item_spec, f"{path}.{key}")
                return

            if not isinstance(obj, dict):
                errors.append(f"{path}: 期望dict，实际{type(obj).__name__}")
                return

            for key, rules in spec.items():
                if key.startswith("_"):
                    continue  # 跳过_type, _item等元字段
                if not isinstance(rules, dict):
                    continue

                required = rules.get("required", False)

                if key not in obj:
                    if required:
                        errors.append(f"{path}.{key}: 必填字段缺失")
                    continue

                _validate_field(obj[key], rules, f"{path}.{key}")

    def _validate_field(value: Any, rules: dict, path: str):
        """校验单个字段"""
        field_type = rules.get("type")

        if field_type == "enum":
            valid_values = rules.get("values", [])
            if value is not None:
                # 大小写容忍
                value_str = str(value).lower()
                valid_lower = [str(v).lower() for v in valid_values]
                if value_str not in valid_lower:
                    errors.append(
                        f"{path}: '{value}' 不在允许值 {valid_values} 中"
                    )

        elif field_type == "int":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{path}: 期望int，实际{type(value).__name__}")
            else:
                min_val = rules.get("min")
                max_val = rules.get("max")
                if min_val is not None and value < min_val:
                    errors.append(f"{path}: 值{value}小于最小值{min_val}")
                if max_val is not None and value > max_val:
                    errors.append(f"{path}: 值{value}大于最大值{max_val}")

        elif field_type == "str":
            if not isinstance(value, str):
                errors.append(f"{path}: 期望str，实际{type(value).__name__}")
            else:
                max_length = rules.get("max_length")
                if max_length and len(value) > max_length:
                    errors.append(f"{path}: 长度{len(value)}超过最大值{max_length}")

        elif field_type == "bool":
            if not isinstance(value, bool):
                errors.append(f"{path}: 期望bool，实际{type(value).__name__}")

        elif field_type == "list":
            _validate(value, rules, path)

        elif field_type == "dict":
            _validate(value, rules, path)

    _validate(data, schema)
    return len(errors) == 0, errors


def compress_step1_for_step2(step1_output: dict) -> dict:
    """压缩Step1输出，只保留Step2需要的字段，去除长文本。

    Step2只需要情绪结论和可疑用户列表，不需要key_argument等长文本。
    """
    all_suspicious = set()
    thread_sentiments = []

    for t in step1_output.get("threads", []):
        suspicious = t.get("suspicious_users") or []
        all_suspicious.update(suspicious)
        thread_sentiments.append({
            "thread_id": t.get("thread_id", ""),
            "sentiment": t.get("sentiment", "neutral"),
            "strength": t.get("strength", 3),
            "sarcasm": t.get("sarcasm", False),
            "suspicious_users": suspicious,
        })

    return {
        "overall_sentiment": step1_output.get("overall_sentiment"),
        "thread_sentiments": thread_sentiments,
        "total_suspicious_count": len(all_suspicious),
        "total_threads_analyzed": len(step1_output.get("threads", [])),
    }
