"""Agent 主循环 — ReAct (Reasoning + Acting) 模式。

核心流程:
1. LLM 接收目标 + 当前上下文 → 输出 THOUGHT + ACTION
2. 执行 ACTION 指定的工具
3. 观察结果 → 更新上下文 → 回到 1
4. 直到 LLM 输出 FINAL，或步数耗尽

步数耗尽 ≠ 失败。返回结果中 completion_status 区分三种情况:
  - "completed": LLM 主动输出 FINAL，任务达成
  - "partial":  步数耗尽但部分工作已完成，可以继续
  - "aborted":  致命错误或所有工具调用失败
"""

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

from .tools import ToolRegistry, summarize_result
from .prompts import AGENT_SYSTEM_PROMPT, USER_GOAL_PROMPT

# —— 里程碑：Agent 完成了哪些关键动作 ——
MILESTONE_TOOLS = {
    "search_jobs": "searched",
    "preview_companies": "companies_previewed",
    "analyze_jd": "analyzed",
    "optimize_resume_for_jd": "resume_optimized",
    "get_chat_suggestion": "chat_suggestion_generated",
    "apply_job": "applied",
    "batch_apply": "batch_applied",
    "generate_reply": "reply_generated",
}


class AgentLoop:
    """ReAct Agent 主循环。

    用法:
        loop = AgentLoop(
            registry=tools,
            llm_chat=llm_chat_deepseek,
            goal="帮我在广州找3个Python后端实习岗位并投递",
        )
        result = await loop.run()

    返回:
        {
            "completion_status": "completed" | "partial" | "aborted",
            "steps": int,           # 消耗步数
            "summary": str,         # LLM 自然语言总结
            "detail": [...],        # 每步的 thought / tool / args / result
            "milestones": {...},    # 各子目标达成情况
            "resume_context": {...} # partial 时可传回来继续执行
        }
    """

    def __init__(
        self,
        registry: ToolRegistry,
        llm_chat: Callable,
        goal: str,
        max_steps: int = 12,
        extra_context: str = "",
        on_step: Optional[Callable] = None,
    ):
        self.registry = registry
        self.llm_chat = llm_chat
        self.goal = goal
        self.max_steps = max_steps
        self.on_step = on_step  # async callback(step_dict) 用于 WebSocket 推送
        self.steps: List[dict] = []
        self._milestones: Dict[str, bool] = {v: False for v in MILESTONE_TOOLS.values()}
        self._consecutive_failures = 0
        self._messages: list = []  # 保存完整对话历史，供 resume 使用

    # ── 公开属性（外部可能在构造后覆盖）──
    def set_llm_chat(self, fn: Callable):
        self.llm_chat = fn

    # ═══════════════════════════════════════
    #  主循环
    # ═══════════════════════════════════════

    async def run(self) -> dict:
        """执行 Agent 循环，返回最终结果。"""
        self._messages = self._build_initial_messages()
        t_start = time.time()

        for step in range(1, self.max_steps + 1):
            t0 = time.time()
            # 1. 调用 LLM
            print(f"[Agent] ⏳ Step {step}/{self.max_steps} 调用 LLM...", flush=True)
            response = await self._call_llm_safe(self._messages)
            t1 = time.time()
            print(f"[Agent] ✅ LLM 返回 ({t1 - t0:.1f}s)", flush=True)

            # 2. 解析输出
            parsed = self._parse(response)
            step_record = {
                "step": step,
                "thought": parsed.get("thought", ""),
                "action": parsed.get("type", "?"),
            }

            # 3a. LLM 认为任务完成
            if parsed["type"] == "final":
                step_record["summary"] = parsed["content"]
                self.steps.append(step_record)
                await self._notify(step_record)
                print(f"[Agent] 🏁 FINAL — 总耗时 {time.time() - t_start:.1f}s，{step}步", flush=True)
                return self._build_result("completed", step)

            # 3b. LLM 请求更多步数
            if parsed["type"] == "request_more_steps":
                extra = parsed.get("extra_steps", 5)
                granted = min(extra, 8)
                self.max_steps += granted
                self._messages.append({
                    "role": "user",
                    "content": f"已批准 {granted} 个额外步数，现在总共剩余 {self.max_steps - step} 步。请继续。",
                })
                self.steps.append({"step": step, "thought": parsed["thought"], "action": "request_more_steps", "granted": granted})
                continue

            # 3c. 工具调用
            if parsed["type"] == "tool_call":
                tool_name = parsed["tool_name"]
                tool_args = parsed["tool_args"]
                step_record["tool"] = tool_name
                step_record["args"] = tool_args

                print(f"[Agent] 🔧 Step {step} 执行工具: {tool_name}...", flush=True)
                t_tool = time.time()
                result = await self.registry.execute(tool_name, tool_args)
                print(f"[Agent] ✅ 工具完成 ({time.time() - t_tool:.1f}s)", flush=True)

                # 判断工具调用是否成功
                is_error = isinstance(result, dict) and "error" in result
                if is_error:
                    self._consecutive_failures += 1
                    step_record["error"] = True
                else:
                    self._consecutive_failures = 0
                    # 打里程碑
                    milestone = MILESTONE_TOOLS.get(tool_name)
                    if milestone:
                        self._milestones[milestone] = True

                result_text = summarize_result(result)
                step_record["result"] = result_text[:300]
                self.steps.append(step_record)
                await self._notify(step_record)

                # 把结果反馈给 LLM
                self._messages.append({
                    "role": "assistant",
                    "content": f"THOUGHT: {parsed['thought']}\nACTION: {tool_name}({json.dumps(tool_args, ensure_ascii=False)})",
                })
                feedback = f"工具返回结果:\n{result_text}"
                if is_error:
                    feedback += "\n\n这个工具调用失败了。请分析原因并决定：1) 修正参数重试 2) 换一个工具 3) 跳过这一步继续"
                else:
                    feedback += f"\n\n当前步数: {step}/{self.max_steps}。请根据结果决定下一步操作。"
                self._messages.append({"role": "user", "content": feedback})

                # 连续失败 ≥3 次 → 中止
                if self._consecutive_failures >= 3:
                    self._messages.append({
                        "role": "user",
                        "content": "连续 3 次工具调用失败。请输出 FINAL: 说明已完成的进度和失败的原因，不要继续尝试。",
                    })
                continue

            # 3d. 无法解析 → 提醒 LLM
            self._messages.append({
                "role": "assistant",
                "content": response[:500],
            })
            self._messages.append({
                "role": "user",
                "content": (
                    "你的输出格式不符合要求。请按以下格式输出:\n"
                    "THOUGHT: <你的思考>\n"
                    "ACTION: <工具名>({\"参数\": \"值\"})\n"
                    "或\n"
                    "THOUGHT: <你的思考>\n"
                    "FINAL: <任务总结>"
                ),
            })

        # ══ 步数耗尽 ══
        return await self._handle_exhausted()

    # ═══════════════════════════════════════
    #  步数耗尽处理
    # ═══════════════════════════════════════

    async def _handle_exhausted(self) -> dict:
        """步数耗尽时：让 LLM 诚实总结已完成和未完成的部分。"""
        applied = self._milestones.get("applied", False) or self._milestones.get("batch_applied", False)
        searched = self._milestones.get("searched", False)
        analyzed = self._milestones.get("analyzed", False)

        # 什么都没做成 → 直接标记 aborted
        if not searched and not applied and not analyzed:
            return self._build_result("aborted", self.max_steps, hint="Agent 未完成任何有效操作，请检查浏览器状态和 AI 配置后重试。")

        # 有部分进展 → 让 LLM 总结
        context = f"""步数已用完（共 {self.max_steps} 步）。

截至目前的情况:
- 搜索岗位: {'✅ 已完成' if searched else '❌ 未完成'}
- 分析匹配度: {'✅ 已完成' if analyzed else '❌ 未完成'}
- 投递岗位: {'✅ 已完成' if applied else '❌ 未完成'}

请诚实输出 FINAL: 告诉用户：
1. 已经完成了什么
2. 还差什么没做
3. 建议用户用更具体的指令继续（例如 "把剩余5个已分析的岗位投递掉"）"""

        self._messages.append({"role": "user", "content": context})
        try:
            final_resp = await self._call_llm_safe(self._messages)
            parsed = self._parse(final_resp)
            summary = parsed.get("content", final_resp[:500])
        except Exception:
            summary = (
                f"Agent 执行了 {self.max_steps} 步后达到上限。\n"
                f"已完成: 搜索{'✅' if searched else '❌'}、分析{'✅' if analyzed else '❌'}、投递{'✅' if applied else '❌'}。\n"
                f"建议: 用更具体的目标继续执行剩余任务。"
            )

        return self._build_result("partial", self.max_steps, override_summary=summary)

    # ═══════════════════════════════════════
    #  内部方法
    # ═══════════════════════════════════════

    def _build_initial_messages(self) -> list:
        return [
            {
                "role": "system",
                "content": AGENT_SYSTEM_PROMPT.format(
                    tools=self.registry.get_text_description(),
                    extra_context=self._build_extra_context(),
                ),
            },
            {
                "role": "user",
                "content": USER_GOAL_PROMPT.format(
                    goal=self.goal,
                    context=self._build_context() or "暂无额外信息",
                ),
            },
        ]

    def _build_result(self, status: str, steps: int, **kwargs) -> dict:
        """统一构建返回结构。"""
        result = {
            "completion_status": status,
            "steps": steps,
            "summary": kwargs.get("override_summary", self.steps[-1].get("summary", "") if self.steps else ""),
            "detail": self.steps,
            "milestones": self._milestones,
        }
        if status in ("partial", "aborted"):
            result["hint"] = kwargs.get(
                "hint",
                "部分工作已完成。你可以用更具体的目标继续执行，例如 '把已搜索的岗位中匹配度>80的投递掉'。",
            )
        return result

    def _build_extra_context(self) -> str:
        """从 boss_state 拉取额外上下文。"""
        try:
            from boss_state import get_setting, get_today_application_count

            parts = []
            resume = get_setting("resume_summary", "")
            if resume and len(resume) > 5:
                parts.append(f"- 求职者简历摘要: {resume[:300]}")
            city = get_setting("default_city", "全国")
            parts.append(f"- 默认搜索城市: {city}")
            limit = get_setting("daily_apply_limit", "15")
            today = get_today_application_count()
            parts.append(f"- 今日已投递: {today}/{limit}")
            return "\n".join(parts) if parts else "无"
        except Exception:
            return "无"

    def _build_context(self) -> str:
        """构建用户上下文文本。"""
        parts = []
        try:
            from boss_state import get_setting

            resume = get_setting("resume_summary", "")
            if resume and len(resume) > 5:
                parts.append(f"简历: {resume[:500]}")
            wechat = get_setting("wechat_id", "")
            if wechat:
                parts.append(f"微信号: {wechat}")
            location = get_setting("user_location", "")
            if location:
                parts.append(f"所在地: {location}")
        except Exception:
            pass
        return "\n".join(parts) if parts else "暂无额外信息"

    async def _call_llm_safe(self, messages: list) -> str:
        """调用 LLM，带指数退避重试。"""
        last_err = None
        for attempt in range(3):
            try:
                return self.llm_chat(messages=messages, temperature=0.3)
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"LLM 调用失败（重试3次）: {last_err}")

    def _parse(self, response: str) -> dict:
        """解析 LLM 输出的 THOUGHT / ACTION / FINAL / MORE_STEPS。"""
        response = response.strip()

        thought = ""
        thought_m = re.search(r"THOUGHT:\s*(.+?)(?=\nACTION:|\nFINAL:|\nMORE_STEPS:|$)", response, re.DOTALL)
        if thought_m:
            thought = thought_m.group(1).strip()

        # ACTION: tool_name({...}) 或 ACTION: tool_name()
        action_m = re.search(r"ACTION:\s*(\w+)\s*\(\s*(\{.*?\})?\s*\)", response, re.DOTALL)
        if action_m:
            tool_name = action_m.group(1).strip()
            args_str = (action_m.group(2) or "").strip()
            if args_str:
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = self._parse_loose_args(args_str)
            else:
                args = {}
            return {
                "type": "tool_call",
                "thought": thought,
                "tool_name": tool_name,
                "tool_args": args,
            }

        # FINAL: ...
        final_m = re.search(r"FINAL:\s*(.+)", response, re.DOTALL)
        if final_m:
            return {
                "type": "final",
                "thought": thought,
                "content": final_m.group(1).strip(),
            }

        # MORE_STEPS: N — LLM 请求更多步数
        more_m = re.search(r"MORE_STEPS:\s*(\d+)", response)
        if more_m:
            return {
                "type": "request_more_steps",
                "thought": thought,
                "extra_steps": int(more_m.group(1)),
            }

        # 兜底: 不含 ACTION 的文本 → 当作 FINAL
        if "ACTION:" not in response and "MORE_STEPS:" not in response:
            return {
                "type": "final",
                "thought": thought,
                "content": response[:800],
            }

        return {"type": "unknown", "thought": thought, "raw": response[:500]}

    def _parse_loose_args(self, args_str: str) -> dict:
        """宽松解析: key="value" / key=value 格式。"""
        result = {}
        for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', args_str):
            result[m.group(1)] = m.group(2)
        for m in re.finditer(r"(\w+)\s*=\s*(\d+)", args_str):
            if m.group(1) not in result:
                result[m.group(1)] = int(m.group(2))
        for m in re.finditer(r"(\w+)\s*=\s*true", args_str):
            if m.group(1) not in result:
                result[m.group(1)] = True
        return result

    async def _notify(self, step_record: dict):
        """通知每步进展。"""
        if self.on_step:
            try:
                await self.on_step(step_record)
            except Exception:
                pass
