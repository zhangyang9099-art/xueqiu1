"""
自定义异常类型。
"""


class AntiCrawlDetected(Exception):
    """检测到反爬机制（403、IP 封禁等）。"""
    pass


class CookieExpired(Exception):
    """Cookie 已失效（400、需要登录）。"""
    pass


class CaptchaRequired(AntiCrawlDetected):
    """触发了验证码。"""
    pass


class ApiError(Exception):
    """API 返回了错误响应。"""
    pass


class MaxRetryExceeded(Exception):
    """超过最大重试次数。"""
    pass


class RequestFailed(Exception):
    """请求失败，但不一定是反爬。"""

    def __init__(self, category: str, message: str, url: str = "", detail: str = ""):
        super().__init__(message)
        self.category = category
        self.url = url
        self.detail = detail or message
