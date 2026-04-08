---
name: agent-monitor
description: 监控 AI 编码 agent 的实时状态和工作进度。当用户询问"进度怎么样"、"任务状态"、"agent 在干嘛"、"有几个任务在跑"、"帮我看看后台"等问题时触发。采集所有活跃 exec session 的 ID、命令、状态、运行时长和 git 变更统计，输出结构化报告。
---

# Agent Monitor

## 概述

监控 OpenClaw 平台上运行的 AI 编码 agent（Claude Code / Codex 等），实时采集 session 状态，
并以人类可读格式或可视化面板展示运行中/已完成/失败的任务进度。

## 触发场景

当用户说以下任意内容时使用本 skill：
- "进度怎么样" / "任务状态" / "agent 在干嘛"
- "有没有任务在跑" / "帮我看看后台"
- "现在有几个 agent" / "哪些任务完成了"
- "失败了吗" / "出错了吗"
- "看看日志" / "输出内容" / "agent 输出了什么"
- "查看执行过程" / "有什么输出" / "打印了什么"
- "日志在哪" / "能看到输出吗" / "实时日志"

**日志查看触发场景：**
- "看看日志" / "输出内容是什么" / "agent 输出了什么"
- "查看执行过程" / "有没有报错信息" / "打印了什么"
- "实时日志" / "跟踪输出" / "tail 日志"
- "启动监控" / "开监控" / "打开面板" / "start monitor"
- "停止监控" / "关监控" / "stop monitor"
- "监控状态" / "监控在跑吗" / "monitor status"

## 工作流程

### 1. 采集状态数据

运行 `scripts/collect_status.sh` 采集所有 session 信息：

```bash
bash scripts/collect_status.sh
```

输出 JSON 数组，每个元素包含：
- `id` — session 标识符
- `command` — 执行命令（完整字符串，不截断）
- `status` — `running` / `done` / `failed`
- `duration` — 运行秒数
- `workdir` — 工作目录路径
- `git` — `{ files, insertions, deletions }` git 变更统计

**数据来源优先级：**
1. `openclaw process list --format json`（有 openclaw CLI 时）
2. 扫描 `/proc` 查找编码 agent 进程（降级方案）

### 2. 格式化报告

将 JSON 通过管道传给 `scripts/format_report.sh`：

```bash
bash scripts/collect_status.sh | bash scripts/format_report.sh
```

报告按状态分组显示，底部附汇总统计。

### 3. 可视化面板

面板通过内置 API 服务动态获取真实数据，使用 `scripts/monitor.sh` 管理：

```bash
# 启动监控（含 API 服务 + 前端页面）
bash scripts/monitor.sh start
# 访问 http://localhost:9090/

# 停止监控
bash scripts/monitor.sh stop

# 重启
bash scripts/monitor.sh restart

# 查看状态
bash scripts/monitor.sh status
```

**触发词：** 用户说"启动监控"、"开监控"、"打开面板"时执行 start；
说"停止监控"、"关监控"时执行 stop；说"监控状态"时执行 status。

启动成功后告知用户访问地址：http://localhost:9090/

## 报告格式说明

```
▶ 运行中 (2)
  ● [session-abc]  时长: 12m30s
    命令: claude-code --task "实现用户登录模块" --workdir /proj...
    目录: /home/user/myproject
    变更: 3个文件  +120 -45

✔ 已完成 (1)
  ✓ [session-xyz]  耗时: 5m12s
    命令: codex "add unit tests for auth module"

✗ 失败 (0)

─────────────────────────────────────────────
汇总统计  总计: 3  运行中: 2  完成: 1  失败: 0
```

## 注意事项

- `collect_status.sh` 需要 `jq` 做完整 JSON 解析；无 jq 时自动降级为原始输出
- `format_report.sh` 支持 `--format terminal|markdown|json` 三种输出格式，默认自动检测终端环境
- `format_report.sh` 终端支持颜色时自动启用 ANSI 颜色
- 进程扫描仅识别包含 `claude`/`codex`/`aider`/`openclaw` 关键字的进程
- git 统计基于 `HEAD` 的 diff，仅在 workdir 为 git 仓库时有效

## 日志 API 数据格式

`GET /api/sessions/<pid>/logs` 返回 JSON：

```json
{
  "pid": "12345",
  "available": true,
  "source": "Claude 会话记录 (abc12345…)",
  "logs": "14:30:00 🤖 Hello world\n\n14:30:05 👤 Fix the bug",
  "messages": [
    {
      "role": "assistant",
      "timestamp": "2026-04-07T14:30:00",
      "text": "Hello world",
      "tools": [{"name": "Read", "input": {"path": "/foo"}}]
    },
    {
      "role": "user",
      "timestamp": "2026-04-07T14:30:05",
      "text": "Fix the bug"
    }
  ],
  "session_info": { ... }
}
```

- `messages` — 结构化消息数组，每条包含 `role`（assistant/user/log）、`timestamp`、`text`，可选 `tools`
- `logs` — 纯文本格式（向后兼容）
- 前端 Dashboard 的 MD 按钮可切换结构化渲染，支持代码块、列表、工具调用等富文本展示
