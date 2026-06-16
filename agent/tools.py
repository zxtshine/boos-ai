"""Agent 工具集 — ToolRegistry + ToolContext + 14个工具函数。

所有工具定义在这里，没有工厂闭包，依赖通过 ToolContext 注入。

用法:
    from agent.tools import ToolRegistry, ToolContext, register_all

    # 初始化上下文（一次）
    ToolContext.init(automation=automation, run_pw=_run_pw)

    # 注册工具
    registry = ToolRegistry()
    register_all(registry)

    # 执行
    result = await registry.execute("search_jobs", {"keyword": "AI", "city": "广州"})
"""

import json
import sys
import time
import datetime
import re as _re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

# ═══════════════════════════════════════
#  ToolContext —— 依赖注入
# ═══════════════════════════════════════


class ToolContext:
    """工具函数的运行时上下文。在服务启动后调用 init() 设置一次。"""

    _instance: Optional["ToolContext"] = None

    def __init__(self, automation=None, run_pw: Optional[Callable] = None):
        self.automation = automation  # BossAutomation 实例（可能为 None）
        self.run_pw = run_pw  # async fn → PW 线程执行

    @classmethod
    def init(cls, automation=None, run_pw: Optional[Callable] = None):
        cls._instance = cls(automation=automation, run_pw=run_pw)

    @classmethod
    def get(cls) -> "ToolContext":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_automation(cls, automation):
        cls.get().automation = automation

    @classmethod
    def has_browser(cls) -> bool:
        a = cls.get().automation
        return bool(a and a.page)


# ═══════════════════════════════════════
#  ToolRegistry
# ═══════════════════════════════════════


class ToolRegistry:
    """管理 Agent 可用工具的注册表。"""

    def __init__(self):
        self._tools: Dict[str, dict] = {}

    def register(self, name: str, description: str, parameters: Dict[str, dict], fn: Callable):
        required = list(parameters.keys())
        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": parameters, "required": required},
            "fn": fn,
        }

    def get_names(self) -> List[str]:
        return list(self._tools.keys())

    def get_tool(self, name: str) -> Optional[dict]:
        return self._tools.get(name)

    def get_openai_schema(self) -> List[dict]:
        return [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in self._tools.values()
        ]

    def get_text_description(self) -> str:
        lines = []
        for t in self._tools.values():
            props = t["parameters"].get("properties", {})
            args_desc = ", ".join(f"{k}: {v.get('type', 'string')}" for k, v in props.items())
            lines.append(f"- **{t['name']}**({args_desc}): {t['description']}")
        return "\n".join(lines)

    async def execute(self, name: str, args: dict) -> Any:
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"未知工具 '{name}'", "available_tools": self.get_names()}
        try:
            result = tool["fn"](**args)
            import asyncio
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except TypeError as e:
            return {"error": f"参数错误: {e}", "tool": name, "expected": tool["parameters"].get("required", [])}
        except Exception as e:
            return {"error": f"工具执行失败: {e}", "tool": name}


def summarize_result(result: Any, max_len: int = 2000) -> str:
    """将工具返回结果压缩为 LLM 友好的紧凑文本，避免灌原始 JSON 浪费 token。"""
    if isinstance(result, dict) and "error" in result:
        return f"错误: {result['error']}"

    # ── 岗位列表 → 一行一个岗位 ──
    if isinstance(result, dict) and "jobs" in result:
        lines = [f"{result.get('message', '')}（共{result.get('count', len(result['jobs']))}条）"]
        for i, j in enumerate(result["jobs"][:15], 1):
            parts = []
            for k in ("title", "company", "salary", "city", "experience", "education", "hr_name", "hr_active"):
                v = j.get(k, "")
                if v:
                    parts.append(str(v))
            url = j.get("url", "")
            line = f"{i}. {' | '.join(parts)}"
            if url:
                # 只保留 path 部分，省字符
                short_url = url.split("zhipin.com")[-1] if "zhipin.com" in url else url
                line += f"  → {short_url}"
            lines.append(line)
        text = "\n".join(lines)
        return text[:max_len] + (f"\n...(截断, 原{len(text)}字符)" if len(text) > max_len else "")

    # ── 会话列表 → 一行一个会话 ──
    if isinstance(result, dict) and "conversations" in result:
        lines = [f"共{result.get('count', len(result['conversations']))}个活跃会话:"]
        for i, c in enumerate(result["conversations"][:10], 1):
            parts = []
            for k in ("hr_name", "company", "job_title", "status", "interest"):
                v = c.get(k, "")
                if v:
                    parts.append(str(v))
            last = c.get("last_message", "")
            line = f"{i}. {' | '.join(parts)}"
            if last:
                line += f"  💬 {last[:60]}"
            lines.append(line)
        text = "\n".join(lines)
        return text[:max_len] + (f"\n...(截断)" if len(text) > max_len else "")

    # ── 聊天记录 → 精简格式 ──
    if isinstance(result, dict) and "messages" in result:
        lines = [f"与{result.get('hr_name','?')}({result.get('company','')})的聊天 ({result.get('count',0)}条):"]
        for m in result["messages"][:20]:
            sender = m.get("sender", "?")
            content = (m.get("content") or "")[:80]
            lines.append(f"  [{sender}] {content}")
        text = "\n".join(lines)
        return text[:max_len] + (f"\n...(截断)" if len(text) > max_len else "")

    # ── 公司画像 → 紧凑排名 ──
    if isinstance(result, dict) and "companies" in result:
        lines = [f"搜索'{result.get('keyword','')}'({result.get('city','')}) → {result.get('total_jobs',0)}岗位/{result.get('total_companies',0)}公司"]
        for i, c in enumerate(result["companies"][:10], 1):
            hr = c.get("top_hr", {})
            sample = c.get("sample_job", {})
            lines.append(f"{i}. {c.get('company','?')}  {c.get('position_count',0)}个岗位 | HR: {hr.get('name','?')}({hr.get('title','?')}) | 例: {sample.get('title','')} {sample.get('salary','')}")
        text = "\n".join(lines)
        return text[:max_len] + (f"\n...(截断)" if len(text) > max_len else "")

    # ── 兜底: JSON dump ──
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text[:max_len] + (f"...(截断, 原{len(text)}字符)" if len(text) > max_len else "")


# ═══════════════════════════════════════
#  工具函数 —— 每个工具一个 async def
# ═══════════════════════════════════════

# ── helpers ────────────────────────────

_CITY_MAP = {
    "济南": "101120100", "青岛": "101120200", "淄博": "101120300",
    "北京": "101010100", "上海": "101020100", "广州": "101280100",
    "深圳": "101280600", "成都": "101270100", "杭州": "101210100",
    "武汉": "101200100", "南京": "101190100", "重庆": "101040100",
    "西安": "101110100", "长沙": "101250100", "天津": "101030100",
    "苏州": "101190400", "郑州": "101180100", "东莞": "101281600",
    "合肥": "101220100", "福州": "101230100", "厦门": "101230200",
    "南昌": "101240100", "贵阳": "101260100", "南宁": "101300100",
    "太原": "101100100", "石家庄": "101090100", "哈尔滨": "101050100",
    "长春": "101060100", "沈阳": "101070100", "昆明": "101290100",
    "兰州": "101160100", "乌鲁木齐": "101130100", "呼和浩特": "101080100",
    "西宁": "101150100", "银川": "101170100", "海口": "101310100",
    "全国": "100010000",
}


def _city_code(city: str) -> str:
    return _CITY_MAP.get(city, "100010000")


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    return urljoin("https://www.zhipin.com", url) if url else ""


def _load_llm():
    sys.path.insert(0, str(Path(__file__).parent.parent / "interview"))
    from llm_client import llm_chat_deepseek
    return llm_chat_deepseek


def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    for prefix in ("```json", "```", "json"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):].strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    return json.loads(raw)


# ── 🔍 搜索发现 (3个) ──────────────────


# 【搜索BOSS直聘岗位】在zhipin.com执行关键词+城市搜索，返回岗位列表（含公司/薪资/城市/经验/学历/HR活跃度）
# 依赖: 浏览器 ✅  |  AI ❌
async def search_jobs(keyword: str, city: str = "全国", limit: int = 30) -> dict:
    ctx = ToolContext.get()
    if not ctx.has_browser():
        return {"error": "浏览器未启动，请先到设置页启动浏览器"}

    city_code = _city_code(city)
    try:

        def _do():
            return ctx.automation.search(keyword, city_code, None, "", None, None)

        jobs = await ctx.run_pw(_do)
    except Exception as e:
        return {"error": f"搜索失败: {e}"}

    if not jobs:
        return {"message": f"在{city}搜索'{keyword}'未找到结果", "jobs": []}

    result = []
    for j in jobs[:limit]:
        result.append({
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "salary": j.get("salary", ""),
            "city": j.get("city", ""),
            "experience": j.get("experience", ""),
            "education": j.get("education", ""),
            "hr_name": j.get("hr_name", ""),
            "hr_title": j.get("hr_title", ""),
            "hr_active": j.get("hr_active", ""),
            "url": _norm_url(j.get("url", "")),
        })

    # 写库
    try:
        from boss_state import add_application, get_application_by_url, update_application_from_job

        for j in jobs[:limit]:
            j["url"] = _norm_url(j.get("url", ""))
            if j.get("url"):
                existing = get_application_by_url(j["url"])
                if existing:
                    update_application_from_job(existing["id"], j)
                else:
                    add_application(j)
    except Exception:
        pass

    return {
        "message": f"搜索'{keyword}' ({city}) 找到 {len(jobs)} 个岗位，返回前 {len(result)} 个",
        "count": len(result),
        "jobs": result,
    }


# 【列本地岗位】读取SQLite中之前搜索过的岗位列表，可筛选状态(pending/applied/replied)，不发起网络请求
# 依赖: 浏览器 ❌  |  AI ❌  |  纯本地DB读取
async def list_jobs(status: str = "", limit: int = 50) -> dict:
    try:
        from boss_state import list_applications

        jobs = list_applications(status or None, limit)
        result = [
            {
                "id": j.get("id"),
                "title": j.get("job_title", ""),
                "company": j.get("company", ""),
                "salary": j.get("salary", ""),
                "city": j.get("city", ""),
                "status": j.get("status", ""),
                "url": j.get("job_url", ""),
                "hr_name": j.get("hr_name", ""),
            }
            for j in jobs
        ]
        return {"count": len(result), "jobs": result}
    except Exception as e:
        return {"error": f"获取岗位列表失败: {e}"}


# 【岗位详情】返回单个岗位的完整快照: JD描述全文/HR信息/公司/薪资/状态，用于决策前细看
# 依赖: 浏览器 ❌  |  AI ❌  |  纯本地DB读取
async def get_job_detail(job_url: str) -> dict:
    try:
        from boss_state import get_application_by_url

        job = get_application_by_url(_norm_url(job_url))
        if not job:
            return {"error": "岗位不存在，请先搜索"}
        return {
            "title": job.get("job_title", ""),
            "company": job.get("company", ""),
            "salary": job.get("salary", ""),
            "city": job.get("city", ""),
            "experience": job.get("experience", ""),
            "education": job.get("education", ""),
            "description": (job.get("description") or "")[:1000],
            "hr_name": job.get("hr_name", ""),
            "hr_title": job.get("hr_title", ""),
            "hr_active": job.get("hr_active", ""),
            "status": job.get("status", ""),
            "url": job.get("job_url", ""),
        }
    except Exception as e:
        return {"error": f"获取岗位详情失败: {e}"}


# ── 🧠 分析决策 (3个) ──────────────────


# 【JD匹配度分析】AI读取JD全文+求职者简历 → 输出匹配分数(0-100)/关键技能/差距/投递建议，投递前必看
# 依赖: 浏览器 ❌  |  AI ✅ (消耗token)
async def analyze_jd(job_url: str) -> dict:
    try:
        from boss_state import get_application_by_url, get_setting

        job = get_application_by_url(_norm_url(job_url))
        if not job:
            return {"error": "岗位不存在，请先搜索"}

        llm = _load_llm()
        resume = get_setting("resume_summary", "")
        desc = (job.get("description") or "")[:2000]
        title = job.get("job_title", "")
        company = job.get("company", "")

        prompt = f"""你是求职辅导专家。分析岗位JD并输出JSON。

## 求职者简历
{resume if resume else "（未提供）"}

## 岗位: {title} @ {company}
{desc}

## 输出严格JSON
{{
  "match_score": 75,
  "decision": "建议投递",
  "key_skills": ["技能1", "技能2"],
  "match_points": ["匹配点1"],
  "gaps": ["差距1"],
  "summary": "一句话总结"
}}"""

        raw = llm([{"role": "user", "content": prompt}], temperature=0.3)
        return _parse_json_response(raw)
    except json.JSONDecodeError:
        return {"error": "AI 返回格式异常，请重试", "match_score": 0}
    except Exception as e:
        return {"error": f"分析失败: {e}", "match_score": 0, "summary": "请检查AI配置"}


# 【简历优化】AI根据JD给出简历修改建议: 一句话核心建议/关键词补充/各模块优化方向，同JD 24h内缓存复用
# 依赖: 浏览器 ❌  |  AI ✅ |  24h缓存
# match_context: 可选，传入 analyze_jd 的结果（gaps/key_skills/match_points），跳过 JD 重分析
async def optimize_resume_for_jd(job_url: str, match_context: dict = None) -> dict:
    try:
        from boss_state import get_application_by_url, get_setting, get_db

        job = get_application_by_url(_norm_url(job_url))
        if not job:
            return {"error": "岗位不存在"}

        # 24h 缓存
        db = get_db()
        row = db.execute(
            "SELECT optimize_result, optimize_at FROM applications WHERE job_url=?",
            (_norm_url(job_url),),
        ).fetchone()
        if row and row["optimize_result"] and row["optimize_at"]:
            try:
                t = datetime.datetime.fromisoformat(row["optimize_at"])
                if (datetime.datetime.now() - t).total_seconds() < 86400:
                    return {**json.loads(row["optimize_result"]), "_cached": True}
            except Exception:
                pass

        llm = _load_llm()
        resume = get_setting("resume_summary", "")
        title = job.get("job_title", "")
        company = job.get("company", "")

        if match_context:
            # 已有分析结果，跳过 JD 重读，直接基于已知 gaps 生成优化建议
            gaps_text = "\n".join(f"- {g}" for g in match_context.get("gaps", []))
            skills_text = ", ".join(match_context.get("key_skills", []))
            prompt = f"""你是简历优化专家。已知岗位分析结果，生成简历优化建议，输出JSON。

岗位: {title} @ {company}
JD要求的关键技能: {skills_text}
候选人与JD的差距:
{gaps_text or '无明显差距'}
简历: {resume[:2000] if resume else "未提供"}

输出:
{{
  "one_line": "一句话核心建议",
  "match_gaps": ["差距1"],
  "optimize_tips": [{{"area": "模块", "suggestion": "建议", "why": "原因"}}],
  "keywords_to_add": ["关键词"],
  "action_items": ["优先级1"]
}}"""
        else:
            desc = (job.get("description") or "")[:3000]
            prompt = f"""你是简历优化专家。根据JD给出优化建议，输出JSON。

岗位: {title} @ {company}
JD: {desc}
简历: {resume[:2000] if resume else "未提供"}

输出:
{{
  "one_line": "一句话核心建议",
  "match_gaps": ["差距1"],
  "optimize_tips": [{{"area": "模块", "suggestion": "建议", "why": "原因"}}],
  "keywords_to_add": ["关键词"],
  "action_items": ["优先级1"]
}}"""

        raw = llm([{"role": "user", "content": prompt}], temperature=0.4)
        result = _parse_json_response(raw)

        db.execute(
            "UPDATE applications SET optimize_result=?, optimize_at=CURRENT_TIMESTAMP WHERE job_url=?",
            (json.dumps(result, ensure_ascii=False), _norm_url(job_url)),
        )
        db.commit()
        return result
    except json.JSONDecodeError:
        return {"error": "AI 返回格式异常"}
    except Exception as e:
        return {"error": f"简历优化失败: {e}"}


# 【沟通策略】AI生成与HR聊天的全套话术: 破冰第一句/可聊话题+示例/避雷点/对方不回复时的跟进话术
# 依赖: 浏览器 ❌  |  AI ✅ |  24h缓存
# match_context: 可选，传入 analyze_jd 的结果（gaps/summary/match_points），跳过 JD 重分析
async def get_chat_suggestion(job_url: str, match_context: dict = None) -> dict:
    try:
        from boss_state import get_application_by_url, get_setting

        job = get_application_by_url(_norm_url(job_url))
        if not job:
            return {"error": "岗位不存在"}

        llm = _load_llm()
        title = job.get("job_title", "")
        company = job.get("company", "")
        hr_name = job.get("hr_name", "")
        is_boss = bool(job.get("is_boss"))
        resume = get_setting("resume_summary", "")

        boss_hint = "对方可能是老板本人，语气要更直接" if is_boss else ""

        if match_context:
            # 已有分析结果，跳过 JD 重读
            strengths = "\n".join(f"- {m}" for m in match_context.get("match_points", []))
            gaps_text = ", ".join(match_context.get("gaps", []))
            summary = match_context.get("summary", "")
            prompt = f"""你是求职沟通教练。已知岗位匹配分析，生成沟通策略，输出JSON。

公司: {company} | 岗位: {title}
HR: {hr_name} {"(老板/法人)" if is_boss else ""}
{boss_hint}
候选人匹配优势:
{strengths or '无特别优势'}
待补充的方向: {gaps_text or '无明显差距'}
分析总结: {summary}
简历: {resume[:1000] if resume else "未提供"}

输出:
{{
  "icebreaker": "第一句话（10-25字）",
  "chat_topics": [{{"topic": "方向", "angle": "角度", "example": "话术示例"}}],
  "avoid": ["踩雷点"],
  "follow_up": "对方不回复时的跟进话术",
  "tone_tip": "沟通风格建议"
}}"""
        else:
            desc = (job.get("description") or "")[:2000]
            prompt = f"""你是求职沟通教练。生成沟通策略，输出JSON。

公司: {company} | 岗位: {title}
HR: {hr_name} {"(老板/法人)" if is_boss else ""}
{boss_hint}
JD: {desc}
简历: {resume[:1000] if resume else "未提供"}

输出:
{{
  "icebreaker": "第一句话（10-25字）",
  "chat_topics": [{{"topic": "方向", "angle": "角度", "example": "话术示例"}}],
  "avoid": ["踩雷点"],
  "follow_up": "对方不回复时的跟进话术",
  "tone_tip": "沟通风格建议"
}}"""

        raw = llm([{"role": "user", "content": prompt}], temperature=0.6)
        return _parse_json_response(raw)
    except json.JSONDecodeError:
        return {"error": "AI 返回格式异常"}
    except Exception as e:
        return {"error": f"沟通建议生成失败: {e}"}


# ── 🚀 投递执行 (2个) ──────────────────


# 【单岗投递】在BOSS直聘上投递一个岗位，自动生成AI招呼语 + 检查日上限 + 公司去重，投递后记录到DB
# 依赖: 浏览器 ✅  |  AI ✅ (生成招呼语)  |  ⚠️ 消耗每日配额
async def apply_job(job_url: str) -> dict:
    ctx = ToolContext.get()
    if not ctx.has_browser():
        return {"error": "浏览器未启动"}

    # 日限检查
    try:
        from boss_state import get_today_application_count, get_setting

        daily_limit = int(get_setting("daily_apply_limit", "15"))
        if get_today_application_count() >= daily_limit:
            return {"error": f"今日投递已达上限 ({daily_limit}条)"}
    except Exception:
        pass

    # 生成招呼语
    greeting = "您好，我对这个岗位很感兴趣，可以聊聊吗？"
    try:
        from boss_replier import generate_greeting_ai
        from boss_state import get_application_by_url, get_setting

        job = get_application_by_url(_norm_url(job_url)) or {}
        greeting = generate_greeting_ai(
            job.get("job_title", ""),
            job.get("company", ""),
            job.get("hr_name", ""),
            job.get("description", ""),
            bool(job.get("is_boss")),
            get_setting("ai_reply_style", "professional"),
            get_setting("resume_summary", ""),
            timeout=15.0,
        )
    except Exception:
        pass

    try:

        def _do():
            return ctx.automation.apply_to_job(_norm_url(job_url), greeting)

        result = await ctx.run_pw(_do)
        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
            "greeting_used": greeting[:80],
        }
    except Exception as e:
        return {"error": f"投递失败: {e}"}


# 【批量投递】一次投递多个岗位，自动控制数量不超日限 + 带随机间隔防风控，返回成功/失败计数
# 依赖: 浏览器 ✅  |  AI ❌ (用默认招呼语)  |  ⚠️ 消耗每日配额
async def batch_apply(job_urls: str) -> dict:
    ctx = ToolContext.get()
    if not ctx.has_browser():
        return {"error": "浏览器未启动"}

    try:
        urls = json.loads(job_urls) if isinstance(job_urls, str) else job_urls
    except Exception:
        return {"error": "job_urls 需要是 JSON 数组字符串，如 '[\"url1\", \"url2\"]'"}

    if not urls:
        return {"error": "job_urls 为空"}

    try:
        from boss_state import get_today_application_count, get_setting

        daily_limit = int(get_setting("daily_apply_limit", "15"))
        remaining = daily_limit - get_today_application_count()
        urls = urls[:max(1, remaining)]
    except Exception:
        pass

    try:

        def _do():
            return ctx.automation.apply_batch([_norm_url(u) for u in urls])

        results = await ctx.run_pw(_do)
        success = sum(1 for r in results if r.get("success"))
        return {
            "total": len(urls),
            "success": success,
            "failed": len(results) - success,
            "detail": [r.get("message", "") for r in results if not r.get("success")][:5],
        }
    except Exception as e:
        return {"error": f"批量投递失败: {e}"}


# ── 💬 沟通管理 (3个) ──────────────────


# 【会话列表】列出所有活跃HR会话: HR姓名/公司/最后一条消息/状态/是否交换微信/兴趣度，用于了解当前沟通全貌
# 依赖: 浏览器 ❌  |  AI ❌  |  纯本地DB读取
async def list_conversations() -> dict:
    try:
        from boss_state import list_active_conversations

        convs = list_active_conversations()
        result = []
        for c in convs:
            result.append({
                "id": c.get("id"),
                "hr_name": c.get("hr_name", ""),
                "company": c.get("hr_company", ""),
                "job_title": c.get("job_title", ""),
                "last_message": (c.get("last_message_text") or "")[:100],
                "last_sender": c.get("last_message_sender", ""),
                "status": c.get("status", ""),
                "interest": c.get("interest_level", ""),
                "wechat": c.get("hr_wechat", ""),
            })
        return {"count": len(result), "conversations": result}
    except Exception as e:
        return {"error": f"获取会话列表失败: {e}"}


# 【聊天记录】获取与指定HR的最近50条消息，区分我方/对方/AI生成，用于对话上下文回顾
# 依赖: 浏览器 ❌  |  AI ❌  |  纯本地DB读取
async def get_chat_messages(conv_id: int) -> dict:
    try:
        from boss_state import get_conversation, get_messages

        conv = get_conversation(conv_id)
        if not conv:
            return {"error": f"会话 {conv_id} 不存在"}

        msgs = get_messages(conv_id, 50)
        result = [
            {
                "sender": "我" if m.get("sender") == "me" else "HR",
                "content": (m.get("content") or "")[:300],
                "ai_generated": bool(m.get("ai_generated")),
            }
            for m in msgs
        ]
        return {
            "hr_name": conv.get("hr_name", ""),
            "company": conv.get("hr_company", ""),
            "count": len(result),
            "messages": result,
        }
    except Exception as e:
        return {"error": f"获取聊天记录失败: {e}"}


# 【AI生成回复】根据HR最新消息+岗位上下文+AI人格风格，生成回复话术/兴趣度判断/是否交换微信，只生成不发送
# 依赖: 浏览器 ❌  |  AI ✅ |  需人工确认后通过其他途径发送
async def generate_reply(conv_id: int) -> dict:
    try:
        from boss_state import get_conversation, get_recent_messages, get_setting, get_application
        from boss_replier import generate_reply as _gen_reply

        conv = get_conversation(conv_id)
        if not conv:
            return {"error": f"会话 {conv_id} 不存在"}

        msgs = get_recent_messages(conv_id, 3)
        hr_msg = ""
        for m in reversed(msgs):
            if m.get("sender") == "hr":
                hr_msg = m.get("content", "")
                break

        if not hr_msg:
            return {"error": "没有找到未回复的HR消息"}

        app_id = conv.get("application_id")
        job_info = {"title": conv.get("job_title", ""), "company": conv.get("hr_company", ""), "description": ""}
        if app_id:
            app = get_application(app_id)
            if app:
                job_info["description"] = app.get("description", "")[:500]

        result = _gen_reply(
            conv_id,
            hr_msg,
            job_info,
            get_setting("ai_reply_style", "professional"),
            get_setting("resume_summary", ""),
            get_setting("wechat_id", ""),
        )
        return {
            "hr_message": hr_msg[:200],
            "reply": result.get("reply", ""),
            "interest": result.get("interest", ""),
            "transfer": result.get("transfer", False),
        }
    except Exception as e:
        return {"error": f"生成回复失败: {e}"}


# ── 📊 统计状态 (2个) ──────────────────


# 【系统状态】返回浏览器是否在线/今日已投递数/日上限/自动回复开关/默认搜索城市，每次开始任务前建议先检查
# 依赖: 浏览器 ❌ (仅检查状态)  |  AI ❌  |  读DB + 检查automation实例
async def get_status() -> dict:
    ctx = ToolContext.get()
    try:
        from boss_state import get_setting, get_today_application_count

        return {
            "browser_running": ctx.has_browser(),
            "auto_reply_enabled": get_setting("auto_reply_enabled", "false") == "true",
            "today_applied": get_today_application_count(),
            "daily_limit": get_setting("daily_apply_limit", "15"),
            "default_city": get_setting("default_city", "全国"),
        }
    except Exception as e:
        return {"error": f"获取状态失败: {e}"}


# 【转化漏斗】投递全流程数据: 今日投递→HR回复(24h内)→高兴趣→活跃会话，用于评估求职效果
# 依赖: 浏览器 ❌  |  AI ❌  |  纯本地DB统计
async def get_stats() -> dict:
    try:
        from boss_state import (
            get_today_application_count,
            get_today_pending_count,
            count_hours_replied_in_range,
            count_interest_level,
            list_active_conversations,
            get_daily_stats,
        )

        return {
            "today_applied": get_today_application_count(),
            "pending": get_today_pending_count(),
            "hr_replied_24h": count_hours_replied_in_range(24),
            "high_interest": count_interest_level("high"),
            "active_conversations": len(list_active_conversations()),
            "daily_stats": get_daily_stats(),
        }
    except Exception as e:
        return {"error": f"获取统计失败: {e}"}


# ── 🏢 公司画像 (1个) ──────────────────


# 【公司画像】搜索关键词 → 按公司聚合排名，展示每家公司岗位数/HR列表(按职级排序)/推荐投递HR，先概览再决策
# 依赖: 浏览器 ✅  |  AI ❌  |  搜索结果聚合分析
async def preview_companies(keyword: str, city: str = "全国") -> dict:
    ctx = ToolContext.get()
    if not ctx.has_browser():
        return {"error": "浏览器未启动"}

    city_code = _city_code(city)
    try:

        def _do():
            return ctx.automation.search(keyword, city_code, None, "", None, None)

        jobs = await ctx.run_pw(_do)
    except Exception as e:
        return {"error": f"搜索失败: {e}"}

    if not jobs:
        return {"message": f"搜索'{keyword}'无结果", "companies": []}

    from boss_automation import pick_top_hr, _hr_title_score

    groups: dict = defaultdict(list)
    for j in jobs:
        comp = (j.get("company") or "").strip()
        if comp and len(comp) >= 2:
            groups[comp].append(j)

    companies = []
    for comp, comp_jobs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        hrs: dict = {}
        for cj in comp_jobs:
            hn = (cj.get("hr_name") or "").strip()
            ht = (cj.get("hr_title") or "").strip()
            if hn and hn not in hrs:
                hrs[hn] = {"name": hn, "title": ht, "priority": _hr_title_score(ht)}
        hrs_list = sorted(hrs.values(), key=lambda h: -h["priority"])
        top = pick_top_hr(hrs_list)

        companies.append({
            "company": comp,
            "position_count": len(comp_jobs),
            "top_hr": top,
            "hr_count": len(hrs_list),
            "sample_job": {
                "title": comp_jobs[0].get("title", ""),
                "salary": comp_jobs[0].get("salary", ""),
                "url": _norm_url(comp_jobs[0].get("url", "")),
            },
        })

    companies.sort(key=lambda c: -c["position_count"])
    return {
        "keyword": keyword,
        "city": city,
        "total_jobs": len(jobs),
        "total_companies": len(companies),
        "companies": companies[:15],
    }


# ═══════════════════════════════════════
#  注册入口 —— 一次性注册全部工具
# ═══════════════════════════════════════

def register_all(registry: ToolRegistry) -> ToolRegistry:
    """向 registry 注册全部 14 个工具。返回 registry 支持链式调用。"""

    # ── 🔍 搜索发现（3个）──
    # 在zhipin.com搜索 → 写入DB → 返回列表，Agent首次接触岗位的主要入口
    registry.register("search_jobs", "在BOSS直聘搜索岗位。返回岗位列表，含公司、薪资、城市、经验要求、HR活跃度等。这是获取新岗位的主要方式。", {
        "keyword": {"type": "string", "description": "搜索关键词，如'Python后端'、'前端实习'"},
        "city": {"type": "string", "description": "城市名，如'广州'、'北京'、'全国'。不传则用默认城市"},
        "limit": {"type": "integer", "description": "返回数量上限，默认30，最大60"},
    }, search_jobs)

    # 读SQLite中历史搜过的岗位，可筛选状态，纯本地操作无需浏览器
    registry.register("list_jobs", "列出本地数据库中已有的岗位（之前搜索过的）。不会发起新的搜索。", {
        "status": {"type": "string", "description": "筛选状态: pending(待投递) / applied(已投递) / replied(HR已回复)。不传返回全部"},
        "limit": {"type": "integer", "description": "返回数量，默认50"},
    }, list_jobs)

    # 返回单个岗位完整快照: JD全文/HR/公司/薪资，决策前细看用
    registry.register("get_job_detail", "获取某个岗位的详细信息，包含完整JD描述、HR信息、公司信息等。", {
        "job_url": {"type": "string", "description": "岗位URL，如 /job_detail/xxx.html"},
    }, get_job_detail)

    # ── 🧠 AI分析决策（3个）──
    # AI阅读JD vs 简历 → 匹配分数+关键技能+差距+建议，投递前必用
    registry.register("analyze_jd", "AI分析岗位JD与求职者简历的匹配度。返回匹配分数、关键技能、差距分析、建议。投递前建议先分析。", {
        "job_url": {"type": "string", "description": "岗位URL"},
    }, analyze_jd)

    # AI生成简历修改清单：核心建议/关键词/各模块优化方向，同JD 24h内缓存
    registry.register("optimize_resume_for_jd", "根据岗位JD生成简历优化建议，告诉求职者简历中哪些地方需要针对这个岗位修改。", {
        "job_url": {"type": "string", "description": "岗位URL"},
    }, optimize_resume_for_jd)

    # AI生成与HR聊天的完整话术：破冰/话题/避雷/跟进，投递后制定沟通策略用
    registry.register("get_chat_suggestion", "生成与HR沟通的建议：第一句怎么说、聊哪些话题、避免说什么。投递后可用来准备沟通策略。", {
        "job_url": {"type": "string", "description": "岗位URL"},
    }, get_chat_suggestion)

    # ── 🚀 投递执行（2个）──
    # 在BOSS直聘点击投递按钮：自动生成AI招呼语 + 检查日限 + 公司去重 + 写DB记录
    registry.register("apply_job", "投递单个岗位（含AI生成的个性化招呼语）。投递前会自动检查日上限和重复投递。", {
        "job_url": {"type": "string", "description": "要投递的岗位URL"},
    }, apply_job)

    # 一次投多个岗位，带随机间隔(2-5s)防触发风控，自动限制不超日上限
    registry.register("batch_apply", "批量投递多个岗位，带随机间隔防风控。适合在筛选完成后一次性投递。", {
        "job_urls": {"type": "string", "description": "岗位URL列表，JSON数组字符串，如 [\"url1\", \"url2\"]"},
    }, batch_apply)

    # ── 💬 沟通管理（3个）──
    # 列出所有活跃的HR对话：谁/哪家公司/最后消息/是否已交换微信/兴趣度
    registry.register("list_conversations", "列出所有活跃的HR会话，含HR姓名、公司、最后消息、状态。用于了解当前沟通进度。", {}, list_conversations)

    # 获取与某HR的最近50条消息，区分我方/HR/AI生成，回顾对话上下文用
    registry.register("get_chat_messages", "获取与某个HR的聊天记录。", {
        "conv_id": {"type": "integer", "description": "会话ID，从 list_conversations 获取"},
    }, get_chat_messages)

    # AI根据HR最新消息+岗位上下文生成回复，只生成文本不发送，需人工确认
    registry.register("generate_reply", "根据HR的消息和上下文，AI生成回复话术。只生成文本，不实际发送。", {
        "conv_id": {"type": "integer", "description": "会话ID"},
    }, generate_reply)

    # ── 📊 统计状态（2个）──
    # 浏览器在线? 今日已投/N 自动回复开关 默认城市，开始任务前先检查
    registry.register("get_status", "获取系统状态：浏览器是否运行、今日投递数、活跃会话数。每次开始任务前建议先检查。", {}, get_status)

    # 投递→HR回复→高兴趣→面试邀请 全漏斗数据，评估求职效果用
    registry.register("get_stats", "获取投递转化漏斗统计：今日投递数、HR回复数、面试邀请数、活跃会话数。", {}, get_stats)

    # ── 🏢 公司画像（1个）──
    # 搜索→按公司聚合排名→展示岗位数+HR职级排序+推荐投递对象，先看全局再单点
    registry.register("preview_companies", "搜索岗位并按公司分组聚合，返回每家公司的岗位数、HR列表（按职级排序）、推荐投递对象。适合先概览再决策。", {
        "keyword": {"type": "string", "description": "搜索关键词"},
        "city": {"type": "string", "description": "城市，默认全国"},
    }, preview_companies)

    # ── ⚡ 复合技能（3个）──
    from .skills import smart_scan, prepare_application, smart_apply

    registry.register("smart_scan",
        "【推荐优先使用】智能搜索+批量分析。搜索岗位并自动对TOP结果做AI匹配度分析，返回按分数排名的列表。一步替代 search_jobs+get_job_detail×N+analyze_jd×N 的多轮操作。", {
            "keyword": {"type": "string", "description": "搜索关键词，如'Python后端'、'前端开发'"},
            "city": {"type": "string", "description": "城市名，如'广州'。默认全国"},
            "top_n": {"type": "integer", "description": "分析TOP多少个岗位，默认5，最大10"},
        }, smart_scan)

    registry.register("prepare_application",
        "对一个岗位完成投递前的全套分析：JD详情+AI匹配度+简历优化建议+HR沟通策略。一步替代 get_job_detail+analyze_jd+optimize_resume+get_chat_suggestion 四轮操作。投递前建议先用这个了解匹配情况。", {
            "job_url": {"type": "string", "description": "岗位URL"},
        }, prepare_application)

    registry.register("smart_apply",
        "智能分析并投递。内部自动执行：详情获取→AI匹配度→简历优化→沟通策略→（达标则）投递。匹配度低于阈值会跳过并返回原因。一步替代 prepare_application+apply_job 的完整决策到执行流程。", {
            "job_url": {"type": "string", "description": "要投递的岗位URL"},
            "min_score": {"type": "integer", "description": "最低匹配分数阈值(0-100)，低于此分不投递。默认60"},
        }, smart_apply)

    return registry
