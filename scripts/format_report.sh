#!/usr/bin/env bash
# format_report.sh — 将 collect_status.sh 的 JSON 输出格式化为人类可读报告
# 用法: collect_status.sh | format_report.sh [--format terminal|markdown|json]
#       format_report.sh [--format terminal|markdown|json] < status.json
#       format_report.sh [--format terminal|markdown|json] '{"id":"..."}'

set -euo pipefail

# ── 参数解析 ────────────────────────────────────────────────────────────────────
FORMAT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --format)
      FORMAT="${2:-}"
      if [[ -z "$FORMAT" ]]; then
        echo "用法: $0 [--format terminal|markdown|json]" >&2
        exit 1
      fi
      shift 2
      ;;
    --format=*)
      FORMAT="${1#*=}"
      shift
      ;;
    -*)
      echo "未知选项: $1" >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

# 自动检测格式：终端用 terminal，否则用 markdown
if [[ -z "$FORMAT" ]]; then
  if [[ -t 1 ]]; then
    FORMAT="terminal"
  else
    FORMAT="markdown"
  fi
fi

case "$FORMAT" in
  terminal|markdown|json) ;;
  *)
    echo "不支持格式: $FORMAT (可选: terminal, markdown, json)" >&2
    exit 1
    ;;
esac

# ── 颜色（仅 terminal 模式）─────────────────────────────────────────────────────
if [[ "$FORMAT" == "terminal" && -t 1 ]]; then
  C_RESET='\033[0m'
  C_BOLD='\033[1m'
  C_GREEN='\033[0;32m'
  C_BLUE='\033[0;34m'
  C_RED='\033[0;31m'
  C_YELLOW='\033[0;33m'
  C_GRAY='\033[0;90m'
  C_CYAN='\033[0;36m'
else
  C_RESET='' C_BOLD='' C_GREEN='' C_BLUE='' C_RED='' C_YELLOW='' C_GRAY='' C_CYAN=''
fi

has_cmd() { command -v "$1" &>/dev/null; }

# ── 终端宽度（默认 120）──────────────────────────────────────────────────────────
TERM_WIDTH=$(tput cols 2>/dev/null || echo "${COLUMNS:-120}")

# ── terminal 模式命令折行显示（indent: 前缀占用的列数）──────────────────────────
# 用法: print_cmd_terminal "命令字符串" "前缀占用宽度"
print_cmd_terminal() {
  local cmd="$1"
  local prefix_len="${2:-10}"  # "    命令: " 约 10 字符
  local avail=$(( TERM_WIDTH - prefix_len ))
  [[ $avail -lt 40 ]] && avail=40

  if has_cmd fold; then
    # 第一行直接输出；后续折行加续行缩进
    local first="${cmd:0:$avail}"
    local rest="${cmd:$avail}"
    printf '%s' "$first"
    if [[ -n "$rest" ]]; then
      local pad
      pad=$(printf '%*s' "$prefix_len" '')
      echo "$rest" | fold -s -w "$avail" | while IFS= read -r line; do
        echo ""
        printf '%s%s' "$pad" "$line"
      done
    fi
    echo ""
  else
    echo "$cmd"
  fi
}

# ── markdown 模式命令显示（超长用代码块）────────────────────────────────────────
# 用法: print_cmd_markdown "命令字符串"
print_cmd_markdown() {
  local cmd="$1"
  if [[ ${#cmd} -gt 100 ]]; then
    echo '  - 命令:'
    echo '    ```'
    echo "    ${cmd}"
    echo '    ```'
  else
    echo "  - 命令: \`${cmd}\`"
  fi
}

# ── 时间格式化 ──────────────────────────────────────────────────────────────────
format_duration() {
  local s=$1
  if [[ $s -lt 60 ]]; then
    echo "${s}s"
  elif [[ $s -lt 3600 ]]; then
    printf "%dm%02ds" "$((s/60))" "$((s%60))"
  else
    printf "%dh%02dm" "$((s/3600))" "$(( (s%3600)/60 ))"
  fi
}

# ── 读取输入 ────────────────────────────────────────────────────────────────────
json_input=""
if [[ $# -gt 0 ]]; then
  json_input="$1"
else
  json_input=$(cat)
fi

if [[ -z "$json_input" || "$json_input" == "null" ]]; then
  case "$FORMAT" in
    terminal) echo -e "${C_YELLOW}⚠ 无数据输入${C_RESET}" ;;
    markdown) echo "_⚠ 无数据输入_" ;;
    json)     echo '{"sessions":[],"summary":{"total":0,"running":0,"done":0,"failed":0}}' ;;
  esac
  exit 0
fi

# ── JSON 格式：直接输出（带元数据）─────────────────────────────────────────────
if [[ "$FORMAT" == "json" ]]; then
  now=$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')
  if has_cmd jq; then
    echo "$json_input" | jq --arg now "$now" '{
      generated_at: $now,
      sessions: .,
      summary: {
        total:      (. | length),
        running:    ([.[] | select(.status == "running")] | length),
        done:       ([.[] | select(.status == "done" or .status == "completed" or .status == "success")] | length),
        failed:     ([.[] | select(.status == "failed" or .status == "error")] | length)
      }
    }'
  else
    echo "$json_input"
  fi
  exit 0
fi

# ── 需要 jq（terminal/markdown 共用解析逻辑）────────────────────────────────────
if ! has_cmd jq; then
  case "$FORMAT" in
    terminal)
      echo -e "${C_YELLOW}⚠ 未找到 jq，输出原始 JSON:${C_RESET}"
      ;;
    markdown)
      echo "> ⚠ 未找到 jq，输出原始 JSON"
      echo ""
      echo '```json'
      ;;
  esac
  echo "$json_input"
  [[ "$FORMAT" == "markdown" ]] && echo '```'
  exit 0
fi

# ── 解析各分组 ──────────────────────────────────────────────────────────────────
running=$(echo "$json_input" | jq -c '[.[] | select(.status == "running")]' 2>/dev/null || echo "[]")
done_list=$(echo "$json_input" | jq -c '[.[] | select(.status == "done" or .status == "completed" or .status == "success")]' 2>/dev/null || echo "[]")
failed=$(echo "$json_input" | jq -c '[.[] | select(.status == "failed" or .status == "error")]' 2>/dev/null || echo "[]")
unknown=$(echo "$json_input" | jq -c '[.[] | select(.status != "running" and .status != "done" and .status != "completed" and .status != "success" and .status != "failed" and .status != "error")]' 2>/dev/null || echo "[]")

count_running=$(echo "$running" | jq 'length')
count_done=$(echo "$done_list" | jq 'length')
count_failed=$(echo "$failed" | jq 'length')
count_unknown=$(echo "$unknown" | jq 'length')
count_total=$(echo "$json_input" | jq 'length')

now=$(date '+%Y-%m-%d %H:%M:%S')

# ═══════════════════════════════════════════════════════════════════════════════
# TERMINAL 格式
# ═══════════════════════════════════════════════════════════════════════════════
if [[ "$FORMAT" == "terminal" ]]; then

  echo -e "${C_BOLD}╔══════════════════════════════════════════════════════════╗${C_RESET}"
  echo -e "${C_BOLD}║          Agent Monitor — 实时状态报告                    ║${C_RESET}"
  echo -e "${C_BOLD}╚══════════════════════════════════════════════════════════╝${C_RESET}"
  echo -e "${C_GRAY}  生成时间: $now${C_RESET}"
  echo ""

  # ── 运行中 ──────────────────────────────────────────────────────────────────
  if [[ $count_running -gt 0 ]]; then
    echo -e "${C_GREEN}${C_BOLD}▶ 运行中 ($count_running)${C_RESET}"
    echo -e "${C_GRAY}  ─────────────────────────────────────────────────────────${C_RESET}"
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      dur=$(echo "$item" | jq -r '.duration // 0')
      workdir=$(echo "$item" | jq -r '.workdir // ""')
      dur_fmt=$(format_duration "$dur")

      echo -e "  ${C_GREEN}●${C_RESET} ${C_BOLD}[$id]${C_RESET}  时长: ${C_CYAN}${dur_fmt}${C_RESET}"
      printf "    ${C_GRAY}命令:${C_RESET} "
      print_cmd_terminal "$cmd" 10
      if [[ -n "$workdir" ]]; then
        echo -e "    ${C_GRAY}目录:${C_RESET} $workdir"
      fi
      git_files=$(echo "$item" | jq -r '.git.files // 0')
      git_ins=$(echo "$item"   | jq -r '.git.insertions // 0')
      git_del=$(echo "$item"   | jq -r '.git.deletions // 0')
      if [[ $git_files -gt 0 || $git_ins -gt 0 || $git_del -gt 0 ]]; then
        echo -e "    ${C_GRAY}变更:${C_RESET} ${git_files}个文件  ${C_GREEN}+${git_ins}${C_RESET} ${C_RED}-${git_del}${C_RESET}"
      fi
      echo ""
    done < <(echo "$running" | jq -c '.[]')
  fi

  # ── 已完成 ──────────────────────────────────────────────────────────────────
  if [[ $count_done -gt 0 ]]; then
    echo -e "${C_BLUE}${C_BOLD}✔ 已完成 ($count_done)${C_RESET}"
    echo -e "${C_GRAY}  ─────────────────────────────────────────────────────────${C_RESET}"
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      dur=$(echo "$item" | jq -r '.duration // 0')
      dur_fmt=$(format_duration "$dur")

      echo -e "  ${C_BLUE}✓${C_RESET} ${C_BOLD}[$id]${C_RESET}  耗时: ${dur_fmt}"
      printf "    ${C_GRAY}命令:${C_RESET} "
      print_cmd_terminal "$cmd" 10
      echo ""
    done < <(echo "$done_list" | jq -c '.[]')
  fi

  # ── 失败 ────────────────────────────────────────────────────────────────────
  if [[ $count_failed -gt 0 ]]; then
    echo -e "${C_RED}${C_BOLD}✗ 失败 ($count_failed)${C_RESET}"
    echo -e "${C_GRAY}  ─────────────────────────────────────────────────────────${C_RESET}"
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      err=$(echo "$item" | jq -r '.error // .exit_code // ""')

      echo -e "  ${C_RED}✗${C_RESET} ${C_BOLD}[$id]${C_RESET}"
      printf "    ${C_GRAY}命令:${C_RESET} "
      print_cmd_terminal "$cmd" 10
      if [[ -n "$err" ]]; then
        echo -e "    ${C_RED}错误:${C_RESET} $err"
      fi
      echo ""
    done < <(echo "$failed" | jq -c '.[]')
  fi

  # ── 其他状态 ────────────────────────────────────────────────────────────────
  if [[ $count_unknown -gt 0 ]]; then
    echo -e "${C_YELLOW}${C_BOLD}? 其他状态 ($count_unknown)${C_RESET}"
    echo -e "${C_GRAY}  ─────────────────────────────────────────────────────────${C_RESET}"
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      status=$(echo "$item" | jq -r '.status // "unknown"')
      echo -e "  ${C_YELLOW}?${C_RESET} ${C_BOLD}[$id]${C_RESET}  状态: $status"
      printf "    ${C_GRAY}命令:${C_RESET} "
      print_cmd_terminal "$cmd" 10
      echo ""
    done < <(echo "$unknown" | jq -c '.[]')
  fi

  # ── 无数据 ──────────────────────────────────────────────────────────────────
  if [[ $count_total -eq 0 ]]; then
    echo -e "  ${C_GRAY}暂无 agent session 记录${C_RESET}"
    echo ""
  fi

  # ── 汇总统计 ────────────────────────────────────────────────────────────────
  echo -e "${C_BOLD}─────────────────────────────────────────────────────────────${C_RESET}"
  echo -e "${C_BOLD}汇总统计${C_RESET}  总计: ${C_BOLD}${count_total}${C_RESET}  " \
    "运行中: ${C_GREEN}${count_running}${C_RESET}  " \
    "完成: ${C_BLUE}${count_done}${C_RESET}  " \
    "失败: ${C_RED}${count_failed}${C_RESET}"
  echo -e "${C_GRAY}─────────────────────────────────────────────────────────────${C_RESET}"

# ═══════════════════════════════════════════════════════════════════════════════
# MARKDOWN 格式
# ═══════════════════════════════════════════════════════════════════════════════
elif [[ "$FORMAT" == "markdown" ]]; then

  echo "# Agent Monitor — 实时状态报告"
  echo ""
  echo "> 生成时间: $now"
  echo ""

  # ── 运行中 ──────────────────────────────────────────────────────────────────
  if [[ $count_running -gt 0 ]]; then
    echo "## ▶ 运行中 (${count_running})"
    echo ""
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      dur=$(echo "$item" | jq -r '.duration // 0')
      workdir=$(echo "$item" | jq -r '.workdir // ""')
      dur_fmt=$(format_duration "$dur")

      echo "- **[${id}]** 时长: \`${dur_fmt}\`"
      print_cmd_markdown "$cmd"
      if [[ -n "$workdir" ]]; then
        echo "  - 目录: \`${workdir}\`"
      fi
      git_files=$(echo "$item" | jq -r '.git.files // 0')
      git_ins=$(echo "$item"   | jq -r '.git.insertions // 0')
      git_del=$(echo "$item"   | jq -r '.git.deletions // 0')
      if [[ $git_files -gt 0 || $git_ins -gt 0 || $git_del -gt 0 ]]; then
        echo "  - 变更: ${git_files}个文件  +${git_ins} -${git_del}"
      fi
      echo ""
    done < <(echo "$running" | jq -c '.[]')
  fi

  # ── 已完成 ──────────────────────────────────────────────────────────────────
  if [[ $count_done -gt 0 ]]; then
    echo "## ✔ 已完成 (${count_done})"
    echo ""
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      dur=$(echo "$item" | jq -r '.duration // 0')
      dur_fmt=$(format_duration "$dur")

      echo "- **[${id}]** 耗时: \`${dur_fmt}\`"
      print_cmd_markdown "$cmd"
      echo ""
    done < <(echo "$done_list" | jq -c '.[]')
  fi

  # ── 失败 ────────────────────────────────────────────────────────────────────
  if [[ $count_failed -gt 0 ]]; then
    echo "## ✗ 失败 (${count_failed})"
    echo ""
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      err=$(echo "$item" | jq -r '.error // .exit_code // ""')

      echo "- **[${id}]**"
      print_cmd_markdown "$cmd"
      if [[ -n "$err" ]]; then
        echo "  - 错误: \`${err}\`"
      fi
      echo ""
    done < <(echo "$failed" | jq -c '.[]')
  fi

  # ── 其他状态 ────────────────────────────────────────────────────────────────
  if [[ $count_unknown -gt 0 ]]; then
    echo "## ? 其他状态 (${count_unknown})"
    echo ""
    while IFS= read -r item; do
      id=$(echo "$item" | jq -r '.id // "unknown"')
      cmd=$(echo "$item" | jq -r '.command // ""')
      status=$(echo "$item" | jq -r '.status // "unknown"')
      echo "- **[${id}]** 状态: \`${status}\`"
      print_cmd_markdown "$cmd"
      echo ""
    done < <(echo "$unknown" | jq -c '.[]')
  fi

  # ── 无数据 ──────────────────────────────────────────────────────────────────
  if [[ $count_total -eq 0 ]]; then
    echo "_暂无 agent session 记录_"
    echo ""
  fi

  # ── 汇总统计 ────────────────────────────────────────────────────────────────
  echo "---"
  echo ""
  echo "| 状态 | 数量 |"
  echo "|------|------|"
  echo "| 总计 | ${count_total} |"
  echo "| 运行中 | ${count_running} |"
  echo "| 完成 | ${count_done} |"
  echo "| 失败 | ${count_failed} |"
  if [[ $count_unknown -gt 0 ]]; then
    echo "| 其他 | ${count_unknown} |"
  fi
  echo ""

fi
