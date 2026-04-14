"""
个股评论区爬虫：爬取指定股票的帖子、评论与楼中楼。

模式:
  - update:   增量更新，从最新向后看，只补新帖和新评论
  - backfill: 历史补全，以数据库最老帖子为边界继续向更早时间爬
"""

import time
from datetime import datetime

from core.client import XueqiuClient
from core.exceptions import CookieExpired, MaxRetryExceeded, RequestFailed
from storage.database import Database
from scrapers.api_endpoints import (
    build_stock_timeline_url,
    build_stock_timeline_params,
    parse_stock_timeline_response,
    build_comments_url,
    build_comments_params,
    parse_comments_response,
    build_comments_v3_url,
    build_comments_v3_main_params,
    build_comments_v3_child_params,
    parse_comments_v3_response,
    extract_post_fields,
    extract_comment_fields,
)
from utils.html_cleaner import html_to_text
from utils.logger import get_logger

logger = get_logger()


class StockCommentScraper:
    """个股评论区爬虫。"""

    def __init__(self, client: XueqiuClient, db: Database, config: dict):
        self.client = client
        self.db = db
        self.max_pages = config.get("max_pages_per_stock", 50)
        self.timeline_page_size = config.get("stock_timeline_page_size", 10)
        self.timeline_source = config.get("stock_timeline_source", "user")
        self.timeline_sort = config.get("stock_timeline_sort", "time")
        self.max_comment_pages = config.get("max_comment_pages", 20)
        self.backfill_days = config.get("comment_backfill_days", 7)
        self.history_confirm_runs = config.get("history_confirm_runs", 2)
        self.comment_variant_page_size = config.get("comment_variant_page_size", 100)
        self.history_inline_comments = bool(config.get("history_inline_comments", False))
        self.history_browser_recycle_pages = config.get("history_browser_recycle_pages", 12)
        self.history_browser_recycle_requests = config.get("history_browser_recycle_requests", 60)
        self.timeline_request_timeout_ms = int(config.get("timeline_request_timeout_ms", 30000) or 30000)
        self.timeline_request_retries = max(1, int(config.get("timeline_request_retries", 2) or 2))
        self.timeline_transport = config.get("stock_timeline_transport", "auto")
        self.history_timeline_transport = config.get("history_timeline_transport", "page")
        self.history_timeline_fallback_transport = config.get(
            "history_timeline_fallback_transport",
            "isolated_page",
        )
        self.history_cursor_enabled = bool(config.get("history_cursor_enabled", True))
        self.comment_request_timeout_ms = int(config.get("comment_request_timeout_ms", 12000) or 12000)
        self.comment_request_retries = max(1, int(config.get("comment_request_retries", 1) or 1))
        self.comment_post_budget_seconds = float(config.get("comment_post_budget_seconds", 25) or 25)
        self.comment_v3_enabled = bool(config.get("comment_v3_enabled", True))
        self.comment_v3_page_size = max(1, int(config.get("comment_v3_page_size", 20) or 20))
        self.comment_v3_child_max_pages = max(1, int(config.get("comment_v3_child_max_pages", 30) or 30))
        self._last_session_recycle_page = 0
        self._last_session_recycle_requests = 0

    def _timeline_transport_for_mode(self, mode: str) -> str:
        if mode == "backfill":
            return self.history_timeline_transport
        return self.timeline_transport

    def _fetch_timeline_page(
        self,
        symbol: str,
        page: int,
        count_per_page: int,
        mode: str = "update",
        transport_override: str | None = None,
    ):
        transport = transport_override or self._timeline_transport_for_mode(mode)
        request_kwargs = {
            "params": build_stock_timeline_params(
                symbol,
                count=count_per_page,
                page=page,
                source=self.timeline_source,
                sort=self.timeline_sort,
            ),
            "referer_path": f"/S/{symbol}",
            "timeout_ms": self.timeline_request_timeout_ms,
            "max_retries": self.timeline_request_retries,
            "transport": transport,
        }
        try:
            data = self.client.get(
                build_stock_timeline_url(),
                **request_kwargs,
            )
        except RequestFailed as e:
            should_fallback = (
                mode == "backfill"
                and transport != self.history_timeline_fallback_transport
                and self.history_timeline_fallback_transport
                and e.category in ("transport_timeout", "page_dead", "transport_failure")
            )
            if not should_fallback:
                raise
            logger.warning(
                f"[{symbol}] 历史时间线 page={page} 主 transport={transport} 失败，"
                f"降级到 {self.history_timeline_fallback_transport} 重试当前页: {e.category}"
            )
            data = self.client.get(
                build_stock_timeline_url(),
                **{**request_kwargs, "transport": self.history_timeline_fallback_transport},
            )
        posts, has_more, max_page = parse_stock_timeline_response(
            data,
            requested_count=count_per_page,
        )
        times = [int((p or {}).get("created_at") or 0) for p in posts if (p or {}).get("created_at")]
        return {
            "posts": posts,
            "has_more": has_more,
            "max_page": max_page,
            "newest": max(times) if times else 0,
            "oldest": min(times) if times else 0,
        }

    def _locate_history_start_page(self, symbol: str, boundary_time: int, count_per_page: int):
        first = self._fetch_timeline_page(symbol, 1, count_per_page, mode="backfill")
        max_page = int(first.get("max_page") or 0)
        if boundary_time <= 0 or max_page <= 1:
            return 1, {1: first}, max_page

        page_cache = {1: first}
        if first.get("oldest", 0) < boundary_time:
            return 1, page_cache, max_page

        def get_page(mid: int):
            if mid not in page_cache:
                page_cache[mid] = self._fetch_timeline_page(symbol, mid, count_per_page, mode="backfill")
            return page_cache[mid]

        low, high = 1, max_page
        candidate = max_page + 1
        while low <= high:
            mid = (low + high) // 2
            info = get_page(mid)
            oldest = info.get("oldest", 0)
            newest = info.get("newest", 0)
            if not info.get("posts"):
                candidate = min(candidate, mid)
                high = mid - 1
                continue
            if oldest >= boundary_time:
                low = mid + 1
                continue
            candidate = mid
            if newest < boundary_time:
                high = mid - 1
            else:
                high = mid - 1

        if candidate > max_page:
            return max_page + 1, page_cache, max_page
        return candidate, page_cache, max_page

    def _resolve_history_start_page(self, symbol: str, boundary_time: int, count_per_page: int, display_name: str):
        if self.history_cursor_enabled:
            cursor = self.db.get_stock_history_cursor(symbol)
            cursor_page = int(cursor.get("page") or 0)
            if cursor_page > 1:
                try:
                    info = self._fetch_timeline_page(symbol, cursor_page, count_per_page, mode="backfill")
                    max_page = int(info.get("max_page") or 0)
                    if info.get("posts") and (max_page <= 0 or cursor_page <= max_page):
                        if info.get("oldest", 0) >= boundary_time and boundary_time > 0:
                            logger.info(
                                f"[{display_name}] 历史游标 page={cursor_page} 边界可能漂移，"
                                "先继续使用游标页并在页内跳过已覆盖帖子"
                            )
                        else:
                            logger.info(
                                f"[{display_name}] 从历史游标 page={cursor_page} 恢复 "
                                f"(transport={self.history_timeline_transport})"
                            )
                        return cursor_page, {cursor_page: info}, max_page
                    logger.info(f"[{display_name}] 历史游标 page={cursor_page} 无效，重新定位边界页")
                except Exception as e:
                    logger.warning(f"[{display_name}] 读取历史游标失败，改为重新定位: {e}")
        return self._locate_history_start_page(symbol, boundary_time, count_per_page)

    def scrape_stock(self, symbol: str, name: str = "", mode: str = "update") -> dict:
        """
        爬取指定股票的讨论区（帖子+评论），然后自动回填近期帖子的新评论。

        Returns:
            爬取结果统计 dict
        """
        display_name = f"{symbol}" + (f"({name})" if name else "")
        # 绑定burst休息回调：刷新浏览器session重置限流
        if hasattr(self.client, "refresh_session"):
            self.client.rate_limiter.on_burst_rest = self.client.refresh_session

        logger.info(f"========== 开始爬取 {display_name} 讨论区 ==========")

        started_at = datetime.now().isoformat()
        mode = "backfill" if mode == "history" else mode
        last_scrape_time = self.db.get_stock_last_scrape_time(symbol)
        oldest_post_time = self.db.get_stock_oldest_post_time(symbol)
        stagnant_runs = self.db.get_stock_history_stagnant_runs(symbol)

        if mode == "update":
            if last_scrape_time:
                logger.info(f"上次增量时间: {datetime.fromtimestamp(last_scrape_time / 1000)}")
            else:
                logger.info("首次增量爬取该股票(无历史记录)")
        else:
            if oldest_post_time:
                logger.info(f"历史补全边界: {datetime.fromtimestamp(oldest_post_time / 1000)} 之前")
            else:
                logger.info("历史补全首次执行：当前无最老帖子边界，将从最新开始补")

        total_new_posts = 0
        total_new_comments = 0
        latest_post_time = 0
        oldest_crawled_post_time = oldest_post_time or 0
        progress_made = False
        status = "success"
        error_msg = ""
        error_category = ""
        history_complete = False
        last_page_attempted = 0

        try:
            page = 1
            pages_processed = 0
            should_stop = False
            count_per_page = self.timeline_page_size
            self._last_session_recycle_page = 0
            self._last_session_recycle_requests = self.client.rate_limiter.total_requests
            page_cache = {}

            if mode == "backfill" and oldest_post_time:
                page, page_cache, located_max_page = self._resolve_history_start_page(
                    symbol,
                    oldest_post_time,
                    count_per_page,
                    display_name,
                )
                self._last_session_recycle_page = max(0, page - 1)
                self._last_session_recycle_requests = self.client.rate_limiter.total_requests
                if page_cache:
                    cursor = self.db.get_stock_history_cursor(symbol)
                    if cursor.get("page") == page and cursor.get("page", 0) > 1:
                        logger.info(f"[{display_name}] 历史模式从历史游标页 {page} 开始")
                    else:
                        logger.info(f"[{display_name}] 历史模式从定位页 {page} 开始，跳过数据库已覆盖的新页")
                if located_max_page > 0 and page > located_max_page:
                    logger.info(f"[{display_name}] 数据库最老时间已接近当前可见最早帖子，无需重扫新页")
                    history_complete = True
                    self.db.clear_stock_history_cursor(symbol)

            while not should_stop and pages_processed < self.max_pages and not history_complete:
                last_page_attempted = page
                self._maybe_recycle_session(display_name, mode, page)
                logger.info(
                    f"[{display_name}] 第 {page} 页 | "
                    f"累计: {total_new_posts} 帖 {total_new_comments} 评论 | "
                    f"总请求: {self.client.rate_limiter.total_requests}"
                )
                if page in page_cache:
                    page_info = page_cache.pop(page)
                else:
                    page_info = self._fetch_timeline_page(symbol, page, count_per_page, mode=mode)

                posts = page_info["posts"]
                has_more = page_info["has_more"]
                max_page = page_info["max_page"]

                if not posts:
                    logger.info(f"[{display_name}] 第 {page} 页无帖子，结束翻页")
                    history_complete = max_page <= 0 or page >= max_page
                    if history_complete:
                        self.db.clear_stock_history_cursor(symbol)
                    break

                for raw_post in posts:
                    post = extract_post_fields(raw_post)

                    if mode == "update":
                        if last_scrape_time and post["created_at"] < last_scrape_time:
                            logger.info(
                                f"[{display_name}] 遇到旧帖子 (id={post['id']}), 停止翻页"
                            )
                            should_stop = True
                            break
                    else:
                        if oldest_post_time and post["created_at"] >= oldest_post_time:
                            continue

                    if post["created_at"] > latest_post_time:
                        latest_post_time = post["created_at"]

                    if oldest_crawled_post_time == 0 or post["created_at"] < oldest_crawled_post_time:
                        oldest_crawled_post_time = post["created_at"]
                        self.db.update_stock_oldest_post_time(symbol, oldest_crawled_post_time)
                        if not oldest_post_time or oldest_crawled_post_time < oldest_post_time:
                            progress_made = True

                    if mode == "update":
                        self.db.update_stock_scrape_time(symbol, latest_post_time)

                    post["text_plain"] = html_to_text(post["text_html"])

                    is_new = self.db.save_post(symbol, post)
                    if is_new:
                        total_new_posts += 1

                    if post.get("_user_profile"):
                        try:
                            self.db.upsert_user_profile(post["_user_profile"])
                        except Exception:
                            pass

                    progress = self.db.get_post_comment_progress(post["id"]) or {}
                    scraped_comments = progress.get("comments_scraped", 0)
                    should_scrape_comments = (
                        (mode == "update" or self.history_inline_comments)
                        and post.get("reply_count", 0) > 0
                        and (is_new or scraped_comments < post.get("reply_count", 0))
                    )

                    if should_scrape_comments:
                        logger.info(
                            f"[{display_name}] 抓取帖子 {post['id']} 评论: "
                            f"声称 {post.get('reply_count', 0)}, 已爬 {scraped_comments}"
                        )
                        new_comments = self._scrape_post_comments(
                            post["id"],
                            display_name,
                            referer_path=f"/S/{symbol}",
                        )
                        total_new_comments += new_comments

                if mode == "backfill":
                    cursor_oldest = oldest_crawled_post_time or page_info.get("oldest", 0)
                    self.db.update_stock_history_cursor(symbol, page + 1, cursor_oldest)

                if max_page > 0 and page >= max_page:
                    history_complete = True
                    self.db.clear_stock_history_cursor(symbol)
                    break

                if not has_more:
                    history_complete = True
                    self.db.clear_stock_history_cursor(symbol)
                    break
                pages_processed += 1
                page += 1

            if mode == "update":
                backfill_comments = self._backfill_recent_comments(symbol, display_name)
                total_new_comments += backfill_comments

            if latest_post_time > 0:
                self.db.update_stock_scrape_time(symbol, latest_post_time)
            elif not last_scrape_time:
                self.db.update_stock_scrape_time(symbol, int(time.time() * 1000))

            if oldest_crawled_post_time > 0:
                self.db.update_stock_oldest_post_time(symbol, oldest_crawled_post_time)
            if mode == "backfill":
                if progress_made:
                    stagnant_runs = 0
                else:
                    stagnant_runs += 1
                self.db.set_stock_history_stagnant_runs(symbol, stagnant_runs)
                confirmed_complete = history_complete and stagnant_runs >= self.history_confirm_runs
                self.db.mark_stock_history_complete(symbol, confirmed_complete)
                if confirmed_complete:
                    self.db.clear_stock_history_cursor(symbol)
            else:
                self.db.set_stock_history_stagnant_runs(symbol, 0)
                self.db.mark_stock_history_complete(symbol, history_complete)

        except CookieExpired:
            status = "failed"
            error_msg = "Cookie 已失效"
            raise

        except MaxRetryExceeded as e:
            status = "partial"
            error_msg = str(e)
            logger.error(f"[{display_name}] 达到最大重试次数: {e}")

        except RequestFailed as e:
            status = "failed"
            error_msg = str(e)
            error_category = e.category
            failure_meta = self.client.get_last_failure_meta()
            logger.error(
                f"[{display_name}] 请求失败: {e} | "
                f"page={last_page_attempted or page} transport={failure_meta.get('transport', '')} "
                f"auth={'yes' if failure_meta.get('has_auth_cookies') else 'no' if 'has_auth_cookies' in failure_meta else '?'} "
                f"excerpt={failure_meta.get('html_excerpt', '')}",
                exc_info=True,
            )

        except Exception as e:
            status = "failed"
            error_msg = f"{type(e).__name__}: {e}"
            error_category = self.client.get_last_failure_meta().get("category", "")
            logger.error(f"[{display_name}] 爬取异常: {e}", exc_info=True)

        finally:
            self.db.log_scrape(
                task_type="stock_comments",
                target=symbol,
                status=status,
                new_items_count=total_new_posts + total_new_comments,
                error_message=error_msg,
                started_at=started_at,
            )

        result = {
            "symbol": symbol,
            "name": name,
            "mode": mode,
            "status": status,
            "new_posts": total_new_posts,
            "new_comments": total_new_comments,
            "error": error_msg,
            "error_category": error_category,
            "last_page": int(last_page_attempted or 0),
        }

        completeness = self.db.get_stock_completeness_report(symbol=symbol)
        if completeness:
            report = completeness[0]
            result["missing_comments"] = report["missing_comments"]
            result["orphan_comments"] = report["orphan_comments"]
            if report["missing_comments"] or report["orphan_comments"]:
                logger.warning(
                    f"[{display_name}] 完整性检查: 缺失评论 {report['missing_comments']} 条, "
                    f"孤儿评论 {report['orphan_comments']} 条"
                )

        logger.info(
            f"[{display_name}] 完成: "
            f"新帖子 {total_new_posts} 条, 新评论 {total_new_comments} 条, "
            f"状态 {status}"
        )

        return result

    def _backfill_recent_comments(self, symbol: str, display_name: str = "") -> int:
        """
        对最近 N 天内存在评论缺口或孤儿评论的帖子重新爬评论。
        """
        posts = self._get_comment_repair_targets(symbol=symbol, days=self.backfill_days)

        if not posts:
            return 0

        logger.info(
            f"[{display_name}] 评论回填: {len(posts)} 个帖子需要补爬评论"
        )

        total_new = 0
        for p in posts:
            gap = p.get("gap", 0)
            orphan_comments = p.get("orphan_comments", 0)
            logger.info(
                f"[{display_name}] 回填帖子 {p['id']} "
                f"({p['user_name']}): 声称 {p['reply_count']}, "
                f"已爬 {p['comments_scraped']}, 差 {gap}, 孤儿 {orphan_comments}"
            )
            try:
                new = self._scrape_post_comments(
                    p["id"],
                    display_name,
                    referer_path=f"/S/{symbol}" if symbol else "/",
                )
                total_new += new
            except (CookieExpired, MaxRetryExceeded):
                raise
            except Exception as e:
                logger.warning(f"[{display_name}] 回填帖子 {p['id']} 失败: {e}")

        if total_new > 0:
            logger.info(f"[{display_name}] 评论回填完成: 新增 {total_new} 条评论")

        return total_new

    def backfill_comments(self, symbol: str = None, days: int = None, max_posts: int = None) -> dict:
        """
        独立的评论回填入口（供 main.py 的 backfill-comments 命令调用）。

        Args:
            symbol: 指定股票（None=全部）
            days: 回填天数（None=使用配置值）

        Returns:
            回填结果统计
        """
        if days == 0:
            # CLI `--days 0` means full-history repair; keep it unbounded.
            days = None
        elif days is None:
            days = self.backfill_days

        posts = self._get_comment_repair_targets(symbol=symbol, days=days, limit=max_posts)

        if not posts:
            logger.info("没有需要回填评论的帖子")
            return {"total_posts": 0, "new_comments": 0}

        logger.info(f"评论回填: 共 {len(posts)} 个帖子需要补爬")

        total_new = 0
        for i, p in enumerate(posts, 1):
            display = f"{p['symbol']}" if not symbol else symbol
            logger.info(
                f"[{i}/{len(posts)}] 帖子 {p['id']} ({p['user_name']}): "
                f"差 {p.get('gap', 0)} 条评论, 孤儿 {p.get('orphan_comments', 0)}"
            )
            try:
                new = self._scrape_post_comments(
                    p["id"],
                    display,
                    referer_path=f"/S/{p['symbol']}" if p.get("symbol") else "/",
                )
                total_new += new
            except (CookieExpired, MaxRetryExceeded):
                raise
            except Exception as e:
                logger.warning(f"回填帖子 {p['id']} 失败: {e}")

        logger.info(f"评论回填完成: 处理 {len(posts)} 帖, 新增 {total_new} 条评论")
        return {"total_posts": len(posts), "new_comments": total_new}

    def backfill_one_post(self, post_id: str, symbol: str = None) -> dict:
        post = self.db.get_post(post_id)
        if not post:
            logger.info(f"帖子 {post_id} 不存在，跳过")
            return {"total_posts": 0, "new_comments": 0}

        display = symbol or post.get("symbol") or ""
        logger.info(
            f"单帖评论回填: 帖子 {post_id} ({post.get('user_name', '')}), "
            f"声称 {post.get('reply_count', 0)}, 已爬 {post.get('comments_scraped', 0)}"
        )
        new_comments = self._scrape_post_comments(
            post_id,
            display,
            referer_path=f"/S/{post.get('symbol')}" if post.get("symbol") else "/",
        )
        return {"total_posts": 1, "new_comments": new_comments}

    def _get_comment_repair_targets(self, symbol: str = None, days: int = None, limit: int = None) -> list[dict]:
        targets = {}
        for post in self.db.get_posts_needing_backfill(symbol=symbol, days=days):
            item = dict(post)
            item.setdefault("gap", 0)
            item.setdefault("orphan_comments", 0)
            targets[item["id"]] = item

        for post in self.db.get_posts_with_orphan_comments(symbol=symbol, days=days):
            item = targets.get(post["id"], {
                "id": post["id"],
                "symbol": post["symbol"],
                "user_name": post["user_name"],
                "reply_count": post["reply_count"],
                "comments_scraped": post["comments_scraped"],
                "gap": 0,
                "orphan_comments": 0,
            })
            item["orphan_comments"] = max(item.get("orphan_comments", 0), post.get("orphan_comments", 0))
            targets[item["id"]] = item

        targets = sorted(
            targets.values(),
            key=lambda item: (item.get("gap", 0), item.get("orphan_comments", 0)),
            reverse=True,
        )
        if limit:
            return targets[:limit]
        return targets

    def _maybe_recycle_session(self, display_name: str, mode: str, page: int):
        if mode != "backfill":
            return

        recycle_by_page = (
            self.history_browser_recycle_pages > 0
            and page > 1
            and (page - self._last_session_recycle_page) >= self.history_browser_recycle_pages
        )
        current_requests = self.client.rate_limiter.total_requests
        recycle_by_request = (
            self.history_browser_recycle_requests > 0
            and (current_requests - self._last_session_recycle_requests) >= self.history_browser_recycle_requests
        )

        if not recycle_by_page and not recycle_by_request:
            return

        logger.info(
            f"[{display_name}] 主动回收浏览器会话: 页码={page}, "
            f"累计请求={current_requests}"
        )
        self.client.refresh_session()
        self._last_session_recycle_page = page
        self._last_session_recycle_requests = current_requests

    def _scrape_post_comments(self, post_id: str, display_name: str = "", referer_path: str = "/") -> int:
        """爬取指定帖子的全部评论（快速模式），爬完后更新 comments_scraped 计数。"""
        new_count = 0
        seen_comment_ids = set()
        started_at = time.time()
        deadline = started_at + self.comment_post_budget_seconds
        v3_meta = {
            "status_reply_count": 0,
            "has_filtered": False,
        }
        v3_success = False
        self.db.conn.execute("BEGIN IMMEDIATE")
        self.client.rate_limiter.enter_comment_mode()
        try:
            self.db.clear_post_comment_memberships(post_id, commit=False)
            if self.comment_v3_enabled:
                try:
                    added, v3_meta = self._collect_comment_thread_v3(
                        post_id,
                        seen_comment_ids,
                        display_name,
                        deadline=deadline,
                        referer_path=referer_path,
                    )
                    new_count += added
                    v3_success = True
                except (CookieExpired, MaxRetryExceeded):
                    raise
                except Exception as e:
                    logger.warning(f"[{display_name}] 帖子 {post_id} v3 评论抓取失败，回退旧接口: {e}")

            if not v3_success:
                new_count += self._collect_comment_variant(
                    post_id,
                    seen_comment_ids,
                    display_name,
                    count=20,
                    asc=False,
                    deadline=deadline,
                    referer_path=referer_path,
                )
            progress = self.db.get_post_comment_progress(post_id) or {}
            current_members = len(seen_comment_ids)
            claimed_comments = max(
                int(progress.get("reply_count", 0) or 0),
                int(v3_meta.get("status_reply_count", 0) or 0),
            )

            if claimed_comments and current_members < claimed_comments:
                if v3_meta.get("has_filtered"):
                    logger.warning(
                        f"[{display_name}] 帖子 {post_id} v3 评论链提示存在官网过滤，"
                        f"声称 {claimed_comments}，当前可抓 {current_members}"
                    )
                else:
                    for asc in (False, True):
                        if time.time() >= deadline:
                            logger.warning(
                                f"[{display_name}] 帖子 {post_id} 评论抓取达到单帖预算 "
                                f"{self.comment_post_budget_seconds:.0f}s，先保留当前结果"
                            )
                            break
                        self._collect_comment_variant(
                            post_id,
                            seen_comment_ids,
                            display_name,
                            count=self.comment_variant_page_size,
                            asc=asc,
                            deadline=deadline,
                            referer_path=referer_path,
                        )
                        if len(seen_comment_ids) >= claimed_comments:
                            break

            self.db.update_post_comments_scraped(post_id, commit=False)
            self.db.conn.commit()
        finally:
            self.client.rate_limiter.exit_comment_mode()
            if self.db.conn.in_transaction:
                self.db.conn.rollback()

        logger.info(
            f"[{display_name}] 帖子 {post_id} 评论完成: "
            f"累计抓到 {len(seen_comment_ids)} 条, 新增 {new_count} 条"
        )

        if new_count > 0:
            logger.debug(f"[{display_name}] 帖子 {post_id}: 新增 {new_count} 条评论")

        return new_count

    def _ingest_comment_payload(self, post_id: str, raw_comments: list, seen_comment_ids: set) -> int:
        new_count = 0
        for raw_comment in raw_comments:
            for comment in extract_comment_fields(raw_comment):
                if not comment.get("id"):
                    continue
                comment["text_plain"] = html_to_text(comment["text_html"])
                is_new_comment = self.db.save_comment(post_id, comment, commit=False)
                self.db.link_comment_to_post(post_id, comment["id"], commit=False)
                if comment["id"] not in seen_comment_ids:
                    seen_comment_ids.add(comment["id"])
                    if is_new_comment:
                        new_count += 1
                if comment.get("_user_profile"):
                    try:
                        self.db.upsert_user_profile(comment["_user_profile"], commit=False)
                    except Exception:
                        pass
        return new_count

    def _collect_comment_thread_v3(
        self,
        post_id: str,
        seen_comment_ids: set,
        display_name: str,
        deadline: float | None = None,
        referer_path: str = "/",
    ) -> tuple[int, dict]:
        new_count = 0
        max_id: int | str = -1
        step = 1
        meta = {
            "status_reply_count": 0,
            "has_filtered": False,
        }

        while step <= self.max_comment_pages:
            if deadline and time.time() >= deadline:
                break
            logger.info(
                f"[{display_name}] 帖子 {post_id} v3评论页 {step} "
                f"(size={self.comment_v3_page_size}, max_id={max_id})"
            )
            data = self.client.get(
                build_comments_v3_url(),
                params=build_comments_v3_main_params(
                    post_id,
                    size=self.comment_v3_page_size,
                    max_id=max_id,
                ),
                referer_path=referer_path,
                timeout_ms=self.comment_request_timeout_ms,
                max_retries=self.comment_request_retries,
                transport="page",
            )
            parsed = parse_comments_v3_response(data)
            comments = parsed["comments"]
            if not comments:
                break

            meta["status_reply_count"] = max(
                meta["status_reply_count"],
                parsed["status_reply_count"],
            )
            meta["has_filtered"] = meta["has_filtered"] or parsed["has_filtered"]
            new_count += self._ingest_comment_payload(post_id, comments, seen_comment_ids)

            for raw_comment in comments:
                reply_count = int(raw_comment.get("comment_reply_count", 0) or 0)
                preview_children = len(raw_comment.get("child_comments") or [])
                if reply_count > preview_children:
                    child_added, child_meta = self._collect_comment_thread_v3_children(
                        post_id,
                        str(raw_comment.get("id", "")),
                        seen_comment_ids,
                        display_name,
                        deadline=deadline,
                        referer_path=referer_path,
                    )
                    new_count += child_added
                    meta["has_filtered"] = meta["has_filtered"] or child_meta.get("has_filtered", False)

            next_max_id = parsed["next_max_id"]
            if next_max_id in (None, -1, max_id):
                break
            max_id = next_max_id
            step += 1

        return new_count, meta

    def _collect_comment_thread_v3_children(
        self,
        post_id: str,
        root_comment_id: str,
        seen_comment_ids: set,
        display_name: str,
        deadline: float | None = None,
        referer_path: str = "/",
    ) -> tuple[int, dict]:
        new_count = 0
        max_id: int | str = -1
        step = 1
        meta = {"has_filtered": False}

        while step <= self.comment_v3_child_max_pages:
            if deadline and time.time() >= deadline:
                break
            logger.info(
                f"[{display_name}] 帖子 {post_id} 子回复 {root_comment_id} "
                f"第 {step} 批 (max_id={max_id})"
            )
            data = self.client.get(
                build_comments_v3_url(),
                params=build_comments_v3_child_params(
                    post_id,
                    root_comment_id,
                    max_id=max_id,
                ),
                referer_path=referer_path,
                timeout_ms=self.comment_request_timeout_ms,
                max_retries=self.comment_request_retries,
                transport="page",
            )
            parsed = parse_comments_v3_response(data)
            comments = parsed["comments"]
            if not comments:
                break

            meta["has_filtered"] = meta["has_filtered"] or parsed["has_filtered"]
            new_count += self._ingest_comment_payload(post_id, comments, seen_comment_ids)

            next_max_id = parsed["next_max_id"]
            root_comment = comments[0] if comments else {}
            child_comments = root_comment.get("child_comments") or []
            if not child_comments or next_max_id in (None, -1, max_id):
                break
            max_id = next_max_id
            step += 1

        return new_count, meta

    def _collect_comment_variant(self, post_id: str, seen_comment_ids: set, display_name: str,
                                 count: int = 20, asc: bool = False, deadline: float | None = None,
                                 referer_path: str = "/") -> int:
        new_count = 0
        page = 1
        while page <= self.max_comment_pages:
            try:
                if deadline and time.time() >= deadline:
                    break
                logger.info(
                    f"[{display_name}] 帖子 {post_id} 评论页 {page} "
                    f"(count={count}, asc={'true' if asc else 'false'})"
                )
                params = build_comments_params(post_id, count=count, page=page)
                params["asc"] = "true" if asc else "false"
                data = self.client.get(
                    build_comments_url(),
                    params=params,
                    referer_path=referer_path,
                    timeout_ms=self.comment_request_timeout_ms,
                    max_retries=self.comment_request_retries,
                    transport="page",
                )

                comments, max_page = parse_comments_response(data)
                if not comments:
                    break

                new_count += self._ingest_comment_payload(post_id, comments, seen_comment_ids)

                if page >= max_page or count >= self.comment_variant_page_size:
                    break
                page += 1

            except (CookieExpired, MaxRetryExceeded):
                raise
            except Exception as e:
                logger.warning(
                    f"[{display_name}] 帖子 {post_id} 评论变体获取失败 "
                    f"(page={page}, count={count}, asc={asc}): {e}"
                )
                break

        return new_count
