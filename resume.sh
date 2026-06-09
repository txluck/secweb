#!/usr/bin/env bash
# 接管已暂停的 claude 任务会话。
#
# 用法:
#   ./resume.sh <session-id 或 task-id 前缀>
#   ./resume.sh                         # 列出可接管的任务
#
# 行为:
#   1. 在 data/secweb.db 中查找匹配的任务 (session_id 完整匹配, 或 task id 前缀匹配)
#   2. cd 到该任务的 workdir
#   3. exec claude --resume <session_id>
set -e
cd "$(dirname "$0")"

DB="data/secweb.db"
if [ ! -f "$DB" ]; then
  echo "找不到数据库: $DB" >&2
  exit 1
fi

if [ -z "${1:-}" ]; then
  echo "可接管的任务 (status=stopped/needs_input/running):"
  sqlite3 -separator $'\t' "$DB" \
    "SELECT id, status, session_id, workdir
       FROM tasks
      WHERE status IN ('stopped','needs_input','running')
      ORDER BY created_at DESC
      LIMIT 20;" \
    | awk -F'\t' 'BEGIN{printf "%-14s %-12s %-40s %s\n","TASK_ID","STATUS","SESSION_ID","WORKDIR"}
                       {printf "%-14s %-12s %-40s %s\n",$1,$2,$3,$4}'
  echo
  echo "用法: $0 <session-id 或 task-id 前缀>"
  exit 0
fi

KEY="$1"
ROW=$(sqlite3 -separator $'\t' "$DB" \
  "SELECT session_id, workdir FROM tasks
    WHERE session_id = '$KEY' OR id LIKE '$KEY%'
    ORDER BY created_at DESC LIMIT 1;")

if [ -z "$ROW" ]; then
  echo "未找到匹配的任务: $KEY" >&2
  exit 1
fi

SID=$(echo "$ROW" | cut -f1)
WD=$(echo "$ROW" | cut -f2)

if [ ! -d "$WD" ]; then
  echo "workdir 不存在: $WD" >&2
  exit 1
fi

echo ">> cd $WD"
echo ">> claude --resume $SID"
cd "$WD"
exec claude --resume "$SID"
