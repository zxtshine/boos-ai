"""
面试问答Agent - 面试引擎核心（V2 对话式）
对话式面试官：基于简历+岗位+知识库，实时追问、评价、引导
"""

import uuid
import json
import random
import re
import sys
import os
import time
from datetime import datetime
from typing import List, Dict, Any

_cur_dir = os.path.dirname(os.path.abspath(__file__))
if _cur_dir not in sys.path:
    sys.path.insert(0, _cur_dir)

from llm_client import llm_chat_ollama, llm_chat_deepseek, parse_json_from_llm

# MySQL 模块可能不可用，懒加载
_db_available = None


def _check_db():
    global _db_available
    if _db_available is None:
        try:
            from db import (semantic_search_qa, search_jobs_by_semantic,
                            save_interview_record, add_qa_pair, get_all_job_categories)
            _db_available = True
        except Exception as e:
            _db_available = False
            print(f"[{_ts()}] ⚠️ [DB] 知识库不可用: {e}")
    return _db_available


def _ts():
    """返回当前时间戳字符串 [HH:MM:SS]"""
    return datetime.now().strftime('%H:%M:%S')


# ── 对话式面试官 System Prompt ──
INTERVIEWER_SYSTEM_PROMPT = """你是一个资深的面试官，正在面试一位候选人。

## 面试背景
- 岗位方向：{job_focus}
- 候选人简历摘要：{resume}
- 匹配到的岗位参考：{job_context}

## ⚠️ 出题范围（重要）
你的出题以**岗位方向 + 候选人简历**为依据，围绕该岗位的核心能力领域展开。

- 仔细阅读候选人简历，提取其中提到的所有技能、项目、工作经验、专业领域
- 面试应全面覆盖简历和岗位JD中的主要能力方向。例如简历写了教学经验和课程设计，那教学方法和课程设计都要问到，不能只盯着一个点
- 围绕岗位方向可以适度发散：从简历出发，延伸到该岗位常见的相关话题，考察候选人的知识广度
- 知识库参考仅用于帮你**评价答案是否到位**，不是出题范围——知识库没覆盖的领域，你照样要问
- category 字段请根据该岗位的实际能力领域自由命名（如教学设计、课堂管理、机械制图、材料工艺、电商运营、供应链等），不要拘泥于固定分类

## 出题权重参考
以下是根据候选人历史面试记录分析出的弱项类别（分数越低越弱）。请优先从弱项类别出题：

{category_weights}

## 你的面试风格
1. 每次发言包含两个部分：**简短评价**（1-2句） + **一个追问**
2. 评价要具体：指出候选人哪里说得好、哪里没说清楚、遗漏了什么关键点
3. **交叉验证简历**：候选人的简历摘要已在背景中提供。评价和追问时要主动核验：
   - 候选人提到的项目/经验是否在简历中有对应——如果有，追问具体贡献和细节
   - 如果候选人声称的经验远超简历描述，温和地质疑
   - 如果简历写了某项能力但候选人回答含糊，追问落实
4. 追问要基于候选人的回答**深入挖掘**，不要突然换话题
5. 考察候选人的真实经验和专业能力，不要问纯理论
6. 面试节奏：5~8轮对话后自然收尾，给出整体评价和学习建议
7. 口语化、自然，用"你"称呼候选人

## 输出格式（严格JSON，只输出JSON，不要其他任何文字）
{{"message": "你的完整发言", "want_to_end": false, "score": 7.5, "category": "教学设计"}}

字段说明：
- message: 你的完整发言（评价 + 追问）
- want_to_end: 是否结束面试（true/false）
- score: 对候选人**本轮回答**的评分（0-10分，根据准确性、深度、实践经验来评）
- category: 你**刚刚问的这道题**属于哪个能力领域（根据岗位方向和简历内容来确定，如教学设计/机械制图/电商运营/数据库/前端框架等，用该岗位领域的自然术语命名即可）

当你想结束面试时设置 want_to_end=true，在 message 中包含总结评价和学习建议。"""

# RAG 注入模板 — 按需检索到的知识库参考，作为当轮 context 注入
RAG_CONTEXT_TEMPLATE = """
## 知识库参考（仅本轮可用，用于辅助评价——不是出题范围）
以下是从知识库中检索到的与当前话题相关的面试题和参考答案。请参考这些内容来**评价**候选人的回答是否到位、提出更深入的追问。但不要限制出题方向：

{rag_context}

注意：知识库未覆盖的技术领域同样重要，不要跳过。"""


class InterviewEngine:
    """对话式面试引擎"""

    # 面试题分类（仅作关键词映射参考，不预设权重——权重由历史面试记录动态决定）
    # 类别不限定技术方向，LLM 根据简历+JD 自由归类，此处仅提供常用映射
    CATEGORIES = {
        "编程语言": ["python", "java", "go", "rust", "c++", "c#", "typescript", "javascript",
                     "kotlin", "swift", "scala", "异步", "多线程", "JVM", "spring", "泛型", "内存管理"],
        "数据库": ["mysql", "redis", "postgresql", "mongodb", "elasticsearch", "oracle",
                    "sql", "索引", "事务", "分库分表", "NoSQL", "缓存"],
        "系统设计": ["架构", "分布式", "微服务", "消息队列", "设计模式",
                      "高并发", "高可用", "领域驱动", "CAP", "负载均衡"],
        "工程化": ["docker", "k8s", "部署", "api", "性能优化",
                   "缓存", "并发", "CI/CD", "监控", "日志", "测试"],
        "前端": ["react", "vue", "angular", "css", "html", "webpack", "vite",
                 "跨域", "响应式", "SEO", "SSR", "小程序"],
        "AI/ML": ["rag", "agent", "llm", "大模型", "微调", "fine-tuning", "prompt",
                  "transformer", "attention", "SFT", "RLHF", "embedding", "向量数据库",
                  "机器学习", "深度学习", "NLP", "CV", "reinforcement learning"],
        "数据处理": ["spark", "hadoop", "flink", "kafka", "etl", "数据仓库",
                     "OLAP", "数据湖", "pipeline", "airflow"],
        "安全": ["认证", "授权", "OAuth", "JWT", "SQL注入", "XSS", "CSRF",
                 "加密", "HTTPS", "零信任", "防火墙"],
    }

    def __init__(self, job_focus: str = "", use_local_model: bool = False, _restore: bool = False):
        self.session_id = uuid.uuid4().hex[:12]
        self.job_focus = job_focus
        self.use_local_model = use_local_model  # True=直接用Ollama本地模型, False=用平台配置的模型
        self.history: List[Dict[str, str]] = []  # [{"role":"assistant","content":...}, {"role":"user","content":...}]
        self.round_count = 0
        self.max_rounds = 10
        self.job_context = ""
        self.resume = ""
        self.kb_context = ""       # 初始化时检索的 KB 摘要，用于 start() 开场选题
        self._qa_cache = []        # 初始化时检索到的完整 QA 列表，供后续动态参考
        self._last_question = ""   # 上一轮提出的问题（用于入库记录）
        self._last_category = ""   # 上一轮问题的类别
        if not _restore:
            self._init_context()

    @classmethod
    def from_saved(cls, saved: dict, use_local_model: bool = False) -> "InterviewEngine":
        """从 SQLite 持久化记录恢复面试会话。"""
        import json as _json
        eng = cls(job_focus=saved.get("job_focus", ""),
                  use_local_model=use_local_model, _restore=True)
        eng.session_id = saved["session_id"]
        eng.job_context = saved.get("job_context", "")
        eng.resume = saved.get("resume", "")
        eng.round_count = saved.get("round_count", 0)
        eng.max_rounds = saved.get("max_rounds", 10)
        eng._last_question = saved.get("last_question", "")
        eng._last_category = saved.get("last_category", "")
        try:
            eng.history = _json.loads(saved.get("history_json", "[]"))
        except Exception:
            eng.history = []
        return eng

    def _persist_session(self):
        """将当前状态持久化到 SQLite（暂停后可以恢复）。"""
        import json as _json
        try:
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
            from boss_state import save_interview_session as _sess
            _sess(
                session_id=self.session_id,
                job_focus=self.job_focus or "",
                job_context=self.job_context,
                resume=self.resume,
                round_count=self.round_count,
                max_rounds=self.max_rounds,
                history_json=_json.dumps(self.history, ensure_ascii=False),
                last_question=self._last_question,
                last_category=self._last_category,
                status="active",
            )
            print(f"[{_ts()}] │ [持久化] ✅ 会话已保存到 SQLite (round={self.round_count})")
        except Exception as e:
            print(f"[{_ts()}] │ [持久化] ⚠️ 保存失败: {e}")

    # ══════════════════════════════════════
    #  上下文初始化（与 V1 相同）
    # ══════════════════════════════════════

    def _init_context(self):
        """初始化面试上下文：简历 + 匹配岗位 + 知识库检索"""
        print(f"[{_ts()}] ┌─ 开始初始化面试上下文 (job_focus=\"{self.job_focus or '通用'}\")")

        # Step 1/3: 加载简历
        print(f"[{_ts()}] │ [Step 1/3] 加载简历摘要...")
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from boss_state import get_setting
            self.resume = (get_setting("resume_summary", "") or "")[:1200]
            if self.resume.strip():
                print(f"[{_ts()}] │ [Step 1/3] ✅ 已加载简历摘要 ({len(self.resume)}字)")
            else:
                print(f"[{_ts()}] │ [Step 1/3] ⚠️ 简历摘要为空，将使用通用出题模式")
        except Exception as e:
            print(f"[{_ts()}] │ [Step 1/3] ⚠️ 加载简历失败: {e}")
            self.resume = ""

        # Step 2/3: 从 SQLite applications 表检索匹配岗位
        print(f"[{_ts()}] │ [Step 2/3] 检索匹配岗位...")
        if self.job_focus:
            try:
                import sys, os
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from boss_state import get_db
                t0 = time.time()
                db = get_db()
                query = f"%{self.job_focus}%"
                rows = db.execute(
                    "SELECT job_title, company, salary, experience, education, description "
                    "FROM applications "
                    "WHERE description IS NOT NULL AND description != '' "
                    "AND (job_title LIKE ? OR description LIKE ?) "
                    "ORDER BY id DESC LIMIT 5",
                    (query, query)
                ).fetchall()
                elapsed = time.time() - t0
                if rows:
                    print(f"[{_ts()}] │ [Step 2/3] ✅ 岗位匹配完成 (耗时: {elapsed*1000:.0f}ms, 匹配到 {len(rows)} 个岗位)")
                    job_lines = []
                    for r in rows:
                        desc = (r["description"] or "")[:800]
                        salary = r["salary"] or ""
                        exp = r["experience"] or ""
                        edu = r["education"] or ""
                        job_lines.append(
                            f"- {r['job_title']} @ {r['company']} ({salary})\n"
                            f"  经验:{exp} 学历:{edu}\n"
                            f"  JD: {desc}"
                        )
                    self.job_context = "\n".join(job_lines)
                else:
                    print(f"[{_ts()}] │ [Step 2/3] ⚠️ 未匹配到相关岗位 (耗时: {elapsed*1000:.0f}ms)")
            except Exception as e:
                print(f"[{_ts()}] │ [Step 2/3] ⚠️ 岗位匹配失败: {e}")
        else:
            print(f"[{_ts()}] │ [Step 2/3] ⏭️ 无岗位方向，跳过岗位匹配")

        # Step 3/3: 从知识库检索相关面试题
        print(f"[{_ts()}] │ [Step 3/3] 检索知识库相关面试题...")
        if _check_db():
            try:
                from db import semantic_search_qa
                query_parts = []
                if self.job_focus:
                    query_parts.append(self.job_focus)
                if self.resume:
                    query_parts.append(self.resume[:200])
                query = " ".join(query_parts).strip()
                if query:
                    t0 = time.time()
                    qa_results = semantic_search_qa(query, limit=20)
                    elapsed = time.time() - t0
                    if qa_results:
                        self._qa_cache = qa_results  # 缓存完整 20 条，供后续动态检索回退
                        # 随机抽选 8 条作为开场参考，避免每次开场题重复
                        sampled = random.sample(qa_results, min(8, len(qa_results)))
                        self.kb_context = "\n".join([
                            f"- [{r['category']}] {r['question']} (难度:{r['difficulty']}, 技能:{r.get('skills','')})"
                            for r in sampled
                        ])
                        print(f"[{_ts()}] │ [Step 3/3] ✅ 知识库检索完成 (耗时: {elapsed*1000:.0f}ms, 匹配 {len(qa_results)} 条, 随机抽 {len(sampled)} 条)")
                    else:
                        print(f"[{_ts()}] │ [Step 3/3] ⚠️ 知识库中未检索到相关题目 (耗时: {elapsed*1000:.0f}ms)")
                else:
                    print(f"[{_ts()}] │ [Step 3/3] ⏭️ 无检索关键词，跳过知识库检索")
            except Exception as e:
                print(f"[{_ts()}] │ [Step 3/3] ⚠️ 知识库检索失败: {e}")
        else:
            print(f"[{_ts()}] │ [Step 3/3] ⏭️ 知识库不可用，跳过检索")

        print(f"[{_ts()}] └─ 面试上下文初始化完成 (简历:{len(self.resume)}字, 岗位:{len(self.job_context)}字, 知识库:{len(self.kb_context)}字)")

    # ══════════════════════════════════════
    #  对话式面试核心方法
    # ══════════════════════════════════════

    def _get_dynamic_weights(self) -> dict | None:
        """从 MySQL 查询历史弱项，动态计算出题权重。

        返回 None 表示"历史记录不足，不设权重（各类别均匀出题）"。
        有足够记录时，弱项类别权重上浮，强项下压，归一化后返回 {category: weight}。
        """
        if not _check_db():
            return None
        try:
            from db import get_category_scores
            cat_scores = get_category_scores()
        except Exception:
            return None

        if not cat_scores:
            return None

        # 总答题数太少 → 不足以判断强弱项
        total_answers = sum(row["count"] for row in cat_scores)
        if total_answers < 5:
            return None

        # 各类别均等起步
        cats = list(self.CATEGORIES.keys())
        base = 1.0 / len(cats)

        # 平均分 → 调整系数
        adjustments = {}
        for row in cat_scores:
            cat = row["category"]
            avg = row["avg_score"]
            if cat not in self.CATEGORIES:
                continue
            if avg < 5.0:
                adjustments[cat] = 2.0    # 严重弱项，权重翻倍
            elif avg < 6.0:
                adjustments[cat] = 1.5    # 偏弱
            elif avg < 7.0:
                adjustments[cat] = 1.0    # 正常
            else:
                adjustments[cat] = 0.7    # 强项，压一下

        for cat in cats:
            if cat not in adjustments:
                adjustments[cat] = 1.0

        total = sum(base * adjustments[c] for c in cats)
        if total > 0:
            return {c: round(base * adjustments[c] / total, 3) for c in cats}
        return None

    def _build_system_prompt(self, rag_context: str = "") -> str:
        """构建系统提示词，注入动态权重和可选 RAG 上下文"""
        weights = self._get_dynamic_weights()
        if weights is None:
            # 历史记录不足，不设权重——各类别均匀出题
            weight_text = "（历史面试记录不足，暂不设类别权重。请根据简历和岗位方向全面覆盖各类技术，各类别均匀出题。）"
        else:
            weight_lines = []
            for cat, w in sorted(weights.items(), key=lambda x: -x[1]):
                pct = int(w * 100)
                bar = "█" * (pct // 5)
                weight_lines.append(f"  - {cat}: {pct}% {bar}")
            weight_text = "\n".join(weight_lines) if weight_lines else "（无历史数据，均匀出题）"

        jf = self.job_focus or ""
        if not jf and self.resume:
            jf = "请从候选人简历中提取关键技术方向作为面试范围"
        if not jf:
            jf = "通用技术"
        prompt = INTERVIEWER_SYSTEM_PROMPT.format(
            job_focus=jf,
            resume=self.resume or "（暂无简历信息）",
            job_context=self.job_context or "（暂无匹配岗位信息）",
            category_weights=weight_text,
        )
        if rag_context:
            prompt += RAG_CONTEXT_TEMPLATE.format(rag_context=rag_context)
        return prompt

    def _retrieve_relevant_qa(self, context: str, limit: int = 3) -> str:
        """根据当前对话上下文，动态检索最相关的知识库 QA 对"""
        if not _check_db() or not context.strip():
            return ""
        try:
            from db import semantic_search_qa
            t0 = time.time()
            results = semantic_search_qa(context, limit=limit)
            elapsed = time.time() - t0
            if results:
                lines = []
                for r in results:
                    ans = (r.get("answer") or "")[:200]
                    lines.append(
                        f"- [{r['category']}] Q: {r['question']}\n"
                        f"  参考答案要点: {ans}"
                    )
                rag_text = "\n".join(lines)
                print(f"[{_ts()}] │ [RAG] 动态检索完成 (耗时: {elapsed*1000:.0f}ms, 匹配 {len(results)} 条)")
                return rag_text
            return ""
        except Exception as e:
            print(f"[{_ts()}] │ [RAG] 动态检索失败: {e}")
            return ""

    def _call_llm(self, messages: list, temperature: float = 0.7, rag_context: str = "") -> str:
        """调用 LLM：use_local_model 时直接用 Ollama，否则优先平台模型并降级到 Ollama"""
        system_prompt = self._build_system_prompt(rag_context=rag_context)
        if self.use_local_model:
            from llm_client import LLM_MODEL
            print(f"[{_ts()}] │ [LLM] 🤖 使用本地模型: {LLM_MODEL} (Ollama)")
            return llm_chat_ollama(messages, system_prompt=system_prompt, temperature=temperature)
        try:
            from llm_client import _load_ai_config
            cfg = _load_ai_config()
            model_name = cfg.get("model", "unknown")
            base_url = cfg.get("base_url", "unknown")
            print(f"[{_ts()}] │ [LLM] ☁️ 使用平台模型: {model_name} ({base_url})")
            return llm_chat_deepseek(messages, system_prompt=system_prompt, temperature=temperature)
        except Exception as e:
            from llm_client import LLM_MODEL
            print(f"[{_ts()}] │ [LLM] ⚠️ 平台模型调用失败: {e}")
            print(f"[{_ts()}] │ [LLM] ⬇️ 降级到本地模型: {LLM_MODEL} (Ollama)")
            return llm_chat_ollama(messages, system_prompt=system_prompt, temperature=temperature)

    def start(self) -> Dict[str, Any]:
        """开始面试：生成开场白 + 第一个问题"""
        self.round_count = 1
        print(f"[{_ts()}] ┌─ 对话式面试启动 (job_focus=\"{self.job_focus or '通用'}\", local={self.use_local_model})")

        opening_prompt = f"""你即将开始面试一位候选人。

面试方向：{self.job_focus or '通用'}
候选人简历摘要：{self.resume or '无'}
匹配的岗位参考：{self.job_context or '无'}
知识库参考面试题（仅作评分参考，不是出题范围）：{self.kb_context or '无'}

⚠️ 重要：请仔细阅读候选人的简历摘要，提取其中提到的所有核心技能、项目经验和工作能力。出题要围绕岗位方向全面覆盖——简历写了什么就考什么，同时可以适度发散到该岗位常见的相关话题。

请向候选人打招呼（1-2句简短开场白），然后自然地提出第一个面试问题。
第一个问题应该是一个开放性问题，用于了解候选人的整体水平——可以从简历中最突出的经验或能力切入。
每次面试的开场题请从不同角度切入（项目经验/工作方法/核心能力/行业理解等），不要总是问同一个方向。
知识库参考仅用于帮你评判答案，出题方向以简历和岗位方向为准。
不要问太偏或太细的问题。

输出JSON：{{"message": "你的开场白+第一个问题"}}"""

        print(f"[{_ts()}] │ [启动] 生成开场白...")
        t0 = time.time()
        # 使用 init 时检索到的 kb_context 作为 RAG 参考
        result = self._call_llm(
            [{"role": "user", "content": opening_prompt}],
            temperature=0.7,
            rag_context=self.kb_context,
        )

        elapsed = time.time() - t0
        print(f"[{_ts()}] │ [启动] LLM 响应完成 (耗时: {elapsed*1000:.0f}ms)")

        data = parse_json_from_llm(result)
        message = data.get("message", result.strip()) if data else result.strip()
        if not message:
            message = f"你好！我是今天的面试官。看到你对 {self.job_focus or '技术'} 方向感兴趣，我们先来聊聊你的项目经验吧。能简单介绍一下你最近做过的相关项目吗？"

        self.history.append({"role": "assistant", "content": message})

        # 提取开场题目的类别（如果有）
        cat = data.get("category", "") if data else ""
        self._last_question = message
        self._last_category = cat

        print(f"[{_ts()}] └─ 面试启动完成! 开场长度: {len(message)} 字, 类别: {cat or '未标注'}")

        self._persist_session()  # 持久化初始状态

        return {
            "session_id": self.session_id,
            "message": message,
            "question_count": self.round_count,
        }

    def chat(self, user_message: str) -> Dict[str, Any]:
        """核心对话方法：处理用户消息，评分入库，返回面试官回复（动态权重 + RAG）。"""
        self.history.append({"role": "user", "content": user_message})
        self.round_count += 1

        print(f"[{_ts()}] ┌─ 对话轮次 {self.round_count}/{self.max_rounds}")
        print(f"[{_ts()}] │ [对话] 用户: \"{user_message[:100]}{'...' if len(user_message) > 100 else ''}\"")

        # 达到最大轮次，强制收尾
        if self.round_count > self.max_rounds:
            print(f"[{_ts()}] │ [对话] 已达最大轮次 {self.max_rounds}，强制收尾")
            closing = self._force_closing()
            self.history.append({"role": "assistant", "content": closing})
            self._save_record(user_message, 5.0, "", closing[:200])
            print(f"[{_ts()}] └─ 强制收尾完成")
            return {"reply": closing, "question_count": self.round_count, "want_to_end": True}

        # 动态 RAG 检索：基于最近对话检索相关知识点
        recent_context = " ".join([
            m["content"][:300] for m in self.history[-4:]
            if m["role"] in ("user", "assistant")
        ])
        rag_context = self._retrieve_relevant_qa(recent_context, limit=3) if recent_context else ""

        # 调用 LLM 生成回复
        print(f"[{_ts()}] │ [对话] 调用 LLM 生成回复... (历史 {len(self.history)} 条消息, RAG={'有' if rag_context else '无'})")
        t0 = time.time()
        result = self._call_llm(self.history, temperature=0.7, rag_context=rag_context)
        elapsed = time.time() - t0
        print(f"[{_ts()}] │ [对话] LLM 响应完成 (耗时: {elapsed*1000:.0f}ms, 响应长度: {len(result)} 字)")

        # 解析 JSON（含 score 和 category）
        print(f"[{_ts()}] │ [对话] 解析回复 JSON...")
        data = parse_json_from_llm(result)
        if data:
            reply = data.get("message", result)
            want_to_end = data.get("want_to_end", False)
            score = data.get("score")
            category = data.get("category", "")
            # 校验 score
            try:
                score = float(score)
                score = max(0, min(10, score))
            except (TypeError, ValueError):
                score = None
            print(f"[{_ts()}] │ [对话] JSON 解析成功 (want_to_end={want_to_end}, score={score}, category={category})")
        else:
            print(f"[{_ts()}] │ [对话] ⚠️ JSON 解析失败，使用原文作为回复")
            reply = result.strip()
            want_to_end = False
            score = None
            category = ""

        # 入库：保存本轮问答记录
        # ⚠️ category 应使用 _last_category（用户刚回答的题的类别），
        # 而非 LLM 本次返回的 category（那是下一道题的类别）
        record_category = self._last_category or category or ""
        self._save_record(user_message, score, record_category, reply[:300])

        self.history.append({"role": "assistant", "content": reply})

        # 更新"上一题"追踪（供下一轮入库时使用）
        self._last_question = reply
        self._last_category = category or self._last_category

        print(f"[{_ts()}] └─ 轮次 {self.round_count} 完成 (want_to_end={want_to_end}, score={score}, "
              f"cat={category or '未标注'}, 回复长度: {len(reply)} 字)")

        self._persist_session()  # 每轮结束后持久化

        return {
            "reply": reply,
            "question_count": self.round_count,
            "want_to_end": want_to_end,
            "score": score,
            "category": category,
        }

    def _save_record(self, user_answer: str, score, category: str, feedback: str):
        """将本轮问答保存到 MySQL interview_records 表。"""
        print(f"[{_ts()}] │ [入库] ┌─ 开始保存本轮问答记录...")
        print(f"[{_ts()}] │ [入库] │  session={self.session_id}, score={score}, category={category or '(空)'}")
        print(f"[{_ts()}] │ [入库] │  question=\"{(self._last_question or '(无)')[:80]}\"")
        print(f"[{_ts()}] │ [入库] │  answer_len={len(user_answer)}, feedback_len={len(feedback)}")

        if not _check_db():
            print(f"[{_ts()}] │ [入库] ⚠️ 数据库不可用！跳过入库 (_db_available={_db_available})")
            return

        try:
            try:
                from db import save_interview_record
                print(f"[{_ts()}] │ [入库] │  导入 db.save_interview_record 成功")
            except ImportError as ie:
                print(f"[{_ts()}] │ [入库] │  首次导入失败: {ie}, 尝试加 sys.path...")
                _dir = os.path.dirname(os.path.abspath(__file__))
                if _dir not in sys.path:
                    sys.path.insert(0, _dir)
                from db import save_interview_record
                print(f"[{_ts()}] │ [入库] │  二次导入成功")
            question = self._last_question or "（开场问题）"
            cat = category or self._last_category or ""
            jf = (self.job_focus or "")[:200]
            rid = save_interview_record(
                session_id=self.session_id,
                question_id=None,
                question=question[:500],
                user_answer=user_answer[:2000],
                score=score if score is not None else 5.0,
                feedback=feedback,
                job_focus=jf,
                category=cat,
            )
            print(f"[{_ts()}] │ [入库] ✅ 问答记录已保存 (id={rid}, score={score}, cat={cat or '无'})")
        except Exception as e:
            import traceback
            print(f"[{_ts()}] │ [入库] ❌ 保存记录失败: {e}")
            traceback.print_exc()

    def _force_closing(self) -> str:
        """超过最大轮次时强制生成收尾"""
        closing_prompt = "面试已经进行了多轮。请现在自然地收尾：简短总结候选人的整体表现，指出1-2个最需要加强的方向，给出学习建议。输出JSON：{\"message\": \"你的收尾发言\"}"

        try:
            result = self._call_llm(
                self.history + [{"role": "user", "content": closing_prompt}],
                temperature=0.5,
            )
            data = parse_json_from_llm(result)
            return data.get("message", result) if data else result
        except Exception:
            pass

        return f"好的，我们的面试就到这里。今天聊了 {self.round_count} 轮，覆盖了 {self.job_focus or '技术'} 相关的多个方向。建议你继续深入实践，多关注工程落地和性能优化方面的经验积累。感谢你的时间！"

    # ══════════════════════════════════════
    #  结束面试 & 总结
    # ══════════════════════════════════════

    def _mark_ended(self):
        """标记 SQLite 中的会话为已结束。"""
        try:
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
            from boss_state import mark_interview_ended as _end
            _end(self.session_id)
            print(f"[{_ts()}] │ [持久化] ✅ 会话已标记为 ended")
        except Exception as e:
            print(f"[{_ts()}] │ [持久化] ⚠️ 标记 ended 失败: {e}")

    def end_session(self) -> Dict[str, Any]:
        """结束面试，生成总结（含弱项分析和动态权重信息）。"""
        print(f"[{_ts()}] ┌─ 生成面试总结 (共 {self.round_count} 轮对话)")
        self._mark_ended()

        # 当前 session 的动态权重快照（无历史数据时为空）
        dyn_weights = self._get_dynamic_weights()
        weak_cats = [(c, w) for c, w in (dyn_weights or {}).items() if w > 0.25] if dyn_weights else []
        strong_cats = [(c, w) for c, w in (dyn_weights or {}).items() if w < 0.15] if dyn_weights else []

        # 尝试从 MySQL 获取记录
        db_summary = None
        if _check_db():
            try:
                from db import get_session_summary
                db_summary = get_session_summary(self.session_id)
            except Exception as e:
                print(f"[{_ts()}] │ [总结] MySQL 查询失败: {e}")

        # 如果有 DB 记录，用 LLM 生成详细总结
        if db_summary and db_summary.get("total_questions", 0) > 0:
            low_scores = [r for r in db_summary.get("records", [])
                         if r.get("score") is not None and r["score"] < 6]

            weak_text = ""
            if weak_cats:
                weak_text = "\n历史弱项类别（权重已调高，出题时会重点考察）：\n" + \
                    "\n".join(f"- {c}: 权重 {w*100:.0f}%" for c, w in weak_cats)

            prompt = f"""本次面试共{db_summary['total_questions']}题，平均得分{db_summary['avg_score']}分。
最高分：{db_summary['max_score']}，最低分：{db_summary['min_score']}。

低分题目（{len(low_scores)}题）：
{chr(10).join(f'- [{r.get("category", "")}] {r["question"][:50]}... (得分: {r["score"]})' for r in low_scores[:5])}

岗位方向：{self.job_focus or '通用'}
{weak_text}

请给出面试总结和建议：
1. 整体表现评价
2. 薄弱环节（需要重点加强的方向）
3. 下一步学习建议
4. 推荐重点准备的面试话题"""

            print(f"[{_ts()}] │ [总结] 基于 DB 记录生成详细总结 (共{db_summary['total_questions']}题, 均分{db_summary['avg_score']})")
            t_review = time.time()
            review = llm_chat_ollama(
                [{"role": "user", "content": prompt}],
                temperature=0.5,
            )
            print(f"[{_ts()}] └─ 详细总结生成完成 (耗时: {time.time()-t_review:.0f}s)")

            return {
                "session_id": self.session_id,
                "total_questions": db_summary["total_questions"],
                "avg_score": db_summary["avg_score"],
                "max_score": db_summary["max_score"],
                "min_score": db_summary["min_score"],
                "low_scores": low_scores[:5],
                "weak_categories": [{"category": c, "weight": round(w, 3)} for c, w in weak_cats],
                "strong_categories": [{"category": c, "weight": round(w, 3)} for c, w in strong_cats],
                "review": review,
            }

        # 没有 DB 记录，基于对话历史生成总结
        if self.round_count > 0:
            weak_text = ""
            if weak_cats:
                weak_text = "\n历史弱项类别：\n" + \
                    "\n".join(f"- {c}: 权重已调高至 {w*100:.0f}%，后续面试会重点考察" for c, w in weak_cats)

            summary_prompt = f"""请基于以下面试对话，用中文生成面试总结。包含：
1. 整体表现评价（2-3句，具体指出优势和不足）
2. 1-2个最需要加强的技术方向
3. 具体的学习建议
{weak_text}

岗位方向：{self.job_focus or '通用'}
共 {self.round_count} 轮对话。

输出JSON：{{"review": "你的总结内容"}}"""

            print(f"[{_ts()}] │ [总结] 基于对话历史生成总结 ({self.round_count} 轮)")
            t0 = time.time()
            try:
                result = self._call_llm(
                    self.history + [{"role": "user", "content": summary_prompt}],
                    temperature=0.5,
                )
                data = parse_json_from_llm(result)
                review = data.get("review", result) if data else result
            except Exception:
                review = f"本次面试共进行了 {self.round_count} 轮对话，覆盖了 {self.job_focus or '技术'} 方向的话题。建议继续深入学习和实践。"
            elapsed = time.time() - t0

            print(f"[{_ts()}] └─ 总结生成完成 (耗时: {elapsed:.0f}s)")
            return {
                "session_id": self.session_id,
                "total_questions": self.round_count,
                "weak_categories": [{"category": c, "weight": round(w, 3)} for c, w in weak_cats],
                "review": review,
            }

        return {"message": "本次没有面试记录"}
