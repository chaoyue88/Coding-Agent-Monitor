#!/usr/bin/env bash
# collect_logs.sh — 采集指定 session 的最近日志输出
# 用法: bash collect_logs.sh <session-id>
# 输出: 最近 50 行日志文本

set -uo pipefail

SESSION_ID="${1:-}"
if [[ -z "$SESSION_ID" ]]; then
  echo "用法: $0 <session-id>" >&2
  exit 1
fi

LINES=50

# ── 工具检测 ──────────────────────────────────────────────────────────────────
has_cmd() { command -v "$1" &>/dev/null; }

# ── 从 openclaw CLI 获取日志 ──────────────────────────────────────────────────
try_openclaw_logs() {
  if ! has_cmd openclaw; then return 1; fi
  local out
  out=$(openclaw process log --session "$SESSION_ID" --lines "$LINES" 2>/dev/null) || return 1
  [[ -z "$out" ]] && return 1
  echo "$out"
  return 0
}

# ── 查找进程 PID ──────────────────────────────────────────────────────────────
find_pid() {
  # 1. 尝试 openclaw process list 获取 PID
  if has_cmd openclaw; then
    local pid
    pid=$(openclaw process list --format json 2>/dev/null \
      | grep -o '"pid":[0-9]*' | grep -o '[0-9]*' | head -1 2>/dev/null) || true
    [[ -n "$pid" ]] && echo "$pid" && return 0
  fi

  # 2. 扫描 /proc，匹配 session ID
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    local cmdline
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
    if echo "$cmdline" | grep -q "$SESSION_ID"; then
      echo "$pid"
      return 0
    fi
  done < <(ls /proc | grep -E '^[0-9]+$' 2>/dev/null)

  return 1
}

# ── 尝试读取 /proc/$PID/fd/1 (stdout) ────────────────────────────────────────
try_proc_fd() {
  local pid="$1"
  local fd1="/proc/$pid/fd/1"
  if [[ ! -e "$fd1" ]]; then return 1; fi
  if [[ ! -r "$fd1" ]]; then
    echo "[权限不足，无法读取进程 stdout (PID=$pid)]"
    return 1
  fi
  # fd/1 通常是管道或 tty，不能直接 tail；尝试读取
  timeout 2 tail -n "$LINES" "$fd1" 2>/dev/null && return 0
  return 1
}

# ── 检查 openclaw 日志目录 ────────────────────────────────────────────────────
try_log_files() {
  local log_dirs=(
    "$HOME/.openclaw/logs"
    "$HOME/.openclaw/sessions"
    "$HOME/.local/share/openclaw/logs"
    "/tmp/openclaw"
  )

  for dir in "${log_dirs[@]}"; do
    [[ -d "$dir" ]] || continue
    # 查找包含 session ID 的日志文件
    local found
    found=$(find "$dir" -type f \( -name "*.log" -o -name "*.txt" \) \
            -newer /proc/1/exe 2>/dev/null \
            | xargs grep -l "$SESSION_ID" 2>/dev/null | head -1 || true)
    if [[ -n "$found" ]]; then
      echo "[日志文件: $found]"
      tail -n "$LINES" "$found" 2>/dev/null
      return 0
    fi
    # 注意：不再回退到"最新日志文件"，以避免返回与 session ID 无关的数据
  done
  return 1
}

# ── 主流程 ────────────────────────────────────────────────────────────────────
main() {
  echo "=== session: $SESSION_ID ==="

  # 优先：openclaw CLI
  if try_openclaw_logs; then return 0; fi

  # 查找进程
  local pid=""
  pid=$(find_pid 2>/dev/null) || true

  if [[ -z "$pid" ]]; then
    echo "[进程未找到: session '$SESSION_ID' 可能已结束或 ID 不正确]"
    # 仍尝试日志文件
    try_log_files && return 0
    echo "[提示] 使用 'openclaw process log --session $SESSION_ID' 查看历史日志"
    return 1
  fi

  echo "[找到进程 PID=$pid]"

  # 尝试 /proc/fd/1
  if try_proc_fd "$pid"; then return 0; fi

  # 尝试日志文件
  if try_log_files; then return 0; fi

  # Fallback 提示
  echo "[无法直接读取日志]"
  echo "可用方式:"
  echo "  openclaw process log --session $SESSION_ID"
  echo "  或查看: ~/.openclaw/logs/"
  return 1
}

main "$@"
