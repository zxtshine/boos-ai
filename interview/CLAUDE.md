# CLAUDE.md — interview/

> 面试练习子模块 — 独立 FastAPI 服务（端口 8001），Ollama 出题 + DeepSeek 批改 + 语义检索问答

## 概述

`interview/` 是一个**独立于主应用的 FastAPI 服务**，提供 AI 面试模拟和知识问答功能。
不依赖主应用的浏览器或 BOSS 直聘 API，可单独部署运行。

## 文件

| 文件 | 用途 |
|------|------|
| `main.py` | FastAPI 入口 — 面试/学习/历史/管理全部端点 |
| `engine.py` | 面试引擎 — 轮次管理 + Ollama 出题 + DeepSeek 批改 + 总结 |
| `fast_qa.py` | 快速问答 — 7 层检索策略（缓存→改写→分类→精确→融合→预置→DeepSeek 兜底） |
| `db.py` | MySQL 数据库层 — 语义搜索 + embedding CRUD + 面试记录 + 薄弱分析 |
| `llm_client.py` | LLM 客户端 — Ollama (embedding/chat) + DeepSeek API (chat)，配置从 SQLite 懒加载，默认打印完整 I/O 日志 |
| `mysql_config.py` | MySQL 连接配置（ai_jobs_db） |
| `seed_data.py` | 内置种子 QA 对（编程语言/数据库/系统设计等 8 个分类） |
| `batch_seed.py` | 批量导入 — DeepSeek 生成 100+ QA 对并写入 MySQL |
| `benchmark.py` | 简易性能基准 — 单轮问答时延 |
| `benchmark_rag.py` | RAG 检索链路评测 — 召回率/MRR/时延 + 多策略对比 + 消融实验 |
| `requirements.txt` | 面试模块 Python 依赖 |
| `start.sh` | 一键启动脚本 |
| `static/index.html` | 面试练习 SPA 前端（深色主题聊天 UI） |

## 架构

```
客户端 (static/index.html)
  │  HTTP REST
  ▼
FastAPI (main.py, 端口 8001)
  │
  ├── /api/interview/*  →  engine.py   ──Ollama (qwen2.5:14b)────►  出题
  │                        engine.py   ──DeepSeek API────────────►  批改
  │                        engine.py   ──MySQL───────────────────►  记录
  │
  ├── /api/learn/*      →  fast_qa.py  ──Ollama (nomic-embed-text)► 语义检索
  │                        fast_qa.py  ──DeepSeek API────────────►  兜底回答
  │                        fast_qa.py  ──MySQL───────────────────►  QA 库
  │
  ├── /api/review/*     →  db.py       ──MySQL───────────────────►  历史/薄弱
  │
  └── /api/admin/*      →  db.py       ──MySQL───────────────────►  embedding 重建
```

## 端点总览

### 面试模式（/api/interview/*）

| 端点 | 说明 |
|------|------|
| `POST /api/interview/start` | 开始新会话（job_focus 可选，use_local_model 开关） |
| `POST /api/interview/chat` | 对话式面试：发消息 → 面试官回复 |
| `POST /api/interview/end` | 结束面试 → 生成总结 + 薄弱分析写库 |

### 学习模式（/api/learn/*）

| 端点 | 说明 |
|------|------|
| `POST /api/learn/ask` | 快速问答（4 层检索，2-3 秒） |
| `GET /api/learn/search` | 联想搜索（全文优先 → 语义兜底） |
| `POST /api/learn/cache-clear` | 清空问答缓存 |

### 知识库（/api/qa/*）

| 端点 | 说明 |
|------|------|
| `GET /api/qa/search` | 语义搜索面试题（可选 category 筛选） |
| `POST /api/qa/add` | 添加 QA 对 |
| `GET /api/qa/categories` | 获取所有分类（QA + 岗位） |

### 岗位（/api/jobs/*）

| 端点 | 说明 |
|------|------|
| `GET /api/jobs/search` | 语义搜索岗位（MySQL embedding 匹配） |

### 历史回顾（/api/review/*）

| 端点 | 说明 |
|------|------|
| `GET /api/review/sessions` | 所有面试会话列表 |
| `GET /api/review/session/{id}` | 会话详细记录 |
| `GET /api/review/weak-areas` | 薄弱环节分析（按 topic 聚合低分项） |

### 管理（/api/admin/*）

| 端点 | 说明 |
|------|------|
| `POST /api/admin/refresh-embeddings` | 重建全部 QA embedding |

## 快速问答的 7 层检索策略

`fast_qa.py` 实现多级回退，兼顾速度与质量：

| 层级 | 策略 | 时延 | 说明 |
|------|------|------|------|
| **L0** | 内存缓存 | <1ms | 完全相同的问法直接返回 |
| **L0.5** | 查询改写 | ~1s | LLM 将口语化问题改写为检索关键词 |
| **L0.6** | 话题分类 | ~10ms | 关键词 + embedding 混合路由到 9 个领域 |
| **L1** | 域内精确匹配 | ~5ms | MySQL LIKE 全文匹配 |
| **L1+L2** | 多路并行召回 | ~100ms | 全文检索 ‖ 关键词 ‖ 语义 → RRF 融合排序 |
| **L3** | 预置回答 | ~1ms | 8 大分类的通用兜底模板 |
| **L4** | DeepSeek 兜底 | ~2s | 前几层都无结果时调用 AI 实时生成 |

结果包含 `layer` 字段标识命中的层级，`confidence` 标识可信度。

## 面试引擎（engine.py）

### 面试流程（对话式 chat 模式）

`/api/interview/chat` 端点支持自由对话：发送任意消息，引擎内部判断状态（自我介绍/技术问答/行为问题/反问环节），模拟真实面试官的渐进式对话。

1. **Start** → 生成会话 ID + job_context（有 job_focus 则去 MySQL 语义搜索相关岗位作上下文）
2. **对话** → 用户发送消息 → Ollama (qwen2.5:14b) 根据岗位背景 + 历史对话动态回应
3. **评估** → DeepSeek API 自动评估回答质量（正确性/完整性/表达）+ 改进建议
4. **End** → DeepSeek 生成总结 + 薄弱点分析写入 MySQL + 面试记录持久化（session 表，支持暂停/恢复）

## 数据库（MySQL 8.0, ai_jobs_db）

| 表 | 用途 |
|----|------|
| `interview_qa_pairs` | QA 对 + category + difficulty + skills + JSON embedding 向量 |
| `interview_records` | 面试记录（session_id, question, answer, score, feedback, topic） |
| `job_requirements` | 岗位 JD 描述（语义匹配面试岗位背景） |

## RAG 评测（benchmark_rag.py）

评测脚本支持多策略对比和消融实验：

```bash
python benchmark_rag.py                  # 100 条，原问题搜自己
python benchmark_rag.py --paraphrase     # 口语改写后搜（真实场景）
python benchmark_rag.py --all            # 全量 QA 对
python benchmark_rag.py --size 200       # 指定条数
```

评测指标：Recall@1/@3/@5, MRR, P50/P95 时延。输出表格 + V2 vs V3 提升百分比。

## LLM 客户端（llm_client.py）

提供统一接口，被主应用 `boss_app.py` 和面试模块共用：

| 函数 | 用途 |
|------|------|
| `get_embedding(text)` | Ollama nomic-embed-text → 768 维向量，主应用 RAG 也用它 |
| `llm_chat_ollama(messages)` | Ollama qwen2.5:14b chat |
| `llm_chat_deepseek(messages, temperature)` | DeepSeek API（OpenAI 兼容），每次调用时从 SQLite settings 懒加载 API 配置 |

所有 LLM 调用默认打印完整输入/输出日志（每条消息截断至 3000 字符），通过 `settings` 表中的 `debug_llm_context` 开关控制额外的调试上下文输出。

## 启动

```bash
cd interview
bash start.sh
# 或
uvicorn main:app --host 0.0.0.0 --port 8001
```

健康检查：`GET http://localhost:8001/api/health`
