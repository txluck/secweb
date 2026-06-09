"""一次性脚本: 给已完成任务回填 skill 契约执行状态。

读取 tasks 表里 finished_at IS NOT NULL 且 report_path 不为空的行,
对每行的 prompt 跑 detect_slash_skill 拿到 skill 名 (如 hack/idor/sqli),
然后对 workdir/report_path 跑 parse_report_contract_status, 把:
  - contract_skill        TEXT
  - contract_total        INTEGER
  - contract_covered      INTEGER
  - contract_missing_json TEXT  (JSON list of missing C-id ints)
回填到 tasks 表。

幂等: 可以重复跑, 每次重新计算 (报告改动 / skill 文件升级后自动反映)。

用法:
  python scripts/backfill_contract_status.py [--dry-run]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# 让脚本能 import app.skill_contract
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.skill_contract import detect_slash_skill, parse_report_contract_status  # noqa: E402

DB_PATH = ROOT / "data" / "secweb.db"


def main() -> None:
    dry = "--dry-run" in sys.argv
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "SELECT id, prompt, workdir, report_path "
        "FROM tasks "
        "WHERE finished_at IS NOT NULL "
        "  AND report_path IS NOT NULL "
        "  AND report_path != ''"
    ).fetchall()
    print(f"待回填任务: {len(rows)}")

    flipped = 0
    no_skill = 0
    no_report = 0
    summary: list[tuple] = []
    for row in rows:
        tid = row["id"]
        prompt = row["prompt"] or ""
        workdir = Path(row["workdir"])
        report_rel = row["report_path"]

        skill = detect_slash_skill(prompt)
        if not skill:
            no_skill += 1
            continue

        report_file = workdir / report_rel
        if not report_file.exists():
            no_report += 1
            continue

        try:
            text = report_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            no_report += 1
            continue

        st = parse_report_contract_status(text, skill)
        total = st.get("total", 0)
        if total == 0:
            # skill 文件抽不出契约项 (老 skill / 升级前格式), 把 skill 名记下来即可
            covered = 0
            missing_ids: list[int] = []
        else:
            covered = len(st.get("covered_ids", set()))
            missing_ids = sorted(st.get("missing_ids", set()))

        if not dry:
            con.execute(
                "UPDATE tasks SET "
                "  contract_skill=?, "
                "  contract_total=?, "
                "  contract_covered=?, "
                "  contract_missing_json=? "
                "WHERE id=?",
                (
                    skill, total, covered,
                    json.dumps(missing_ids, ensure_ascii=False),
                    tid,
                ),
            )
        summary.append((tid, skill, covered, total, len(missing_ids)))
        flipped += 1

    if not dry:
        con.commit()
    con.close()

    print(f"{'(dry-run) ' if dry else ''}回填: {flipped} 个任务")
    print(f"  跳过 (prompt 没识别到 skill): {no_skill}")
    print(f"  跳过 (report.md 不存在 / 不可读): {no_report}")
    for tid, skill, covered, total, missing in summary[:20]:
        ratio = (covered / total * 100) if total else 0
        print(f"  {tid[:12]} skill={skill:18} {covered:3}/{total:3} ({ratio:5.1f}%) miss={missing}")
    if len(summary) > 20:
        print(f"  ... 还有 {len(summary)-20} 个")


if __name__ == "__main__":
    main()
