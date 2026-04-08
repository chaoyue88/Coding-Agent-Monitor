#!/bin/bash
# agent-monitor 启停控制脚本
# Usage: monitor.sh start|stop|status|open

PID_FILE="/tmp/agent-monitor.pid"
PORT=9090
SERVER="/home/sam/.openclaw/workspace/skills/agent-monitor/scripts/server.py"

start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        echo "agent-monitor 已在运行 (PID: $(cat $PID_FILE))"
        return 0
    fi
    nohup python3 "$SERVER" > /tmp/agent-monitor.log 2>&1 &
    local new_pid=$!
    sleep 2
    # 先验证服务已就绪，再写 PID 文件，避免竞态导致 PID 文件指向死进程
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/ | grep -q "200"; then
        echo $new_pid > "$PID_FILE"
        echo "✅ agent-monitor 已启动 (PID: $new_pid)"
        echo "   访问: http://localhost:$PORT/"
    else
        echo "❌ 启动失败，查看日志: cat /tmp/agent-monitor.log"
        kill "$new_pid" 2>/dev/null || true
        return 1
    fi
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        # 尝试按端口杀
        local pid=$(lsof -ti:$PORT 2>/dev/null)
        if [ -n "$pid" ]; then
            kill $pid 2>/dev/null
            echo "✅ agent-monitor 已停止"
        else
            echo "agent-monitor 未在运行"
        fi
        return 0
    fi
    local pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        echo "✅ agent-monitor 已停止 (PID: $pid)"
    else
        echo "agent-monitor 未在运行（清理残留 PID 文件）"
    fi
    rm -f "$PID_FILE"
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        echo "✅ 运行中 (PID: $(cat $PID_FILE))"
        echo "   地址: http://localhost:$PORT/"
        return 0
    fi
    # 检查端口
    local pid=$(lsof -ti:$PORT 2>/dev/null)
    if [ -n "$pid" ]; then
        echo "✅ 运行中 (PID: $pid)"
        echo "   地址: http://localhost:$PORT/"
        return 0
    fi
    echo "❌ 未运行"
    return 1
}

case "$1" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    open)    start && xdg-open "http://localhost:$PORT/" 2>/dev/null || true ;;
    *)       echo "Usage: $0 {start|stop|restart|status|open}" ;;
esac
