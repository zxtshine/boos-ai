# CLAUDE.md — lakejobai-job-radar

> AI 驱动的 BOSS 直聘智能求职助手 · Web 控制台 + CLI + ReAct Agent + 面试练习

## 项目概述

BOSS 直聘（zhipin.com）自动化求职工具，核心能力：
- **搜索**：60+ 城市、福利关键词、薪资/经验/学历/规模/融资阶段多维筛选，支持多区+多规模
- **批量投递**：翻页扫描 + 公司去重 + HR 活跃度过滤 + 法人识别优先排序 + 一键批量投递
- **AI 接管聊天**：自动回复 HR（人格化）+ 自动交换微信/简历/电话 + 转人工检测
- **AI 智能体**：ReAct Agent 自主执行求职任务（搜索→分析→投递→沟通全流程）
- **AI 分析**：岗位匹配度分析、简历优化（24h 缓存）、沟通建议（24h 缓存）
- **面试练习**：独立 FastAPI 服务，Ollama 出题 + DeepSeek 批改 + 语义检索问答 + 薄弱点分析
- **Web 控制台**：单文件 SPA 深色 UI，微信风格聊天界面，投递漏斗可视化
- **CLI**：18 条命令，stdout JSON 信封，专为 AI Agent 集成设计

## 技术栈

| 层级 | 选型 |
|------|------|
| 后端 | Python ≥ 3.10 + FastAPI + Uvicorn |
| 浏览器自动化 | Playwright + Firefox 持久化 Profile + 注入 JS 反检测（`add_init_script` + `window.__bossApply`） |
| 数据库（主应用） | SQLite (WAL 模式)，文件 `.boss_profile/boss_state.db` |
| 数据库（面试模块） | MySQL 8.0 (`ai_jobs_db`)，配置在 `interview/mysql_config.py` |
| 前端 | 单文件 HTML + Vanilla JS + WebSocket（无构建步骤、无外部 CDN） |
| CLI | Click + httpx + JSON 信封 |
| AI（主应用） | OpenAI Chat Completions 兼容 API（DeepSeek / OpenRouter / 自定义） |
| AI（面试出题） | Ollama 本地模型（qwen2.5:14b + nomic-embed-text） |
| AI（面试批改） | DeepSeek API（兼容 OpenAI 协议） |
| 包管理 | hatchling + pyproject.toml |

## 项目结构

```
boss_app.py              # FastAPI 后端 —— REST API + WebSocket + 后台监控 + Agent + 面试端点
boss_firefox.py           # BOSS 直聘搜索 + 详情 XHR + ANTI_DETECT JS 注入 + window.__bossApply 原生投递（Playwright 基类 BossScraper）
boss_automation.py        # 自动化投递（hybrid: JS原生点击+Playwright兜底）+ 聊天 + 发简历/微信 + 风控冷却退避 + 人类行为模拟
boss_replier.py            # AI 回复生成 + 打招呼语 + 简历优化上下文（调用 interview/llm_client）
boss_state.py              # SQLite 数据持久化层（7 张表 + 线程本地连接）
boss_company.py            # 公司画像聚合（岗位聚合 + HR 聚合 + 法人识别 + smart-send）
boss_geo.py                # 城市/区/规模 BOSS 编码映射（惰性获取 + 6h 缓存 + 静态回退）
boss_rag.py                # 历史 JD RAG 检索（余弦相似度 + embedding 缓存 + few-shot 上下文构建）
scraper.py                 # 智联招聘独立爬虫（非主流程，旧版工具）

agent/                     # ReAct AI Agent 子包
├── __init__.py            # 公开导出
├── prompts.py             # ReAct 系统提示模板（THOUGHT/ACTION/FINAL 格式）
├── tools.py               # 14 个原子工具 + 3 个复合技能 + ToolRegistry + ToolContext
├── skills.py              # 3 个复合技能（smart_scan / prepare_application / smart_apply）
└── loop.py                # AgentLoop 主循环（最多 12 步，支持 MORE_STEPS 扩展）

lakejob_cli/               # CLI 子包
├── cli.py                 # 18 条 Click 命令定义
├── client.py              # HTTP 客户端（调用 FastAPI）
├── output.py              # JSON 信封输出工具
└── schema.json            # AI Agent 工具描述

static/
└── dashboard.html         # Web 前端单文件 SPA（~150KB，深色科技风）

interview/                 # 面试问答子模块（独立 FastAPI 服务，端口 8001）
├── main.py                # FastAPI 服务入口（面试 + 学习模式 + 管理端点）
├── engine.py              # 面试引擎（轮次管理 + 出题 + 批改 + 总结）
├── fast_qa.py             # 快速问答（话题分类 + 域内语义检索 + DeepSeek 兜底）
├── db.py                  # MySQL 数据库层（语义搜索 + embedding + 面试记录）
├── llm_client.py          # LLM 调用客户端（Ollama embedding/chat + DeepSeek API）
├── mysql_config.py        # MySQL 连接配置
├── seed_data.py           # 种子数据（内置 QA 对）
├── batch_seed.py          # 批量导入种子数据（DeepSeek 生成 100+ QA 对）
├── benchmark.py           # 性能基准测试脚本
├── benchmark_rag.py       # RAG 检索链路评测（召回率/MRR/时延 + 多策略对比 + 消融实验）
├── requirements.txt       # 面试模块 Python 依赖
├── start.sh               # 面试服务启动脚本
└── static/
    └── index.html         # 面试练习 SPA 前端（深色主题聊天 UI）

tests/
├── test_boss_state.py     # 数据层测试（公司去重、薪资过滤）
└── test_smart_send.py     # 智能投递测试（HR 排序、法人检测、风控退避、问候回退）

.github/workflows/
└── ci.yml                 # GitHub Actions CI（Python 3.10 + pip install）

config.yaml                # scraper.py 通用爬虫配置（非主流程）
pyproject.toml             # 项目元数据 + CLI 入口 lakejob = lakejob_cli.cli:main
requirements.txt           # 主应用 Python 依赖
setup.sh                   # 一键环境安装脚本（pip + Playwright 浏览器）
.pre-commit-config.yaml    # pre-commit 钩子（ruff lint/format + yaml/toml/json 检查 + 防密钥泄露）
.gitignore
.gitattributes
```

## 架构与数据流

```
浏览器 (dashboard.html)
  │  WebSocket (/ws) + HTTP REST
  ▼
FastAPI (boss_app.py)  ←──HTTP───  lakejob CLI
  │  Python 函数调用
  ├── boss_automation.py  ──Playwright/Firefox──►  zhipin.com
  │   │                    ──page.evaluate()────►  window.__bossApply (注入JS原生操作)
  ├── boss_replier.py     ──HTTP────────────────►  AI API (DeepSeek/OpenRouter/...)
  ├── boss_rag.py         ──embedding────────────►  AI API + SQLite
  ├── boss_state.py       ──sqlite3─────────────►  .boss_profile/boss_state.db
  ├── boss_company.py     ──聚合查询─────────────►  内存 + DB
  ├── agent/tools.py      ──工具注册/分派────────►  BossAutomation + AI API
  ├── agent/skills.py     ──复合技能────────────►  编排多个工具一步完成
  └── agent/loop.py       ──ReAct 循环──────────►  LLM + 工具执行
        ▲
        │  HTTP (httpx)
lakejob CLI (lakejob_cli/)

面试服务（独立进程，端口 8001）
  interview/main.py (FastAPI)
    ├── engine.py   ──Ollama (qwen2.5:14b)──────►  出题
    │               ──DeepSeek API───────────────►  批改
    ├── fast_qa.py  ──Ollama (nomic-embed-text)──►  语义检索
    │               ──DeepSeek API───────────────►  兜底回答
    └── db.py       ──MySQL──────────────────────►  ai_jobs_db
```

## 数据库

### 主应用（SQLite WAL）

7 张表，启动时自动建表 + 兼容迁移（ALTER TABLE）：

| 表 | 用途 |
|----|------|
| `applications` | 投递记录（含 HR 活跃度、公司信息、法人、AI 优化结果缓存、JD embedding） |
| `conversations` | HR 会话（含微信交换、兴趣度） |
| `messages` | 聊天消息 |
| `companies` | 公司信息缓存（24h 复用） |
| `shortlists` | 候选池 |
| `daily_stats` | 每日统计 |
| `settings` | KV 配置（含 AI Key 等敏感信息） |

数据文件：`.boss_profile/boss_state.db`（已在 .gitignore 中）

### 面试模块（MySQL）

| 表 | 用途 |
|----|------|
| `interview_qa_pairs` | 面试问答对（含 JSON embedding 向量） |
| `interview_records` | 面试记录（会话 ID、题目、回答、评分） |
| `job_requirements` | 岗位 JD（语义匹配用） |

## FastAPI 端点总览

主服务（boss_app.py，默认端口 8010）：

| 分类 | 端点 | 说明 |
|------|------|------|
| 系统 | `POST /api/system/start\|stop\|relogin\|heartbeat\|navigate-chat` | 浏览器生命周期 |
| 监控 | `POST /api/monitor/pause\|resume` | 自动回复控制 |
| 诊断 | `GET /api/health\|status\|stats\|doctor` | 健康检查、漏斗统计 |
| 搜索 | `POST /api/jobs/search`, `GET /api/jobs`, `GET /api/jobs/{id}` | 岗位搜索与列表 |
| 投递 | `POST /api/jobs/apply\|apply-batch\|scan\|scan-and-apply` | 单投/批量/扫描 |
| AI | `POST /api/jobs/analyze\|optimize-resume\|chat-suggestion` | JD分析/简历优化/沟通建议 |
| 公司 | `GET /api/companies/preview`, `POST /api/companies/smart-send` | 公司画像/智能投递 |
| 聊天 | `GET /api/conversations`, `POST /api/conversations/{id}/send\|sync\|open` | 会话管理 |
| 地理 | `GET /api/geo/cities\|districts\|areas` | 城市/区 BOSS 编码 |
| 面试 | `POST /api/interview/start\|answer\|skip\|end` | 内嵌面试端点 |
| Agent | `GET /api/agent/tools`, `POST /api/agent/run` | ReAct Agent |
| 设置 | `GET\|PUT /api/settings` | KV 配置读写 |
| WebSocket | `WS /ws` | 实时截图 + 监控事件推送 |

面试服务（interview/main.py，端口 8001）：

| 分类 | 端点 | 说明 |
|------|------|------|
| 面试 | `POST /api/interview/start\|next\|answer\|end` | 完整面试流程 |
| 学习 | `POST /api/learn/ask` | 快速问答（4 层检索） |
| 管理 | `POST /api/admin/refresh-embeddings` | 重建 QA 向量 |
| 历史 | `GET /api/history/sessions\|session/{id}\|weak-areas` | 面试历史回顾 |

## Agent 工具清单（17 个：14 原子 + 3 复合技能）

| 工具 | 功能 |
|------|------|
| `search_jobs` | BOSS 搜索岗位 |
| `list_jobs` | 列出数据库中的岗位 |
| `get_job_detail` | 获取岗位详情 |
| `analyze_jd` | AI 分析 JD 匹配度 |
| `optimize_resume_for_jd` | AI 简历优化 |
| `get_chat_suggestion` | AI 沟通建议 |
| `apply_job` | 投递单个岗位 |
| `batch_apply` | 批量投递 |
| `list_conversations` | HR 会话列表 |
| `get_chat_messages` | 聊天消息 |
| `generate_reply` | AI 生成回复 |
| `get_status` | 系统状态 |
| `get_stats` | 投递统计 |
| `preview_companies` | 公司画像预览 |
| **⚡ `smart_scan`** | 搜索+批量AI分析，一步替代 search→detail×N→analyze×N |
| **⚡ `prepare_application`** | 详情+匹配+简历优化+沟通策略，一步替代4轮工具调用 |
| **⚡ `smart_apply`** | 分析+匹配门槛+自动投递，达标即投不达标跳过 |

## 关键设计决策

### 风控绕开（双层策略）

**第一层 — 数据采集（XHR 代理）**：使用 `page.evaluate(fetch)` 在浏览器内发起 XHR 请求，自动携带 cookie 和 referer，模拟真实浏览器行为。用于搜索列表和岗位详情的数据抓取。

**第二层 — 投递操作（注入 JS 原生操作）**：投递简历等高风险点击/输入操作绕过 Playwright CDP 协议，改用 `add_init_script` 注入 `window.__bossApply(greeting)` 函数（~207 行 JS），通过 `page.evaluate()` 调用。JS 内部使用原生 DOM API 完成：查找按钮 → `dispatchEvent(MouseEvent)` 点击 → 轮询等待聊天弹窗 → `textContent` + `InputEvent` 逐字输入招呼语 → 点击发送。避免了 Playwright `locator.click()` 被 BOSS 通过 CDP runtime 检测到的问题。

两种策略互补：普通页面导航和 DOM 抓取仍用 Playwright（稳定可靠），仅在高风险的"点击立即沟通 + 输入招呼语"环节切换到注入 JS（安全隐蔽）。

### 公司去重
投递前检查是否有中缀/后缀变体的重复公司名（如 "字节跳动" vs "字节跳动科技"），避免同一公司重复投递。基于 `_normalize_company_name` 进行模糊匹配。

### HR 活跃度
搜索时抓取 HR 最近活跃时间，超过阈值的自动跳过，避免浪费每日投递配额。法人（Boss 直聘身份）优先于普通 HR 排序。

### 风控退避（指数退避）
触发风控时使用指数退避（`_trigger_cooldown`），冷却时间随连续触发次数递增：
- 按类别设定基础冷却：rate_limit=120s, captcha=600s, banned=3600s
- 连续触发时翻倍：`seconds = min(base * 2^(strikes-1), 7200)`，最多 2 小时
- 投递成功或冷却期结束自动清零 `_risk_strikes`
- 冷却期间心跳和保活照常执行，仅跳过高风险操作（发消息、浏览对话）

### AI 缓存
简历优化和沟通建议两个 AI 端点带 24h 持久化缓存（存 SQLite），相同 JD 不重复消耗 token。

### 历史 JD RAG 检索（boss_rag.py）
投递新岗位时，通过 embedding 余弦相似度检索历史相似 JD，复用之前的优化建议和 HR 反馈信号。相似度阈值 0.55，取 top-5 构建 few-shot 上下文，辅助 LLM 生成更精准的简历优化建议、招呼语和沟通策略。支持三种上下文模式：`optimize`（引用历史优化建议）、`greeting`（引用历史招呼语 + HR 反馈信号）、`chat`（引用历史沟通建议）。

### CLI JSON 信封
所有 CLI 命令 stdout 输出统一 JSON（`{ok, command, data, pagination?, error}`），stderr 输出日志，exit 0=成功 1=失败。专为 AI Agent 子进程调用设计。

### ReAct Agent 循环
Agent 使用 THOUGHT → ACTION → OBSERVATION → ... → FINAL 格式，最多 12 步自动循环。支持连续 3 次失败自动中断，结束后可 `MORE_STEPS` 继续执行。

### 监控循环与 Agent 互斥
监控循环（`chat_monitor_loop`）和 Agent 端点（`/api/agent/run`）共享 `asyncio.Lock`（`browser_sync_lock`），同一事件循环内互斥执行。Agent 持有锁期间监控循环阻塞，反之亦然。监控循环在风控冷却期间心跳和保活照常执行（防止 session 超时），仅跳过 `run_chat_monitor_cycle` 高风险操作。监控日志完整记录每轮耗时、扫描会话数、新消息数、回复数。

### 人类行为模拟
`BossAutomation` 提供两个辅助方法降低风控风险：
- `_human_pace(min_gap, max_gap)` — 两次高风险操作间强制随机间隔（投递 10-25s，回复 5-15s）
- `_human_scroll()` — 模拟人类浏览（随机滚动 1-3 次 + 阅读停留 0.8-2.5s），投递前滚动阅读 JD 后再操作，更符合真人行为模式。滚动后自动 `scrollTo(0,0)` 回顶部，避免 Playwright auto-scroll 触发 BOSS header 动画导致 click 超时。

### 投递混合架构（Hybrid Playwright + Injected JS）
`apply_to_job` 方法采用两阶段策略：
1. **页面导航和阅读**：Playwright 导航到详情页 → `_human_scroll()` 模拟阅读 → `scrollTo(0,0)` 复位 → 安全检查和已投递检查
2. **点击和输入**：调用 `page.evaluate("window.__bossApply(greeting)")` → 注入 JS 完成原生 DOM 点击和逐字输入 → 如果 JS 返回 `fallback_chat_page`，Playwright 导航到聊天页兜底发送

### 面试多层检索
快速问答使用 4 层回退策略：L0 缓存 → L0.5 话题分类 → L1 精确匹配 → L2 语义检索（embedding）→ L3 预置回答 → L4 DeepSeek 兜底。

## 常用命令

```bash
# 启动主服务
python boss_app.py --port 8010
# 或
lakejob server --start --port 8010

# 启动面试服务
cd interview && bash start.sh
# 或
cd interview && uvicorn main:app --host 0.0.0.0 --port 8001

# 安装（含 CLI）
pip install -e .
playwright install firefox

# CLI 核心流程
lakejob doctor                          # 环境诊断
lakejob search "AI Agent" --city 广州    # 搜索岗位
lakejob scan-apply --max-pages 5        # 翻5页扫描投递
lakejob apply-batch                     # 批量投递待投递
lakejob conversations                   # HR 会话列表
lakejob stats                           # 投递漏斗
lakejob schema                          # 输出工具描述 JSON
lakejob smart-send                      # 智能投递（法人优先排序）

# 面试种子数据
cd interview && python batch_seed.py    # 批量生成 100+ QA 对

# 测试
pytest tests/ -v
```

## 配置

- **浏览器**：`config.yaml` — headless / profile_dir（boss_firefox.py 使用）
- **AI 平台**：Web 设置页 — API Key、Base URL、Model（存 SQLite settings 表）
- **面试 AI**：`interview/llm_client.py` — Ollama 地址 `localhost:11434`，模型 qwen2.5:14b / nomic-embed-text
- **面试数据库**：`interview/mysql_config.py` — MySQL 连接参数
- **投递参数**：Web 设置页 — 招呼语模板/模式、每日上限、回复间隔、HR 不活跃阈值、公司去重开关
- **环境变量**：`LAKEJOB_API` — CLI 连接的后端地址（默认 `http://127.0.0.1:8010`）
- **运行时数据**：`.boss_profile/` — SQLite 数据库 + Firefox Profile（gitignored）

## 合规边界

- 仅用于个人账号求职辅助
- 每日投递有上限（默认 15 条）
- 风控触发时自动冷却退避
- 不得批量注册、商业采集、规避风控