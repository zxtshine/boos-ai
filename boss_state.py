#!/usr/bin/env python3
"""
SQLite 数据层 —— 投递记录、聊天消息、设置、每日统计。
"""

import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path(__file__).parent / ".boss_profile" / "boss_state.db"

_local = threading.local()


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_title TEXT NOT NULL,
            company TEXT,
            company_id TEXT,
            salary TEXT,
            job_url TEXT UNIQUE NOT NULL,
            city TEXT,
            experience TEXT,
            education TEXT,
            hr_name TEXT,
            hr_title TEXT,
            hr_active TEXT DEFAULT '',
            description TEXT,
            status TEXT DEFAULT 'pending',
            greeting_text TEXT,
            greeting_sent_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

    # 迁移：已有DB补字段
    try:
        db.execute("ALTER TABLE applications ADD COLUMN hr_active TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN area_district TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN business_district TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN company_size TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN industry TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN optimize_result TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN optimize_at TIMESTAMP")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN chat_suggestion_result TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN chat_suggestion_at TIMESTAMP")
    except Exception:
        pass

    db.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER REFERENCES applications(id),
            hr_name TEXT NOT NULL,
            hr_company TEXT,
            job_title TEXT,
            last_message_text TEXT,
            last_message_from TEXT,
            last_message_at TIMESTAMP,
            unread_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            auto_reply_enabled INTEGER DEFAULT 1,
            interest_level TEXT,
            hr_wechat TEXT,
            wechat_shared_at TIMESTAMP,
            resume_sent INTEGER DEFAULT 0,
            phone_shared INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            delivery_status TEXT,
            ai_generated INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            applications_sent INTEGER DEFAULT 0,
            messages_sent INTEGER DEFAULT 0,
            messages_received INTEGER DEFAULT 0,
            auto_replies_sent INTEGER DEFAULT 0
        );
    """)
    try:
        db.execute("ALTER TABLE messages ADD COLUMN delivery_status TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN interest_level TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN hr_wechat TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN wechat_shared_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN resume_sent INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN phone_shared INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN transfer_requested INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN transfer_requested_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN company_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN has_unreplied INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_applications_company_id ON applications(company_id)")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company)")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN legal_rep TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN is_boss INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN embedding TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # 面试会话持久化（支持暂停/恢复）
    db.executescript("""
        CREATE TABLE IF NOT EXISTS interview_sessions (
            session_id TEXT PRIMARY KEY,
            job_focus TEXT DEFAULT '',
            job_context TEXT DEFAULT '',
            resume TEXT DEFAULT '',
            round_count INTEGER DEFAULT 0,
            max_rounds INTEGER DEFAULT 10,
            history_json TEXT DEFAULT '[]',
            last_question TEXT DEFAULT '',
            last_category TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 候选池表
    db.executescript("""
        CREATE TABLE IF NOT EXISTS shortlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT UNIQUE NOT NULL,
            job_title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            city TEXT,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 线下面试地点追踪表
    db.executescript("""
        CREATE TABLE IF NOT EXISTS offline_interview_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            hr_name TEXT DEFAULT '',
            hr_company TEXT DEFAULT '',
            job_title TEXT DEFAULT '',
            city TEXT DEFAULT '',
            location_detail TEXT DEFAULT '',
            hr_message TEXT DEFAULT '',
            replied INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 默认设置
    defaults = {
        "greeting_template": "您好！看到贵司在招{job_title}，很感兴趣。PS：正在和你聊天的这个AI工具是我自己开发的——就当是我的技术名片了",
        "greeting_enabled": "true",
        "ai_greeting_enabled": "true",
        "ai_reply_style": "professional",
        "daily_apply_limit": "15",
        "auto_reply_enabled": "false",
        "min_reply_delay_sec": "20",
        "max_reply_delay_sec": "40",
        "batch_delay_min_sec": "45",
        "batch_delay_max_sec": "120",
        "batch_rest_every": "8",
        "resume_summary": "",
        "wechat_id": "",
        "search_keywords": "",
        "default_city": "淄博",
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()


def _row_to_dict(row) -> Optional[dict]:
    return dict(row) if row else None


def _rows_to_list(rows) -> List[dict]:
    return [dict(r) for r in rows]


def _apply_hr_active_days(job: dict):
    """将 hr_active 字符串转为 hr_active_days / hr_active_label。"""
    import re

    raw = (job.get("hr_active") or "").strip()
    if not raw:
        return
    job["hr_active_label"] = raw
    m = re.search(r"(\d+)", raw)
    if m:
        job["hr_active_days"] = int(m.group(1))
        return
    if "今日" in raw or "刚刚" in raw or "在线" in raw:
        job["hr_active_days"] = 0
    elif "昨日" in raw or "昨天" in raw:
        job["hr_active_days"] = 1
    elif "本周" in raw:
        job["hr_active_days"] = 3
    elif "本月" in raw or "近月" in raw:
        job["hr_active_days"] = 7
    elif "半年前" in raw or "超过半年" in raw:
        job["hr_active_days"] = 180
    else:
        job["hr_active_days"] = 14


# ══════════════════════════════════════
#  Applications
# ══════════════════════════════════════


def add_application(job: dict) -> int:
    db = get_db()
    cur = db.execute(
        """INSERT OR IGNORE INTO applications
           (job_title, company, company_id, salary, job_url, city, experience, education, hr_name, hr_title, hr_active, description, legal_rep, is_boss, area_district, business_district, company_size, industry)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.get("title", ""),
            job.get("company", ""),
            job.get("company_id", ""),
            job.get("salary", ""),
            job.get("url", ""),
            job.get("city", ""),
            job.get("experience", ""),
            job.get("education", ""),
            job.get("hr_name", ""),
            job.get("hr_title", ""),
            job.get("hr_active", ""),
            job.get("description", ""),
            job.get("legal_rep", ""),
            1 if job.get("is_boss") else 0,
            job.get("area_district", ""),
            job.get("business_district", ""),
            job.get("company_size", ""),
            job.get("industry", ""),
        ),
    )
    db.commit()
    return cur.lastrowid if cur.lastrowid else 0


def get_application(app_id: int) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())


def get_application_by_url(url: str) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM applications WHERE job_url=?", (url,)).fetchone())


def update_application_from_job(app_id: int, job: dict) -> Optional[dict]:
    """用本次搜索结果刷新已有岗位；空值不覆盖旧值。"""
    fields = {
        "job_title": job.get("title", ""),
        "company": job.get("company", ""),
        "company_id": job.get("company_id", ""),
        "salary": job.get("salary", ""),
        "city": job.get("city", ""),
        "experience": job.get("experience", ""),
        "education": job.get("education", ""),
        "hr_name": job.get("hr_name", ""),
        "hr_title": job.get("hr_title", ""),
        "hr_active": job.get("hr_active", ""),
        "description": job.get("description", ""),
        "area_district": job.get("area_district", ""),
        "business_district": job.get("business_district", ""),
        "company_size": job.get("company_size", ""),
        "industry": job.get("industry", ""),
    }
    params = []
    assignments = []
    for column, value in fields.items():
        value = (value or "").strip()
        assignments.append(f"{column}=CASE WHEN ?!='' THEN ? ELSE {column} END")
        params.extend([value, value])
    params.append(app_id)

    db = get_db()
    db.execute(
        f"""UPDATE applications SET {", ".join(assignments)},
            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        params,
    )
    db.commit()
    return get_application(app_id)


def list_applications(status: Optional[str] = None, limit: int = 50) -> List[dict]:
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM applications WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM applications ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    result = _rows_to_list(rows)
    for j in result:
        _apply_hr_active_days(j)
    return result


def update_application_status(app_id: int, status: str, greeting_text: Optional[str] = None):
    db = get_db()
    if greeting_text:
        db.execute(
            """UPDATE applications SET status=?, greeting_text=?, greeting_sent_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, greeting_text, app_id),
        )
    else:
        db.execute(
            "UPDATE applications SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, app_id),
        )
    db.commit()


def get_today_application_count() -> int:
    row = (
        get_db()
        .execute("SELECT COUNT(*) as cnt FROM applications WHERE date(greeting_sent_at)=date('now','localtime')")
        .fetchone()
    )
    return row["cnt"] if row else 0


def get_today_pending_count() -> int:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM applications WHERE status='pending'").fetchone()
    return row["cnt"] if row else 0


def count_hours_replied_in_range(hours: int) -> int:
    row = (
        get_db()
        .execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE last_message_from='hr' AND datetime(last_message_at) > datetime('now','localtime',? || ' hours')",
            (f"-{hours}",),
        )
        .fetchone()
    )
    return row["cnt"] if row else 0


def count_interest_level(level: str) -> int:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM conversations WHERE interest_level=?", (level,)).fetchone()
    return row["cnt"] if row else 0


def get_pending_applications(limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM applications WHERE status='pending' AND job_url!='' ORDER BY id LIMIT ?",
            (limit,),
        )
        .fetchall()
    )


def list_jobs_by_company(company_id: str = "", company: str = "") -> List[dict]:
    """按 company_id 或 company 名返回该公司下所有已入库的岗位。
    优先用 company_id；为空时用 company 名兜底。"""
    db = get_db()
    if company_id:
        rows = db.execute(
            "SELECT * FROM applications WHERE company_id=? ORDER BY id DESC",
            (company_id,),
        ).fetchall()
        if rows:
            return _rows_to_list(rows)
    if company:
        return _rows_to_list(
            db.execute(
                "SELECT * FROM applications WHERE company=? ORDER BY id DESC",
                (company,),
            ).fetchall()
        )
    return []


def list_companies_by_position_count(min_count: int = 1, limit: int = 50) -> List[dict]:
    """按公司聚合，统计 distinct job_url 数倒序，返回 [{company, company_id, position_count, latest_job_id}]。
    company_id 为空的公司会被单独归到 (company, '')。"""
    db = get_db()
    rows = db.execute(
        """SELECT company, company_id, COUNT(DISTINCT job_url) AS position_count, MAX(id) AS latest_job_id
           FROM applications
           WHERE company != '' AND job_url != ''
           GROUP BY company, COALESCE(NULLIF(company_id, ''), company)
           HAVING position_count >= ?
           ORDER BY position_count DESC, latest_job_id DESC
           LIMIT ?""",
        (min_count, limit),
    ).fetchall()
    return _rows_to_list(rows)


def company_already_applied(company: str = "", company_id: str = "") -> bool:
    """该公司下是否已经有 status in (applied, replied) 的记录。"""
    db = get_db()
    if company_id:
        row = db.execute(
            "SELECT 1 FROM applications WHERE company_id=? AND status IN ('applied','replied') LIMIT 1",
            (company_id,),
        ).fetchone()
        if row:
            return True
    if company:
        row = db.execute(
            "SELECT 1 FROM applications WHERE company=? AND status IN ('applied','replied') LIMIT 1",
            (company,),
        ).fetchone()
        return bool(row)
    return False


def save_job_embedding(job_url: str, embedding: list):
    """存储 JD 的 embedding 向量（JSON 格式）。"""
    import json
    db = get_db()
    db.execute(
        "UPDATE applications SET embedding=? WHERE job_url=?",
        (json.dumps(embedding, ensure_ascii=False), job_url),
    )
    db.commit()


def get_all_job_embeddings() -> list:
    """返回所有已存储 embedding 的岗位摘要 + HR 反馈信号。

    每条返回: {job_url, job_title, company, description, embedding, optimize_result,
               chat_suggestion_result, greeting_text, status, interest_level, resume_sent,
               wechat_shared}
    embedding 从 JSON 反序列化为 list[float]。
    """
    import json
    db = get_db()
    rows = db.execute(
        """SELECT a.job_url, a.job_title, a.company, a.description, a.embedding,
                  a.optimize_result, a.chat_suggestion_result, a.greeting_text, a.status,
                  c.interest_level, c.resume_sent, c.hr_wechat, c.wechat_shared_at
           FROM applications a
           LEFT JOIN conversations c ON c.application_id = a.id
           WHERE a.embedding IS NOT NULL AND a.embedding != ''
           ORDER BY a.id DESC""",
    ).fetchall()
    result = []
    for r in rows:
        emb_str = r["embedding"]
        if not emb_str:
            continue
        try:
            emb = json.loads(emb_str)
        except Exception:
            continue
        if not emb or len(emb) < 16:
            continue
        result.append({
            "job_url": r["job_url"],
            "job_title": r["job_title"],
            "company": r["company"],
            "description": (r["description"] or "")[:500],
            "embedding": emb,
            "optimize_result": r["optimize_result"] or "",
            "chat_suggestion_result": r["chat_suggestion_result"] or "",
            "greeting_text": r["greeting_text"] or "",
            "status": r["status"] or "",
            "interest_level": r["interest_level"] or "",
            "resume_sent": bool(r["resume_sent"]),
            "wechat_shared": bool(r["hr_wechat"] and r["wechat_shared_at"]),
        })
    return result


# ══════════════════════════════════════
#  Conversations
# ══════════════════════════════════════


def get_or_create_conversation(application_id: int, hr_name: str, hr_company: str, job_title: str) -> int:
    db = get_db()
    if application_id:
        row = db.execute("SELECT id FROM conversations WHERE application_id=?", (application_id,)).fetchone()
        if row:
            return row["id"]
    # 按 HR 名字查重（精确匹配，去空白）
    name = hr_name.strip() if hr_name else ""
    if name:
        row = db.execute("SELECT id FROM conversations WHERE hr_name=? AND status!='closed'", (name,)).fetchone()
        if row:
            return row["id"]
    cur = db.execute(
        """INSERT INTO conversations (application_id, hr_name, hr_company, job_title)
           VALUES (?, ?, ?, ?)""",
        (application_id, name, hr_company, job_title),
    )
    db.commit()
    return cur.lastrowid


def get_conversation(conv_id: int) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone())


def list_active_conversations() -> List[dict]:
    return _rows_to_list(
        get_db().execute("SELECT * FROM conversations WHERE status!='closed' ORDER BY updated_at DESC").fetchall()
    )


def list_unreplied_conversations() -> List[dict]:
    """返回 has_unreplied=1 且 status=active 且 auto_reply_enabled=1 的会话"""
    return _rows_to_list(
        get_db().execute(
            "SELECT * FROM conversations WHERE has_unreplied=1 AND status='active' AND auto_reply_enabled=1 ORDER BY updated_at DESC"
        ).fetchall()
    )


def find_conversation_by_hr_name(hr_name: str) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .execute(
            "SELECT * FROM conversations WHERE hr_name=? ORDER BY updated_at DESC LIMIT 1",
            (hr_name,),
        )
        .fetchone()
    )


def update_conversation_last_message(conv_id: int, text: str, sender: str, unread_delta: int = 0):
    db = get_db()
    db.execute(
        """UPDATE conversations SET last_message_text=?, last_message_from=?,
           last_message_at=CURRENT_TIMESTAMP, unread_count=MAX(0, unread_count+?),
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (text[:200], sender, unread_delta, conv_id),
    )
    db.commit()


def update_conversation_status(conv_id: int, status: str):
    get_db().execute(
        "UPDATE conversations SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, conv_id),
    )
    get_db().commit()


def update_conversation_interest(conv_id: int, level: str):
    get_db().execute(
        "UPDATE conversations SET interest_level=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (level, conv_id),
    )
    get_db().commit()


def update_conversation_wechat(conv_id: int, wechat_id: str):
    get_db().execute(
        "UPDATE conversations SET hr_wechat=?, wechat_shared_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (wechat_id, conv_id),
    )
    get_db().commit()


def mark_resume_sent(conv_id: int):
    get_db().execute("UPDATE conversations SET resume_sent=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
    get_db().commit()


def mark_phone_shared(conv_id: int):
    get_db().execute("UPDATE conversations SET phone_shared=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
    get_db().commit()


def get_wechat_exchanges() -> List[dict]:
    """返回所有已获取到微信号的会话，包含岗位详情。"""
    return _rows_to_list(
        get_db()
        .execute(
            """SELECT c.id, c.hr_name, c.hr_company, c.job_title, c.hr_wechat,
                      c.wechat_shared_at, c.interest_level,
                      a.city, a.salary, a.experience, a.education, a.description
               FROM conversations c
               LEFT JOIN applications a ON c.application_id = a.id
               WHERE c.hr_wechat IS NOT NULL AND c.hr_wechat != ''
               ORDER BY c.wechat_shared_at DESC"""
        )
        .fetchall()
    )


def update_conversation_transfer_requested(conv_id: int):
    get_db().execute(
        "UPDATE conversations SET transfer_requested=1, transfer_requested_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (conv_id,),
    )
    get_db().commit()


def get_transfer_requests() -> List[dict]:
    """返回所有转人工请求的会话，包含岗位详情。"""
    return _rows_to_list(
        get_db()
        .execute(
            """SELECT c.id, c.hr_name, c.hr_company, c.job_title, c.last_message_text,
                      c.transfer_requested_at, c.interest_level,
                      a.city, a.salary, a.experience, a.education, a.description
               FROM conversations c
               LEFT JOIN applications a ON c.application_id = a.id
               WHERE c.transfer_requested = 1
               ORDER BY c.transfer_requested_at DESC"""
        )
        .fetchall()
    )


def set_auto_reply(conv_id: int, enabled: bool):
    get_db().execute(
        "UPDATE conversations SET auto_reply_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (1 if enabled else 0, conv_id),
    )
    get_db().commit()


# ══════════════════════════════════════
#  Messages
# ══════════════════════════════════════


def add_message(
    conversation_id: int, sender: str, content: str, ai_generated: bool = False, delivery_status: str = ""
) -> int:
    db = get_db()
    cur = db.execute(
        "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, sender, content, delivery_status, 1 if ai_generated else 0),
    )
    # 我发消息后标记为已回复
    if sender == "me":
        db.execute(
            "UPDATE conversations SET has_unreplied=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (conversation_id,),
        )
    db.commit()
    return cur.lastrowid


def get_messages(conversation_id: int, limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC, id ASC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def get_recent_messages(conversation_id: int, limit: int = 5) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def replace_conversation_messages(conversation_id: int, messages: List[dict]):
    """用 BOSS 当前消息历史覆盖本地缓存，避免 Web 端展示过期或错会话内容。"""
    db = get_db()
    old_ai = {
        r["content"]
        for r in db.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND ai_generated=1",
            (conversation_id,),
        ).fetchall()
    }
    db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    for msg in messages:
        sender = msg.get("sender", "hr")
        content = (msg.get("content") or "").strip()
        delivery_status = (msg.get("status") or msg.get("delivery_status") or "").strip()
        if not content:
            continue
        ai_generated = 1 if sender == "me" and content in old_ai else 0
        db.execute(
            "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, sender, content, delivery_status, ai_generated),
        )
    # 自动判断: 最后一条非系统HR消息之后是否有"me"的回复
    _system_prefixes = (
        "你与该职位竞争者PK情况", "竞争力分析", "BOSS安全提示",
        "系统消息", "沟通分析", "今日推荐", "该Boss已查看了你的简历",
        "对方已查看了您的附件简历", "附件简历请求",
        "对方已同意，您的附件简历已发送给对方",
    )
    _has_unreplied = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("sender") != "hr":
            continue
        _c = m.get("content") or ""
        if _c.startswith(_system_prefixes) or "已发送给Boss" in _c or "点击预览附件简历" in _c:
            continue
        _has_reply = any(
            messages[j].get("sender") == "me"
            for j in range(i + 1, len(messages))
        )
        _has_unreplied = 0 if _has_reply else 1
        break
    db.execute(
        "UPDATE conversations SET has_unreplied=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (_has_unreplied, conversation_id),
    )
    db.commit()


def get_last_hr_message(conversation_id: int) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? AND sender='hr' ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        )
        .fetchone()
    )


def message_exists(conversation_id: int, content: str, sender: str) -> bool:
    row = (
        get_db()
        .execute(
            "SELECT id FROM messages WHERE conversation_id=? AND content=? AND sender=? ORDER BY created_at DESC LIMIT 1",
            (conversation_id, content, sender),
        )
        .fetchone()
    )
    return row is not None


# ══════════════════════════════════════
#  Settings
# ══════════════════════════════════════


def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    get_db().execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )
    get_db().commit()


def get_all_settings() -> dict:
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ══════════════════════════════════════
#  Daily Stats
# ══════════════════════════════════════


def _today() -> str:
    return date.today().isoformat()


def _ensure_today():
    get_db().execute("INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (_today(),))
    get_db().commit()


def increment_daily_stat(field: str):
    _ensure_today()
    get_db().execute(
        f"UPDATE daily_stats SET {field} = {field} + 1 WHERE date=?",
        (_today(),),
    )
    get_db().commit()


def get_daily_stats(date_str: Optional[str] = None) -> dict:
    d = date_str or _today()
    row = get_db().execute("SELECT * FROM daily_stats WHERE date=?", (d,)).fetchone()
    return dict(row) if row else {}


def get_today_auto_reply_count() -> int:
    row = (
        get_db()
        .execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE ai_generated=1 AND date(created_at)=date('now','localtime')"
        )
        .fetchone()
    )
    return row["cnt"] if row else 0


# ═══════════════════════
#  候选池
# ═══════════════════════
def add_to_shortlist(
    job_url: str, title: str, company: str = "", salary: str = "", city: str = "", note: str = ""
) -> int:
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO shortlists (job_url, job_title, company, salary, city, note) VALUES (?,?,?,?,?,?)",
            (job_url, title, company, salary, city, note),
        )
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return 0


def remove_from_shortlist(shortlist_id: int):
    get_db().execute("DELETE FROM shortlists WHERE id=?", (shortlist_id,))
    get_db().commit()


def list_shortlists(limit: int = 100) -> list:
    rows = get_db().execute("SELECT * FROM shortlists ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return _rows_to_list(rows)


def is_in_shortlist(job_url: str) -> bool:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM shortlists WHERE job_url=?", (job_url,)).fetchone()
    return row["cnt"] > 0 if row else False


# ══════════════════════════════════════
#  面试会话持久化
# ══════════════════════════════════════

def save_interview_session(
    session_id: str,
    job_focus: str = "",
    job_context: str = "",
    resume: str = "",
    round_count: int = 0,
    max_rounds: int = 10,
    history_json: str = "[]",
    last_question: str = "",
    last_category: str = "",
    status: str = "active",
):
    db = get_db()
    db.execute(
        """INSERT OR REPLACE INTO interview_sessions
           (session_id, job_focus, job_context, resume, round_count, max_rounds,
            history_json, last_question, last_category, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (session_id, job_focus, job_context, resume, round_count, max_rounds,
         history_json, last_question, last_category, status),
    )
    db.commit()


def get_interview_session(session_id: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM interview_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def list_active_interview_sessions() -> list:
    rows = get_db().execute(
        "SELECT session_id, job_focus, round_count, status, created_at, updated_at "
        "FROM interview_sessions WHERE status='active' ORDER BY updated_at DESC"
    ).fetchall()
    return _rows_to_list(rows)


def list_all_interview_sessions() -> list:
    rows = get_db().execute(
        "SELECT session_id, job_focus, round_count, status, created_at, updated_at "
        "FROM interview_sessions ORDER BY updated_at DESC"
    ).fetchall()
    return _rows_to_list(rows)


def mark_interview_ended(session_id: str):
    db = get_db()
    db.execute(
        "UPDATE interview_sessions SET status='ended', updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
        (session_id,),
    )
    db.commit()


# ══════════════════════════════════════
#  线下面试地点追踪
# ══════════════════════════════════════

# 常见城市列表（用于从消息中提取地点）
_KNOWN_CITIES = [
    "上海", "北京", "深圳", "广州", "杭州", "成都", "武汉", "南京", "西安",
    "重庆", "苏州", "天津", "长沙", "郑州", "东莞", "青岛", "合肥", "佛山",
    "宁波", "昆明", "沈阳", "大连", "福州", "厦门", "济南", "无锡", "南宁",
    "长春", "泉州", "贵阳", "南昌", "常州", "太原", "烟台", "嘉兴", "南通",
    "金华", "珠海", "惠州", "徐州", "海口", "乌鲁木齐", "兰州", "中山",
    "绍兴", "温州", "潍坊", "哈尔滨", "淄博", "临沂", "台州", "湖州",
    "芜湖", "镇江", "扬州", "盐城", "泰州", "襄阳", "宜昌", "洛阳",
]

# 线下面试关键词
_OFFLINE_INTERVIEW_KEYWORDS = [
    "线下面试", "线下", "现场面试", "到面", "到场面试", "实地面试", "面对面",
    "过来面试", "来面试", "到公司面试", "面聊", "见面聊聊", "面谈",
    "线下面聊", "线下沟通", "实地面聊", "到场", "到公司聊", "当面聊聊",
    "当面面试", "线下一面", "线下二面", "线下面", "实地", "线下笔试",
    "来公司", "到司面试", "上门面试", "线下复试", "线下面试地点",
    "面试地点", "面试地址", "线下面试地址", "线下详聊", "到现场", "公司地址",
]


def _extract_city_from_text(text: str) -> tuple:
    """从文本中提取城市和地点详情。返回 (city, location_detail)。"""
    if not text:
        return ("", "")
    found_cities = []
    for city in _KNOWN_CITIES:
        if city in text:
            found_cities.append(city)
    if not found_cities:
        return ("", "")
    # 取最后出现的城市（通常是具体地点）
    city = found_cities[-1]
    # 尝试提取更具体的地点信息（区/街道/大厦等）
    detail = ""
    detail_patterns = [
        r'(?:在|到|去|地址[：:]?\s*)([一-龥]{2,20}(?:区|路|街|道|镇|园|大厦|广场|中心|楼|层|号))',
        r'([一-龥]{2,10}(?:区|街道|镇|园区|开发区))',
        r'(?:地点|地址|位置)[：:]\s*([^\n，。,]{2,50})',
    ]
    for pat in detail_patterns:
        m = re.search(pat, text)
        if m:
            detail = m.group(1).strip()
            break
    if not detail and len(found_cities) >= 2:
        detail = found_cities[0]
    return (city, detail)


def _detect_offline_interview_requirement(message: str) -> bool:
    """检测HR消息是否要求线下面试。"""
    if not message:
        return False
    return any(kw in message for kw in _OFFLINE_INTERVIEW_KEYWORDS)


def save_offline_interview_location(
    conversation_id: int,
    hr_name: str = "",
    hr_company: str = "",
    job_title: str = "",
    city: str = "",
    location_detail: str = "",
    hr_message: str = "",
) -> int:
    """保存线下面试地点记录。已存在则更新。返回记录ID。"""
    db = get_db()
    # 检查是否已存在相同会话的记录
    existing = db.execute(
        "SELECT id FROM offline_interview_locations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE offline_interview_locations
               SET hr_name=?, hr_company=?, job_title=?, city=?,
                   location_detail=?, hr_message=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (hr_name, hr_company, job_title, city, location_detail, hr_message, existing["id"]),
        )
        db.commit()
        return existing["id"]
    cur = db.execute(
        """INSERT INTO offline_interview_locations
           (conversation_id, hr_name, hr_company, job_title, city, location_detail, hr_message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (conversation_id, hr_name, hr_company, job_title, city, location_detail, hr_message),
    )
    db.commit()
    return cur.lastrowid


def list_offline_interview_locations() -> list:
    """获取所有线下面试地点，按城市分组排序。"""
    rows = get_db().execute(
        """SELECT * FROM offline_interview_locations
           ORDER BY city, updated_at DESC"""
    ).fetchall()
    return _rows_to_list(rows)


def get_offline_locations_grouped() -> dict:
    """获取按城市分组的线下面试地点。"""
    locations = list_offline_interview_locations()
    grouped = {}
    for loc in locations:
        city = loc.get("city", "") or "未分类"
        if city not in grouped:
            grouped[city] = []
        grouped[city].append(loc)
    # 按每个城市的记录数降序排列
    return dict(sorted(grouped.items(), key=lambda x: len(x[1]), reverse=True))


def mark_offline_location_replied(location_id: int):
    """标记线下面试地点记录已回复。"""
    get_db().execute(
        "UPDATE offline_interview_locations SET replied=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (location_id,),
    )
    get_db().commit()


# 启动时初始化
init_db()
