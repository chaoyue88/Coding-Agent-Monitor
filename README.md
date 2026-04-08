# Coding Agent Monitor

Real-time monitoring for AI coding agent sessions — Claude Code, Codex, Aider, and more.

## Features

- **Live process list** — detects running agent sessions via `openclaw process list` or `/proc` fallback
- **Child-process deduplication** — only shows root sessions, not transient subprocess noise
- **Git change stats** — files changed, insertions, deletions per session
- **CLI pipeline** — collect + format without any server
- **Web dashboard** — single-page frontend polling a lightweight Python HTTP server on `:9090`
- **Session history** — completed sessions remain visible for 7 days via JSONL cache
- **Structured log viewer** — parses Claude Code JSONL conversation files; dashboard "MD" button renders messages with tool calls, code blocks, and rich text

## Quick Start

```bash
# One-shot report in the terminal
bash scripts/collect_status.sh | bash scripts/format_report.sh

# Start the live web dashboard (http://localhost:9090/)
bash scripts/monitor.sh start

# Stop it
bash scripts/monitor.sh stop
```

## Requirements

- Python 3 (stdlib only — no pip install needed)
- `jq` (optional — improves JSON parsing; auto-degrades without it)
- `git` (optional — for change stats)
- Linux `/proc` filesystem (for the fallback process scanner)

## Architecture

```
collect_status.sh  →  format_report.sh     CLI pipeline, no server needed
       ↓
  server.py  ←  assets/dashboard.html      HTTP API + web UI on :9090
```

| Script | Role |
|---|---|
| `scripts/collect_status.sh` | Collects session data as a JSON array |
| `scripts/format_report.sh` | Renders the JSON into a terminal/markdown/JSON report |
| `scripts/server.py` | HTTP API server (`/api/sessions`, `/api/sessions/<pid>/logs`) |
| `assets/dashboard.html` | Single-file frontend, polls every 3 seconds |
| `scripts/monitor.sh` | Process manager for `server.py` |
| `scripts/collect_logs.sh` | Print last 50 lines of a session's logs and exit |
| `scripts/stream_logs.sh` | Tail a session's logs continuously |

## API

| Endpoint | Description |
|---|---|
| `GET /api/sessions` | Live process list (4s TTL cache) |
| `GET /api/sessions/<pid>/logs` | Structured log data for a session |
| `GET /` | Serves `assets/dashboard.html` |

### Log response shape

```json
{
  "pid": "12345",
  "available": true,
  "source": "Claude session (abc12345…)",
  "logs": "14:30:00 🤖 Hello world\n14:30:05 👤 Fix the bug",
  "messages": [
    {
      "role": "assistant",
      "timestamp": "2026-04-07T14:30:00",
      "text": "Hello world",
      "tools": [{ "name": "Read", "input": { "path": "/foo" } }]
    }
  ]
}
```

## Report Format

```
▶ Running (2)
  ● [proc-1234]  duration: 12m30s
    command: claude --dangerously-skip-permissions
    workdir: /home/user/myproject
    changes: 3 files  +120 -45

✔ Done (1)
  ✓ [proc-5678]  duration: 5m12s
    command: aider --model gpt-4o

✗ Failed (0)

─────────────────────────────────────────────────
Summary  total: 3  running: 2  done: 1  failed: 0
```

## License

MIT
