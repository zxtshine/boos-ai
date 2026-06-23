# CLAUDE.md — agent/

> BOSS 直聘 Plan-then-Execute Agent 子包 — 工具注册表 + 四阶段主循环 + 复合技能 + 系统提示

## 概述

`agent/` 是 AI 求职 Agent 的核心编排层。它不直接操作浏览器或调用 AI API，而是通过
`ToolRegistry` 注册底层工具函数，由 `AgentLoop` 驱动四阶段流程
（规划→执行→重规划→总结），自主完成"搜索→分析→投递→沟通"全流程。

## 文件

| 文件 | 用途 |
|------|------|
| `__init__.py` | 公开导出 ToolRegistry, ToolContext, AgentLoop, AGENT_SYSTEM_PROMPT |
| `tools.py` | 14 个原子工具 + 3 个复合技能 + ToolRegistry + ToolContext + `summarize_result` 结果压缩 |
| `skills.py` | 3 个复合技能（smart_scan / prepare_application / smart_apply） |
| `loop.py` | AgentLoop 主循环（规划→执行→重规划→总结，最多 10 步，最多 2 次重规划） |
| `prompts.py` | 规划系统提示 + 重规划提示 + 总结提示（JSON 计划格式） |

## 架构

```
LLM (DeepSeek/OpenRouter)
    │  1 次规划调用 → JSON 执行计划
    │  异常时 ≤2 次重规划
    │  复杂任务 1 次总结
    ▼
AgentLoop (loop.py)
    │  Phase 1: _plan()     — LLM 生成 JSON 计划
    │  Phase 2: _execute()  — Python 执行器顺序调用工具（不经 LLM）
    │  Phase 3: _replan()   — 仅失败/空结果时回调 LLM
    │  Phase 4: _summarize()— 简单任务直接拼接，复杂任务 LLM 生成摘要
    ▼
ToolRegistry (tools.py)
    │  14 原子工具 + 3 复合技能
    ├── 搜索发现: search_jobs, list_jobs, get_job_detail
    ├── AI 分析:   analyze_jd, optimize_resume_for_jd, get_chat_suggestion
    ├── 投递执行: apply_job, batch_apply
    ├── 沟通管理: list_conversations, get_chat_messages, generate_reply
    ├── 统计状态: get_status, get_stats
    ├── 公司画像: preview_companies
    └── 复合技能: smart_scan, prepare_application, smart_apply
```

## 核心组件

### ToolContext（依赖注入）

单例模式，工具函数通过 `ToolContext.get()` 获取运行时依赖：

```python
from agent.tools import ToolContext

# 服务启动时初始化一次
ToolContext.init(automation=boss_automation, run_pw=_run_pw)

# 工具内部获取
ctx = ToolContext.get()
ctx.automation  # BossAutomation 实例
ctx.run_pw      # async fn → Playwright 线程
ctx.has_browser()  # 检查浏览器是否在线
```

### ToolRegistry（工具注册表）

管理所有可用工具，支持 text/OpenAI function schema 两种描述格式：

```python
registry = ToolRegistry()
register_all(registry)  # 一键注册全部 17 个工具

# 获取 LLM 可消费的描述
registry.get_text_description()     # Markdown 列表（规划阶段用）
registry.get_openai_schema()        # OpenAI function calling 格式

# 执行工具
result = await registry.execute("search_jobs", {"keyword": "AI", "city": "广州"})
```

TypeError 自动捕获：参数缺失/多余时返回 `{"error": "参数错误: ...", "expected": [...]}`，触发重规划。

### AgentLoop（四阶段主循环）

核心流程：

1. **Phase 1 — 规划（`_plan`，1 次 LLM 调用）**：将用户目标 + 求职者信息 + 可用工具列表发给 LLM → 输出 JSON 执行计划（analysis / plan / constraints）
2. **Phase 2 — 执行（`_execute_plan`，0 次 LLM 调用）**：按计划顺序调用 `registry.execute()` → 记录结果到 context → 支持 `$N` 引用前步结果 → 遵守 constraints（如"不投递"自动跳过 apply_job 等）
3. **Phase 3 — 重规划（`_replan`，仅异常时，最多 2 次）**：步骤失败或搜索返回空时，将已完成步骤 + 错误信息发给 LLM → 产出修正计划 → 继续执行
4. **Phase 4 — 总结（`_summarize`，0-1 次 LLM）**：≤3 步的简单任务直接拼接；复杂任务 1 次 LLM 生成 3-5 句中文摘要

```python
loop = AgentLoop(
    registry=registry,
    llm_chat=llm_chat_deepseek,
    goal="帮我在广州找3个Python后端实习岗位并投递",
    max_steps=10,           # 计划步数上限
    on_step=async_callback, # WebSocket 推送每步进展
    on_plan=async_callback, # WebSocket 推送完整计划（agent_plan 事件）
)
result = await loop.run()
# → {completion_status, steps, summary, detail, milestones, llm_calls}
```

**返回结构**：
```python
{
    "completion_status": "completed" | "partial" | "aborted",
    "steps": int,           # 实际执行步数
    "summary": str,         # 自然语言中文总结
    "detail": [...],        # 每步: {step, tool, args, result_preview, error?}
    "milestones": {...},    # 关键动作: searched / analyzed / applied 等
    "llm_calls": int,       # LLM 总调用次数
}
```

### 计划格式（LLM 输出的 JSON）

```json
{
  "analysis": "用户想在杭州找Python岗位，明确不投递，只需搜索和展示结果",
  "plan": [
    {"tool": "get_status", "args": {}, "reason": "检查浏览器是否就绪"},
    {"tool": "smart_scan", "args": {"keyword": "Python", "city": "杭州", "top_n": 5}, "reason": "搜索并批量分析TOP5"}
  ],
  "constraints": ["不投递简历"]
}
```

### 执行器关键机制

**$N 变量引用** — 跨步数据传递：
```json
{"tool": "apply_job", "args": {"job_url": "$2.analyzed[0].url"}}
```
执行时解析为 `context[2]["analyzed"][0]["url"]` 的实际值。支持 dict 索引和 list 索引。

**约束检查（`_violates_constraints`）** — 20 个中文关键词匹配：
- `"不投递"、"只搜不投"、"不要投递"、"禁止投递"、"仅搜索"、"暂时不投"` 等
- 触发时自动跳过 `apply_job` / `batch_apply` / `smart_apply`

**步间延迟（`_inter_step_delay`）** — 模拟人类节奏，降低风控检测：
- 正常步间：随机 2.0-6.0s
- 错误退避（`_error_backoff`）：第 1 次 5-10s，第 2 次 15-25s，第 3 次+ 30-45s

**风控冷却感知（`_check_automation_cooldown`）** — 执行前检查 `boss_automation` 的指数退避状态，冷却中跳过浏览器操作。

### 结果压缩（summarize_result）

将工具返回的完整 JSON 压缩为 LLM 友好的紧凑文本，节省 token：
- 岗位列表 → 一行一个（title | company | salary | city | url）
- 会话列表 → 一行一个（hr_name | company | status | last_message）
- 聊天记录 → `[sender] content`
- 公司画像 → 排名 + 岗位数 + HR 信息

## 复合技能（Skills）

3 个技能在 Python 内部顺序调用原子工具，不经 LLM 编排，与 Plan-then-Execute 模式天然契合：

| 技能 | 编排的工具 | 用途 |
|------|-----------|------|
| `smart_scan` | search_jobs → get_job_detail×N → analyze_jd×N | 搜索 + 批量分析，按匹配分排名 |
| `prepare_application` | get_job_detail → analyze_jd → optimize_resume (并行) + get_chat_suggestion (并行) | 投递前全套分析 |
| `smart_apply` | prepare_application → (达标) apply_job | 分析 + 门槛决策 + 自动投递 |

`prepare_application` 会将 `analyze_jd` 的产出（gaps/key_skills/match_points）传给下游工具，避免重复分析 JD。

## 工具分类总览

### 搜索发现（3 个）
- `search_jobs` — 浏览器 ✅ | AI ❌ — BOSS 搜索，写 DB
- `list_jobs` — 浏览器 ❌ | AI ❌ — 纯 DB 读取
- `get_job_detail` — 浏览器 ❌ | AI ❌ — DB 快照

### AI 分析决策（3 个）
- `analyze_jd` — 浏览器 ❌ | AI ✅ — 匹配分数 + 差距 + 建议
- `optimize_resume_for_jd` — 浏览器 ❌ | AI ✅ | 24h 缓存 — 简历修改建议
- `get_chat_suggestion` — 浏览器 ❌ | AI ✅ — 沟通话术 + 避雷

### 投递执行（2 个）
- `apply_job` — 浏览器 ✅ | AI ✅（招呼语）| 消耗配额 — 单岗投递
- `batch_apply` — 浏览器 ✅ | AI ❌（默认招呼语）| 消耗配额 — 批量投递

### 沟通管理（3 个）
- `list_conversations` — 纯 DB 读
- `get_chat_messages` — 纯 DB 读
- `generate_reply` — AI ✅ — 只生成不发送

### 统计状态（2 个）
- `get_status` — 浏览器状态 + 日配额
- `get_stats` — 转化漏斗

### 公司画像（1 个）
- `preview_companies` — 浏览器 ✅ — 按公司聚合排名

## Prompt 设计

### 规划提示（PLANNING_SYSTEM_PROMPT）
核心指令：
1. **技能优先**：能用 smart_scan 就不单独调 search_jobs + analyze_jd
2. **检查先行**：开始前先 get_status 检查浏览器状态和今日配额
3. **搜索→分析→投递**：遵循这个顺序，不要跳过分析直接投递
4. **分批投递**：一次不超过 5 个
5. **遵守约束**：用户说"不投递"就绝对不要规划 apply_job/batch_apply/smart_apply
6. **严禁编造参数**：禁止编造 URL/ID/公司名，正确做法是搜索→引用结果→投递
7. **数据依赖**：如果步骤 B 需要步骤 A 的返回值，步骤 A 必须在步骤 B 之前

### 重规划提示（REPLAN_PROMPT）
失败时传入已完成步骤 + 失败信息 → LLM 分析原因 → 产出修正计划（换工具/换参数/跳过）

### 总结提示（SUMMARY_PROMPT）
传入目标 + 执行日志 → 3-5 句中文汇报（完成了什么/没完成什么/建议下一步）

## 与主应用集成

在 `boss_app.py` 中：

```python
from agent import ToolRegistry, ToolContext, register_all, AgentLoop

# 初始化
ToolContext.init(automation=automation, run_pw=run_playwright)
registry = ToolRegistry()
register_all(registry)

# 运行 Agent（带 plan 回调广播 agent_plan WS 事件）
async def on_plan(plan):
    await broadcast_ws({"type": "agent_plan", "plan": plan})

loop = AgentLoop(registry, llm_chat, goal, on_step=ws_callback, on_plan=on_plan)
result = await loop.run()
```

Agent 端点：`GET /api/agent/tools`（工具列表）、`POST /api/agent/run`（执行任务）。

`auto_execute=false` 时只规划不执行（dry-run），用于调试计划质量。
