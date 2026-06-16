"""
面试问答Agent - 快速检索层（V2）
学习模式：用户提问，系统在2-3秒内给出答案

V2改进：
1. Layer 0.5: 话题分类（关键词+规则，不调LLM，5ms内）
2. 域内检索：限制在分类后的知识域内搜索，避免跨域误配
3. 自动推荐同域相关问题
"""

import json, time, re
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
import numpy as np

from llm_client import get_embedding, cosine_similarity
from mysql_config import get_conn

SEMANTIC_MATCH_THRESHOLD = 0.65


# ===== 缓存 =====
class LRUCache:
    def __init__(self, capacity=200):
        self.cache = {}
        self.capacity = capacity
        self.order = []

    def get(self, key):
        if key in self.cache:
            self.order.remove(key)
            self.order.append(key)
            return self.cache[key]
        return None

    def set(self, key, value):
        if key in self.cache:
            self.order.remove(key)
        elif len(self.cache) >= self.capacity:
            oldest = self.order.pop(0)
            del self.cache[oldest]
        self.cache[key] = value
        self.order.append(key)

    def clear(self):
        self.cache.clear()
        self.order.clear()


query_cache = LRUCache(capacity=200)

# ===== Layer 0.5: 话题分类（基于embedding语义匹配） =====

# 每个话题域的代表性描述（用于embedding分类）
TOPIC_DESCRIPTIONS = {
    "编程语言": "Python Java Go Rust C++ TypeScript JavaScript 语法 特性 异步 多线程 并发 内存管理 泛型 装饰器 协程",
    "数据库": "MySQL Redis PostgreSQL MongoDB Elasticsearch SQL 索引 事务 分库分表 NoSQL 缓存 查询优化 ACID",
    "系统设计": "架构 分布式 微服务 消息队列 设计模式 高并发 高可用 CAP 负载均衡 领域驱动 DDD RESTful GraphQL",
    "工程化": "Docker K8s 部署 CI/CD 监控 日志 测试 性能优化 限流 安全 容器编排 自动化 运维",
    "前端": "React Vue Angular CSS HTML 响应式 SSR 跨域 小程序 浏览器API WebAssembly 状态管理",
    "AI/ML": "RAG Agent LLM Transformer 微调 LoRA Prompt Embedding 向量数据库 机器学习 深度学习 NLP CV",
    "数据处理": "Spark Hadoop Flink Kafka ETL 数据仓库 OLAP 数据湖 Pandas NumPy 数据清洗 可视化",
    "安全": "认证 授权 OAuth JWT SQL注入 XSS CSRF 加密 HTTPS 零信任 防火墙 渗透 权限 RBAC",
}

# 关键词辅助（embedding兜底，高置信度关键词覆盖embedding结果）
TOPIC_KEYWORDS = {
    "编程语言": {
        "keywords": [
            "python", "java", "go", "golang", "rust", "c++", "cpp", "c#", "csharp",
            "typescript", "javascript", "js", "kotlin", "swift", "scala",
            "异步", "asyncio", "多线程", "并发", "内存管理", "泛型", "装饰器", "协程",
            "JVM", "spring", "springboot", "django", "flask", "fastapi",
            "面向对象", "函数式", "闭包", "迭代器", "生成器",
        ],
        "high_confidence": ["python", "java", "golang", "typescript", "rust"],
    },
    "数据库": {
        "keywords": [
            "mysql", "redis", "postgresql", "mongodb", "elasticsearch", "oracle",
            "sql", "索引", "事务", "分库分表", "nosql", "缓存", "查询优化",
            "acid", "慢查询", "锁", "死锁", "主从", "读写分离", "分片",
            "连接池", "orm", "数据迁移", "备份",
        ],
        "high_confidence": ["mysql", "redis", "索引", "事务", "sql"],
    },
    "系统设计": {
        "keywords": [
            "架构", "分布式", "微服务", "消息队列", "设计模式", "高并发", "高可用",
            "cap", "负载均衡", "领域驱动", "ddd", "restful", "graphql",
            "rpc", "api", "网关", "服务发现", "配置中心", "限流", "降级", "熔断",
            "幂等", "最终一致性", "事件驱动", "cqrs", "saga",
        ],
        "high_confidence": ["微服务", "分布式", "高并发", "消息队列"],
    },
    "工程化": {
        "keywords": [
            "docker", "k8s", "kubernetes", "部署", "deploy", "ci/cd",
            "限流", "监控", "日志", "测试", "容器编排", "自动化", "安全配置", "可观测",
            "nginx", "jenkins", "git", "devops", "灰度", "回滚",
            "单元测试", "集成测试", "性能测试", "压测", "链路追踪",
        ],
        "high_confidence": ["docker", "k8s", "ci/cd", "devops"],
    },
    "前端": {
        "keywords": [
            "react", "vue", "angular", "css", "html", "webpack", "vite",
            "跨域", "响应式", "seo", "ssr", "小程序", "组件化",
            "dom", "浏览器", "http", "ajax", "axios", "路由", "状态管理",
            "redux", "pinia", "vuex", "nextjs", "nuxt",
        ],
        "high_confidence": ["react", "vue", "angular", "前端", "css"],
    },
    "AI/ML": {
        "keywords": [
            "rag", "agent", "llm", "大模型", "transformer", "微调", "fine-tuning",
            "lora", "prompt", "embedding", "向量数据库", "机器学习", "深度学习",
            "nlp", "cv", "ner", "分类", "回归", "聚类", "神经网络",
            "langchain", "langgraph", "autogen", "crewai", "function calling",
            "幻觉", "预训练", "sft", "rlhf", "dpo", "mcp",
        ],
        "high_confidence": ["rag", "agent", "llm", "大模型", "transformer"],
    },
    "数据处理": {
        "keywords": [
            "spark", "hadoop", "flink", "kafka", "etl", "数据仓库",
            "olap", "数据湖", "pipeline", "airflow",
            "pandas", "numpy", "数据清洗", "数据分析", "可视化",
        ],
        "high_confidence": ["spark", "flink", "etl", "数据仓库"],
    },
    "安全": {
        "keywords": [
            "认证", "授权", "oauth", "jwt", "sql注入", "xss", "csrf",
            "加密", "https", "零信任", "防火墙",
            "渗透", "漏洞", "安全审计", "权限", "rbac",
        ],
        "high_confidence": ["sql注入", "xss", "oauth", "加密"],
    },
}


def classify_topic(question: str) -> Optional[str]:
    """对用户问题做话题分类，返回最匹配的话题域（纯关键词为主，embedding兜底）"""
    q_lower = question.lower()

    # 1. 关键词匹配（主方案，更快更准）
    kw_scores = {}
    for topic, info in TOPIC_KEYWORDS.items():
        score = 0
        for kw in info["keywords"]:
            if kw in q_lower:
                score += 1
        for kw in info.get("high_confidence", []):
            if kw in q_lower:
                score += 3
        if score > 0:
            kw_scores[topic] = score

    # 高置信度关键词命中 → 直接返回
    for topic, score in kw_scores.items():
        if score >= 3:
            return topic

    # 单个关键词命中 → 返回得分最高的
    if kw_scores:
        kw_best = max(kw_scores, key=kw_scores.get)
        if kw_scores[kw_best] >= 1:
            return kw_best

    # 2. embedding兜底：关键词没命中时用语义判断
    if not hasattr(classify_topic, "_desc_vecs"):
        classify_topic._desc_vecs = {}
    desc_vecs = classify_topic._desc_vecs

    query_vec = get_embedding(question)
    best_topic, best_score = None, 0
    for topic, desc in TOPIC_DESCRIPTIONS.items():
        if topic not in desc_vecs:
            desc_vecs[topic] = get_embedding(desc)
        sim = cosine_similarity(query_vec, desc_vecs[topic])
        if sim > best_score:
            best_score, best_topic = sim, topic

    return best_topic if best_score >= 0.35 else None


# ===== 域内检索 =====


def _domain_filter_sql(topic: Optional[str]) -> Tuple[str, list]:
    """生成按话题域过滤的SQL条件"""
    if topic:
        # 标准化新topic → 数据库category映射
        topic_to_category = {
            "编程语言": "Python",      # DB中仅Python归属编程语言类
            "工程化": "工程化",
            # AI/ML 覆盖多个旧类别(RAG/Agent/大模型)，不限制单类，搜全部
            # 数据库/系统设计/前端/数据处理/安全——DB暂无对应数据，搜全部
        }
        category = topic_to_category.get(topic)
        if category:
            return "AND category = %s", [category]
    return "", []


def _load_qa_in_domain(topic: Optional[str]) -> List[Dict]:
    """加载指定域的所有问答对"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            filter_sql, params = _domain_filter_sql(topic)
            sql = f"""
                SELECT id, category, question, answer, difficulty, related_skills, embedding
                FROM interview_qa_pairs
                WHERE embedding IS NOT NULL AND embedding != ''
                {filter_sql}
            """
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def domain_fulltext_search(query: str, topic: Optional[str], limit: int = 5) -> List[Dict]:
    """域内全文检索"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            filter_sql, params = _domain_filter_sql(topic)
            sql = f"""
                SELECT id, category, question, answer, difficulty, related_skills,
                       MATCH(question) AGAINST(%s IN NATURAL LANGUAGE MODE) as score
                FROM interview_qa_pairs
                WHERE MATCH(question) AGAINST(%s IN NATURAL LANGUAGE MODE)
                {filter_sql}
                ORDER BY score DESC
                LIMIT %s
            """
            full_params = [query, query] + params + [limit]
            cur.execute(sql, full_params)
            rows = cur.fetchall()
            if rows and rows[0]["score"] > 0.5:
                return [dict(r) for r in rows]
    finally:
        conn.close()

    # 回退：关键词LIKE检索
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    eng = re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]{1,}", query)
    keywords = [w for w in cjk if len(w) >= 2] + eng
    if not keywords:
        return []

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 精确匹配回退：在所有关键词中找到匹配最多的那条题
            candidates = set()
            for w in keywords:
                cur.execute(
                    "SELECT id, category, question, answer, difficulty, related_skills "
                    "FROM interview_qa_pairs WHERE question LIKE %s",
                    (f"%{w}%",),
                )
                for row in cur.fetchall():
                    candidates.add((row["id"], row["question"]))

            if candidates:
                import collections

                # 统计每道题被多少关键词匹配到
                counter = collections.Counter(qid for qid, _ in candidates)
                best_id = counter.most_common(1)[0][0]
                cur.execute(
                    "SELECT id, category, question, answer, difficulty, related_skills "
                    "FROM interview_qa_pairs WHERE id = %s",
                    (best_id,),
                )
                row = cur.fetchone()
                if row:
                    return [dict(row)]

            # 大范围LIKE检索
            conditions, params = [], []
            for w in keywords[:8]:
                conditions.append("(question LIKE %s OR answer LIKE %s)")
                params.extend([f"%{w}%", f"%{w}%"])
            filter_sql, filter_params = _domain_filter_sql(topic)
            sql = f"""
                SELECT id, category, question, answer, difficulty, related_skills
                FROM interview_qa_pairs
                WHERE ({" OR ".join(conditions)})
                {filter_sql}
                ORDER BY id
                LIMIT {limit * 3}
            """
            cur.execute(sql, params + filter_params)
            rows = cur.fetchall()
            if rows:

                def match_score(row):
                    q = row["question"]
                    return sum(1 for w in keywords if w.lower() in q.lower())

                rows.sort(key=match_score, reverse=True)
                return [dict(r) for r in rows[:limit]]
    finally:
        conn.close()
    return []


def domain_semantic_search(query: str, topic: Optional[str], limit: int = 5, threshold: float = 0.55) -> List[Dict]:
    """域内语义检索（主检索方案）

    核心方案：embedding语义匹配，能处理同义词和不同表达。
    阈值0.55比之前的0.65宽松，提升召回率。
    """
    qas = _load_qa_in_domain(topic)
    if not qas:
        return []

    query_vec = get_embedding(query)
    results = []
    for qa in qas:
        try:
            stored_vec = json.loads(qa["embedding"])
            sim = cosine_similarity(query_vec, stored_vec)
            results.append({**qa, "similarity": round(sim, 4)})
        except:
            continue

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return [r for r in results if r["similarity"] >= threshold][:limit]


def domain_exact_match(query: str, topic: Optional[str]) -> Optional[Dict]:
    """域内精确匹配"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if topic:
                topic_to_category = {
                    "RAG": "RAG",
                    "Agent": "Agent",
                    "大模型": "大模型",
                    "工程化": "工程化",
                    "Python": "Python",
                    "Prompt Engineering": "大模型",
                }
                cat = topic_to_category.get(topic)
                if cat:
                    cur.execute(
                        "SELECT id, category, question, answer, difficulty, related_skills "
                        "FROM interview_qa_pairs WHERE question = %s AND category = %s LIMIT 1",
                        (query, cat),
                    )
                    row = cur.fetchone()
                    if row:
                        return dict(row)
            # 无域限制的LIKE回退
            cur.execute(
                "SELECT id, category, question, answer, difficulty, related_skills "
                "FROM interview_qa_pairs WHERE question LIKE %s LIMIT 1",
                (f"%{query}%",),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
    finally:
        conn.close()
    return None


# ===== 同域推荐 =====


def get_related_questions(
    question: str, topic: Optional[str], current_id: Optional[int] = None, limit: int = 3
) -> List[Dict]:
    """推荐同域内相关问题（排除当前匹配的题）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            filter_sql, params = _domain_filter_sql(topic)
            sql = f"""
                SELECT id, question FROM interview_qa_pairs
                WHERE 1=1 {filter_sql}
                ORDER BY id
                LIMIT {limit * 3}
            """
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    # 排除当前问题，按关键词重叠排序
    q_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", question.lower()))
    q_words.update(w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]{1,}", question))

    scored = []
    for r in rows:
        if current_id and r["id"] == current_id:
            continue
        r_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", r["question"].lower()))
        r_words.update(w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]{1,}", r["question"]))
        overlap = len(q_words & r_words)
        scored.append((overlap, r["question"], r["id"]))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"question": q[1], "id": q[2]} for q in scored[:limit] if q[0] > 0]


# ===== 预置快捷回答 =====

SHORT_ANSWERS = {
    "什么是RAG": "RAG（Retrieval-Augmented Generation）即检索增强生成，将信息检索与大语言模型结合。核心：用户问题→检索文档→作为上下文→LLM生成。优势：无需训练、知识可更新、减少幻觉。",
    "什么是Agent": "AI Agent是能自主感知环境、制定计划并执行行动的智能体。核心特征：工具使用、记忆、规划、循环推理（ReAct）。区别于普通LLM调用，Agent能自主决策和执行多步任务。",
    "什么是Transformer": "Transformer是2017年Google提出的架构，核心是Self-Attention机制。由Encoder-Decoder组成，含多头注意力、FFN、残差连接和LayerNorm。BERT/GPT等大模型都基于此。",
    "什么是LoRA": "LoRA（Low-Rank Adaptation）通过在权重旁加低秩矩阵微调，只需训练0.1%-1%参数，大幅降低显存。QLoRA结合量化，单卡可微调大模型。",
    "什么是Prompt": "Prompt Engineering是通过设计输入提示引导LLM输出预期结果。核心技巧：角色设定、思维链(CoT)、Few-shot示例、结构化输出、负向提示、分步指令。",
    "什么是向量数据库": "向量数据库存储和检索高维向量，支持ANN搜索。主流：Milvus、FAISS、Pinecone、Qdrant、Weaviate。在RAG中用于存储文档embedding并做相似度检索。",
    "什么是微调": "微调是在预训练模型上用特定领域数据继续训练。常见方式：全参数微调、LoRA、Adapter。能显著提升特定任务表现。",
    "什么是embedding": "Embedding将文本映射到高维向量空间，语义相近的内容在空间中距离也近。常用模型：OpenAI text-embedding-3、nomic-embed-text、BGE。",
}


def check_short_answer(query: str) -> Optional[str]:
    """预置快捷回答"""
    if query in SHORT_ANSWERS:
        return SHORT_ANSWERS[query]
    for key, answer in SHORT_ANSWERS.items():
        if key in query or query in key:
            return answer
    return None


def ask_deepseek(question: str) -> str:
    """AI API兜底（从SQLite设置读取配置）"""
    import urllib.request, os, sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from boss_state import get_setting, init_db

    init_db()
    api_key = get_setting("ai_api_key") or ""
    api_url = (get_setting("ai_base_url") or "https://api.deepseek.com") + "/chat/completions"
    model = get_setting("ai_model") or "deepseek-chat"

    t0 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在调用 AI API 兜底回答... (model={model}, question=\"{question[:60]}...\")")

    prompt = f"""你是一个技术专家。用户问了一个技术问题，请用简短（50-100字）、准确的方式回答。

用户问题：{question}

要求：
- 答案控制在50-100字
- 直接回答问题，不要铺垫
- 技术术语要准确
- 如果是概念性问题，给出定义+一句话解释"""

    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.2,
        }
    ).encode()
    req = urllib.request.Request(
        api_url, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read().decode())
    elapsed = time.time() - t0
    result_len = len(data["choices"][0]["message"]["content"])
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ AI API 兜底回答完成 (耗时: {elapsed*1000:.0f}ms, 输出长度: {result_len}字)")
    return data["choices"][0]["message"]["content"]


def _keyword_overlap(question: str, matched: str) -> float:
    """计算用户问题与匹配结果的关键词重叠比例"""
    q_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", question.lower()))
    q_words.update(w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]{1,}", question))
    m_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", matched.lower()))
    m_words.update(w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]{1,}", matched))
    if not q_words or not m_words:
        return 1.0
    overlap = len(q_words & m_words)
    return overlap / len(q_words) if q_words else 1.0


# ===== 主入口 =====


def fast_answer(question: str) -> Dict[str, Any]:
    """
    完整检索流程：
    L0: 缓存命中
    L0.5: 话题分类
    L1: 域内精确匹配 + LIKE
    L2: 域内语义检索（embedding匹配）
    L3: 预置短回答
    L4: DeepSeek兜底
    """
    start = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 快速问答开始... (question=\"{question[:60]}...\")")

    # L0: 缓存
    cached = query_cache.get(question)
    if cached:
        elapsed = time.time() - start
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚡ 缓存命中 (耗时: {elapsed*1000:.0f}ms, layer=0)")
        resp = {**cached, "layer": 0, "elapsed_ms": round(elapsed * 1000)}
        return resp

    # L0.5: 话题分类
    t_topic = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在进行话题分类...")
    topic = classify_topic(question)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 话题分类完成 (耗时: {(time.time()-t_topic)*1000:.0f}ms, topic={topic})")

    # L1: 域内精确匹配 + LIKE
    result = domain_exact_match(question, topic)
    if result:
        elapsed = time.time() - start
        related = get_related_questions(result["question"], topic or result["category"], result["id"])
        resp = {
            "answer": result["answer"],
            "category": result["category"],
            "question": result["question"],
            "matched": result["question"],
            "topic": topic,
            "layer": 1,
            "elapsed_ms": round(elapsed * 1000),
            "related": related,
        }
        query_cache.set(question, resp)
        return resp

    # L2: 域内语义检索（主方案，embedding匹配）
    t_sem = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在进行域内语义检索... (threshold={SEMANTIC_MATCH_THRESHOLD})")
    sem = domain_semantic_search(question, topic, threshold=SEMANTIC_MATCH_THRESHOLD)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 语义检索完成 (耗时: {(time.time()-t_sem)*1000:.0f}ms, 匹配数={len(sem)})")
    if sem:
        best = sem[0]
        elapsed = time.time() - start

        # 置信度检查：关键词重叠太低说明embedding只靠语义框架匹配而非真正理解
        overlap_ratio = _keyword_overlap(question, best["question"])

        # 低置信度（关键词完全没重叠）→ 放弃检索结果，走DeepSeek
        if overlap_ratio < 0.18:
            confidence = "low"
            # 不走缓存，直接降级到DeepSeek
            try:
                answer = ask_deepseek(question)
                ds_elapsed = time.time() - start
                resp = {
                    "answer": answer,
                    "category": "AI生成",
                    "question": question,
                    "matched": question,
                    "confidence": "low",
                    "topic": topic,
                    "layer": 4,
                    "elapsed_ms": round(ds_elapsed * 1000),
                    "related": [],
                    "note": "知识库中未找到精确匹配，以下为AI生成回答，仅供参考",
                }
                query_cache.set(question, resp)
                return resp
            except:
                # DeepSeek失败，退回检索结果
                pass

        confidence = "medium"
        related = get_related_questions(best["question"], topic or best["category"], best["id"])
        resp = {
            "answer": best["answer"],
            "category": best["category"],
            "question": best["question"],
            "matched": best["question"],
            "similarity": best.get("similarity", 0),
            "confidence": confidence,
            "topic": topic,
            "layer": 2,
            "elapsed_ms": round(elapsed * 1000),
            "related": related,
        }
        query_cache.set(question, resp)
        return resp

    # L3: 预置短回答
    preset = check_short_answer(question)
    if preset:
        elapsed = time.time() - start
        resp = {
            "answer": preset,
            "category": topic or "快速应答",
            "question": question,
            "matched": question,
            "topic": topic,
            "layer": 3,
            "elapsed_ms": round(elapsed * 1000),
            "related": [],
        }
        query_cache.set(question, resp)
        return resp

    # L4: DeepSeek兜底
    try:
        answer = ask_deepseek(question)
        elapsed = time.time() - start
        resp = {
            "answer": answer,
            "category": "AI生成",
            "question": question,
            "matched": question,
            "topic": topic,
            "layer": 4,
            "elapsed_ms": round(elapsed * 1000),
            "related": [],
        }
        query_cache.set(question, resp)
        return resp
    except Exception as e:
        elapsed = time.time() - start
        return {
            "answer": f"抱歉，未能找到答案。错误：{str(e)}。请换个问法试试。",
            "category": "未知",
            "question": question,
            "matched": question,
            "topic": topic,
            "layer": -1,
            "elapsed_ms": round(elapsed * 1000),
            "related": [],
        }
