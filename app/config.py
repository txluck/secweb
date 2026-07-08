"""配置加载: 读取项目根 .env (若存在) + 环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """加载项目根 .env 文件到 os.environ.

    设计原则: **.env 是 dashboard 的唯一 ANTHROPIC API 配置源, 完全独立于
    本地 shell**. 启动时先清掉父 shell 的 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
    / ANTHROPIC_MODEL, 然后从 .env 加载. 这样:

    - 你本地 ccswitch / .zshrc / 别的工具切来切去, 是给**交互式 CLI** 用的,
      跟 secweb 这个后台服务无关.
    - 改 .env 立即生效, 不需要 unset 任何 shell env.
    - .env 留空时走 SDK 默认 (~/.claude/ 已登录态), 不会意外继承父 shell.

    历史 (2026-06-09):
    - v1: setdefault, 父 env 永远优先 → 改 .env 不生效, 调试很难.
    - v2: .env 有值覆盖, 留空走父 env → 仍受本地 shell 影响.
    - v3 (本版): 主动清父 env, 只认 .env. 完全解耦本地 shell.
    """
    f = ROOT / ".env"
    if not f.exists():
        return

    # 关键: 先清掉父 shell 的 ANTHROPIC_* 配置 (ccswitch / .zshrc 等切换器
    # 设置的值不应影响 secweb 这个独立服务).
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
        os.environ.pop(k, None)

    # 然后从 .env 加载. 显式覆盖 (.env 里写啥就生效啥).
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v:
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
    # 子进程 TEMP 目录 (Windows U+2018 等 unicode 用户名污染 %USERPROFILE% 时,
    # 默认 %TEMP% 路径会导致 PowerShell/curl.exe 命令解析失败).
    # 留空 = 用系统默认. SDK env 注入见 runner_sdk._build_sdk_options.
    temp_dir: str

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
            temp_dir=os.environ.get("SECWEB_TEMP_DIR", ""),
        )

    def export_anthropic_env(self) -> None:
        """把 Config 里的 Anthropic 配置回写到 os.environ, 给 SDK 子进程继承.

        注: _load_dotenv 已在模块加载时清父 env + 写 .env 值, 这里再写一次
        是为了保证 Config 实例化后 (e.g. 测试场景手动构造 Config) 行为一致.

        secweb 独立于本地 shell — ANTHROPIC_* 完全由 .env 决定, 不受
        ccswitch / .zshrc 等本地切换器影响 (见 _load_dotenv 注释).
        """
        for k, v in (
            ("ANTHROPIC_BASE_URL", self.anthropic_base_url),
            ("ANTHROPIC_AUTH_TOKEN", self.anthropic_auth_token),
            ("ANTHROPIC_MODEL", self.anthropic_model),
        ):
            if v:
                os.environ[k] = v
            else:
                # .env 留空 → 确保 os.environ 里也没有 (可能被 _load_dotenv 清过, 这里再保险)
                os.environ.pop(k, None)
