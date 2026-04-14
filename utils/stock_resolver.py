"""
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
