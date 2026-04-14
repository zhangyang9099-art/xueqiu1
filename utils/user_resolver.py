"""
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
