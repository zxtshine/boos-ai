#!/usr/bin/env python3
"""
BOSS直聘自动化控制台 —— FastAPI 后端
提供 REST API + WebSocket + 后台监控循环。
用法: python boss_app.py --port 8000
"""

import argparse
import asyncio
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from boss_automation import BossAutomation
from boss_state import (
    add_application,
    get_application,
    get_application_by_url,
    update_application_from_job,
    list_applications,
    update_application_status,
    get_today_application_count,
    get_or_create_conversation,
    get_conversation,
    list_active_conversations,
    add_message,
    get_messages,
    replace_conversation_messages,
    update_conversation_last_message,
    update_conversation_status,
    set_auto_reply,
    get_setting,
    set_setting,
    get_all_settings,
    get_daily_stats,
    get_wechat_exchanges,
    get_transfer_requests,
    get_today_pending_count,
    count_hours_replied_in_range,
    count_interest_level,
    add_to_shortlist,
    remove_from_shortlist,
    list_shortlists,
    is_in_shortlist,
    list_jobs_by_company,
    list_companies_by_position_count,
    company_already_applied,
)
from boss_replier import generate_greeting, generate_greeting_ai
from boss_company import build_company_preview, rank_companies_by_position_count
from agent.tools import ToolRegistry, ToolContext, register_all
from agent.loop import AgentLoop

# ── FastAPI 应用 ──
app = FastAPI(title="BOSS直聘自动化控制台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ── 全局状态 ──
automation: Optional[BossAutomation] = None
monitor_task: Optional[asyncio.Task] = None
ws_clients: List[WebSocket] = []
monitor_paused: bool = False
browser_sync_lock: Optional[asyncio.Lock] = None
_cached_zp_headers: dict = {}  # zp_token + token + Cookie，geo 端点免 _run_pw
_HEADERS_CACHE_TTL = 15 * 60  # 15 分钟
interview_sessions: dict = {}  # session_id → InterviewEngine


def _refresh_zp_cache():
    """在 Playwright executor 中提取 zp_token / token / Cookie 并写入全局缓存。"""
    global _cached_zp_headers
    try:
        auth = {}
        if hasattr(automation, "_extract_zp_headers"):
            auth = automation._extract_zp_headers() or {}
        h = {}
        if auth.get("zp_token"):
            h["zp_token"] = auth["zp_token"]
        if auth.get("token"):
            h["token"] = auth["token"]
        try:
            cookies = automation.page.context.cookies(["https://www.zhipin.com", "https://.zhipin.com"])
            h["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value"))
        except Exception:
            pass
        h["_ts"] = time.time()
        _cached_zp_headers = h
    except Exception:
        pass


def _get_cached_headers() -> Optional[dict]:
    """返回缓存的鉴权 headers，过期返回 None。"""
    ts = _cached_zp_headers.get("_ts", 0)
    if ts and (time.time() - ts) < _HEADERS_CACHE_TTL:
        out = dict(_cached_zp_headers)
        out.pop("_ts", None)
        return out if out else None
    return None


@app.on_event("startup")
async def on_startup():
    global automation, monitor_task, browser_sync_lock
    browser_sync_lock = asyncio.Lock()
    # 清理旧垃圾会话 + 合并同名重复会话
    try:
        from boss_state import get_db

        db = get_db()
        junk_names = [
            "HR",
            "你好",
            "消息",
            "未知HR",
            "AI简历",
            "简历更新",
            "附件简历制作",
            "附件上传",
        ]
        for name in junk_names:
            db.execute("DELETE FROM conversations WHERE hr_name = ?", (name,))
        db.execute("DELETE FROM conversations WHERE hr_name IS NULL OR length(hr_name) < 2")
        # 合并同名重复：保留最早的，把重复的改成 closed
        db.execute("""
            UPDATE conversations SET status = 'closed'
            WHERE id NOT IN (
                SELECT MIN(id) FROM conversations WHERE status != 'closed' GROUP BY hr_name
            ) AND status != 'closed'
        """)
        db.commit()
    except Exception:
        pass
    if automation is not None and automation.page is not None:
        monitor_task = asyncio.create_task(chat_monitor_loop())


# Playwright 同步 API 要求所有操作在同一线程 —— 用单线程池保证
_playwright_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw")


async def _run_pw(fn, *args):
    """在 Playwright 专属线程中执行同步操作，清除该线程的 asyncio 状态。"""

    def _wrapper():
        # Playwright sync API 检测到 event loop 会拒绝运行，先清掉
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        return fn(*args)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_playwright_executor, _wrapper)


@asynccontextmanager
async def _lock_browser():
    """上下文管理器：暂停监控 + 获取浏览器锁，确保不跟 chat_monitor 争夺资源。
    任何异常都会安全释放锁和恢复监控状态。

    用法:
        async with _lock_browser():
            ...
    """
    global monitor_paused, browser_sync_lock
    was_paused = monitor_paused
    monitor_paused = True
    try:
        for _ in range(20):
            await asyncio.sleep(0.5)
            if browser_sync_lock is None or not browser_sync_lock.locked():
                break
        if browser_sync_lock is None:
            browser_sync_lock = asyncio.Lock()
        await browser_sync_lock.acquire()
        yield
    finally:
        if browser_sync_lock and browser_sync_lock.locked():
            browser_sync_lock.release()
        monitor_paused = was_paused


# BOSS直聘城市代码（按省份分组）
CITY_MAP = {
    # 山东省
    "济南": "101120100",
    "青岛": "101120200",
    "淄博": "101120300",
    "德州": "101120400",
    "烟台": "101120500",
    "潍坊": "101120600",
    "济宁": "101120700",
    "泰安": "101120800",
    "临沂": "101120900",
    "菏泽": "101121000",
    "滨州": "101121100",
    "东营": "101121200",
    "威海": "101121300",
    "枣庄": "101121400",
    "日照": "101121500",
    "聊城": "101121700",
    # 一线城市
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    # 新一线城市
    "成都": "101270100",
    "杭州": "101210100",
    "武汉": "101200100",
    "南京": "101190100",
    "重庆": "101040100",
    "西安": "101110100",
    "长沙": "101250100",
    "天津": "101030100",
    "苏州": "101190400",
    "郑州": "101180100",
    "东莞": "101281600",
    "沈阳": "101070100",
    "宁波": "101210400",
    "昆明": "101290100",
    # 其他省会城市
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "南昌": "101240100",
    "贵阳": "101260100",
    "南宁": "101300100",
    "太原": "101100100",
    "石家庄": "101090100",
    "哈尔滨": "101050100",
    "长春": "101060100",
    "兰州": "101160100",
    "乌鲁木齐": "101130100",
    "呼和浩特": "101080100",
    "拉萨": "101140100",
    "西宁": "101150100",
    "银川": "101170100",
    "海口": "101310100",
    "三亚": "101310200",
    # 特殊选项
    "全国": "100010000",
}


def _normalize_job_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return urljoin("https://www.zhipin.com", url)


def _search_job_payload(job: dict, application: Optional[dict] = None) -> dict:
    """统一搜索结果和数据库记录的字段名，方便前端直接渲染。"""
    application = application or {}
    company = application.get("company") or job.get("company", "")
    company_id = application.get("company_id") or job.get("company_id", "")
    result = {
        "id": application.get("id"),
        "job_title": application.get("job_title") or job.get("title", ""),
        "company": company,
        "company_id": company_id,
        "company_already_applied": company_already_applied(company=company, company_id=company_id),
        "salary": application.get("salary") or job.get("salary", ""),
        "job_url": application.get("job_url") or _normalize_job_url(job.get("url", "")),
        "city": application.get("city") or job.get("city", ""),
        "area_district": application.get("area_district") or job.get("area_district", ""),
        "business_district": application.get("business_district") or job.get("business_district", ""),
        "experience": application.get("experience") or job.get("experience", ""),
        "education": application.get("education") or job.get("education", ""),
        "company_size": application.get("company_size") or job.get("company_size", ""),
        "industry": application.get("industry") or job.get("industry", ""),
        "legal_rep": application.get("legal_rep") or job.get("legal_rep", ""),
        "is_boss": bool(application.get("is_boss") or job.get("is_boss", False)),
        "optimize_at": application.get("optimize_at") or "",
        "chat_suggestion_at": application.get("chat_suggestion_at") or "",
        "hr_name": application.get("hr_name") or job.get("hr_name", ""),
        "hr_title": application.get("hr_title") or job.get("hr_title", ""),
        "hr_active": application.get("hr_active") or job.get("hr_active", ""),
        "description": application.get("description") or job.get("description", ""),
        "status": application.get("status") or ("pending" if job.get("url") else "missing_url"),
    }
    _legal = result.get("legal_rep") or ""
    _hr = result.get("hr_name") or ""
    if _legal and _hr and _hr == _legal:
        result["is_boss"] = True
    _attach_hr_active_days(result)
    return result


def _attach_hr_active_days(job: dict):
    """将 hr_active 字符串（如'今日活跃'、'3日内活跃'）转为 hr_active_days 和 hr_active_label。"""
    import re

    raw = (job.get("hr_active") or "").strip()
    if not raw:
        return
    job["hr_active_label"] = raw

    # 匹配数字
    m = re.search(r"(\d+)", raw)
    if m:
        job["hr_active_days"] = int(m.group(1))
        return

    # 文本匹配
    if "今日" in raw or "刚刚" in raw:
        job["hr_active_days"] = 0
    elif "昨日" in raw or "昨天" in raw:
        job["hr_active_days"] = 1
    elif "本周" in raw:
        job["hr_active_days"] = 3
    elif "本月" in raw or "近月" in raw:
        job["hr_active_days"] = 7
    elif "在线" in raw or "刚刚活跃" in raw:
        job["hr_active_days"] = 0
    elif "半年前" in raw or "超过半年" in raw:
        job["hr_active_days"] = 180
    else:
        job["hr_active_days"] = 14  # 默认不活跃


def _clean_messages_for_web(messages: List[dict]) -> List[dict]:
    """清理 BOSS DOM 里混入的已读/送达状态，保持 Web 端只展示聊天正文。"""
    cleaned = []
    status_words = ("已读", "未读", "送达", "发送失败", "已发送")
    for msg in messages:
        item = dict(msg)
        content = (item.get("content") or "").strip()
        for word in status_words:
            if content.startswith(word):
                content = content[len(word) :].strip()
            if content.endswith(word):
                content = content[: -len(word)].strip()
        item["content"] = content
        if content:
            cleaned.append(item)
    return cleaned


# ══════════════════════════════════════
#  Pydantic Models
# ══════════════════════════════════════


class SearchRequest(BaseModel):
    keyword: str = ""
    city: str = ""
    welfare: Optional[str] = None
    limit: int = 60
    # 区域/公司规模过滤
    district: Optional[str] = ""  # 单区字符串（兼容旧接口），如 "张店区"
    districts: Optional[List[str]] = None  # 多区列表：["增城区", "番禺区"]，优先于 district
    # 「工作区域」过滤（如淄博→["张店区", "临淄区"]，与 districts 是不同维度）
    areas: Optional[List[str]] = None
    # company_size 支持列表多选 / 字符串单值 / "302,303" 逗号串
    company_size: Optional[List[str]] = None  # 列表形式优先
    company_size_str: Optional[str] = None  # 兼容旧的字符串字段


class ApplyRequest(BaseModel):
    job_url: str
    greeting: Optional[str] = None


class ApplyBatchRequest(BaseModel):
    job_urls: List[str]
    greeting: Optional[str] = None


class ScanAndApplyRequest(BaseModel):
    greeting: Optional[str] = None


class AnalyzeRequest(BaseModel):
    job_url: str
    job_title: Optional[str] = ""
    company: Optional[str] = ""
    description: Optional[str] = ""
    company_id: Optional[str] = ""
    with_company_info: Optional[bool] = False


class SendMessageRequest(BaseModel):
    content: str


class SettingsUpdate(BaseModel):
    greeting_template: Optional[str] = None
    greeting_enabled: Optional[str] = None
    greeting_mode: Optional[str] = None  # 招呼语模式：template / smart
    smart_greeting_prompt: Optional[str] = None  # 智能跟进 Prompt
    ai_reply_style: Optional[str] = None
    daily_apply_limit: Optional[str] = None
    auto_reply_enabled: Optional[str] = None
    min_reply_delay_sec: Optional[str] = None
    max_reply_delay_sec: Optional[str] = None
    batch_delay_min_sec: Optional[str] = None
    batch_delay_max_sec: Optional[str] = None
    resume_summary: Optional[str] = None
    wechat_id: Optional[str] = None
    search_keywords: Optional[str] = None
    default_city: Optional[str] = None
    user_location: Optional[str] = None  # 用户所在地（用于优先搜索）
    selector_overrides: Optional[str] = None
    ai_api_key: Optional[str] = None
    ai_base_url: Optional[str] = None
    ai_model: Optional[str] = None
    filter_inactive_hr: Optional[str] = None  # 是否过滤不活跃 HR
    max_hr_inactive_days: Optional[str] = None  # HR 不活跃天数阈值
    dedup_company_by_default: Optional[str] = None  # 默认公司去重
    conversation_cooldown_sec: Optional[str] = None  # 会话冷却时间
    reply_rules_system_prompt: Optional[str] = None  # 回复规则 prompt
    debug_llm_context: Optional[str] = None  # LLM 上下文调试日志


class CompanyPreviewRequest(BaseModel):
    keyword: Optional[str] = ""
    city: Optional[str] = ""
    company: Optional[str] = ""
    company_id: Optional[str] = ""


class SmartSendRequest(BaseModel):
    company: Optional[str] = ""
    company_id: Optional[str] = ""
    job_url: Optional[str] = ""
    top_hr: Optional[dict] = None
    hr_name: Optional[str] = ""
    greeting: Optional[str] = ""
    confirm: bool = False
    targets: Optional[list] = None  # [{company, job_url, hr_name, hr_title}, ...]


class AgentRunRequest(BaseModel):
    goal: str
    max_steps: int = 12
    auto_execute: bool = True  # False = dry-run，只规划不执行（预留）


class InterviewStartRequest(BaseModel):
    job_url: Optional[str] = ""
    job_focus: Optional[str] = ""


class InterviewAnswerRequest(BaseModel):
    session_id: str
    answer: str


class InterviewSessionRequest(BaseModel):
    session_id: str


# ══════════════════════════════════════
#  WebSocket 广播
# ══════════════════════════════════════


async def broadcast_ws(message: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


# ══════════════════════════════════════
#  页面
# ══════════════════════════════════════


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = static_dir / "dashboard.html"
    if html_path.exists():
        resp = HTMLResponse(content=html_path.read_text(encoding="utf-8"))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    return HTMLResponse(content="<h1>BOSS直聘自动化控制台</h1><p>dashboard.html 未找到</p>")


# ══════════════════════════════════════
#  系统状态
# ══════════════════════════════════════


@app.get("/api/status")
def get_status():
    browser_ok = automation is not None and automation.page is not None
    return {
        "browser_running": browser_ok,
        "auto_reply_enabled": get_setting("auto_reply_enabled", "false") == "true",
        "monitor_running": monitor_task is not None and not monitor_task.done(),
        "monitor_paused": monitor_paused,
        "today_applications": get_today_application_count(),
        "active_conversations": len(list_active_conversations()),
        "daily_stats": get_daily_stats(),
    }


@app.get("/api/stats")
def get_stats():
    """投递转化漏斗统计。"""
    today = get_daily_stats()
    return {
        "today_applications": get_today_application_count(),
        "pending": get_today_pending_count(),
        "replied": count_hours_replied_in_range(24),
        "interview": count_interest_level("high"),
        "active_conversations": len(list_active_conversations()),
        "daily_stats": today,
    }


@app.get("/api/doctor")
def doctor():
    """诊断环境：Python版本、浏览器状态、登录态、AI配置等。"""
    import os
    import sys as _sys

    try:
        _sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import _load_ai_config

        cfg = _load_ai_config()
        ai_key_ok = bool(cfg.get("api_key") and len(cfg["api_key"]) > 10)
    except Exception:
        ai_key_ok = False

    browser_ok = automation is not None and automation.page is not None
    checks = {
        "python": {"ok": True, "detail": _sys.version.split()[0]},
        "browser": {"ok": browser_ok, "detail": "运行中" if browser_ok else "未启动"},
        "boss_login": {"ok": browser_ok, "detail": "已登录" if browser_ok else "未登录"},
        "ai_key": {"ok": ai_key_ok, "detail": "已配置" if ai_key_ok else "未配置"},
        "today_applications": get_today_application_count(),
        "pending_jobs": get_today_pending_count(),
    }
    # 只对包含 ok 字段的检查项汇总 all_ok，数值项不参与
    _oks = []
    for v in checks.values():
        if isinstance(v, dict) and "ok" in v:
            try:
                _oks.append(bool(v.get("ok", True)))
            except Exception:
                _oks.append(True)
    all_ok = all(_oks) if _oks else True
    return {"ok": all_ok, "checks": checks}


@app.get("/api/debug/legal-rep")
async def debug_legal_rep(company_id: str):
    """[临时] 调 fetch_company_legal_rep。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    rep = await _run_pw(automation.fetch_company_legal_rep, company_id)
    return {"company_id": company_id, "legal_rep": rep}


@app.get("/api/debug/dump-injection")
async def debug_dump_injection(company_id: str = "608f74e849bb98e10HB-39-5Ew~~"):
    """[临时] 导航到公司介绍页, dump 出注入对象里所有 'legalPerson' 路径。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    url = f"https://www.zhipin.com/gongsi/{company_id}.html"

    def _dump():
        try:
            automation.page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:
            return {"goto_error": str(e)[:200]}
        import time as _t

        _t.sleep(2)
        js = r"""
        () => {
            function walk(obj, path, depth, maxDepth, results) {
                if (depth > maxDepth) return;
                if (obj === null || obj === undefined) return;
                if (typeof obj !== 'object') {
                    var lower = path.toLowerCase();
                    if (lower.endsWith('legalperson') || lower.endsWith('legal_person') ||
                        lower.endsWith('legalpersonname') || lower.endsWith('representative') ||
                        lower.endsWith('representativename')) {
                        if (typeof obj === 'string' || typeof obj === 'number') {
                            results.push({path: path, value: String(obj).slice(0,80)});
                        }
                    }
                    return;
                }
                for (var k in obj) {
                    try {
                        walk(obj[k], path ? (path + '.' + k) : k, depth+1, maxDepth, results);
                    } catch(e) {}
                }
            }
            var results = [];
            for (var name in window) {
                try {
                    var v = window[name];
                    if (v && typeof v === 'object' && Object.keys(v).length > 0) {
                        walk(v, name, 0, 5, results);
                    }
                } catch(e) {}
            }
            return {url: window.location.href, results: results.slice(0, 50)};
        }
        """
        try:
            return automation.page.evaluate(js) or {"results": []}
        except Exception as e:
            return {"error": str(e)[:200]}

    return await _run_pw(_dump)


@app.post("/api/system/start")
async def start_automation():
    global automation, monitor_task
    if automation is not None and automation.page is not None:
        return {"status": "already_started"}

    # 在后台线程启动浏览器，避免阻塞事件循环
    def _do_start():
        a = BossAutomation(headless=False)
        a.start()
        return a

    try:
        automation = await _run_pw(_do_start)
    except Exception as e:
        automation = None
        return {"status": "error", "message": f"浏览器启动失败: {e}"}

    if automation is None or automation.page is None:
        automation = None
        return {"status": "error", "message": "浏览器启动后页面为空，请重试"}

    # 快速检查登录状态
    def _check_login():
        try:
            url = automation.page.url
            if "passport" in url or "security" in url:
                return {"logged_in": False, "reason": "触发安全验证", "url": url}
            if automation._login_prompt_visible():
                return {"logged_in": False, "reason": "需要登录", "url": url}
            return {"logged_in": True, "reason": "", "url": url}
        except Exception:
            return {"logged_in": True, "reason": "", "url": ""}

    login_status = await _run_pw(_check_login)
    if not login_status.get("logged_in"):
        msg = f"浏览器已启动但登录态异常: {login_status.get('reason')}。请手动登录或使用 /api/system/relogin。"
        return {"status": "warning", "message": msg, "login_status": login_status}

    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(chat_monitor_loop())
    try:
        await _run_pw(_refresh_zp_cache)
    except Exception:
        pass
    await broadcast_ws({"type": "system", "event": "started"})
    return {"status": "started", "login_status": login_status}


@app.post("/api/system/stop")
async def stop_automation():
    global automation, monitor_task
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
        monitor_task = None
    if automation:
        try:
            await _run_pw(automation._save_state)  # 正常关闭时保存登录态
        except Exception:
            pass
        try:
            await _run_pw(automation.close)
        except Exception:
            pass
        automation = None
    await broadcast_ws({"type": "system", "event": "stopped"})
    return {"status": "stopped"}


@app.post("/api/system/relogin")
async def relogin():
    """重新登录 BOSS直聘。会打开浏览器让用户扫码。"""
    global automation, monitor_task
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
        monitor_task = None
    if automation:
        try:
            await _run_pw(automation.close)
        except Exception:
            pass
        automation = None

    def _do_relogin():
        a = BossAutomation(headless=False)
        a.start()
        a.login()
        # login() 会轮询等用户扫码，完成后保存状态
        return a

    try:
        automation = await _run_pw(_do_relogin)
    except Exception as e:
        automation = None
        return {"status": "error", "message": f"登录失败: {e}"}

    if automation is None or automation.page is None:
        automation = None
        return {"status": "error", "message": "登录后页面异常，请重试"}

    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(chat_monitor_loop())
    try:
        await _run_pw(_refresh_zp_cache)
    except Exception:
        pass
    await broadcast_ws({"type": "system", "event": "relogin_ok"})
    return {"status": "ok", "message": "扫码登录成功"}


@app.post("/api/system/heartbeat")
async def manual_heartbeat():
    """手动心跳保活。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    alive = await _run_pw(automation.heartbeat)
    if not alive:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return {"status": "ok", "alive": True}


@app.post("/api/monitor/pause")
async def pause_monitor():
    global monitor_paused
    monitor_paused = True
    await broadcast_ws({"type": "monitor_paused"})
    return {"status": "paused"}


@app.post("/api/monitor/resume")
async def resume_monitor():
    global monitor_paused
    monitor_paused = False
    await broadcast_ws({"type": "monitor_resumed"})
    return {"status": "resumed"}


@app.post("/api/system/navigate-chat")
async def navigate_to_chat_page():
    """在浏览器中打开 BOSS 直聘聊天页。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    success = await _run_pw(automation.navigate_to_chat)
    return {
        "status": "ok" if success else "error",
        "message": "已跳转到聊天页" if success else "跳转失败，请检查登录状态",
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "browser": automation is not None}


# ══════════════════════════════════════
#  调试 / 页面分析（BOSS改版时诊断选择器）
# ══════════════════════════════════════


class SelectorTest(BaseModel):
    selector: str


@app.post("/api/debug/selector-test")
async def test_selector(req: SelectorTest):
    """测试任意 CSS 选择器，返回匹配元素数和文本。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    try:
        result = await asyncio.wait_for(
            _run_pw(
                lambda: automation.page.evaluate(
                    """(sel) => {
                try {
                    const els = document.querySelectorAll(sel);
                    const items = [];
                    for (let i = 0; i < Math.min(els.length, 10); i++) {
                        items.push((els[i].innerText || '').trim().substring(0, 200));
                    }
                    return {count: els.length, samples: items};
                } catch(e) {
                    return {error: e.message};
                }
            }""",
                    req.selector,
                )
            ),
            timeout=8.0,
        )
        return result
    except asyncio.TimeoutError:
        return {"error": "browser_busy", "detail": "浏览器线程繁忙（监控/搜索占用），稍后重试"}


@app.get("/api/debug/page-stats")
async def page_stats():
    """返回当前页面 DOM 统计，帮助诊断选择器失效。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    try:
        result = await asyncio.wait_for(
            _run_pw(
                lambda: automation.page.evaluate("""() => {
            const stats = {};
            stats.url = window.location.href;
            stats.title = document.title;
            stats.bodyLength = (document.body?.innerText || '').length;
            stats.liCount = document.querySelectorAll('li').length;
            stats.inputCount = document.querySelectorAll('input, textarea, [contenteditable]').length;
            stats.buttonCount = document.querySelectorAll('button').length;
            stats.messageItems = document.querySelectorAll('li.message-item, [class*="message-item"]').length;
            stats.listItems = document.querySelectorAll('li[role="listitem"]').length;
            stats.chatInput = document.querySelector('#chat-input') ? 1 : 0;
            stats.sendButton = document.querySelector('button[type="send"]') ? 1 : 0;
            stats.bodyPreview = (document.body?.innerText || '').substring(0, 500);
            return stats;
        }""")
            ),
            timeout=8.0,
        )
        return result
    except asyncio.TimeoutError:
        return {"error": "browser_busy", "detail": "浏览器线程繁忙，稍后重试"}


@app.get("/api/debug/selectors-status")
async def selectors_status():
    """检查所有关键选择器的有效性。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    from boss_automation import SELECTORS

    try:
        result = await asyncio.wait_for(
            _run_pw(
                lambda: automation.page.evaluate(
                    """(groups) => {
                const res = {};
                for (const [key, sels] of Object.entries(groups)) {
                    for (const sel of sels) {
                        try {
                            const count = document.querySelectorAll(sel).length;
                            if (count > 0) {
                                res[key] = {selector: sel, count: count, ok: true};
                                break;
                            }
                        } catch(e) {}
                    }
                    if (!res[key]) res[key] = {selector: sels[sels.length-1], count: 0, ok: false};
                }
                return res;
            }""",
                    SELECTORS,
                )
            ),
            timeout=10.0,
        )
        return result
    except asyncio.TimeoutError:
        return {"error": "browser_busy", "detail": "浏览器线程繁忙（监控/搜索占用），稍后重试"}


# ══════════════════════════════════════
#  岗位搜索 & 管理
# ══════════════════════════════════════


@app.get("/api/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 100):
    jobs = list_applications(status, limit)
    return {"jobs": jobs, "total": len(jobs)}


@app.post("/api/jobs/search")
async def search_jobs(req: SearchRequest):
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动，请先到设置Tab点击「启动浏览器」")
    async with _lock_browser():
        city_code = CITY_MAP.get(req.city or get_setting("default_city", "全国"), "100010000")
        try:
            # 合并 company_size 列表/字符串两路来源（前端可用 list，旧 CLI 可用 str）
            cs_list: list = []
            if req.company_size:
                if isinstance(req.company_size, list):
                    cs_list = [str(s).strip() for s in req.company_size if s]
                else:
                    cs_list = [str(req.company_size).strip()]
            if req.company_size_str and req.company_size_str not in cs_list:
                cs_list.append(req.company_size_str)

            # 合并 district 单/多两路来源
            ds_list: list = []
            if req.districts:
                if isinstance(req.districts, list):
                    ds_list = [str(d).strip() for d in req.districts if d]
                else:
                    ds_list = [str(req.districts).strip()]
            if req.district and req.district not in ds_list:
                ds_list.append(req.district)

            # 「工作区域」（如淄博→张店区/临淄区）
            ar_list: list = []
            if req.areas:
                if isinstance(req.areas, list):
                    ar_list = [str(a).strip() for a in req.areas if a]
                else:
                    ar_list = [str(req.areas).strip()]

            jobs = await _run_pw(
                automation.search,
                req.keyword,
                city_code,
                ds_list or None,  # 多区列表（list / None）
                "",  # 旧单区字段已合并到 ds_list
                cs_list if cs_list else None,  # 规模列表
                ar_list if ar_list else None,  # 工作区域列表
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"搜索失败: {e}")

        # 福利筛选
        if req.welfare:
            welfare_kw = [w.strip() for w in req.welfare.split(",") if w.strip()]
            jobs = automation._filter_by_welfare(jobs, welfare_kw)

        saved_ids = []
        result_jobs = []
        for j in jobs:
            j["url"] = _normalize_job_url(j.get("url", ""))
            if j.get("url"):
                existing = get_application_by_url(j["url"])
                if existing:
                    updated = update_application_from_job(existing["id"], j) or existing
                    saved_ids.append(updated["id"])
                    result_jobs.append(_search_job_payload(j, updated))
                else:
                    aid = add_application(j)
                    if aid:
                        saved_ids.append(aid)
                        result_jobs.append(_search_job_payload(j, get_application(aid)))
                    else:
                        result_jobs.append(_search_job_payload(j))
            else:
                result_jobs.append(_search_job_payload(j))

        # 补抓法人信息：对缺少 legal_rep 但有 encrypt_brand_id 的公司批量补抓
        need_legal = [j for j in jobs if j.get("encrypt_brand_id") and not j.get("legal_rep")]
        seen_cids = set()
        for j in need_legal:
            cid = j.get("encrypt_brand_id", "")
            if cid and cid not in seen_cids:
                seen_cids.add(cid)
                try:
                    legal = await _run_pw(automation.fetch_company_legal_rep, cid)
                    if legal:
                        for k in jobs:
                            if k.get("encrypt_brand_id") == cid:
                                k["legal_rep"] = legal
                                k["is_boss"] = (k.get("hr_name") or "") == legal
                        if j.get("url"):
                            db = get_db()
                            db.execute(
                                "UPDATE applications SET legal_rep=?, is_boss=? WHERE job_url=?",
                                (legal, 1 if (j.get("hr_name") or "") == legal else 0, j["url"]),
                            )
                            db.commit()
                except Exception:
                    pass
        # 更新 result_jobs 中对应的 legal_rep / is_boss
        for rj in result_jobs:
            for j in jobs:
                if j.get("url") and rj.get("job_url") == _normalize_job_url(j.get("url", "")):
                    if j.get("legal_rep") and not rj.get("legal_rep"):
                        rj["legal_rep"] = j["legal_rep"]
                    if j.get("is_boss") and not rj.get("is_boss"):
                        rj["is_boss"] = True

        await broadcast_ws(
            {
                "type": "search_complete",
                "keyword": req.keyword,
                "city": req.city,
                "found": len(jobs),
            }
        )
        try:
            await _run_pw(_refresh_zp_cache)
        except Exception:
            pass
        return {"jobs_found": len(jobs), "saved": len(saved_ids), "jobs": result_jobs}


# ══════════════════════════════════════
#  公司画像 & smart-send
# ══════════════════════════════════════

_INVALID_COMPANY_RE = re.compile(
    r"^("
    r"\d+[-~]\d+K"  # 薪资：5-10K
    r"|\d+-\d+元"  # 薪资：3000-5000元
    r"|\d+元/[时月]"  # 薪资：20元/时
    r"|\d+-\d+年"  # 经验：1-3年
    r"|\d+年以[上内]"  # 经验：3年以上
    r"|1年以内"
    r"|经验不限|学历不限|不限"
    r"|在校|应届|实习"
    r"|本科|硕士|博士|大专|中专|中技|高中|初中"
    r"|全职|兼职"
    r"|\d+天/周"  # 5天/周
    r"|\d+-\d+人"  # 20-99人
    r"|\d+人以上"
    r"|\d+人"
    r")$",
    re.I,
)


def _is_valid_company(name: str) -> bool:
    """判断公司名是否有效（排除把经验/学历/薪资当公司名的脏数据）。"""
    if not name or len(name) < 2:
        return False
    name = name.strip()
    if _INVALID_COMPANY_RE.match(name):
        return False
    # "中专/中技" 这种斜杠分隔的短词
    if "/" in name and len(name) <= 8:
        parts = name.split("/")
        if all(len(p.strip()) <= 4 for p in parts):
            # 检查是否每一段都像学历/经验
            if any(k in name for k in ["专", "技", "科", "士", "中", "高", "年", "限"]):
                return False
    return True


@app.get("/api/companies/preview")
async def api_companies_preview(
    company: Optional[str] = "",
    company_id: Optional[str] = "",
    keyword: Optional[str] = "",
    city: Optional[str] = "",
    mode: Optional[str] = "fast",
    districts: Optional[str] = None,
    company_size: Optional[str] = None,
    areas: Optional[str] = None,
):
    """智能投递预览：搜索 → 按公司分组 → 每家公司从搜索结果里提取 HR → pick_top_hr。
    返回所有公司列表，每家带 top_hr + 岗位列表 + 推荐投递的 job_url。

    新增过滤参数：
      - districts: 多个区 code 用英文逗号分隔（如 "440118,440113"），与 search 共享同一参数
      - company_size: 多个 scale code 逗号分隔（如 "302,303"），同上
      - areas: 多个工作区域名逗号分隔（如 "张店区,临淄区"），与 search 共享同一参数
    """
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动，请先到设置Tab点击「启动浏览器」")

    if not keyword and not company:
        raise HTTPException(status_code=400, detail="keyword 或 company 至少给一个")

    # 1) 搜索：合并 districts/company_size/areas
    city_code = CITY_MAP.get(city or get_setting("default_city", "全国"), "100010000")
    ds_list = [x.strip() for x in (districts or "").split(",") if x.strip()] or None
    cs_list = [x.strip() for x in (company_size or "").split(",") if x.strip()] or None
    ar_list = [x.strip() for x in (areas or "").split(",") if x.strip()] or None
    try:
        jobs = await _run_pw(
            automation.search,
            keyword or company,
            city_code,
            ds_list,  # 多区列表（districts 优先）
            "",  # 单区字段已合并到 ds_list
            cs_list,  # 规模列表
            ar_list,  # 工作区域列表
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}")
    if not jobs:
        return {"ok": False, "message": f"未找到任何 '{keyword or company}' 岗位", "companies": []}

    # 2) 写库 + 按公司分组（每家保留完整岗位信息）
    from collections import defaultdict
    from boss_automation import pick_top_hr, _hr_title_score

    groups: dict = defaultdict(list)  # key=(company, company_id) → [job_dict, ...]
    for j in jobs:
        j["url"] = _normalize_job_url(j.get("url", ""))
        if not j.get("url"):
            continue
        existing = get_application_by_url(j["url"])
        if existing:
            update_application_from_job(existing["id"], j)
            rec = existing
        else:
            aid = add_application(j)
            rec = get_application(aid) if aid else j
        comp = (rec.get("company") or j.get("company") or "").strip()
        cid = (rec.get("company_id") or j.get("company_id") or "").strip()
        if not comp or not _is_valid_company(comp):
            continue
        key = (comp, cid) if cid else (comp, "")
        groups[key].append(
            {
                "title": rec.get("job_title") or j.get("title") or "",
                "salary": rec.get("salary") or j.get("salary") or "",
                "url": rec.get("job_url") or j.get("url") or "",
                "hr_name": rec.get("hr_name") or j.get("hr_name") or "",
                "hr_title": rec.get("hr_title") or j.get("hr_title") or "",
                "city": rec.get("city") or j.get("city") or "",
            }
        )

    # 3) 每家公司：聚合 HR → pick_top_hr → 推荐投递岗位
    companies_result = []
    for (comp, cid), comp_jobs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        # 从搜索结果提取 HR
        hrs_seen = {}
        for cj in comp_jobs:
            hr_n = (cj.get("hr_name") or "").strip()
            hr_t = (cj.get("hr_title") or "").strip()
            if hr_n and hr_n not in hrs_seen:
                hrs_seen[hr_n] = {"name": hr_n, "title": hr_t, "priority": _hr_title_score(hr_t)}
            elif hr_n and hr_t:
                old_score = hrs_seen[hr_n]["priority"]
                new_score = _hr_title_score(hr_t)
                if new_score > old_score:
                    hrs_seen[hr_n]["title"] = hr_t
                    hrs_seen[hr_n]["priority"] = new_score

        hrs_list = sorted(hrs_seen.values(), key=lambda h: -h["priority"])
        top_hr = pick_top_hr(hrs_list)

        # 推荐投递的岗位：优先 top_hr 负责的，否则第一个有 url 的
        target_job = None
        if top_hr:
            target_job = next((cj for cj in comp_jobs if cj.get("hr_name") == top_hr["name"] and cj.get("url")), None)
        if not target_job:
            target_job = next((cj for cj in comp_jobs if cj.get("url")), None)

        already_applied = company_already_applied(company=comp, company_id=cid)

        companies_result.append(
            {
                "company": comp,
                "company_id": cid,
                "position_count": len(comp_jobs),
                "top_hr": top_hr,
                "hrs": hrs_list,
                "target_job": target_job,
                "already_applied": already_applied,
                "jobs": comp_jobs,
            }
        )

    # 4) 深度模式：打开公司页拿 HR（仅 top5 或 all）
    if mode in ("top5", "all"):
        limit_deep = 5 if mode == "top5" else len(companies_result)
        for i, cr in enumerate(companies_result[:limit_deep]):
            cid_val = cr.get("company_id") or ""
            # 从 comp_jobs 里找一个有 url 的
            anchor = next((j.get("url") for j in cr.get("jobs", []) if j.get("url")), "")
            if not anchor:
                continue
            try:
                resolved_cid = await _run_pw(automation.goto_company_similar_jobs, anchor)
                parsed = await _run_pw(automation.parse_company_similar_jobs_page)
                page_jobs = parsed.get("jobs") or []
                open_count = parsed.get("open_count") or 0
                # 法人信息只在 intro 页 (/gongsi/<id>.html)，需单独导航抓取
                legal_rep = ""
                rep_cid = resolved_cid or cid_val
                if rep_cid:
                    try:
                        legal_rep = await _run_pw(automation.fetch_company_legal_rep, rep_cid)
                    except Exception:
                        legal_rep = ""
                hrs_agg = await _run_pw(automation.aggregate_company_hrs, page_jobs)
                from boss_automation import pick_top_hr as _pick

                deep_top = _pick(hrs_agg, legal_rep)
                cr["top_hr"] = deep_top
                cr["hrs"] = hrs_agg
                cr["open_count"] = open_count
                cr["legal_rep"] = legal_rep
                cr["deep_analyzed"] = True
                # 更新 target_job：优先 top_hr 关联
                if deep_top and page_jobs:
                    tj = next((pj for pj in page_jobs if pj.get("hr_name") == deep_top["name"] and pj.get("url")), None)
                    if tj:
                        cr["target_job"] = tj
            except Exception as e:
                cr["deep_error"] = str(e)

    return {
        "ok": True,
        "mode": mode,
        "keyword": keyword or company,
        "city": city or get_setting("default_city", "全国"),
        "total_jobs": len(jobs),
        "total_companies": len(companies_result),
        "companies": companies_result,
    }


@app.post("/api/companies/smart-send")
async def api_companies_smart_send(req: SmartSendRequest):
    """批量智能投递：对 targets 列表里的每家公司投递到其 top_hr 的岗位。
    req.targets: [{company, job_url, hr_name, hr_title}]
    req.confirm 必须为 true。
    """
    global monitor_paused, browser_sync_lock
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    if not req.confirm:
        return {"success": False, "message": "需要 confirm=true 才执行"}
    # 暂停监控 + 获取浏览器锁（手动 acquire/release，避免大规模缩进）
    was_paused = monitor_paused
    monitor_paused = True
    for _ in range(20):
        await asyncio.sleep(0.5)
        if browser_sync_lock is None or not browser_sync_lock.locked():
            break
    if browser_sync_lock is None:
        browser_sync_lock = asyncio.Lock()
    await browser_sync_lock.acquire()
    try:
        targets = req.targets or []
        if not targets:
            # 兼容旧的单条模式
            if req.job_url:
                targets = [
                    {
                        "company": req.company or "",
                        "job_url": req.job_url,
                        "hr_name": req.hr_name or (req.top_hr or {}).get("name", ""),
                        "hr_title": (req.top_hr or {}).get("title", ""),
                    }
                ]
            else:
                raise HTTPException(status_code=400, detail="缺少 targets 或 job_url")

        daily_limit = int(get_setting("daily_apply_limit", "15"))
        results = []
        applied_count = 0
        skipped_count = 0

        for t in targets:
            if get_today_application_count() >= daily_limit:
                results.append({"company": t.get("company"), "status": "skipped", "reason": "日投递上限"})
                skipped_count += 1
                continue

            job_url = (t.get("job_url") or "").strip()
            if not job_url:
                results.append({"company": t.get("company"), "status": "skipped", "reason": "无 job_url"})
                skipped_count += 1
                continue

            hr_name = (t.get("hr_name") or "").strip()
            company_name = (t.get("company") or "").strip()
            job_record = get_application_by_url(job_url) or {}
            job_title = job_record.get("job_title") or "该岗位"
            job_desc = job_record.get("description") or ""
            is_boss = bool(t.get("is_boss") or job_record.get("is_boss"))
            style = get_setting("ai_reply_style", "professional")
            resume = get_setting("resume_summary", "")

            # AI 个性化招呼语（失败自动回退模板），在 PW 线程外生成（纯 HTTP 调用）
            greeting = await asyncio.to_thread(
                generate_greeting_ai,
                job_title,
                company_name,
                hr_name,
                job_desc,
                is_boss,
                style,
                resume,
            )

            try:
                result = await _run_pw(automation.apply_to_job, job_url, greeting)
                if result.get("success"):
                    applied_count += 1
                    results.append(
                        {
                            "company": company_name,
                            "job_title": job_title,
                            "hr_name": hr_name,
                            "status": "success",
                            "application_id": result.get("application_id"),
                            "already_applied": result.get("already_applied", False),
                        }
                    )
                else:
                    results.append(
                        {"company": company_name, "status": "failed", "reason": result.get("message", "投递失败")}
                    )
            except Exception as e:
                results.append({"company": company_name, "status": "failed", "reason": str(e)})

        # 广播 WS：让前端的投递漏斗/统计/会话列表实时刷新（与普通批量投递一致）
        await broadcast_ws(
            {
                "type": "batch_complete",
                "total": len(targets),
                "success": applied_count,
            }
        )

        return {
            "success": applied_count > 0,
            "applied": applied_count,
            "skipped": skipped_count,
            "total": len(targets),
            "message": f"批量投递完成：{applied_count} 成功 / {skipped_count} 跳过 / {len(targets)} 总计",
            "results": results,
        }
    finally:
        if browser_sync_lock and browser_sync_lock.locked():
            browser_sync_lock.release()
        monitor_paused = was_paused


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    job = get_application(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")
    return {"job": job}


@app.post("/api/jobs/{job_id}/skip")
async def skip_job(job_id: int):
    update_application_status(job_id, "skipped")
    await broadcast_ws({"type": "job_updated", "job_id": job_id, "status": "skipped"})
    return {"status": "ok"}


# ══════════════════════════════════════
#  投递
# ══════════════════════════════════════


@app.post("/api/jobs/apply")
async def apply_to_job(req: ApplyRequest):
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    async with _lock_browser():
        daily_limit = int(get_setting("daily_apply_limit", "15"))
        if get_today_application_count() >= daily_limit:
            raise HTTPException(status_code=429, detail="已达到今日投递上限")

        greeting = req.greeting
        if not greeting:
            job = get_application_by_url(req.job_url)
            title = job["job_title"] if job else "相关岗位"
            company = job["company"] if job else "贵公司"
            job_desc = (job or {}).get("description", "") if job else ""
            is_boss = bool((job or {}).get("is_boss")) if job else False
            style = get_setting("ai_reply_style", "professional")
            resume = get_setting("resume_summary", "")
            # 注入简历优化建议，让招呼语更贴合JD
            opt_hints = ""
            if job:
                try:
                    import json as _json

                    _opt = job.get("optimize_result") or ""
                    if _opt:
                        _opt_data = _json.loads(_opt) if isinstance(_opt, str) else _opt
                        parts = []
                        if _opt_data.get("one_line"):
                            parts.append("核心定位: " + _opt_data["one_line"])
                        if _opt_data.get("match_gaps"):
                            parts.append("匹配差距: " + ", ".join(_opt_data["match_gaps"][:3]))
                        if _opt_data.get("optimize_tips"):
                            for t in _opt_data.get("optimize_tips", [])[:2]:
                                parts.append(f"{t.get('area', '')}: {t.get('suggestion', '')}")
                        opt_hints = "\n".join(parts)
                except Exception:
                    pass
            greeting = await asyncio.to_thread(
                generate_greeting_ai, title, company, "", job_desc, is_boss, style, resume, opt_hints
            )

        # 在后台线程运行（Playwright 是同步的）
        result = await _run_pw(automation.apply_to_job, req.job_url, greeting)
        if result.get("success"):
            await broadcast_ws(
                {
                    "type": "apply_complete",
                    "job_url": req.job_url,
                    "job_id": result.get("application_id"),
                }
            )
        return result


@app.post("/api/jobs/apply-batch")
async def apply_batch(req: ApplyBatchRequest):
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    async with _lock_browser():
        daily_limit = int(get_setting("daily_apply_limit", "15"))
        remaining = daily_limit - get_today_application_count()
        urls = req.job_urls[: max(1, remaining)]

        results = await _run_pw(automation.apply_batch, urls, req.greeting)
        await broadcast_ws(
            {
                "type": "batch_complete",
                "total": len(results),
                "success": sum(1 for r in results if r.get("success")),
            }
        )
        return {"results": results}


@app.post("/api/jobs/scan")
async def scan_current_page():
    """扫描当前BOSS搜索结果页面，提取所有可见岗位，保存到数据库并返回。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动，请先到设置Tab点击「启动浏览器」")
    async with _lock_browser():
        try:
            jobs = await _run_pw(automation.scan_current_page)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"扫描失败: {e}")

        saved_ids = []
        result_jobs = []
        for j in jobs:
            j["url"] = _normalize_job_url(j.get("url", ""))
            if j.get("url"):
                existing = get_application_by_url(j["url"])
                if existing:
                    updated = update_application_from_job(existing["id"], j) or existing
                    saved_ids.append(updated["id"])
                    result_jobs.append(_search_job_payload(j, updated))
                else:
                    aid = add_application(j)
                    if aid:
                        saved_ids.append(aid)
                        result_jobs.append(_search_job_payload(j, get_application(aid)))
                    else:
                        result_jobs.append(_search_job_payload(j))
            else:
                result_jobs.append(_search_job_payload(j))

        await broadcast_ws(
            {
                "type": "scan_complete",
                "found": len(jobs),
                "saved": len(saved_ids),
            }
        )
        return {"jobs_found": len(jobs), "saved": len(saved_ids), "jobs": result_jobs}


@app.post("/api/jobs/scan-and-apply")
async def scan_and_apply(req: ScanAndApplyRequest = ScanAndApplyRequest()):
    """扫描当前页面全部岗位 → 一键批量投递。"""
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    async with _lock_browser():
        daily_limit = int(get_setting("daily_apply_limit", "15"))
        if get_today_application_count() >= daily_limit:
            raise HTTPException(status_code=429, detail="已达到今日投递上限")

        result = await _run_pw(automation.scan_and_apply_current_page, req.greeting)
        await broadcast_ws(
            {
                "type": "scan_apply_complete",
                "scanned": result.get("scanned", 0),
                "applied": result.get("applied", 0),
            }
        )
        return result


@app.post("/api/jobs/analyze")
async def analyze_jd(req: AnalyzeRequest):
    """AI分析岗位JD，返回匹配度、关键技能、差距、建议。"""
    resume = get_setting("resume_summary", "")
    desc = req.description or ""
    title = req.job_title or ""
    company = req.company or ""

    if resume and len(resume.strip()) > 5:
        prompt = f"""你是求职辅导专家。分析以下岗位JD，对比求职者简历，输出JSON。

## 求职者简历
{resume}

## 岗位信息
- 公司: {company}
- 职位: {title}
- JD: {desc[:2000]}

## 输出格式（严格JSON）
{{
  "match_score": 85,
  "decision": "建议投递",
  "key_skills": ["Python", "LangChain", "RAG"],
  "gap": "缺少K8s部署经验",
  "advice": "建议强调Agent开发经验，问对方技术栈",
  "summary": "整体匹配度较高，注意补充部署相关经验",
  "reasons": ["匹配理由1", "匹配理由2"],
  "risks": ["风险点1"],
  "suggested_questions": ["建议追问1"]
}}"""
    else:
        prompt = f"""你是求职辅导专家。分析以下岗位JD，提取关键信息，输出JSON。

## 岗位信息
- 公司: {company}
- 职位: {title}
- JD: {desc[:2000]}

## 输出格式（严格JSON）
{{
  "match_score": 70,
  "decision": "可以尝试",
  "key_skills": ["Python", "LangChain", "RAG"],
  "gap": "",
  "advice": "",
  "summary": "该岗位的核心要求是...",
  "reasons": ["理由1"],
  "risks": ["风险1"],
  "suggested_questions": ["追问1"]
}}

注意：match_score 基于 JD 难度和市场需求预估即可，不必对比简历。summary 用一两句总结这个岗位的核心要求。"""

    try:
        sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import llm_chat_deepseek

        raw = llm_chat_deepseek(
            [{"role": "user", "content": prompt}],
            system_prompt="你是求职辅导专家，输出严格JSON。",
            temperature=0.3,
        )
        import json

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        if raw.lower().startswith("```json"):
            raw = raw[7:].strip()
        return json.loads(raw)
    except Exception as e:
        return {"error": f"AI分析失败: {e}", "match_score": 0, "summary": "请检查AI配置"}


class OptimizeResumeRequest(BaseModel):
    job_url: str
    job_title: Optional[str] = ""
    company: Optional[str] = ""
    description: Optional[str] = ""
    force_refresh: Optional[bool] = False  # 是否强制重新生成


@app.post("/api/jobs/optimize-resume")
async def optimize_resume(req: OptimizeResumeRequest):
    """根据岗位JD生成简历优化建议（24h内缓存复用，避免重复消耗token）。"""
    resume = get_setting("resume_summary", "")
    desc = req.description or ""
    title = req.job_title or ""
    company = req.company or ""

    if not desc and req.job_url:
        existing = get_application_by_url(req.job_url)
        if existing:
            desc = existing.get("description") or ""
            title = title or existing.get("job_title", "")
            company = company or existing.get("company", "")

    import datetime
    from boss_state import get_db

    db = get_db()
    row = db.execute(
        "SELECT optimize_result, optimize_at FROM applications WHERE job_url=?",
        (req.job_url,),
    ).fetchone()
    cached_result = None
    if row and row["optimize_result"]:
        try:
            cached_result = json.loads(row["optimize_result"])
        except Exception:
            cached_result = None
        if cached_result and not req.force_refresh and row["optimize_at"]:
            try:
                t = datetime.datetime.fromisoformat(row["optimize_at"])
                if (datetime.datetime.now() - t).total_seconds() < 86400:
                    cached_result["_cached"] = True
                    cached_result["_cached_at"] = row["optimize_at"]
                    return cached_result
            except Exception:
                pass

    # RAG: 检索历史相似JD经验
    from boss_rag import similar_jds, build_rag_context, ensure_jd_embedding
    desc = desc.strip()
    if desc and req.job_url:
        ensure_jd_embedding(req.job_url, desc)
    rag_context = ""
    if desc:
        similar = similar_jds(desc, limit=3, exclude_url=req.job_url or "")
        rag_context = build_rag_context(similar, "optimize")

    prompt = f"""你是站在求职者一侧的简历审计官和优化专家。根据以下岗位JD，生成简历优化建议。

{rag_context}
## 岗位信息
- 公司: {company}
- 职位: {title}
- JD: {desc[:3000]}

{"## 求职者当前简历" + chr(10) + resume[:2000] if resume and len(resume.strip()) > 5 else "（求职者未提供简历，请基于JD给出通用优化建议）"}

## 输出格式（严格JSON）
{{
  "one_line": "一句话结论：这份简历最需要改什么",
  "key_requirements": ["JD最核心的3-5个要求"],
  "match_gaps": ["简历中缺少但JD强调的方向", "..."],
  "optimize_tips": [
    {{"area": "需要优化的模块", "current": "当前写了什么（如有简历）", "suggestion": "建议改为什么", "why": "为什么要这样改"}},
    ...
  ],
  "keywords_to_add": ["需要在简历中加入的关键词"],
  "action_items": ["立刻修改的1-3个最高优先级事项"],
  "rewrite_example": "一段改写后的项目经历示例（改写前后对比）"
}}

## 改写原则
1. 不编造数据、项目、经历
2. 把"负责了什么"改成"做成了什么"
3. 每条经历体现：动作 + 产物 + 结果
4. 量化优先：没有精确数字时用范围、频率、规模
5. 关键词匹配JD优先于通用优化
6. 用简体中文"""

    try:
        sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import llm_chat_deepseek

        raw = llm_chat_deepseek(
            [{"role": "user", "content": prompt}],
            system_prompt="你是求职简历优化专家，输出严格JSON，用简体中文。",
            temperature=0.4,
        )
        import json

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        if raw.lower().startswith("```json"):
            raw = raw[7:].strip()
        result = json.loads(raw)
        try:
            db.execute(
                "UPDATE applications SET optimize_result=?, optimize_at=CURRENT_TIMESTAMP WHERE job_url=?",
                (json.dumps(result, ensure_ascii=False), req.job_url),
            )
            db.commit()
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": f"AI优化失败: {e}", "one_line": "请检查AI配置", "optimize_tips": [], "action_items": []}


class ChatSuggestionRequest(BaseModel):
    job_url: str
    job_title: Optional[str] = ""
    company: Optional[str] = ""
    description: Optional[str] = ""
    hr_name: Optional[str] = ""
    hr_title: Optional[str] = ""
    is_boss: Optional[bool] = False


@app.post("/api/jobs/chat-suggestion")
async def chat_suggestion(req: ChatSuggestionRequest):
    """根据岗位JD + HR信息生成沟通建议（打招呼话术、聊什么、避雷点）。
    缓存到 DB chat_suggestion_result / chat_suggestion_at，24h 内复用。
    """
    desc = req.description or ""
    title = req.job_title or ""
    company = req.company or ""
    hr_name = req.hr_name or ""
    hr_title = req.hr_title or ""
    is_boss = req.is_boss or False

    if not desc and req.job_url:
        existing = get_application_by_url(req.job_url)
        if existing:
            desc = existing.get("description") or desc
            title = title or existing.get("job_title", "")
            company = company or existing.get("company", "")
            hr_name = hr_name or existing.get("hr_name", "")
            hr_title = hr_title or existing.get("hr_title", "")
            is_boss = is_boss or bool(existing.get("is_boss"))

    import datetime
    from boss_state import get_db

    db = get_db()
    row = db.execute(
        "SELECT chat_suggestion_result, chat_suggestion_at FROM applications WHERE job_url=?",
        (req.job_url,),
    ).fetchone()
    cached_result = None
    if row and row["chat_suggestion_result"]:
        try:
            cached_result = json.loads(row["chat_suggestion_result"])
        except Exception:
            cached_result = None
        if cached_result and row["chat_suggestion_at"]:
            try:
                t = datetime.datetime.fromisoformat(row["chat_suggestion_at"])
                if (datetime.datetime.now() - t).total_seconds() < 86400:
                    cached_result["_cached"] = True
                    cached_result["_cached_at"] = row["chat_suggestion_at"]
                    return cached_result
            except Exception:
                pass

    resume = get_setting("resume_summary", "")

    # RAG: 检索历史相似JD经验
    from boss_rag import similar_jds, build_rag_context, ensure_jd_embedding
    desc = desc.strip()
    if desc and req.job_url:
        ensure_jd_embedding(req.job_url, desc)
    rag_context = ""
    if desc:
        similar = similar_jds(desc, limit=3, exclude_url=req.job_url or "")
        rag_context = build_rag_context(similar, "chat")

    boss_hint = "对方很可能是公司老板/法人本人，语气要更诚意、更直接" if is_boss else ""
    hr_ctx = f"HR姓名: {hr_name}" + (f"，头衔: {hr_title}" if hr_title else "")
    if is_boss and hr_name:
        hr_ctx += f"（⚠ {hr_name}是公司法人/老板）"

    prompt = f"""你是求职沟通教练，帮求职者生成和 HR 的沟通策略。

{rag_context}
## 岗位信息
- 公司: {company}
- 职位: {title}
- JD: {desc[:2000]}

## HR 信息
- {hr_ctx}
{("- " + boss_hint) if boss_hint else ""}

{"## 求职者简历摘要" + chr(10) + resume[:1500] if resume and len(resume.strip()) > 5 else ""}

## 输出格式（严格JSON）
{{
  "icebreaker": "一句自然的第一句话打招呼（10-25字，像真人说话，不客套）",
  "chat_topics": [
    {{"topic": "话题方向", "angle": "怎么说", "example": "具体话术示例"}},
    ...
  ],
  "avoid": ["千万别说的话/踩雷点", "..."],
  "follow_up": "如果对方不回复，怎么优雅跟进的话术",
  "close_deal": "如何引导到面试/后续的话术",
  "tone_tip": "整体沟通风格建议（1-2句）"
}}

## 原则
1. 打招呼语必须紧扣JD内容，不说空话套话
2. 不要列技能清单，要找JD中感兴趣的具体点聊
3. 避免过于卑微或过于自信
4. {boss_hint if boss_hint else "HR是招聘方代表，专业自然即可"}
5. 用简体中文"""

    try:
        sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import llm_chat_deepseek

        raw = llm_chat_deepseek(
            [{"role": "user", "content": prompt}],
            system_prompt="你是求职沟通教练，输出严格JSON，用简体中文。",
            temperature=0.6,
        )
        import json as _json

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        if raw.lower().startswith("```json"):
            raw = raw[7:].strip()
        result = _json.loads(raw)
        try:
            db.execute(
                "UPDATE applications SET chat_suggestion_result=?, chat_suggestion_at=CURRENT_TIMESTAMP WHERE job_url=?",
                (_json.dumps(result, ensure_ascii=False), req.job_url),
            )
            db.commit()
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": f"沟通建议生成失败: {e}", "icebreaker": "", "chat_topics": [], "avoid": []}


# ══════════════════════════════════════
#  候选池
# ══════════════════════════════════════


@app.get("/api/shortlists")
def get_shortlists():
    return {"shortlists": list_shortlists()}


@app.post("/api/shortlists")
def add_shortlist(req: dict = {}):
    url = req.get("job_url", "")
    if not url:
        raise HTTPException(status_code=400, detail="缺少 job_url")
    if is_in_shortlist(url):
        return {"status": "already_exists"}
    sid = add_to_shortlist(
        url,
        req.get("title", ""),
        req.get("company", ""),
        req.get("salary", ""),
        req.get("city", ""),
        req.get("note", ""),
    )
    if sid:
        return {"status": "ok", "id": sid}
    return {"status": "duplicate"}


@app.delete("/api/shortlists/{sid}")
def remove_shortlist(sid: int):
    remove_from_shortlist(sid)
    return {"status": "ok"}


# ══════════════════════════════════════
#  会话 & 聊天
# ══════════════════════════════════════


@app.get("/api/wechat-exchanges")
def list_wechat_exchanges():
    """返回所有已获取到 HR 微信号的会话。"""
    records = get_wechat_exchanges()
    return {"exchanges": records}


@app.get("/api/transfer-requests")
def list_transfer_requests():
    """返回所有 HR 要求转人工的会话。"""
    records = get_transfer_requests()
    return {"requests": records}


@app.get("/api/conversations")
def list_conversations():
    convs = list_active_conversations()
    return {"conversations": convs}


@app.post("/api/conversations/sync-list")
async def sync_conversation_list():
    """手动触发：在「全部」Tab 下抽取每张会话卡的 company / job_title / last_msg 并写回 DB。
    台账 P10 修复入口。
    """
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    try:
        updated = await asyncio.wait_for(_run_pw(automation.sync_conversation_list_full), timeout=15.0)
        return {"success": True, "updated": int(updated or 0)}
    except asyncio.TimeoutError:
        return {"success": False, "error": "browser_busy", "detail": "浏览器线程繁忙，稍后重试"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败: {e}")


@app.get("/api/conversations/{conv_id}")
def get_conversation_detail(conv_id: int):
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = _clean_messages_for_web(get_messages(conv_id, 100))
    return {"conversation": conv, "messages": messages}


@app.get("/api/conversations/{conv_id}/messages")
def get_conversation_messages(conv_id: int, limit: int = 50):
    # 这个接口被前端频繁轮询，必须只读本地缓存，不能每次都控制浏览器。
    return {"messages": _clean_messages_for_web(get_messages(conv_id, limit))}


@app.post("/api/conversations/{conv_id}/sync")
async def sync_conversation_messages(conv_id: int):
    """按需从当前 BOSS 浏览器会话同步一次消息。"""
    global browser_sync_lock
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not automation or automation.page is None:
        return {
            "success": False,
            "message": "浏览器未启动",
            "messages": _clean_messages_for_web(get_messages(conv_id, 100)),
        }

    hr_name = conv.get("hr_name", "")
    if not hr_name:
        raise HTTPException(status_code=400, detail="会话缺少HR姓名")

    if browser_sync_lock is None:
        browser_sync_lock = asyncio.Lock()
    if browser_sync_lock.locked():
        return {
            "success": False,
            "message": "浏览器正忙，先显示缓存",
            "messages": _clean_messages_for_web(get_messages(conv_id, 100)),
        }

    try:
        async with browser_sync_lock:
            opened = await asyncio.wait_for(_run_pw(automation.open_conversation_by_name, hr_name), timeout=8)
            if not opened:
                return {
                    "success": False,
                    "message": f"无法打开 {hr_name} 的会话",
                    "messages": _clean_messages_for_web(get_messages(conv_id, 100)),
                }

            live_messages = await asyncio.wait_for(_run_pw(automation.read_visible_messages), timeout=5)
            if live_messages:
                replace_conversation_messages(conv_id, live_messages)
                last = live_messages[-1]
                update_conversation_last_message(conv_id, last.get("content", ""), last.get("sender", "hr"))
    except asyncio.TimeoutError:
        return {
            "success": False,
            "message": "同步超时，先显示缓存",
            "messages": _clean_messages_for_web(get_messages(conv_id, 100)),
        }

    return {
        "success": True,
        "messages": _clean_messages_for_web(get_messages(conv_id, 100)),
    }


@app.post("/api/conversations/{conv_id}/send")
async def send_manual_message(conv_id: int, req: SendMessageRequest):
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    hr_name = conv.get("hr_name", "")
    if not hr_name:
        raise HTTPException(status_code=400, detail="会话缺少HR姓名")

    # 先打开对应会话
    opened = await _run_pw(automation.open_conversation_by_name, hr_name)
    if not opened:
        raise HTTPException(status_code=500, detail=f"无法在浏览器中打开 {hr_name} 的会话")

    browser_ok = await _run_pw(automation.send_message, req.content, False)
    if not browser_ok:
        raise HTTPException(status_code=500, detail="浏览器发送失败，本地不会写入这条消息")

    add_message(conv_id, "me", req.content, ai_generated=False)
    update_conversation_last_message(conv_id, req.content, "me")
    await broadcast_ws(
        {
            "type": "manual_message_sent",
            "conversation_id": conv_id,
        }
    )
    return {"success": True, "browser_sent": browser_ok}


@app.post("/api/conversations/{conv_id}/open")
async def open_conversation_in_browser(conv_id: int):
    """在浏览器中打开对应会话。"""
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    hr_name = conv.get("hr_name", "")
    if not hr_name:
        raise HTTPException(status_code=400, detail="会话缺少HR姓名")
    success = await _run_pw(automation.open_conversation_by_name, hr_name)
    return {
        "success": success,
        "message": f"已在浏览器中打开 {hr_name} 的会话" if success else "打开失败",
    }


@app.post("/api/conversations/{conv_id}/pause")
async def pause_auto_reply(conv_id: int):
    set_auto_reply(conv_id, False)
    await broadcast_ws(
        {
            "type": "auto_reply_toggled",
            "conversation_id": conv_id,
            "enabled": False,
        }
    )
    return {"status": "ok"}


@app.post("/api/conversations/{conv_id}/resume")
async def resume_auto_reply(conv_id: int):
    set_auto_reply(conv_id, True)
    update_conversation_status(conv_id, "active")
    await broadcast_ws(
        {
            "type": "auto_reply_toggled",
            "conversation_id": conv_id,
            "enabled": True,
        }
    )
    return {"status": "ok"}


# ══════════════════════════════════════
#  地区 / 城市 / 区商圈（基于 BOSS 接口）
# ══════════════════════════════════════


@app.get("/api/geo/cities")
def geo_cities(force: bool = False):
    """BOSS 支持的招聘城市列表。"""
    try:
        from boss_geo import get_cities

        return {"cities": get_cities(force=force)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取城市失败: {e}")


@app.get("/api/debug/auth")
async def debug_auth():
    """临时调试端点：查看缓存的鉴权状态。"""
    from boss_geo import resolve_city_code

    cached = _get_cached_headers()
    zp_ok = bool(cached and cached.get("zp_token"))
    cookie_ok = bool(cached and cached.get("Cookie"))
    # 强制刷新一次
    if automation and automation.page:
        try:
            await asyncio.wait_for(_run_pw(_refresh_zp_cache), timeout=8.0)
        except Exception:
            pass
    fresh = _get_cached_headers()
    return {
        "cached_before": {"zp_token": zp_ok, "cookie": cookie_ok},
        "cached_after": {
            "zp_token": bool(fresh and fresh.get("zp_token")),
            "cookie": bool(fresh and fresh.get("Cookie")),
            "cookie_len": len(fresh.get("Cookie", "")) if fresh else 0,
        },
        "browser_running": bool(automation and automation.page),
    }


@app.get("/api/geo/districts")
async def geo_districts(city: str, force: bool = False):
    """某城市下的区/商圈列表。优先直连 BOSS 公开接口，失败时回退浏览器 fetch。"""
    if not city:
        raise HTTPException(status_code=400, detail="缺少 city 参数")
    try:
        from boss_geo import resolve_city_code, _parse_districts, _cache, get_districts
        import time as _time

        city_code = resolve_city_code(city)
        if not city_code:
            raise HTTPException(status_code=404, detail=f"未找到城市: {city}")

        if not force:
            cached = _cache["districts"].get(city_code)
            ts = _cache["districts_ts"].get(city_code, 0)
            if cached and (_time.time() - ts) < _cache["ttl_sec"]:
                return {"city": city, "city_code": city_code, "districts": cached}

        try:
            districts = await asyncio.wait_for(
                asyncio.to_thread(get_districts, city_code, force),
                timeout=6.0,
            )
            if districts:
                return {"city": city, "city_code": city_code, "districts": districts}
        except asyncio.TimeoutError:
            districts = []
        except Exception:
            districts = []

        if not (automation and automation.page):
            cached = _cache["districts"].get(city_code) or []
            return {"city": city, "city_code": city_code, "districts": cached}

        def _fetch():
            try:
                url = f"https://www.zhipin.com/wapi/zpgeek/businessDistrict.json?cityCode={city_code}"
                return automation.page.evaluate(
                    "async (url) => { try { const r = await fetch(url); return await r.json(); } catch(e) { return {code: -1, error: String(e)}; } }",
                    url,
                )
            except Exception as e:
                print(f"[DEBUG geo] _fetch exception: {e}")
                return None

        raw = await asyncio.wait_for(_run_pw(_fetch), timeout=10.0)

        if not raw or raw.get("code") != 0:
            cached = _cache["districts"].get(city_code)
            return {"city": city, "city_code": city_code, "districts": cached or []}

        if isinstance(raw, dict) and raw.get("source") and isinstance(raw.get("data"), list):
            items = raw["data"]
            districts = [{"name": d.get("name", ""), "code": str(d.get("code", ""))} for d in items if d.get("name")]
            by_name = {d["name"]: d["code"] for d in districts}
            by_code = {d["code"]: d["name"] for d in districts}
        else:
            districts, by_name, by_code = _parse_districts(raw)

        _cache["districts"][city_code] = districts
        _cache["districts_ts"][city_code] = _time.time()
        _cache["district_by_name"][city_code] = by_name
        _cache["district_by_code"][city_code] = by_code

        return {"city": city, "city_code": city_code, "districts": districts}
    except asyncio.TimeoutError:
        return {"city": city, "city_code": city_code, "districts": _cache["districts"].get(city_code, [])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取区域失败: {e}")


@app.get("/api/geo/areas")
async def geo_areas(city: str):
    """某城市下的「工作区域」列表（6位行政区域码）。复用 districts 缓存过滤。"""
    if not city:
        raise HTTPException(status_code=400, detail="缺少 city 参数")
    try:
        from boss_geo import resolve_city_code, _cache

        city_code = resolve_city_code(city)
        if not city_code:
            raise HTTPException(status_code=404, detail=f"未找到城市: {city}")

        districts = _cache["districts"].get(city_code) or []
        areas = [d for d in districts if d.get("code", "").isdigit() and len(d.get("code", "")) == 6]
        return {"city": city, "city_code": city_code, "areas": areas}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取工作区域失败: {e}")


# ══════════════════════════════════════
#  设置
# ══════════════════════════════════════


@app.get("/api/settings")
def read_settings():
    settings = get_all_settings()
    # 检查AI Key是否已配置
    ai_key = settings.get("ai_api_key", "")
    settings["ai_key_configured"] = "true" if ai_key and len(ai_key) > 10 else "false"
    return {"settings": settings}


@app.put("/api/settings")
async def update_settings(req: SettingsUpdate):
    updates = {}
    # greeting_enabled → 实际 key 是 ai_greeting_enabled
    _key_alias = {"greeting_enabled": "ai_greeting_enabled"}
    for k, v in req.model_dump().items():
        actual_key = _key_alias.get(k, k)
        if actual_key == "ai_api_key" and v:
            set_setting("ai_api_key", str(v))
            updates["ai_key_configured"] = "true"
            continue
        # 允许清空个人微信：前端传空字符串时覆盖为""
        if actual_key == "wechat_id" and (v is None or v == ""):
            set_setting("wechat_id", "")
            updates["wechat_id"] = ""
            continue
        if v is not None and v != "":
            set_setting(actual_key, str(v))
            updates[actual_key] = str(v)
    await broadcast_ws({"type": "settings_updated", "updates": updates})
    return {"status": "ok", "updated": updates}


# ══════════════════════════════════════
#  模拟面试 — AI 对话式面试
# ══════════════════════════════════════


@app.post("/api/interview/start")
async def interview_start(req: InterviewStartRequest):
    """启动模拟面试，生成开场白 + 第一道题（对话式）。"""
    import sys, os
    sys.path.insert(0, str(Path(__file__).parent / "interview"))
    from engine import InterviewEngine

    # 检查 AI Key
    try:
        from llm_client import _load_ai_config
        cfg = _load_ai_config()
        if not cfg.get("api_key") or len(cfg["api_key"]) < 10:
            raise HTTPException(status_code=400, detail="AI API Key 未配置")
    except HTTPException:
        raise
    except Exception:
        pass

    # 从岗位 URL 读取岗位信息作为面试上下文
    job_focus = req.job_focus or ""
    job_title = ""
    job_desc = ""
    if req.job_url and not job_focus:
        try:
            from boss_state import get_application_by_url
            job = get_application_by_url(req.job_url)
            if job:
                job_title = job.get("job_title") or job.get("title") or ""
                job_desc = (job.get("description") or "")[:500]
                if job_title:
                    job_focus = job_title
                if job_desc:
                    job_focus = f"{job_title} —— {job_desc[:120]}"
        except Exception:
            pass

    # 读取是否使用本地模型
    use_local = get_setting("use_local_model", "false") == "true"

    engine = InterviewEngine(job_focus=job_focus or "", use_local_model=use_local)
    try:
        result = engine.start()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成面试题失败: {e}")

    interview_sessions[engine.session_id] = engine

    return {
        "session_id": engine.session_id,
        "message": result["message"],
        "question_count": result["question_count"],
        "job_title": job_title,
    }


@app.post("/api/interview/answer")
async def interview_answer(req: InterviewAnswerRequest):
    """对话式面试：提交回答，获取面试官回复（评价 + 追问）。"""
    engine = interview_sessions.get(req.session_id)
    if not engine:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    if not req.answer.strip():
        raise HTTPException(status_code=400, detail="回答不能为空")

    try:
        reply = engine.chat(req.answer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {e}")

    return reply


@app.post("/api/interview/skip")
async def interview_skip(req: InterviewSessionRequest):
    """跳过当前话题，让面试官换个方向提问。"""
    engine = interview_sessions.get(req.session_id)
    if not engine:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    try:
        reply = engine.chat("我想跳过这个话题，请换一个方向提问")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"跳过失败: {e}")

    return reply


@app.post("/api/interview/end")
async def interview_end(req: InterviewSessionRequest):
    """结束面试，生成总结。"""
    engine = interview_sessions.pop(req.session_id, None)
    if not engine:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    try:
        summary = engine.end_session()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成总结失败: {e}")

    return {"summary": summary}


@app.get("/api/interview/sessions")
def interview_list_sessions():
    """列出所有已保存的面试会话（含进行中和已结束）。"""
    try:
        from boss_state import list_all_interview_sessions as _list
        return {"sessions": _list()}
    except Exception as e:
        return {"sessions": [], "error": str(e)}


@app.post("/api/interview/resume")
async def interview_resume(req: InterviewSessionRequest):
    """恢复一个进行中的面试会话，接着上次的对话继续。"""
    from boss_state import get_interview_session as _get

    saved = _get(req.session_id)
    if not saved:
        raise HTTPException(status_code=404, detail="会话不存在")
    if saved.get("status") != "active":
        raise HTTPException(status_code=400, detail="该会话已结束，不能继续")

    # 检查是否已存在于内存中
    engine = interview_sessions.get(req.session_id)
    if not engine:
        import sys as _sys, os as _os
        _sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from engine import InterviewEngine
        use_local = get_setting("use_local_model", "false") == "true"
        engine = InterviewEngine.from_saved(saved, use_local_model=use_local)
        interview_sessions[req.session_id] = engine

    return {
        "session_id": engine.session_id,
        "message": engine._last_question,
        "question_count": engine.round_count,
        "history": engine.history,
    }


# ══════════════════════════════════════
#  知识库批量导入
# ══════════════════════════════════════

@app.post("/api/qa/import-questions")
async def qa_import_questions(data: dict):
    """批量导入面试题（仅问题，LLM 自动生成答案）"""
    import sys, os, time
    from datetime import datetime
    ts = lambda: datetime.now().strftime('%H:%M:%S')
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "interview"))
    from db import add_qa_pair
    from llm_client import llm_chat_deepseek, llm_chat_ollama

    category = data.get("category", "通用")
    text = (data.get("text") or "").strip()
    difficulty = data.get("difficulty", "medium")
    skills = data.get("skills", "")

    if not text:
        raise HTTPException(status_code=400, detail="文本不能为空")

    questions = [q.strip() for q in text.split("\n") if q.strip()]
    if not questions:
        raise HTTPException(status_code=400, detail="未解析到有效问题")

    if len(questions) > 50:
        raise HTTPException(status_code=400, detail="单次最多导入 50 条")

    print(f"\n[{ts()}] ═══ 开始批量导入（仅问题模式） ═══")
    print(f"[{ts()}] 分类: {category} | 难度: {difficulty} | 技能: {skills or '无'}")
    print(f"[{ts()}] 待导入: {len(questions)} 题")

    imported, failed = 0, 0
    t_total = time.time()
    for i, q in enumerate(questions, 1):
        t_item = time.time()
        print(f"[{ts()}] [{i}/{len(questions)}] 📝 {q[:60]}{'...' if len(q)>60 else ''}")
        try:
            # 调 LLM 生成答案
            prompt = f"你是一位资深面试官。请用中文简洁、专业地回答以下面试题（200-400字内），给出核心要点和技术细节：\n\n{q}"
            t_llm = time.time()
            try:
                answer = llm_chat_deepseek([{"role": "user", "content": prompt}], temperature=0.3)
                print(f"[{ts()}]   ├─ ☁️ 答案生成完成 (DeepSeek, {time.time()-t_llm:.1f}s, {len(answer)}字)")
            except Exception:
                answer = llm_chat_ollama([{"role": "user", "content": prompt}], temperature=0.3)
                print(f"[{ts()}]   ├─ 🤖 答案生成完成 (Ollama, {time.time()-t_llm:.1f}s, {len(answer)}字)")

            t_db = time.time()
            add_qa_pair(category, q, answer, difficulty, skills)
            imported += 1
            print(f"[{ts()}]   └─ ✅ 写入数据库 (embedding+insert {time.time()-t_db:.1f}s, 总耗时 {time.time()-t_item:.1f}s)")
        except Exception as e:
            failed += 1
            print(f"[{ts()}]   └─ ❌ 失败: {e}")

    elapsed = time.time() - t_total
    print(f"[{ts()}] ═══ 导入完成: 成功 {imported}, 失败 {failed}, 总耗时 {elapsed:.1f}s ═══\n")

    return {"imported": imported, "failed": failed, "total": len(questions)}


@app.post("/api/qa/import-pairs")
async def qa_import_pairs(data: dict):
    """批量导入面试题（问题+答案，从文本解析）"""
    import sys, os, time
    from datetime import datetime
    ts = lambda: datetime.now().strftime('%H:%M:%S')
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "interview"))
    from db import add_qa_pair

    category = data.get("category", "通用")
    text = (data.get("text") or "").strip()
    difficulty = data.get("difficulty", "medium")
    skills = data.get("skills", "")

    if not text:
        raise HTTPException(status_code=400, detail="文本不能为空")

    # 解析 Q&A 对，支持格式：Q:... \n A:... 或 问：... \n 答：... 或 --- 分隔
    pairs = _parse_qa_text(text)
    if not pairs:
        raise HTTPException(status_code=400, detail="未解析到有效 Q&A 对，请使用格式：Q:问题\\nA:答案")

    if len(pairs) > 50:
        raise HTTPException(status_code=400, detail="单次最多导入 50 条")

    print(f"\n[{ts()}] ═══ 开始批量导入（Q&A 模式） ═══")
    print(f"[{ts()}] 分类: {category} | 难度: {difficulty} | 技能: {skills or '无'}")
    print(f"[{ts()}] 待导入: {len(pairs)} 题")

    imported, failed = 0, 0
    t_total = time.time()
    for i, (q, a) in enumerate(pairs, 1):
        t_item = time.time()
        print(f"[{ts()}] [{i}/{len(pairs)}] 📝 {q[:60]}{'...' if len(q)>60 else ''}")
        try:
            add_qa_pair(category, q, a, difficulty, skills)
            imported += 1
            print(f"[{ts()}]   └─ ✅ 写入数据库 (embedding+insert {time.time()-t_item:.1f}s)")
        except Exception as e:
            failed += 1
            print(f"[{ts()}]   └─ ❌ 失败: {e}")

    elapsed = time.time() - t_total
    print(f"[{ts()}] ═══ 导入完成: 成功 {imported}, 失败 {failed}, 总耗时 {elapsed:.1f}s ═══\n")

    return {"imported": imported, "failed": failed, "total": len(pairs)}


def _parse_qa_text(text: str):
    """从文本中解析 Q&A 对"""
    pairs = []
    # 尝试 Q:...A:... 格式
    if re.search(r'^Q\s*[:：]', text, re.MULTILINE | re.IGNORECASE):
        blocks = re.split(r'\n(?=Q\s*[:：])', text, flags=re.MULTILINE | re.IGNORECASE)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            parts = re.split(r'\nA\s*[:：]', block, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                q = re.sub(r'^Q\s*[:：]\s*', '', parts[0], flags=re.IGNORECASE).strip()
                a = parts[1].strip()
                if q and a:
                    pairs.append((q, a))
        if pairs:
            return pairs

    # 尝试 问：...答：... 格式
    if re.search(r'^问\s*[:：]', text, re.MULTILINE):
        blocks = re.split(r'\n(?=问\s*[:：])', text, flags=re.MULTILINE)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            parts = re.split(r'\n答\s*[:：]', block, maxsplit=1)
            if len(parts) == 2:
                q = re.sub(r'^问\s*[:：]\s*', '', parts[0]).strip()
                a = parts[1].strip()
                if q and a:
                    pairs.append((q, a))
        if pairs:
            return pairs

    # 尝试 --- 分隔的格式
    blocks = re.split(r'\n---+\n', text)
    if len(blocks) >= 2:
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            lines = block.strip().split("\n")
            # 第一行是问题，后面是答案
            if len(lines) >= 2:
                q = lines[0].strip()
                a = "\n".join(lines[1:]).strip()
                if q and a:
                    pairs.append((q, a))
        if pairs:
            return pairs

    return pairs


@app.get("/api/qa/categories")
async def qa_categories():
    """获取知识库分类统计"""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "interview"))
    from db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT category, COUNT(*) as cnt FROM interview_qa_pairs GROUP BY category ORDER BY cnt DESC")
            rows = cur.fetchall()
            categories = [f"{r['category']}({r['cnt']})" for r in rows]
            return {"qa_categories": categories, "total": sum(r["cnt"] for r in rows)}
    except Exception:
        return {"qa_categories": [], "total": 0}
    finally:
        conn.close()


@app.post("/api/qa/ask")
async def qa_ask(req: dict):
    """知识库问答：RAG 检索 + 多路召回 + 融合排序"""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "interview"))
    from fast_qa import fast_answer

    question = (req.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    result = fast_answer(question)
    return {
        "question": question,
        "answer": result["answer"],
        "matched_question": result.get("matched", ""),
        "category": result.get("category", ""),
        "topic": result.get("topic"),
        "confidence": result.get("confidence", "high"),
        "note": result.get("note", ""),
        "layer": result.get("layer", -1),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "related": result.get("related", []),
        "recall_detail": result.get("recall_detail", {}),
    }

# ══════════════════════════════════════
#  面试回顾 — 历史记录 & 弱项分析
# ══════════════════════════════════════

@app.get("/api/review/sessions")
def api_review_sessions():
    """获取所有面试会话列表（含状态、轮次、岗位方向）。"""
    sessions_map = {}  # session_id → metadata

    # 1. 从 SQLite 拉会话元数据（含 status, round_count, job_focus）
    try:
        from boss_state import list_all_interview_sessions as _list
        for s in _list():
            sid = s["session_id"]
            sessions_map[sid] = {
                "session_id": sid,
                "job_focus": s.get("job_focus", ""),
                "round_count": s.get("round_count", 0),
                "status": s.get("status", "active"),
                "created_at": s.get("created_at", ""),
                "updated_at": s.get("updated_at", ""),
                "question_count": 0,
            }
    except Exception as e:
        pass  # SQLite 不可用时退回纯 MySQL

    # 2. 从 MySQL 补问题数
    try:
        from interview.db import get_all_session_ids, get_session_summary
        mysql_sids = get_all_session_ids()
        for sid in mysql_sids:
            try:
                summary = get_session_summary(sid)
                qc = summary.get("total_questions", 0)
            except Exception:
                qc = 0
            if sid in sessions_map:
                sessions_map[sid]["question_count"] = qc
            else:
                sessions_map[sid] = {
                    "session_id": sid,
                    "job_focus": "",
                    "round_count": qc,
                    "status": "ended",
                    "created_at": "",
                    "updated_at": "",
                    "question_count": qc,
                }
    except Exception:
        pass

    return {"sessions": list(sessions_map.values())}


@app.get("/api/review/session/{session_id}")
def api_review_session(session_id: str):
    """获取某个面试会话的详细记录。"""
    try:
        from interview.db import get_session_summary
        return get_session_summary(session_id)
    except Exception as e:
        return {"session_id": session_id, "total_questions": 0, "error": str(e)}


@app.get("/api/review/weak-areas")
def api_review_weak_areas(limit: int = 10):
    """薄弱环节分析 — 得分最低的题目 + 各类别均分。"""
    try:
        from interview.db import get_weak_areas, get_category_scores
        areas = get_weak_areas(limit)
        cat_scores = get_category_scores()
        return {"weak_areas": areas, "category_scores": cat_scores}
    except Exception as e:
        return {"weak_areas": [], "category_scores": [], "error": str(e)}


# ══════════════════════════════════════
#  Agent — ReAct 自主求职智能体
# ══════════════════════════════════════


@app.get("/api/agent/tools")
def agent_list_tools():
    """列出 Agent 可用的所有工具。"""
    registry = ToolRegistry()
    ToolContext.init(automation=automation, run_pw=_run_pw)
    register_all(registry)
    return {
        "tools": [
            {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
            for t in registry.get_openai_schema()
        ]
    }


@app.post("/api/agent/run")
async def agent_run(req: AgentRunRequest):
    """启动 ReAct Agent 自主执行求职任务。

    Agent 会根据 goal 自主决策：搜索 → 分析 → 筛选 → 投递 → 总结。
    每步进度通过 WebSocket 实时推送（type: agent_step）。
    """
    global monitor_paused, browser_sync_lock

    if not req.goal or len(req.goal.strip()) < 5:
        raise HTTPException(status_code=400, detail="goal 太短，请描述你的求职目标")

    # 检查 AI Key
    try:
        sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import _load_ai_config

        cfg = _load_ai_config()
        if not cfg.get("api_key") or len(cfg["api_key"]) < 10:
            raise HTTPException(status_code=400, detail="AI API Key 未配置，请先在设置页配置")
    except HTTPException:
        raise
    except Exception:
        pass

    # 暂停监控 + 获取浏览器锁
    was_paused = monitor_paused
    monitor_paused = True
    for _ in range(20):
        await asyncio.sleep(0.5)
        if browser_sync_lock is None or not browser_sync_lock.locked():
            break
    if browser_sync_lock is None:
        browser_sync_lock = asyncio.Lock()
    await browser_sync_lock.acquire()
    try:
        # 构建 ToolRegistry
        ToolContext.init(automation=automation, run_pw=_run_pw)
        registry = ToolRegistry()
        register_all(registry)

        # LLM 调用适配器
        def _agent_llm_chat(messages: list, temperature: float = 0.3) -> str:
            sys.path.insert(0, str(Path(__file__).parent / "interview"))
            from llm_client import llm_chat_deepseek

            return llm_chat_deepseek(messages, temperature=temperature)

        # 每步回调：推送到 WebSocket
        async def _on_step(step_record: dict):
            await broadcast_ws({
                "type": "agent_step",
                "step": step_record.get("step"),
                "thought": step_record.get("thought", ""),
                "tool": step_record.get("tool", ""),
                "args": step_record.get("args", {}),
                "result_preview": (step_record.get("result") or "")[:200],
            })

        # 创建并运行 Agent
        loop = AgentLoop(
            registry=registry,
            llm_chat=_agent_llm_chat,
            goal=req.goal.strip(),
            max_steps=req.max_steps,
            on_step=_on_step,
        )

        await broadcast_ws({
            "type": "agent_started",
            "goal": req.goal,
            "max_steps": req.max_steps,
        })

        result = await loop.run()

        await broadcast_ws({
            "type": "agent_complete",
            "steps": result.get("steps", 0),
            "summary": result.get("summary", "")[:500],
        })

        return result

    except Exception as e:
        await broadcast_ws({
            "type": "agent_error",
            "message": str(e),
        })
        raise HTTPException(status_code=500, detail=f"Agent 执行失败: {e}")
    finally:
        if browser_sync_lock and browser_sync_lock.locked():
            browser_sync_lock.release()
        monitor_paused = was_paused


# ══════════════════════════════════════
#  WebSocket
# ══════════════════════════════════════


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        await websocket.send_json(
            {
                "type": "connected",
                "status": {
                    "browser_running": automation is not None,
                    "monitor_running": monitor_task is not None and not monitor_task.done(),
                },
            }
        )
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ══════════════════════════════════════
#  后台监控循环
# ══════════════════════════════════════


async def chat_monitor_loop():
    """后台轮询聊天消息 + 自动回复。带 session 心跳保活。"""
    global automation, browser_sync_lock
    await asyncio.sleep(3)  # 启动后简短等待

    if automation:
        print("[监控] 后台监控任务已启动")
        await _run_pw(automation.keep_alive)

    # 验证 AI 回复系统
    try:
        sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import _load_ai_config

        cfg = _load_ai_config()
        if cfg["api_key"] and len(cfg["api_key"]) > 10:
            print(f"[监控] AI API 已配置（{cfg['model']}），自动回复就绪")
        else:
            print("[监控] ⚠️ AI API Key 未配置，请在前端设置页配置")
    except Exception as e:
        print(f"[监控] ⚠️ AI 回复系统加载失败: {e}")

    # 首次立即跑一轮监控，不等延迟
    if automation and not monitor_paused:
        print("[监控] 执行首次会话扫描...")
        try:
            if browser_sync_lock is None:
                browser_sync_lock = asyncio.Lock()
            async with browser_sync_lock:
                result = await _run_pw(automation.run_chat_monitor_cycle)
            if result.get("new_messages", 0) > 0:
                await broadcast_ws({"type": "new_messages", "summary": result})
            if result.get("replies_sent", 0) > 0:
                await broadcast_ws({"type": "auto_reply_sent", "summary": result})
            if result.get("new_conversations"):
                await broadcast_ws({"type": "new_messages"})
            if result.get("transfer_requested"):
                await broadcast_ws({"type": "transfer_requested"})
        except Exception as e:
            print(f"  [监控] 首次扫描异常: {e}")

    _heartbeat_count = 0
    _heartbeat_misses = 0
    while True:
        try:
            min_delay = int(get_setting("min_reply_delay_sec", "15"))
            max_delay = int(get_setting("max_reply_delay_sec", "20"))
            delay = random.randint(min(min_delay, max_delay), max(min_delay, max_delay) + 5)
            await asyncio.sleep(delay)

            if monitor_paused:
                continue

            if not automation:
                continue

            # 风控冷却期：跳过本轮高风险操作（聊天监控会发消息）
            try:
                if automation.in_cooldown():
                    continue
            except Exception:
                pass

            # 每轮轻量检查登录态（不导航，不触发BOSS反爬）
            _heartbeat_count += 1
            alive = await _run_pw(automation.heartbeat)
            if not alive:
                await asyncio.sleep(5)
                alive = await _run_pw(automation.heartbeat)

            if not alive:
                _heartbeat_misses += 1
            else:
                _heartbeat_misses = 0

            if _heartbeat_misses >= 2:
                await broadcast_ws(
                    {
                        "type": "session_expired",
                        "message": "BOSS直聘登录已过期，请点击设置Tab的「重新扫码登录」",
                    }
                )
                break

            # 每轮都轻量保活，避免 BOSS session 超时
            if _heartbeat_count >= 1:
                await _run_pw(automation.keep_alive)

            if get_setting("auto_reply_enabled", "false") != "true":
                continue

            if browser_sync_lock is None:
                browser_sync_lock = asyncio.Lock()
            async with browser_sync_lock:
                result = await _run_pw(automation.run_chat_monitor_cycle)

            # 每轮监控后刷新 zp headers 缓存，供 geo 端点使用
            try:
                await _run_pw(_refresh_zp_cache)
            except Exception:
                pass

            if result.get("new_messages", 0) > 0:
                await broadcast_ws(
                    {
                        "type": "new_messages",
                        "summary": result,
                    }
                )
            if result.get("replies_sent", 0) > 0:
                await broadcast_ws(
                    {
                        "type": "auto_reply_sent",
                        "summary": result,
                    }
                )
            if result.get("new_conversations"):
                await broadcast_ws({"type": "new_messages"})
            if result.get("wechat_exchanged"):
                await broadcast_ws({"type": "wechat_exchanged"})
            if result.get("transfer_requested"):
                await broadcast_ws({"type": "transfer_requested"})

            safety_ok = await _run_pw(automation.check_page_safety)
            if not safety_ok:
                await broadcast_ws(
                    {
                        "type": "safety_warning",
                        "message": "检测到页面异常(验证码/登录失效/账号限制)，已暂停自动操作。请手动检查浏览器。",
                    }
                )
                break

        except asyncio.CancelledError:
            break
        except Exception as e:
            await broadcast_ws(
                {
                    "type": "error",
                    "message": f"监控循环异常: {e}",
                }
            )
            await asyncio.sleep(60)


# ══════════════════════════════════════
#  启动
# ══════════════════════════════════════


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--auto-start", action="store_true", help="启动时自动打开浏览器")
    args = parser.parse_args()

    if args.auto_start:
        global automation, monitor_task
        try:

            def _do_start():
                a = BossAutomation(headless=False)
                a.start()
                return a

            automation = _playwright_executor.submit(_do_start).result()
            print("✅ 浏览器已启动")
        except Exception as e:
            print(f"⚠️ 自动启动失败: {e}")

    print(f"\n🚀 BOSS直聘自动化控制台: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
