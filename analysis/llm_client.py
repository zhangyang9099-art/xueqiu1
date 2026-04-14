#!/usr/bin/env python3
"""
LLM API客户端 v2 — 双客户端架构

1. LLMClient: 同步requests，用于Chain分析（保留V4，不改动）
   - 分步temperature: step1=0.15, step2=0.35, step3=0.45
   - JSON输出自动提取
   - 3次重试 + 指数退避

2. AnnotatorClient: openai库 + response_format=json_object，用于批量标注
   - response_format={"type": "json_object"} 保证JSON输出
   - 成本追踪（input/output tokens）
   - 批量调用优化（低temperature=0.1，高一致性）
   - 支持 OpenAI / DeepSeek / Ollama（全部走 OpenAI 兼容接口）
"""

import json
import re
import time
from typing import Any, Optional

import requests


# ═══════════════════════════════════════════════════
# 原有 LLMClient（Chain分析用，V4保留不动）
# ═══════════════════════════════════════════════════

class TokenBudgetExceeded(Exception):
    """Prompt超过token预算导致超时"""
    pass


class LLMError(Exception):
    """LLM API调用失败"""
    pass


class LLMClient:
    """同步LLM API客户端（Chain分析用）。"""

    STEP_TEMPERATURES = {
        "step1": 0.15,
        "step2": 0.35,
        "step3": 0.45,
        "default": 0.3,
    }

    def __init__(self, config: dict):
        self.base_url = config["base_url"].rstrip("/")
        self.api_key = config["api_key"]
        self.model = config.get("model", "deepseek-chat")
        self.timeout = config.get("timeout_seconds", 120)
        self.max_tokens = config.get("max_tokens", 8000)
        self.max_retries = config.get("max_retries", 3)

    def call(self, prompt: str, step: str = "default",
             expect_json: bool = False, system_prompt: str = "") -> Any:
        temperature = self.STEP_TEMPERATURES.get(step, 0.3)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": self.max_tokens,
                    },
                    timeout=self.timeout,
                )

                if resp.status_code == 429:
                    wait = int(resp.headers.get("retry-after", 60))
                    print(f"  ⚠ API限流(429)，等待{wait}秒后重试...")
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    raise LLMError(f"API返回{resp.status_code}: {resp.text[:500]}")

                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                if expect_json:
                    extracted = self._extract_json(content)
                    try:
                        return json.loads(extracted)
                    except json.JSONDecodeError as e:
                        if attempt < self.max_retries - 1:
                            fix_prompt = (
                                f"你上次的输出JSON格式错误: {e}\n"
                                f"请严格重新输出JSON，不要添加任何markdown包裹。"
                            )
                            messages.append({"role": "assistant", "content": content})
                            messages.append({"role": "user", "content": fix_prompt})
                            continue
                        raise LLMError(f"JSON解析失败(3次重试后): {e}\n原始输出: {content[:500]}")

                return content

            except requests.Timeout:
                if self._estimate_tokens(prompt) > 10000:
                    raise TokenBudgetExceeded(
                        f"Prompt过长（约{self._estimate_tokens(prompt)}tokens）导致超时"
                    )
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMError("请求超时（3次重试后）")

            except requests.ConnectionError as e:
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMError(f"连接失败: {e}")

            except json.JSONDecodeError:
                raise LLMError("API返回非JSON格式")

    def call_streaming(self, prompt: str, step: str = "default",
                       system_prompt: str = "") -> str:
        temperature = self.STEP_TEMPERATURES.get(step, 0.3)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            with requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": self.max_tokens,
                    "stream": True,
                },
                timeout=self.timeout,
                stream=True,
            ) as resp:
                if resp.status_code == 429:
                    wait = int(resp.headers.get("retry-after", 60))
                    time.sleep(wait)
                    return self.call(prompt, step, system_prompt=system_prompt)
                if resp.status_code != 200:
                    raise LLMError(f"API返回{resp.status_code}: {resp.text[:500]}")

                full_content = []
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            print(content, end="", flush=True)
                            full_content.append(content)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                print()
                return "".join(full_content)

        except requests.Timeout:
            raise LLMError("流式请求超时")

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start) if "```" in text[start:] else len(text)
            return text[start:end].strip()
        if "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start) if "```" in text[start:] else len(text)
            return text[start:end].strip()
        for i, c in enumerate(text):
            if c in "{[":
                return self._find_balanced(text, i)
        return text

    def _find_balanced(self, text: str, start: int) -> str:
        if start >= len(text):
            return text
        open_char = text[start]
        close_char = "}" if open_char == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return text[start:]

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        en = len(text) - cn
        return int(cn / 1.5 + en / 4) if en > 0 else int(cn / 1.5)


def get_llm_client(config: dict) -> Optional[LLMClient]:
    """从config.yaml创建LLM客户端（Chain分析用）。"""
    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("api_key"):
        return None
    return LLMClient(llm_cfg)


# ═══════════════════════════════════════════════════
# 新增 AnnotatorClient（批量标注用）
# ═══════════════════════════════════════════════════

class AnnotatorError(Exception):
    """批量标注调用失败"""
    pass


class AnnotatorClient:
    """基于 openai 库的 LLM 客户端，用于批量标注。

    特点:
    - 使用 openai 库（支持 response_format=json_object）
    - 低 temperature=0.1（标注任务需要高一致性）
    - 内置成本追踪（total_input_tokens / total_output_tokens）
    - 自动重试 + 限流处理
    - 支持 DeepSeek / OpenAI / Ollama（OpenAI 兼容接口）
    """

    def __init__(self, config: dict):
        """
        Args:
            config: config.yaml 中 llm 段的字典
        """
        self.provider = config.get("provider", "deepseek")
        self.model = config.get("model", "deepseek-chat")
        self.max_retries = config.get("max_retries", 3)
        self.timeout = config.get("timeout_seconds", 120)
        self.temperature = config.get("annotate_temperature", 0.1)

        # API Key: 优先从 config.yaml 读取，也支持环境变量
        import os
        api_key_env = config.get("api_key_env", "")
        self.api_key = config.get("api_key", "") or (
            os.environ.get(api_key_env) if api_key_env else ""
        )

        if not self.api_key:
            raise ValueError(
                "未配置 API Key。请在 config.yaml 的 llm 段设置 api_key。"
            )

        # base_url
        self.base_url = config.get("base_url")
        if self.provider == "deepseek" and not self.base_url:
            self.base_url = "https://api.deepseek.com"
        elif self.provider == "ollama" and not self.base_url:
            self.base_url = "http://localhost:11434/v1"
        elif not self.base_url:
            self.base_url = "https://api.openai.com/v1"

        # 初始化 OpenAI 客户端
        from openai import OpenAI
        client_kwargs = {"api_key": self.api_key, "timeout": self.timeout}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self.client = OpenAI(**client_kwargs)

        # 成本追踪
        self.total_calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def annotate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = 4000) -> Optional[dict]:
        """发送标注请求，期望返回 JSON dict。

        使用 response_format=json_object 确保 LLM 输出合法 JSON。

        Args:
            system_prompt: 系统提示词（定义标注规则）
            user_prompt: 用户消息（待标注的文本数据）
            max_tokens: 最大输出 token 数

        Returns:
            解析后的 dict，失败返回 None
        """
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                )

                # 记录用量
                self.total_calls += 1
                if response.usage:
                    self.total_input_tokens += response.usage.prompt_tokens
                    self.total_output_tokens += response.usage.completion_tokens

                content = response.choices[0].message.content.strip()

                try:
                    return json.loads(content)
                except json.JSONDecodeError as e:
                    if attempt < self.max_retries - 1:
                        print(f"  ⚠ JSON解析失败({e})，重试中...")
                        time.sleep(1)
                        continue
                    print(f"  ✗ JSON解析最终失败: {e}")
                    print(f"    原始输出: {content[:500]}")
                    return None

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "rate" in err_str.lower():
                    wait = 30 + attempt * 30
                    print(f"  ⚠ API限流，等待{wait}秒...")
                    time.sleep(wait)
                    continue
                if attempt < self.max_retries - 1:
                    print(f"  ⚠ 调用失败({e})，{2 ** attempt}秒后重试...")
                    time.sleep(2 ** attempt)
                    continue
                raise AnnotatorError(f"标注调用失败(3次重试后): {e}")

        return None

    def get_cost_summary(self) -> dict:
        """返回成本摘要"""
        # DeepSeek 定价估算（input $0.14/M, output $0.28/M）
        # OpenAI gpt-4o-mini 定价估算（input $0.15/M, output $0.6/M）
        return {
            "total_calls": self.total_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "model": self.model,
            "provider": self.provider,
        }


def get_annotator_client(config: dict) -> Optional[AnnotatorClient]:
    """从 config.yaml 创建 AnnotatorClient（批量标注用）。
    
    支持两种 API Key 来源：
    1. config.yaml 的 llm.api_key（传统方式）
    2. 通过 analysis_profile 合并后的 llm.api_key（新方式）
    """
    llm_cfg = config.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    
    # 如果没有 key，尝试从环境变量获取
    if not api_key:
        import os
        env_name = llm_cfg.get("api_key_env", "")
        if env_name:
            api_key = os.environ.get(env_name, "")
    
    if not api_key:
        return None
    
    # 构建临时配置
    init_cfg = dict(llm_cfg)
    init_cfg["api_key"] = api_key
    
    try:
        return AnnotatorClient(init_cfg)
    except ValueError as e:
        print(f"[错误] {e}")
        return None
