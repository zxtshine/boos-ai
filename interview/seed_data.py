"""
面试问答Agent - 种子数据脚本
用大模型生成初始面试问答对
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from db import add_qa_pair


# 预置的面试题结构（先加一批基础题）
SEED_QUESTIONS = [
    # RAG篇
    ("RAG", "请详细说说RAG（检索增强生成）的工作原理和核心流程", 
     "RAG的核心流程：1) 文档分块(chunking) - 将文档切分成合适大小的块；2) 向量化 - 用embedding模型将文本转向量；3) 向量存储 - 存入向量数据库(Milvus/FAISS等)；4) 检索 - 用户查询时先向量化，在向量库做相似度搜索(top-k)；5) 生成 - 将检索结果+原始问题一起送入LLM生成回答。关键点：chunk大小策略(256-1024 tokens)、chunk重叠、检索策略(相似度/MMR/混合检索)、rerank排序。",
     "hard", "RAG,embedding,向量数据库,chunking"),

    ("RAG", "RAG和微调有什么区别？什么时候用RAG，什么时候用微调？",
     "RAG优势：1) 无需训练，成本低；2) 知识可随时更新；3) 可追溯来源，减少幻觉；4) 适合知识频繁更新的场景。微调优势：1) 模型深度理解领域知识；2) 推理速度更快（无需检索）；3) 可改变模型行为/输出风格。选型建议：知识性问答(公司文档/产品手册)→RAG；任务型(代码生成/格式转换)→微调；实际项目中常RAG+微调结合使用。",
     "medium", "RAG,微调,finetuning"),

    ("RAG", "RAG系统中chunk大小怎么设计？有什么策略？",
     "chunk策略：1) 固定大小分块(256/512/1024 tokens)；2) 语义分块(按段落、标题、句子断句)；3) 递归分块(先大块再细分)。大小权衡：大块→上下文丰富但检索精确度下降；小块→检索精准但上下文不足。经验值：256-512 tokens是常见选择。高级技巧：chunk重叠(10-20%)避免信息断裂；配合Metadata过滤(按来源/日期筛选)；多粒度索引(大块+小块双通道)。",
     "hard", "RAG,chunking,分块策略"),

    ("RAG", "RAG系统中有哪些检索策略？各自的优缺点是什么？",
     "主流检索策略：1) 向量相似度检索(cosine/IP) - 最常用，适合语义匹配；2) 关键词检索(BM25) - 精确匹配，适合专有名词；3) 混合检索(向量+BM25) - 取两者之长，通常用RRF融合；4) MMR(Maximal Marginal Relevance) - 在相关性和多样性间平衡；5) Rerank - 先用轻量方法召回top-50,再用重排序模型精排。实践建议：混合检索+rerank效果最好，但成本高，简单场景向量检索就够了。",
     "hard", "RAG,检索策略,BM25,MMR,rerank"),

    ("RAG", "生产环境中RAG系统有哪些落地挑战？如何解决？",
     "主要挑战：1) 检索质量 - 漏检/误检，解决：混合检索+rerank+HyDE；2) 延迟 - 多步检索耗时，解决：缓存(query/answer缓存)、异步检索、lightweight reranker；3) 知识更新 - 文档变动怎么同步，解决：增量索引、定时重建；4) 幻觉 - 检索结果未正确使用，解决：引用标注、fact-check；5) 多轮对话 - 上下文中的历史信息管理，解决：query重写、上下文压缩。",
     "hard", "RAG,生产环境,工程化"),

    ("RAG", "什么是HyDE（假设文档嵌入）？它怎么提升检索效果？",
     "HyDE (Hypothetical Document Embeddings) 是提升RAG检索质量的技术。核心思路：先用LLM根据用户问题生成一个假设的完美回答文档，然后用这个假设文档的embedding去做检索。原理：假设文档比原始query在语义空间中更接近真实目标文档。优点：解决query-document语义gap问题，特别是用户query表达模糊时效果显著。代价：多一次LLM调用，增加延迟。",
     "hard", "RAG,HyDE,检索优化"),

    # Agent篇
    ("Agent", "什么是AI Agent？和普通的LLM调用有什么区别？",
     "AI Agent = LLM + 工具 + 记忆 + 规划。核心区别：1) 普通LLM：单次问答，无状态无行动能力；2) Agent：有循环推理能力(ReAct)，能调用工具(API/数据库/代码执行)，能维护短期/长期记忆，能做任务规划。典型框架：Function Calling + ReAct Loop，让LLM不断思考-行动-观察。主流框架：LangGraph/AutoGen/CrewAI。",
     "medium", "Agent,function calling,ReAct"),

    ("Agent", "Agent的ReAct模式是怎么工作的？画一下它的循环流程",
     "ReAct (Reasoning + Acting) 循环：1) Thought(思考) - 分析当前情况，决定下一步；2) Action(行动) - 选择一个工具并给出参数；3) Observation(观察) - 获取工具执行结果；4) 回到Thought→直到完成任务或达到限制。关键设计：system prompt要给出清晰的工具描述和调用格式；需要处理工具调用失败(重试/降级)；需要token预算控制(限制循环次数)。",
     "medium", "Agent,ReAct,循环推理"),

    ("Agent", "Multi-Agent架构有哪些常见模式？分别在什么场景使用？",
     "常见模式：1)  Supervisor模式 - 一个主Agent调度多个子Agent，适合复杂任务分解；2)  Debate模式 - 多个Agent辩论协作，适合需要多角度分析的决策场景；3) Pipeline模式 - Agent链式传递，适合流水线式任务(如代码生成→评审→测试)；4) 广场模式 - 所有Agent自由协作，灵活但难控制。选型：任务确定性强→Pipeline；需要决策质量→Debate；业务复杂多环节→Supervisor。",
     "hard", "Agent,多智能体,架构设计"),

    ("Agent", "Agent的工具调用(Function Calling)怎么设计？有什么最佳实践？",
     "设计要点：1) 工具描述要清晰准确 - LLM靠描述选工具，写清楚参数和用途；2) 参数尽量用必填+类型约束 - 减少解析失败；3) 幂等性 - 同一工具多次调用结果一致；4) 错误处理 - 工具返回错误信息要详细，帮助LLM理解并重试；5) 鉴权 - 敏感操作需要权限验证。实践：工具注册用JSON Schema格式；复杂逻辑用Parallel Tool Calls并行调用；工具返回要有结构化。",
     "hard", "Agent,function calling,tool use"),

    ("Agent", "Agent的记忆(Memory)有哪几种？怎么设计？",
     "记忆分层：1) 短期记忆 - 当前对话上下文(注意窗口限制)，用滑动窗口/压缩；2) 长期记忆 - 持久化到数据库，关键信息摘要存储；3) 工作记忆 - 任务进行中的中间状态。实现方案：短期→LLM context window + summarize；长期→向量数据库存储+检索；工作记忆→JSON结构维护。RAG本质也是一种外挂记忆。关键设计：记忆的更新策略(什么信息值得记)、检索时机(什么时候查记忆)、记忆的淘汰策略。",
     "hard", "Agent,记忆,memory"),

    # 大模型篇
    ("大模型", "说一下Transformer的核心结构和自注意力机制的原理",
     "Transformer核心：Encoder-Decoder结构。Self-Attention：QKV(Query/Key/Value)机制，通过计算Q和K的点积得到注意力权重，加权求和V。公式：Attention(Q,K,V)=softmax(QK^T/√d)V。√d是缩放因子防止梯度消失。多头注意力(MHA)让模型在不同子空间学习。位置编码(Positional Encoding)给序列注入位置信息。LayerNorm+残差连接保证训练稳定。FFN做非线性变换。",
     "hard", "Transformer,attention,深度学习"),

    ("大模型", "Prompt Engineering有哪些核心技巧？",
     "核心技巧：1) 角色设定 - 给模型明确的角色和约束；2) 思维链(CoT) - 引导模型分步推理，如\"Let's think step by step\"；3) Few-shot - 给2-5个示例；4) 结构化输出 - 明确指定输出格式(JSON/markdown)；5) 负向提示 - 明确告诉模型不要做什么；6) 分步指令 - 复杂任务拆成小步骤；7) 温度控制 - 创造性任务高温度，精确任务低温度。高级：Tree of Thoughts、Self-Consistency、ReAct。",
     "medium", "prompt,prompt engineering"),

    ("大模型", "NLU微调和Chat微调有什么区别？分别怎么操作？",
     "NLU微调(Pre-training后微调)：用标注数据做分类/序列标注等任务，用[CLS]或全连接层输出。Chat微调(SFT)：用对话数据训练，保持自回归生成。操作流程：1) NLU微调 - 加分类头→冻结部分层→训练few epochs；2) SFT - 格式化成chat模板→全参数/LoRA训练→偏好对齐(RLHF/DPO)。当前趋势：偏向直接SFT训练chat模型，NLU能力通过prompt工程利用chat模型实现。LoRA是最常用的微调方式。",
     "hard", "微调,SFT,LoRA,finetuning"),

    # 工程化篇
    ("工程化", "FastAPI中怎么整合大模型接口？需要注意什么？",
     "核心设计：1) 异步端点 async def - 避免阻塞event loop；2) Streaming响应 - 用StreamingResponse + async generator实现逐字输出；3) 连接池 - 复用HTTP连接(httpx.AsyncClient)；4) 超时控制 - 给LLM调用设timeout；5) 错误处理 - LLM可能返回错误/超时，要做好fallback；6) 限流 - 防止并发请求打爆API；7) 请求/响应模型 - 用Pydantic做输入校验。注意：大模型响应时间长，考虑WebSocket或SSE。",
     "medium", "FastAPI,API设计,流式输出"),

    ("工程化", "AI应用的Docker部署有什么最佳实践？",
     "最佳实践：1) 多阶段构建 - build阶段用完整镜像，run阶段用slim镜像；2) 模型挂载 - 大模型权重不要打进镜像，用volume挂载；3) 健康检查 - 写健康检查端点，Dockerfile加HEALTHCHECK；4) 资源限制 - --memory/--cpus防止OOM；5) 日志 - 结构化日志输出到stdout，用Docker日志驱动收集；6) 环境变量 - 配置外置，DB连接/ApiKey等通过env传入；7) docker-compose编排 - 应用+数据库+缓存一起管理。",
     "medium", "Docker,部署,运维"),

    # Python篇
    ("Python", "Python异步编程(asyncio)在AI应用中怎么用？",
     "asyncio在AI应用中的典型场景：1) 并发调用多个LLM API - asyncio.gather()并行请求；2) 流式输出 - async for逐token输出；3) Web服务 - FastAPI基于asyncio，处理高并发；4) 异步数据库 - aiomysql/asyncpg非阻塞查询。关键概念：event loop(事件循环)、coroutine(协程)、await(等待)、Task(任务)。注意：CPU密集型任务用asyncio不会加速，要用多进程。实践：httpx.AsyncClient做并发请求，semaphore控制并发数。",
     "medium", "Python,asyncio,异步编程"),
]


def seed_database():
    """预置面试问答对到数据库"""
    print(f"🔄 正在导入 {len(SEED_QUESTIONS)} 条面试题...")
    count = 0
    for category, question, answer, difficulty, skills in SEED_QUESTIONS:
        try:
            add_qa_pair(category, question, answer, difficulty, skills)
            count += 1
            print(f"  ✅ [{category}] {question[:40]}...")
        except Exception as e:
            print(f"  ❌ 导入失败: {e}")
    print(f"\n✅ 成功导入 {count}/{len(SEED_QUESTIONS)} 条面试题")
    return count


if __name__ == "__main__":
    seed_database()
