"""
用户发言跟踪爬虫：支持增量更新与历史补全。
"""

import time
from datetime import datetime

from core.client import XueqiuClient
from core.exceptions import CookieExpired, MaxRetryExceeded, RequestFailed
from storage.database import Database
from scrapers.api_endpoints import (
    build_user_timeline_url,
    build_user_timeline_params,
    parse_user_timeline_response,
    extract_user_status_fields,
    extract_post_fields,
    extract_comment_fields,
    build_single_status_url,
    build_single_status_params,
    parse_single_status_response,
    build_comments_v3_url,
    build_comments_v3_main_params,
    build_comments_v3_child_params,
    parse_comments_v3_response,
    build_comments_url,
    build_comments_params,
    parse_comments_response,
)
from utils.html_cleaner import html_to_text
from utils.logger import get_logger

logger = get_logger()


class UserTracker:
    """用户发言跟踪爬虫。"""

    def __init__(self, client: XueqiuClient, db: Database, config: dict):
        """
        Args:
            client: HTTP 客户端
            db: 数据库实例
            config: scraping 段配置
        """
        self.client = client
        self.db = db
        self.max_pages = config.get("max_pages_per_user", 20)
        self.history_confirm_runs = config.get("history_confirm_runs", 2)
        self.history_chunk_pages = max(1, int(config.get("user_history_chunk_pages", 3) or 3))
        self.timeline_page_size = int(config.get("user_timeline_page_size", 20) or 20)
        self.timeline_request_timeout_ms = int(config.get("timeline_request_timeout_ms", 30000) or 30000)
        self.timeline_request_retries = max(1, int(config.get("timeline_request_retries", 2) or 2))
        # 评论抓取配置（复用股票模式的参数）
        self.comment_v3_enabled = bool(config.get("comment_v3_enabled", True))
        self.comment_v3_page_size = max(1, int(config.get("comment_v3_page_size", 20) or 20))
        self.comment_v3_child_max_pages = max(1, int(config.get("comment_v3_child_max_pages", 15) or 15))
        self.comment_post_budget_seconds = float(config.get("user_comment_post_budget_seconds", 30) or 30)
        self.max_comments_per_post = int(config.get("user_comment_max_pages", 20) or 20)
        self.fetch_comments_enabled = bool(config.get("user_fetch_comments_enabled", True))
        self.user_incremental_comment_gap_limit = max(
            0, int(config.get("user_incremental_comment_gap_limit", 60) or 60)
        )
        self.history_cursor_enabled = bool(config.get("history_cursor_enabled", True))
        # Transport 配置（参照评论回填模式）
        self.timeline_transport = config.get("user_timeline_transport", "auto")
        self.history_timeline_transport = config.get("user_history_timeline_transport", "page")
        self.history_timeline_fallback_transport = config.get(
            "user_history_timeline_fallback_transport", "isolated_page"
        )

    def _resolve_comment_backfill_target(self, status_data: dict) -> tuple[str, str]:
        """根据用户发言解析评论回填的目标帖子。"""
        if not status_data:
            return "", ""
        if bool(status_data.get("is_original_post", True)):
            return str(status_data.get("id") or "").strip(), "original"
        parent_id = str(status_data.get("parent_status_id") or "").strip()
        if parent_id:
            return parent_id, "parent"
        retweet_id = str(status_data.get("retweet_status_id") or "").strip()
        if retweet_id:
            return retweet_id, "retweet"
        return "", ""

    @staticmethod
    def _should_stop_update_after_page(page_new_statuses: int, page_hit_synced_boundary: bool) -> bool:
        """
        更新模式下，只有整页都没有新增发言，且页内已经命中数据库同步边界时，才停止翻页。
        这样可以避免第一页混入置顶/乱序旧帖时过早退出。
        """
        return page_new_statuses == 0 and page_hit_synced_boundary

    def _backfill_target_post_once(self, target_post_id: str, display_name: str = "") -> tuple[int, int]:
        """确保目标帖子存在，并补齐其评论。返回 (new_posts, new_comments)。"""
        existed = self.db.get_post(target_post_id) is not None
        new_comments = self._fetch_and_save_parent_post(target_post_id, display_name)
        exists_now = self.db.get_post(target_post_id) is not None
        new_posts = 1 if (not existed and exists_now) else 0
        return new_posts, new_comments

    def _backfill_target_post_with_retry(self, target_post_id: str, display_name: str = "") -> dict:
        """
        单帖评论回填：失败一次轻恢复重试；第二次失败则 deferred。
        """
        last_error = ""
        last_category = ""
        for attempt in range(1, 3):
            try:
                new_posts, new_comments = self._backfill_target_post_once(
                    target_post_id,
                    f"{display_name} 帖子回填[{attempt}/2]",
                )
                return {
                    "status": "success",
                    "new_posts": new_posts,
                    "new_comments": new_comments,
                    "error": "",
                    "error_category": "",
                }
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                failure_meta = self.client.get_last_failure_meta() or {}
                last_category = str(failure_meta.get("category") or "")
                logger.warning(
                    f"[{display_name}] 目标帖子 {target_post_id} 回填失败 {attempt}/2: "
                    f"{last_error} category={last_category}"
                )
                if attempt == 1:
                    try:
                        self.client.rate_limiter.on_failure()
                    except Exception:
                        pass
                    continue
        return {
            "status": "deferred",
            "new_posts": 0,
            "new_comments": 0,
            "error": last_error,
            "error_category": last_category,
        }

    def backfill_user_comment_threads(
        self,
        user_id: str,
        screen_name: str = "",
        *,
        max_statuses: int | None = None,
        max_posts: int | None = None,
        days: int | None = None,
        resume: bool = True,
    ) -> dict:
        """
        从 user_statuses 推导目标帖子，独立执行帖子本体与评论回填。
        """
        display_name = f"用户 {user_id}" + (f"({screen_name})" if screen_name else "")
        started_at_iso = datetime.now().isoformat()
        started_at_ms = int(time.time() * 1000)
        logger.info(f"---------- 开始用户评论独立回填 {display_name} ----------")

        state = self.db.get_user_comment_backfill_state(user_id)
        deferred_ids = [str(pid).strip() for pid in (state.get("deferred_post_ids") or []) if str(pid).strip()]
        after_created_at = int(state.get("last_status_created_at") or 0) if resume else 0
        after_status_id = str(state.get("last_status_id") or "") if resume else ""

        processed_statuses = int(state.get("processed_statuses") or 0)
        discovered_posts = int(state.get("discovered_posts") or 0)
        processed_posts = int(state.get("processed_posts") or 0)
        new_posts_total = int(state.get("new_posts") or 0)
        new_comments_total = int(state.get("new_comments") or 0)
        last_error_category = str(state.get("last_error_category") or "")
        seen_targets: set[str] = set(deferred_ids)
        run_processed_statuses = 0
        run_discovered_posts = 0
        run_processed_posts = 0
        run_new_posts = 0
        run_new_comments = 0
        final_status = "success"
        posts_budget = max(1, int(max_posts)) if max_posts else None
        statuses_budget = max(1, int(max_statuses)) if max_statuses else None

        self.db.update_user_comment_backfill_runtime(
            user_id,
            state="running",
            status_id=after_status_id,
            post_id=deferred_ids[0] if deferred_ids else "",
            started_at=started_at_ms,
        )

        def persist_state(*, status_id="", status_created_at=0, post_id="", runtime_state="running"):
            self.db.upsert_user_comment_backfill_state(
                user_id,
                last_status_id=status_id or after_status_id,
                last_status_created_at=status_created_at or after_created_at,
                last_target_post_id=post_id or state.get("last_target_post_id", ""),
                processed_statuses=processed_statuses + run_processed_statuses,
                discovered_posts=discovered_posts + run_discovered_posts,
                processed_posts=processed_posts + run_processed_posts,
                new_posts=new_posts_total + run_new_posts,
                new_comments=new_comments_total + run_new_comments,
                deferred_posts=len(deferred_ids),
                deferred_post_ids=deferred_ids,
                last_error_category=last_error_category,
                runtime_state=runtime_state,
                runtime_status_id=status_id or "",
                runtime_post_id=post_id or "",
                runtime_started_at=started_at_ms,
                runtime_updated_at=int(time.time() * 1000),
            )

        try:
            # 优先处理上轮 deferred 的帖子
            while deferred_ids and (posts_budget is None or run_processed_posts < posts_budget):
                target_post_id = deferred_ids[0]
                self.db.update_user_comment_backfill_runtime(
                    user_id,
                    state="running",
                    status_id=after_status_id,
                    post_id=target_post_id,
                    started_at=started_at_ms,
                )
                result = self._backfill_target_post_with_retry(target_post_id, display_name)
                if result["status"] == "success":
                    deferred_ids.pop(0)
                    run_processed_posts += 1
                    run_new_posts += int(result.get("new_posts") or 0)
                    run_new_comments += int(result.get("new_comments") or 0)
                    last_error_category = ""
                else:
                    last_error_category = str(result.get("error_category") or "")
                    # 避免同一个 deferred 帖子本轮反复空转
                    break
                persist_state(post_id=target_post_id)

            statuses = self.db.iter_user_statuses_for_comment_backfill(
                user_id,
                after_created_at=after_created_at,
                after_status_id=after_status_id,
                days=days,
                limit=statuses_budget,
            )

            for status_data in statuses:
                if posts_budget is not None and run_processed_posts >= posts_budget:
                    break

                status_id = str(status_data.get("id") or "").strip()
                status_created_at = int(status_data.get("created_at") or 0)
                run_processed_statuses += 1
                self.db.update_user_comment_backfill_runtime(
                    user_id,
                    state="running",
                    status_id=status_id,
                    post_id="",
                    started_at=started_at_ms,
                )

                target_post_id, target_kind = self._resolve_comment_backfill_target(status_data)
                after_status_id = status_id
                after_created_at = status_created_at
                if not target_post_id:
                    persist_state(status_id=status_id, status_created_at=status_created_at)
                    continue
                if target_post_id in seen_targets:
                    persist_state(status_id=status_id, status_created_at=status_created_at)
                    continue

                seen_targets.add(target_post_id)
                run_discovered_posts += 1
                self.db.update_user_comment_backfill_runtime(
                    user_id,
                    state="running",
                    status_id=status_id,
                    post_id=target_post_id,
                    started_at=started_at_ms,
                )
                result = self._backfill_target_post_with_retry(
                    target_post_id,
                    f"{display_name} {target_kind}",
                )
                if result["status"] == "success":
                    run_processed_posts += 1
                    run_new_posts += int(result.get("new_posts") or 0)
                    run_new_comments += int(result.get("new_comments") or 0)
                    last_error_category = ""
                else:
                    if target_post_id not in deferred_ids:
                        deferred_ids.append(target_post_id)
                    last_error_category = str(result.get("error_category") or "")
                persist_state(
                    status_id=status_id,
                    status_created_at=status_created_at,
                    post_id=target_post_id,
                )
                elapsed = max(1.0, time.time() - (started_at_ms / 1000))
                eta_str = ""
                if run_processed_posts > 0:
                    per_post = elapsed / run_processed_posts
                    eta = per_post * max(0, len(deferred_ids))
                    if eta > 0:
                        eta_str = f" | ETA {eta:.0f}s"
                print(
                    f"  [用户评论回填] {display_name} | status {run_processed_statuses}"
                    f" | 目标帖 {run_discovered_posts}"
                    f" | 已补 {run_processed_posts}"
                    f" | 新帖 {run_new_posts}"
                    f" | 新评论 {run_new_comments}"
                    f" | deferred {len(deferred_ids)}{eta_str}",
                    flush=True,
                )

            persist_state(
                status_id=after_status_id,
                status_created_at=after_created_at,
                post_id=state.get("last_target_post_id", ""),
                runtime_state="deferred" if deferred_ids else "completed",
            )
            final_status = "partial" if deferred_ids else "success"
            return {
                "user_id": user_id,
                "screen_name": screen_name,
                "status": final_status,
                "processed_statuses": run_processed_statuses,
                "discovered_posts": run_discovered_posts,
                "processed_posts": run_processed_posts,
                "new_posts": run_new_posts,
                "new_comments": run_new_comments,
                "deferred_posts": len(deferred_ids),
                "last_error_category": last_error_category,
            }
        except Exception as e:
            last_error_category = str(self.client.get_last_failure_meta().get("category", "") or "")
            final_status = "failed"
            persist_state(
                status_id=after_status_id,
                status_created_at=after_created_at,
                post_id=state.get("last_target_post_id", ""),
                runtime_state="failed",
            )
            raise
        finally:
            self.db.log_scrape(
                task_type="user_comment_backfill",
                target=user_id,
                status=final_status,
                new_items_count=run_new_comments,
                error_message=last_error_category,
                started_at=started_at_iso,
                duration_seconds=time.time() - (started_at_ms / 1000),
            )
            self.db.clear_user_comment_backfill_runtime(user_id)

    def _timeline_transport_for_mode(self, mode: str) -> str:
        """根据模式选择 transport 类型（参照 stock_comments 模式）。"""
        if mode == "backfill":
            return self.history_timeline_transport
        return self.timeline_transport

    def _fetch_timeline_page(
        self,
        user_id: str,
        page: int,
        mode: str = "update",
        transport_override: str | None = None,
    ):
        transport = transport_override or self._timeline_transport_for_mode(mode)
        data = self.client.get(
            build_user_timeline_url(),
            params=build_user_timeline_params(user_id, page=page),
            referer_path=f"/u/{user_id}",
            timeout_ms=self.timeline_request_timeout_ms,
            max_retries=self.timeline_request_retries,
            transport=transport,
        )
        statuses, max_page = parse_user_timeline_response(data)
        times = [int((s or {}).get("created_at") or 0) for s in statuses if (s or {}).get("created_at")]
        return {
            "statuses": statuses,
            "max_page": max_page,
            "newest": max(times) if times else 0,
            "oldest": min(times) if times else 0,
        }

    def _fetch_post_comments(self, post_id: str, display_name: str = "", symbol: str = "") -> int:
        """
        抓取指定帖子的全部评论并入库（参照 stock_comments._scrape_post_comments）。

        Returns:
            新增评论数
        """
        new_count = 0
        seen_comment_ids = set()
        started_at = time.time()
        deadline = started_at + self.comment_post_budget_seconds
        v3_meta = {"status_reply_count": 0, "has_filtered": False}
        v3_success = False

        self.client.rate_limiter.enter_comment_mode()
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            self.db.clear_post_comment_memberships(post_id, commit=False)

            if self.comment_v3_enabled:
                try:
                    added, v3_meta = self._collect_comment_thread_v3(
                        post_id, seen_comment_ids, display_name, deadline=deadline,
                    )
                    new_count += added
                    v3_success = True
                except (CookieExpired, MaxRetryExceeded):
                    raise
                except Exception as e:
                    logger.warning(f"[{display_name}] 帖子 {post_id} v3 评论抓取失败，回退旧接口: {e}")

            if not v3_success:
                new_count += self._collect_comment_variant(
                    post_id, seen_comment_ids, display_name, deadline=deadline,
                )

            self.db.update_post_comments_scraped(post_id, commit=False)
            self.db.conn.commit()
        except Exception:
            if self.db.conn.in_transaction:
                self.db.conn.rollback()
            raise
        finally:
            self.client.rate_limiter.exit_comment_mode()

        return new_count

    def _collect_comment_thread_v3(self, post_id, seen_comment_ids, display_name, deadline):
        """使用 v3 评论接口收集评论线程（参照 stock_comments 实现）。"""
        added = 0
        max_id = -1
        page = 0
        while time.time() < deadline:
            page += 1
            data = self.client.get(
                build_comments_v3_url(),
                params=build_comments_v3_main_params(post_id, size=self.comment_v3_page_size, max_id=max_id),
                referer_path=f"/",
                transport="page",
            )
            parsed = parse_comments_v3_response(data)
            if not parsed.get("comments"):
                break
            for raw_comment in parsed["comments"]:
                flat_records = extract_comment_fields(raw_comment)
                for record in flat_records:
                    cid = record["id"]
                    if cid not in seen_comment_ids:
                        seen_comment_ids.add(cid)
                        is_new = self.db.save_comment(post_id, record, commit=False)
                        if is_new:
                            added += 1
                        self.db.link_comment_to_post(post_id, cid, commit=False)
            max_id = parsed.get("next_max_id", -1)
            if max_id == -1 or max_id is None:
                break
            # 收集楼中楼
            for raw_comment in parsed["comments"]:
                child_count = int((raw_comment.get("comment_reply_count") or 0))
                if child_count > 0:
                    self._collect_v3_children(
                        post_id, str(raw_comment.get("id", "")),
                        seen_comment_ids, display_name, deadline,
                    )
        return added, parsed

    def _collect_v3_children(self, post_id, comment_id, seen_comment_ids, display_name, deadline):
        """收集楼中楼子回复。"""
        max_id = -1
        for _ in range(self.comment_v3_child_max_pages):
            if time.time() >= deadline:
                break
            data = self.client.get(
                build_comments_v3_url(),
                params=build_comments_v3_child_params(post_id, comment_id, max_id=max_id),
                referer_path=f"/",
                transport="page",
            )
            parsed = parse_comments_v3_response(data)
            children = parsed.get("comments", [])
            if not children:
                break
            for raw_child in children:
                flat_records = extract_comment_fields(raw_child)
                for record in flat_records:
                    cid = record["id"]
                    if cid not in seen_comment_ids:
                        seen_comment_ids.add(cid)
                        self.db.save_comment(post_id, record, commit=False)
                        self.db.link_comment_to_post(post_id, cid, commit=False)
            max_id = parsed.get("next_max_id", -1)
            if max_id == -1 or max_id is None:
                break

    def _collect_comment_variant(self, post_id, seen_comment_ids, display_name, deadline, count=20, asc=False):
        """使用旧版评论接口作为 v3 的回退。"""
        added = 0
        page = 1
        while time.time() < deadline and page <= self.max_comments_per_post:
            data = self.client.get(
                build_comments_url(),
                params=build_comments_params(post_id, count=count, page=page),
                referer_path=f"/",
                transport="page",
            )
            comments, max_page = parse_comments_response(data)
            if not comments:
                break
            for raw_comment in comments:
                flat_records = extract_comment_fields(raw_comment)
                for record in flat_records:
                    cid = record["id"]
                    if cid not in seen_comment_ids:
                        seen_comment_ids.add(cid)
                        is_new = self.db.save_comment(post_id, record, commit=False)
                        if is_new:
                            added += 1
                        self.db.link_comment_to_post(post_id, cid, commit=False)
            if page >= max_page:
                break
            page += 1
        return added

    def _fetch_and_save_parent_post(self, status_id: str, display_name: str = "") -> int:
        """
        获取单条帖子详情并存入 posts 表，然后抓取其评论。

        用于 Type c: 用户评论了别人的帖子，需要把父帖完整抓取。
        Returns:
            新增评论数
        """
        data = self.client.get(
            build_single_status_url(),
            params=build_single_status_params(status_id),
            referer_path=f"/",
            transport="page",
        )
        raw_post = parse_single_status_response(data)
        if not raw_post:
            logger.warning(f"[{display_name}] 无法获取父帖子 {status_id} 详情")
            return 0

        post = extract_post_fields(raw_post)
        post["text_plain"] = html_to_text(post["text_html"])
        # 用户跟踪场景下帖子可能无 symbol，用空字符串
        symbol = post.get("target_symbol", "") or ""
        is_new = self.db.save_post(symbol, post)
        if is_new:
            logger.info(f"[{display_name}] 新增父帖子 {status_id} (symbol={symbol or '无'})")
            if post.get("_user_profile"):
                try:
                    self.db.upsert_user_profile(post["_user_profile"])
                except Exception:
                    pass

        # 检查是否需要抓评论
        progress = self.db.get_post_comment_progress(status_id) or {}
        scraped = progress.get("comments_scraped", 0)
        claimed = post.get("reply_count", 0)
        if claimed > 0 and scraped < claimed:
            return self._fetch_post_comments(status_id, display_name, symbol=symbol)
        return 0

    def _refresh_user_comment_gaps(self, user_id: str, display_name: str = "") -> int:
        """
        增量同步后，补扫该用户旧发言下后来新增的评论。

        这一步不是重新翻用户时间线，而是针对该用户已入库帖子里
        仍然存在 reply_count > comments_scraped 的帖子做一次评论修复。
        """
        if not self.fetch_comments_enabled or self.user_incremental_comment_gap_limit <= 0:
            return 0

        gap_posts = self.db.get_user_comment_gap_posts(
            user_id, limit=self.user_incremental_comment_gap_limit
        )
        if not gap_posts:
            return 0

        logger.info(
            f"[{display_name}] 增量后置评论修复开始: {len(gap_posts)} 个旧发言仍有评论缺口"
        )
        added_total = 0
        for idx, post in enumerate(gap_posts, start=1):
            post_id = str(post.get("id", ""))
            claimed = int(post.get("reply_count", 0) or 0)
            scraped = int(post.get("comments_scraped", 0) or 0)
            gap = max(0, claimed - scraped)
            if not post_id or gap <= 0:
                continue
            try:
                added = self._fetch_post_comments(
                    post_id,
                    f"{display_name} 旧发言评论修复[{idx}/{len(gap_posts)}]",
                    symbol=str(post.get("symbol", "") or ""),
                )
                added_total += added
                progress = self.db.get_post_comment_progress(post_id) or {}
                logger.info(
                    f"[{display_name}] 旧发言评论修复 {idx}/{len(gap_posts)} | "
                    f"帖子 {post_id} 差 {gap} 条 -> 已抓 {progress.get('comments_scraped', 0)} / {claimed}"
                )
            except Exception as e:
                logger.warning(
                    f"[{display_name}] 旧发言评论修复失败 {idx}/{len(gap_posts)} | 帖子 {post_id}: {e}"
                )
        return added_total

    def _locate_history_start_page(self, user_id: str, boundary_time: int) -> tuple:
        """二分定位历史模式起始页。返回 (start_page, page_cache, max_page)。"""
        first = self._fetch_timeline_page(user_id, 1, mode="backfill")
        max_page = int(first.get("max_page") or 0)
        if boundary_time <= 0 or max_page <= 1:
            return 1, {1: first}, max_page

        page_cache = {1: first}
        if first.get("oldest", 0) < boundary_time:
            return 1, page_cache, max_page

        def get_page(mid: int):
            if mid not in page_cache:
                page_cache[mid] = self._fetch_timeline_page(user_id, mid, mode="backfill")
            return page_cache[mid]

        low, high = 1, max_page
        candidate = max_page + 1
        while low <= high:
            mid = (low + high) // 2
            info = get_page(mid)
            oldest = info.get("oldest", 0)
            newest = info.get("newest", 0)
            if not info.get("statuses"):
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

    def _resolve_history_start_page(self, user_id: str, boundary_time: int, display_name: str) -> tuple:
        """优先从 cursor 恢复，回退到二分定位。"""
        if self.history_cursor_enabled:
            cursor = self.db.get_user_history_cursor(user_id)
            cursor_page = int(cursor.get("page") or 0)
            if cursor_page > 1:
                try:
                    info = self._fetch_timeline_page(user_id, cursor_page, mode="backfill")
                    max_page = int(info.get("max_page") or 0)
                    if info.get("statuses") and (max_page <= 0 or cursor_page <= max_page):
                        if info.get("oldest", 0) >= boundary_time and boundary_time > 0:
                            logger.info(
                                f"[{display_name}] 历史游标 page={cursor_page} 边界可能漂移，"
                                "先继续使用游标页并在页内跳过已覆盖帖子"
                            )
                        else:
                            logger.info(f"[{display_name}] 从历史游标 page={cursor_page} 恢复")
                        return cursor_page, {cursor_page: info}, max_page
                    logger.info(f"[{display_name}] 历史游标 page={cursor_page} 无效，重新定位边界页")
                except Exception as e:
                    logger.warning(f"[{display_name}] 读取历史游标失败，改为重新定位: {e}")
        return self._locate_history_start_page(user_id, boundary_time)

    def track_user(self, user_id: str, screen_name: str = "", mode: str = "update") -> dict:
        """
        跟踪指定用户的发言。

        Args:
            user_id: 雪球用户数字 ID
            screen_name: 用户昵称（仅用于日志）

        Returns:
            跟踪结果统计 dict
        """
        display_name = f"用户 {user_id}" + (f"({screen_name})" if screen_name else "")
        logger.info(f"---------- 开始跟踪 {display_name} ----------")

        started_at = datetime.now().isoformat()
        mode = "backfill" if mode == "history" else mode
        last_check_time = self.db.get_user_last_check_time(user_id)
        oldest_status_time = self.db.get_user_oldest_status_time(user_id)
        stagnant_runs = self.db.get_user_history_stagnant_runs(user_id)

        if mode == "update":
            if last_check_time:
                logger.info(f"上次增量时间: {datetime.fromtimestamp(last_check_time / 1000)}")
            else:
                logger.info("首次跟踪该用户（无历史记录）")
        else:
            if oldest_status_time:
                logger.info(f"历史补全边界: {datetime.fromtimestamp(oldest_status_time / 1000)} 之前")
            else:
                logger.info("历史补全首次执行：当前无最老发言边界，将从最新开始补")

        total_new = 0
        total_new_comments = 0
        status = "success"
        error_msg = ""
        error_category = ""
        latest_status_time = last_check_time or 0
        oldest_crawled_status_time = oldest_status_time or 0
        progress_made = False
        history_complete = False
        last_page_attempted = 0
        track_start_time = time.time()
        runtime_started_at = int(track_start_time * 1000)
        mode_label = "历史补全" if mode == "backfill" else "增量同步"

        self.db.update_user_runtime_progress(
            user_id,
            mode=mode,
            state="running",
            page=0,
            chunk=0,
            total_pages=self.max_pages,
            started_at=runtime_started_at,
        )

        try:
            page = 1
            pages_processed = 0
            should_stop = False
            page_cache = {}
            located_max_page = 0  # 历史模式定位到的最大页数（backfill/无历史时为 0）
            max_page = 0  # 当前可见最大页数（首次请求后更新）
            page_new_statuses = 0  # 当前页新增发言数
            page_new_comments = 0  # 当前页新增评论数

            if mode == "backfill" and oldest_status_time:
                page, page_cache, located_max_page = self._resolve_history_start_page(
                    user_id,
                    oldest_status_time,
                    display_name,
                )
                if page_cache:
                    logger.info(
                        f"[{display_name}] 历史模式从定位页 {page} 开始，跳过数据库已覆盖的新页"
                    )
                if located_max_page > 0 and page > located_max_page:
                    logger.info(f"[{display_name}] 数据库最老时间已接近当前可见最早发言，无需重扫新页")
                    history_complete = True

            while not should_stop and pages_processed < self.max_pages and not history_complete:
                last_page_attempted = page
                runtime_chunk = max(1, ((pages_processed) // self.history_chunk_pages) + 1) if mode == "backfill" else 1
                self.db.update_user_runtime_progress(
                    user_id,
                    mode=mode,
                    state="running",
                    page=page,
                    chunk=runtime_chunk,
                    total_pages=max_page or located_max_page or self.max_pages,
                    started_at=runtime_started_at,
                )
                logger.info(f"[{display_name}] 获取发言列表 第 {page}/{located_max_page or '?'} 页...")
                page_new_statuses = 0
                page_new_comments = 0
                page_t0 = time.time()
                page_hit_synced_boundary = False
                page_boundary_status_id = ""

                if page in page_cache:
                    page_info = page_cache.pop(page)
                else:
                    page_info = self._fetch_timeline_page(user_id, page, mode=mode)

                statuses = page_info["statuses"]
                max_page = page_info["max_page"]

                if not statuses:
                    logger.info(f"[{display_name}] 第 {page} 页无发言，结束翻页")
                    history_complete = True
                    break

                for raw_status in statuses:
                    status_data = extract_user_status_fields(raw_status)

                    if mode == "update":
                        status_exists = self.db.user_status_exists(status_data["id"])
                        if last_check_time and (
                            (status_data["created_at"] < last_check_time)
                            or (
                                status_data["created_at"] <= last_check_time
                                and status_exists
                            )
                        ):
                            page_hit_synced_boundary = True
                            page_boundary_status_id = status_data["id"]
                            continue
                    else:
                        if oldest_status_time and status_data["created_at"] >= oldest_status_time:
                            continue

                    if status_data["created_at"] > latest_status_time:
                        latest_status_time = status_data["created_at"]

                    if oldest_crawled_status_time == 0 or status_data["created_at"] < oldest_crawled_status_time:
                        oldest_crawled_status_time = status_data["created_at"]
                        self.db.update_user_oldest_status_time(user_id, oldest_crawled_status_time)
                        if not oldest_status_time or oldest_crawled_status_time < oldest_status_time:
                            progress_made = True

                    status_data["text_plain"] = html_to_text(status_data["text_html"])

                    if self.db.save_user_status(status_data):
                        total_new += 1
                        page_new_statuses += 1

                    # ── 评论采集（Type a / Type c）──
                    if self.fetch_comments_enabled and status_data.get("reply_count", 0) > 0:
                        is_original = status_data.get("is_original_post", True)
                        if is_original:
                            # Type a: 用户原创帖 → 抓帖子下所有评论
                            post_id = status_data["id"]
                            progress = self.db.get_post_comment_progress(post_id) or {}
                            scraped = progress.get("comments_scraped", 0)
                            claimed = status_data["reply_count"]
                            if scraped < claimed:
                                try:
                                    nc = self._fetch_post_comments(post_id, display_name)
                                    total_new_comments += nc
                                    page_new_comments += nc
                                except Exception as e:
                                    logger.warning(f"[{display_name}] 帖子 {post_id} 评论抓取失败: {e}")
                        else:
                            # Type c: 用户评论/转发了别人的帖子 → 抓父帖子完整内容+评论
                            parent_id = status_data.get("parent_status_id") or status_data.get("retweet_status_id", "")
                            if parent_id:
                                # 检查父帖子是否已在 posts 表中
                                existing = self.db.get_post(parent_id)
                                if not existing:
                                    try:
                                        nc = self._fetch_and_save_parent_post(parent_id, display_name)
                                        total_new_comments += nc
                                        page_new_comments += nc
                                    except Exception as e:
                                        logger.warning(f"[{display_name}] 父帖子 {parent_id} 抓取失败: {e}")
                                else:
                                    # 父帖子已存在，检查评论是否需要补全
                                    progress = self.db.get_post_comment_progress(parent_id) or {}
                                    scraped = progress.get("comments_scraped", 0)
                                    claimed = int(existing.get("reply_count", 0) or 0)
                                    if scraped < claimed:
                                        try:
                                            nc = self._fetch_post_comments(parent_id, display_name)
                                            total_new_comments += nc
                                            page_new_comments += nc
                                        except Exception as e:
                                            logger.warning(f"[{display_name}] 父帖子 {parent_id} 评论补全失败: {e}")
                                # Type c: 也把用户这条评论存为帖子本身（如果它是转发帖包含正文）
                                retweet_id = status_data.get("retweet_status_id", "")
                                if retweet_id and retweet_id != parent_id:
                                    existing_rt = self.db.get_post(retweet_id)
                                    if not existing_rt:
                                        try:
                                            nc = self._fetch_and_save_parent_post(retweet_id, display_name)
                                            total_new_comments += nc
                                            page_new_comments += nc
                                        except Exception as e:
                                            logger.warning(f"[{display_name}] 转发原帖 {retweet_id} 抓取失败: {e}")

                # ── 每页进度输出 ──
                page_elapsed = time.time() - page_t0
                elapsed_total = time.time() - track_start_time
                pages_per_sec = (pages_processed + 1) / max(elapsed_total, 0.01)
                remaining_pages = max(0, (max_page or self.max_pages) - page)
                eta_sec = remaining_pages / max(pages_per_sec, 0.001) if remaining_pages > 0 else 0
                eta_str = f"预计剩余 {eta_sec:.0f}s" if eta_sec > 0 else ""
                mode_tag = "补全" if mode == "backfill" else "增量"
                print(
                    f"  [{mode_tag}] page {page}/{max_page or '?'} | chunk {runtime_chunk} | "
                    f"本页 +{page_new_statuses}发言 +{page_new_comments}评论 | "
                    f"累计 {total_new}发言 {total_new_comments}评论 | "
                    f"耗时 {page_elapsed:.1f}s{eta_str}",
                    flush=True,
                )

                if mode == "update" and self._should_stop_update_after_page(
                    page_new_statuses,
                    page_hit_synced_boundary,
                ):
                    if page_boundary_status_id:
                        logger.info(
                            f"[{display_name}] 当前页未发现新增发言，且命中数据库最新重复发言 "
                            f"(id={page_boundary_status_id}), 停止翻页"
                        )
                    should_stop = True
                    break

                if page >= max_page:
                    history_complete = mode == "backfill"
                    if mode == "backfill":
                        self.db.clear_user_history_cursor(user_id)
                    break

                # backfill 模式下更新游标
                if mode == "backfill":
                    cursor_oldest = oldest_crawled_status_time or page_info.get("oldest", 0)
                    self.db.update_user_history_cursor(user_id, page + 1, cursor_oldest)

                pages_processed += 1
                page += 1

            if latest_status_time > 0:
                self.db.update_user_check_time(user_id, latest_status_time)
            elif not last_check_time:
                self.db.update_user_check_time(user_id, int(time.time() * 1000))

            if mode == "update":
                repaired_comments = self._refresh_user_comment_gaps(user_id, display_name)
                total_new_comments += repaired_comments

            if mode == "update":
                self.db.update_user_last_sync_time(user_id, int(time.time() * 1000))

            if oldest_crawled_status_time > 0:
                self.db.update_user_oldest_status_time(user_id, oldest_crawled_status_time)
            if mode == "backfill":
                if progress_made:
                    stagnant_runs = 0
                else:
                    stagnant_runs += 1
                self.db.set_user_history_stagnant_runs(user_id, stagnant_runs)
                confirmed_complete = history_complete and stagnant_runs >= self.history_confirm_runs
                self.db.mark_user_history_complete(user_id, confirmed_complete)
            else:
                self.db.set_user_history_stagnant_runs(user_id, 0)

        except CookieExpired:
            status = "failed"
            error_msg = "Cookie 已失效"
            raise

        except MaxRetryExceeded as e:
            status = "partial"
            error_msg = str(e)
            logger.error(f"[{display_name}] 达到最大重试次数: {e}")

        except RequestFailed as e:
            status = "deferred" if e.category == "http_400_10022" else "failed"
            error_msg = str(e)
            error_category = e.category
            failure_meta = self.client.get_last_failure_meta()
            logger.error(
                f"[{display_name}] 请求失败: {e} | "
                f"page={last_page_attempted or page} "
                f"category={failure_meta.get('category', '')} "
                f"excerpt={str(failure_meta.get('html_excerpt', '') or '')[:200]}",
                exc_info=True,
            )

        except Exception as e:
            status = "failed"
            error_msg = f"{type(e).__name__}: {e}"
            error_category = self.client.get_last_failure_meta().get("category", "")
            logger.error(f"[{display_name}] 跟踪异常: {e}", exc_info=True)

        finally:
            if status == "deferred":
                self.db.update_user_runtime_progress(
                    user_id,
                    mode=mode,
                    state="deferred",
                    page=last_page_attempted,
                    chunk=max(1, ((pages_processed) // self.history_chunk_pages) + 1) if mode == "backfill" else 1,
                    total_pages=max_page or located_max_page or self.max_pages,
                    started_at=runtime_started_at,
                )
            else:
                self.db.clear_user_runtime_progress(user_id)
            self.db.log_scrape(
                task_type="user_track",
                target=user_id,
                status=status,
                new_items_count=total_new,
                error_message=error_msg,
                started_at=started_at,
            )

        result = {
            "user_id": user_id,
            "screen_name": screen_name,
            "mode": mode,
            "status": status,
            "new_statuses": total_new,
            "new_comments": total_new_comments,
            "error": error_msg,
            "error_category": error_category,
            "last_page": int(last_page_attempted or 0),
        }

        logger.info(
            f"[{display_name}] {mode_label}完成: 新发言 {total_new} 条, 新评论 {total_new_comments} 条, 状态 {status} | "
            f"总耗时 {(time.time() - track_start_time):.1f}s, 处理 {pages_processed} 页"
        )

        return result
