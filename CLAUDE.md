# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this skill does

`agent-monitor` is an OpenClaw skill that monitors running AI coding agent sessions (Claude Code, Codex, Aider, etc.). It collects session status via `openclaw process list` or falls back to scanning `/proc`, formats the output as a human-readable report, and optionally serves a live web dashboard.

## Running the tools

```bash
# Collect raw session status (JSON array)
bash scripts/collect_status.sh

# Format as human-readable report (auto-detects terminal vs markdown)
bash scripts/collect_status.sh | bash scripts/format_report.sh

# Force a specific format
bash scripts/collect_status.sh | bash scripts/format_report.sh --format terminal
bash scripts/collect_status.sh | bash scripts/format_report.sh --format markdown
bash scripts/collect_status.sh | bash scripts/format_report.sh --format json

# Manage the web dashboard (http://localhost:9090/)
bash scripts/monitor.sh start
bash scripts/monitor.sh stop
bash scripts/monitor.sh status
bash scripts/monitor.sh restart

# View logs for a specific session
bash scripts/collect_logs.sh <session-id>       # last 50 lines, exits
bash scripts/stream_logs.sh <session-id>        # tail -f style, Ctrl+C to stop

# Lint Python
ruff check scripts/server.py
```

## Architecture

```
collect_status.sh  →  format_report.sh  (CLI pipeline, no server needed)
       ↓
  server.py  ←  assets/dashboard.html   (web dashboard via HTTP on :9090)
```

**`scripts/collect_status.sh`** — Data collection layer. Tries `openclaw process list --format json` first; falls back to scanning `/proc` for processes matching `claude|codex|aider|cursor|copilot|openclaw`. Outputs a JSON array with fields: `id`, `command`, `status`, `workdir`, `duration`, `startedAt`, `git`.

**`scripts/format_report.sh`** — Presentation layer for the CLI pipeline. Reads the JSON array and renders it grouped by status (`running` / `done` / `failed` / other). Auto-detects terminal for ANSI colors.

**`scripts/server.py`** — Standalone HTTP API server using only Python stdlib (no `pip install` needed). Listens on `127.0.0.1:9090`. Key endpoints:
- `GET /api/sessions` — live process list (4s TTL cache to avoid hammering `ps aux`)
- `GET /api/sessions/<pid>/logs` — structured log data for a session; reads Claude JSONL conversation files from `~/.claude/projects/` and caches results at `~/.openclaw/session-cache/`
- `GET /` — serves `assets/dashboard.html`

**`assets/dashboard.html`** — Single-file frontend (vanilla JS, no bundler). Polls `/api/sessions` every 3 seconds. The "MD" button on each session switches between raw text and structured message rendering.

**`scripts/monitor.sh`** — Process manager for `server.py`. Uses `/tmp/agent-monitor.pid` and port 9090. Verifies server health via HTTP before writing the PID file.

## Key implementation details

- `server.py` caches session history for 7 days so completed sessions remain visible after the process exits
- Log data is read from Claude Code's JSONL files at `~/.claude/projects/<hashed-path>/` — the server parses these directly without any Claude API calls
- `collect_status.sh` uses `python3 -c 'import json,sys; ...'` inline to safely JSON-encode strings (handles special chars in paths/commands) — `jq` is optional but improves parsing
- The `/proc` fallback computes elapsed time using `starttime` from `/proc/<pid>/stat` plus `getconf CLK_TCK` for portability across non-100Hz kernels
