#!/usr/bin/env python3
"""
持久化配置管理器

首次运行时交互式设置，之后自动沿用。
用户可通过 python main.py config 重新进入设置。

配置持久化到 data/analysis_profile.yaml
与 config.yaml 的关系：
  - config.yaml: 系统级配置（数据库路径、爬虫参数等），手工编辑
  - analysis_profile.yaml: 分析运行时配置（模型、目标、参数），交互式设置
"""

import os
import yaml
from datetime import datetime
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_PATH = os.path.join(PROJECT_ROOT, "data", "analysis_profile.yaml")

# 支持的LLM提供商预设
PROVIDER_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "annotate_model": "deepseek-chat",
        "briefing_model": "deepseek-chat",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "annotate_model": "gpt-4o-mini",
        "briefing_model": "gpt-4o",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:14b",
        "api_key_env": "",
        "annotate_model": "qwen2.5:14b",
        "briefing_model": "qwen2.5:14b",
    },
    "custom": {
        "base_url": "",
        "model": "",
        "api_key_env": "",
        "annotate_model": "",
        "briefing_model": "",
    },
}

DEFAULT_PROFILE = {
    "version": 2,
    "created_at": "",
    "updated_at": "",

    # LLM配置
    "llm": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",  # 直接存储，或从环境变量读
        "api_key_env": "DEEPSEEK_API_KEY",
        "annotate_model": "deepseek-chat",   # 标注用：便宜、快
        "briefing_model": "deepseek-chat",    # 研判用：可以用更贵的模型
        "max_retries": 3,
        "timeout_seconds": 120,
    },

    # 分析范围
    "scope": {
        "mode": "watchlist",         # watchlist=自选股/ custom=自定义列表
        "custom_symbols": [],        # mode=custom时生效
        "exclude_symbols": [],       # 排除的股票
        "min_comments_threshold": 10, # 评论数低于此值的股票跳过深度研判
        "kol_min_followers": 5000,   # KOL识别的最低粉丝数
    },

    # 时间范围
    "time": {
        "scan_days": 7,              # 扫描最近N天数据
        "baseline_period": 30,       # 基准线计算周期
        "baseline_update_interval": 7, # 基准线更新间隔（天）
    },

    # 输出配置
    "output": {
        "mode": "terminal",          # terminal / file / both
        "terminal_width": 80,        # 终端输出宽度
        "top_n_findings": 5,         # 展示TOP-N个发现
        "top_n_content": 3,          # 展示TOP-N条推荐阅读
        "show_cost": True,           # 是否显示API成本
        "save_report": True,         # 是否同时保存报告文件
        "report_dir": "data/daily-reports",
    },

    # 管线控制
    "pipeline": {
        "auto_annotate": True,       # 自动标注未标注评论
        "auto_baseline": True,       # 自动更新过期基准线
        "auto_kol_update": True,     # 自动更新KOL评级
        "run_deep_briefing": True,   # 是否运行LLM深度研判
        "run_cross_stock": True,     # 是否运行跨票综合研判
        "max_deep_briefing_stocks": 10, # 最多对几只票做深度研判
    },
}


def load_profile() -> dict:
    """加载分析配置。不存在则返回None。"""
    if not os.path.exists(PROFILE_PATH):
        return None
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_profile(profile: dict):
    """保存分析配置"""
    profile["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        yaml.dump(profile, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)


def get_api_key(profile: dict) -> str:
    """获取API Key：优先profile中的直接值，其次环境变量"""
    llm = profile.get("llm", {})
    key = llm.get("api_key", "")
    if key:
        return key
    env_name = llm.get("api_key_env", "")
    if env_name:
        return os.environ.get(env_name, "")
    return ""


def ensure_profile(force_interactive: bool = False) -> dict:
    """确保配置存在。首次运行或force_interactive=True时进入交互式设置。

    Returns:
        完整的profile字典
    """
    profile = load_profile()

    if profile and not force_interactive:
        # 验证API Key可用
        api_key = get_api_key(profile)
        if api_key:
            return profile
        else:
            print("⚠️  API Key 未设置或已过期，需要重新配置。\n")

    # 交互式设置
    return _interactive_setup(profile)


def _interactive_setup(existing: Optional[dict] = None) -> dict:
    """交互式配置设置"""
    profile = existing or dict(DEFAULT_PROFILE)
    if not existing:
        profile["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 55)
    print("  舆情分析系统 — 运行配置")
    print("=" * 55)

    # 1. LLM提供商
    print("\n📡 LLM提供商")
    print("  1. DeepSeek（推荐，便宜）")
    print("  2. OpenAI")
    print("  3. Ollama（本地）")
    print("  4. 自定义（其他OpenAI兼容接口）")

    current = profile.get("llm", {}).get("provider", "deepseek")
    choice_map = {"1": "deepseek", "2": "openai", "3": "ollama", "4": "custom"}
    reverse_map = {v: k for k, v in choice_map.items()}
    default_num = reverse_map.get(current, "1")

    try:
        choice = input(f"  选择 [{default_num}]: ").strip() or default_num
    except (EOFError, KeyboardInterrupt):
        print("\n使用默认配置。")
        choice = default_num

    provider = choice_map.get(choice, current)
    preset = PROVIDER_PRESETS.get(provider, {})

    profile.setdefault("llm", {})
    profile["llm"]["provider"] = provider

    if provider == "custom":
        try:
            base_url = input(f"  API Base URL: ").strip()
            model = input(f"  模型名称: ").strip()
        except (EOFError, KeyboardInterrupt):
            base_url, model = "", ""
        profile["llm"]["base_url"] = base_url or "https://api.openai.com/v1"
        profile["llm"]["annotate_model"] = model or "gpt-4o-mini"
        profile["llm"]["briefing_model"] = model or "gpt-4o-mini"
    else:
        profile["llm"]["base_url"] = preset["base_url"]
        profile["llm"]["annotate_model"] = preset["annotate_model"]
        profile["llm"]["briefing_model"] = preset["briefing_model"]

    # 2. API Key
    if provider != "ollama":
        current_key = profile["llm"].get("api_key", "")
        masked = f"...{current_key[-8:]}" if current_key and len(current_key) > 8 else "(未设置)"
        print(f"\n🔑 API Key（当前: {masked}）")
        try:
            key_input = input("  输入新Key（直接回车保留当前）: ").strip()
        except (EOFError, KeyboardInterrupt):
            key_input = ""
        if key_input:
            profile["llm"]["api_key"] = key_input
    else:
        profile["llm"]["api_key"] = "ollama"

    # 3. 分析范围
    print(f"\n📊 分析范围")
    print(f"  当前: 自选股全部（排除列表可在配置文件中编辑）")
    # 这里不做复杂交互，默认watchlist全部

    # 4. 确认
    print(f"\n✅ 配置完成:")
    print(f"  提供商: {provider}")
    print(f"  标注模型: {profile['llm']['annotate_model']}")
    print(f"  研判模型: {profile['llm']['briefing_model']}")
    print(f"  配置文件: {PROFILE_PATH}")

    save_profile(profile)
    return profile


def merge_with_system_config(profile: dict, config: dict) -> dict:
    """将analysis_profile与config.yaml合并，profile优先"""
    merged = dict(config)

    # 覆盖llm配置
    merged["llm"] = dict(config.get("llm", {}))
    llm_profile = profile.get("llm", {})

    merged["llm"]["provider"] = llm_profile.get("provider", "deepseek")
    merged["llm"]["base_url"] = llm_profile.get("base_url", "")
    merged["llm"]["model"] = llm_profile.get("annotate_model", "deepseek-chat")
    merged["llm"]["api_key"] = get_api_key(profile)
    merged["llm"]["timeout_seconds"] = llm_profile.get("timeout_seconds", 120)
    merged["llm"]["max_retries"] = llm_profile.get("max_retries", 3)

    # 覆盖analysis配置
    merged["analysis"] = dict(config.get("analysis", {}))
    time_cfg = profile.get("time", {})
    merged["analysis"]["default_days"] = time_cfg.get("scan_days", 7)

    return merged
