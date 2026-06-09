"""配置加载: 读取项目根 .env (若存在) + 环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


@dataclass
class Config:
    password: str
    host: str
    port: int
    default_concurrency: int
    default_prompt: str
    claude_bin: str
    task_timeout: int
    runs_dir: Path
    secret_key: str
    # Anthropic API (SDK 用):
    # 优先级 ANTHROPIC_BASE_URL > 默认空 (= SDK 用官方 api.anthropic.com)
    # ccswitch 等外部工具若已 export 同名 env, lifespan 用 setdefault 不覆盖
    anthropic_base_url: str
    anthropic_auth_token: str
    anthropic_model: str

    @classmethod
    def load(cls) -> "Config":
        return cls(
            password=os.environ.get("SECWEB_PASSWORD", "changeme"),
            host=os.environ.get("SECWEB_HOST", "127.0.0.1"),
            port=int(os.environ.get("SECWEB_PORT", "8765")),
            default_concurrency=int(
                os.environ.get("SECWEB_DEFAULT_CONCURRENCY", "3")
            ),
            default_prompt=os.environ.get(
                "SECWEB_DEFAULT_PROMPT",
                # 授权书已由 scheduler 注入到任务 CWD 的 CLAUDE.md, 此处只下发动作指令,
                # 避免任何让模型自然 end_turn 的尾随自然语言 (如"回复明白")
                "/hack {url} auto",
            ),
            claude_bin=os.environ.get("SECWEB_CLAUDE_BIN", "claude"),
            task_timeout=int(os.environ.get("SECWEB_TASK_TIMEOUT", "0")),
            runs_dir=ROOT / "runs",
            secret_key=os.environ.get(
                "SECWEB_SECRET_KEY", "secweb-" + os.environ.get("SECWEB_PASSWORD", "x")
            ),
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", ""),
            anthropic_auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", ""),
        )

    def export_anthropic_env(self) -> None:
        """把 .env 里读到的 Anthropic 配置回写到 os.environ, 给 SDK 子进程继承.

        用 setdefault 实现"配置文件 < 父进程 env"的优先级:
        - 父进程已 export (ccswitch 等) → 保持原值, 不覆盖
        - 父进程未 export → 用 .env 里的值
        """
        for k, v in (
            ("ANTHROPIC_BASE_URL", self.anthropic_base_url),
            ("ANTHROPIC_AUTH_TOKEN", self.anthropic_auth_token),
            ("ANTHROPIC_MODEL", self.anthropic_model),
        ):
            if v and not os.environ.get(k):
                os.environ[k] = v
