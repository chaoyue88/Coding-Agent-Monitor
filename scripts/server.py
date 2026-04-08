#!/usr/bin/env python3
"""
Agent Monitor HTTP API Server
轻量级本地 API，用标准库实现，无外部依赖。
监听 127.0.0.1:9090（仅本机可访问）
"""

import datetime
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


# ─── 会话历史缓存 ──────────────────────────────────────────────────────────────

_session_history: dict = {}  # pid -> session_dict
_session_history_lock = threading.Lock()
HISTORY_TTL_SECS = 7 * 24 * 3600  # 已完成会话保留 7 天

# ─── 子进程快照缓存 ──────────────────────────────────────────────────────────

_children_cache: dict = {}  # pid -> {"children": [...], "cached_at": float}
_children_cache_lock = threading.Lock()
CHILDREN_CACHE_TTL_SECS = 7 * 24 * 3600

# ─── 日志快照缓存 ──────────────────────────────────────────────────────────────

_log_cache: dict = {}  # pid -> {data: dict, cached_at: float}
_log_cache_lock = threading.Lock()
LOG_CACHE_TTL_SECS = 7 * 24 * 3600  # 日志快照保留 7 天

# ─── 进程列表 TTL 缓存 ────────────────────────────────────────────────────────

_proc_cache_result: "list | None" = None
_proc_cache_time: float = 0.0
_proc_cache_lock = threading.Lock()
PROC_CACHE_TTL = 4.0  # 秒；dashboard 3s 刷新，4s 缓存可消除重复 ps aux

# ─── ppid map TTL 缓存 ────────────────────────────────────────────────────────

_ppid_map_cache_data: dict = {}
_ppid_map_cache_time: float = 0.0
_ppid_map_cache_lock = threading.Lock()
PPID_MAP_CACHE_TTL = 1.0  # 秒

# ─── Token 使用量缓存（按 session 精确缓存，JSONL 按 mtime 失效）────────────────

_token_usage_cache: dict = {}  # cache_key (session_id|pid|cwd) → {tokens, mtime, jsonl_path}
_token_usage_cache_lock = threading.Lock()

# ─── CPU 使用率后台缓存 ──────────────────────────────────────────────────────

_cpu_percent_cached: "float | None" = None
_cpu_percent_lock = threading.Lock()

# ─── 持久化文件缓存 ────────────────────────────────────────────────────────────

SESSION_CACHE_DIR = os.path.expanduser("~/.openclaw/session-cache")


def _get_cache_filepath(pid: str, data: dict) -> str:
    """根据 session ID 或 pid+启动时间生成缓存文件路径（每次调用相同会话返回同一路径）。"""
    session_info = data.get("session_info") or {}
    session_id = session_info.get("sessionId", "")
    if session_id:
        return os.path.join(SESSION_CACHE_DIR, f"{session_id}.json")
    with _session_history_lock:
        started_at = _session_history.get(pid, {}).get("startedAt", "")
    if started_at:
        safe_ts = started_at.replace(":", "-").replace(" ", "_")
        return os.path.join(SESSION_CACHE_DIR, f"pid{pid}_{safe_ts}.json")
    return os.path.join(SESSION_CACHE_DIR, f"pid{pid}.json")


def _write_session_cache_file(pid: str, data: dict):
    """将会话日志写入持久化 JSON 文件（权限 0o600，仅本用户可读）。"""
    try:
        os.makedirs(SESSION_CACHE_DIR, mode=0o700, exist_ok=True)
        filepath = _get_cache_filepath(pid, data)
        cache_data = dict(data)
        cache_data["_cached_at"] = time.time()
        cache_data["_pid"] = pid
        with _session_history_lock:
            hist = _session_history.get(pid, {})
        cache_data["_session_meta"] = {
            "command": hist.get("command", ""),
            "workdir": hist.get("workdir", ""),
            "startedAt": hist.get("startedAt", ""),
            "endedAt": hist.get("endedAt", ""),
            "duration": hist.get("duration", 0),
            "user": hist.get("user", ""),
            "childrenCount": hist.get("_childrenCount", 0),
            "tokens": hist.get("tokens", {}),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(filepath, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _read_session_cache_file(pid: str) -> "dict | None":
    """从持久化文件中读取指定 pid 的日志快照。"""
    if not os.path.isdir(SESSION_CACHE_DIR):
        return None
    # 先尝试 pid 前缀匹配（覆盖 pid 型文件名）
    for name in os.listdir(SESSION_CACHE_DIR):
        if not name.endswith(".json"):
            continue
        if name.startswith(f"pid{pid}"):
            try:
                with open(os.path.join(SESSION_CACHE_DIR, name), encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("_pid") == pid:
                    return data
            except Exception:
                pass
    # 再按 session_id 类型文件的 _pid 字段匹配
    try:
        for name in os.listdir(SESSION_CACHE_DIR):
            if not name.endswith(".json") or name.startswith("pid"):
                continue
            filepath = os.path.join(SESSION_CACHE_DIR, name)
            try:
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("_pid") == pid:
                    return data
            except Exception:
                pass
    except Exception:
        pass
    return None


def _is_agent_command(cmd: str) -> bool:
    """判断命令行是否为合法 agent 进程（与 get_agent_processes 过滤逻辑一致）。"""
    tokens = cmd.split()
    if not tokens:
        return False
    argv0_base = os.path.basename(tokens[0])
    if AGENT_BINARIES.match(argv0_base):
        return True
    if _INTERP_NAMES.match(argv0_base):
        # 排除内联脚本（node -e '...' / python -c '...'）
        second = tokens[1] if len(tokens) > 1 else ""
        if second in ("-e", "-c"):
            return False
        # 对各参数 token 逐个检查是否为已知 agent 名称
        # （不搜索完整路径字符串，避免 .claude/ 路径误匹配）
        for tok in tokens[1:6]:
            tok_base = os.path.basename(tok)
            # 去掉常见脚本后缀
            tok_stem = re.sub(r"\.(py|js|ts)$", "", tok_base)
            if AGENT_BINARIES.match(tok_stem):
                return True
    return False


def _load_session_history_from_files():
    """服务启动时，从持久化文件恢复已完成会话到内存缓存。

    同一 pid 可能有多个文件（最小元数据文件 + 完整日志文件），
    优先选 available=True 的文件；同等条件下取 _cached_at 最新的。
    """
    if not os.path.isdir(SESSION_CACHE_DIR):
        return
    now = time.time()

    # 第一遍：收集每个 pid 对应的所有候选文件
    pid_candidates: dict = {}  # pid → list of (data, filepath)
    for name in os.listdir(SESSION_CACHE_DIR):
        if not name.endswith(".json"):
            continue
        filepath = os.path.join(SESSION_CACHE_DIR, name)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("_pid", "")
            cached_at = data.get("_cached_at", 0)
            if not pid:
                continue
            if cached_at and (now - cached_at) > LOG_CACHE_TTL_SECS:
                continue
            # 过滤掉非 agent 进程的缓存（bash脚本、find、hooks 等误收录历史）
            meta_cmd = (data.get("_session_meta") or {}).get("command", "")
            if meta_cmd and not _is_agent_command(meta_cmd):
                try:
                    os.remove(filepath)  # 顺手清理脏文件
                except Exception:
                    pass
                continue
            pid_candidates.setdefault(pid, []).append((data, filepath))
        except Exception:
            pass

    # 第二遍：每个 pid 选最优文件（available=True 优先，再取最新 _cached_at）
    for pid, candidates in pid_candidates.items():
        best_data, best_path = max(
            candidates,
            key=lambda x: (x[0].get("available", False), x[0].get("_cached_at", 0)),
        )
        try:
            meta = best_data.get("_session_meta", {})
            cached_at = best_data.get("_cached_at", 0)
            session_id = (best_data.get("session_info") or {}).get("sessionId", "")
            with _session_history_lock:
                if pid not in _session_history:
                    entry = {
                        "pid": pid,
                        "command": meta.get("command", ""),
                        "workdir": meta.get("workdir", ""),
                        "startedAt": meta.get("startedAt"),
                        "endedAt": meta.get("endedAt"),
                        "duration": meta.get("duration", 0),
                        "status": "done",
                        "tokens": meta.get("tokens", {}),
                        "user": meta.get("user", ""),
                        "cpu": "0.0",
                        "mem": "0.0",
                        "lastSeenAt": cached_at,
                        "_from_file": True,
                        "_cache_file": best_path,
                    }
                    if session_id:
                        entry["sessionId"] = session_id
                    _session_history[pid] = entry
            # 仅当日志有效时才放入内存日志缓存
            if best_data.get("available"):
                with _log_cache_lock:
                    _log_cache[pid] = {"data": best_data, "cached_at": cached_at}
        except Exception:
            pass


def _persist_session_done(pid: str):
    """同步写入会话结束的最小元数据文件。

    在 merge_with_history 检测到进程刚结束时立即调用，
    保证服务意外 kill 时该会话记录不丢失。
    后续 _cache_logs_async 会用完整日志数据覆盖此文件。
    """
    try:
        with _session_history_lock:
            hist = dict(_session_history.get(pid, {}))
        if not hist:
            return
        minimal_data = {
            "pid": pid,
            "messages": [],
            "logs": "",
            "available": False,
            "source": "session_done_marker",
            "session_info": {"cwd": hist.get("workdir", ""), "sessionId": ""},
        }
        _write_session_cache_file(pid, minimal_data)
    except Exception:
        pass


def _cache_logs_async(pid: str):
    """在后台线程中抓取进程日志并写入缓存。进程刚结束时调用。"""
    # 在主线程中读取 hint_workdir，避免会话历史被清理后拿不到
    with _session_history_lock:
        hint_workdir = _session_history.get(pid, {}).get("workdir")

    def _run():
        try:
            data = get_proc_logs(pid, hint_workdir=hint_workdir)
            # 无论日志是否可读，都写文件——确保会话记录重启后可恢复
            _write_session_cache_file(pid, data)
            if data.get("available"):
                with _log_cache_lock:
                    _log_cache[pid] = {"data": data, "cached_at": time.time()}
                # 将 sessionId 写回 session_history，供后续精确匹配
                session_id = (data.get("session_info") or {}).get("sessionId", "")
                if session_id:
                    with _session_history_lock:
                        if pid in _session_history:
                            _session_history[pid]["sessionId"] = session_id
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _evict_log_cache():
    """清理过期日志快照（内部自行加锁，调用方无需持有 _log_cache_lock）。"""
    now = time.time()
    with _log_cache_lock:
        for pid in list(_log_cache.keys()):
            if now - _log_cache[pid]["cached_at"] > LOG_CACHE_TTL_SECS:
                del _log_cache[pid]


def get_cached_logs(pid: str):
    """返回缓存的日志快照，内存未命中则降级到文件缓存。"""
    with _log_cache_lock:
        entry = _log_cache.get(pid)
    if entry:
        return entry["data"]
    # 降级：从持久化文件读取
    file_data = _read_session_cache_file(pid)
    if file_data:
        with _log_cache_lock:
            _log_cache[pid] = {
                "data": file_data,
                "cached_at": file_data.get("_cached_at", time.time()),
            }
        return file_data
    return None


def _get_ppid(pid):
    """Read PPID from /proc/<pid>/stat. Returns str or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
        rpar = raw.rfind(")")
        if rpar == -1:
            return None
        fields = raw[rpar + 2 :].split()
        if len(fields) < 2:
            return None
        return fields[1]
    except Exception:
        return None


def _get_ppid_from_history(pid):
    """Read PPID from cached session_history when /proc is unavailable."""
    with _session_history_lock:
        entry = _session_history.get(pid)
    if entry:
        return entry.get("_ppid")
    return None


def _get_live_children(pid):
    """Get live child processes list from /proc for a running process."""
    return get_child_processes(pid).get("children", [])


def get_cached_children(pid):
    """Return cached children for a process (alive or done).

    For each cached child, check if it's still alive in /proc.
    If not, mark its status as "done".
    """
    with _children_cache_lock:
        entry = _children_cache.get(pid)
    if entry:
        children = []
        for child in entry["children"]:
            child = dict(child)
            if not os.path.exists(f"/proc/{child['pid']}"):
                child["status"] = "done"
            children.append(child)
        return children
    return None


def _cache_children(pid, children):
    """Cache children list for a process."""
    with _children_cache_lock:
        _children_cache[pid] = {"children": children, "cached_at": time.time()}


def _build_full_ppid_map() -> dict:
    """Build a complete pid→ppid map for all live processes from /proc (1s TTL cache).

    Returns a dict mapping pid_str → ppid_str for every readable /proc/<pid>/stat.
    Used for transitive ancestor checks (handles cases like:
    agent(100) → shell(200) → sub-agent(300) where sub-agent's PPID is 200, not 100).
    """
    global _ppid_map_cache_data, _ppid_map_cache_time
    now = time.time()
    with _ppid_map_cache_lock:
        if now - _ppid_map_cache_time < PPID_MAP_CACHE_TTL:
            return dict(_ppid_map_cache_data)
    result = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            ppid = _get_ppid(entry)
            if ppid:
                result[entry] = ppid
    except Exception:
        pass
    with _ppid_map_cache_lock:
        _ppid_map_cache_data = result
        _ppid_map_cache_time = time.time()
    return result


def filter_child_processes(sessions):
    """Remove agent processes that are descendants of other agent processes.

    Uses a full /proc ppid map for transitive ancestor detection, so multi-hop
    chains (e.g. agent→shell→sub-agent) are correctly identified as children.
    childrenCount on each parent reflects ALL direct child processes (not only
    agent-matching ones), consistent with the modal children tab.
    For running processes, children are captured and cached now so they survive
    after the process exits.
    """
    pid_set = {s["pid"] for s in sessions}

    # Build complete live ppid map from /proc for transitive ancestor walk
    full_ppid_map = _build_full_ppid_map()

    # Supplement with cached ppids from session_history (for done processes)
    with _session_history_lock:
        for pid, data in _session_history.items():
            if "_ppid" in data and pid not in full_ppid_map:
                full_ppid_map[pid] = data["_ppid"]

    # Persist each session's ppid into history for future done-process lookups
    for s in sessions:
        pid = s["pid"]
        ppid = full_ppid_map.get(pid)
        with _session_history_lock:
            if pid in _session_history:
                _session_history[pid]["_ppid"] = ppid

    def has_agent_ancestor(pid: str) -> bool:
        """Walk ancestor chain; return True if any ancestor is an agent process."""
        visited: set = set()
        current = full_ppid_map.get(pid)
        while current and current not in visited:
            if current in pid_set:
                return True
            visited.add(current)
            current = full_ppid_map.get(current)
        return False

    child_pids = {s["pid"] for s in sessions if has_agent_ancestor(s["pid"])}

    result = []
    for s in sessions:
        if s["pid"] in child_pids:
            continue
        s = dict(s)
        pid = s["pid"]
        if s.get("status") == "running":
            children = _get_live_children(pid)
            cc = len(children)
            _cache_children(pid, children)
        else:
            cached = get_cached_children(pid)
            if cached is not None:
                cc = len(cached)
            else:
                cc = 0
        s["childrenCount"] = cc
        with _session_history_lock:
            if pid in _session_history:
                _session_history[pid]["_childrenCount"] = cc
        result.append(s)
    return result


def merge_with_history(live_sessions):
    """合并活跃进程与历史缓存，将消失进程标记为 done，返回完整列表。"""
    now = time.time()
    newly_done = []  # 本轮刚变为 done 的 pid 列表

    with _session_history_lock:
        live_pids = {s["pid"] for s in live_sessions}

        # 更新/新增活跃会话
        for s in live_sessions:
            pid = s["pid"]
            entry = dict(s)
            entry["lastSeenAt"] = now
            # 保留历史中的 endedAt（不应覆盖）
            if pid in _session_history and "endedAt" in _session_history[pid]:
                entry["endedAt"] = _session_history[pid]["endedAt"]
            _session_history[pid] = entry

        # 将消失的进程标记为已完成并记录结束时间（显式赋值保证原子性）
        for pid, cached in list(_session_history.items()):
            if pid not in live_pids and cached.get("status") == "running":
                _session_history[pid] = {**cached, "status": "done", "endedAt": now}
                newly_done.append(pid)

            # 清理超过 TTL 的已结束会话
            ended = cached.get("endedAt")
            if ended and (now - ended) > HISTORY_TTL_SECS:
                del _session_history[pid]

        # 返回全部（活跃 + 近期已完成）
        result = list(_session_history.values())

    # 锁外：同步写最小元数据（防止 kill 时丢失），再异步写完整日志
    for pid in newly_done:
        _persist_session_done(pid)  # 同步，防 kill 安全网
        _cache_logs_async(pid)  # 异步，完整日志（成功时覆盖上面的文件）

    # 顺带清理过期日志缓存
    _evict_log_cache()

    return result


# ─── 进程扫描 ─────────────────────────────────────────────────────────────────

# 直接作为可执行文件运行的 agent 名称（精确匹配 argv[0] 的 basename）
AGENT_BINARIES = re.compile(r"^(claude|codex|opencode|aider|cursor|copilot)$", re.IGNORECASE)
# 对于 python/node 解释器，需要在完整 cmdline 中搜索关键词
AGENT_PATTERNS_INTERP = re.compile(r"claude|codex|opencode|aider", re.IGNORECASE)
# 解释器列表（argv[0] 是这些时才搜索完整 cmdline）
_INTERP_NAMES = re.compile(r"^(python3?|node|npx|uvx|pipx)$", re.IGNORECASE)


def _read_proc_cmdline(pid: str) -> str:
    """从 /proc/<pid>/cmdline 读取完整命令行（不受 ps aux 宽度截断限制）。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return (
                f.read()
                .replace(b"\x00", b" ")
                .decode("utf-8", errors="replace")
                .strip()
            )
    except Exception:
        return ""


def get_agent_processes():
    """扫描系统进程，返回编码 agent 进程列表。"""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        return []

    sessions = []
    now = time.time()

    for line in result.stdout.splitlines()[1:]:  # 跳过表头
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue

        user, pid, cpu, mem, vsz, rss, tty, stat, start_str, elapsed_str, ps_cmd = parts
        ps_cmd = ps_cmd.strip()

        # 过滤掉 grep 自身 和 server.py 自身
        if "grep" in ps_cmd or "server.py" in ps_cmd:
            continue

        # 从 /proc/<pid>/cmdline 读取完整命令行（不受 ps aux 宽度截断限制）
        cmd = _read_proc_cmdline(pid) or ps_cmd

        # 提取 argv[0] 的 basename 用于精确匹配
        argv0 = cmd.split()[0] if cmd.split() else ""
        argv0_base = os.path.basename(argv0)

        # 判断是否为 agent 进程（复用 _is_agent_command 逻辑）
        if not _is_agent_command(cmd):
            continue

        # 获取工作目录
        workdir = get_proc_cwd(pid)

        # 读取 session 元数据（用于精确定位 JSONL / SQLite session）
        session_info = get_claude_session_info(pid)

        # 计算运行时长和启动时间
        duration, started_at = get_proc_timing(pid)

        # token 使用量（按 session 精确定位，不再按 cwd 模糊匹配）
        tokens = (
            get_session_token_usage(
                workdir,
                session_info=session_info,
                pid=pid,
                started_at=started_at,
            )
            if workdir
            else {}
        )

        # 判断进程状态（Linux /proc/<pid>/stat）
        status_char = stat[7] if len(stat) > 7 else "R"
        status = "running"
        if status_char in ("Z", "x", "X"):
            status = "failed"  # zombie 或已退出
        elif status_char in ("t", "T"):
            status = "stopped"  # 停止态
        elif status_char == "D":
            status = "running"  # 不可中断睡眠（通常仍活跃）

        sessions.append(
            {
                "pid": pid,
                "command": cmd,
                "status": status,
                "duration": duration,
                "startedAt": started_at,
                "workdir": workdir or "",
                "tokens": tokens,
                "user": user,
                "cpu": cpu,
                "mem": mem,
            }
        )

    return sessions


def _get_agent_processes_cached() -> list:
    """get_agent_processes() 的 TTL 缓存包装（4s），避免 dashboard 高频轮询重复执行 ps aux。"""
    global _proc_cache_result, _proc_cache_time
    now = time.time()
    with _proc_cache_lock:
        if _proc_cache_result is not None and (now - _proc_cache_time) < PROC_CACHE_TTL:
            return _proc_cache_result
    result = get_agent_processes()
    with _proc_cache_lock:
        _proc_cache_result = result
        _proc_cache_time = time.time()
    return result


def get_proc_cwd(pid):
    """从 /proc/<pid>/cwd 获取工作目录（Linux）。"""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def get_proc_timing(pid):
    """从 /proc/<pid>/stat 计算进程运行时长（秒）和启动时间（ISO 字符串）。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().split()
        if len(stat) < 22:
            return 0, None
        clk_tck = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        starttime_ticks = int(stat[21])
        start_secs_since_boot = starttime_ticks / clk_tck
        duration = int(uptime - start_secs_since_boot)
        if duration < 0:
            duration = 0
        boot_epoch = time.time() - uptime
        start_epoch = boot_epoch + start_secs_since_boot
        started_at = datetime.datetime.fromtimestamp(start_epoch).isoformat(
            timespec="seconds"
        )
        return duration, started_at
    except FileNotFoundError:
        return 0, None
    except (ValueError, OSError) as e:
        return 0, None


def get_child_processes(ppid: str) -> dict:
    """返回 ppid 的直接子进程列表（通过遍历 /proc/*/stat 匹配 ppid 字段）。"""
    children = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or entry == ppid:
                continue
            stat_path = f"/proc/{entry}/stat"
            try:
                with open(stat_path) as f:
                    stat_data = f.read()
                # 进程名含括号，需正确拆分：跳过括号内内容
                rpar = stat_data.rfind(")")
                if rpar == -1:
                    continue
                fields_after = stat_data[rpar + 2 :].split()
                if len(fields_after) < 2:
                    continue
                parent_pid = fields_after[1]  # 第4字段（0-indexed after name）
                if parent_pid != ppid:
                    continue
            except (OSError, IOError):
                continue

            # 读取命令行
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmd_raw = (
                        f.read()
                        .replace(b"\x00", b" ")
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
            except Exception:
                cmd_raw = ""

            # 保留完整命令（显示层可自行截断）
            cmd_short = cmd_raw

            # 状态字符
            status_char = fields_after[0] if fields_after else "R"
            if status_char in ("Z", "x", "X"):
                status = "failed"
            elif status_char in ("t", "T"):
                status = "stopped"
            else:
                status = "running"

            # 时长和启动时间
            duration, started_at = get_proc_timing(entry)

            # CPU 和内存（从 /proc/{pid}/status 读 VmRSS）
            mem_mb = 0
            try:
                with open(f"/proc/{entry}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            mem_mb = round(int(line.split()[1]) / 1024, 1)
                            break
            except Exception:
                pass

            # CPU%（单次读取 utime+stime，粗略估算占比）
            cpu_pct = 0.0
            try:
                with open(stat_path) as f:
                    raw = f.read()
                rp = raw.rfind(")")
                flds = raw[rp + 2 :].split()
                utime = int(flds[11])  # field 14 (0-indexed: 11 after name+state+ppid)
                stime = int(flds[12])
                clk_tck = os.sysconf("SC_CLK_TCK")
                with open("/proc/uptime") as f:
                    uptime = float(f.read().split()[0])
                proc_time = (utime + stime) / clk_tck
                cpu_pct = round(proc_time / uptime * 100, 1) if uptime > 0 else 0.0
            except Exception:
                pass

            children.append(
                {
                    "pid": entry,
                    "cmd": cmd_short,
                    "status": status,
                    "duration": duration,
                    "startedAt": started_at,
                    "mem_mb": mem_mb,
                    "cpu_pct": cpu_pct,
                }
            )
    except Exception as e:
        return {"children": [], "error": str(e)}

    # 按启动时间排序
    children.sort(key=lambda c: c.get("startedAt") or "")
    return {"children": children}


def estimate_duration(pid):
    """估算进程运行时长（秒），从 /proc/<pid>/stat 中读取。"""
    duration, _ = get_proc_timing(pid)
    return duration


def _parse_jsonl_tokens(jsonl_file: str, started_at: str = "") -> dict:
    """解析单个 Claude JSONL 文件，汇总 token 使用量。

    started_at: ISO 字符串（进程启动时间）。非空时仅统计该时间点之后的 entry，
    确保 resumed session 下只显示当前进程的 token 用量。
    """
    import datetime as _dt

    cutoff_ts: "float | None" = None
    if started_at:
        try:
            cutoff_ts = _dt.datetime.fromisoformat(started_at).timestamp()
        except Exception:
            cutoff_ts = None

    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    turns = 0
    last_context = 0
    try:
        with open(jsonl_file, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("type") != "assistant":
                        continue
                    # 按进程启动时间过滤：只统计当前进程会话内的 token
                    if cutoff_ts is not None:
                        ts_str = entry.get("timestamp", "")
                        if ts_str:
                            try:
                                entry_ts = _dt.datetime.fromisoformat(
                                    ts_str.replace("Z", "+00:00")
                                ).timestamp()
                                if entry_ts < cutoff_ts:
                                    continue
                            except Exception:
                                pass  # 解析失败则不过滤该条
                    usage = entry.get("message", {}).get("usage", {})
                    if not usage:
                        continue
                    inp = usage.get("input_tokens", 0) or 0
                    out = usage.get("output_tokens", 0) or 0
                    cr = usage.get("cache_read_input_tokens", 0) or 0
                    cw = usage.get("cache_creation_input_tokens", 0) or 0
                    input_tokens += inp
                    output_tokens += out
                    cache_read += cr
                    cache_write += cw
                    last_context = inp + cr + cw
                    if out > 0:
                        turns += 1
                except Exception:
                    continue
    except Exception:
        return {}
    return {
        "output_tokens": output_tokens,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "last_context": last_context,
        "turns": turns,
    }


def _cached_token_read(cache_key: str, jsonl_file: str, started_at: str = "") -> "dict | None":
    """尝试从缓存返回 token 统计，未命中返回 None。

    cache_key 已由调用方将 started_at 编入，保证不同启动时间互不干扰。
    """
    try:
        mtime = os.path.getmtime(jsonl_file)
    except Exception:
        return None
    with _token_usage_cache_lock:
        cached = _token_usage_cache.get(cache_key)
        if (
            cached
            and cached.get("jsonl_path") == jsonl_file
            and cached.get("mtime") == mtime
        ):
            return cached["tokens"]
    result = _parse_jsonl_tokens(jsonl_file, started_at=started_at)
    if result:
        with _token_usage_cache_lock:
            _token_usage_cache[cache_key] = {
                "tokens": result,
                "mtime": mtime,
                "jsonl_path": jsonl_file,
            }
    return result


def get_session_token_usage(
    cwd: str, session_info: dict = None, pid: str = "", started_at: str = ""
) -> dict:
    """读取 token 使用量，按 session 精确定位。

    优先级：
    1. Claude: 用 session_info.sessionId 精确匹配 JSONL 文件
    2. Claude: 有 cwd 但无 sessionId → 取最新 JSONL（降级，可能不精确）
    3. opencode: 从 SQLite 按进程启动时间匹配 session
    4. 缓存 key 依次用 sessionId / opencode_session_id / cwd
    """
    is_opencode = False
    opencode_sid = ""

    if session_info:
        sid = session_info.get("sessionId", "")
        session_cwd = session_info.get("cwd", "")
        if sid and session_cwd:
            sanitized = _sanitize_path_for_claude(session_cwd)
            project_dir = os.path.expanduser(f"~/.claude/projects/{sanitized}")
            jsonl_file = os.path.join(project_dir, f"{sid}.jsonl")
            if os.path.exists(jsonl_file):
                # 加入 started_at 使不同进程启动时间的缓存互不干扰
                cache_key = f"{sid}|{started_at}" if started_at else sid
                return _cached_token_read(cache_key, jsonl_file, started_at=started_at) or {}

    if _is_opencode_process(pid):
        is_opencode = True
        opencode_sid, tokens = get_opencode_token_usage(
            cwd=cwd, session_info=session_info, pid=pid, started_at=started_at
        )
        # opencode 进程无论是否有 token 记录，都不走 Claude JSONL 逻辑
        # 否则会把同 cwd 下 Claude 的 token 误算为 opencode 的
        return tokens if tokens else {}

    if not cwd:
        return {}
    sanitized = _sanitize_path_for_claude(cwd)
    project_dir = os.path.expanduser(f"~/.claude/projects/{sanitized}")
    if not os.path.isdir(project_dir):
        return {}
    try:
        candidates = sorted(
            [
                os.path.join(project_dir, f)
                for f in os.listdir(project_dir)
                if f.endswith(".jsonl")
            ],
            key=os.path.getmtime,
            reverse=True,
        )
    except Exception:
        return {}
    if not candidates:
        return {}
    # 如有 started_at，仅选取 mtime >= 进程启动时间的 JSONL（精确匹配当前会话文件）
    if started_at:
        import datetime as _dt
        try:
            cutoff = _dt.datetime.fromisoformat(started_at).timestamp()
            current_candidates = [f for f in candidates if os.path.getmtime(f) >= cutoff - 5]
            if current_candidates:
                candidates = current_candidates
        except Exception:
            pass
    cache_key = f"{cwd}|{started_at}" if started_at else cwd
    return _cached_token_read(cache_key, candidates[0], started_at=started_at) or {}


def get_opencode_token_usage(
    cwd: str = "", session_info: dict = None, pid: str = "", started_at: str = ""
) -> tuple:
    """从 opencode SQLite 按精确 session 统计 token 用量。

    优先用进程启动时间 (started_at) 匹配 session 的 time_created，
    确保只统计当前会话的 token，而非同目录下所有会话的累计值。

    返回 (session_id, tokens_dict) 或 ("", {})。
    tokens_dict 格式与 Claude 一致：
      output_tokens, input_tokens, cache_read_tokens, cache_write_tokens, last_context, turns
    """
    import sqlite3

    db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.exists(db_path):
        return "", {}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return "", {}

    try:
        eff_cwd = cwd
        if session_info:
            eff_cwd = session_info.get("cwd", "") or cwd

        if not eff_cwd:
            return "", {}

        # 将 started_at ISO 字符串转为 epoch 秒
        process_start_epoch = None
        if started_at:
            try:
                process_start_epoch = datetime.datetime.fromisoformat(
                    started_at
                ).timestamp()
            except Exception:
                pass

        if process_start_epoch:
            # 策略：找进程启动后创建的、最近活跃的 session
            # opencode session 的 time_created 是毫秒级 epoch
            pre_tolerance_ms = 30_000  # 允许 session 比进程早 30 秒创建（边界情况）
            start_ms = process_start_epoch * 1000
            row = conn.execute(
                "SELECT id FROM session WHERE directory=? AND time_created >= ? ORDER BY time_updated DESC LIMIT 1",
                (eff_cwd, start_ms - pre_tolerance_ms),
            ).fetchone()
            if not row:
                # 降级：进程启动前就存在的 session（如 attach 到已有 session 的场景）
                row = conn.execute(
                    "SELECT id FROM session WHERE directory=? ORDER BY time_updated DESC LIMIT 1",
                    (eff_cwd,),
                ).fetchone()
        else:
            # 无 started_at 时取最近更新的会话（兼容旧逻辑）
            row = conn.execute(
                "SELECT id FROM session WHERE directory=? ORDER BY time_updated DESC LIMIT 1",
                (eff_cwd,),
            ).fetchone()
        if not row:
            return "", {}
        session_id = row[0]

        msgs = conn.execute(
            "SELECT data FROM message WHERE session_id=? ORDER BY time_created",
            (session_id,),
        ).fetchall()

        if not msgs:
            return session_id, {}

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_reasoning = 0
        turns = 0
        last_context = 0

        for (msg_data,) in msgs:
            try:
                d = json.loads(msg_data)
                if d.get("role") != "assistant":
                    continue
                tokens = d.get("tokens", {})
                if not tokens:
                    continue
                inp = tokens.get("input", 0) or 0
                out = tokens.get("output", 0) or 0
                cache = tokens.get("cache", {}) or {}
                cr = cache.get("read", 0) or 0
                cw = cache.get("write", 0) or 0
                reasoning = tokens.get("reasoning", 0) or 0
                total_input += inp
                total_output += out
                total_cache_read += cr
                total_cache_write += cw
                total_reasoning += reasoning
                last_context = inp + cr + cw
                if out > 0:
                    turns += 1
            except Exception:
                continue

        if turns == 0 and total_input == 0:
            return session_id, {}

        result = {
            "output_tokens": total_output,
            "input_tokens": total_input,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "last_context": last_context,
            "turns": turns,
        }
        if total_reasoning > 0:
            result["reasoning_tokens"] = total_reasoning

        conn.close()
        return session_id, result
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return "", {}


def get_git_status(workdir):
    """获取 workdir 的 git 状态：分支名、变更文件数、增删行数。"""
    if not workdir or not os.path.isdir(workdir):
        return {}
    try:
        # 当前分支
        branch_result = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""

        # diff 统计（已暂存 + 未暂存）
        diff_result = subprocess.run(
            ["git", "-C", workdir, "diff", "--stat", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        files, insertions, deletions = 0, 0, 0
        if diff_result.returncode == 0:
            for line in diff_result.stdout.splitlines():
                m = re.search(r"(\d+) file", line)
                if m:
                    files = int(m.group(1))
                m_ins = re.search(r"(\d+) insertion", line)
                if m_ins:
                    insertions = int(m_ins.group(1))
                m_del = re.search(r"(\d+) deletion", line)
                if m_del:
                    deletions = int(m_del.group(1))

        return {
            "branch": branch,
            "files": files,
            "insertions": insertions,
            "deletions": deletions,
        }
    except Exception:
        return {}


# ─── 日志读取 ──────────────────────────────────────────────────────────────────


def _sanitize_path_for_claude(cwd):
    """将路径转换为 Claude 项目目录名（'/' → '-'，'.' → '-'）。

    Claude Code 实际行为：同时将 '/' 和 '.' 替换为 '-'。
    例如 /home/sam/.openclaw → -home-sam--openclaw
    """
    return cwd.replace("/", "-").replace(".", "-")


def get_claude_session_info(pid):
    """读取 ~/.claude/sessions/<pid>.json，返回 sessionId、cwd 等元数据。"""
    session_file = os.path.expanduser(f"~/.claude/sessions/{pid}.json")
    try:
        with open(session_file) as f:
            return json.load(f)
    except Exception:
        return None


def _parse_jsonl_messages(jsonl_file, max_chars=50000):
    """解析 JSONL 对话文件，返回结构化消息列表。"""
    try:
        with open(jsonl_file, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    messages = []
    total_chars = 0
    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        role = entry.get("type", "")
        if role not in ("assistant", "user"):
            continue
        msg = entry.get("message", {})
        raw_content = msg.get("content", "")
        ts = entry.get("timestamp", "")

        ts_str = ""
        if ts:
            try:
                ts_str = datetime.datetime.fromtimestamp(ts / 1000).isoformat(
                    timespec="seconds"
                )
            except Exception:
                pass

        text_parts = []
        tool_calls = []
        if isinstance(raw_content, list):
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        }
                    )
                elif btype == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, list):
                        rc = " ".join(
                            b.get("text", "") for b in rc if isinstance(b, dict)
                        )
                    tool_calls.append(
                        {
                            "type": "result",
                            "content": str(rc)[:500],
                        }
                    )
        elif isinstance(raw_content, str):
            text_parts.append(raw_content)

        text = "\n".join(t for t in text_parts if t)
        if not text.strip() and not tool_calls:
            continue

        msg_obj = {
            "role": role,
            "timestamp": ts_str,
            "text": text.strip(),
        }
        if tool_calls:
            msg_obj["tools"] = tool_calls

        messages.append(msg_obj)
        total_chars += len(text)
        if total_chars > max_chars:
            break

    if not messages:
        return []

    if total_chars > max_chars:
        messages = _truncate_messages(messages, max_chars)

    return messages


def _truncate_messages(messages, max_chars):
    """从末尾保留消息直到总字符数不超过 max_chars。"""
    result = []
    total = 0
    for msg in reversed(messages):
        msg_len = len(msg.get("text", ""))
        if total + msg_len > max_chars:
            msg["text"] = "…（截断）\n" + msg["text"][-(max_chars - total) :]
            result.insert(0, msg)
            break
        result.insert(0, msg)
        total += msg_len
    if not result:
        result = messages[-3:]
    return result


def _messages_to_plain_text(messages):
    """将结构化消息列表转换为纯文本（向后兼容）。"""
    parts = []
    for msg in messages:
        ts_str = ""
        if msg.get("timestamp"):
            try:
                dt = datetime.datetime.fromisoformat(msg["timestamp"])
                ts_str = dt.strftime("%H:%M:%S") + " "
            except Exception:
                ts_str = msg["timestamp"] + " "
        prefix = {"assistant": "🤖 ", "user": "👤 "}.get(msg["role"], "  ")
        text = msg.get("text", "")
        for tool in msg.get("tools", []):
            if tool.get("type") == "result":
                text += f"\n[工具结果: {tool['content']}]"
            else:
                text += f"\n[工具调用: {tool.get('name', '')}({json.dumps(tool.get('input', {}), ensure_ascii=False)[:120]})]"
        if text.strip():
            parts.append(f"{ts_str}{prefix}{text.strip()}")
    if not parts:
        return None
    return "\n\n".join(parts)


def get_claude_conversation_logs(session_info, max_chars=50000):
    """根据 session_info 找到 JSONL 对话文件，提取最近 N 轮消息。"""
    cwd = session_info.get("cwd", "")
    session_id = session_info.get("sessionId", "")
    if not cwd or not session_id:
        return None

    sanitized = _sanitize_path_for_claude(cwd)
    project_dir = os.path.expanduser(f"~/.claude/projects/{sanitized}")
    jsonl_file = os.path.join(project_dir, f"{session_id}.jsonl")

    if not os.path.exists(jsonl_file) and os.path.isdir(project_dir):
        candidates = sorted(
            [
                os.path.join(project_dir, f)
                for f in os.listdir(project_dir)
                if f.endswith(".jsonl")
            ],
            key=os.path.getmtime,
            reverse=True,
        )
        if candidates:
            jsonl_file = candidates[0]

    if not os.path.exists(jsonl_file):
        return None

    return _parse_jsonl_messages(jsonl_file, max_chars)


def _fd_log_to_messages(content):
    """将文件 fd 读取的纯文本日志转换为结构化消息列表。"""
    messages = [{"role": "log", "timestamp": "", "text": content}]
    return messages


def _is_opencode_process(pid):
    """检查进程是否为 opencode 进程（读取 /proc/<pid>/cmdline）。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = (
                f.read().decode("utf-8", errors="replace").replace("\x00", " ").lower()
            )
        return "opencode" in cmdline
    except Exception:
        return False


def get_opencode_session_logs(cwd, max_chars=50000):
    """从 opencode SQLite 数据库读取指定目录最近会话的对话消息。

    返回 (messages, session_id) 或 (None, None)。
    """
    import sqlite3

    db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.exists(db_path):
        return None, None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

        # 找到该目录最近更新的会话
        row = conn.execute(
            "SELECT id FROM session WHERE directory=? ORDER BY time_updated DESC LIMIT 1",
            (cwd,),
        ).fetchone()
        if not row:
            conn.close()
            return None, None

        session_id = row[0]

        msgs = conn.execute(
            "SELECT id, data FROM message WHERE session_id=? ORDER BY time_created",
            (session_id,),
        ).fetchall()

        messages = []
        total_chars = 0

        for msg_id, msg_data in msgs:
            entry = json.loads(msg_data)
            role = entry.get("role", "")
            if role not in ("assistant", "user"):
                continue

            ts_ms = (entry.get("time") or {}).get("created") or 0
            ts_str = ""
            if ts_ms:
                try:
                    ts_str = datetime.datetime.fromtimestamp(ts_ms / 1000).isoformat(
                        timespec="seconds"
                    )
                except Exception:
                    pass

            parts = conn.execute(
                "SELECT data FROM part WHERE message_id=? ORDER BY time_created",
                (msg_id,),
            ).fetchall()

            text_parts = []
            tool_calls = []

            for (part_data,) in parts:
                part = json.loads(part_data)
                ptype = part.get("type", "")
                if ptype == "text":
                    t = part.get("text", "").strip()
                    if t:
                        text_parts.append(t)
                elif ptype == "tool":
                    state = part.get("state", {})
                    tool_entry = {
                        "name": part.get("tool", ""),
                        "input": state.get("input", {}),
                    }
                    if state.get("status") == "error":
                        tool_entry["error"] = state.get("error", "")
                    tool_calls.append(tool_entry)

            text = "\n".join(text_parts)
            if not text.strip() and not tool_calls:
                continue

            msg_obj = {
                "role": role,
                "timestamp": ts_str,
                "text": text.strip(),
            }
            if tool_calls:
                msg_obj["tools"] = tool_calls

            messages.append(msg_obj)
            total_chars += len(text)

        conn.close()

        if total_chars > max_chars:
            messages = _truncate_messages(messages, max_chars)

        return messages, session_id

    except Exception:
        return None, None


def _find_recent_claude_logs_by_cwd(cwd, max_chars=50000):
    """无 session 文件时，直接用 workdir 找最近修改的 JSONL 对话文件。"""
    if not cwd:
        return None, None
    sanitized = _sanitize_path_for_claude(cwd)
    project_dir = os.path.expanduser(f"~/.claude/projects/{sanitized}")
    if not os.path.isdir(project_dir):
        return None, None
    try:
        candidates = sorted(
            [
                os.path.join(project_dir, f)
                for f in os.listdir(project_dir)
                if f.endswith(".jsonl")
            ],
            key=os.path.getmtime,
            reverse=True,
        )
    except Exception:
        return None, None
    if not candidates:
        return None, None
    jsonl_file = candidates[0]
    session_id = os.path.splitext(os.path.basename(jsonl_file))[0]
    msgs = _parse_jsonl_messages(jsonl_file, max_chars)
    return msgs or None, session_id


def get_proc_logs(pid, hint_workdir=None, hint_session_id=None):
    """多级策略读取进程日志：opencode SQLite → Claude 会话 JSONL → 文件 fd → 错误提示。

    hint_workdir: 进程已结束时由调用方从 session_history 传入的工作目录，
                  用于在没有 sessions/<pid>.json 的情况下定位 JSONL 文件。
    hint_session_id: 已结束会话的精确 sessionId，优先用于精确 JSONL 匹配，
                     避免 _find_recent_claude_logs_by_cwd 误拿到新会话日志。

    返回 dict 包含:
      - messages: 结构化消息列表 [{role, timestamp, text, tools?}]
      - logs: 纯文本（向后兼容）
      - 其余字段同前
    """
    # 方式0: opencode 进程优先读取 SQLite 对话数据库
    if _is_opencode_process(pid):
        cwd = get_proc_cwd(pid) or hint_workdir
        if cwd:
            msgs, session_id = get_opencode_session_logs(cwd)
            if msgs:
                plain = _messages_to_plain_text(msgs)
                return {
                    "pid": pid,
                    "messages": msgs,
                    "logs": plain or "",
                    "available": True,
                    "source": f"opencode 会话记录 ({session_id[:8] if session_id else ''}…)",
                    "session_info": {"cwd": cwd, "sessionId": session_id or ""},
                }

    session_info = get_claude_session_info(pid)

    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except Exception:
        fds = []

    for fd in fds:
        fd_path = os.path.join(fd_dir, fd)
        try:
            target = os.readlink(fd_path)
        except PermissionError:
            return {
                "pid": pid,
                "messages": [],
                "logs": "",
                "available": False,
                "error": "permission_denied",
                "hint": f"进程 {pid} 的文件描述符不可访问，可能需要 sudo。",
                "session_info": session_info,
            }
        except Exception:
            continue

        if any(
            x in target
            for x in ("pipe:", "socket:", "anon_inode", "/dev/pts", "/dev/tty")
        ):
            continue
        tgt_lower = target.lower()
        if any(
            kw in tgt_lower
            for kw in (
                "credential",
                "secret",
                "token",
                "auth",
                "password",
                "private_key",
                ".env",
                "id_rsa",
                "id_ed25519",
            )
        ):
            continue
        claude_home = os.path.expanduser("~/.claude")
        openclaw_home = os.path.expanduser("~/.openclaw")
        opencode_log_dir = os.path.expanduser("~/.local/share/opencode/log")
        if (
            target.startswith(claude_home)
            or target.startswith(openclaw_home)
            or target.startswith(opencode_log_dir)
        ):
            continue
        is_log_like = (
            target.endswith((".log", ".txt", ".out", ".err"))
            or target.startswith("/tmp/")
            or "/logs/" in target
        )
        if not is_log_like:
            continue
        try:
            if not os.path.isfile(target):
                continue
            size = os.path.getsize(target)
            if size == 0:
                continue
            with open(target, "rb") as f:
                f.seek(max(0, size - 4096))
                content = f.read().decode("utf-8", errors="replace").strip()
            if content:
                messages = _fd_log_to_messages(content)
                return {
                    "pid": pid,
                    "messages": messages,
                    "logs": content,
                    "available": True,
                    "source": target,
                    "session_info": session_info,
                }
        except Exception:
            continue

    # 方式2a: 读取 Claude 会话 JSONL（有 session 文件）
    if session_info:
        msgs = get_claude_conversation_logs(session_info)
        if msgs:
            plain = _messages_to_plain_text(msgs)
            cwd = session_info.get("cwd", "")
            sid = session_info.get("sessionId", "")
            return {
                "pid": pid,
                "messages": msgs,
                "logs": plain or "",
                "available": True,
                "source": f"Claude 会话记录 ({sid[:8]}…)",
                "session_info": session_info,
            }

    # 方式2b: 进程已结束、无 session 文件，但有 workdir —— 先尝试精确 sessionId 匹配
    effective_cwd = (session_info or {}).get("cwd") or hint_workdir
    if effective_cwd and hint_session_id:
        sanitized = _sanitize_path_for_claude(effective_cwd)
        project_dir = os.path.expanduser(f"~/.claude/projects/{sanitized}")
        exact_jsonl = os.path.join(project_dir, f"{hint_session_id}.jsonl")
        if os.path.exists(exact_jsonl):
            msgs = _parse_jsonl_messages(exact_jsonl)
            if msgs:
                plain = _messages_to_plain_text(msgs)
                return {
                    "pid": pid,
                    "messages": msgs,
                    "logs": plain or "",
                    "available": True,
                    "source": f"Claude 会话记录 ({hint_session_id[:8]}…)",
                    "session_info": {
                        "cwd": effective_cwd,
                        "sessionId": hint_session_id,
                    },
                }

    # 方式2c: 无精确 sessionId，降级为扫目录最新文件（可能不精确）
    if effective_cwd:
        msgs, sid = _find_recent_claude_logs_by_cwd(effective_cwd)
        if msgs:
            plain = _messages_to_plain_text(msgs)
            return {
                "pid": pid,
                "messages": msgs,
                "logs": plain or "",
                "available": True,
                "source": f"Claude 历史记录 ({sid[:8] if sid else '?'}…)",
                "session_info": {"cwd": effective_cwd, "sessionId": sid or ""},
            }

    # 方式3: 无法读取
    hint = "进程输出流为 tty/pipe，无法直接读取。"
    if session_info:
        hint += f"\n会话目录: {session_info.get('cwd', '')}\n会话 ID: {session_info.get('sessionId', '')}"
    elif hint_workdir:
        hint += f"\n工作目录: {hint_workdir}\n未找到 ~/.claude/projects/ 下的对话记录"
    else:
        hint += f"\n未找到 ~/.claude/sessions/{pid}.json"
    return {
        "pid": pid,
        "messages": [],
        "logs": "",
        "available": False,
        "error": "no_accessible_fd",
        "hint": hint,
        "session_info": session_info,
    }


# ─── 系统信息 ──────────────────────────────────────────────────────────────────


def get_system_info():
    """从 /proc 读取 CPU、内存、磁盘使用率（Linux）。"""
    info = {}

    # CPU 使用率：由后台线程每秒更新，直接读取缓存值（避免阻塞 HTTP 线程 100ms）
    with _cpu_percent_lock:
        info["cpu_percent"] = _cpu_percent_cached

    # 内存
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, val = line.split(":")
                mem[key.strip()] = int(val.strip().split()[0])  # kB
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", 0)
        used = total - available
        info["mem_total_mb"] = round(total / 1024, 1)
        info["mem_used_mb"] = round(used / 1024, 1)
        info["mem_percent"] = round(used / max(total, 1) * 100, 1)
    except Exception:
        info["mem_total_mb"] = info["mem_used_mb"] = info["mem_percent"] = None

    # 磁盘（根分区）
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        info["disk_total_gb"] = round(total / 1024**3, 1)
        info["disk_used_gb"] = round(used / 1024**3, 1)
        info["disk_percent"] = round(used / max(total, 1) * 100, 1)
    except Exception:
        info["disk_total_gb"] = info["disk_used_gb"] = info["disk_percent"] = None

    return info


# ─── HTTP 处理器 ───────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 静默日志，减少终端噪音
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "http://localhost:9090")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://localhost:9090")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path in ("", "/"):
            self.serve_dashboard()

        elif path == "/api/sessions":
            merged = merge_with_history(_get_agent_processes_cached())
            self.send_json(filter_child_processes(merged))

        elif path.startswith("/api/sessions/") and path.endswith("/logs"):
            pid = path.split("/")[3]
            if re.match(r"^\d+$", pid):
                # 优先返回缓存快照（进程已结束时）
                cached = get_cached_logs(pid)
                if cached:
                    self.send_json(cached)
                else:
                    # 从历史记录中取 workdir / sessionId，用于已结束进程的日志回溯
                    with _session_history_lock:
                        hist = _session_history.get(pid, {})
                        hint_workdir = hist.get("workdir")
                        hint_session_id = hist.get("sessionId", "")
                    self.send_json(
                        get_proc_logs(
                            pid,
                            hint_workdir=hint_workdir,
                            hint_session_id=hint_session_id,
                        )
                    )
            else:
                self.send_json({"error": "invalid_pid"}, 400)

        elif path.startswith("/api/sessions/") and path.endswith("/children"):
            pid = path.split("/")[3]
            if re.match(r"^\d+$", pid):
                cached = get_cached_children(pid)
                if cached is not None:
                    self.send_json({"children": cached})
                else:
                    self.send_json(get_child_processes(pid))
            else:
                self.send_json({"error": "invalid_pid"}, 400)

        elif path == "/api/system":
            self.send_json(get_system_info())

        elif path == "/api/session-files":
            self.handle_session_files()

        else:
            self.send_json({"error": "not_found"}, 404)

    def handle_session_files(self):
        """列出所有持久化会话缓存文件的元数据。"""
        files = []
        if os.path.isdir(SESSION_CACHE_DIR):
            for name in sorted(os.listdir(SESSION_CACHE_DIR), reverse=True):
                if not name.endswith(".json"):
                    continue
                filepath = os.path.join(SESSION_CACHE_DIR, name)
                try:
                    with open(filepath, encoding="utf-8") as f:
                        data = json.load(f)
                    meta = data.get("_session_meta", {})
                    session_info = data.get("session_info") or {}
                    files.append(
                        {
                            "file": name,
                            "pid": data.get("_pid", ""),
                            "sessionId": session_info.get("sessionId", ""),
                            "cachedAt": data.get("_cached_at", 0),
                            "command": meta.get("command", ""),
                            "workdir": meta.get("workdir", ""),
                            "startedAt": meta.get("startedAt", ""),
                            "endedAt": meta.get("endedAt", ""),
                            "duration": meta.get("duration", 0),
                            "source": data.get("source", ""),
                            "available": data.get("available", False),
                        }
                    )
                except Exception:
                    pass
        self.send_json(files)

    def serve_dashboard(self):
        dashboard_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "assets", "dashboard.html"
        )
        if not os.path.exists(dashboard_path):
            self.send_json({"error": "dashboard_not_found"}, 404)
            return
        with open(dashboard_path, "r", encoding="utf-8") as f:
            content = f.read()
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(body)


# ─── CPU 后台采样线程 ─────────────────────────────────────────────────────────


def _cpu_monitor_loop():
    """后台线程：每秒采样一次 /proc/stat 计算 CPU 使用率，写入 _cpu_percent_cached。"""
    global _cpu_percent_cached

    def read_cpu_times():
        with open("/proc/stat") as f:
            line = f.readline()
        vals = list(map(int, line.split()[1:]))
        return vals[3], sum(vals)  # idle, total

    while True:
        try:
            idle1, total1 = read_cpu_times()
            time.sleep(1.0)
            idle2, total2 = read_cpu_times()
            delta_idle = idle2 - idle1
            delta_total = total2 - total1
            cpu = round((1 - delta_idle / max(delta_total, 1)) * 100, 1)
            with _cpu_percent_lock:
                _cpu_percent_cached = cpu
        except Exception:
            time.sleep(1.0)


# ─── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = 9090
    _load_session_history_from_files()
    # 启动 CPU 后台采样线程
    cpu_thread = threading.Thread(target=_cpu_monitor_loop, daemon=True)
    cpu_thread.start()
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Agent Monitor API 已启动: http://localhost:{port}")
    print("端点: /api/sessions  /api/sessions/<pid>/logs  /api/system")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
