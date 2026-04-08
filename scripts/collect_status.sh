#!/usr/bin/env bash
# collect_status.sh — 采集所有活跃/最近 exec session 的状态，输出 JSON
# 依赖: openclaw CLI (可选), jq (可选), git
# 可独立运行，不依赖 OpenClaw agent 上下文

set -euo pipefail

# ── 工具检测 ──────────────────────────────────────────────────────────────────
has_cmd() { command -v "$1" &>/dev/null; }

# ── 时间格式化 ────────────────────────────────────────────────────────────────
format_duration() {
  local seconds=$1
  if [[ $seconds -lt 60 ]]; then
    echo "${seconds}s"
  elif [[ $seconds -lt 3600 ]]; then
    echo "$((seconds / 60))m$((seconds % 60))s"
  else
    echo "$((seconds / 3600))h$(( (seconds % 3600) / 60 ))m"
  fi
}

# ── Git 变更统计 ───────────────────────────────────────────────────────────────
git_stats() {
  local workdir="${1:-}"
  if [[ -z "$workdir" ]] || ! git -C "$workdir" rev-parse --git-dir &>/dev/null 2>&1; then
    echo '{"files":0,"insertions":0,"deletions":0}'
    return
  fi
  local stat
  stat=$(git -C "$workdir" diff --shortstat HEAD 2>/dev/null || echo "")
  local files=0 ins=0 del=0
  if [[ -n "$stat" ]]; then
    files=$(echo "$stat" | grep -oP '\d+(?= file)' || echo 0)
    ins=$(echo   "$stat" | grep -oP '\d+(?= insertion)' || echo 0)
    del=$(echo   "$stat" | grep -oP '\d+(?= deletion)' || echo 0)
  fi
  # 也统计 untracked
  local untracked
  untracked=$(git -C "$workdir" ls-files --others --exclude-standard 2>/dev/null | wc -l || echo 0)
  files=$(( files + untracked ))
  echo "{\"files\":${files},\"insertions\":${ins},\"deletions\":${del}}"
}

# ── 截断命令字符串（max=0 表示不限制）────────────────────────────────────────
truncate_cmd() {
  local cmd="$1"
  local max="${2:-0}"
  if [[ $max -gt 0 && ${#cmd} -gt $max ]]; then
    echo "${cmd:0:$((max-3))}..."
  else
    echo "$cmd"
  fi
}

# ── 从 openclaw process list 采集数据 ─────────────────────────────────────────
collect_from_openclaw() {
  # 尝试调用 openclaw CLI
  if ! has_cmd openclaw; then
    return 1
  fi

  local raw
  raw=$(openclaw process list --format json 2>/dev/null) || return 1

  local now
  now=$(date +%s)

  # 解析 JSON（需要 jq）
  if has_cmd jq; then
    echo "$raw" | jq --argjson now "$now" '
      .processes // . | map(
        . as $p |
        {
          id:       ($p.id // $p.session_id // "unknown"),
          command:  ($p.command // $p.cmd // ""),
          status:   ($p.status // "unknown"),
          workdir:  ($p.workdir // $p.working_dir // ""),
          startedAt: ($p.started_at // $p.start_time // ""),
          duration: (
            if ($p.started_at // null) != null then
              ($now - ($p.started_at | tonumber? // $now))
            else 0 end
          )
        }
      )
    ' 2>/dev/null && return 0
  fi

  # 无 jq：粗略解析（降级）
  echo "$raw"
  return 0
}

# ── 从 /proc 采集 (fallback) ──────────────────────────────────────────────────
collect_from_proc() {
  local now
  now=$(date +%s)
  # 系统启动时间（epoch）= now - uptime
  local uptime_secs
  uptime_secs=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)
  local boot_epoch=$(( now - uptime_secs ))
  # 动态读取时钟频率（避免在非 100Hz 系统上出错）
  local hz
  hz=$(getconf CLK_TCK 2>/dev/null || echo 100)

  local sessions=()
  local idx=0

  # 第一遍：收集所有匹配编码 agent 的 PID 集合（用于过滤子进程）
  declare -A matched_pids=()
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    local cmdline
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
    [[ -z "$cmdline" ]] && continue
    if echo "$cmdline" | grep -qiE 'claude|codex|aider|cursor|copilot|openclaw'; then
      matched_pids["$pid"]=1
    fi
  done < <(ls /proc | grep -E '^[0-9]+$' 2>/dev/null)

  # 查找 claude / codex / aider 等编码 agent 进程（只保留根进程，跳过子进程）
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    local cmdline workdir status_char elapsed=0

    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
    [[ -z "$cmdline" ]] && continue

    # 仅关注编码 agent
    if ! echo "$cmdline" | grep -qiE 'claude|codex|aider|cursor|copilot|openclaw'; then
      continue
    fi

    # 如果父进程也在匹配集合里，跳过（这是子进程，不是独立 session）
    local ppid
    ppid=$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo 0)
    if [[ -n "${matched_pids[$ppid]+_}" ]]; then
      continue
    fi

    workdir=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo "")
    status_char=$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null || echo "R")

    # 计算运行时长
    # 正确公式：进程启动 epoch = boot_epoch + starttime/hz
    #           elapsed = now - 进程启动 epoch = uptime_secs - starttime/hz
    local starttime
    starttime=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || echo 0)
    if [[ "$starttime" -gt 0 && "$uptime_secs" -gt 0 ]]; then
      elapsed=$(( uptime_secs - starttime / hz ))
      [[ $elapsed -lt 0 ]] && elapsed=0
    fi

    local status="running"
    case "$status_char" in
      Z|z) status="failed" ;;
      T|t) status="stopped" ;;
      x|X) status="failed" ;;
    esac

    # 计算启动时间（epoch）
    local started_at=""
    if [[ $starttime -gt 0 && $boot_epoch -gt 0 ]]; then
      local start_epoch=$(( boot_epoch + starttime / hz ))
      if [[ $start_epoch -gt 0 ]]; then
        started_at=$(date -d "@$start_epoch" '+%Y-%m-%dT%H:%M:%S' 2>/dev/null || echo "")
      fi
    fi

    local cmd_trunc
    cmd_trunc=$(truncate_cmd "$cmdline")
    local git_info
    git_info=$(git_stats "$workdir")

    local started_json="\"\""
    if [[ -n "$started_at" ]]; then
      started_json="\"$started_at\""
    fi
    sessions+=("{\"id\":\"proc-${pid}\",\"command\":$(printf '%s' "$cmd_trunc" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo "\"$cmd_trunc\""),\"status\":\"$status\",\"workdir\":$(printf '%s' "$workdir" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo "\"$workdir\""),\"duration\":$elapsed,\"startedAt\":$started_json,\"git\":$git_info}")
    (( idx++ )) || true
  done < <(ls /proc | grep -E '^[0-9]+$' 2>/dev/null)

  if [[ ${#sessions[@]} -eq 0 ]]; then
    echo "[]"
    return
  fi

  # 组装 JSON 数组
  local joined
  joined=$(IFS=,; echo "${sessions[*]}")
  echo "[$joined]"
}

# ── 主流程 ────────────────────────────────────────────────────────────────────
main() {
  local result

  # 优先用 openclaw CLI
  if result=$(collect_from_openclaw 2>/dev/null) && [[ -n "$result" ]]; then
    echo "$result"
    return
  fi

  # 降级：扫描 /proc
  result=$(collect_from_proc 2>/dev/null)
  echo "$result"
}

main "$@"
