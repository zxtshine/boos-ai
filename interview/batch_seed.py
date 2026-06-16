#!/usr/bin/env python3
"""
批量生成面试问答对 - 用DeepSeek API生成100+高频RAG/Agent/大模型问答对
修复死锁问题 + 每条记录独立提交 + 跳过已存在
"""

import json, os, re, sys, time
import urllib.request
from datetime import datetime

# AI API（从SQLite设置读取）
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from boss_state import get_setting, init_db

init_db()
API_KEY = get_setting("ai_api_key") or ""
BASE_URL = get_setting("ai_base_url") or "https://api.deepseek.com"
API_URL = f"{BASE_URL}/chat/completions"
MODEL = get_setting("ai_model") or "deepseek-chat"

from mysql_config import get_conn


def question_exists(question):
    """检查问题是否已存在"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM interview_qa_pairs WHERE question = %s", (question,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def insert_qa(topic, question, answer):
    """插入一条问答对（单独连接+提交，避免死锁）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO interview_qa_pairs (category, question, answer, difficulty, related_skills) VALUES (%s, %s, %s, %s, %s)",
                (topic, question, answer, "medium", topic),
            )
            conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"\n  ❌ 插入失败: {e}")
        return False
    finally:
        conn.close()


# 完整题目列表（覆盖所有核心知识点）
TOPICS = {
    "RAG": [
        "什么是RAG？它的核心思想和价值是什么？",
        "RAG的完整工作流程是怎样的？",
        "Chunking有哪些策略？chunk大小怎么选？",
        "什么是chunk重叠？为什么要用重叠？",
        "有哪些embedding模型？中文场景推荐哪些？",
        "向量数据库有哪些？Milvus/FAISS/Pinecone怎么选？",
        "向量检索和关键词检索的区别？什么时候用哪个？",
        "什么是混合检索？如何融合向量和BM25的结果？",
        "什么是Rerank重排序？Cross-encoder和Bi-encoder的区别？",
        "重排序是怎么提升检索质量的？",
        "什么是HyDE？它如何提升检索效果？",
        "什么是RRF（Reciprocal Rank Fusion）？",
        "RAG系统怎么处理多轮对话中的上下文？",
        "生产环境中RAG系统有哪些落地挑战？",
        "如何评估RAG系统的检索质量？有哪些指标？",
        "RAG的chunk检索准确率低怎么办？",
        "RAG的幻觉问题怎么解决？",
        "RAG和Agent如何结合使用？",
        "RAG系统中如何更新文档知识？",
        "RAG的多模态场景如何处理（图文混合文档）？",
        "什么是Graph RAG？和传统RAG的区别？",
        "RAG系统如何做query改写/query理解？",
        "什么是Self-RAG？它解决了什么问题？",
        "RAG的检索源有哪些？数据库/API/文件系统怎么整合？",
        "如何设计RAG系统的缓存机制？",
        "RAG中Metadata Filtering是什么？有什么用？",
        "RAG系统如何处理长文档（几百页PDF）？",
        "RAG的响应延迟如何优化？",
        "什么是CRAG（Corrective RAG）？",
        "RAG在智能客服场景的完整落地案例",
    ],
    "Agent": [
        "什么是AI Agent？核心特征是什么？",
        "Agent和普通LLM调用有什么区别？",
        "ReAct模式的工作原理是什么？",
        "Tool Use/Function Calling怎么设计？",
        "Agent的Memory有几种？短期/长期/工作记忆如何设计？",
        "Multi-Agent有哪些架构模式？",
        "Supervisor Agent模式怎么工作？",
        "Agent的Planning能力如何实现？",
        "什么是Plan-and-Execute模式？",
        "Agent如何做错误恢复和重试？",
        "Agent的安全性考虑有哪些？权限如何控制？",
        "LangGraph和AutoGen的区别？",
        "CrewAI的Agent协作模式？",
        "如何设计Agent的System Prompt？",
        "Agent的执行循环有什么常见陷阱？",
        "什么是Reflection Agent？",
        "Agent的Token预算管理怎么做？",
        "Agent如何访问外部知识库？",
        "什么是Agentic RAG？",
        "Agent做复杂任务时如何分解步骤？",
        "Agent的日志和可观测性怎么设计？",
        "多Agent通信的消息格式怎么设计？",
        "Agent的并行执行和串行执行怎么选？",
        "Agent如何处理用户的模糊指令？",
        "Agent工具的描述怎么写才能让LLM正确使用？",
        "什么是MCP（Model Context Protocol）？",
        "Agent的持久化状态怎么管理？",
        "如何测试Agent的可靠性？",
        "Agent的流式输出怎么实现？",
        "Agent在实际项目中的落地案例",
    ],
    "大模型": [
        "Transformer的核心结构是什么？",
        "Self-Attention的计算过程？",
        "多头注意力机制的作用是什么？",
        "位置编码（Positional Encoding）的作用？",
        "什么是LLM的上下文窗口？",
        "Prompt Engineering的核心技巧？",
        "什么是Few-shot/Zero-shot/Chain-of-Thought？",
        "什么是SFT（监督微调）？",
        "什么是LoRA？为什么有效？",
        "QLoRA和LoRA的区别？",
        "什么是RLHF/DPO？",
        "什么是量化？GPTQ/AWQ/GGUF的区别？",
        "大模型推理加速有哪些技术？",
        "什么是KV Cache？",
        "什么是Speculative Decoding？",
        "什么是MoE（混合专家）架构？",
        "模型评估指标：Perplexity/BLEU/ROUGE的适用场景？",
        "什么是模型幻觉？如何缓解？",
        "LLM的Temperature/Top-p/Top-k参数怎么调？",
        "什么是搜索增强（Search Augmented LLM）？",
        "大模型的训练流程（预训练→SFT→RLHF）？",
        "什么是DPO（Direct Preference Optimization）？",
        "什么是长文本外推？有哪些技术？",
        "OpenAI和开源模型的生态对比？",
        "什么是Embedding模型？怎么选型？",
        "大模型应用中的安全对齐策略？",
        "什么是Agents的System 1和System 2思考？",
        "大模型领域的最新趋势？",
    ],
    "工程化": [
        "FastAPI设计LLM接口的最佳实践？",
        "流式输出（SSE/WebSocket）怎么实现？",
        "LLM API的超时和重试怎么设计？",
        "AI应用的Docker部署最佳实践？",
        "容器编排中GPU资源怎么管理？",
        "AI应用的监控和日志怎么设计？",
        "大模型服务的负载均衡怎么做？",
        "RAG系统的API设计要点？",
        "AI应用的数据库选型：向量库/关系库/缓存？",
        "AI应用的CI/CD流程怎么设计？",
        "AI应用的单元测试和集成测试要点？",
        "LLM API调用的成本控制策略？",
        "AI应用的Rate Limiting和配额管理？",
        "多租户场景下的RAG系统隔离设计？",
        "AI应用的安全性：提示注入怎么防？",
    ],
}


def call_deepseek(messages, max_tokens=400, temperature=0.3):
    """调用DeepSeek API，带重试"""
    t0 = time.time()
    last_content = (messages[-1]["content"] if messages else "")[:80].replace("\n", " ")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在调用 AI API 批量生成... (model={MODEL}, context=\"{last_content}...\")")
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode()
    req = urllib.request.Request(
        API_URL, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    )
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read().decode())
    elapsed = time.time() - t0
    result_len = len(data["choices"][0]["message"]["content"])
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ AI API 批量生成完成 (耗时: {elapsed*1000:.0f}ms, 输出长度: {result_len}字)")
    return data["choices"][0]["message"]["content"]


def generate_answer(topic, question):
    """生成单个问题-答案对（重试1次）"""
    prompt = f"""你是一个资深的技术面试专家。请为以下面试题提供高质量答案。

分类：{topic}
面试题：{question}

要求：
1. 答案要专业、准确，300字以内
2. 包含具体的技术细节和最佳实践
3. 切中面试官想考察的核心要点
4. 用中文回答，直接输出答案内容"""

    for attempt in range(2):
        try:
            answer = call_deepseek([{"role": "user", "content": prompt}], max_tokens=500)
            # 去掉"答案："前缀
            answer = answer.replace("答案：", "").replace("答案:", "").strip()
            return answer
        except Exception as e:
            print(f"\n  ⚠️ 重试({attempt + 1}/2): {e}")
            time.sleep(3)
    return ""


def seed_all():
    """生成所有问答对"""
    total = sum(len(qs) for qs in TOPICS.values())
    done = 0
    skipped = 0
    added = 0
    errors = 0

    print(f"🚀 准备生成 {total} 条面试问答对...\n")

    for topic, questions in TOPICS.items():
        count = 0
        for q in questions:
            done += 1

            if question_exists(q):
                print(f"  ⏭️ [{done}/{total}] [{topic}] 已存在")
                skipped += 1
                continue

            print(f"  🔄 [{done}/{total}] [{topic}] 生成中...", end="", flush=True)
            answer = generate_answer(topic, q)

            if answer and len(answer) > 20:
                if insert_qa(topic, q, answer):
                    count += 1
                    added += 1
                    print(f" ✅ ({len(answer)}字)")
                else:
                    errors += 1
                    print(f" ❌ 入库失败")
            else:
                errors += 1
                print(f" ❌ 生成内容为空")

            time.sleep(0.3)  # API限速

        print(f"\n📊 [{topic}] 新增 {count} 条\n")

    print(f"🎉 完成！总计 {total} 条 | 新增 {added} | 跳过 {skipped} | 失败 {errors}")


def refresh_embeddings():
    """刷新所有问答对的embedding"""
    from llm_client import get_embedding
    import json

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT id, question FROM interview_qa_pairs WHERE embedding IS NULL OR embedding = ""')
            rows = cur.fetchall()
    finally:
        conn.close()

    count = 0
    for qid, question in rows:
        try:
            emb = get_embedding(question)
            emb_json = json.dumps(emb, ensure_ascii=False)
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE interview_qa_pairs SET embedding = %s WHERE id = %s", (emb_json, qid))
                    conn.commit()
                count += 1
            finally:
                conn.close()
        except Exception as e:
            print(f"embedding错误 id={qid}: {e}")

    print(f"\n刷新了 {count} 条embedding")


if __name__ == "__main__":
    seed_all()
    print("\n刷新embedding...")
    refresh_embeddings()
    print("全部完成！")
