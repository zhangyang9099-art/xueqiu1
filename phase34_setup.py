#!/usr/bin/env python3
"""
阶段三 + 阶段四 安装脚本

阶段三: 智能参数输入
  - python main.py login → 弹出浏览器让用户登录，自动获取 token
  - python main.py add-stock 茅台 → 自然语言识别股票代码
  - python main.py add-user 但斌 → 搜索雪球用户

阶段四: Claude Skill 封装
  - SKILL.md → Skill 定义文件
  - skill_api.py → Claude 调用入口

用法:
  cd ~/Desktop/xueqiu-scraper
  source venv/bin/activate
  python phase34_setup.py
"""

import os
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def backup(filepath):
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{filepath}.bak_{ts}"
        shutil.copy2(filepath, bak)
        print(f"  备份: {os.path.basename(bak)}")


def write_file(rel_path, content, desc):
    full = os.path.join(PROJECT_ROOT, rel_path)
    d = os.path.dirname(full)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ {rel_path} ({desc})")


# ================================================================
# utils/stock_resolver.py
# ================================================================

STOCK_RESOLVER = r'''"""
股票名称解析器：自然语言 → 股票代码。

支持:
  - 全称: "贵州茅台" → SH600519
  - 简称: "茅台" → SH600519
  - 纯数字: "600519" → SH600519
  - 模糊匹配: "宁德" → SZ300750 宁德时代
  - 雪球搜索兜底: 本地找不到时调用雪球接口
"""

# 常用 A 股映射表（约 200 只热门股票）
# 格式: (代码, 全称, 简称/别称列表)
STOCK_MAP = [
    ("SH600519", "贵州茅台", ["茅台", "贵茅"]),
    ("SZ000858", "五粮液", ["五粮"]),
    ("SZ300750", "宁德时代", ["宁德", "CATL"]),
    ("SH601318", "中国平安", ["平安"]),
    ("SH600036", "招商银行", ["招行", "招商"]),
    ("SZ000333", "美的集团", ["美的"]),
    ("SH600900", "长江电力", ["长电"]),
    ("SZ002594", "比亚迪", ["比亚迪", "BYD"]),
    ("SH601888", "中国中免", ["中免", "中国中免"]),
    ("SH600276", "恒瑞医药", ["恒瑞"]),
    ("SZ000651", "格力电器", ["格力"]),
    ("SH601012", "隆基绿能", ["隆基"]),
    ("SH600809", "山西汾酒", ["汾酒"]),
    ("SZ000568", "泸州老窖", ["老窖", "泸州"]),
    ("SH603259", "药明康德", ["药明"]),
    ("SH600030", "中信证券", ["中信"]),
    ("SZ002714", "牧原股份", ["牧原"]),
    ("SH601899", "紫金矿业", ["紫金"]),
    ("SH600887", "伊利股份", ["伊利"]),
    ("SZ300059", "东方财富", ["东财", "东方财富"]),
    ("SH601166", "兴业银行", ["兴业"]),
    ("SZ002475", "立讯精密", ["立讯"]),
    ("SH600309", "万华化学", ["万华"]),
    ("SH601398", "工商银行", ["工行"]),
    ("SH601939", "建设银行", ["建行"]),
    ("SH601288", "农业银行", ["农行"]),
    ("SH601988", "中国银行", ["中行"]),
    ("SH600000", "浦发银行", ["浦发"]),
    ("SH600016", "民生银行", ["民生"]),
    ("SH601668", "中国建筑", ["中建"]),
    ("SH601857", "中国石油", ["中石油", "石油"]),
    ("SH600028", "中国石化", ["中石化", "石化"]),
    ("SH601088", "中国神华", ["神华"]),
    ("SH600585", "海螺水泥", ["海螺"]),
    ("SZ002304", "洋河股份", ["洋河"]),
    ("SH600588", "用友网络", ["用友"]),
    ("SZ000001", "平安银行", ["平银"]),
    ("SZ002415", "海康威视", ["海康"]),
    ("SZ000725", "京东方A", ["京东方", "BOE"]),
    ("SH601919", "中远海控", ["中远", "海控"]),
    ("SH600050", "中国联通", ["联通"]),
    ("SH600104", "上汽集团", ["上汽"]),
    ("SZ002352", "顺丰控股", ["顺丰"]),
    ("SH688981", "中芯国际", ["中芯"]),
    ("SH688111", "金山办公", ["金山"]),
    ("SZ300124", "汇川技术", ["汇川"]),
    ("SZ300015", "爱尔眼科", ["爱尔"]),
    ("SH600690", "海尔智家", ["海尔"]),
    ("SZ002230", "科大讯飞", ["讯飞", "科大"]),
    ("SH603288", "海天味业", ["海天"]),
    ("SH600031", "三一重工", ["三一"]),
    ("SZ000002", "万科A", ["万科"]),
    ("SH600048", "保利发展", ["保利"]),
    ("SZ002142", "宁波银行", ["宁波银行", "宁行"]),
    ("SH601669", "中国电建", ["电建"]),
    ("SH688012", "中微公司", ["中微"]),
    ("SH688036", "传音控股", ["传音"]),
    ("SZ002602", "世纪华通", ["华通"]),
    ("SZ300760", "迈瑞医疗", ["迈瑞"]),
    ("SH600941", "中国移动", ["移动"]),
    ("SH601728", "中国电信", ["电信"]),
]


def resolve_stock(query: str, client=None) -> list:
    """
    解析股票名称，返回候选列表。

    Args:
        query: 用户输入（股票名称、简称、代码等）
        client: XueqiuClient 实例（用于雪球搜索兜底，可选）

    Returns:
        [(symbol, name, match_type), ...]
        match_type: "exact" / "alias" / "fuzzy" / "search"
    """
    query = query.strip()
    if not query:
        return []

    results = []

    # 1. 纯数字 → 自动补前缀
    if query.isdigit() and len(query) == 6:
        prefix = "SH" if query[0] in ("6", "5", "9") else "SZ"
        symbol = prefix + query
        # 查本地表确认
        for code, name, aliases in STOCK_MAP:
            if code == symbol:
                return [(code, name, "exact")]
        # 本地没有，但格式正确
        return [(symbol, "", "code")]

    # 2. 已经是完整代码格式
    upper = query.upper()
    if (upper.startswith("SH") or upper.startswith("SZ")) and len(upper) == 8:
        for code, name, aliases in STOCK_MAP:
            if code == upper:
                return [(code, name, "exact")]
        return [(upper, "", "code")]

    # 3. 全称精确匹配
    for code, name, aliases in STOCK_MAP:
        if query == name:
            results.append((code, name, "exact"))
            return results

    # 4. 别称匹配
    for code, name, aliases in STOCK_MAP:
        if query in aliases:
            results.append((code, name, "alias"))
    if results:
        return results

    # 5. 模糊匹配（包含关系）
    for code, name, aliases in STOCK_MAP:
        if query in name or name in query:
            results.append((code, name, "fuzzy"))
        else:
            for alias in aliases:
                if query in alias or alias in query:
                    results.append((code, name, "fuzzy"))
                    break
    if results:
        return results

    # 6. 雪球搜索兜底
    if client:
        try:
            search_results = _search_xueqiu_stock(client, query)
            return search_results
        except Exception:
            pass

    return []


def _search_xueqiu_stock(client, query):
    """调用雪球搜索接口查找股票。"""
    try:
        data = client.get(
            "https://xueqiu.com/stock/search.json",
            params={"code": query, "size": 5},
            referer_path="/",
        )
        stocks = data.get("stocks", [])
        results = []
        for s in stocks:
            code = s.get("code", "")
            name = s.get("name", "")
            if code:
                results.append((code, name, "search"))
        return results
    except Exception:
        return []


def format_candidates(candidates: list) -> str:
    """格式化候选列表供用户选择。"""
    if not candidates:
        return "未找到匹配的股票"
    lines = []
    for i, (code, name, mtype) in enumerate(candidates, 1):
        tag = {"exact": "精确", "alias": "别称", "fuzzy": "模糊",
               "search": "搜索", "code": "代码"}.get(mtype, "")
        lines.append(f"  {i}. {code} {name} [{tag}匹配]")
    return "\n".join(lines)
'''


# ================================================================
# utils/user_resolver.py
# ================================================================

USER_RESOLVER = r'''"""
用户搜索器：通过雪球搜索接口查找用户。

用法:
  results = search_xueqiu_user(client, "但斌")
  → [{"id": "1247347556", "name": "但斌", "followers": 12000000}, ...]
"""


def search_xueqiu_user(client, query: str, count: int = 5) -> list:
    """
    搜索雪球用户。

    Args:
        client: XueqiuClient 实例
        query: 搜索关键词（用户名）
        count: 返回数量

    Returns:
        [{"id", "name", "description", "followers_count"}, ...]
    """
    try:
        data = client.get(
            "https://xueqiu.com/statuses/search.json",
            params={"q": query, "count": count, "comment": 0, "symbol": 0,
                    "user": 1, "page": 1},
            referer_path="/",
        )
        users = data.get("users", [])
        results = []
        for u in users:
            results.append({
                "id": str(u.get("id", "")),
                "name": u.get("screen_name", ""),
                "description": (u.get("description") or "")[:60],
                "followers_count": u.get("followers_count", 0),
            })
        return results
    except Exception:
        # 搜索接口可能格式不同，尝试备用解析
        pass

    # 备用：尝试另一个搜索接口
    try:
        data = client.get(
            "https://xueqiu.com/query/v1/search/user.json",
            params={"q": query, "count": count, "page": 1},
            referer_path="/",
        )
        users = data.get("list", []) or data.get("users", [])
        results = []
        for u in users:
            results.append({
                "id": str(u.get("id", u.get("uid", ""))),
                "name": u.get("screen_name", u.get("name", "")),
                "description": (u.get("description") or "")[:60],
                "followers_count": u.get("followers_count", 0),
            })
        return results
    except Exception:
        return []


def format_user_candidates(users: list) -> str:
    """格式化用户列表供选择。"""
    if not users:
        return "未找到匹配的用户"
    lines = []
    for i, u in enumerate(users, 1):
        fc = u.get("followers_count", 0)
        if fc >= 10000:
            fc_str = f"{fc / 10000:.1f}万"
        else:
            fc_str = str(fc)
        desc = u.get("description", "")
        desc_str = f" — {desc}" if desc else ""
        lines.append(f"  {i}. {u['name']} (ID: {u['id']}) 粉丝 {fc_str}{desc_str}")
    return "\n".join(lines)
'''


# ================================================================
# core/auto_login.py（新文件，Token 自动获取）
# ================================================================

AUTO_LOGIN = r'''"""
自动登录模块：打开可视浏览器让用户登录雪球，自动获取 xq_a_token。

流程:
  1. 启动 Playwright Chromium（非无头，用户可见）
  2. 导航到 xueqiu.com
  3. 提示用户在浏览器中登录（扫码或账号密码）
  4. 轮询检测 xq_a_token cookie
  5. 获取后写入 config.yaml
  6. 关闭浏览器
"""

import time
import os
import yaml


def auto_login(config_path="config.yaml", timeout=120):
    """
    打开浏览器让用户登录雪球，获取 xq_a_token。

    Args:
        config_path: config.yaml 的路径
        timeout: 等待登录的超时秒数（默认 120 秒）

    Returns:
        获取到的 token 字符串，失败返回 None
    """
    from playwright.sync_api import sync_playwright

    print()
    print("正在启动浏览器...")
    print("=" * 50)
    print("  请在弹出的浏览器窗口中登录雪球账号")
    print("  支持：扫码登录 / 手机验证码 / 账号密码")
    print("  登录成功后系统会自动检测并保存 Token")
    print("=" * 50)
    print()

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=False,  # 可视化，用户能看到浏览器
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1200, "height": 800},
        locale="zh-CN",
    )
    page = context.new_page()

    # 导航到雪球登录页
    page.goto("https://xueqiu.com", wait_until="networkidle", timeout=30000)
    time.sleep(2)

    # 尝试点击登录按钮（如果有的话）
    try:
        login_btn = page.query_selector('a[href*="login"]') or \
                    page.query_selector('button:has-text("登录")') or \
                    page.query_selector('.nav__login__btn') or \
                    page.query_selector('a:has-text("登录")')
        if login_btn:
            login_btn.click()
            time.sleep(1)
    except Exception:
        pass

    # 轮询等待 xq_a_token
    print("等待登录中", end="", flush=True)
    token = None
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            cookies = context.cookies("https://xueqiu.com")
            for cookie in cookies:
                if cookie["name"] == "xq_a_token" and cookie["value"]:
                    token = cookie["value"]
                    break
            if token:
                break
        except Exception:
            pass

        print(".", end="", flush=True)
        time.sleep(2)

    print()

    # 关闭浏览器
    try:
        page.close()
        context.close()
        browser.close()
        pw.stop()
    except Exception:
        pass

    if not token:
        print("✗ 超时未检测到登录，请重试")
        return None

    # 写入 config.yaml
    print(f"✓ 获取到 Token: {token[:12]}...{token[-8:]}")

    try:
        full_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), config_path)
        if not os.path.exists(full_path):
            full_path = config_path

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 用正则替换或字符串替换
        import re
        new_content = re.sub(
            r'(xq_a_token:\s*["\']?)([^"\'\n]*)',
            f'xq_a_token: "{token}"',
            content,
        )

        if new_content != content:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"✓ Token 已写入 {config_path}")
        else:
            print(f"⚠ 无法自动写入，请手动更新 config.yaml:")
            print(f"  xq_a_token: \"{token}\"")

    except Exception as e:
        print(f"⚠ 写入 config.yaml 失败: {e}")
        print(f"  请手动更新 xq_a_token: \"{token}\"")

    return token
'''


# ================================================================
# SKILL.md — Claude Skill 定义
# ================================================================

SKILL_MD = r'''# 雪球股票讨论区爬虫 Skill

## 触发条件
当用户提到以下关键词时触发本 Skill:
- "雪球"、"股票讨论"、"股票评论"、"舆情"
- "爬取"、"抓取" + 股票名称
- "帮我看看 XXX 的讨论"
- "跟踪用户 XXX"
- "导出数据"

## 能力描述
本 Skill 可以:
1. 爬取指定股票在雪球网的讨论区帖子和评论
2. 跟踪指定雪球用户的全部公开发言
3. 以讨论线程（DiscussionThread）为单元导出数据
4. 支持增量爬取（只爬新内容）和评论回填

## 使用方法

### 前置条件
项目路径: ~/Desktop/xueqiu-scraper/
需要先激活虚拟环境: cd ~/Desktop/xueqiu-scraper && source venv/bin/activate

### 可用命令

```bash
# Token 管理（登录）
python main.py login                    # 弹出浏览器让用户登录，自动获取token
python main.py test-cookie              # 测试 token 是否有效

# 股票管理
python main.py add-stock 茅台           # 自然语言添加股票
python main.py add-stock SH600519 贵州茅台
python main.py remove-stock SH600519

# 用户管理
python main.py add-user 但斌            # 搜索并添加跟踪用户
python main.py add-user 1234567890 某大V
python main.py remove-user 1234567890

# 爬取
python main.py run                      # 执行完整爬取（所有股票+用户）
python main.py backfill-comments        # 回填缺失评论

# 导出
python main.py export                           # 全部格式
python main.py export --format json --days 7    # JSON快照，最近7天
python main.py export --format md               # Markdown文档
python main.py export --format csv              # 层级CSV

# 状态
python main.py status                   # 查看数据统计和爬取日志
```

### 调用 skill_api.py（程序化调用）
```python
from skill_api import XueqiuSkillAPI

api = XueqiuSkillAPI()
api.ensure_token()                      # 检查token，无效则提示登录
api.add_stock("茅台")                   # 添加股票
api.run_scrape()                        # 执行爬取
result = api.export_json(days=7)        # 导出JSON
api.close()
```

## 数据模型
核心分析单元: DiscussionThread（讨论线程）= 帖子 + 全部评论
详见 PROJECT_BIBLE_xueqiu_scraper.py 第二部分（本体论模型）

## 输出格式
- JSON 快照: data/snapshots/{SYMBOL}_{DATE}.json（面向AI分析）
- 层级 CSV: data/export/{SYMBOL}_{TIMESTAMP}.csv（Excel浏览）
- Markdown: data/export/{SYMBOL}_{DATE}.md（人工阅读）

## 注意事项
- xq_a_token 会过期，过期后需要 `python main.py login` 重新登录
- 首次爬取某股票耗时较长（30-60分钟）
- 请求间隔 10-30 秒，避免被封
- 每次运行自动回填近 7 天帖子的新评论
'''


# ================================================================
# skill_api.py — Claude 程序化调用入口
# ================================================================

SKILL_API = r'''"""
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
'''


# ================================================================
# main.py 补丁内容
# ================================================================

LOGIN_FUNC = '''
def cmd_login(args, config):
    """自动登录获取 Token。"""
    from core.auto_login import auto_login
    token = auto_login(config_path="config.yaml")
    if token:
        print()
        print("现在可以运行 python main.py test-cookie 验证")
'''

ADD_STOCK_NEW = '''
def cmd_add_stock(args, config):
    """添加监控股票（支持自然语言）。"""
    from utils.stock_resolver import resolve_stock, format_candidates

    components = init_components(config)
    db = components["db"]
    client = components["client"]

    query = args.symbol  # 可能是名称、简称或代码
    name_arg = args.name or ""

    # 如果用户同时给了代码和名称（旧格式），直接添加
    if (query.upper().startswith("SH") or query.upper().startswith("SZ")) and len(query) == 8:
        symbol = query.upper()
        db.upsert_stock(symbol, name_arg)
        print(f"✓ 已添加监控股票: {symbol} {name_arg}")
        components["client"].close()
        db.close()
        return

    # 自然语言解析
    candidates = resolve_stock(query, client=client)

    if not candidates:
        print(f"未找到匹配的股票: {query}")
        print("请尝试输入完整股票名称或代码（如 SH600519）")
        components["client"].close()
        db.close()
        return

    if len(candidates) == 1 or candidates[0][2] in ("exact", "alias"):
        symbol, name, mtype = candidates[0]
        db.upsert_stock(symbol, name)
        print(f"✓ 已添加监控股票: {symbol} {name}")
    else:
        print(f"找到多个匹配:")
        print(format_candidates(candidates))
        print()
        try:
            choice = input("请输入序号选择（直接回车选第1个）: ").strip()
            idx = int(choice) - 1 if choice else 0
            if 0 <= idx < len(candidates):
                symbol, name, _ = candidates[idx]
                db.upsert_stock(symbol, name)
                print(f"✓ 已添加监控股票: {symbol} {name}")
            else:
                print("无效选择")
        except (ValueError, EOFError):
            symbol, name, _ = candidates[0]
            db.upsert_stock(symbol, name)
            print(f"✓ 已添加监控股票: {symbol} {name}")

    components["client"].close()
    db.close()
'''

ADD_USER_NEW = '''
def cmd_add_user(args, config):
    """添加跟踪用户（支持搜索）。"""
    components = init_components(config)
    db = components["db"]
    client = components["client"]

    query = args.user_id  # 可能是数字ID或用户名
    name_arg = args.name or ""

    # 如果是纯数字，直接当作 user_id
    if query.isdigit():
        db.upsert_tracked_user(query, name_arg, args.note or "")
        print(f"✓ 已添加跟踪用户: {query} {name_arg}")
        components["client"].close()
        db.close()
        return

    # 不是数字，尝试搜索
    print(f"正在搜索用户: {query}...")
    from utils.user_resolver import search_xueqiu_user, format_user_candidates

    users = search_xueqiu_user(client, query)

    if not users:
        print(f"未找到匹配的用户: {query}")
        print("请尝试输入用户的雪球数字ID")
        components["client"].close()
        db.close()
        return

    print(format_user_candidates(users))
    print()

    try:
        choice = input("请输入序号选择（直接回车选第1个）: ").strip()
        idx = int(choice) - 1 if choice else 0
        if 0 <= idx < len(users):
            u = users[idx]
            db.upsert_tracked_user(u["id"], u["name"], args.note or "")
            print(f"✓ 已添加跟踪用户: {u['id']} {u['name']}")
        else:
            print("无效选择")
    except (ValueError, EOFError):
        u = users[0]
        db.upsert_tracked_user(u["id"], u["name"], args.note or "")
        print(f"✓ 已添加跟踪用户: {u['id']} {u['name']}")

    components["client"].close()
    db.close()
'''


def patch_main_py():
    """修改 main.py：添加 login 命令，替换 add-stock 和 add-user。"""
    filepath = os.path.join(PROJECT_ROOT, "main.py")
    backup(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 添加 cmd_login 函数（在 cmd_run 前面）
    if "cmd_login" not in content:
        content = content.replace(
            "def cmd_run(args, config):",
            LOGIN_FUNC + "\ndef cmd_run(args, config):"
        )
        print("  ✓ 新增 cmd_login 函数")

    # 2. 替换 cmd_add_stock
    if "resolve_stock" not in content:
        # 找旧函数
        old_start = content.find("def cmd_add_stock(args, config):")
        if old_start != -1:
            old_end = content.find("\ndef ", old_start + 10)
            if old_end != -1:
                content = content[:old_start] + ADD_STOCK_NEW.strip() + "\n\n" + content[old_end + 1:]
                print("  ✓ 替换 cmd_add_stock（支持自然语言）")
    else:
        print("  · 跳过: cmd_add_stock 已更新")

    # 3. 替换 cmd_add_user
    if "search_xueqiu_user" not in content:
        old_start = content.find("def cmd_add_user(args, config):")
        if old_start != -1:
            old_end = content.find("\ndef ", old_start + 10)
            if old_end != -1:
                content = content[:old_start] + ADD_USER_NEW.strip() + "\n\n" + content[old_end + 1:]
                print("  ✓ 替换 cmd_add_user（支持搜索）")
    else:
        print("  · 跳过: cmd_add_user 已更新")

    # 4. 添加 login 子命令到 argparse
    if '"login"' not in content:
        content = content.replace(
            '    # run\n    subparsers.add_parser("run"',
            '    # login\n    subparsers.add_parser("login", help="打开浏览器登录雪球，自动获取Token")\n\n    # run\n    subparsers.add_parser("run"'
        )
        print("  ✓ 新增 login 子命令")

    # 5. 注册 login 命令
    if '"login": cmd_login' not in content:
        content = content.replace(
            '"run": cmd_run,',
            '"login": cmd_login,\n        "run": cmd_run,'
        )
        print("  ✓ commands dict 注册 login")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print("  ✓ main.py 已更新")


def main():
    print("=" * 55)
    print("  雪球爬虫 阶段三+四 — 智能输入 + Skill 封装")
    print("=" * 55)
    print()

    print("[1/5] 创建 utils/stock_resolver.py...")
    write_file("utils/stock_resolver.py", STOCK_RESOLVER, "股票名称解析器")
    print()

    print("[2/5] 创建 utils/user_resolver.py...")
    write_file("utils/user_resolver.py", USER_RESOLVER, "用户搜索器")
    print()

    print("[3/5] 创建 core/auto_login.py...")
    write_file("core/auto_login.py", AUTO_LOGIN, "自动登录模块")
    print()

    print("[4/5] 创建 Skill 文件...")
    write_file("SKILL.md", SKILL_MD, "Claude Skill 定义")
    write_file("skill_api.py", SKILL_API, "Skill 程序化入口")
    print()

    print("[5/5] 修改 main.py...")
    patch_main_py()
    print()

    print("=" * 55)
    print("  阶段三+四安装完成！")
    print("=" * 55)
    print()
    print("新增功能:")
    print()
    print("  1. 自动登录获取 Token:")
    print("     python main.py login")
    print()
    print("  2. 自然语言添加股票:")
    print("     python main.py add-stock 茅台")
    print("     python main.py add-stock 五粮液")
    print("     python main.py add-stock 宁德时代")
    print("     python main.py add-stock 600519")
    print()
    print("  3. 搜索并添加用户:")
    print("     python main.py add-user 但斌")
    print("     python main.py add-user 1234567890")
    print()
    print("  4. Skill API（程序化调用）:")
    print("     python -c \"from skill_api import XueqiuSkillAPI; api=XueqiuSkillAPI(); print(api.get_status()); api.close()\"")
    print()


if __name__ == "__main__":
    main()
