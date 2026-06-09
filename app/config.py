"""配置加载: 读取项目根 .env (若存在) + 环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """加载项目根 .env 文件到 os.environ.

    优先级 (`.env` 有值优先, 无值走父 env):
    - .env 显式填了值 → **覆盖** 父进程已 export 的同名 env
    - .env 留空        → 不写, 保留父进程值 (兼容 ccswitch 等切换器)

    历史 (2026-06-09 改): 旧版用 setdefault, 父 env 永远优先, 改 .env 后
    需要 unset shell env 才生效, 违反"改配置文件立即生效"直觉.
    """
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v:
            # .env 显式填了值 → 强制覆盖, 立即生效
            os.environ[k] = v


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

        优先级 (`.env` 有值优先, 无值走父 env):
        - .env 填了值      → **以 .env 为准** (覆盖 shell export)
        - .env 留空        → 保留父进程 env (兼容 ccswitch / shell rc 等切换工具)
        - 两边都没         → SDK 走 ~/.claude/ 默认认证

        历史 (2026-06-09 改): 旧版用 setdefault, 父 env 永远优先于 .env.
        但这违反"改 .env 立即生效"直觉, 开源用户调试很难定位为何配置不生效.
        现版让 .env 显式赋值时覆盖父 env, 留空时仍兼容 ccswitch 切换器.
        """
        for k, v in (
            ("ANTHROPIC_BASE_URL", self.anthropic_base_url),
            ("ANTHROPIC_AUTH_TOKEN", self.anthropic_auth_token),
            ("ANTHROPIC_MODEL", self.anthropic_model),
        ):
            if v:
                # .env 显式填了值 → 强制覆盖父 env, 立即生效
                os.environ[k] = v
