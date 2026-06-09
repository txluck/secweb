"""任务级 metric 收集器 - SDK 工具事件流的单一计数源.

历史: runner.py 同时从 stream-json (pump_stdout) 和 PostToolUse hook 写的
.tool-events.jsonl 累计同一组 metric (mcp_calls / nav_calls / ...), 收尾时
取 max(a, b) 兜底. 三处都要维护、补丁层层叠加.

收敛: 现在 SDK 唯一通道 = PostToolUse hook in-process 回调, 直接调
TaskMetrics.observe(name, input) 累计. 不再依赖 stream-json 解析,
不再依赖 JSONL 落盘后回扫.

阈值常量也搬到这里, 替代 runner.py 顶部那一坨 _MIN_*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# ─── SPA 探索深度阈值 (case_b_browser_missing 用) ──────────────────
# 仅"调过三件套"不算合格, SPA 真正攻击面要靠交互打开.
_MIN_NAV_CALLS = 3
_MIN_UNIQUE_ROUTES = 3
_MIN_INTERACTION_CALLS = 5
_MIN_NETWORK_REQ_CALLS = 3

# 早停阈值 (case_a_early 用)
_EARLY_TURN_THRESHOLD = 8
_EARLY_TOOLCALL_THRESHOLD = 3

# 浏览器降级阈值 (degraded 紫标 / case_b 兜底)
_MIN_BROWSER_CALLS = 5
_BROWSER_NUDGE_BASH_FLOOR = 6

# Bash 输入里出现以下 token, 视为 "用 python 库替代浏览器" 的降级.
# 注意: curl/wget/openssl/nmap 等属于正确工具链, 不算降级.
_PY_WEB_PATTERNS = (
    "requests.", "import requests", "from requests",
    "httpx.", "import httpx", "from httpx",
    "aiohttp", "urllib.request", "urllib3",
    "from playwright", "import playwright",
)

# 浏览器交互类工具集 (browser_click / type / fill_form / select / hover / press / drag)
_INTERACTION_TOOLS = {
    "browser_click", "browser_type", "browser_fill_form",
    "browser_select_option", "browser_hover",
    "browser_press_key", "browser_drag",
}


@dataclass
class TaskMetrics:
    """单任务的工具调用计数与成本统计.

    更新约定: SDK PostToolUse hook 每次调 observe(name, input);
    SDK ResultMessage 到达时调 observe_result(msg).
    """

    # 工具类型计数
    mcp_calls: int = 0           # mcp__playwright__* 全部
    bash_calls: int = 0          # Bash 工具 (含合理的 curl/工具链)
    py_web_calls: int = 0        # Bash 内 python web 降级 (子集)
    # SPA 探索细分
    nav_calls: int = 0           # browser_navigate
    network_req_calls: int = 0   # browser_network_requests
    interaction_calls: int = 0   # click/type/fill_form/select/hover/press/drag
    unique_routes: set[str] = field(default_factory=set)  # navigate 目标 origin+path 集合
    # 来自 ResultMessage
    num_turns: int = 0
    last_stop_reason: str | None = None
    cost_usd: float = 0.0
    total_tokens: int = 0
    duration_ms: int = 0

    def observe(self, tool_name: str, tool_input: Any) -> None:
        """每次 PostToolUse 调一次."""
        if not tool_name:
            return
        if tool_name.startswith("mcp__playwright__"):
            self.mcp_calls += 1
            short = tool_name[len("mcp__playwright__"):]
            if short == "browser_navigate":
                self.nav_calls += 1
                url = ""
                if isinstance(tool_input, dict):
                    url = tool_input.get("url") or ""
                if url:
                    try:
                        pr = urlparse(url)
                        key = f"{pr.scheme}://{pr.netloc}{pr.path}".rstrip("/")
                        if key:
                            self.unique_routes.add(key)
                    except Exception:
                        pass
            elif short == "browser_network_requests":
                self.network_req_calls += 1
            elif short in _INTERACTION_TOOLS:
                self.interaction_calls += 1
        elif tool_name == "Bash":
            self.bash_calls += 1
            cmdtxt = ""
            if isinstance(tool_input, dict):
                cmd = tool_input.get("command", "")
                if isinstance(cmd, str):
                    cmdtxt = cmd.lower()
            if any(k in cmdtxt for k in _PY_WEB_PATTERNS):
                self.py_web_calls += 1

    def observe_result(
        self,
        *,
        num_turns: int | None = None,
        stop_reason: str | None = None,
        cost_usd: float | None = None,
        tokens: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """每条 ResultMessage 到达时调."""
        if isinstance(num_turns, int):
            self.num_turns = num_turns
        if isinstance(stop_reason, str):
            self.last_stop_reason = stop_reason
        if cost_usd is not None:
            self.cost_usd = float(cost_usd)
        if tokens is not None:
            self.total_tokens = int(tokens)
        if duration_ms is not None:
            self.duration_ms = int(duration_ms)

    @property
    def total_tool_calls(self) -> int:
        """所有真实工具调用之和 (含 mcp + bash + py_web)."""
        return self.mcp_calls + self.bash_calls + self.py_web_calls

    def is_early_stop(self) -> bool:
        """case_a_early: 模型几乎没干活就 end_turn."""
        return (
            0 < self.num_turns < _EARLY_TURN_THRESHOLD
            and self.total_tool_calls < _EARLY_TOOLCALL_THRESHOLD
        )

    def is_browser_shallow(self) -> bool:
        """case_b_browser_missing: 跑了真测试但 SPA 探索深度不达标."""
        if self.total_tool_calls < _EARLY_TOOLCALL_THRESHOLD:
            return False  # 空跑由 case_a 处理
        if (
            len(self.unique_routes) < _MIN_UNIQUE_ROUTES
            or self.interaction_calls < _MIN_INTERACTION_CALLS
            or self.network_req_calls < _MIN_NETWORK_REQ_CALLS
        ):
            return True
        # 旧维度兜底: mcp 极低 + bash 暴多 = 降级到 curl 路线
        if (
            self.mcp_calls < _MIN_BROWSER_CALLS
            and self.bash_calls >= _BROWSER_NUDGE_BASH_FLOOR
        ):
            return True
        return False

    def is_degraded(self) -> bool:
        """degraded 紫标四种情形之一.

        (a) 完全没 pw + 用了 python 库
        (b) 跑了真测试但 pw 调用偏少
        (c) Bash 占比明显高于 pw (>= 2x), 大概率被诱导成 curl 路线
        (d) SPA 探索深度不够 (= is_browser_shallow)
        """
        if self.mcp_calls == 0 and self.py_web_calls > 0:
            return True
        if (
            self.mcp_calls < _MIN_BROWSER_CALLS
            and self.bash_calls >= _BROWSER_NUDGE_BASH_FLOOR
        ):
            return True
        if self.mcp_calls > 0 and self.bash_calls >= self.mcp_calls * 2:
            return True
        if self.is_browser_shallow():
            return True
        return False

    def to_db_columns(self) -> dict[str, Any]:
        """落 tasks 表的 metric 列字典."""
        return {
            "mcp_calls": self.mcp_calls,
            "py_web_calls": self.py_web_calls,
            "nav_calls": self.nav_calls,
            "unique_routes": len(self.unique_routes),
            "interaction_calls": self.interaction_calls,
            "network_req_calls": self.network_req_calls,
            "cost_usd": self.cost_usd,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
        }
