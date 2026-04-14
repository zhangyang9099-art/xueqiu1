"""
通知模块：在 Cookie 失效、爬虫异常等情况下发送告警。
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from utils.logger import get_logger

logger = get_logger()


class Notifier:
    """告警通知器，支持邮件通知。"""

    def __init__(self, config: dict):
        self.config = config.get("notification", {})
        self.enabled = self.config.get("enabled", False)

    def send_alert(self, subject: str, message: str):
        """
        发送告警通知。

        Args:
            subject: 告警标题
            message: 告警内容
        """
        if not self.enabled:
            logger.warning(f"[告警-未启用通知] {subject}: {message}")
            return

        notify_type = self.config.get("type", "email")

        if notify_type == "email":
            self._send_email(subject, message)
        else:
            logger.warning(f"未知的通知类型: {notify_type}，告警: {subject}")

    def _send_email(self, subject: str, message: str):
        """发送邮件通知。"""
        email_cfg = self.config.get("email", {})
        smtp_server = email_cfg.get("smtp_server", "")
        smtp_port = email_cfg.get("smtp_port", 465)
        sender = email_cfg.get("sender", "")
        password = email_cfg.get("password", "")
        receiver = email_cfg.get("receiver", "")

        if not all([smtp_server, sender, password, receiver]):
            logger.warning(f"邮件配置不完整，无法发送告警: {subject}")
            return

        try:
            msg = MIMEMultipart()
            msg["From"] = sender
            msg["To"] = receiver
            msg["Subject"] = f"[雪球爬虫告警] {subject}"

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            body = f"时间: {timestamp}\n\n{message}"
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(sender, password)
                server.send_message(msg)

            logger.info(f"告警邮件已发送: {subject}")

        except Exception as e:
            logger.error(f"发送告警邮件失败: {e}")

    def notify_cookie_expired(self):
        """Cookie 失效专用告警。"""
        self.send_alert(
            "Cookie 已失效",
            "雪球爬虫的 xq_a_token 已失效，请手动更新 config.yaml 中的 Cookie。\n\n"
            "操作步骤：\n"
            "1. 在 Chrome 浏览器中登录 https://xueqiu.com\n"
            "2. F12 → Application → Cookies → xueqiu.com\n"
            "3. 复制 xq_a_token 的值\n"
            "4. 更新 config.yaml 中的 cookie.xq_a_token 字段\n"
        )

    def notify_scrape_error(self, task_type: str, target: str, error: str):
        """爬取异常告警。"""
        self.send_alert(
            f"爬取异常 - {task_type}",
            f"任务类型: {task_type}\n目标: {target}\n错误: {error}",
        )
