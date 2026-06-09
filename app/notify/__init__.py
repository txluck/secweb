"""通知模块: 邮件等外部通知渠道。"""
from .mail import (
    MailConfig,
    get_mail_config,
    save_mail_config,
    send_mail,
    send_finding_notification,
)

__all__ = [
    "MailConfig",
    "get_mail_config",
    "save_mail_config",
    "send_mail",
    "send_finding_notification",
]
