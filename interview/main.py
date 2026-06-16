"""
面试问答Agent - FastAPI Web服务
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import uuid

from engine import InterviewEngine
from db import (semantic_search_qa, add_qa_pair, get_all_job_categories,
                get_session_summary, get_weak_areas, get_all_session_ids,
                search_jobs_by_semantic, refresh_all_embeddings)
from fast_qa import fast_answer, query_cache as qa_cache

app = FastAPI(title="面试问答Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 活跃的面试会话
active_sessions: Dict[str, InterviewEngine] = {}


# ===== 数据模型 =====

class StartInterviewRequest(BaseModel):
    job_focus: str = ""
    use_local_model: bool = False


class AddQARequest(BaseModel):
    category: str
    question: str
    answer: str
    difficulty: str = "medium"
    skills: str = ""


# ===== 面试接口 =====

@app.post("/api/interview/start")
def start_interview(req: StartInterviewRequest):
    """开始新的面试会话"""
    engine = InterviewEngine(job_focus=req.job_focus, use_local_model=req.use_local_model)
    session_id = engine.session_id
    active_sessions[session_id] = engine

    result = engine.start()

    return {
        "session_id": session_id,
        "message": result["message"],
        "question_count": result["question_count"],
        "job_focus": req.job_focus or "",
        "job_context": engine.job_context,
    }


@app.post("/api/interview/chat")
def interview_chat(data: dict):
    """对话式面试：发送用户消息，获取面试官回复"""
    session_id = data.get("session_id", "")
    engine = active_sessions.get(session_id)
    if not engine:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    message = data.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    reply = engine.chat(message)
    return reply


@app.post("/api/interview/end")
def end_interview(data: dict):
    """结束面试"""
    session_id = data.get("session_id", "")
    engine = active_sessions.pop(session_id, None)
    if not engine:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    summary = engine.end_session()
    return {"summary": summary}


# ===== 学习模式接口（快速问答） =====

class LearnQuestion(BaseModel):
    question: str


@app.post("/api/learn/ask")
def learn_ask(req: LearnQuestion):
    """学习模式：快速回答问题（2-3秒）"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    
    result = fast_answer(req.question)
    return {
        "question": req.question,
        "answer": result["answer"],
        "matched_question": result.get("matched", ""),
        "category": result.get("category", ""),
        "topic": result.get("topic"),
        "confidence": result.get("confidence", "high"),
        "note": result.get("note", ""),
        "layer": result.get("layer", -1),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "related": result.get("related", []),
    }


@app.get("/api/learn/search")
def learn_search(query: str, limit: int = 5):
    """学习模式：搜索相关问题（联想搜索）"""
    from fast_qa import fulltext_search, semantic_search
    
    # 优先全文检索
    ft = fulltext_search(query, limit)
    if ft:
        return {"results": ft[:limit], "source": "fulltext"}
    
    # 语义检索
    sem = semantic_search(query, limit=limit, threshold=0.5)
    if sem:
        return {"results": sem[:limit], "source": "semantic"}
    
    return {"results": [], "source": "none"}


@app.post("/api/learn/cache-clear")
def clear_qa_cache():
    """清空问答缓存"""
    qa_cache.clear()
    return {"message": "缓存已清空"}


# ===== 知识库接口 =====

@app.get("/api/qa/search")
def search_qa(query: str, category: Optional[str] = None, limit: int = 10):
    """语义搜索面试题"""
    results = semantic_search_qa(query, category, limit)
    return {"results": results}


@app.post("/api/qa/add")
def add_qa(req: AddQARequest):
    """添加面试问答对"""
    qa_id = add_qa_pair(
        category=req.category,
        question=req.question,
        answer=req.answer,
        difficulty=req.difficulty,
        skills=req.skills,
    )
    return {"id": qa_id, "message": "添加成功"}


@app.get("/api/qa/categories")
def list_categories():
    """获取所有分类"""
    qa_categories = ["编程语言", "数据库", "系统设计", "工程化", "前端", "AI/ML", "数据处理", "安全"]
    job_categories = get_all_job_categories()
    return {
        "qa_categories": qa_categories,
        "job_categories": [c for c in job_categories if c],
    }


# ===== 岗位接口 =====

@app.get("/api/jobs/search")
def search_jobs(query: str, limit: int = 5):
    """语义搜索岗位"""
    results = search_jobs_by_semantic(query, limit)
    return {"results": results}


# ===== 历史记录 =====

@app.get("/api/review/sessions")
def list_sessions():
    """获取所有面试会话"""
    sessions = get_all_session_ids()
    return {"sessions": sessions}


@app.get("/api/review/session/{session_id}")
def get_session(session_id: str):
    """获取某个会话的详细记录"""
    summary = get_session_summary(session_id)
    return summary


@app.get("/api/review/weak-areas")
def weak_areas(limit: int = 10):
    """薄弱环节分析"""
    areas = get_weak_areas(limit)
    return {"weak_areas": areas}


# ===== 管理接口 =====

@app.post("/api/admin/refresh-embeddings")
def refresh_embeddings():
    """刷新所有embedding"""
    count = refresh_all_embeddings()
    return {"refreshed": count, "message": f"刷新了{count}条embedding"}


@app.get("/api/health")
def health():
    """健康检查"""
    return {"status": "ok", "llm": "qwen2.5:14b (ollama)", "embedding": "nomic-embed-text (ollama)"}


# ===== 前端页面 =====

from fastapi.responses import HTMLResponse
from pathlib import Path

@app.get("/", response_class=HTMLResponse)
def index():
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>面试问答Agent</h1><p>前端页面未找到</p>")
