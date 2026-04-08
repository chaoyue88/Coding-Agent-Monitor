#!/usr/bin/env python3
"""
Agent Monitor MCP Server
通过 MCP 协议将 agent-monitor 能力暴露给其他 AI Agent。
传输方式：stdio（标准 MCP 本地工具协议）

依赖：mcp  (pip install mcp)
启动：python3 scripts/mcp_server.py

在 Claude Code 中注册：
  ~/.claude/settings.json → mcpServers → agent-monitor
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "请先安装 MCP SDK：pip install mcp",
        file=sys.stderr,
    )
    sys.exit(1)

# ─── 路径常量 ─────────────────────────────────────────────────────────────────

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
DASHBOARD_URL = "http://localhost:9090"

mcp = FastMCP(
    "agent-monitor",
    instructions=(
        "Monitors running AI coding agent sessions (Claude Code, Codex, Aider, etc.)."
        " Use list_sessions to see what's running, get_report for a human-readable summary,"
        " get_session_logs to inspect a session's conversation, and dashboard to manage the web UI."
    ),
)


# ─── Helper ───────────────────────────────────────────────────────────────────

def _run_script(*args: str, timeout: int = 15) -> str:
    """Run a shell script and return stdout. Raises on non-zero exit."""
    result = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(SKILL_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Script exited {result.returncode}")
    return result.stdout.strip()


def _http_get(url: str, timeout: int = 5) -> dict:
    """GET JSON from the local dashboard API."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _dashboard_running() -> bool:
    try:
        _http_get(f"{DASHBOARD_URL}/api/sessions", timeout=2)
        return True
    except Exception:
        return False


# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_sessions() -> list[dict]:
    """
    Return all active (and recently completed) AI coding agent sessions.

    Each session has:
      id, command, status (running/done/failed), duration (seconds),
      workdir, startedAt, git {files, insertions, deletions}
    """
    if _dashboard_running():
        try:
            return _http_get(f"{DASHBOARD_URL}/api/sessions")
        except Exception:
            pass
    raw = _run_script("bash", str(SCRIPTS_DIR / "collect_status.sh"))
    return json.loads(raw) if raw else []


@mcp.tool()
def get_report(format: str = "markdown") -> str:
    """
    Return a human-readable report of all agent sessions grouped by status.

    format: "terminal" | "markdown" | "json"  (default: markdown)
    """
    allowed = {"terminal", "markdown", "json"}
    if format not in allowed:
        raise ValueError(f"format must be one of {allowed}")

    collect = _run_script("bash", str(SCRIPTS_DIR / "collect_status.sh"))
    proc = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "format_report.sh"), "--format", format],
        input=collect,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(SKILL_DIR),
    )
    return proc.stdout.strip()


@mcp.tool()
def get_session_logs(session_id: str) -> dict:
    """
    Return structured conversation logs for a session.

    session_id: the 'id' field from list_sessions() (e.g. "proc-12345")

    Returns:
      available (bool), source (str), logs (plain text),
      messages (list of {role, timestamp, text, tools?})
    """
    pid = session_id.replace("proc-", "")

    # 优先从 dashboard API 获取（有结构化消息和 token 数据）
    if _dashboard_running():
        try:
            return _http_get(f"{DASHBOARD_URL}/api/sessions/{pid}/logs")
        except Exception:
            pass

    # 降级：读取最近 50 行日志文本
    try:
        text = _run_script("bash", str(SCRIPTS_DIR / "collect_logs.sh"), pid)
    except Exception as e:
        return {"available": False, "source": "error", "logs": str(e), "messages": []}

    return {
        "available": bool(text),
        "source": f"collect_logs (pid {pid})",
        "logs": text,
        "messages": [],
    }


@mcp.tool()
def dashboard(action: str = "status") -> str:
    """
    Manage the web dashboard (http://localhost:9090/).

    action: "start" | "stop" | "restart" | "status"

    Returns a status message. After 'start', the dashboard is accessible at
    http://localhost:9090/ and also serves the /api/sessions endpoint.
    """
    allowed = {"start", "stop", "restart", "status"}
    if action not in allowed:
        raise ValueError(f"action must be one of {allowed}")

    output = _run_script("bash", str(SCRIPTS_DIR / "monitor.sh"), action, timeout=20)
    return output or f"dashboard {action}: ok"


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
