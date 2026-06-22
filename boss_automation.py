#!/usr/bin/env python3
"""
BossAutomation — 继承 BossScraper，增加点击/输入/聊天等交互能力。
"""

import json
import random
import re
import time
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin

from playwright.sync_api import Locator

from boss_firefox import BossScraper, pause, decode_salary
from boss_state import (
    init_db,
    add_application,
    get_application_by_url,
    update_application_status,
    get_setting,
    get_today_application_count,
    get_or_create_conversation,
    get_conversation,
    add_message,
    get_messages,
    get_recent_messages,
    replace_conversation_messages,
    message_exists,
    update_conversation_last_message,
    update_conversation_status,
    update_conversation_interest,
    update_conversation_wechat,
    update_conversation_transfer_requested,
    increment_daily_stat,
    get_today_auto_reply_count,
    find_conversation_by_hr_name,
    get_daily_stats,
)

# ── 选择器配置（BOSS UI 改版时只改这里，也可通过设置表覆盖）──
SELECTORS = {
    "apply_button": [
        'button:has-text("立即沟通")',
        'a:has-text("立即沟通")',
        '[class*="btn-chat"]',
        '[class*="start-chat"]',
        'span:has-text("立即沟通")',
        'div:has-text("立即沟通")',
    ],
    "chat_input": [
        "#chat-input",
        'div[contenteditable="true"]',
        '[class*="chat-input"]',
        '[placeholder*="请输入"]',
    ],
    "chat_send_button": [
        'button[type="send"]',
        ".btn-send",
        'button:has-text("发送")',
        'button[class*="send"]',
    ],
    "conversation_items": [
        'li[role="listitem"]',
        ".friend-content",
        '[class*="chat-item"]',
    ],
    "message_items_in_chat": [
        "li.message-item",
        'li[class*="message-item"]',
        '[class*="message-item"]',
    ],
    "unread_badge": [
        '[class*="unread"]',
        '[class*="badge"]',
        ".red-dot",
    ],
    "greeting_dialog_close": [
        'button[class*="close"]',
        '[class*="dialog-close"]',
        'span:has-text("×")',
        '[class*="modal-close"]',
        'svg[class*="close"]',
    ],
    "resume_attach_btn": [
        'div.toolbar-btn:has-text("发简历")',
        'div:has-text("发简历")',
        'button:has-text("发简历")',
        'span:has-text("发简历")',
    ],
    "resume_confirm_btn": [
        ".btn-sure-v2.btn-confirm",
        ".choose-resume-dialog .btn-confirm",
        'button:has-text("发送")',
        '.boss-popup__content button:has-text("发送")',
    ],
    "wechat_share_btn": [
        ".btn-weixin",
        'div:has-text("换微信")',
        'span:has-text("换微信")',
        '[class*="btn-weixin"]',
    ],
    "phone_share_btn": [
        ".btn-contact",
        'div:has-text("换电话")',
        'span:has-text("换电话")',
        '[class*="btn-contact"]',
    ],
    "back_to_list": [
        '[class*="back"]',
        'span:has-text("返回")',
        'button:has-text("返回")',
        'a[href*="/chat"]',
    ],
    "company_similar_jobs_link": [
        'a[href*="/gongsi/job/"]',
        'a:has-text("查看该公司其他职位")',
        'a:has-text("查看该公司")',
        'a:has-text("全部职位")',
        '.company-info a[href*="gongsi"]',
    ],
    "company_job_cards": [
        ".job-card",
        ".job-primary",
        "li.job-primary",
        '[class*="job-card"]',
        '[class*="job-primary"]',
    ],
}


def _merge_selectors():
    """合并 settings 表中的选择器覆盖。"""
    try:
        from boss_state import get_setting
        import json as _json

        raw = get_setting("selector_overrides", "")
        if raw:
            overrides = _json.loads(raw)
            for k, v in overrides.items():
                if k in SELECTORS and isinstance(v, list) and len(v) > 0:
                    SELECTORS[k] = v
    except Exception:
        pass


_merge_selectors()

# ── 绝对上限 ──
MAX_APPLY_PER_DAY = 30
MAX_AUTO_REPLY_PER_DAY = 200

# ── HR 头衔优先级（数字越大越"高"，用于 pick_top_hr）──
HR_TITLE_PRIORITY = [
    ("招聘经理", 100),
    ("HR经理", 95),
    ("HRBP", 90),
    ("HR负责人", 85),
    ("招聘主管", 80),
    ("招聘负责人", 75),
    ("HR主管", 70),
    ("招聘官", 60),
    ("HR", 50),
    ("人事", 40),
    ("招聘专员", 30),
    ("HR助理", 20),
    ("猎头", 10),
]


def _hr_title_score(title: str) -> int:
    """根据 HR 头衔文本返回优先级分；命中多条取最高。"""
    if not title:
        return 0
    t = title.strip()
    for kw, score in HR_TITLE_PRIORITY:
        if kw in t:
            return score
    return 0


# ── 法人 / 老板识别 ──
# 中国最常见姓氏：同姓巧合概率高，作为弱信号降权。
COMMON_SURNAMES = set(
    "王李张刘陈杨黄赵周吴徐孙朱马胡郭林何高梁郑罗宋谢唐韩曹许邓萧冯曾程蔡彭潘"
    "袁于董余苏叶吕魏蒋田杜丁沈姜范江傅钟卢汪戴崔任陆廖姚方金邱夏谭韦贾邹石熊"
    "孟秦阎薛侯雷白龙段郝孔邵史毛常万顾赖武康贺严尹钱施牛洪龚汤陶黎温莫易樊乔"
)

# 复姓：同姓基本可锁定，作为强信号。
COMPOUND_SURNAMES = [
    "欧阳",
    "太史",
    "端木",
    "上官",
    "司马",
    "东方",
    "独孤",
    "南宫",
    "万俟",
    "闻人",
    "夏侯",
    "诸葛",
    "尉迟",
    "公羊",
    "赫连",
    "澹台",
    "皇甫",
    "宗政",
    "濮阳",
    "公冶",
    "太叔",
    "申屠",
    "公孙",
    "慕容",
    "仲孙",
    "钟离",
    "长孙",
    "宇文",
    "司徒",
    "鲜于",
    "司空",
    "闾丘",
    "子车",
    "亓官",
    "司寇",
    "巫马",
    "公西",
    "颛孙",
    "乐正",
    "宰父",
    "谷梁",
    "拓跋",
    "夹谷",
    "轩辕",
    "令狐",
    "段干",
    "百里",
    "呼延",
    "东郭",
    "南门",
    "羊舌",
    "微生",
    "梁丘",
    "左丘",
    "西门",
    "东里",
    "仲长",
    "即墨",
    "达奚",
]


_LEGAL_REP_NOISE = ("变更", "信息", "登记", "为", "是", "持股", "对外", "及")


def _extract_legal_rep(body_text: str, lines: Optional[List[str]] = None) -> str:
    """从公司页纯文本里抽取「法定代表人」姓名。

    常见格式：
        法定代表人：张三 / 法定代表人  张三 / 法定代表人\\n张三
    抽不到返回空字符串。
    """
    if not body_text:
        return ""
    # 同行 / 紧邻：法定代表人[: ：空白换行]姓名
    m = re.search(r"法定代表人[\s：:]*([\u4e00-\u9fa5·•・]{2,8})", body_text)
    if m:
        name = m.group(1).strip("·•・ ")
        if name and not any(n in name for n in _LEGAL_REP_NOISE):
            return name
    # 兜底：按行找「法定代表人」标签，取下一行非空文本
    rows = lines or []
    for i, ln in enumerate(rows):
        if "法定代表人" in ln and i + 1 < len(rows):
            cand = rows[i + 1].strip("·•・ ")
            if re.fullmatch(r"[\u4e00-\u9fa5·•・]{2,8}", cand) and not any(n in cand for n in _LEGAL_REP_NOISE):
                return cand
    return ""


def _extract_surname(name: str) -> str:
    """提取姓氏：先匹配复姓，否则取首字。空字符串返回空。"""
    name = (name or "").strip()
    if not name:
        return ""
    for cs in COMPOUND_SURNAMES:
        if name.startswith(cs):
            return cs
    return name[0]


def detect_boss(hr_name: str, legal_rep: str) -> Dict[str, Any]:
    """根据招聘者姓名与公司法人姓名，判断招聘者是否疑似老板/法人本人。

    返回 {is_boss, confidence, reason, score_bonus}：
      - 全名完全一致      → high   （几乎肯定本人）
      - 同姓 + 复姓/罕见姓 → medium （巧合概率低）
      - 同姓 + 常见姓      → low    （仅供参考）
      - 缺数据 / 不同姓    → none
    score_bonus 用于在 pick_top_hr 里加权（数值越大越优先）。
    """
    hr_name = (hr_name or "").strip()
    legal_rep = (legal_rep or "").strip()
    none = {"is_boss": False, "confidence": "none", "reason": "", "score_bonus": 0}
    if not hr_name or not legal_rep:
        return none

    if hr_name == legal_rep:
        return {
            "is_boss": True,
            "confidence": "high",
            "reason": f"招聘者姓名与法人「{legal_rep}」完全一致",
            "score_bonus": 1000,
        }

    hr_surname = _extract_surname(hr_name)
    rep_surname = _extract_surname(legal_rep)
    if not hr_surname or hr_surname != rep_surname:
        return none

    if hr_surname in COMPOUND_SURNAMES or hr_surname not in COMMON_SURNAMES:
        return {
            "is_boss": True,
            "confidence": "medium",
            "reason": f"与法人同为「{hr_surname}」姓（复姓/罕见姓，巧合概率低）",
            "score_bonus": 300,
        }

    return {
        "is_boss": True,
        "confidence": "low",
        "reason": f"与法人同为「{hr_surname}」姓（常见姓，仅供参考）",
        "score_bonus": 50,
    }


def pick_top_hr(hrs: List[Dict[str, Any]], legal_rep: str = "") -> Optional[Dict[str, Any]]:
    """从 HR 列表中挑出职务最高的一个。
    同分时按输入顺序（保持稳定）。输入为空返回 None。
    每条 hr 至少包含 name / title 字段。

    传入 legal_rep（公司法人姓名）时，疑似老板的 HR 会被加权优先选出，
    并在返回的 hr dict 上附加 is_boss / boss_confidence / boss_reason 字段。
    """
    if not hrs:
        return None
    best = None
    best_score = -1
    for hr in hrs:
        title_score = _hr_title_score(hr.get("title") or "")
        boss = detect_boss(hr.get("name") or "", legal_rep)
        if legal_rep:
            hr["is_boss"] = boss["is_boss"]
            hr["boss_confidence"] = boss["confidence"]
            hr["boss_reason"] = boss["reason"]
        total = title_score + boss["score_bonus"]
        if total > best_score:
            best_score = total
            best = hr
    return best


class BossAutomation(BossScraper):
    """在 BossScraper 基础上增加交互能力"""

    def __init__(self, headless=False):
        super().__init__(headless)
        init_db()
        # 风控冷却状态：检测到频率限制时设置 _cooldown_until，期间暂停高风险操作
        self._cooldown_until = 0.0
        self._risk_strikes = 0  # 连续命中风控信号次数，越高退避越久
        self._last_action_ts = 0.0  # 上一次高风险动作（投递/发消息）时间戳

    # ══════════════════════════════════════
    #  底层交互 helpers
    # ══════════════════════════════════════

    def _find_element(self, selector_list: List[str], timeout_ms: int = 5000) -> Optional[Locator]:
        """逐个尝试选择器，返回第一个可见匹配。"""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for sel in selector_list:
                try:
                    loc = self.page.locator(sel).first
                    if loc.is_visible():
                        return loc
                except Exception:
                    continue
            time.sleep(0.3)
        return None

    def _find_all_elements(self, selector_list: List[str]) -> List[Locator]:
        """返回所有匹配的可见元素。"""
        for sel in selector_list:
            try:
                locs = self.page.locator(sel)
                count = locs.count()
                if count > 0:
                    return [locs.nth(i) for i in range(count)]
            except Exception:
                continue
        return []

    def _human_type(self, locator: Locator, text: str):
        """逐字输入，模拟真人打字。"""
        try:
            locator.click()
            time.sleep(random.uniform(0.1, 0.3))
        except Exception:
            pass
        for ch in text:
            self.page.keyboard.type(ch, delay=random.randint(50, 150))
        time.sleep(random.uniform(0.3, 0.8))

    def _safe_click(self, locator: Locator):
        """带随机延迟的点击。"""
        time.sleep(random.uniform(0.2, 0.6))
        try:
            locator.hover()
            time.sleep(random.uniform(0.1, 0.3))
        except Exception:
            pass
        locator.click()

    def _has_text(self, *texts: str) -> bool:
        """检查页面是否包含任意关键词。"""
        try:
            body = self.page.inner_text("body").lower()
            return any(t.lower() in body for t in texts)
        except Exception:
            return False

    # ══════════════════════════════════════
    #  安全检查
    # ══════════════════════════════════════

    def inspect_page_safety(self) -> Dict[str, Any]:
        """检查页面安全状态，返回结构化结果。

        返回 {ok, reason, category, backoff}：
          - category: ok / login / captcha / banned / rate_limit / error
          - backoff: 建议退避秒数（rate_limit 时随 _risk_strikes 递增）
        同时维护 self._risk_strikes / self._cooldown_until。
        """
        try:
            body = self.page.inner_text("body")
            body_lower = body.lower()
        except Exception:
            return {"ok": True, "reason": "", "category": "error", "backoff": 0}

        if self._login_prompt_visible():
            return {"ok": False, "reason": "需要重新登录", "category": "login", "backoff": 0}

        head = body_lower[:600]
        if any(kw in head for kw in ["验证", "滑块", "拼图", "captcha", "verify"]):
            self._trigger_cooldown(category="captcha")
            return {"ok": False, "reason": "检测到验证码", "category": "captcha", "backoff": self._cooldown_remaining()}
        if any(kw in head for kw in ["账号异常", "违规", "限制使用", "冻结"]):
            self._trigger_cooldown(category="banned")
            return {"ok": False, "reason": "账号异常", "category": "banned", "backoff": self._cooldown_remaining()}
        if any(kw in head for kw in ["操作太频繁", "稍后再试", "休息一下", "访问过于频繁", "请求过于频繁"]):
            self._trigger_cooldown(category="rate_limit")
            return {
                "ok": False,
                "reason": "操作频率限制",
                "category": "rate_limit",
                "backoff": self._cooldown_remaining(),
            }

        return {"ok": True, "reason": "", "category": "ok", "backoff": 0}

    def check_page_safety(self) -> bool:
        """所有自动化操作前检查页面安全状态。布尔版（向后兼容）。"""
        status = self.inspect_page_safety()
        if not status["ok"]:
            print(f"  ⚠️ 安全检查: {status['reason']}")
        return status["ok"]

    # ── 风控冷却退避 ──
    def _trigger_cooldown(self, category: str = "rate_limit") -> None:
        """指数退避：连续命中风控时冷却时间递增，避免账号被限制。

        如果当前不在冷却期，说明是新一轮风控触发，重置计数器从基础档开始；
        如果在冷却期内再次命中，说明风控在升级，递增计数器延长冷却。
        """
        if not self.in_cooldown():
            self._risk_strikes = 0  # 新一轮触发，重置计数
        self._risk_strikes += 1
        base = {
            "rate_limit": 120,   # 2 分钟起
            "captcha": 600,      # 10 分钟起
            "banned": 3600,      # 1 小时起
        }.get(category, 120)
        seconds = min(base * (2 ** (self._risk_strikes - 1)), 7200)
        self._cooldown_until = time.time() + seconds
        print(f"  ⚠️ 触发风控冷却 category={category} strikes={self._risk_strikes} 冷却 {seconds:.0f}s")

    def _cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def in_cooldown(self) -> bool:
        return self._cooldown_remaining() > 0

    def _respect_cooldown(self) -> bool:
        if not self.in_cooldown():
            return True
        remaining = self._cooldown_remaining()
        print(f"  🧊 风控冷却中，剩余 {remaining:.0f}s，跳过本次操作")
        return False

    def _human_pace(self, min_gap: float = 8.0, max_gap: float = 20.0) -> None:
        """两次高风险动作之间保证最小人性化间隔，避免节奏过于机械。"""
        now = time.time()
        elapsed = now - self._last_action_ts
        target = random.uniform(min_gap, max_gap)
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last_action_ts = time.time()

    def _human_scroll(self) -> None:
        """模拟人类浏览：随机滚动页面，停留阅读。"""
        try:
            for _ in range(random.randint(1, 3)):
                delta = random.randint(200, 600)
                self.page.mouse.wheel(0, delta)
                time.sleep(random.uniform(0.8, 2.5))
        except Exception:
            pass

    # ══════════════════════════════════════
    #  Session 保活 & 心跳
    # ══════════════════════════════════════

    def check_logged_in(self) -> bool:
        """快速检查当前是否已登录；未知空白页不直接当作过期。"""
        try:
            return self.is_logged_in_page()
        except Exception:
            return False

    def heartbeat(self) -> bool:
        """心跳: 只检查当前页面登录状态，不主动跳转。"""
        try:
            return self.check_logged_in()
        except Exception:
            return False

    def keep_alive(self):
        """主动保活: 在聊天页保持 BOSS session 活跃。已登录时用轻量操作代替完整刷新。"""
        try:
            current_url = self.page.url
            need_navigate = "/web/geek/chat" not in current_url
            try:
                if need_navigate:
                    self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=30000)
                    pause(2, 4)
                else:
                    # 已在聊天页，轻量滚动模拟用户活动，避免频繁 reload 被检测
                    try:
                        self.page.mouse.move(random.randint(200, 600), random.randint(300, 500))
                        pause(0.5, 1.0)
                        self.page.evaluate("window.scrollBy(0, %d)" % random.randint(-100, 100))
                    except Exception:
                        pass
            except Exception:
                pass
            return self.check_logged_in()
        except Exception:
            return False

    def _save_state(self):
        """保存当前浏览器状态到文件。"""
        try:
            from boss_firefox import STATE_FILE

            state = self._ctx.storage_state()
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    # ══════════════════════════════════════
    #  自动投递
    # ══════════════════════════════════════

    def apply_to_job(self, job_url: str, greeting: Optional[str] = None) -> dict:
        """
        对单个岗位执行投递流程:
        1. 打开详情页
        2. 点击"立即沟通"
        3. 发送招呼语
        返回 {success, message, application_id}
        """
        if not job_url:
            return {"success": False, "message": "缺少岗位链接"}

        # 日限检查
        today_count = get_today_application_count()
        daily_limit = int(get_setting("daily_apply_limit", "15"))
        if today_count >= min(daily_limit, MAX_APPLY_PER_DAY):
            return {"success": False, "message": f"已达今日上限({today_count}条)"}

        # 风控冷却：处于退避期直接跳过，让上层稍后重试
        if not self._respect_cooldown():
            return {"success": False, "message": "风控冷却中", "cooldown": True}

        print(f"  🚀 投递: {job_url[:60]}...")

        try:
            # 人类化节奏：两次投递间保持随机间隔
            self._human_pace(min_gap=10.0, max_gap=25.0)

            self.page.goto(job_url, wait_until="load", timeout=45000)
            pause(2, 4)

            # 模拟人类浏览：滚动阅读JD，让BOSS觉得是真人在看
            self._human_scroll()
            # 滚回顶部，避免 Playwright auto-scroll 触发 BOSS header 重排动画导致 click 超时
            try:
                self.page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            except Exception:
                pass
            pause(1, 2)

            safety = self.inspect_page_safety()
            if not safety["ok"]:
                return {"success": False, "message": f"安全检查未通过: {safety['reason']}", "safety": safety}

            # 从详情页提取岗位名和JD（数据库可能没有JD）
            page_title = ""
            page_desc = ""
            try:
                page_title = self.page.evaluate("""() => {
                    const el = document.querySelector('.name h1, .job-name h1, .post-name h1, h1.name, .job-title, .title');
                    return el ? el.innerText.trim() : '';
                }""")
                page_desc = self.page.evaluate("""() => {
                    const el = document.querySelector('.job-detail, .job-sec-text, .detail-content, .job_desc, .job-detail-section');
                    return el ? el.innerText.trim() : '';
                }""")
            except Exception:
                pass

            # 检查是否已投递
            if self._has_text("已沟通", "继续沟通"):
                existing = get_application_by_url(job_url)
                if existing and existing["status"] == "pending":
                    update_application_status(existing["id"], "applied")
                print(f"  ✅ 已投递过，跳过")
                return {"success": True, "message": "已投递过", "already_applied": True}

            # ── 准备招呼语 ──
            greeting_text = greeting or get_setting(
                "greeting_template",
                "您好，我对贵公司的{job_title}岗位很感兴趣，请问可以详细了解一下吗？",
            )
            if page_title and "{job_title}" in greeting_text:
                greeting_text = greeting_text.replace("{job_title}", page_title)
            if page_title and "{company}" in greeting_text:
                company_name = get_application_by_url(job_url)
                company_name = (company_name or {}).get("company", "") if company_name else ""
                greeting_text = greeting_text.replace("{company}", company_name or "贵公司")

            greeting_mode = get_setting("greeting_mode", "template")
            if greeting_mode == "smart" and (not greeting or greeting_text == greeting):
                try:
                    from boss_replier import generate_greeting_ai
                    style = get_setting("ai_reply_style", "professional")
                    resume = get_setting("resume_summary", "")
                    is_boss = bool((get_application_by_url(job_url) or {}).get("is_boss"))
                    title = page_title or "相关岗位"
                    company = (get_application_by_url(job_url) or {}).get("company", "贵公司")
                    greeting_text = generate_greeting_ai(title, company, "", page_desc or "", is_boss, style, resume)
                    print(f"  📝 smart模式重新生成招呼语: {greeting_text[:60]!r}...")
                except Exception as e:
                    print(f"  ⚠️ 重新生成招呼语失败: {e}")

            if not greeting_text or len(greeting_text.strip()) < 2:
                print(f"  ⚠️ 招呼语为空，跳过投递")
                return {"success": False, "message": "招呼语为空"}

            print(f"  📝 招呼语({len(greeting_text)}字): {greeting_text[:80]}...")

            # ── 调用注入JS原生点击+打字（绕过CDP检测）──
            print(f"  🖱️ 调用 window.__bossApply() 原生投递...")
            js_result = None
            try:
                js_result = self.page.evaluate(
                    "async (greeting) => { return await window.__bossApply(greeting); }",
                    greeting_text,
                )
                print(f"  📡 JS返回: success={js_result.get('success')} message={js_result.get('message', '')[:60]}")
                if js_result.get("already_applied"):
                    existing = get_application_by_url(job_url)
                    if existing and existing["status"] == "pending":
                        update_application_status(existing["id"], "applied")
                    print(f"  ✅ 已投递过")
                    return {"success": True, "message": "已投递过", "already_applied": True}
                if js_result.get("cooldown"):
                    self._trigger_cooldown(category="rate_limit")
                    print(f"  ⚠️ 触发风控冷却")
                    return {"success": False, "message": js_result.get("message", "触发风控"), "cooldown": True}
            except Exception as e:
                print(f"  ❌ window.__bossApply() 异常: {e}")
                js_result = {"success": False, "message": str(e)}

            greeting_sent = js_result.get("success", False) if js_result else False

            # ── 兜底: JS失败且标记了 fallback_chat_page，走聊天页发 ──
            if not greeting_sent and js_result and js_result.get("fallback_chat_page"):
                print(f"  🔄 JS兜底: 导航到聊天页发招呼语...")
                greeting_sent = self._send_greeting_via_chat_page(greeting_text, job_url)
                if greeting_sent:
                    print(f"  ✅ 聊天页发送成功")
                else:
                    print(f"  ⚠️ 聊天页发送也失败")

            # 记录到 SQLite
            existing = get_application_by_url(job_url)
            if existing:
                if greeting_sent:
                    update_application_status(existing["id"], "applied", greeting_text)
                else:
                    update_application_status(existing["id"], "applied")
                app_id = existing["id"]
            else:
                app_id = add_application({"title": "", "company": "", "url": job_url})
                if greeting_sent:
                    update_application_status(app_id, "applied", greeting_text)
                else:
                    update_application_status(app_id, "applied")

            # 从详情页提取 HR 真实姓名和岗位信息
            hr_name = ""
            hr_company = ""
            job_title = ""
            try:
                from boss_firefox import BossScraper

                hr_info = self.page.evaluate("""() => {
                    const body = (document.body || {}).innerText || '';
                    const lines = body.split('\\n').map(l => l.trim()).filter(Boolean);
                    let hrName = '', hrTitle = '';
                    for (let i = 0; i < lines.length; i++) {
                        const l = lines[i];
                        if (l.includes('HR') || l.includes('招聘者') || l.includes('招聘经理') ||
                            l.includes('人事') || l.includes('HRBP') || l.includes('猎头')) {
                            if (i > 0 && lines[i-1].length <= 6 && !/\\d|省|市|区|路|号|招聘|公司|BOSS/.test(lines[i-1])) {
                                hrName = lines[i-1];
                            }
                            hrTitle = l;
                            break;
                        }
                    }
                    return {hrName, hrTitle};
                }""")
                hr_name = (hr_info.get("hrName") or "").strip()
                if not hr_name:
                    hr_name = ""
            except Exception:
                pass

            app_record = get_application_by_url(job_url) or {}
            hr_name = hr_name or app_record.get("hr_name", "")
            hr_company = app_record.get("company", "")
            job_title = app_record.get("job_title", "")

            # 只创建有 HR 名字的会话，避免"未知HR"垃圾数据
            if hr_name and len(hr_name) >= 2:
                get_or_create_conversation(app_id, hr_name, hr_company, job_title)

            increment_daily_stat("applications_sent")
            self._save_state()
            self._risk_strikes = 0  # 投递成功说明账号正常，清零风控计数
            print(f"  ✅ 投递成功")
            return {"success": True, "message": "投递成功", "application_id": app_id}

        except Exception as e:
            print(f"  ❌ 投递失败: {e}")
            return {"success": False, "message": str(e)}

    def apply_batch(self, job_urls: List[str], greeting_template: Optional[str] = None) -> List[dict]:
        """批量投递，带间隔延迟 + 风控退避。

        - 普通间隔由 batch_delay_min/max_sec 控制（随机）
        - 命中风控冷却时，等待冷却结束再继续，而不是直接放弃整批
        - 每投递若干条插入一次更长的"喝水休息"，模拟真人节奏
        """
        results = []
        min_delay = int(get_setting("batch_delay_min_sec", "30"))
        max_delay = int(get_setting("batch_delay_max_sec", "90"))
        # 每投 N 条后来一次长休息（5-10分钟），打散机械节奏
        rest_every = int(get_setting("batch_rest_every", "8"))
        applied_since_rest = 0

        for i, url in enumerate(job_urls):
            # 冷却期：等待结束（最多等一次冷却时长），避免硬撞风控
            if self.in_cooldown():
                print("  🧊 批量投递遇冷却，暂缓继续...")
                # 仍然实际等待冷却结束，但不打印剩余时间
                wait = self._cooldown_remaining()
                time.sleep(wait + random.uniform(2, 8))

            if i > 0:
                delay = random.uniform(min_delay, max_delay)
                print(f"  ⏳ 等待 {delay:.0f}s 后投递下一条...")
                time.sleep(delay)

            # 周期性长休息
            if rest_every > 0 and applied_since_rest >= rest_every:
                rest = random.uniform(300, 600)
                print(f"  ☕ 已连续投递 {applied_since_rest} 条，休息 {rest:.0f}s 降低风控风险...")
                time.sleep(rest)
                applied_since_rest = 0

            result = self.apply_to_job(url, greeting_template)
            results.append(result)

            if result.get("success"):
                applied_since_rest += 1

            # 命中"今日上限"：整批停止
            if not result["success"] and "上限" in result.get("message", ""):
                print("  ⛔ 触达今日投递上限，停止本批")
                break
            # 账号异常等强风控：立即停止，交由上层人工处理
            safety = result.get("safety") or {}
            if safety.get("category") in ("banned", "captcha"):
                print(f"  ⛔ 触发强风控({safety.get('category')})，停止本批等待人工处理")
                break
        self._save_state()
        return results

    # ══════════════════════════════════════
    #  聊天监控
    # ══════════════════════════════════════

    def navigate_to_chat(self) -> bool:
        """导航到 BOSS 聊天页，切到「未读」标签，只显示有未读消息的会话。"""
        try:
            self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=45000)
            pause(2, 3)
            # 点击「未读」标签，只显示有未读的会话
            for sel in ['span.label-name:has-text("未读")', 'li:has-text("未读")', '.label-name:has-text("未读")']:
                try:
                    unread_tab = self.page.locator(sel).first
                    if unread_tab.is_visible():
                        unread_tab.click()
                        pause(1, 2)
                        break
                except Exception:
                    pass
            return self.check_page_safety()
        except Exception:
            return False

    def poll_conversation_list(self) -> List[dict]:
        """从 BOSS 聊天页 DOM 获取会话列表。DOM 失败用 body text 正则兜底。"""
        conversations = []

        # 方式1: DOM 选择器
        conv_els = self._find_all_elements(SELECTORS["conversation_items"])
        if conv_els:
            for el in conv_els:
                try:
                    text = el.inner_text().strip()
                    if not text or len(text) < 3:
                        continue
                    # 从 BOSS 真实结构提取 HR 名字: .name-text
                    try:
                        hr_name = el.locator(".name-text").first.inner_text().strip()
                    except Exception:
                        hr_name = ""
                    if not hr_name:
                        # 兜底：从 body_text 行中提取
                        hr_name = (
                            el.evaluate("""(el) => {
                            const lines = (el.innerText||'').split('\\n').map(l=>l.trim()).filter(Boolean);
                            for (const l of lines) {
                                if (/^\\d{1,2}:\\d{2}$/.test(l)) continue;
                                if (/^\\[.+\\]$/.test(l)) continue;
                                const ch = l.replace(/[^\\u4e00-\\u9fff]/g,'');
                                if (ch.length>=2 && ch.length<=5) return l.split(/[\\s|·]/)[0].trim();
                            }
                            return '';
                        }""")
                            or ""
                        )
                    has_unread = False
                    try:
                        badge = el.locator('.red-dot, [class*="unread"]').first
                        has_unread = badge.is_visible()
                    except Exception:
                        pass
                    conversations.append(
                        {
                            "text": text,
                            "has_unread": has_unread,
                            "element": el,
                            "hr_name": hr_name,
                        }
                    )
                except Exception:
                    continue

        # 方式2: body text 正则兜底
        if not conversations:
            try:
                body = self.page.inner_text("body") or ""
                pattern = r"(\d{1,2}:\d{2})\s+([\u4e00-\u9fff\w·]+?)\s+(\[\s*\S+\s*\])\s+(.+?)(?=\s*\d{1,2}:\d{2}\s+|没有更多了|\Z)"
                for m in re.findall(pattern, body):
                    time_str, name_block, status, msg = m
                    # 提取纯名字：从 name_block 中去掉公司后缀
                    hr_name = re.sub(
                        r"[\u4e00-\u9fff]{2,}(?:有限|集团|科技|网络|信息|文化|教育|医疗|能源|贸易|实业|发展|控股|投资).*|经理.*|主管.*|专员.*|总监.*|[\[\]].*",
                        "",
                        name_block,
                    ).strip()
                    if not hr_name or len(hr_name) < 2:
                        m2 = re.match(r"^[\u4e00-\u9fff]{2,4}", name_block)
                        hr_name = m2.group(0) if m2 else name_block[:6]
                    hr_name = hr_name.strip()
                    if not hr_name or len(hr_name) < 2:
                        continue
                    conversations.append(
                        {
                            "text": f"{time_str}\n{name_block}\n{status}\n{msg}".strip(),
                            "has_unread": "未读" in status,
                            "element": None,
                            "hr_name": hr_name,
                        }
                    )
            except Exception:
                pass

        return conversations

    def read_visible_messages(self) -> List[dict]:
        """读取当前右侧聊天窗口中的可见消息，避免把左侧会话列表误当聊天内容。"""
        try:
            raw = self.page.evaluate("""() => {
                const result = [];
                const vw = window.innerWidth || 1200;
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const clean = text => (text || '')
                    .replace(/^(已读|未读|送达|发送失败|已发送)\\s*/g, '')
                    .replace(/\\n?(已读|未读|送达|发送失败|已发送)$/g, '')
                    .trim();
                const pickStatus = text => {
                    const m = (text || '').match(/(^|\\n)\\s*(已读|未读|送达|发送失败|已发送)\\s*(\\n|$)/);
                    return m ? m[2] : '';
                };
                const push = (el, contentEl) => {
                    if (!visible(el)) return;
                    const r = el.getBoundingClientRect();
                    if (r.left + r.width / 2 < vw * 0.35) return;
                    const textNode = contentEl || el.querySelector('.text p, .text span:last-child, .text, [class*="bubble"], [class*="content"]');
                    const fullText = el.innerText || '';
                    const content = clean(textNode ? textNode.innerText : el.innerText);
                    if (!content || /^(已读|未读|送达|发送失败|已发送)$/.test(content)) return;
                    if (content.length > 1000) return;
                    const cls = el.className || '';
                    const sender = cls.includes('item-myself') || cls.includes('myself') || cls.includes('self') || r.left > vw * 0.52 ? 'me' : 'hr';
                    const status = sender === 'me' ? pickStatus(fullText) : '';
                    result.push({sender: sender, content: content, status: status});
                };

                document.querySelectorAll('li.message-item, li[class*="message-item"]').forEach(el => push(el));
                if (result.length === 0) {
                    document.querySelectorAll('[class*="message"] [class*="bubble"], [class*="msg"] [class*="bubble"], [class*="chat"] [class*="text"]').forEach(el => push(el, el));
                }
                return result;
            }""")
            return raw or []
        except Exception:
            return []

    def open_conversation_by_name(self, hr_name: str) -> bool:
        """在聊天页中按 HR 名字定位并打开对应会话。"""
        try:
            current_url = self.page.url
            if "/web/geek/chat" not in current_url:
                self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=45000)
                pause(2, 3)

            # 优先用 Playwright 文本选择器点击列表项。BOSS 的左栏布局会随宽度变化，
            # 不能强依赖元素在屏幕左半边。
            for sel in [
                f'li[role="listitem"]:has-text("{hr_name}")',
                f'.user-list li:has-text("{hr_name}")',
                f'[class*="friend"]:has-text("{hr_name}")',
                f'text="{hr_name}"',
            ]:
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(force=True, timeout=3000)
                        pause(1, 2)
                        return True
                except Exception:
                    pass

            # 兜底：在 DOM 中找包含 HR 名的最小可点击会话容器并触发点击。
            clicked = self.page.evaluate(
                """(name) => {
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const candidates = [];
                    const selectors = [
                        '.user-list li', 'li[role="listitem"]', '.friend-content',
                        '[class*="friend"]', '[class*="conversation"]', '[class*="chat-item"]'
                    ];
                    document.querySelectorAll(selectors.join(',')).forEach(el => {
                        const text = (el.innerText || '');
                        if (text.length < 3 || text.length > 200) return;
                        if (!text.includes(name)) return;
                        if (!visible(el)) return;
                        const rect = el.getBoundingClientRect();
                        const nameEl = el.querySelector('.name-text, [class*="name"]');
                        const nameText = (nameEl && nameEl.innerText || '').trim();
                        const exact = nameText === name || text.split('\\n').some(line => line.trim() === name);
                        candidates.push({el: el, exact: exact ? 1 : 0, area: rect.width * rect.height, top: rect.top});
                    });
                    candidates.sort((a,b) => b.exact - a.exact || a.area - b.area || a.top - b.top);
                    for (const c of candidates) {
                        try {
                            c.el.scrollIntoView({block: 'center'});
                            const r = c.el.getBoundingClientRect();
                            const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
                            c.el.dispatchEvent(new MouseEvent('mousedown', opts));
                            c.el.dispatchEvent(new MouseEvent('mouseup', opts));
                            c.el.dispatchEvent(new MouseEvent('click', opts));
                            return true;
                        } catch(e) {}
                    }
                    return false;
                }""",
                hr_name,
            )
            if clicked:
                pause(1, 2)
                return True
            return False
        except Exception as e:
            print(f"  ⚠️ 打开会话失败 ({hr_name}): {e}")
            return False

    def send_message(self, text: str, fast: bool = True) -> bool:
        """逐字模拟键盘输入 + Enter 发送，确保 BOSS 检测到输入事件。"""
        try:
            # 点击输入框激活
            try:
                self.page.locator("#chat-input").first.click()
                time.sleep(0.15)
            except Exception:
                try:
                    self.page.locator('[contenteditable="true"]').first.click()
                    time.sleep(0.15)
                except Exception:
                    pass

            # 清除已有内容
            try:
                self.page.keyboard.press("Control+a")
                time.sleep(0.05)
                self.page.keyboard.press("Backspace")
                time.sleep(0.05)
            except Exception:
                pass

            # 逐字键入，模拟真人打字
            delay = 20 if fast else 40
            self.page.keyboard.type(text, delay=delay)
            pause(0.3, 0.6)

            # 按 Enter 发送
            self.page.keyboard.press("Enter")
            pause(0.5, 1)

            # 验证：消息区出现了刚发的文本
            body = self.page.inner_text("body")
            check = text[:8] if len(text) >= 8 else text[:4]
            if check in body:
                return True

            # 再试一次 Enter
            try:
                self.page.keyboard.press("Enter")
                pause(0.3, 0.5)
                return True
            except Exception:
                pass

            return False
        except Exception as e:
            print(f"  ⚠️ send_message 失败: {e}")
            return False

    def _send_greeting_via_chat_page(self, greeting_text: str, job_url: str = "") -> bool:
        """导航到聊天页，找到最近对话（刚才投递的HR），发送招呼语。"""
        try:
            # 导航到聊天页
            self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=15000)
            pause(2, 3)

            # 点"全部"tab 看所有对话（刚投递的HR在第一位）
            for sel in [
                'span.label-name:has-text("全部")',
                '.label-name:has-text("全部")',
                'li:has-text("全部")',
            ]:
                try:
                    tab = self.page.locator(sel).first
                    if tab.is_visible():
                        tab.click()
                        pause(0.5, 1)
                        break
                except Exception:
                    pass

            # 找到第一个对话（刚投递的HR在最前面）
            conv_selectors = [
                ".chat-item",
                'li[role="listitem"]',
                ".friend-content",
                '[class*="contact"]',
                '[class*="conversation"]',
            ]
            clicked = False
            for sel in conv_selectors:
                try:
                    items = self.page.locator(sel).all()
                    for item in items[:5]:
                        try:
                            if item.is_visible():
                                # 跳过"30天内暂无联系人"空状态
                                txt = item.inner_text()[:30] if hasattr(item, "inner_text") else ""
                                if "暂无" in txt or "30天" in txt:
                                    continue
                                item.click()
                                clicked = True
                                pause(0.5, 1)
                                break
                        except Exception:
                            continue
                    if clicked:
                        break
                except Exception:
                    continue

            if not clicked:
                # 最后尝试：直接点聊天列表第一个可见项
                try:
                    first = self.page.locator("li:visible").first
                    if first:
                        first.click()
                        clicked = True
                        pause(0.5, 1)
                except Exception:
                    pass

            if not clicked:
                body = ""
                try:
                    body = self.page.inner_text("body")[:200]
                except Exception:
                    pass
                print(f"  ⚠️ 聊天页未找到对话，body预览: {body}")
                return False

            # 在聊天输入框发消息
            return self.send_message(greeting_text)
        except Exception as e:
            print(f"  ⚠️ _send_greeting_via_chat_page 失败: {e}")
            return False

    def _get_chat_security_id(self, hr_name: str = "") -> str:
        """从 BOSS API 或页面提取对方 securityId。"""
        import re

        for attempt in range(3):  # 重试3次
            try:
                # 方式1: 页面 HTML 正则搜
                html = self.page.content()
                m = re.search(r'securityId["\']?\s*[:=]\s*["\']([A-Za-z0-9_~+/=-]{30,})["\']', html)
                if m:
                    return m.group(1)

                # 方式2: JS 全局对象
                sid = self.page.evaluate("""() => {
                    for (const key of Object.keys(window)) {
                        try {
                            const v = window[key];
                            if (!v || typeof v !== 'object') continue;
                            if (v.securityId) return v.securityId;
                        } catch(e) {}
                    }
                    return '';
                }""")
                if sid:
                    return sid

                # 方式3: BOSS API 获取会话列表, 按 HR 名匹配
                encrypt_id = ""
                try:
                    encrypt_id = self.page.evaluate("""() => {
                        for (const key of Object.keys(window)) {
                            try { if (window[key] && window[key].encryptSystemId) return window[key].encryptSystemId; } catch(e) {}
                        }
                        return '';
                    }""")
                except Exception:
                    pass

                if encrypt_id and hr_name:
                    url = f"https://www.zhipin.com/wapi/zprelation/friend/geekFilterByLabel?labelId=0&encryptSystemId={encrypt_id}"
                    data = self.page.evaluate(
                        """async (url) => {
                        const r = await fetch(url, {headers:{'Accept':'application/json','x-requested-with':'XMLHttpRequest'}, credentials:'include'});
                        return await r.json();
                    }""",
                        url,
                    )
                    friends = (data or {}).get("zpData", {}).get("friends", [])
                    for f in friends:
                        fn = (f.get("bossName") or f.get("realName") or "").strip()
                        if fn == hr_name:
                            return f.get("securityId", "")

                if attempt < 2:
                    print(f"  [securityId] 第{attempt + 1}次获取失败，重试...")
                    pause(1, 2)

            except Exception as e:
                print(f"  [securityId] 获取异常: {e}")
                if attempt < 2:
                    pause(1, 2)

        print(f"  ⚠️ securityId 获取失败（3次重试），HR: {hr_name}")
        return ""

    def send_wechat(self, hr_name: str = "") -> bool:
        """通过 BOSS API 发起交换，等弹窗出现后点「确定」。"""
        try:
            sid = self._get_chat_security_id(hr_name)

            if sid:
                self.page.evaluate(
                    """
                    async (sid) => {
                        await fetch('https://www.zhipin.com/wapi/zpchat/exchange/test', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded', 'x-requested-with': 'XMLHttpRequest'},
                            body: 'securityId=' + encodeURIComponent(sid) + '&type=2&friendSource=0',
                            credentials: 'include',
                        });
                    }
                """,
                    sid,
                )
                print("  [换微信] API /exchange/test 已调用")
            else:
                btn = self._find_element(SELECTORS["wechat_share_btn"], timeout_ms=5000)
                if not btn:
                    print("  ⚠️ send_wechat: 无法获取 securityId 且未找到按钮")
                    return False
                btn.click()
                print("  [换微信] 已点击换微信按钮")

            # 等弹窗 → 点「确定」
            confirm_clicked = self.page.evaluate("""() => {
                return new Promise((resolve) => {
                    let tries = 0;
                    const check = () => {
                        // 先找「确定与对方交换微信吗？」弹窗里的确定按钮
                        const btns = document.querySelectorAll('span');
                        for (const b of btns) {
                            if (b.innerText.trim() === '确定' && b.offsetParent !== null) {
                                const parent = b.closest('.secure-exchange, .sentence-popover, [class*="exchange"], [class*="popover"]');
                                if (parent) {
                                    b.click();
                                    resolve(true);
                                    return;
                                }
                            }
                        }
                        // 兜底：任何可见的"确定"按钮
                        const all = document.querySelectorAll('.btn-sure-v2, span');
                        for (const el of all) {
                            if (el.innerText.trim() === '确定' && el.offsetParent !== null && !el.closest('.btn-outline-v2')) {
                                el.click();
                                resolve(true);
                                return;
                            }
                        }
                        if (++tries < 30) setTimeout(check, 300);
                        else resolve(false);
                    };
                    check();
                });
            }""")
            if confirm_clicked:
                pause(0.5, 1)
                print("  [换微信] 已点确定按钮")
                return True

            print("  [换微信] 超时: 未找到确定按钮")
            return False

        except Exception as e:
            print(f"  ⚠️ send_wechat 失败: {e}")
            return False

    def send_phone(self, hr_name: str = "") -> bool:
        """通过 BOSS API 交换手机号（type=1），等弹窗出现后点「确定」。"""
        try:
            sid = self._get_chat_security_id(hr_name)

            if sid:
                self.page.evaluate(
                    """
                    async (sid) => {
                        await fetch('https://www.zhipin.com/wapi/zpchat/exchange/test', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded', 'x-requested-with': 'XMLHttpRequest'},
                            body: 'securityId=' + encodeURIComponent(sid) + '&type=1&friendSource=0',
                            credentials: 'include',
                        });
                    }
                """,
                    sid,
                )
                print("  [换电话] API /exchange/test (type=1) 已调用")
            else:
                btn = self._find_element(SELECTORS["phone_share_btn"], timeout_ms=5000)
                if not btn:
                    print("  ⚠️ send_phone: 无法获取 securityId 且未找到按钮")
                    return False
                btn.click()
                print("  [换电话] 已点击换电话按钮")

            # 等弹窗 → 点「确定」
            confirm_clicked = self.page.evaluate("""() => {
                return new Promise((resolve) => {
                    let tries = 0;
                    const check = () => {
                        const btns = document.querySelectorAll('span');
                        for (const b of btns) {
                            if (b.innerText.trim() === '确定' && b.offsetParent !== null) {
                                const parent = b.closest('.secure-exchange, .sentence-popover, .panel-contact, [class*="exchange"], [class*="popover"]');
                                if (parent) {
                                    b.click();
                                    resolve(true);
                                    return;
                                }
                            }
                        }
                        const all = document.querySelectorAll('.btn-sure-v2, span');
                        for (const el of all) {
                            if (el.innerText.trim() === '确定' && el.offsetParent !== null && !el.closest('.btn-outline-v2')) {
                                el.click();
                                resolve(true);
                                return;
                            }
                        }
                        if (++tries < 30) setTimeout(check, 300);
                        else resolve(false);
                    };
                    check();
                });
            }""")
            if confirm_clicked:
                pause(0.5, 1)
                print("  [换电话] 已点确定按钮")
                return True

            print("  [换电话] 超时: 未找到确定按钮")
            return False

        except Exception as e:
            print(f"  ⚠️ send_phone 失败: {e}")
            return False

    def send_resume(self) -> bool:
        """点击「发简历」按钮，等弹窗后点「发送」确认。

        策略：
        1. 优先用 page.evaluate 在 JS 里精确按 innerText 匹配 toolbar-btn 元素
           （Playwright `:has-text` 是部分匹配，可能误中其他按钮）
        2. 用 click() + 多次重试，等待 toolbar 完全渲染
        3. 弹窗里点「发送」用同样的精确匹配
        """
        try:
            # 等待 toolbar 渲染（最多 8 秒）
            clicked = False
            for attempt in range(16):
                try:
                    found = self.page.evaluate(
                        """() => {
                            const btns = document.querySelectorAll('.toolbar-btn, [class*="toolbar"] [class*="btn"]');
                            for (const b of btns) {
                                const txt = (b.innerText || '').trim();
                                if (txt === '发简历' && b.offsetParent !== null) {
                                    b.click();
                                    return {ok: true, txt: txt};
                                }
                            }
                            // 兜底：找所有可见元素中文本完全等于"发简历"的可点击元素
                            const all = document.querySelectorAll('div, span, button, a');
                            for (const el of all) {
                                const txt = (el.innerText || el.textContent || '').trim();
                                if (txt === '发简历' && el.offsetParent !== null) {
                                    el.click();
                                    return {ok: true, txt: txt, fallback: true};
                                }
                            }
                            const all_btns = Array.from(document.querySelectorAll('.toolbar-btn, .toolbar-btn-content'));
                            const names = all_btns.map(b => `"${(b.innerText||'').trim()}"`).join(', ');
                            return {ok: false, available: names};
                        }"""
                    )
                    if isinstance(found, dict) and found.get("ok"):
                        print(
                            f"  [发简历] 已点击发简历按钮 (txt={found.get('txt')!r}{', fallback' if found.get('fallback') else ''})"
                        )
                        clicked = True
                        break
                    if attempt == 15 and isinstance(found, dict):
                        print(f"  ⚠️ send_resume: 未找到发简历按钮，当前工具栏: {found.get('available', '?')}")
                except Exception as e:
                    if attempt == 15:
                        print(f"  ⚠️ send_resume evaluate 失败: {e}")
                time.sleep(0.5)

            if not clicked:
                # 最后兜底：用原 selector
                btn = self._find_element(SELECTORS["resume_attach_btn"], timeout_ms=2000)
                if not btn:
                    return False
                btn.click()
                print("  [发简历] 已点击发简历按钮 (selector fallback)")

            pause(1.2, 2.2)

            # ==========================================
            # 弹窗出来后，需要先勾选一份简历，"发送"按钮才会从 disabled 变 enabled
            # ==========================================
            _resume_selected = False
            for _sel_attempt in range(10):
                try:
                    _sel_result = self.page.evaluate(
                        """() => {
                            const dlgs = document.querySelectorAll('.choose-resume-dialog, .boss-popup, [class*="dialog"], [class*="popup"], [class*="modal"]');
                            for (const dlg of dlgs) {
                                if (dlg.offsetParent === null) continue;
                                const radios = dlg.querySelectorAll('input[type="radio"], input[type="checkbox"]');
                                for (const r of radios) {
                                    if (r.offsetParent !== null && !r.checked) {
                                        const label = r.closest('label') || r.parentElement;
                                        if (label) { label.click(); return {ok: true, method: 'radio'}; }
                                    }
                                }
                                const items = dlg.querySelectorAll('li, .resume-item, [class*="resume"], [class*="item"]');
                                for (const item of items) {
                                    const txt = (item.innerText || '').trim();
                                    if ((txt.includes('在线简历') || txt.includes('附件简历')) && item.offsetParent !== null) {
                                        item.click();
                                        return {ok: true, method: 'resume-item', txt: txt.slice(0, 30)};
                                    }
                                }
                            }
                            const allRadios = document.querySelectorAll('input[type="radio"]:checked, input[type="checkbox"]:checked');
                            for (const r of allRadios) {
                                const txt = (r.closest('label')?.innerText || r.parentElement?.innerText || '').trim();
                                if (txt.includes('简历')) {
                                    return {ok: true, method: 'already-checked', txt: txt.slice(0, 30)};
                                }
                            }
                            return {ok: false};
                        }"""
                    )
                    if isinstance(_sel_result, dict) and _sel_result.get("ok"):
                        print(f"  [发简历] 已选择简历 ({_sel_result.get('method')})")
                        _resume_selected = True
                        break
                except Exception:
                    pass
                pause(0.8, 1.5)

            if not _resume_selected:
                print(f"  ⚠️ send_resume: 未能选择简历项（弹窗中无可用简历）")

            pause(1.5, 2.5)

            # 点「发送」按钮（此时应已 enabled）
            for attempt in range(8):
                try:
                    confirmed = self.page.evaluate(
                        """() => {
                            const sels = ['.btn-sure-v2.btn-confirm', '.choose-resume-dialog .btn-confirm', '.boss-popup__content .btn-confirm'];
                            for (const sel of sels) {
                                const el = document.querySelector(sel);
                                if (el && el.offsetParent !== null && !el.disabled && !el.classList.contains('disabled')) {
                                    el.click(); return {ok: true, sel: sel};
                                }
                            }
                            const dlgs = document.querySelectorAll('.choose-resume-dialog, .boss-popup, [class*="dialog"], [class*="popup"]');
                            for (const dlg of dlgs) {
                                if (dlg.offsetParent === null) continue;
                                const btns = dlg.querySelectorAll('button, .btn, [class*="btn"]');
                                for (const b of btns) {
                                    if (b.disabled || b.classList.contains('disabled')) continue;
                                    const txt = (b.innerText || '').trim();
                                    if ((txt === '发送' || txt === '确定' || txt === '确认发送') && b.offsetParent !== null) {
                                        b.click();
                                        return {ok: true, txt: txt};
                                    }
                                }
                            }
                            return {ok: false};
                        }"""
                    )
                    if isinstance(confirmed, dict) and confirmed.get("ok"):
                        pause(0.8, 1.5)
                        print(f"  [发简历] 已点发送按钮 ({confirmed.get('sel') or confirmed.get('txt')})")
                        return True
                except Exception:
                    pass
                pause(0.8, 1.5)

            # 兜底：原 selector
            confirm = self._find_element(SELECTORS["resume_confirm_btn"], timeout_ms=2000)
            if confirm:
                confirm.click()
                pause(0.8, 1.5)
                print("  [发简历] 已点发送按钮 (selector fallback)")
                return True

            print("  [发简历] 无弹窗，按钮已点击但未确认")
            return True
        except Exception as e:
            print(f"  ⚠️ send_resume 失败: {e}")
            return False

    # ══════════════════════════════════════
    #  页面扫描 & 一键投递
    # ══════════════════════════════════════

    def scan_current_page(self) -> List[dict]:
        """扫描当前BOSS搜索结果页，提取所有可见岗位卡片。不跳转，只读当前页。"""
        print(f"  [扫描] 开始扫描当前页面...")
        self._scroll_all()
        jobs = self._extract_job_cards()
        if not jobs:
            lines = [l.strip() for l in self.page.inner_text("body").split("\n") if l.strip()]
            sal_idx = [i for i, l in enumerate(lines) if re.search(r"\d+[-~]\d+K", decode_salary(l), re.I)]
            for n, si in enumerate(sal_idx):
                if n > 0 and si - sal_idx[n - 1] < 3:
                    continue
                if si == 0:
                    continue
                title = lines[si - 1]
                if not (2 < len(title) < 60):
                    continue
                salary = decode_salary(lines[si])
                company = exp = edu = city = ""
                end = sal_idx[n + 1] if n + 1 < len(sal_idx) else min(si + 10, len(lines))
                for j in range(si + 1, min(end, len(lines))):
                    ln = lines[j]
                    if "经验" in ln or "应届" in ln:
                        exp = ln
                    elif re.search(r"本科|硕士|博士|大专|学历不限", ln):
                        edu = ln
                    elif "·" in ln and len(ln) < 30:
                        city = ln
                    elif (
                        not company
                        and len(ln) > 2
                        and len(ln) < 40
                        and not re.search(r"年|学历|大专|本科|硕士|博士|不限|应届|·", ln)
                    ):
                        company = ln
                jobs.append(
                    {
                        "title": title,
                        "salary": salary,
                        "company": company,
                        "experience": exp,
                        "education": edu,
                        "city": city,
                        "url": "",
                        "description": "",
                        "hr_name": "",
                        "hr_title": "",
                    }
                )
            links = self._extract_links()
            if links:
                lm = {l["title"][:12]: l["href"] for l in links if l["title"][:12]}
                for j in jobs:
                    if not j["url"] and j["title"][:12] in lm:
                        j["url"] = lm[j["title"][:12]]
        print(f"  [扫描] 从当前页面提取到 {len(jobs)} 个岗位")
        return jobs

    # ══════════════════════════════════════
    #  公司画像（用于 smart-send：先看公司再投递）
    # ══════════════════════════════════════

    def goto_company_similar_jobs(self, job_url: str) -> str:
        """从岗位详情页找到"查看该公司其他职位"链接并跳过去，返回 companyId。
        找不到链接抛 RuntimeError。"""
        if not job_url:
            raise RuntimeError("缺少岗位URL")
        try:
            self.page.goto(job_url, wait_until="load", timeout=45000)
        except Exception as e:
            raise RuntimeError(f"打开岗位详情页失败: {e}")
        pause(1, 2)

        href = None
        for sel in SELECTORS["company_similar_jobs_link"]:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    href = loc.get_attribute("href")
                    if href and "/gongsi/job/" in href:
                        break
            except Exception:
                continue

        # 回退：找不到 jobs 页链接时，用本岗位卡片关联的公司介绍页 /gongsi/<id>.html
        if not href or "/gongsi/job/" not in (href or ""):
            try:
                cid = self.page.evaluate(
                    r"""() => {
                    // 优先取页面内指向公司主页的链接（排除推荐区）
                    const a = document.querySelector('a[ka="company-name"][href*="/gongsi/"]')
                        || document.querySelector('.company-info a[href*="/gongsi/"]')
                        || document.querySelector('a[href*="/gongsi/"]:not([href$="/gongsi/"])');
                    if (!a) return "";
                    const m = (a.getAttribute('href') || '').match(/\/gongsi\/(?:job\/)?([0-9a-zA-Z]+~?)/);
                    return m ? m[1] : "";
                }"""
                )
                if cid:
                    # 直接拼 jobs 页 URL 跳过去
                    full_url = f"https://www.zhipin.com/gongsi/job/{cid}.html"
                    self.page.goto(full_url, wait_until="load", timeout=45000)
                    pause(2, 3)
                    if not self.check_page_safety():
                        raise RuntimeError("公司页安全检查未通过（可能被风控）")
                    return cid
            except RuntimeError:
                raise
            except Exception:
                pass

        if not href:
            raise RuntimeError("未找到 '查看该公司其他职位' 链接")

        m = re.search(r"/gongsi/job/([0-9a-zA-Z]+~?)", href)
        company_id = m.group(1) if m else ""
        full_url = urljoin("https://www.zhipin.com", href.split("?")[0])
        try:
            self.page.goto(full_url, wait_until="load", timeout=45000)
        except Exception as e:
            raise RuntimeError(f"打开公司页失败: {e}")
        pause(2, 3)
        if not self.check_page_safety():
            raise RuntimeError("公司页安全检查未通过（可能被风控）")
        return company_id

    def fetch_company_legal_rep(self, company_id: str) -> str:
        """从 BOSS 公司列表页注入对象里拿法定代表人。

        关键发现: BOSS 公司列表页
        https://www.zhipin.com/gongsi/job/<encryptBrandId>.html
        在 window.__BOSSCOMPANY__ 注入的初始数据里,brandInfo 节点
        直接带 `legalPerson`(法定代表人姓名)。

        注入结构路径: __BOSSCOMPANY__[1].data[0].brandJobInfo.brandComInfo
        实际不同版本路径可能不同,这里采用多路径容错:
          1. window.__BOSSCOMPANY__ 的 brandInfo.legalPerson
          2. window.__INITIAL_STATE__ 兜底
        若都拿不到则返回空字符串,不影响主流程。
        """
        cid = (company_id or "").strip()
        if not cid:
            return ""
        intro_url = f"https://www.zhipin.com/gongsi/{cid}.html"
        try:
            self.page.goto(intro_url, wait_until="load", timeout=45000)
        except Exception:
            return ""
        pause(1, 2)
        if not self.check_page_safety():
            return ""

        try:
            js = r"""
            () => {
                function tryRead(obj, path) {
                    try {
                        var cur = obj;
                        var parts = path.split('.');
                        for (var i = 0; i < parts.length && cur; i++) {
                            cur = cur[parts[i]];
                        }
                        return (typeof cur === 'string') ? cur : '';
                    } catch (e) { return ''; }
                }
                // 来自真实 BOSS 公司介绍页 __BOSSCOMPANY__ 注入结构路径
                var paths = [
                    '__BOSSCOMPANY__.brandInfo.legalPerson',
                    '__BOSSCOMPANY__.comInfo.legalPerson',
                    '__BOSSCOMPANY__.brandComInfo.legalPerson',
                    '__BOSSCOMPANY__.[1].data.[0].brandInfo.legalPerson',
                    '__BOSSCOMPANY__.[1].data.[0].brandComInfo.legalPerson',
                    '__BOSSCOMPANY__.[1].data.[0].comInfo.legalPerson',
                    '__INITIAL_STATE__.data.brandInfo.legalPerson'
                ];
                for (var i = 0; i < paths.length; i++) {
                    var segs = paths[i].split('.');
                    var root = segs[0];
                    var rest = segs.slice(1).join('.');
                    var v = tryRead(window[root], rest);
                    if (v) return v;
                }
                return '';
            }
            """
            result = self.page.evaluate(js) or ""
        except Exception:
            return ""

        result = str(result).strip()
        if result and len(result) <= 8 and not any(c in result for c in ("(", "（", " ", "\n")):
            return result
        return ""

    def parse_company_similar_jobs_page(self) -> dict:
        """解析 /gongsi/job/<id>.html 页面。
        返回 {open_count:int, jobs:[{title,salary,url,hr_block,hr_name,hr_title,company}], page_url:str, company:str}。

        核心策略：用 page.inner_text('body') 拿纯文本 → 按行分块解析。
        BOSS 公司页格式（每个岗位块）：
            标题
            薪资（5-8K）
            经验（1-3年）
            学历（大专）
            HR名·头衔
            城市·区·地标
        用"薪资行"作为锚点，往上取标题，往下取 HR。
        """
        result = {"open_count": 0, "jobs": [], "page_url": self.page.url, "company": ""}

        try:
            body_text = self.page.inner_text("body")
        except Exception as e:
            print(f"  [公司页] inner_text 失败: {e}")
            return result

        lines = [l.strip() for l in body_text.split("\n") if l.strip()]

        # 1) open_count：从文本里匹配 "11在招职位" / "在招 11 个" / "招聘职位(11)"
        for ln in lines[:30]:
            m = re.search(r"(\d+)\s*在招\s*职位", ln)
            if not m:
                m = re.search(r"在招\s*(\d+)\s*个", ln)
            if not m:
                m = re.search(r"招聘职位\s*\(\s*(\d+)\s*\)", ln)
            if not m:
                m = re.search(r"(\d+)\s*个在招", ln)
            if m:
                result["open_count"] = int(m.group(1))
                break

        # 2) 公司名：从页面 title 或文本中拿
        try:
            page_title = self.page.title() or ""
            # 格式："「淄博齐行家体育文...招聘」2026年..."  或 "XX公司招聘..."
            company_name = re.sub(r"[「【]", "", page_title)
            company_name = re.sub(r"招聘.*$", "", company_name)
            company_name = re.sub(r"[」】]", "", company_name).strip()
            if company_name:
                result["company"] = company_name
        except Exception:
            pass

        # 3) 解析岗位块。BOSS 公司页岗位块结构：
        #    标题 / 经验(3-5年|经验不限) / 学历(本科|大专|硕士|学历不限) / HR名·头衔 / 城市·区·地标
        #    注意：公司页岗位卡不一定显示薪资，所以用 HR 行作为锚点（最稳定），往上找标题。
        salary_re = re.compile(r"^\d+[-~]\d+K|\d+-\d+元|^\d+元/[时月]", re.I)
        exp_re = re.compile(r"^(?:\d+[-~]\d+年|\d+年以[上下]|经验不限|应届生?|在校生?|\d+年内)$")
        edu_re = re.compile(r"^(?:初中及?以下|高中|中专|大专|本科|硕士|博士|学历不限|MBA|EMBA)$")
        hr_re = re.compile(
            r"^([\u4e00-\u9fa5]{2,4})\s*[·•・]\s*"
            r"(HRBP|HR|猎头|顾问|招聘[\u4e00-\u9fa5]{0,3}|HR[\u4e00-\u9fa5]{0,3}|"
            r"[\u4e00-\u9fa5]{0,4}(?:经理|主管|总监|负责人|助理|专员|人事))$"
        )

        # 先提取所有 job_detail 链接
        try:
            links = (
                self.page.evaluate(r"""() => {
                const r = []; const s = new Set();
                document.querySelectorAll('a[href*="/job_detail/"]').forEach(a => {
                    const h = a.href, t = (a.innerText || '').trim();
                    if (h && t && !s.has(h) && t.length < 80) { s.add(h); r.push({href:h, title:t.substring(0,60)}); }
                });
                return r;
            }""")
                or []
            )
        except Exception:
            links = []
        link_map = {}
        for lk in links:
            key = (lk.get("title") or "")[:12]
            if key:
                link_map[key] = lk.get("href", "")

        # 以 HR 行为锚点定位岗位块
        jobs = []
        seen_titles = set()
        hr_indices = [i for i, ln in enumerate(lines) if hr_re.match(ln)]
        for hi in hr_indices:
            m = hr_re.match(lines[hi])
            if not m:
                continue
            hr_name = m.group(1)
            hr_title = m.group(2)
            hr_block = lines[hi]

            # 往上跳过 学历 / 经验 / 薪资 行，剩下的第一行即标题
            ti = hi - 1
            salary = ""
            while ti >= 0 and (edu_re.match(lines[ti]) or exp_re.match(lines[ti]) or salary_re.match(lines[ti])):
                if salary_re.match(lines[ti]) and not salary:
                    salary = lines[ti]
                ti -= 1
            if ti < 0:
                continue
            title = lines[ti]
            if not (2 < len(title) < 80):
                continue
            if title in seen_titles:
                continue
            # 过滤明显非岗位标题的导航/区块行
            if any(bad in title for bad in ("招聘职位", "公司简介", "职位类型", "工作城市", "全部(")):
                continue
            seen_titles.add(title)

            url = link_map.get(title[:12], "")
            jobs.append(
                {
                    "title": title,
                    "salary": salary,
                    "url": url,
                    "hr_block": hr_block,
                    "hr_name": hr_name,
                    "hr_title": hr_title,
                    "company": result.get("company") or "",
                }
            )

        result["jobs"] = jobs
        return result

    def aggregate_company_hrs(self, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """从 parse_company_similar_jobs_page 返回的 jobs 列表里聚合去重 HR。
        每条 hr: {name, title, priority, associated_jobs:[...]}。
        同一 HR 出现在多个岗位时，职务取最高（按 _hr_title_score）。"""
        hrs_map: Dict[str, Dict[str, Any]] = {}
        hr_name_re = re.compile(
            r"([\u4e00-\u9fa5]{2,4})\s*[·｜\|\s]\s*"
            r"((?:HRBP|HR|猎头|顾问)|招聘[\u4e00-\u9fa5]{0,3}|"
            r"HR[\u4e00-\u9fa5]{0,3}|"
            r"[\u4e00-\u9fa5]{0,4}(?:经理|主管|总监|负责人|助理|专员|人事))"
        )
        for j in jobs or []:
            block = (j.get("hr_block") or "").strip()
            if not block:
                continue
            for m in hr_name_re.finditer(block):
                name = m.group(1).strip()
                title = m.group(2).strip()
                if name not in hrs_map:
                    hrs_map[name] = {
                        "name": name,
                        "title": title,
                        "priority": _hr_title_score(title),
                        "associated_jobs": [],
                    }
                else:
                    cur_score = hrs_map[name]["priority"]
                    new_score = _hr_title_score(title)
                    if new_score > cur_score:
                        hrs_map[name]["title"] = title
                        hrs_map[name]["priority"] = new_score
                if j.get("title") and j["title"] not in hrs_map[name]["associated_jobs"]:
                    hrs_map[name]["associated_jobs"].append(j["title"])
        hrs = list(hrs_map.values())
        hrs.sort(key=lambda h: (-h["priority"], h["name"]))
        for h in hrs:
            h["is_top"] = bool(hrs) and h["name"] == hrs[0]["name"]
        return hrs

    def scan_and_apply_current_page(self, greeting_template: Optional[str] = None) -> dict:
        """扫描当前页面全部岗位 → 一键批量投递。"""
        jobs = self.scan_current_page()
        if not jobs:
            return {"success": False, "message": "当前页面未找到任何岗位", "scanned": 0, "applied": 0}
        urls = [j["url"] for j in jobs if j.get("url")]
        if not urls:
            return {"success": False, "message": "扫描到的岗位没有有效URL", "scanned": len(jobs), "applied": 0}
        results = self.apply_batch(urls, greeting_template)
        success_count = sum(1 for r in results if r.get("success"))
        return {
            "success": success_count > 0,
            "message": f"扫描 {len(jobs)} 个岗位，投递 {success_count}/{len(urls)}",
            "scanned": len(jobs),
            "applied": success_count,
            "results": results,
        }

    # ══════════════════════════════════════
    #  监控周期（供后台循环调用）
    # ══════════════════════════════════════

    def _click_chat_tab(self, label: str) -> bool:
        """切换聊天页顶部 Tab（全部/未读/新招呼/仅沟通）。"""
        for sel in (f'span.label-name:has-text("{label}")', f'.label-name:has-text("{label}")'):
            try:
                tab = self.page.locator(sel).first
                if tab.count() > 0 and tab.is_visible():
                    tab.click()
                    pause(0.5, 1)
                    return True
            except Exception:
                continue
        return False

    def sync_conversation_list_full(self) -> int:
        """在「全部」Tab 下抽取每个会话卡片的 hr_name / company / job_title / last_msg
        并写回数据库。返回更新的会话数。失败返回 0，不抛异常。
        """
        try:
            self._click_chat_tab("全部")
        except Exception:
            pass
        try:
            cards = self.page.evaluate(
                """() => {
                const out = [];
                const seen = new Set();
                const items = document.querySelectorAll('li[role="listitem"], .user-list li');
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const ROLE_RE = /(招聘者|招聘专员|招聘主管|招聘总监|HR(经理|主管|总监|专员)?|人力(资源)?(专员|经理|主管|总监|顾问)?|人力|人事(经理|主管|总监|专员)?|人事|猎头(顾问|经理)?|猎头|顾问|总监|经理|老板|创始人|合伙人|首席执行官|CEO|CTO|COO|CFO|VP|主管|专员|区域)$/;
                let pos = 0;
                for (const el of items) {
                    if (!visible(el)) continue;
                    const fullText = (el.innerText || '').trim();
                    if (!fullText || fullText.length < 2) continue;
                    const key = fullText.replace(/\\s+/g, ' ').slice(0, 80);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    let hr = '';
                    try {
                        const nm = el.querySelector('.name-text, .geek-name, [class*="name-text"]');
                        if (nm) hr = (nm.innerText || '').trim();
                    } catch(e) {}
                    let wrap = '';
                    try {
                        const w = el.querySelector('[class*="name"]:not(.name-text):not(.geek-name)');
                        if (w) wrap = (w.innerText || '').trim();
                    } catch(e) {}
                    if (!hr && wrap) hr = (wrap.split('\\n')[0] || '').trim();
                    let after = wrap;
                    if (hr && wrap.startsWith(hr)) after = wrap.slice(hr.length).trim();
                    let role = '';
                    let company = after;
                    const m = ROLE_RE.exec(after);
                    if (m) {
                        role = m[0];
                        company = after.slice(0, m.index).trim();
                    }
                    let lastMsg = '';
                    try {
                        const lm = el.querySelector('[class*="last"], [class*="msg-text"], [class*="message-text"]');
                        if (lm) lastMsg = (lm.innerText || '').trim();
                    } catch(e) {}
                    lastMsg = lastMsg.replace(/^\\[(送达|已读|未读|发送失败|已发送)\\]\\s*/g, '').trim();
                    const unreadEl = el.querySelector('.red-dot, [class*="unread"], [class*="badge"]');
                    const unread = !!(unreadEl && visible(unreadEl));
                    out.push({
                        hr_name: hr,
                        company: company,
                        role: role,
                        job_title: '',
                        last_msg: lastMsg,
                        unread: unread,
                        raw: fullText.slice(0, 200),
                        position: pos++,
                    });
                }
                return out;
            }"""
            )
        except Exception as e:
            print(f"  [sync-list] DOM 抽取失败: {e}")
            return 0

        if not cards:
            return 0

        try:
            from boss_state import get_db, list_active_conversations
        except Exception:
            return 0

        known = {c["hr_name"]: c for c in list_active_conversations() if c.get("hr_name")}
        updated = 0
        db = get_db()
        for c in cards:
            hr = (c.get("hr_name") or "").strip()
            if not hr or len(hr) < 2:
                continue
            company = (c.get("company") or "").strip()
            job = (c.get("job_title") or "").strip()
            last_msg = (c.get("last_msg") or "").strip()
            kc = known.get(hr)
            if not kc:
                continue
            sets = []
            args: list = []
            old_company = (kc.get("hr_company") or "").strip()
            if company and company != old_company and len(company) >= 2:
                sets.append("hr_company=?")
                args.append(company[:80])
            if job and not kc.get("job_title"):
                sets.append("job_title=?")
                args.append(job[:80])
            old_last = (kc.get("last_message_text") or "").strip()
            if last_msg and last_msg != old_last:
                sets.append("last_message_text=?")
                args.append(last_msg[:200])
            if not sets:
                continue
            sets.append("updated_at=CURRENT_TIMESTAMP")
            args.append(kc["id"])
            try:
                db.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?", tuple(args))
                updated += 1
            except Exception:
                pass
        try:
            db.commit()
        except Exception:
            pass
        if updated:
            print(f"  [sync-list] 回填 {updated} 个会话字段")
        return updated

    def run_chat_monitor_cycle(self) -> dict:
        """
        一个完整的监控周期:
        1. 导航到聊天页
        2. 扫描未读会话
        3. 对每个未读会话: 打开→读消息→存库→AI回复
        """
        import datetime
        _t_start = time.time()
        print(f"\n{'='*60}")
        print(f"[监控] ====== 开始新一轮监控周期 {datetime.datetime.now().strftime('%H:%M:%S')} ======")
        result = {"checked": 0, "new_messages": 0, "replies_sent": 0}

        # 只在不在聊天页时才导航（避免每轮刷新页面，触发 BOSS 登录检查）
        current_url = self.page.url
        need_nav = "/web/geek/chat" not in current_url
        print(f"[监控] 当前URL: {current_url[:80]}  need_nav={need_nav}")
        if need_nav:
            print(f"[监控] 导航到聊天页...")
            if not self.navigate_to_chat():
                print(f"[监控] ❌ 导航到聊天页失败")
                return result
            print(f"[监控] ✅ 导航完成")

        # 先在「全部」Tab 下抓一次完整列表，回填 company/job_title/last_msg（修 P10 漏字段）
        print(f"[监控] 同步会话列表...")
        try:
            self.sync_conversation_list_full()
            print(f"[监控] ✅ 会话列表同步完成")
        except Exception as e:
            print(f"[监控] ⚠️ sync_conversation_list_full 异常: {e}")

        # 切到「未读」Tab 处理待回复会话
        if not need_nav:
            self._click_chat_tab("未读")

        if not self.check_page_safety():
            print(f"[监控] ❌ 安全检查未通过（登录过期/验证码等），终止本轮")
            return result

        conversations = self.poll_conversation_list()
        result["checked"] = len(conversations)
        print(f"[监控] 扫描到 {len(conversations)} 个会话")
        try:
            preview = (self.page.inner_text("body") or "")[:800].replace("\n", " | ")
            print(f"[监控] Body预览: {preview}")
        except Exception:
            pass

        from boss_state import list_active_conversations

        known_convs = list_active_conversations()
        print(f"[监控] DB已知活跃会话: {len(known_convs)}个")

        if not conversations:
            print(f"[监控] 无未读消息，周期结束")
            print(f"{'='*60}\n")
            return result
        if len(conversations) > 3:
            print(f"[监控] 未读会话 {len(conversations)} 个，本轮处理前3个")
            conversations = conversations[:3]

        for idx, conv_data in enumerate(conversations):
            print(f"\n[监控] --- 处理会话 {idx+1}/{len(conversations)} ---")
            text = conv_data.get("text", "")
            has_unread = conv_data.get("has_unread", False)
            element = conv_data.get("element")

            if not text:
                continue

            # 尝试匹配已知会话：用提取的 HR 名字精确匹配
            matched_conv = None
            extracted_name = conv_data.get("hr_name", "")
            for kc in known_convs:
                kc_name = kc.get("hr_name", "")
                if kc_name and extracted_name and kc_name == extracted_name:
                    matched_conv = kc
                    break

            if not matched_conv:
                for kc in known_convs:
                    kc_name = kc.get("hr_name", "")
                    if kc_name and len(kc_name) >= 3 and kc_name in text:
                        matched_conv = kc
                        break

            if not matched_conv:
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                hr_name = conv_data.get("hr_name", "") or lines[0] if lines else ""
                hr_name = hr_name[:20] if len(hr_name) > 20 else hr_name

                # 过滤无效名称
                skip_keywords = [
                    "消息",
                    "联系人",
                    "沟通",
                    "设置",
                    "搜索",
                    "我的",
                    "首页",
                    "已沟通",
                    "继续沟通",
                    "新对话",
                    "系统",
                    "通知",
                    "BOSS",
                    "在线",
                    "离线",
                    "刚刚",
                    "分钟",
                    "小时",
                    "昨天",
                    "简历",
                    "附件",
                    "上传",
                    "制作",
                    "更新",
                    "AI",
                ]
                is_valid = (
                    hr_name
                    and len(hr_name) >= 2
                    and not hr_name.isdigit()
                    and not any(kw == hr_name for kw in skip_keywords)
                    and not any(kw in hr_name and len(hr_name) <= len(kw) + 1 for kw in skip_keywords)
                )
                if not is_valid:
                    print(f"  [监控] 跳过无效会话名: '{hr_name}' (原文: {text[:50]})")
                    continue

                conv_id = get_or_create_conversation(
                    None, hr_name, conv_data.get("company", ""), conv_data.get("job_title", "")
                )
                known_convs = list_active_conversations()
                matched_conv = get_conversation(conv_id)
                if not matched_conv:
                    continue
                print(f"  [监控] 新建会话: {hr_name}")
                # 标记用于 WebSocket 广播
                result.setdefault("new_conversations", []).append(hr_name)
            else:
                conv_id = matched_conv["id"]
                # 提取的名字比 DB 更精确时自动修正
                if extracted_name and len(extracted_name) >= 2:
                    old_name = matched_conv.get("hr_name", "")
                    if old_name != extracted_name and (
                        old_name in extracted_name or extracted_name in old_name or len(extracted_name) < len(old_name)
                    ):
                        try:
                            from boss_state import get_db as _gdb2

                            _gdb2().execute("UPDATE conversations SET hr_name=? WHERE id=?", (extracted_name, conv_id))
                            _gdb2().commit()
                            matched_conv["hr_name"] = extracted_name
                        except Exception:
                            pass

            # 从会话文本里提取公司名（格式：HR名+公司名+岗位）
            if not matched_conv.get("hr_company"):
                company_info = text.split("\n")[0] if "\n" in text else text
                import re as _re3

                hr_name_part = matched_conv.get("hr_name", "")
                if hr_name_part and len(hr_name_part) >= 2:
                    company_info = company_info.replace(hr_name_part, "", 1)
                # 去掉时间/状态/括号等
                company_info = _re3.sub(r"\d{1,2}:\d{2}|\[.*?\]|送达|已读|未读", "", company_info)
                # 提取公司名（纯中文 4-12字）
                m = _re3.search(r"[\u4e00-\u9fa5]{4,12}", company_info)
                if m:
                    company = m.group()
                    try:
                        from boss_state import get_db as _gdb3

                        _gdb3().execute("UPDATE conversations SET hr_company=? WHERE id=?", (company, conv_id))
                        _gdb3().commit()
                        matched_conv["hr_company"] = company
                        print(f"  [监控] 提取公司名: {company}")
                    except Exception:
                        pass

            if matched_conv.get("status") != "active":
                continue
            if not matched_conv.get("auto_reply_enabled"):
                continue

            # 读取消息：打开会话从 DOM 提取
            hr_name_to_open = matched_conv["hr_name"]
            opened = self.open_conversation_by_name(hr_name_to_open)
            if not opened and len(hr_name_to_open) > 4:
                short = re.match(r"^[\u4e00-\u9fff]{2,3}", hr_name_to_open)
                if short:
                    opened = self.open_conversation_by_name(short.group(0))
            if not opened:
                print(f"  [监控] 无法打开会话: {hr_name_to_open}")
                continue
            pause(1, 2)
            msgs = self.read_visible_messages()
            print(f"  [监控] 会话 {matched_conv.get('hr_name')}: 读到 {len(msgs)} 条消息")

            new_count = 0
            clean_msgs = []
            for msg in msgs:
                sender = msg.get("sender", "hr")
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                clean_msgs.append({"sender": sender, "content": content, "status": msg.get("status", "")})

            if clean_msgs:
                replace_conversation_messages(conv_id, clean_msgs)
                last_msg = clean_msgs[-1]
                update_conversation_last_message(conv_id, last_msg["content"], last_msg["sender"], 0)

                # 从 HR 消息里提取微信号
                if not matched_conv.get("hr_wechat"):
                    import re as _re

                    for m in clean_msgs:
                        if m["sender"] == "hr":
                            patterns = [
                                # wxid_xxxxxxxx 格式
                                r"(?:wxid|WXID)[_\-]?\s*[:：]?\s*([a-zA-Z0-9_-]{6,30})",
                                # 微信/VX/WeChat：xxx 格式
                                r"(?:微信|VX|vx|wechat|WeChat)[号：:]*\s*[:：]?\s*([a-zA-Z0-9_-]{4,30})",
                                # 加我/加V -> xxx
                                r"(?:加我|加V|找V|加个V)\s*[:：]?\s*([a-zA-Z0-9_-]{4,30})",
                                # 微信号 xxx（纯中文前缀）
                                r"\u5fae\u4fe1\u53f7\s+([a-zA-Z0-9_-]{4,30})",
                            ]
                            for pat in patterns:
                                match = _re.search(pat, m["content"])
                                if match:
                                    wx_id = match.group(1).strip()
                                    if wx_id and len(wx_id) >= 5:
                                        update_conversation_wechat(conv_id, wx_id)
                                        matched_conv["hr_wechat"] = wx_id
                                        result["wechat_exchanged"] = True
                                        print(f"  [监控] 提取HR微信: {wx_id}")
                                        break

            # 检测需要回复的 HR 消息：仅跳过纯 BOSS 系统通知（<80字且以系统模式开头）
            def _is_system_notification(content):
                content = content.strip()
                if len(content) > 80:
                    return False
                patterns = (
                    "你与该职位竞争者PK情况",
                    "竞争力分析",
                    "BOSS安全提示",
                    "系统消息",
                    "沟通分析",
                    "今日推荐",
                    "该Boss已查看了你的简历",
                )
                return any(content.startswith(p) for p in patterns)

            unreplied_hr_msg = None
            for i in range(len(clean_msgs) - 1, -1, -1):
                m = clean_msgs[i]
                if m["sender"] == "me":
                    continue
                if _is_system_notification(m["content"]):
                    continue
                # HR 消息
                has_reply_after = any(clean_msgs[j]["sender"] == "me" for j in range(i + 1, len(clean_msgs)))
                if not has_reply_after:
                    unreplied_hr_msg = m["content"]
                    new_count = 1
                    print(f"  [监控] 待回复HR消息: {m['content'][:60]}...")
                break

            if unreplied_hr_msg:
                result["new_messages"] += 1

            # 自动回复
            auto_reply_enabled = get_setting("auto_reply_enabled", "false") == "true"
            if unreplied_hr_msg and auto_reply_enabled:
                print(f"[监控] 自动回复: 已启用, 开始生成回复...")
                today_replies = get_today_auto_reply_count()
                if today_replies >= MAX_AUTO_REPLY_PER_DAY:
                    print(f"[监控] 自动回复: 已达日上限({MAX_AUTO_REPLY_PER_DAY}条), 跳过")
                    continue

                # 风控冷却期：本轮不发消息
                if not self._respect_cooldown():
                    print(f"[监控] 自动回复: 风控冷却中, 跳过")
                    continue

                self._human_pace(min_gap=5.0, max_gap=15.0)

                try:
                    from boss_replier import generate_reply

                    job_title = matched_conv.get("job_title", "")
                    job_company = matched_conv.get("hr_company", "")
                    job_desc = ""
                    app_id = matched_conv.get("application_id")
                    if app_id:
                        from boss_state import get_application

                        app = get_application(app_id)
                        if app:
                            job_desc = app.get("description") or ""
                            job_title = job_title or app.get("job_title", "")
                            job_company = job_company or app.get("company", "")

                    job_info = {
                        "title": job_title,
                        "company": job_company,
                        "description": job_desc,
                    }
                    style = get_setting("ai_reply_style", "professional")
                    resume = get_setting("resume_summary", "")
                    wechat = get_setting("wechat_id", "")

                    gen = generate_reply(conv_id, unreplied_hr_msg, job_info, style, resume, wechat)
                    reply = gen["reply"]
                    interest = gen.get("interest", "")
                    if gen.get("transfer"):
                        update_conversation_transfer_requested(conv_id)
                        result["transfer_requested"] = True
                    if reply:
                        # 先执行发送操作（简历/微信/电话），确保AI说"已发送"时东西已经发出去了
                        msg_lower = unreplied_hr_msg.lower()

                        # 发简历：HR明确要求简历时，且未发送过
                        if any(kw in msg_lower for kw in ("简历", "cv", "resume")):
                            if not matched_conv.get("resume_sent"):
                                print(f"  [监控] HR要简历，正在发送...")
                                if self.send_resume():
                                    from boss_state import mark_resume_sent

                                    mark_resume_sent(conv_id)
                                    pause(1, 2)

                        # 换微信：HR主动要联系方式时（排除"保持联系"等模糊表达）
                        wechat_keywords = (
                            "加微信",
                            "加个微信",
                            "微信聊",
                            "vx",
                            "加v",
                            "v我",
                            "加个v",
                            "微信号",
                            "换微信",
                        )
                        if any(kw in msg_lower for kw in wechat_keywords):
                            if not matched_conv.get("hr_wechat"):
                                print(f"  [监控] HR要微信，正在发送...")
                                self.send_wechat(hr_name_to_open)
                                pause(1, 2)

                        # 换电话：HR明确要电话时，且未发送过
                        if any(kw in msg_lower for kw in ("电话", "手机号")):
                            if not matched_conv.get("phone_shared"):
                                print(f"  [监控] HR要电话，正在发送...")
                                if self.send_phone(hr_name_to_open):
                                    from boss_state import mark_phone_shared

                                    mark_phone_shared(conv_id)
                                    pause(1, 2)

                        # 然后再发送AI回复
                        print(f"  [监控] AI回复: {reply[:60]}...")
                        # 模拟真人"看到消息→思考→打字"的延迟，按回复长度浮动
                        think = random.uniform(2, 5) + min(len(reply) * 0.05, 6)
                        time.sleep(think)
                        if self.send_message(reply):
                            add_message(conv_id, "me", reply, ai_generated=True)
                            update_conversation_last_message(conv_id, reply, "me", 0)
                            increment_daily_stat("auto_replies_sent")
                            result["replies_sent"] += 1
                            if interest:
                                update_conversation_interest(conv_id, interest)
                                print(f"  [监控] HR兴趣度: {interest}")
                            print(f"  [监控] 回复已发送")
                        else:
                            print(f"  [监控] 回复发送失败!")
                        pause(5, 15)

                        # ══════════════════════════════════════
                        # 回复后再扫描：防止HR在等待期间发新消息被自动已读
                        # 问题：回复+等待期间HR回复多条消息，BOSS自动已读，
                        # 下个周期 poll_conversation_list 在"未读"tab看不到此会话
                        # 解决：回复后重新扫描当前会话，有新未回复HR消息则追加回复
                        # ══════════════════════════════════════
                        _recheck_rounds = 0
                        _max_recheck = 2
                        while _recheck_rounds < _max_recheck:
                            _recheck_rounds += 1
                            pause(1, 2)
                            _msgs2 = self.read_visible_messages()
                            _clean2 = []
                            for _m2 in _msgs2:
                                _s2 = _m2.get("sender", "hr")
                                _c2 = (_m2.get("content") or "").strip()
                                if not _c2:
                                    continue
                                _clean2.append({"sender": _s2, "content": _c2, "status": _m2.get("status", "")})

                            if not _clean2 or len(_clean2) <= len(clean_msgs):
                                print(f"  [监控] 无新消息，退出再检查 (round={_recheck_rounds})")
                                break

                            # 有新消息，保存到DB
                            print(f"  [监控] 检测到新消息 ({len(_clean2)} 条, 原{len(clean_msgs)}条)，重新保存")
                            replace_conversation_messages(conv_id, _clean2)

                            # 找最新一条未回复的HR消息
                            _new_hr_msg = None
                            for _i2 in range(len(_clean2) - 1, -1, -1):
                                _m2 = _clean2[_i2]
                                if _m2["sender"] == "me":
                                    continue
                                if _is_system_notification(_m2["content"]):
                                    continue
                                _has_my_reply = any(
                                    _clean2[_j]["sender"] == "me" for _j in range(_i2 + 1, len(_clean2))
                                )
                                if not _has_my_reply:
                                    _new_hr_msg = _m2["content"]
                                    break

                            if not _new_hr_msg:
                                print(f"  [监控] 新消息已全部回复，退出再检查")
                                break

                            print(f"  [监控] 再检查发现待回复HR消息: {_new_hr_msg[:60]}...")

                            # 检查日配额
                            _today_replies = get_today_auto_reply_count()
                            if _today_replies >= MAX_AUTO_REPLY_PER_DAY:
                                print(f"  [监控] 再回复已达日上限({MAX_AUTO_REPLY_PER_DAY}条), 跳过")
                                break

                            if not self._respect_cooldown():
                                print(f"  [监控] 再回复风控冷却中, 跳过")
                                break

                            self._human_pace(min_gap=3.0, max_gap=8.0)

                            try:
                                _gen2 = generate_reply(conv_id, _new_hr_msg, job_info, style, resume, wechat)
                                _reply2 = _gen2["reply"]
                                if _reply2:
                                    _think2 = random.uniform(2, 4) + min(len(_reply2) * 0.05, 5)
                                    time.sleep(_think2)
                                    if self.send_message(_reply2):
                                        add_message(conv_id, "me", _reply2, ai_generated=True)
                                        update_conversation_last_message(conv_id, _reply2, "me", 0)
                                        increment_daily_stat("auto_replies_sent")
                                        result["replies_sent"] += 1
                                        _interest2 = _gen2.get("interest", "")
                                        if _interest2:
                                            update_conversation_interest(conv_id, _interest2)
                                        print(f"  [监控] 追回回复已发送: {_reply2[:40]}...")
                                        clean_msgs = _clean2  # 更新基线，下一轮检查用
                                        pause(3, 8)
                                    else:
                                        print(f"  [监控] 追回回复发送失败!")
                                        break
                                else:
                                    print(f"  [监控] 追回回复为空，跳过")
                                    break
                            except Exception as _e2:
                                print(f"  ⚠️ 追回回复生成失败: {_e2}")
                                break
                except Exception as e:
                    print(f"  ⚠️ AI回复生成失败: {e}")
            elif unreplied_hr_msg and not auto_reply_enabled:
                print(f"  [监控] 自动回复已关闭，跳过")

            # 下一个会话前确保输入框已清空，避免残留文字
            try:
                input_el = self.page.locator("#chat-input").first
                text = input_el.inner_text().strip()
                if text:
                    print(f"  [监控] 输入框残留文字「{text[:30]}...」，正在清空")
                    input_el.click()
                    self.page.keyboard.press("Control+a")
                    self.page.keyboard.press("Backspace")
                    pause(0.3, 0.5)
            except Exception:
                pass
            pause(0.5, 1)

        # 监控循环结束后切回「全部」Tab，避免 BOSS 页面长期停留在未读视图（P09）
        try:
            self._click_chat_tab("全部")
        except Exception:
            pass

        _t_elapsed = time.time() - _t_start
        print(f"[监控] ====== 本轮完成: 扫描{result['checked']}个会话, 新消息{result['new_messages']}条, 回复{result['replies_sent']}条, 耗时{_t_elapsed:.1f}s ======")
        print(f"{'='*60}\n")
        return result
