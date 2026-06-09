#!/usr/bin/env bash
# 一键启动: 创建 venv + 安装依赖 + 预热 chromium + 启动
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo ">> 创建虚拟环境"
  python3 -m venv .venv
fi
source .venv/bin/activate

if [ ! -f .env ]; then
  echo ">> 复制 .env.example -> .env，请按需修改密码"
  cp .env.example .env
fi

# 只在 requirements.txt 哈希变化时才装依赖, 否则 99% 启动跳过这一步.
# 指纹存在 .venv/.req-fingerprint, pip 装完后才写, 中途 ctrl-c 不会留脏指纹.
# 想强制重装: rm .venv/.req-fingerprint  (或直接 rm -rf .venv)
REQ_FILE="requirements.txt"
FP_FILE=".venv/.req-fingerprint"
CUR_FP="$(shasum -a 256 "$REQ_FILE" 2>/dev/null | awk '{print $1}')"
OLD_FP="$(cat "$FP_FILE" 2>/dev/null || echo)"
if [ "$CUR_FP" != "$OLD_FP" ]; then
  echo ">> requirements.txt 有变化, 装依赖中…"
  pip install -q -r "$REQ_FILE"
  echo "$CUR_FP" > "$FP_FILE"
else
  echo ">> 依赖未变, 跳过 pip install (强制重装: rm $FP_FILE)"
fi

# 预热 playwright chromium: 首次冷启会下载 ~150MB 二进制,
# 多任务并发时容易互相争用导致 MCP 握手超时. 一次预热, 永久缓存.
PW_CACHE="$HOME/Library/Caches/ms-playwright"
[ -d "$PW_CACHE" ] || PW_CACHE="$HOME/.cache/ms-playwright"
if [ ! -d "$PW_CACHE" ] || [ -z "$(ls -A "$PW_CACHE" 2>/dev/null | grep -i chromium)" ]; then
  PW_BIN="$(command -v playwright-mcp || true)"
  if [ -z "$PW_BIN" ] && [ -x "$HOME/.npm-global/bin/playwright-mcp" ]; then
    PW_BIN="$HOME/.npm-global/bin/playwright-mcp"
  fi
  if [ -n "$PW_BIN" ]; then
    echo ">> 预热 playwright chromium (一次性, 数十秒)"
    ( "$PW_BIN" --user-data-dir="$(mktemp -d)" >/dev/null 2>&1 ) &
    PW_PID=$!
    # 给它最多 90s 拉完 chromium, 完事就 kill
    for _ in $(seq 1 90); do
      sleep 1
      if [ -d "$PW_CACHE" ] && ls -A "$PW_CACHE" 2>/dev/null | grep -qi chromium; then
        break
      fi
    done
    kill "$PW_PID" 2>/dev/null || true
    wait "$PW_PID" 2>/dev/null || true
    echo ">> chromium 预热完成"
  fi
fi

# 防止 macOS 在屏幕休眠 / 长时间挂机时把 python / claude / playwright-mcp 挂到
# App Nap 或 idle/system sleep, 导致任务停摆 (典型现象: 凌晨任务起跑后卡住,
# 早上唤醒屏幕才继续执行)。默认 caffeinate -ims:
#   -i 阻 idle sleep    -m 阻 disk idle    -s 阻 (AC 供电下) system sleep
# 不带 -d, 屏幕仍可休眠 / 关闭, 只是后台 python+claude+chromium 不会被挂起。
# 关闭: 设 SECWEB_NO_CAFFEINATE=1 (或直接 python main.py)。
if [ "$(uname -s)" = "Darwin" ] && [ -z "$SECWEB_NO_CAFFEINATE" ] && command -v caffeinate >/dev/null 2>&1; then
  echo ">> 启用 caffeinate -ims 防止 idle/system sleep (设 SECWEB_NO_CAFFEINATE=1 关闭)"
  exec caffeinate -ims python main.py
else
  exec python main.py
fi
