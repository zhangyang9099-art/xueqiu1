"""
Skill API — Claude 程序化调用雪球爬虫的入口。

用法:
  from skill_api import XueqiuSkillAPI
  api = XueqiuSkillAPI()
  api.ensure_token()
  api.add_stock("茅台")
  api.run_scrape()
  result = api.export_json(days=7)
  api.close()
"""

import os
import sys
import yaml

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


class XueqiuSkillAPI:
    """雪球爬虫的程序化接口，供 Claude Skill 或其他自动化工具调用。"""

    def __init__(self, config_path="config.yaml"):
        self.config_path = config_path
        self.config = self._load_config()
        self._components = None

    def _load_config(self):
        path = os.path.join(PROJECT_ROOT, self.config_path)
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _get_components(self):
        if self._components is None:
            from main import init_components, sync_config_to_db
            self._components = init_components(self.config)
            sync_config_to_db(self.config, self._components["db"])
        return self._components

    # ── Token 管理 ──

    def check_token(self) -> bool:
        """检查 token 是否有效。"""
        comp = self._get_components()
        return comp["cookie_manager"].validate(comp["client"])

    def ensure_token(self) -> bool:
        """
        确保 token 有效。无效时提示用户运行 login。

        Returns:
            True 有效, False 无效
        """
        if self.check_token():
            print("✓ Token 有效")
            return True
        else:
            print("✗ Token 无效或已过期")
            print("  请运行: python main.py login")
            return False

    def login(self) -> str | None:
        """启动自动登录流程。"""
        from core.auto_login import auto_login
        return auto_login(self.config_path)

    # ── 股票管理 ──

    def add_stock(self, query: str) -> dict | None:
        """
        用自然语言添加监控股票。

        Args:
            query: 股票名称、简称或代码

        Returns:
            {"symbol": "SH600519", "name": "贵州茅台"} 或 None
        """
        from utils.stock_resolver import resolve_stock

        comp = self._get_components()
        candidates = resolve_stock(query, client=comp.get("client"))

        if not candidates:
            print(f"未找到匹配的股票: {query}")
            return None

        if len(candidates) == 1 or candidates[0][2] == "exact":
            symbol, name, _ = candidates[0]
            comp["db"].upsert_stock(symbol, name)
            print(f"✓ 已添加: {symbol} {name}")
            return {"symbol": symbol, "name": name}

        # 多个候选，返回列表
        print(f"找到多个匹配:")
        from utils.stock_resolver import format_candidates
        print(format_candidates(candidates))
        # 默认选第一个
        symbol, name, _ = candidates[0]
        comp["db"].upsert_stock(symbol, name)
        print(f"✓ 已自动选择第一个: {symbol} {name}")
        return {"symbol": symbol, "name": name}

    def list_stocks(self) -> list:
        """获取当前监控的股票列表。"""
        comp = self._get_components()
        return comp["db"].get_watched_stocks()

    # ── 用户管理 ──

    def search_user(self, query: str) -> list:
        """搜索雪球用户。"""
        from utils.user_resolver import search_xueqiu_user
        comp = self._get_components()
        return search_xueqiu_user(comp["client"], query)

    def add_user(self, user_id: str, screen_name: str = "", note: str = ""):
        """添加跟踪用户。"""
        comp = self._get_components()
        comp["db"].upsert_tracked_user(user_id, screen_name, note)
        print(f"✓ 已添加跟踪用户: {user_id} {screen_name}")

    # ── 爬取 ──

    def run_scrape(self) -> dict:
        """执行完整爬取。"""
        from main import run_full_scrape
        comp = self._get_components()
        run_full_scrape(comp)
        return comp["db"].get_stats()

    def backfill_comments(self, symbol=None, days=7) -> dict:
        """回填评论。"""
        comp = self._get_components()
        return comp["stock_scraper"].backfill_comments(symbol=symbol, days=days)

    # ── 导出 ──

    def export_json(self, symbol=None, days=None) -> list:
        """导出 JSON 快照，返回文件路径列表。"""
        from export.json_exporter import export_json
        comp = self._get_components()
        return export_json(comp["db"], symbol=symbol, days=days)

    def export_csv(self, symbol=None, days=None) -> list:
        """导出层级 CSV。"""
        from export.csv_exporter import export_csv
        comp = self._get_components()
        return export_csv(comp["db"], symbol=symbol, days=days)

    def export_markdown(self, symbol=None, days=None) -> list:
        """导出 Markdown。"""
        from export.markdown_exporter import export_markdown
        comp = self._get_components()
        return export_markdown(comp["db"], symbol=symbol, days=days)

    # ── 状态 ──

    def get_status(self) -> dict:
        """获取系统状态。"""
        comp = self._get_components()
        stats = comp["db"].get_stats()
        stocks = comp["db"].get_watched_stocks()
        users = comp["db"].get_tracked_users()
        return {
            "stats": stats,
            "stocks": stocks,
            "users": users,
        }

    # ── 清理 ──

    def close(self):
        """释放资源。"""
        if self._components:
            try:
                self._components["client"].close()
            except Exception:
                pass
            try:
                self._components["db"].close()
            except Exception:
                pass
            self._components = None
