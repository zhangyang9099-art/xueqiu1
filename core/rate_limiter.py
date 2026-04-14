"""
请求节流器 v3 — 自适应 + 可配置评论模式

优化点:
  - 基础间隔 2-5s（或由调用方覆盖）
  - 评论翻页模式可单独配置，也可完全禁用
  - 爆发休息与失败退避可按不同任务类型覆盖
  - on_success/on_failure 真正生效
"""

import time
import random
from collections import deque
from utils.logger import get_logger

logger = get_logger()


class RateLimiter:
    """自适应请求频率控制器 v3。"""

    def __init__(self, config: dict):
        self.min_interval = config.get("min_request_interval", 2.0)
        self.max_interval = config.get("max_request_interval", 5.0)
        self.max_per_minute = config.get("max_requests_per_minute", 20)
        self.burst_rest_count = config.get("burst_rest_count", 95)
        self.burst_rest_min = config.get("burst_rest_seconds_min", 90)
        self.burst_rest_max = config.get("burst_rest_seconds_max", 120)
        self._normal_min_interval = float(self.min_interval)
        self._normal_max_interval = float(self.max_interval)

        # 评论模式参数；评论回填可切到与历史帖子同级别的慢速稳态。
        self._comment_mode_enabled = bool(config.get("comment_mode_enabled", True))
        self._comment_min = float(config.get("comment_mode_min_interval", 1.5) or 1.5)
        self._comment_max = float(config.get("comment_mode_max_interval", 3.0) or 3.0)

        # 历史模式自适应节流；默认关闭，稳定运行一段后可单独打开。
        self._adaptive_enabled = bool(config.get("adaptive_pacing_enabled", False))
        self._adaptive_fast_min = float(
            config.get(
                "adaptive_fast_min_interval",
                max(0.5, self._normal_min_interval - 1.0),
            )
            or max(0.5, self._normal_min_interval - 1.0)
        )
        self._adaptive_fast_max = float(
            config.get(
                "adaptive_fast_max_interval",
                max(self._adaptive_fast_min, self._normal_max_interval - 2.0),
            )
            or max(self._adaptive_fast_min, self._normal_max_interval - 2.0)
        )
        self._adaptive_slow_min = float(
            config.get("adaptive_slow_min_interval", self._normal_min_interval + 1.0)
            or (self._normal_min_interval + 1.0)
        )
        self._adaptive_slow_max = float(
            config.get("adaptive_slow_max_interval", self._normal_max_interval + 3.0)
            or (self._normal_max_interval + 3.0)
        )
        self._adaptive_success_threshold = max(
            1,
            int(config.get("adaptive_success_threshold", 20) or 20),
        )
        self._adaptive_slow_request_count = max(
            1,
            int(config.get("adaptive_slow_request_count", 20) or 20),
        )

        # 自适应状态
        self._current_interval = self.min_interval
        self._consecutive_success = 0
        self._in_comment_mode = False
        self._request_times: deque = deque()
        self._total_requests: int = 0
        self._last_request_time: float = 0.0
        self.on_burst_rest = None
        self._adaptive_clean_successes = 0
        self._adaptive_slow_remaining = 0
        self._adaptive_fast_active = False

    @property
    def total_requests(self):
        return self._total_requests

    def enter_comment_mode(self):
        """进入评论翻页快速模式（同帖子内连续翻页）。"""
        self._in_comment_mode = self._comment_mode_enabled

    def exit_comment_mode(self):
        """退出评论快速模式。"""
        self._in_comment_mode = False

    def wait(self):
        """请求前调用，自适应等待。"""
        if self._in_comment_mode:
            lo, hi = self._comment_min, self._comment_max
        else:
            lo, hi = self._current_wait_bounds()

        elapsed = time.time() - self._last_request_time
        delay = random.uniform(lo, hi)
        if elapsed < delay:
            time.sleep(delay - elapsed)

        # 每分钟滑动窗口
        now = time.time()
        while self._request_times and now - self._request_times[0] > 60:
            self._request_times.popleft()
        if len(self._request_times) >= self.max_per_minute:
            wait_until = self._request_times[0] + 60
            sleep_time = wait_until - now + random.uniform(0.5, 1.5)
            if sleep_time > 0:
                time.sleep(sleep_time)

        # 爆发休息
        self._total_requests += 1
        if self.burst_rest_count > 0 and self._total_requests % self.burst_rest_count == 0:
            rest = random.uniform(self.burst_rest_min, self.burst_rest_max)
            logger.info(f"累计 {self._total_requests} 请求，休息 {rest:.0f}s")
            time.sleep(rest)
            if callable(self.on_burst_rest):
                try:
                    self.on_burst_rest()
                except Exception as e:
                    logger.warning(f"爆发休息后的 session 刷新失败: {e}")

        self._request_times.append(time.time())
        self._last_request_time = time.time()

    def _current_wait_bounds(self):
        base_min, base_max = self._normal_min_interval, self._normal_max_interval
        if self._adaptive_enabled:
            if self._adaptive_slow_remaining > 0:
                base_min, base_max = self._adaptive_slow_min, self._adaptive_slow_max
            elif self._adaptive_fast_active:
                base_min, base_max = self._adaptive_fast_min, self._adaptive_fast_max
        lo = max(base_min, self._current_interval * 0.85)
        hi = max(base_max, self._current_interval * 1.15)
        if hi < lo:
            hi = lo
        return lo, hi

    def on_success(self):
        """请求成功，逐步降低间隔。"""
        self._consecutive_success += 1
        if self._adaptive_enabled and not self._in_comment_mode:
            if self._adaptive_slow_remaining > 0:
                self._adaptive_slow_remaining -= 1
                self._adaptive_clean_successes = 0
                self._adaptive_fast_active = False
            else:
                self._adaptive_clean_successes += 1
                if self._adaptive_clean_successes >= self._adaptive_success_threshold:
                    self._adaptive_fast_active = True
        if self._consecutive_success >= 5:
            self._current_interval = max(self.min_interval, self._current_interval - 0.2)
            self._consecutive_success = 0

    def on_failure(self):
        """请求失败（403/WAF），间隔翻倍。"""
        self._consecutive_success = 0
        if self._adaptive_enabled:
            self._adaptive_slow_remaining = self._adaptive_slow_request_count
            self._adaptive_clean_successes = 0
            self._adaptive_fast_active = False
        old = self._current_interval
        self._current_interval = min(self.max_interval * 3, self._current_interval * 2)
        logger.warning(f"频率自适应: 间隔 {old:.1f}s → {self._current_interval:.1f}s")

    def on_recover(self):
        """WAF 恢复后降回正常。"""
        self._current_interval = self.max_interval

    def reset(self):
        self._request_times.clear()
        self._total_requests = 0
        self._last_request_time = 0.0
        self._current_interval = self.min_interval
        self._consecutive_success = 0
        self._adaptive_clean_successes = 0
        self._adaptive_slow_remaining = 0
        self._adaptive_fast_active = False
