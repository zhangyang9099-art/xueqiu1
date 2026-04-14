"""
定时调度器 — 按 A 股交易日历多时段智能调度

交易日:
  08:30 盘前预扫 — 快速扫描帖子数量变化
  10:00/14:00 盘中扫描 — 热门帖子评论 + 话题热度
  16:00 盘后深扫 — 完整爬取
  20:00 每日摘要
非交易日:
  10:00 轻量巡检
"""

import time
import os
import signal
import sys
from datetime import datetime
from utils.time_utils import is_trading_day
from utils.logger import get_logger

logger = get_logger()


class Scheduler:
    """多时段智能调度器。"""

    def __init__(self, config: dict, run_callback=None, summary_callback=None):
        self.config = config
        self.schedule_cfg = config.get("schedule", {})
        self.run_callback = run_callback
        self.summary_callback = summary_callback
        self._running = True
        self._pid_file = "data/scheduler.pid"

    def start(self, daemon=False):
        """启动调度器。"""
        if daemon:
            self._write_pid()

        signal.signal(signal.SIGINT, self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)

        logger.info("定时调度器已启动")
        logger.info(f"  交易日: 盘前08:30 / 盘中10:00,14:00 / 盘后16:00 / 摘要20:00")
        logger.info(f"  非交易日: 轻巡10:00")
        logger.info("按 Ctrl+C 停止")

        executed_today = set()

        while self._running:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            hm = now.strftime("%H:%M")
            trading = is_trading_day(now)

            # 每天重置已执行记录
            if f"{today}_reset" not in executed_today:
                executed_today = {f"{today}_reset"}

            task_key = f"{today}_{hm}"

            if trading:
                if hm == "08:30" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("pre_market_scan", "盘前预扫")
                elif hm in ("10:00", "14:00") and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("in_market_scan", f"盘中扫描 {hm}")
                elif hm == "16:00" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("post_market_deep", "盘后深扫")
                elif hm == "20:00" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("daily_digest", "每日摘要")
            else:
                if hm == "10:00" and task_key not in executed_today:
                    executed_today.add(task_key)
                    self._run_task("non_trading_light", "非交易日轻巡")

            time.sleep(30)  # 每30秒检查一次

    def _run_task(self, task_type, desc):
        logger.info(f"[调度] 执行: {desc} ({task_type})")
        try:
            if task_type == "daily_digest" and self.summary_callback:
                self.summary_callback()
            elif self.run_callback:
                self.run_callback(task_type)
        except Exception as e:
            logger.error(f"[调度] {desc} 执行失败: {e}")

    def _handle_stop(self, signum, frame):
        logger.info("调度器收到停止信号")
        self._running = False
        self._cleanup_pid()

    def _write_pid(self):
        os.makedirs(os.path.dirname(self._pid_file), exist_ok=True)
        with open(self._pid_file, "w") as f:
            f.write(str(os.getpid()))

    def _cleanup_pid(self):
        if os.path.exists(self._pid_file):
            os.remove(self._pid_file)
