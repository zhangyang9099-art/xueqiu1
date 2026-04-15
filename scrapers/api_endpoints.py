"""
雪球 API 接口定义模块。

集中管理所有 API 的 URL、参数模板和响应解析逻辑。
当接口发生变更时只需修改此文件。

⚠️ 重要提醒：
  这些接口基于抓包分析，雪球可能随时更新接口。
  如果某个接口失效，请用 Chrome DevTools 重新抓包确认后更新本文件。
  操作方法：打开个股讨论页 → F12 → Network → XHR → 观察请求
"""

import time


# ============================================================
# 个股讨论区帖子列表
# ============================================================

def build_stock_timeline_url() -> str:
    """个股讨论区帖子列表接口 URL。"""
    return "https://xueqiu.com/query/v1/symbol/search/status.json"


def build_stock_timeline_params(
    symbol: str,
    count: int = 10,
    page: int = 1,
    source: str = "user",
    sort: str = "time",
) -> dict:
    """
    构建个股讨论区帖子列表请求参数。

    Args:
        symbol: 股票代码（如 SH600519）
        count: 每页条数
        page: 页码
        source: 来源过滤
        sort: 排序方式（time / reply / relevance）
    """
    return {
        "symbol": symbol,
        "count": count,
        "comment": 0,
        "hl": 0,
        "source": source,
        "sort": sort,
        "page": page,
        "q": "",
        "type": 11,
        "_": int(time.time() * 1000),
    }


def parse_stock_timeline_response(data: dict, requested_count: int = 20) -> tuple:
    """
    解析个股讨论区帖子列表响应。

    Args:
        data: API 返回的 JSON 字典
        requested_count: 请求时的 count 参数，用于判断是否有下一页

    Returns:
        (帖子列表, 是否有下一页, 总页数)
    """
    if not data:
        return [], False, 0

    posts = data.get("list", [])
    if not posts:
        # 尝试备用字段名
        posts = data.get("statuses", [])

    max_page = int(data.get("maxPage", 0) or 0)
    page = int(data.get("page", 0) or 0)

    if max_page > 0 and page > 0:
        has_more = page < max_page
    else:
        has_more = len(posts) >= requested_count

    return posts, has_more, max_page


# ============================================================
# 帖子评论列表
# ============================================================

def build_comments_url() -> str:
    """帖子评论列表接口 URL。"""
    return "https://xueqiu.com/statuses/comments.json"


def build_comments_params(
    post_id: str,
    count: int = 20,
    page: int = 1,
) -> dict:
    """
    构建帖子评论列表请求参数。

    Args:
        post_id: 帖子 ID
        count: 每页条数
        page: 页码
    """
    return {
        "id": str(post_id),
        "count": count,
        "page": page,
        "reply": "true",
        "asc": "false",
        "_": int(time.time() * 1000),
    }


def parse_comments_response(data: dict) -> tuple:
    """
    解析评论列表响应。

    Args:
        data: API 返回的 JSON 字典

    Returns:
        (评论列表, 总页数)
    """
    if not data:
        return [], 1

    comments = data.get("comments", [])
    max_page = data.get("maxPage", 1)

    return comments, max_page


def build_comments_v3_url() -> str:
    """网页展开评论/楼中楼使用的 v3 评论接口 URL。"""
    return "https://xueqiu.com/statuses/v3/comments.json"


def build_comments_v3_main_params(
    post_id: str,
    size: int = 20,
    max_id: int | str = -1,
    thread_type: int = 4,
) -> dict:
    """
    构建 v3 主评论时间线参数。

    该接口与网页“全部讨论”弹层一致，返回主评论及其首批 child_comments。
    """
    return {
        "id": str(post_id),
        "type": int(thread_type),
        "size": int(size),
        "max_id": max_id,
        "_": int(time.time() * 1000),
    }


def build_comments_v3_child_params(
    post_id: str,
    comment_id: str,
    max_id: int | str = -1,
    thread_type: int = 4,
    child_type: int = 2,
) -> dict:
    """
    构建 v3 楼中楼子回复翻页参数。

    网页点击“查看 N 条回复”后，会对同一个根评论持续请求该接口，并使用 next_max_id 翻页。
    """
    return {
        "comment_id": str(comment_id),
        "type": int(thread_type),
        "child_type": int(child_type),
        "id": str(post_id),
        "max_id": max_id,
        "_": int(time.time() * 1000),
    }


def parse_comments_v3_response(data: dict) -> dict:
    """
    解析 v3 评论接口响应。

    Returns:
        {
            "comments": [...],
            "next_max_id": ...,
            "status_reply_count": int,
            "comment_tl_count": int,
            "has_filtered": bool,
        }
    """
    if not data:
        return {
            "comments": [],
            "next_max_id": -1,
            "status_reply_count": 0,
            "comment_tl_count": 0,
            "has_filtered": False,
        }

    return {
        "comments": data.get("comments", []) or [],
        "next_max_id": data.get("next_max_id", -1),
        "status_reply_count": int(data.get("status_reply_count", 0) or 0),
        "comment_tl_count": int(data.get("comment_tl_count", 0) or 0),
        "has_filtered": bool(data.get("has_filtered", False)),
    }


# ============================================================
# 用户发言时间线
# ============================================================

def build_user_timeline_url() -> str:
    """用户发言列表接口 URL。"""
    return "https://xueqiu.com/v4/statuses/user_timeline.json"


def build_user_timeline_params(
    user_id: str,
    count: int = 40,
    page: int = 1,
) -> dict:
    """
    构建用户发言列表请求参数。

    Args:
        user_id: 用户数字 ID
        page: 页码
    """
    return {
        "user_id": str(user_id),
        "count": int(count),
        "page": page,
        "type": "all",
        "_": int(time.time() * 1000),
    }


def parse_user_timeline_response(data: dict) -> tuple:
    """
    解析用户发言列表响应。

    Args:
        data: API 返回的 JSON 字典

    Returns:
        (发言列表, 总页数)
    """
    if not data:
        return [], 1

    statuses = data.get("statuses", [])
    if not statuses:
        statuses = data.get("list", [])

    max_page = data.get("maxPage", 1)
    # 额外安全检查: 如果 maxPage 为 0 或负数，至少返回 1
    if max_page < 1:
        max_page = 1

    return statuses, max_page


# ============================================================
# 搜索接口（辅助，用于按关键词搜索）
# ============================================================

def build_search_url() -> str:
    """搜索接口 URL。"""
    return "https://xueqiu.com/statuses/search.json"


def build_search_params(
    query: str,
    count: int = 10,
    page: int = 1,
    sort: str = "time",
    source: str = "all",
) -> dict:
    """
    构建搜索请求参数。

    Args:
        query: 搜索关键词（股票名称等）
        count: 每页条数
        page: 页码
        sort: 排序方式（time / reply）
        source: 来源
    """
    return {
        "q": query,
        "count": count,
        "page": page,
        "sort": sort,
        "source": source,
        "_": int(time.time() * 1000),
    }


# ============================================================
# 公共辅助函数
# ============================================================

def extract_post_fields(post: dict) -> dict:
    """
    从 API 返回的帖子数据中提取标准化字段（v3: 含互动指标+用户画像）。
    """
    user_info = post.get("user", {}) or {}

    return {
        "id": str(post.get("id", "")),
        "user_id": str(user_info.get("id", post.get("user_id", ""))),
        "user_name": user_info.get("screen_name", ""),
        "title": post.get("title", "") or "",
        "text_html": post.get("text", "") or post.get("description", "") or "",
        "description": post.get("description", "") or "",
        "created_at": post.get("created_at", 0),
        "reply_count": post.get("reply_count", 0) or 0,
        "like_count": post.get("like_count", 0) or 0,
        "retweet_count": post.get("retweet_count", 0) or 0,
        "fav_count": post.get("fav_count", 0) or post.get("favorite_count", 0) or 0,
        "view_count": post.get("view_count", 0) or 0,
        # 用户画像原始数据（存入 user_profiles 表）
        "_user_profile": {
            "user_id": str(user_info.get("id", "")),
            "screen_name": user_info.get("screen_name", ""),
            "profile_image_url": user_info.get("profile_image_url", ""),
            "followers_count": user_info.get("followers_count"),
            "following_count": user_info.get("friends_count"),
            "status_count": user_info.get("status_count"),
            "verified_type": str(user_info.get("verified_type", "")),
            "description": user_info.get("description", ""),
        },
    }


def _extract_user_profile(user_info: dict) -> dict:
    return {
        "user_id": str(user_info.get("id", "")),
        "screen_name": user_info.get("screen_name", ""),
        "profile_image_url": user_info.get("profile_image_url", ""),
        "followers_count": user_info.get("followers_count"),
        "following_count": user_info.get("friends_count"),
        "status_count": user_info.get("status_count"),
        "verified_type": str(user_info.get("verified_type", "")),
        "description": user_info.get("description", ""),
    }


def _get_parent_id(comment: dict) -> str:
    reply_comment = comment.get("reply_comment") or {}
    return str(
        comment.get("in_reply_to_comment_id")
        or comment.get("reply_comment_id")
        or reply_comment.get("id")
        or ""
    )


def _get_reply_user(comment: dict, fallback_user: dict | None = None) -> dict:
    reply_user = comment.get("reply_user") or {}
    if reply_user:
        return reply_user

    reply_comment = comment.get("reply_comment") or {}
    parent_user = reply_comment.get("user") or {}
    if parent_user:
        return parent_user

    return fallback_user or {}


def _compute_comment_depth(comment: dict) -> int:
    parent_id = _get_parent_id(comment)
    if not parent_id:
        return 1

    reply_comment = comment.get("reply_comment")
    if isinstance(reply_comment, dict) and reply_comment.get("id"):
        return 1 + _compute_comment_depth(reply_comment)

    # 缺少更深层父评论对象时，至少认为这是二级或更深评论。
    return 2


def _merge_comment_record(existing: dict | None, incoming: dict) -> dict:
    if not existing:
        return incoming

    merged = dict(existing)
    for key, value in incoming.items():
        if key == "_user_profile":
            base_profile = dict(existing.get("_user_profile") or {})
            for p_key, p_value in (value or {}).items():
                if p_value not in ("", None):
                    base_profile[p_key] = p_value
            merged["_user_profile"] = base_profile
            continue

        if key == "depth":
            merged[key] = max(existing.get(key, 1), incoming.get(key, 1))
            continue

        if key == "comment_reply_count":
            merged[key] = max(existing.get(key, 0), incoming.get(key, 0))
            continue

        current = existing.get(key)
        if current in ("", None, 0) and value not in ("", None, 0):
            merged[key] = value
        elif key in {"parent_comment_id", "reply_comment_id"} and value:
            merged[key] = value
    return merged


def _flatten_comment(
    comment: dict,
    records: dict,
    forced_parent_id: str = "",
    forced_depth: int | None = None,
    forced_reply_user: dict | None = None,
):
    if not isinstance(comment, dict):
        return

    user_info = comment.get("user", {}) or {}
    parent_id = forced_parent_id or _get_parent_id(comment)
    depth = forced_depth if forced_depth is not None else _compute_comment_depth(comment)
    reply_user = _get_reply_user(comment, fallback_user=forced_reply_user)
    comment_id = str(comment.get("id", ""))
    status_id = str(comment.get("statusId") or "")
    root_status_id = str(comment.get("root_in_reply_to_status_id") or "")
    retweet_status_id = str(comment.get("retweet_status_id") or "")
    if status_id == "0":
        status_id = ""
    if root_status_id == "0":
        root_status_id = ""
    if retweet_status_id == "0":
        retweet_status_id = ""

    if comment_id:
        record = {
            "id": comment_id,
            "user_id": str(user_info.get("id", comment.get("user_id", ""))),
            "user_name": user_info.get("screen_name", ""),
            "text_html": comment.get("text", "") or comment.get("description", "") or "",
            "created_at": comment.get("created_at", 0),
            "like_count": comment.get("like_count", 0) or 0,
            "reply_comment_id": parent_id,
            "parent_comment_id": parent_id,
            "reply_to_user_id": str(reply_user.get("id", "")) if reply_user else "",
            "reply_to_user_name": reply_user.get("screen_name", "") if reply_user else "",
            "depth": depth,
            "status_id": status_id,
            "root_status_id": root_status_id,
            "retweet_status_id": retweet_status_id,
            "comment_reply_count": int(comment.get("comment_reply_count", 0) or 0),
            "_user_profile": _extract_user_profile(user_info),
        }
        records[comment_id] = _merge_comment_record(records.get(comment_id), record)

    children = comment.get("child_comments") or []
    if isinstance(children, list):
        parent_user = {
            "id": str(user_info.get("id", comment.get("user_id", ""))),
            "screen_name": user_info.get("screen_name", ""),
        }
        for child in children:
            _flatten_comment(
                child,
                records,
                forced_parent_id=comment_id,
                forced_depth=depth + 1,
                forced_reply_user=parent_user,
            )


def extract_comment_fields(comment: dict) -> list[dict]:
    """
    从 API 返回的评论数据中提取标准化字段（支持多级楼中楼展平）。
    """
    records = {}
    _flatten_comment(comment, records)
    return list(records.values())


def extract_user_status_fields(status: dict) -> dict:
    """
    从 API 返回的用户发言数据中提取标准化字段。

    Args:
        status: 原始发言字典

    Returns:
        标准化后的字段字典
    """
    user_info = status.get("user", {}) or {}

    # 尝试提取关联股票
    target = status.get("target", "") or ""
    # 雪球帖子中可能在 text 或 target 中包含 $SH600519$ 格式
    symbol = ""
    symbol_name = ""
    if target and isinstance(target, str):
        symbol = target
    elif isinstance(target, dict):
        symbol = target.get("symbol", "")
        symbol_name = target.get("name", "")

    # 提取关联帖子 ID（评论类型 c 识别）
    retweet_status_id = ""
    parent_status_id = ""
    retweet_status = status.get("retweet_status") or {}
    reply_to_status = status.get("reply_to_status") or {}
    in_reply_to_status = status.get("in_reply_to_status") or {}

    if isinstance(retweet_status, dict) and retweet_status.get("id"):
        retweet_status_id = str(retweet_status["id"])
    if isinstance(reply_to_status, dict) and reply_to_status.get("id"):
        parent_status_id = str(reply_to_status["id"])
    elif isinstance(in_reply_to_status, dict) and in_reply_to_status.get("id"):
        parent_status_id = str(in_reply_to_status["id"])
    elif retweet_status_id and not parent_status_id:
        parent_status_id = retweet_status_id

    is_original_post = not retweet_status_id and not parent_status_id

    return {
        "id": str(status.get("id", "")),
        "user_id": str(user_info.get("id", status.get("user_id", ""))),
        "user_name": user_info.get("screen_name", ""),
        "text_html": status.get("text", "") or status.get("description", "") or "",
        "target_symbol": symbol,
        "target_name": symbol_name,
        "created_at": status.get("created_at", 0),
        "reply_count": status.get("reply_count", 0) or 0,
        "like_count": status.get("like_count", 0) or 0,
        "retweet_status_id": retweet_status_id,
        "parent_status_id": parent_status_id,
        "is_original_post": is_original_post,
    }


# ============================================================
# 热门话题（已验证 2026-03-15）
# ============================================================

def build_trending_url() -> str:
    """热门话题接口 URL。"""
    return "https://xueqiu.com/hot_event/list.json"


def build_trending_params(count: int = 10) -> dict:
    """构建热门话题请求参数。"""
    import time
    return {
        "count": count,
        "_": int(time.time() * 1000),
    }


def parse_trending_response(data: dict) -> list:
    """
    解析热门话题响应。

    Args:
        data: API 返回的 JSON

    Returns:
        话题列表 [{id, tag, content, status_count, pic, hot}, ...]
    """
    if not data:
        return []
    return data.get("list", [])


# ============================================================
# 单条帖子详情（用于 Type c 获取父帖子）
# ============================================================

def build_single_status_url() -> str:
    """单条帖子详情接口 URL。"""
    return "https://xueqiu.com/statuses/show.json"


def build_single_status_params(status_id: str) -> dict:
    """构建单条帖子详情请求参数。"""
    return {
        "id": str(status_id),
        "_": int(time.time() * 1000),
    }


def parse_single_status_response(data: dict) -> dict:
    """
    解析单条帖子详情响应。

    Returns:
        帖子数据字典（与 extract_post_fields 格式兼容），或空 dict
    """
    if not data:
        return {}
    return data
