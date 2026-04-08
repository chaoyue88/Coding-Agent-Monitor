#!/usr/bin/env bash
# stream_logs.sh — 实时流式输出指定 session 的日志（增量追加）
# 用法: bash stream_logs.sh <session-id>
# 按 Ctrl+C 退出

set -uo pipefail

SESSION_ID="${1:-}"
if [[ -z "$SESSION_ID" ]]; then
  echo "用法: $0 <session-id>" >&2
  exit 1
fi

INTERVAL=2   # 轮询间隔（秒）
LAST_POS=0   # 上次读取位置（字节偏移）
LOG_FILE=""  # 当前追踪的日志文件

# ── 工具检测 ──────────────────────────────────────────────────────────────────
has_cmd() { command -v "$1" &>/dev/null; }

# ── Ctrl+C 处理 ───────────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "[stream_logs] 已停止监控 session: $SESSION_ID"
  exit 0
}
trap cleanup INT TERM

# ── 检查 session 是否仍在运行 ─────────────────────────────────────────────────
is_session_alive() {
  # 方式 1: openclaw CLI
  if has_cmd openclaw; then
    local status
    status=$(openclaw process list --format json 2>/dev/null \
      | grep -o "\"$SESSION_ID\"" 2>/dev/null || true)
    [[ -n "$status" ]] && return 0
  fi

  # 方式 2: 扫描 /proc
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    local cmdline
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
    echo "$cmdline" | grep -q "$SESSION_ID" && return 0
  done < <(ls /proc | grep -E '^[0-9]+$' 2>/dev/null)

  return 1
}

# ── 查找日志文件 ──────────────────────────────────────────────────────────────
find_log_file() {
  local log_dirs=(
    "$HOME/.openclaw/logs"
    "$HOME/.openclaw/sessions"
    "$HOME/.local/share/openclaw/logs"
    "/tmp/openclaw"
  )
  for dir in "${log_dirs[@]}"; do
    [[ -d "$dir" ]] || continue
    local found
    found=$(find "$dir" -type f \( -name "*.log" -o -name "*.txt" \) \
            2>/dev/null \
            | xargs grep -l "$SESSION_ID" 2>/dev/null | head -1 || true)
    [[ -n "$found" ]] && echo "$found" && return 0
    # 最新日志文件降级
    local latest
    latest=$(ls -t "$dir"/*.log 2>/dev/null | head -1 || true)
    [[ -n "$latest" ]] && echo "$latest" && return 0
  done
  return 1
}

# ── 从 openclaw CLI 增量拉取日志 ──────────────────────────────────────────────
stream_via_openclaw() {
  local offset=0
  echo "[openclaw] 开始流式监控 session: $SESSION_ID"
  echo "────────────────────────────────────────"

  while true; do
    if ! is_session_alive; then
      echo ""
      echo "[session $SESSION_ID 已结束]"
      return 0
    fi

    local out
    out=$(openclaw process log --session "$SESSION_ID" --since "$offset" 2>/dev/null || true)
    if [[ -n "$out" ]]; then
      echo "$out"
      offset=$(( offset + $(echo "$out" | wc -l) ))
    fi

    sleep "$INTERVAL"
  done
}

# ── 文件增量读取 ──────────────────────────────────────────────────────────────
stream_via_file() {
  local file="$1"
  LAST_POS=$(wc -c < "$file" 2>/dev/null || echo 0)

  echo "[文件] 开始追踪: $file"
  echo "[session: $SESSION_ID]"
  echo "────────────────────────────────────────"
  # 先输出最近 20 行历史
  tail -n 20 "$file" 2>/dev/null || true
  echo "────────────────────────────────────────"
  echo "[等待新内容...]"

  while true; do
    if ! is_session_alive; then
      echo ""
      echo "[session $SESSION_ID 已结束]"
      # 输出剩余内容
      local cur_size
      cur_size=$(wc -c < "$file" 2>/dev/null || echo "$LAST_POS")
      if [[ "$cur_size" -gt "$LAST_POS" ]]; then
        tail -c +"$((LAST_POS + 1))" "$file" 2>/dev/null
      fi
      return 0
    fi

    local cur_size
    cur_size=$(wc -c < "$file" 2>/dev/null || echo "$LAST_POS")

    if [[ "$cur_size" -gt "$LAST_POS" ]]; then
      # 输出增量内容
      tail -c +"$((LAST_POS + 1))" "$file" 2>/dev/null
      LAST_POS="$cur_size"
    fi

    sleep "$INTERVAL"
  done
}

# ── 主流程 ────────────────────────────────────────────────────────────────────
main() {
  echo "Agent Monitor — 日志流  (Ctrl+C 退出)"

  # 方式 1: openclaw CLI stream
  if has_cmd openclaw; then
    if openclaw process log --session "$SESSION_ID" --lines 1 &>/dev/null; then
      stream_via_openclaw
      return $?
    fi
  fi

  # 方式 2: 查找并追踪日志文件
  LOG_FILE=$(find_log_file 2>/dev/null) || true
  if [[ -n "$LOG_FILE" ]]; then
    stream_via_file "$LOG_FILE"
    return $?
  fi

  # 检查 session 是否存在
  if ! is_session_alive; then
    echo "[错误] session '$SESSION_ID' 未找到或已结束"
    echo "[提示] 使用 'bash collect_logs.sh $SESSION_ID' 查看历史日志"
    exit 1
  fi

  echo "[警告] 找不到可追踪的日志源"
  echo "进程仍在运行，但无法定位日志文件。"
  echo "尝试: openclaw process log --session $SESSION_ID"
  exit 1
}

main "$@"
