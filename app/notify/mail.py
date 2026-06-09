"""邮件通知 - SMTP 配置存于 app_settings.mail, 漏洞产出时异步发送。

为什么不用 .env: 用户在 UI 里就能改, 不需要重启服务。密码字段在前端编辑时
若提交空字符串则保留旧值, 避免 GET 接口回显时把空密码覆盖回去。
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from typing import Any

from .. import store

SETTING_KEY = "mail"


@dataclass
class MailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    use_ssl: bool = True       # 465=ssl, 587=starttls, 25=plain
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    notify_on_finding: bool = True   # 有漏洞产出时发
    notify_on_failure: bool = False  # 任务失败时发 (噪音大, 默认关)

    @classmethod
    def from_dict(cls, d: dict | None) -> "MailConfig":
        if not d:
            return cls()
        # to_addrs 兼容字符串 / 列表
        to = d.get("to_addrs", [])
        if isinstance(to, str):
            to = [x.strip() for x in to.replace(";", ",").split(",") if x.strip()]
        return cls(
            enabled=bool(d.get("enabled", False)),
            smtp_host=str(d.get("smtp_host", "")),
            smtp_port=int(d.get("smtp_port", 465) or 465),
            use_ssl=bool(d.get("use_ssl", True)),
            username=str(d.get("username", "")),
            password=str(d.get("password", "")),
            from_addr=str(d.get("from_addr", "")),
            to_addrs=list(to),
            notify_on_finding=bool(d.get("notify_on_finding", True)),
            notify_on_failure=bool(d.get("notify_on_failure", False)),
        )

    def to_public_dict(self) -> dict:
        """对外回显的安全版: 不返回明文密码, 只指示是否已设置。"""
        d = asdict(self)
        d.pop("password", None)
        d["password_set"] = bool(self.password)
        return d

    def is_ready(self) -> bool:
        return bool(
            self.enabled
            and self.smtp_host
            and self.from_addr
            and self.to_addrs
        )


# ---------- 持久化 ----------

def get_mail_config() -> MailConfig:
    return MailConfig.from_dict(store.get_setting(SETTING_KEY) or {})


async def save_mail_config(payload: dict) -> MailConfig:
    """合并保存。password 为空 -> 保留旧密码 (避免 UI 回显丢失)。"""
    current = get_mail_config()
    merged = {**asdict(current), **payload}
    if not (payload.get("password") or "").strip():
        merged["password"] = current.password
    cfg = MailConfig.from_dict(merged)
    await store.set_setting(SETTING_KEY, asdict(cfg))
    return cfg


# ---------- 发送 ----------

def _send_sync(cfg: MailConfig, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to_addrs)
    msg["Subject"] = subject
    msg.set_content(body)

    if cfg.use_ssl:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=20) as s:
            if cfg.username:
                s.login(cfg.username, cfg.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as s:
            try:
                s.starttls(context=ssl.create_default_context())
            except smtplib.SMTPNotSupportedError:
                pass
            if cfg.username:
                s.login(cfg.username, cfg.password)
            s.send_message(msg)


async def send_mail(subject: str, body: str, cfg: MailConfig | None = None) -> None:
    cfg = cfg or get_mail_config()
    if not cfg.is_ready():
        return
    await asyncio.to_thread(_send_sync, cfg, subject, body)


async def send_finding_notification(task: dict, stats: dict[str, Any]) -> None:
    """有漏洞产出时调用。task = store.get_task() 的 dict。"""
    cfg = get_mail_config()
    if not cfg.is_ready() or not cfg.notify_on_finding:
        return
    proj = task.get("project_name") or task.get("project_id") or "—"
    subject = f"[secweb] 新漏洞: {task.get('url', '')[:80]}"
    lines = [
        "secweb 检测到新的有效漏洞产出",
        "",
        f"目标:    {task.get('url', '')}",
        f"项目:    {proj}",
        f"任务 ID: {task.get('id', '')}",
        f"报告:    {task.get('report_path', '(未知)')}",
        f"工作目录: {task.get('workdir', '')}",
        "",
        "当前面板状态:",
        f"  排队 {stats.get('queued', 0)} / 运行 {stats.get('running', 0)} / "
        f"待补充 {stats.get('needs_input', 0)} / 完成 {stats.get('done', 0)} / "
        f"失败 {stats.get('failed', 0)} / 停止 {stats.get('stopped', 0)} / "
        f"发现 {stats.get('findings', 0)}",
    ]
    await send_mail(subject, "\n".join(lines), cfg)
