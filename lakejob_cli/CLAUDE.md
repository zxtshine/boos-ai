# CLAUDE.md — lakejob_cli/

> BOSS 直聘 CLI 子包 — 18 条命令 + HTTP 客户端 + JSON 信封输出 + Agent 工具描述

## 概述

`lakejob_cli/` 是 BOSS 直聘求职助手的命令行前端。通过 `lakejob` 命令与 FastAPI 后端（`boss_app.py`）交互，
专为 AI Agent 集成和终端用户设计。所有 stdout 输出采用统一 JSON 信封格式，
stderr 输出日志，exit code 0=成功 1=失败。

## 文件

| 文件 | 用途 |
|------|------|
| `__init__.py` | 空（标记为 Python 包） |
| `cli.py` | 18 条 Click 命令定义（含 `_kill_boss_app` 进程管理） |
| `client.py` | httpx HTTP 客户端（调用 FastAPI 后端） |
| `output.py` | JSON 信封输出工具（ok / fail / emit） |
| `schema.json` | AI Agent 工具描述 JSON（供 LLM function calling 消费） |

## 安装

```bash
pip install -e .        # pyproject.toml 注册 entry point lakejob
```

配置后端地址：环境变量 `LAKEJOB_API`（默认 `http://127.0.0.1:8010`）。

## 命令清单（18 条）

### 系统管理

| 命令 | 说明 |
|------|------|
| `lakejob version` | 输出版本号 |
| `lakejob schema` | 输出 AI Agent 工具描述 JSON |
| `lakejob doctor` | 环境诊断（检查后端连通性、浏览器状态等） |
| `lakejob login` | 触发浏览器重新登录（扫码） |
| `lakejob server --start/--stop` | 启动/停止后台服务（精确杀 boss_app 进程） |
| `lakejob restart` | 杀旧进程 + 起新服务（Windows 用 wmic/psutil 精确杀） |

### 搜索与浏览

| 命令 | 说明 |
|------|------|
| `lakejob search <keyword>` | BOSS 搜索岗位（--city, --welfare, --count） |
| `lakejob jobs` | 列出 DB 中岗位（--status pending/applied/replied） |
| `lakejob scan` | 扫描当前 BOSS 搜索结果页 |
| `lakejob stats` | 投递转化漏斗统计 |
| `lakejob status` | 系统运行状态 |

### 投递

| 命令 | 说明 |
|------|------|
| `lakejob apply <job_url>` | 单岗投递（含 AI 招呼语） |
| `lakejob apply-batch` | 批量投递所有待投递岗位 |
| `lakejob scan-apply` | 当前页一键扫描并批量投递 |
| `lakejob smart-send` | 智能投递（法人优先排序 + 交互确认） |

### 沟通

| 命令 | 说明 |
|------|------|
| `lakejob conversations` | HR 会话列表 |
| `lakejob chat <conv_id>` | 查看聊天记录 |
| `lakejob send <conv_id> --msg "..."` | 手动发送消息 |

### AI 分析

| 命令 | 说明 |
|------|------|
| `lakejob analyze <job_url>` | AI 分析 JD 匹配度 |

### 候选池

| 命令 | 说明 |
|------|------|
| `lakejob shortlist list/add/remove` | 候选池管理 |

## JSON 信封格式

所有命令 stdout 输出统一结构：

```json
{
  "ok": true,
  "command": "search",
  "data": { ... },
  "pagination": { "count": 30, "page": 1 },
  "error": null
}
```

错误时：

```json
{
  "ok": false,
  "command": "search",
  "data": null,
  "error": "搜索失败: 浏览器未启动"
}
```

由 `output.py` 的两个工厂函数生成：
- `output.ok(command, data)` — 成功信封
- `output.fail(command, error_message)` — 失败信封
- `output.emit(envelope)` — 输出 JSON + exit
- `output.ok_or_fail(response, command)` — 根据 HTTP 响应自动判断

## HTTP 客户端（client.py）

封装 httpx 同步调用，连接 FastAPI 后端。关键方法：

```python
from lakejob_cli import client

client.search("Python", "广州", 60)         # POST /api/jobs/search
client.apply_one(job_url)                    # POST /api/jobs/apply
client.apply_batch(urls)                     # POST /api/jobs/apply-batch
client.status()                              # GET /api/status
client.stats()                               # GET /api/stats
client.doctor()                              # GET /api/doctor
client.company_preview(keyword, city, ...)   # POST /api/companies/preview
client.smart_send(...)                       # POST /api/companies/smart-send
```

返回 `httpx.Response`，由 `output.ok_or_fail` 统一处理。

## 进程管理（_kill_boss_app）

`cli.py` 内置精确进程杀死逻辑，避免误杀其他 Python 进程：

1. **优先 psutil**：遍历所有 python 进程，只杀 cmdline 包含 `boss_app.py` 的
2. **兜底 wmic**：旧 Windows 系统用 `wmic process where` 精确匹配
3. **保护自身**：跳过当前 CLI 进程的 PID

## schema.json

为 AI Agent（如 Claude、GPT）提供的工具描述文件，JSON 数组格式，
每条包含 `name` / `description` / `parameters`，可直接用于 OpenAI function calling。
通过 `lakejob schema` 命令输出。

## 设计原则

- **stdout 纯 JSON**：方便 AI Agent 和脚本解析
- **stderr 日志**：人类可读的日志不污染 stdout
- **exit code 明确**：0=成功，1=失败
- **同步 HTTP**：CLI 不需要异步，httpx 同步客户端够用
- **环境变量配置**：后端地址通过 `LAKEJOB_API` 环境变量覆盖
- **不依赖浏览器**：CLI 是瘦客户端，由后端的浏览器实例处理所有 zhipin.com 交互
