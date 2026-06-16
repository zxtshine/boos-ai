#!/usr/bin/env python3
"""历史 JD 的 RAG 检索 —— 发现与当前岗位相似的历史投递，复用优化建议和招呼语经验。"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "interview"))
from llm_client import get_embedding

from boss_state import get_all_job_embeddings, save_job_embedding, get_application_by_url

SIMILARITY_THRESHOLD = 0.55  # 低于此相似度的历史 JD 不纳入参考


def _cosine_similarity(a: list, b: list) -> float:
    """余弦相似度，numpy 一把梭。"""
    a_arr = np.array(a, dtype=float)
    b_arr = np.array(b, dtype=float)
    na, nb = np.linalg.norm(a_arr), np.linalg.norm(b_arr)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / (na * nb))


def similar_jds(description: str, limit: int = 5, exclude_url: str = "") -> list[dict]:
    """给定一段 JD 文本，返回历史中最相似的岗位列表。

    每条包含: job_url, job_title, company, similarity,
              optimize_result, chat_suggestion_result, greeting_text, status
    """
    if not description or len(description.strip()) < 20:
        return []

    t0 = time.time()
    desc_preview = description[:60].replace("\n", " ")
    print(f"[RAG] 正在检索相似历史JD... (desc_len={len(description)}, preview=\"{desc_preview}...\")")

    query_vec = get_embedding(description[:1000])
    all_jobs = get_all_job_embeddings()

    if not all_jobs:
        print(f"[RAG] 历史 JD 库为空，跳过 ({(time.time()-t0)*1000:.0f}ms)")
        return []

    results = []
    for job in all_jobs:
        if exclude_url and job["job_url"] == exclude_url:
            continue
        sim = _cosine_similarity(query_vec, job["embedding"])
        if sim >= SIMILARITY_THRESHOLD:
            job["similarity"] = round(sim, 4)
            del job["embedding"]  # 不往外传向量
            results.append(job)

    results.sort(key=lambda x: x["similarity"], reverse=True)
    top = results[:limit]

    elapsed = (time.time() - t0) * 1000
    if top:
        best = top[0]
        print(f"[RAG] 检索完成 ({elapsed:.0f}ms, 扫描{len(all_jobs)}条, 命中{len(results)}条, "
              f"top={best['job_title']}@{best['company']} sim={best['similarity']})")
    else:
        print(f"[RAG] 检索完成 ({elapsed:.0f}ms, 扫描{len(all_jobs)}条, 无高相似命中)")

    return top


def ensure_jd_embedding(job_url: str, description: str) -> list | None:
    """确保指定 JD 有 embedding，没有则生成并存库。返回向量或 None。"""
    if not description or len(description.strip()) < 20:
        return None
    existing = get_application_by_url(job_url)
    if existing and (existing.get("embedding") or "").strip():
        import json
        try:
            return json.loads(existing["embedding"])
        except Exception:
            pass
    vec = get_embedding(description[:1000])
    save_job_embedding(job_url, vec)
    return vec


def build_rag_context(similar: list[dict], context_type: str = "optimize") -> str:
    """将相似历史 JD 格式化为 LLM few-shot 上下文，含 HR 真实反馈信号。

    context_type:
      - "optimize": 重点引用历史优化建议
      - "greeting":  重点引用历史招呼语 + HR反馈(回复/兴趣/要简历/加微信)
      - "chat":      重点引用历史沟通建议
    """
    if not similar:
        return ""

    parts = ["## 参考：历史上相似的岗位投递经验"]
    parts.append("以下是你之前投递过的与当前岗位语义相似的历史记录，参考其经验来优化本次输出：\n")

    for i, job in enumerate(similar, 1):
        status = job.get("status", "")
        status_label = {
            "applied": "已投递(HR未读)", "replied": "HR已回复",
            "pending": "待投递", "greeting_sent": "已打招呼(HR未回)",
            "ignored": "已忽略",
        }.get(status, status)

        parts.append(f"### 历史相似岗位 {i}：{job['job_title']} @ {job.get('company', '?')}")
        parts.append(f"- 语义相似度: {job['similarity']}, 投递状态: {status_label}")

        # HR 反馈信号（招呼语和沟通建议模式重点关注）
        if context_type in ("greeting", "chat"):
            # 正向信号
            positive_signals = []
            if job.get("interest_level") == "high":
                positive_signals.append("HR 兴趣度=高(high)，说明当时话题打中了对方")
            elif job.get("interest_level") == "medium":
                positive_signals.append("HR 兴趣度=中(medium)，对方有基本交流意愿")
            elif job.get("interest_level") == "low":
                positive_signals.append("⚠ HR 兴趣度=低(low)，对方可能只是客套")

            if job.get("resume_sent"):
                positive_signals.append("HR 主动要了简历 —— 说明招呼语+沟通成功激发了对方的兴趣")
            if job.get("wechat_shared"):
                positive_signals.append("HR 交换了微信 —— 谈话已经推进到线下")

            if positive_signals:
                parts.append(f"  **HR反馈**: {'；'.join(positive_signals)}")

            # 招呼语原文（如果是 greeting 模式直接展示）
            gt = job.get("greeting_text") or ""
            if gt and len(gt) > 5:
                parts.append(f"  **当时发出的招呼语**: \"{gt[:300]}\"")
                # 分析：如果这招呼语效果很好，提示LLM可以模仿
                if job.get("interest_level") == "high" or job.get("resume_sent"):
                    parts.append(f"  👆 这份招呼语效果很好，可以借鉴其风格和切入点")

        elif context_type == "optimize":
            opt = job.get("optimize_result") or ""
            if opt and len(opt) > 20:
                parts.append(f"- 当时的简历优化建议: {opt[:600]}")

        if context_type == "chat":
            sug = job.get("chat_suggestion_result") or ""
            if sug and len(sug) > 20:
                parts.append(f"- 当时的沟通建议: {sug[:600]}")

        parts.append("")

    # 结尾提示
    if context_type == "greeting":
        parts.append("**要点**：参考效果好的历史招呼语（特别是HR要了简历/加微信/高兴趣度的），模仿其切入点和表达风格，但内容要根据当前JD定制。避免效果差的历史模式。\n")
    else:
        parts.append("参考以上历史经验，但不要机械照搬。结合当前JD的实际内容进行调整。\n")
    return "\n".join(parts)