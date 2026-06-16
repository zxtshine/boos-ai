"""Agent 复合技能 — 把高频工具组合封装成一步操作，减少 LLM 循环轮次。

每个 skill 在 Python 内部顺序调用现有工具函数，不经过 LLM 编排。
"""

import json
import sys
import time
import asyncio
from pathlib import Path
from typing import Any

from .tools import (
    search_jobs, list_jobs, get_job_detail,
    analyze_jd, optimize_resume_for_jd, get_chat_suggestion,
    apply_job, batch_apply, generate_reply,
    get_status, get_stats, preview_companies,
    summarize_result,
)

# ═══════════════════════════════════════
#  Skill 1: 智能搜索 + 批量分析
# ═══════════════════════════════════════

async def smart_scan(keyword: str, city: str = "全国", top_n: int = 5) -> dict:
    """搜索岗位并对 TOP-N 结果做匹配度分析，返回按分数排名的列表。

    LLM 只需调用一步，就能拿到已筛选、已分析的岗位列表，直接做决策。
    """
    t0 = time.time()

    # Step 1: 搜索
    search_result = await search_jobs(keyword=keyword, city=city, limit=30)
    if "error" in search_result:
        return search_result
    jobs = search_result.get("jobs", [])
    if not jobs:
        return {"message": f"搜索'{keyword}'({city})无结果", "analyzed": [], "total_searched": 0}

    # Step 2: 取 top_n 个岗位，逐个补详情 + 分析
    candidates = jobs[:top_n]
    analyzed = []
    total_score = 0

    for i, job in enumerate(candidates, 1):
        url = job.get("url", "")
        if not url:
            continue

        # 获取详情（已有从 DB 读的快照，不耗网络）
        detail = await get_job_detail(job_url=url)
        if "error" in detail:
            analyzed.append({
                "rank": i, "title": job.get("title", ""),
                "company": job.get("company", ""),
                "salary": job.get("salary", ""),
                "url": url,
                "error": detail.get("error", "获取详情失败"),
            })
            continue

        # AI 匹配度分析
        match = await analyze_jd(job_url=url)
        score = match.get("match_score", 0) if "error" not in match else 0
        total_score += score

        analyzed.append({
            "rank": i,
            "title": detail.get("title", job.get("title", "")),
            "company": detail.get("company", job.get("company", "")),
            "salary": detail.get("salary", job.get("salary", "")),
            "city": detail.get("city", job.get("city", "")),
            "hr_name": detail.get("hr_name", ""),
            "hr_active": detail.get("hr_active", ""),
            "url": url,
            "match_score": score,
            "decision": match.get("decision", ""),
            "summary": match.get("summary", ""),
            "key_skills": match.get("key_skills", []),
            "gaps": match.get("gaps", []),
        })

    # 按匹配分降序
    analyzed.sort(key=lambda j: -j.get("match_score", 0))
    # 重新标 rank
    for idx, item in enumerate(analyzed, 1):
        item["rank"] = idx

    elapsed = time.time() - t0
    avg_score = total_score / len(analyzed) if analyzed else 0

    summary = (
        f"smart_scan 完成 ({elapsed:.1f}s): 搜索'{keyword}'({city}) "
        f"→ 共{len(jobs)}个岗位, 分析TOP{len(analyzed)}个, "
        f"均分{avg_score:.0f}, 最高分{analyzed[0]['match_score'] if analyzed else 0}"
    )

    return {
        "summary": summary,
        "total_searched": len(jobs),
        "analyzed_count": len(analyzed),
        "avg_match_score": round(avg_score, 1),
        "analyzed": analyzed,
    }


# ═══════════════════════════════════════
#  Skill 2: 投递前全套准备
# ═══════════════════════════════════════

async def prepare_application(job_url: str) -> dict:
    """对一个岗位完成投递前的全套分析：详情 + 匹配度 + 简历优化 + 沟通策略。

    一个调用替代 4 次 LLM 编排轮次。
    """
    t0 = time.time()

    # 1. 岗位详情
    detail = await get_job_detail(job_url=job_url)
    if "error" in detail:
        return detail

    title = detail.get("title", "")
    company = detail.get("company", "")

    # 2-4. AI 分析
    # analyze_jd 先跑，产出 gaps/key_skills/match_points
    match = await analyze_jd(job_url=job_url)
    if "error" in match:
        return {"error": f"分析失败: {match['error']}", "job_url": job_url}

    # 把分析结果传给下游，避免它们重复分析 JD
    match_ctx = {
        "gaps": match.get("gaps", []),
        "key_skills": match.get("key_skills", []),
        "match_points": match.get("match_points", []),
        "summary": match.get("summary", ""),
    }
    optimize_task = optimize_resume_for_jd(job_url=job_url, match_context=match_ctx)
    chat_task = get_chat_suggestion(job_url=job_url, match_context=match_ctx)
    optimize, chat = await asyncio.gather(optimize_task, chat_task)

    elapsed = time.time() - t0
    cached_tags = []
    if optimize.get("_cached"):
        cached_tags.append("简历优化(缓存)")
    if chat.get("_cached"):
        cached_tags.append("沟通建议(缓存)")

    return {
        "job_url": job_url,
        "title": title,
        "company": company,
        "salary": detail.get("salary", ""),
        "city": detail.get("city", ""),
        "hr_name": detail.get("hr_name", ""),
        "hr_active": detail.get("hr_active", ""),
        "description": (detail.get("description") or "")[:600],

        # 分析结果
        "match_score": match.get("match_score", 0),
        "decision": match.get("decision", ""),
        "key_skills": match.get("key_skills", []),
        "match_points": match.get("match_points", []),
        "gaps": match.get("gaps", []),
        "summary": match.get("summary", ""),

        # 简历优化
        "resume_tips": {
            "one_line": optimize.get("one_line", ""),
            "keywords_to_add": optimize.get("keywords_to_add", []),
            "action_items": optimize.get("action_items", []),
        },

        # 沟通策略
        "chat_strategy": {
            "icebreaker": chat.get("icebreaker", ""),
            "topics": [t.get("topic", "") for t in chat.get("chat_topics", [])],
            "avoid": chat.get("avoid", []),
            "follow_up": chat.get("follow_up", ""),
        },

        "_elapsed_ms": int(elapsed * 1000),
        "_cached": cached_tags,
    }


# ═══════════════════════════════════════
#  Skill 3: 智能投递（分析 + 决策 + 投递）
# ═══════════════════════════════════════

async def smart_apply(job_url: str, min_score: int = 60) -> dict:
    """分析一个岗位的匹配度，达标则自动投递，不达标则返回原因。

    等于 prepare_application + apply_job 合体，带分数门槛。
    """
    # 1. 分析
    prep = await prepare_application(job_url=job_url)
    if "error" in prep:
        return prep

    score = prep.get("match_score", 0)
    title = prep.get("title", "")
    company = prep.get("company", "")

    # 2. 决策
    if score < min_score:
        return {
            "applied": False,
            "reason": f"匹配度{score}分 低于阈值{min_score}分，跳过投递",
            "title": title,
            "company": company,
            "match_score": score,
            "summary": prep.get("summary", ""),
            "gaps": prep.get("gaps", []),
            "resume_tips": prep.get("resume_tips", {}),
            "chat_strategy": prep.get("chat_strategy", {}),
        }

    # 3. 投递
    apply_result = await apply_job(job_url=job_url)
    if "error" in apply_result:
        return {
            "applied": False,
            "reason": f"投递失败: {apply_result['error']}",
            "title": title, "company": company, "match_score": score,
        }

    return {
        "applied": apply_result.get("success", False),
        "title": title,
        "company": company,
        "match_score": score,
        "greeting": apply_result.get("greeting_used", ""),
        "message": apply_result.get("message", ""),
        "summary": prep.get("summary", ""),
        "resume_tips": prep.get("resume_tips", {}),
        "chat_strategy": prep.get("chat_strategy", {}),
    }
