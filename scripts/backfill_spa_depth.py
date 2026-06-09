"""一次性脚本: 扫历史 events 表的 tool_use 行, 反推 SPA 探索深度指标, 回填 tasks 表。

回填字段:
- nav_calls / unique_routes (browser_navigate)
- interaction_calls (click/type/fill_form/select/hover/press/drag)
- network_req_calls (browser_network_requests)
- 顺带按新维度重算 degraded_to_python 紫标

不动 mcp_calls / py_web_calls — 这两个老逻辑已写过, 不重复。

用法: python scripts/backfill_spa_depth.py [--dry-run]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "secweb.db"
INTERACTION_NAMES = {
    "browser_click", "browser_type", "browser_fill_form",
    "browser_select_option", "browser_hover",
    "browser_press_key", "browser_drag",
}
# 与 runner 的阈值保持一致
_MIN_BROWSER_CALLS = 5
_BROWSER_NUDGE_BASH_FLOOR = 6
_MIN_UNIQUE_ROUTES = 3
_MIN_INTERACTION_CALLS = 5
_MIN_NETWORK_REQ_CALLS = 3
_EARLY_TOOLCALL_THRESHOLD = 3


def main() -> None:
    dry = "--dry-run" in sys.argv
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # 处理所有已完成任务: 重新从 events 反推, 幂等可重跑
    rows = con.execute(
        "SELECT id FROM tasks WHERE finished_at IS NOT NULL"
    ).fetchall()
    print(f"待回填任务: {len(rows)}")

    flipped = 0
    summary = []
    for row in rows:
        tid = row["id"]
        nav = 0
        net = 0
        inter = 0
        mcp_total = 0   # 所有 mcp__playwright__* 调用总数 (用于覆盖错误的 mcp_calls)
        bash = 0
        routes: set[str] = set()
        events = con.execute(
            "SELECT payload FROM events WHERE task_id=? AND kind='tool_use'", (tid,)
        ).fetchall()
        for e in events:
            try:
                d = json.loads(e["payload"])
            except Exception:
                continue
            name = (d.get("name") or "").strip()
            inp_raw = d.get("input")
            try:
                inp = json.loads(inp_raw) if isinstance(inp_raw, str) else (inp_raw or {})
            except Exception:
                inp = {}
            if name == "Bash":
                bash += 1
                continue
            if not name.startswith("mcp__playwright__"):
                continue
            mcp_total += 1
            short = name[len("mcp__playwright__"):]
            if short == "browser_navigate":
                nav += 1
                u = (inp.get("url") if isinstance(inp, dict) else "") or ""
                if u:
                    try:
                        pr = urlparse(u)
                        key = f"{pr.scheme}://{pr.netloc}{pr.path}".rstrip("/")
                        if key:
                            routes.add(key)
                    except Exception:
                        pass
            elif short == "browser_network_requests":
                net += 1
            elif short in INTERACTION_NAMES:
                inter += 1

        if nav or net or inter or routes:
            # 用从 events 重算出的 mcp / bash, 不再依赖旧 stream-json 解析的 mcp_calls
            t = con.execute(
                "SELECT mcp_calls, py_web_calls FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            mcp_old = t["mcp_calls"] or 0
            py = t["py_web_calls"] or 0
            mcp = max(mcp_total, mcp_old)
            total = mcp + bash + py
            spa_depth_missing = (
                total >= _EARLY_TOOLCALL_THRESHOLD
                and (
                    len(routes) < _MIN_UNIQUE_ROUTES
                    or inter < _MIN_INTERACTION_CALLS
                    or net < _MIN_NETWORK_REQ_CALLS
                )
            )
            degraded = (
                (mcp == 0 and py > 0)
                or (mcp < _MIN_BROWSER_CALLS and bash >= _BROWSER_NUDGE_BASH_FLOOR)
                or (mcp > 0 and bash >= mcp * 2)
                or spa_depth_missing
            )
            if not dry:
                con.execute(
                    "UPDATE tasks SET nav_calls=?, unique_routes=?, "
                    "interaction_calls=?, network_req_calls=?, "
                    "mcp_calls=?, degraded_to_python=? WHERE id=?",
                    (nav, len(routes), inter, net,
                     mcp, 1 if degraded else 0, tid),
                )
            summary.append((tid, nav, len(routes), inter, net, mcp, bash, 1 if degraded else 0))
            flipped += 1

    if not dry:
        con.commit()
    con.close()

    print(f"{'(dry-run) ' if dry else ''}回填: {flipped} 个任务")
    for tid, nav, routes, inter, net, mcp, bash, deg in summary[:20]:
        print(f"  {tid[:12]} mcp={mcp:3} bash={bash:3} nav={nav} routes={routes} interact={inter} network={net} degraded={deg}")
    if len(summary) > 20:
        print(f"  ... 还有 {len(summary)-20} 个")


if __name__ == "__main__":
    main()
