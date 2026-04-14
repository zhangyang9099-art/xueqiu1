"""
浏览器实例池 — 管理多个 Playwright 实例实现并发爬取

用法:
  pool = BrowserPool(cookie_manager, config, max_instances=3)
  client = pool.acquire()
  try:
      data = client.get(url, params)
  finally:
      pool.release(client)
  pool.close_all()
"""

import threading
import queue
from utils.logger import get_logger

logger = get_logger()


class BrowserPool:
    """Playwright 浏览器实例池。"""

    def __init__(self, cookie_manager, rate_limiter_config: dict, max_instances: int = 3):
        self.cookie_manager = cookie_manager
        self.rate_limiter_config = rate_limiter_config
        self.max_instances = max_instances
        self._pool = queue.Queue()
        self._all_clients = []
        self._lock = threading.Lock()
        self._created = 0

    def acquire(self):
        """从池中获取一个可用的 XueqiuClient 实例。"""
        # 先尝试从池中取
        try:
            client = self._pool.get_nowait()
            return client
        except queue.Empty:
            pass

        # 池空则创建新实例
        with self._lock:
            if self._created < self.max_instances:
                from core.rate_limiter import RateLimiter
                from core.client import XueqiuClient
                rl = RateLimiter(self.rate_limiter_config)
                client = XueqiuClient(self.cookie_manager, rl, self.rate_limiter_config)
                self._all_clients.append(client)
                self._created += 1
                logger.info(f"浏览器池: 创建实例 #{self._created}/{self.max_instances}")
                return client

        # 已达上限，等待归还
        logger.debug("浏览器池: 等待可用实例...")
        return self._pool.get(timeout=300)

    def release(self, client):
        """归还实例到池中。"""
        self._pool.put(client)

    def close_all(self):
        """关闭所有实例。"""
        for c in self._all_clients:
            try:
                c.close()
            except Exception:
                pass
        self._all_clients.clear()
        self._created = 0
        logger.info("浏览器池: 所有实例已关闭")
