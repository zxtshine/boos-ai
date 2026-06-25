#!/usr/bin/env python3
"""
AI 回复生成 —— 调用 DeepSeek API 为 BOSS直聘聊天生成自动回复。
每次回复同时由 DeepSeek 根据对话上下文评估 HR 兴趣度 (high/medium/low)。
"""

import json
import re
import sys
from pathlib import Path

# 复用 interview/llm_client.py
sys.path.insert(0, str(Path(__file__).parent / "interview"))
from llm_client import llm_chat_deepseek

from boss_state import get_recent_messages, get_setting
import httpx

OLLAMA_BASE = "http://localhost:11434"


def _needs_human_transfer(hr_message: str, conversation_id: int = 0) -> bool:
    """用本地小模型判断 HR 是否要求转人工 / 跟真人沟通。

    成功返回 True/False，Ollama 不可用时回退到关键词匹配。
    """
    CLASSIFY_PROMPT = (
        '你是一个我的ai求职助手，我正在用ai和hr聊天。你现在需要判断招聘平台的HR发来的消息，'
        '是否包含了「要求与真人/本人沟通」的意图。\n'
        '这包括：要求转人工、想跟真人聊天、要求本人回复、不要AI回复、想和开发者聊天。\n'
        '不包括：普通寒暄（你好/在吗）、询问岗位信息、安排面试、问技术问题。\n'
        '只输出一个单词：yes 或 no，不要有多余的输出。'
    )
    # 第一层：明显关键词，秒判（不调模型）
    obvious = [
        "转人工", "转吧", "本人回复", "本人来", "真人回复", "真人来",
        "找本人", "喊一下", "叫一下", "帮喊", "帮叫", "帮我喊", "帮我叫",
        "通知本人", "叫你主人", "叫你老板", "让开发者", "让求职者",
    ]
    if any(kw in hr_message for kw in obvious):
        return True

    # 第二层：模糊表达，需要结合上下文（调模型）
    ambiguous = [
        "你是AI", "自动回复", "机器人", "不要AI", "不是本人",
        "让本人", "本人呢", "真人在哪", "开发者呢", "求职者呢",
        "真人吗", "AI吗", "自动吗", "机器人吗",
    ]
    if not any(kw in hr_message for kw in ambiguous):
        return False
    # 带上最近 3 条聊天记录作为上下文
    user_content = hr_message
    if conversation_id:
        try:
            history = get_recent_messages(conversation_id, 3)
            if history:
                lines = ["最近的对话记录（用于理解上下文）:"]
                for m in reversed(history):
                    sender = "HR" if m["sender"] == "hr" else "我"
                    content = m["content"]
                    # 己方消息太长会淹没 HR 信号，截短
                    limit = 30 if sender == "我" else 80
                    lines.append(f"  {sender}: {content[:limit]}")
                lines.append(f"\nHR 最新消息: {hr_message}")
                user_content = "\n".join(lines)
        except Exception:
            pass
    print("LLM:", user_content)
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/chat",
            json={
                "model": "qwen2.5:1.5b",
                "messages": [
                    {"role": "system", "content": CLASSIFY_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.0,
                "stream": False,
                "options": {
                    "num_predict": 3,  # 最多输出 3 个 token，阻止长篇推理
                },
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        result = resp.json()["message"]["content"].strip().lower()
        return result.startswith("yes")
    except Exception:
        # Ollama 不可用 → 回退关键词
        keywords = ["转人工", "想跟真人", "要真人", "不要AI", "本人回复"]
        return any(kw in hr_message for kw in keywords)

SYSTEM_PROMPT = """你是一个求职者开发的AI助手，在BOSS直聘上帮他自动与招聘方沟通。

## 核心身份
- 坦诚告诉对方你是AI助手，由求职者本人开发
- 这个AI工具本身就是求职者技术能力的证明
- 如果对方感兴趣，求职者本人会亲自跟进

## 求职者背景（动态适配）
- 下面上下文中的「我的简历摘要」是求职者真实经历的唯一定义——只能从中引用技能和经验
- 回复时用「我对X方向有经验/感兴趣」代替「我做过X」，除非简历明确写到了X
- 如果简历信息不足，多引导对方介绍岗位细节，不要编造经历来填补空白
- 不要硬套一个万能模板：如果对方招的是AI产品经理，就围绕AI产品方向聊；如果招的是大模型开发，就围绕模型/工程方向聊
- 绝不要编造求职者没有的实习经历

## 回复原则
- 2-4句话，自然真诚，不许生硬
- 围绕对方发布的岗位信息（岗位名、公司、JD）来回复
- 主动了解对方岗位的具体要求、技术栈、团队情况
- 回答技术问题时给出专业、具体的内容
- 不承诺薪资、入职时间——"这些可以后续和本人详细聊"
- 不要重复寒暄，不要每一轮都自我介绍

## 面试处理（重要）
- 当HR提到"线下面试""现场面试""到面""面聊""面谈"等要求时，表示接受但说明需要统计地点：
  回复方向："可以接受线下面试。我先帮您登记一下面试地点，求职者本人会整理好线下面试的安排，有什么需要了解的我可以继续帮您沟通~"
- 绝对不要直接替求职者确定面试时间和具体日期
- 当HR只是问"方便面试吗""什么时候有空"时，按以下方式处理：
  - 如果HR提到了线下/实地/到场等：按上方线下面试话术回复
  - 如果没有提到线下：引导加微信 "方便的话可以先加微信聊聊，让求职者本人跟您沟通会更清楚，面试的事你们微信上直接定"

## 无法回答的问题（重要）
当HR问到以下你无法决定的事情时，在回复末尾务必加上一句引导转人工的话：
- 线下面试时间/地点、线上视频面试安排
- 到岗时间、入职日期、最早什么时候能来
- 薪资期望、薪资范围、最低能接受多少
- 是否接受加班/出差/外包/驻场
- 以及其他需要求职者本人决策的问题

引导话术示例（根据语境选一句，不要机械重复）：
- "如果需要的话，您可以发送'转人工'，求职者本人会马上来处理。"
- "您方便的话发一下'转人工'，本人就能立刻收到消息赶来回复您。"
- "这些得本人来定，您发个'转人工'他就收到通知了，会很快回复的。"

## 触发发送规则（重要）
系统会根据HR的消息内容自动执行以下操作，你只需要在回复中适当提及即可：

### 简历发送
- 当HR明确要求"发简历""看看简历""CV""作品集"时，系统会自动通过BOSS官方「发简历」按钮发送附件简历
- 你只需要回复"已通过BOSS把简历发给您了，请查收"即可
- 绝对不要说"我这边不存储简历""没有简历文件"之类的话

### 微信交换
- 当HR说"加微信""微信聊""加个v""换微信"时，系统会自动通过BOSS官方「换微信」按钮分享求职者微信
- 你只需要回复"我把联系方式通过BOSS发您了"这类话即可
- 绝对不要在文字回复里出现"微信""WeChat""VX""微信号"这些词，BOSS会过滤掉整条消息

### 电话交换
- 当HR说"电话""手机号"时，系统会自动通过BOSS官方「换电话」按钮分享求职者电话
- 你只需要回复"我把电话通过BOSS发您了"即可

### 重要提醒
- 不要在HR没有要求的情况下主动说"已发送"
- 不要重复说"已发送"，如果之前已经发过，就不再提
- 这些操作会在你回复之前执行，所以你说"已发送"时东西确实已经发出去了

## 输出格式（严格JSON）
{"reply": "你的回复内容", "interest": "high/medium/low"}

interest 评估标准（根据完整对话判断HR当前兴趣程度）：
- high: HR问了技术细节、项目经历、面试时间、薪资期望、要了微信、表达了明确合作意向
- medium: HR配合沟通、说"方便""可以""好的""聊聊"、发了JD、问了基本情况
- low: 简单打招呼、摸底试探、回复敷衍、未表现出进一步了解的意愿"""


def _encode_wechat(wechat_id: str) -> str:
    """把微信号编码，绕开 BOSS 直聘的聊天内容过滤。"""
    if not wechat_id:
        return ""
    result = wechat_id
    result = result.replace("--", "一一")
    result = result.replace("-", "一")
    return result


def build_reply_context(
    conversation_id: int, hr_message: str, job_info: dict, resume_summary: str, wechat_id: str = ""
) -> str:
    parts = []

    parts.append(f"招聘方公司: {job_info.get('company', '未知')}")
    parts.append(f"应聘岗位: {job_info.get('title', '未知')}")

    job_desc = job_info.get("description", "")
    if job_desc:
        parts.append(f"岗位描述: {job_desc[:500]}")

    if resume_summary:
        parts.append(f"我的简历摘要: {resume_summary}")

    if wechat_id:
        encoded = _encode_wechat(wechat_id)
        parts.append(f"求职者微信: {wechat_id}（BOSS会过滤微信号，实际发送时请用编码形式: {encoded}，不要发原始形式）")
    else:
        parts.append("求职者微信: 未设置")

    msgs = get_recent_messages(conversation_id, 5)
    if msgs:
        parts.append("\n最近的对话记录:")
        for m in reversed(msgs):
            sender_label = "HR" if m["sender"] == "hr" else "我"
            ai_tag = " [AI代发]" if m.get("ai_generated") else ""
            parts.append(f"  {sender_label}{ai_tag}: {m['content'][:200]}")

    parts.append(f"\nHR刚刚说: {hr_message}")
    parts.append('\n请以JSON格式输出回复和兴趣度: {"reply": "...", "interest": "high/medium/low"}')

    return "\n".join(parts)


def generate_reply(
    conversation_id: int,
    hr_message: str,
    job_info: dict,
    style: str = "professional",
    resume_summary: str = "",
    wechat_id: str = "",
) -> dict:
    """
    根据 HR 消息生成 AI 回复和兴趣度评估。
    返回 {"reply": str, "interest": str, "transfer": bool}，失败时返回 {"reply": "", "interest": "", "transfer": False}.
    """
    if not hr_message or len(hr_message.strip()) < 1:
        return {"reply": "", "interest": "", "transfer": False}

    hr_lower = hr_message.strip().lower()
    if hr_lower in ("你好", "您好", "hi", "hello", "嗨", "在吗", "在吗？", "在不在", "在不在？"):
        company = job_info.get("company", "贵公司")
        title = job_info.get("title", "相关岗位")
        desc_hint = ""
        if job_info.get("description"):
            desc_hint = f"，看了JD感觉挺对口的"
        return {
            "reply": f"您好！看到贵司在招{title}，挺感兴趣的{desc_hint}。PS：正在和你聊的这个AI是我自己开发的，正在不断优化当中。您可以和它聊聊看，如果您觉得它还有什么不好的地方，或者有什么事情想和我聊聊，可以对他说转人工，我就能收到提示啦~",
            "interest": "low",
            "transfer": False,
        }
    if _needs_human_transfer(hr_message, conversation_id):
        return {
            "reply": "好的，已经为您发送转人工提醒，但是在我看到消息并且回复您之前，请让它代替我陪您聊天~",
            "interest": "low",
            "transfer": True,
        }
    try:
        context = build_reply_context(conversation_id, hr_message, job_info, resume_summary, wechat_id)

        style_hint = {
            "professional": "语气正式专业",
            "casual": "语气轻松友好",
            "enthusiastic": "语气热情积极",
        }.get(style, "语气正式专业")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + f"\n\n本次回复风格: {style_hint}"},
            {"role": "user", "content": context},
        ]

        raw = llm_chat_deepseek(messages, temperature=0.6)
        raw = raw.strip().strip('"').strip("'").strip()

        reply = ""
        interest = ""
        try:
            parsed = json.loads(raw)
            reply = (parsed.get("reply") or parsed.get("content") or "").strip()
            interest = (parsed.get("interest") or parsed.get("level") or "").strip().lower()
        except json.JSONDecodeError:
            import re
            m = re.search(r'"reply"\s*:\s*"([^"]*)"', raw)
            if m:
                reply = m.group(1).strip()
            m2 = re.search(r'"interest"\s*:\s*"(\w+)"', raw)
            if m2:
                interest = m2.group(1).strip().lower()

        if interest not in ("high", "medium", "low"):
            interest = ""

        if not reply or len(reply) < 2:
            if not reply:
                reply = raw
            if len(reply) < 2:
                return {"reply": "", "interest": "", "transfer": False}

        if len(reply) > 300:
            reply = reply[:300] + "..."

        refusal_patterns = [
            "无法提供",
            "无法回答",
            "不能回答",
            "无法帮助",
            "爱莫能助",
            "as an AI, I cannot",
            "I cannot provide",
        ]
        for pattern in refusal_patterns:
            if pattern.lower() in reply.lower():
                return {"reply": "", "interest": "", "transfer": False}

        return {"reply": reply, "interest": interest, "transfer": False}

    except Exception as e:
        print(f"  ⚠️ generate_reply error: {e}")
        return {"reply": "", "interest": "", "transfer": False}


def generate_greeting(
    job_title: str, company: str, template: str = "", style: str = "professional", hr_name: str = ""
) -> str:
    if not template:
        template = get_setting(
            "greeting_template",
            "您好，我对贵公司的{job_title}岗位很感兴趣，请问可以详细了解一下吗？",
        )

    greeting = (
        template.replace("{hr_name}", hr_name or "").replace("{job_title}", job_title).replace("{company}", company)
    )

    if "{job_title}" in greeting or "{company}" in greeting:
        greeting = f"您好，我对贵公司的{job_title}岗位很感兴趣，请问可以详细了解一下吗？"

    if hr_name and not greeting.startswith(hr_name):
        # 招呼语开头没称呼时，把 hr_name 当称呼加到最前
        greeting = f"{hr_name}您好，" + greeting

    return greeting


GREETING_SYSTEM_PROMPT = """你是求职者的写作助理，帮他生成BOSS直聘上的第一句打招呼语。

核心原则：你是在帮一个真实的人写招呼语，不是在扮演他。他的真实经历来自下面的简历摘要，JD是目标岗位的要求。你的任务是把两者之间的匹配点用自然的口语表达出来。

硬性规则：
- 只能声称简历摘要中明确写到的技能和经历，禁止编造任何简历中没有的实习/项目/技能
- 所有说的实习经历必须要依据简历回答
- 表达方式从"我会X"改为"我在X方面有经验"或"我对X方向感兴趣"——不确定的用兴趣表达，确定的用经验表达
- 1-2句话，口语自然，不要客套话堆砌
- 不要夸张吹捧，不要列技能清单，不要说"贵公司"，用"咱们/你们"更自然
- 不出现微信/电话/QQ等联系方式（BOSS会拦截整条消息）
- 末尾可以带一句："顺便说下，正在跟你聊的这个自动回复是我自己开发的AI，算我的技术名片"——仅当岗位与AI/开发/技术相关时才加
- 只输出招呼语正文，不要任何解释、不要引号、不要JSON"""


def generate_greeting_ai(
    job_title: str,
    company: str,
    hr_name: str = "",
    job_desc: str = "",
    is_boss: bool = False,
    style: str = "professional",
    resume_summary: str = "",
    optimize_hints: str = "",
    timeout: float = 15.0,
) -> str:
    """用 LLM 生成个性化打招呼语；任何失败都回退到模板版 generate_greeting。

    依据 JD、是否老板、简历摘要定制。AI 不可用时无感降级。
    模式：
      - greeting_mode == "smart"：按用户在前端「智能」选项下保存的 smart_greeting_prompt
        规则化生成（方向 + 3痛点 + 效果付费话术），结果中若占位符残留则回退。
      - 其他 / 关闭 AI 招呼：使用通用自然风格。
    """
    # 设置里可关闭 AI 招呼
    ai_greeting_on = get_setting("ai_greeting_enabled", "true")
    greeting_mode_dbg = get_setting("greeting_mode", "template")
    print(
        f"[greeting] job={job_title!r} company={company!r} hr={hr_name!r} "
        f"ai_enabled={ai_greeting_on!r} mode={greeting_mode_dbg!r} "
        f"has_desc={bool(job_desc)} desc_len={len(job_desc or '')}"
    )
    #判断是否启用智能模板
    if ai_greeting_on != "true":
        print(f"[greeting] → 模板 (ai_greeting_enabled={ai_greeting_on!r})")
        return generate_greeting(job_title, company, style=style, hr_name=hr_name)

    if not job_title and not company:
        print("[greeting] → 模板 (缺少 job_title 和 company)")
        return generate_greeting(job_title, company, style=style, hr_name=hr_name)

    try:
        greeting_mode = greeting_mode_dbg
        style_hint = {
            "professional": "语气正式专业",
            "casual": "语气轻松友好",
            "enthusiastic": "语气热情积极",
        }.get(style, "语气正式专业")

        if greeting_mode == "smart":
            system_prompt, user_prompt = _build_smart_prompts(
                job_title, company, hr_name, job_desc, is_boss, resume_summary, style_hint, optimize_hints
            )
            print(f"[greeting] →---- smart 模式 ---- prompt 长度 sys={len(system_prompt)} user={len(user_prompt)}")
            print(f"[greeting] system_prompt: {system_prompt}")
            print(f"[greeting] user_prompt: {user_prompt}")
        else:
            system_prompt = GREETING_SYSTEM_PROMPT
            user_prompt = _build_generic_prompt(
                job_title, company, hr_name, job_desc, is_boss, resume_summary, style_hint, optimize_hints
            )
            print(f"[greeting] → ----generic 模式 ----prompt 长度 sys={len(system_prompt)} user={len(user_prompt)}")
            print(f"[greeting] system_prompt: {system_prompt}")
            print(f"[greeting] user_prompt: {user_prompt}")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        print(f"[greeting] → 调用 LLM ...")
        raw = llm_chat_deepseek(messages, temperature=0.3)
        print(f"[greeting] LLM 返回长度={len(raw)},raw:{raw}")
        text = (raw or "").strip().strip('"').strip("'").strip()
        # 去掉模型可能多输出的前缀
        text = re.sub(r"^(招呼语|打招呼语|回复)[:：]\s*", "", text)
        print(f"[greeting] LLM 返回长度={len(text)} 预览={text!r}")

        # 质量校验：太短/太长/含联系方式 → 回退模板
        if not text or len(text) < 6:
            print(f"[greeting] → 模板 (LLM 返回太短 len={len(text)})")
            return generate_greeting(job_title, company, style=style, hr_name=hr_name)
        if len(text) > 220:
            text = text[:220]
        if re.search(r"微信|wechat|vx|\bv信\b|qq|电话|手机号|\d{11}", text, re.I):
            print(f"[greeting] → 模板 (含联系方式被拦截)")
            return generate_greeting(job_title, company, style=style, hr_name=hr_name)

        # smart 模式额外校验：占位符未替换完 → 回退
        if greeting_mode == "smart" and re.search(r"【[^】]*】|\{[^}]*\}", text):
            return generate_greeting(job_title, company, style=style, hr_name=hr_name)

        # 开头补称呼（如果有 hr_name 且没带）
        if hr_name and not text.startswith(hr_name):
            text = f"{hr_name}您好，{text}"
        return text
    except Exception as e:
        print(f"  ⚠️ generate_greeting_ai 回退模板: {e}")
        return generate_greeting(job_title, company, style=style, hr_name=hr_name)

def _build_generic_prompt(
    job_title, company, hr_name, job_desc, is_boss, resume_summary, style_hint, optimize_hints=""
):
    # 先放简历（真实经历），再放 JD（目标岗位），防止 LLM 把 JD 要求当成自己的经历
    parts = []

    parts.append("=== 真实经历（只能从这里引用技能和经验） ===")
    if resume_summary:
        parts.append(resume_summary[:400])
    else:
        parts.append("（未提供简历摘要，请用兴趣/学习方向等方式表达，不要声称具体实习经验）")
    if optimize_hints:
        parts.append(f"简历优化方向（用于发现可强调的已有技能）:\n{optimize_hints[:300]}")

    parts.append("")
    parts.append("=== 岗位JD（目标岗位要求，用于找匹配点） ===")
    parts.extend([
        f"招聘公司: {company or '未知'}",
        f"岗位名称: {job_title or '未知'}",
        f"招聘者称呼: {hr_name or '（未知，可不带称呼）'}",
        f"boss_hint: {'true' if is_boss else 'false'}",
    ])
    if job_desc:
        parts.append(f"岗位职责与要求: {job_desc[:400]}")

    if job_desc and len(job_desc.strip()) >= 20:
        try:
            from boss_rag import similar_jds, build_rag_context
            similar = similar_jds(job_desc, limit=3)
            rag = build_rag_context(similar, "greeting")
            if rag:
                parts.append(f"\n历史相似岗位招呼语参考:\n{rag[:500]}")
        except Exception:
            pass

    parts.append(f"\n本次风格: {style_hint}")
    parts.append("请生成打招呼语正文：")
    return "\n".join(parts)


def _build_smart_prompts(job_title, company, hr_name, job_desc, is_boss, resume_summary, style_hint, optimize_hints=""):
    """smart 模式：消费用户在前端填的 smart_greeting_prompt（规则化）。

    核心改造：简历驱动而非 JD 驱动。先展示真实经历（只能从简历中引用），
    再展示 JD 要求，让 LLM 找交集而非把 JD 要求当作自己的技能。
    """
    # RAG: 检索历史相似JD的招呼语经验
    rag_context = ""
    if job_desc and len(job_desc.strip()) >= 20:
        try:
            from boss_rag import similar_jds, build_rag_context
            similar = similar_jds(job_desc, limit=3)
            rag_context = build_rag_context(similar, "greeting")
        except Exception:
            pass

    user_rules = get_setting("smart_greeting_prompt", "")
    if not user_rules.strip():
        user_rules = (
            "规则：\n"
            "1. 从下方「真实经历」中提取求职者已有的2-3个技能/经验，每个不超过10个字\n"
            "2. 从下方「岗位JD」中提取1个最匹配的方向词\n"
            "3. 格式（不要自己编方向，不要加解释）：\n"
            "您好，我的方向是【从JD提取的方向词】，我在【简历中的技能1】、【简历中的技能2】方面有实际经验，看到贵司的JD觉得很匹配，方便聊聊吗？"
        )

    # 先放简历（真实经历），再放 JD（目标岗位），物理分隔防止 LLM 混淆
    user_prompt_parts = [
        "=== 真实经历（只能从这里引用技能和经验，禁止编造以下未包含的内容） ===",
    ]
    if resume_summary:
        user_prompt_parts.append(resume_summary[:400])
    else:
        user_prompt_parts.append("（未提供简历摘要，请用兴趣/学习方向等方式表达，不要声称具体实习经验）")
    if optimize_hints:
        user_prompt_parts.append(f"简历优化方向（用于发现可强调的已有技能）:\n{optimize_hints[:300]}")

    user_prompt_parts.append("")
    user_prompt_parts.append("=== 岗位JD（目标岗位要求，用于找匹配方向，不要把这些要求当作自己的技能 ===")
    user_prompt_parts.append(f"招聘公司: {company or '未知'}")
    user_prompt_parts.append(f"岗位名称: {job_title or '未知'}")
    if job_desc:
        user_prompt_parts.append(f"岗位职责与要求: {job_desc[:600]}")
    if rag_context:
        user_prompt_parts.append(f"\n历史相似岗位的招呼语参考（风格参考，不要照抄内容）:\n{rag_context[:500]}")

    user_prompt_parts.append("\n请按上方规则生成一行招呼语：")

    system_prompt = (
        "你是求职者的写作助理，帮他生成BOSS直聘打招呼语。你不是求职者本人，你是在帮他写。\n\n"
        "硬性规则——违反以下任何一条都会导致招呼语被拦截：\n"
        "1. 只能声称「真实经历」中明确写到的技能和经历，一字不差地从简历中引用\n"
        "2. 如果简历中没有写某个实习/项目/技能，绝对不能编造——用「我对X方向感兴趣」代替「我有X经验」\n"
        "3. 「岗位JD」中的要求是目标方向参考，不是你的经历，不要混为一谈\n"
        "4. 不出现微信/电话/QQ等联系方式\n"
        f"5. 称呼：{hr_name or '不带称呼'}\n"
        f"6. 风格：{style_hint}\n"
        "7. 只输出招呼语正文一行，不要任何解释、不要引号、不要JSON\n\n"
        f"=== 用户规则 ===\n{user_rules}\n"
    )

    return system_prompt, "\n".join(user_prompt_parts)
