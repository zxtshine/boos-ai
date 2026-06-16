"""
面试问答Agent - 数据库模块
MySQL操作 + 向量存储/检索
"""

import json
import sys
import os
import time
from datetime import datetime
from typing import List, Optional, Dict, Any

_cur_dir = os.path.dirname(os.path.abspath(__file__))
if _cur_dir not in sys.path:
    sys.path.insert(0, _cur_dir)

from mysql_config import get_conn


def _ensure_tables():
    """确保所有表存在且字段齐全。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS interview_records (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(32) NOT NULL,
                    question_id INT,
                    question TEXT,
                    user_answer TEXT,
                    score FLOAT,
                    feedback TEXT,
                    category VARCHAR(64) DEFAULT '',
                    job_focus TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_session (session_id),
                    INDEX idx_category (category),
                    INDEX idx_score (score)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            conn.commit()
    finally:
        conn.close()
    # 兼容已有表：补 category 列
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE interview_records ADD COLUMN category VARCHAR(64) DEFAULT ''")
            conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


_ensure_tables()


# ========== 面试问答对操作 ==========

def add_qa_pair(category: str, question: str, answer: str,
                difficulty: str = "medium", skills: str = "",
                source_job_id: Optional[int] = None) -> int:
    """添加面试问答对（自动生成embedding）"""
    from llm_client import get_embedding
    t0 = time.time()
    from datetime import datetime
    ts = lambda: datetime.now().strftime('%H:%M:%S')

    print(f"[{ts()}]   ├─ 🔄 生成 embedding... (nomic-embed-text)")
    t_emb = time.time()
    embedding = get_embedding(question)
    print(f"[{ts()}]   │  ✅ embedding 完成 ({time.time()-t_emb:.1f}s, dims={len(embedding)})")

    t_json = time.time()
    embedding_json = json.dumps(embedding, ensure_ascii=False)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = """INSERT INTO interview_qa_pairs
                     (category, question, answer, difficulty, embedding, related_skills, source_job_id)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)"""
            cur.execute(sql, (category, question, answer, difficulty, embedding_json, skills, source_job_id))
            conn.commit()
            rowid = cur.lastrowid
            print(f"[{ts()}]   │  💾 INSERT 完成 (id={rowid}, {time.time()-t_json:.1f}s)")
    finally:
        conn.close()

    print(f"[{ts()}]   └─ 总耗时: {time.time()-t0:.1f}s")
    return rowid


def update_qa_embedding(qa_id: int) -> None:
    """更新指定问答对的embedding"""
    from llm_client import get_embedding
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT question FROM interview_qa_pairs WHERE id = %s", (qa_id,))
            row = cur.fetchone()
            if row:
                embedding = get_embedding(row["question"])
                embedding_json = json.dumps(embedding, ensure_ascii=False)
                cur.execute("UPDATE interview_qa_pairs SET embedding = %s WHERE id = %s",
                           (embedding_json, qa_id))
                conn.commit()
    finally:
        conn.close()


def refresh_all_embeddings() -> int:
    """刷新所有问答对的embedding"""
    from llm_client import get_embedding
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, question FROM interview_qa_pairs WHERE embedding IS NULL OR embedding = ''")
            rows = cur.fetchall()
            count = 0
            for row in rows:
                embedding = get_embedding(row["question"])
                embedding_json = json.dumps(embedding, ensure_ascii=False)
                cur.execute("UPDATE interview_qa_pairs SET embedding = %s WHERE id = %s",
                           (embedding_json, row["id"]))
                count += 1
            conn.commit()
            return count
    finally:
        conn.close()


def semantic_search_qa(query: str, category: Optional[str] = None,
                       limit: int = 10) -> List[Dict[str, Any]]:
    """语义搜索最相关的面试题"""
    from llm_client import get_embedding, cosine_similarity
    query_vec = get_embedding(query)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if category:
                cur.execute(
                    "SELECT id, category, question, answer, difficulty, related_skills, embedding "
                    "FROM interview_qa_pairs WHERE embedding IS NOT NULL AND category = %s",
                    (category,)
                )
            else:
                cur.execute(
                    "SELECT id, category, question, answer, difficulty, related_skills, embedding "
                    "FROM interview_qa_pairs WHERE embedding IS NOT NULL"
                )
            rows = cur.fetchall()

        results = []
        for row in rows:
            stored_vec = json.loads(row["embedding"])
            sim = cosine_similarity(query_vec, stored_vec)
            results.append({
                "id": row["id"],
                "category": row["category"],
                "question": row["question"],
                "answer": row["answer"],
                "difficulty": row["difficulty"],
                "skills": row["related_skills"],
                "similarity": round(sim, 4),
            })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]
    finally:
        conn.close()


# ========== 岗位JD操作 ==========

def search_jobs_by_semantic(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """语义搜索匹配的岗位"""
    from llm_client import get_embedding, cosine_similarity
    t0 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在语义搜索匹配岗位... (query=\"{query[:50]}...\")")
    query_vec = get_embedding(query)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, salary, company, experience, education, "
                "requirement_category, requirement_text, source_url "
                "FROM job_requirements"
            )
            rows = cur.fetchall()

        results = []
        for row in rows:
            # 用title+requirement_text算相似度
            text = f"{row['title']} {row['requirement_category'] or ''} {row['requirement_text'] or ''}"
            text_vec = get_embedding(text[:500])  # 截取前500字符
            sim = cosine_similarity(query_vec, text_vec)
            results.append({
                "id": row["id"],
                "title": row["title"],
                "salary": row["salary"],
                "company": row["company"],
                "experience": row["experience"],
                "education": row["education"],
                "category": row["requirement_category"],
                "description": (row["requirement_text"] or "")[:800],
                "url": row["source_url"],
                "similarity": round(sim, 4),
            })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        elapsed = time.time() - t0
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 语义搜索岗位完成 (耗时: {elapsed*1000:.0f}ms, 扫描{len(rows)}个岗位, 返回{min(limit, len(results))}个)")
        return results[:limit]
    finally:
        conn.close()


def get_all_job_categories() -> List[str]:
    """获取所有岗位分类"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT requirement_category FROM job_requirements WHERE requirement_category IS NOT NULL")
            return [r["requirement_category"] for r in cur.fetchall()]
    finally:
        conn.close()


# ========== 面试记录操作 ==========

def save_interview_record(session_id: str, question_id: Optional[int],
                          question: str, user_answer: str,
                          score: float, feedback: str,
                          job_focus: str = "", category: str = "") -> int:
    """保存面试记录"""
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 💾 [DB-INSERT] 准备写入 interview_records: session={session_id}, "
          f"q=\"{question[:60]}...\", score={score}, cat={category}, ans_len={len(user_answer)}")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = """INSERT INTO interview_records
                     (session_id, question_id, question, user_answer, score, feedback, job_focus, category)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
            cur.execute(sql, (session_id, question_id, question, user_answer,
                             round(score, 1), feedback, job_focus, category))
            conn.commit()
            rid = cur.lastrowid
            print(f"[{ts}] ✅ [DB-INSERT] 写入成功 id={rid}, session={session_id}, "
                  f"q=\"{question[:50]}...\", score={score}, cat={category}")
            return rid
    finally:
        conn.close()


def get_session_summary(session_id: str) -> Dict[str, Any]:
    """获取一次面试的总结"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as total, AVG(score) as avg_score, "
                "MIN(score) as min_score, MAX(score) as max_score "
                "FROM interview_records WHERE session_id = %s",
                (session_id,)
            )
            stats = cur.fetchone()

            cur.execute(
                "SELECT question, user_answer, score, feedback, category, created_at "
                "FROM interview_records WHERE session_id = %s ORDER BY created_at",
                (session_id,)
            )
            records = cur.fetchall()

        return {
            "session_id": session_id,
            "total_questions": stats["total"],
            "avg_score": round(float(stats["avg_score"] or 0), 1),
            "min_score": float(stats["min_score"] or 0),
            "max_score": float(stats["max_score"] or 0),
            "records": records,
        }
    finally:
        conn.close()


def get_weak_areas(limit: int = 10) -> List[Dict[str, Any]]:
    """分析薄弱环节（得分最低的题目）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT question, user_answer, score, feedback, category, job_focus, created_at "
                "FROM interview_records "
                "WHERE score IS NOT NULL "
                "ORDER BY score ASC LIMIT %s",
                (limit,)
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_category_scores() -> List[Dict[str, Any]]:
    """按类别统计平均分，用于动态出题权重调整。

    返回 [{category, avg_score, count}, ...] 按平均分升序（最弱的在前）。
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT category, AVG(score) as avg_score, COUNT(*) as count
                   FROM interview_records
                   WHERE score IS NOT NULL AND category IS NOT NULL AND category != ''
                   GROUP BY category
                   ORDER BY avg_score ASC"""
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_all_session_ids() -> List[str]:
    """获取所有会话ID"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT session_id, MIN(created_at) as first_time "
                "FROM interview_records GROUP BY session_id ORDER BY first_time DESC"
            )
            return [r["session_id"] for r in cur.fetchall()]
    finally:
        conn.close()
