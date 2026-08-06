# CLAUDE.md — lakejobai-job-radar

> AI 驱动的 BOSS 直聘智能求职助手 · Web 控制台 + CLI + Plan-then-Execute Agent + 面试练习

## 项目概述

BOSS 直聘（zhipin.com）自动化求职工具，核心能力：搜索 → 批量投递 → AI 自动回复 HR → AI 分析 JD/简历优化 → Plan-then-Execute Agent 自主执行求职任务。附带独立面试练习服务（Ollama 出题 + DeepSeek 批改）。

## 技术栈

| 层级 | 选型 |
|------|------|
| 后端 | Python ≥ 3.10 + FastAPI + Uvicorn |
| 浏览器自动化 | Playwright + Firefox 持久化 Profile + 注入 JS 反检测 |
| 数据库（主应用） | SQLite WAL，文件 `.boss_profile/boss_state.db`，启动自动建表 |
| 数据库（面试） | MySQL 8.0，配置在 `interview/mysql_config.py` |
| 前端 | 单文件 HTML + Vanilla JS + WebSocket（无构建步骤） |
| CLI | Click + httpx，stdout JSON 信封 |、
| AI（主应用） | OpenAI 兼容 API（DeepSeek / OpenRouter / 自定义） |
| AI（面试） | Ollama 本地模型 + DeepSeek API |

## 项目结构

```
boss_app.py              # FastAPI 后端 + WebSocket + 后台监控 + Agent + 面试端点
boss_firefox.py           # BOSS 搜索/详情 XHR + 反检测 JS 注入 (BossScraper 基类)
boss_automation.py        # 投递/聊天/发简历/风控退避/人类行为模拟/未读兜底
boss_replier.py            # AI 回复生成 + 打招呼语 + 转人工引导
boss_state.py              # SQLite 持久化层（8 张表 + 线程本地连接）
boss_company.py            # 公司画像聚合 + 法人识别 + smart-send
boss_geo.py                # 城市/区/规模编码映射（惰性获取 + 6h 缓存）
boss_rag.py                # 历史 JD RAG 检索（embedding 余弦相似度 + few-shot）
agent/                     # Plan-then-Execute Agent（tools/skills/loop）
lakejob_cli/               # CLI（20 条命令）
static/dashboard.html      # Web 前端 SPA
interview/                 # 面试子模块（独立 FastAPI，端口 8001，详见 interview/CLAUDE.md）
tests/                     # 测试
```

## 架构与数据流

```
浏览器 (dashboard.html)
  │  WebSocket + HTTP REST
  ▼
FastAPI (boss_app.py)  ←──HTTP───  lakejob CLI
  │
  ├── boss_automation.py  ──Playwright/Firefox──►  zhipin.com
  │   │                    ──page.evaluate()────►  window.__bossApply (注入JS原生操作)
  ├── boss_replier.py     ──HTTP────────────────►  AI API
  ├── boss_state.py       ──sqlite3─────────────►  .boss_profile/boss_state.db
  ├── boss_rag.py         ──embedding────────────►  AI API + SQLite
  └── agent/loop.py       ──规划→执行→重规划────►  LLM + 工具

面试服务（独立进程，端口 8001）
  interview/main.py ──Ollama + DeepSeek──► 出题/批改/语义检索
```

## 关键设计决策

### 风控绕开（双层策略）

BOSS 直聘会检测 Playwright CDP 协议。因此采用两层策略：
- **数据采集**：`page.evaluate(fetch)` 在浏览器内发起 XHR，自动携带 cookie/referer
- **投递操作**：`add_init_script` 注入 `window.__bossApply()`，用原生 DOM API（`dispatchEvent(MouseEvent)` + `InputEvent` 逐字输入）完成点击和输入，绕过 CDP 检测

普通导航仍用 Playwright，仅高风险操作切换注入 JS。

### 投递混合架构

`apply_to_job`：Playwright 导航 + 阅读 → `scrollTo(0,0)` 复位 → `page.evaluate("window.__bossApply(greeting)")` 原生点击 → 若返回 fallback 则 Playwright 兜底。

### 公司去重

投递前用 `_normalize_company_name` 模糊匹配中缀/后缀变体（"字节跳动" vs "字节跳动科技"），避免重复投递。

### HR 活跃度过滤

搜索时抓取 HR 最近活跃时间，超阈值自动跳过。法人（BOSS 直聘身份）优先于普通 HR 排序。

### 风控退避（指数退避）

`_trigger_cooldown`：rate_limit=120s, captcha=600s, banned=3600s。连续触发翻倍 `min(base*2^(n-1), 7200)`，最多 2h。冷却期间心跳照常，仅跳过高风险操作。

### AI 缓存

简历优化和沟通建议 24h SQLite 缓存，相同 JD 不重复消耗 token。

### 未读消息双重检测（DOM + DB 兜底）

DOM 扫描可能遗漏消息（页面渲染不完整、名称匹配失败）。`conversations` 表增加 `has_unreplied` 字段：入库时自动计算，我回复后自动清零。监控循环 DOM 扫描后查 DB 兜底，排除已处理的。

### Plan-then-Execute Agent

四阶段：规划（1 次 LLM 生成 JSON 计划）→ 执行（0 次 LLM，按序调工具，支持 `$N` 跨步引用）→ 重规划（异常时最多 2 次）→ 总结。最多 12 步，优先用复合技能。遇"不投递"等约束自动跳过投递工具。

### 监控循环与 Agent 互斥

监控循环和 Agent 共享 `asyncio.Lock`（`browser_sync_lock`），互斥执行。监控循环在风控冷却期间只做心跳保活，跳过高风险操作。单轮最多回复 3 条。

### CLI JSON 信封

所有 CLI stdout 输出 `{ok, command, data, error}` JSON，stderr 输出日志。专为 AI Agent 子进程调用设计。

## 常用命令

```bash
# 启动
python boss_app.py --port 8010                     # 主服务
cd interview && uvicorn main:app --port 8001        # 面试服务

# 安装
pip install -e . && playwright install firefox

# CLI
lakejob search "AI Agent" --city 广州   # 搜索
lakejob scan-apply                       # 扫描并投递
lakejob conversations                    # HR 会话列表
lakejob stats                            # 投递漏斗
lakejob doctor                           # 环境诊断

# 测试
pytest tests/ -v
```

## 配置

- **浏览器**：`config.yaml` — headless / profile_dir
- **AI Key**：Web 设置页填入（存 SQLite settings 表）
- **面试 AI**：`interview/llm_client.py` — Ollama 地址 + 模型名
- **面试数据库**：`interview/mysql_config.py` — MySQL 连接参数
- **环境变量**：`LAKEJOB_API` — CLI 后端地址（默认 `http://127.0.0.1:8010`）
- **运行时数据**：`.boss_profile/` — SQLite + Firefox Profile（gitignored）

## 合规

仅限个人求职辅助。每日投递上限（默认 15 条）。风控触发自动退避。不得批量注册、商业采集、规避风控。