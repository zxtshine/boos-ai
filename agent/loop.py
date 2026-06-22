"""Agent 主循环 — Plan-then-Execute 模式。

核心流程:
1. LLM 接收目标 + 可用工具 → 输出 JSON 执行计划（一次性规划）
2. Python 执行器按计划顺序调用工具（0 次 LLM 调用）
3. 仅步骤失败/空结果时回调 LLM 修正计划（重规划，最多2次）
4. 全部完成后生成总结（简单任务直接拼接，复杂任务 1 次 LLM）

返回结果中 completion_status 区分三种情况:
  - "completed": 所有计划步骤执行完毕
  - "partial":   步数耗尽但部分工作已完成，可以继续
  - "aborted":   致命错误、连续失败或 LLM 无法生成有效计划
"""

import asyncio
import json
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional

from .tools import summarize_result
from .prompts import (
    PLANNING_SYSTEM_PROMPT,
    USER_GOAL_PROMPT,
    REPLAN_PROMPT,
    SUMMARY_PROMPT,
)

# —— 里程碑工具映射 ——
MILESTONE_TOOLS = {
    "search_jobs": "searched",
    "preview_companies": "companies_previewed",
    "analyze_jd": "analyzed",
    "optimize_resume_for_jd": "resume_optimized",
    "get_chat_suggestion": "chat_suggestion_generated",
    "apply_job": "applied",
    "batch_apply": "batch_applied",
    "generate_reply": "reply_generated",
    "smart_scan": "searched",       # smart_scan 内部含搜索+分析
    "smart_apply": "applied",       # smart_apply 内部含投递
    "prepare_application": "analyzed",
}

# —— 投递相关工具（受 "不投递" 约束限制）——
APPLY_TOOLS = {"apply_job", "batch_apply", "smart_apply"}

# —— 可能返回空结果的工具（空结果时触发重规划）——
SEARCH_TOOLS = {"search_jobs", "preview_companies", "smart_scan"}


class AgentLoop:
    """Plan-then-Execute Agent 主循环。

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
            "steps": int,           # 实际执行步数
            "summary": str,         # 自然语言总结
            "detail": [...],        # 每步: {step, tool, args, result_preview, thought, error?}
            "milestones": {...},    # 各子目标达成情况
            "plan": [...],          # LLM 生成的原始计划
        }
    """

    def __init__(
        self,
        registry,
        llm_chat: Callable,
        goal: str,
        max_steps: int = 12,
        extra_context: str = "",
        on_step: Optional[Callable] = None,
        on_plan: Optional[Callable] = None,
    ):
        self.registry = registry
        self.llm_chat = llm_chat
        self.goal = goal
        self.max_steps = max_steps
        self.extra_context = extra_context
        self.on_step = on_step
        self.on_plan = on_plan
        self.steps: List[dict] = []
        self._milestones: Dict[str, bool] = {v: False for v in MILESTONE_TOOLS.values()}
        self._replan_count = 0
        self._max_replans = 2
        self._plan_data: dict = {}
        self._llm_calls = 0

    def set_llm_chat(self, fn: Callable):
        self.llm_chat = fn

    # ═══════════════════════════════════════
    #  风控间隔
    # ═══════════════════════════════════════

    async def _inter_step_delay(self) -> None:
        """两步工具执行之间加入随机间隔，模拟人类操作节奏。

        搜索/列表类操作: 3~8s（浏览间隔）
        投递/发送类操作: 已在 boss_automation._human_pace() 中处理，这里给更短的 2~5s
        默认: 2~6s
        """
        gap = random.uniform(2.0, 6.0)
        print(f"[Agent]   ⏳ 间隔 {gap:.1f}s...", flush=True)
        await asyncio.sleep(gap)

    async def _error_backoff(self, attempt: int) -> None:
        """工具失败后的退避等待，避免机械重试被风控识别。

        attempt=1: 5~10s  (初次失败，模拟人类困惑/检查)
        attempt=2: 15~25s (再次失败，模拟更长的思考)
        attempt>=3: 30~45s (多次失败，模拟换策略)
        """
        ranges = {1: (5.0, 10.0), 2: (15.0, 25.0)}
        min_s, max_s = ranges.get(attempt, (30.0, 45.0))
        gap = random.uniform(min_s, max_s)
        print(f"[Agent]   ⏳ 错误退避 {gap:.1f}s（第{attempt}次失败）...", flush=True)
        await asyncio.sleep(gap)

    async def _check_automation_cooldown(self) -> None:
        """检查 boss_automation 的风控冷却状态，如正在冷却则等待。"""
        try:
            from .tools import ToolContext
            ctx = ToolContext.get()
            if ctx.automation and hasattr(ctx.automation, 'in_cooldown'):
                if ctx.automation.in_cooldown():
                    remaining = ctx.automation._cooldown_remaining()
                    print(f"[Agent]   🧊 风控冷却中，等待 {remaining:.0f}s...", flush=True)
                    await asyncio.sleep(remaining + random.uniform(1.0, 3.0))
        except Exception:
            pass

    # ═══════════════════════════════════════
    #  主入口
    # ═══════════════════════════════════════

    async def run(self) -> dict:
        """执行 Plan-then-Execute 流程，返回最终结果。"""
        t_start = time.time()

        # ── Phase 1: 规划 ──
        print("[Agent] ⏳ Phase 1: 规划...", flush=True)
        try:
            plan_data = await self._plan()
        except Exception as e:
            print(f"[Agent] ❌ 规划失败: {e}", flush=True)
            return self._build_result("aborted", 0, hint=f"LLM 规划失败: {e}")

        self._plan_data = plan_data
        constraints = plan_data.get("constraints", [])
        plan_steps = plan_data.get("plan", [])
        analysis = plan_data.get("analysis", "")

        print(f"[Agent] 📋 计划: {len(plan_steps)} 步 — {analysis}", flush=True)
        for i, s in enumerate(plan_steps, 1):
            print(f"[Agent]   {i}. {s.get('tool')} — {s.get('reason', '')}", flush=True)

        # 通知计划（WebSocket 推送）
        if self.on_plan:
            try:
                await self.on_plan(plan_data)
            except Exception:
                pass

        if not plan_steps:
            return self._build_result("aborted", 0, hint="LLM 未能生成有效的执行计划")

        # ── Phase 2: 执行 ──
        print("[Agent] ⏳ Phase 2: 执行...", flush=True)
        context: Dict[int, Any] = {}  # step_num → tool_result
        remaining = list(plan_steps)
        step_counter = 0
        consecutive_failures = 0

        while remaining and step_counter < self.max_steps:
            step = remaining.pop(0)
            step_counter += 1

            # 约束检查
            if self._violates_constraints(step.get("tool", ""), constraints):
                step_record = {
                    "step": step_counter,
                    "tool": step.get("tool", ""),
                    "args": step.get("args", {}),
                    "thought": step.get("reason", ""),
                    "result": "⏭️ 已跳过（与用户约束冲突）",
                    "skipped": True,
                }
                self.steps.append(step_record)
                await self._notify(step_record)
                print(f"[Agent]   ⏭️ Step {step_counter} {step.get('tool')} 跳过（约束冲突）", flush=True)
                continue

            # 解析参数中的 $N 引用
            try:
                resolved_args = self._resolve_args(step.get("args", {}), context)
            except Exception as e:
                resolved_args = step.get("args", {})
                print(f"[Agent]   ⚠️ 参数解析失败: {e}", flush=True)

            # 执行工具
            tool_name = step.get("tool", "")
            # 检查风控冷却状态
            await self._check_automation_cooldown()
            print(f"[Agent]   🔧 Step {step_counter} {tool_name}...", flush=True)
            t_tool = time.time()
            result = await self.registry.execute(tool_name, resolved_args)
            print(f"[Agent]   ✅ 完成 ({time.time() - t_tool:.1f}s)", flush=True)

            is_error = isinstance(result, dict) and "error" in result
            is_empty = self._is_empty_result(tool_name, result)

            step_record = {
                "step": step_counter,
                "tool": tool_name,
                "args": resolved_args,
                "thought": step.get("reason", ""),
                "result": summarize_result(result)[:300],
            }

            if is_error:
                step_record["error"] = True
                consecutive_failures += 1
            else:
                consecutive_failures = 0
                milestone = MILESTONE_TOOLS.get(tool_name)
                if milestone:
                    self._milestones[milestone] = True

            self.steps.append(step_record)
            context[step_counter] = result
            await self._notify(step_record)

            # 连续 3 次失败 → 中止
            if consecutive_failures >= 3:
                print("[Agent] ❌ 连续 3 步失败，中止", flush=True)
                return self._build_result("aborted", step_counter,
                    hint="连续 3 个步骤执行失败，请检查浏览器状态和 AI 配置")

            # 失败或空结果 → 仅在还有后续步骤时触发重规划
            needs_replan = (is_error or is_empty) and bool(remaining)
            if needs_replan:
                if self._replan_count < self._max_replans:
                    # 失败后退避：模拟人类遇到问题后的思考和等待
                    await self._error_backoff(consecutive_failures)
                    print(f"[Agent] 🔄 Phase 3: 重规划 ({self._replan_count + 1}/{self._max_replans})...", flush=True)
                    new_steps = await self._replan(
                        context, step_counter, step, result, constraints
                    )
                    self._replan_count += 1
                    if new_steps:
                        remaining = new_steps
                        consecutive_failures = 0
                        print(f"[Agent]   📋 新计划: {len(new_steps)} 步", flush=True)
                        continue
                    else:
                        print("[Agent] ❌ LLM 判断无法继续", flush=True)
                        summary = await self._summarize(context, exhausted=True)
                        return self._build_result("aborted", step_counter, override_summary=summary)
                else:
                    print(f"[Agent] ⚠️ 已达重规划上限 ({self._max_replans}次)，继续执行剩余步骤", flush=True)

            # 两步之间加入随机间隔，避免机械节奏被风控识别
            if remaining and not is_error:
                await self._inter_step_delay()

        # ── Phase 4: 总结 ──
        print("[Agent] ⏳ Phase 4: 总结...", flush=True)

        # 判断完成状态
        if step_counter >= self.max_steps and remaining:
            status = "partial"
            hint = "步数耗尽，部分计划未执行。你可以用更具体的目标继续。"
        else:
            status = "completed"
            hint = None

        summary = await self._summarize(context, exhausted=(status == "partial"))
        elapsed = time.time() - t_start
        print(f"[Agent] 🏁 {status.upper()} — {elapsed:.1f}s, {step_counter}步, LLM调用{self._llm_calls}次", flush=True)

        kwargs = {"override_summary": summary}
        if hint:
            kwargs["hint"] = hint
        return self._build_result(status, step_counter, **kwargs)

    # ═══════════════════════════════════════
    #  Phase 1: 规划
    # ═══════════════════════════════════════

    async def _plan(self) -> dict:
        """调用 LLM 生成 JSON 执行计划。"""
        messages = [
            {
                "role": "system",
                "content": PLANNING_SYSTEM_PROMPT.format(
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
        response = await self._call_llm_safe(messages, temperature=0.2)
        return self._parse_plan_json(response)

    def _parse_plan_json(self, response: str) -> dict:
        """从 LLM 原始输出中提取 JSON 计划。"""
        response = response.strip()

        # 尝试直接解析
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # 去掉 markdown 代码块标记
        cleaned = response
        for prefix in ("```json", "```"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 尝试提取第一个 JSON 对象
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"无法从 LLM 输出中解析 JSON 计划: {response[:300]}")

    # ═══════════════════════════════════════
    #  Phase 3: 重规划
    # ═══════════════════════════════════════

    async def _replan(
        self,
        context: Dict[int, Any],
        failed_step_num: int,
        failed_step: dict,
        failed_result: Any,
        constraints: List[str],
    ) -> Optional[List[dict]]:
        """步骤失败时调用 LLM 生成修正计划。返回 None 表示 LLM 判断无法继续。"""
        completed_steps = self._format_completed_steps()
        error_message = (
            failed_result.get("error", str(failed_result))
            if isinstance(failed_result, dict)
            else str(failed_result)
        )
        context_summary = self._summarize_context(context)

        prompt = REPLAN_PROMPT.format(
            goal=self.goal,
            completed_steps=completed_steps or "（无）",
            failed_step=failed_step_num,
            failed_tool=failed_step.get("tool", "?"),
            failed_args=json.dumps(failed_step.get("args", {}), ensure_ascii=False),
            error_message=error_message[:500],
            tools=self.registry.get_text_description(),
            context_summary=context_summary,
            constraints=json.dumps(constraints, ensure_ascii=False),
        )

        try:
            response = await self._call_llm_safe(
                [{"role": "user", "content": prompt}], temperature=0.2
            )
            data = self._parse_plan_json(response)
            new_plan = data.get("plan", [])

            if not new_plan:
                analysis = data.get("analysis", "")
                print(f"[Agent]   LLM 判断无法继续: {analysis}", flush=True)
                return None

            return new_plan
        except Exception as e:
            print(f"[Agent]   ⚠️ 重规划失败: {e}", flush=True)
            return None

    # ═══════════════════════════════════════
    #  Phase 4: 总结
    # ═══════════════════════════════════════

    async def _summarize(self, context: Dict[int, Any], exhausted: bool = False) -> str:
        """生成执行总结。简单任务直接拼接，复杂任务调用 LLM。"""
        execution_log = self._build_execution_log()

        # ≤3 步的简单任务直接拼接，不调 LLM
        if len(self.steps) <= 3 and not exhausted:
            return self._simple_summary()

        # 复杂任务或步数耗尽：调 LLM 生成自然语言总结
        messages = [
            {
                "role": "user",
                "content": SUMMARY_PROMPT.format(
                    goal=self.goal,
                    execution_log=execution_log,
                ),
            },
        ]
        try:
            return await self._call_llm_safe(messages, temperature=0.3)
        except Exception:
            return self._simple_summary()

    def _simple_summary(self) -> str:
        """不调 LLM，基于执行结果拼接摘要。"""
        parts = []
        for s in self.steps:
            tool = s.get("tool", "")
            result = s.get("result", "")
            if s.get("skipped"):
                parts.append(f"⏭️ {tool}: 已跳过")
            elif s.get("error"):
                parts.append(f"❌ {tool}: {result[:100]}")
            else:
                parts.append(f"✅ {tool}: {result[:150]}")
        return "\n".join(parts) if parts else "Agent 未执行任何操作。"

    # ═══════════════════════════════════════
    #  辅助方法
    # ═══════════════════════════════════════

    def _build_result(self, status: str, steps: int, **kwargs) -> dict:
        result = {
            "completion_status": status,
            "steps": steps,
            "summary": kwargs.get("override_summary", ""),
            "detail": self.steps,
            "milestones": self._milestones,
            "plan": self._plan_data.get("plan", []),
            "analysis": self._plan_data.get("analysis", ""),
            "replans": self._replan_count,
        }
        if hint := kwargs.get("hint"):
            result["hint"] = hint
        return result

    def _build_extra_context(self) -> str:
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

    def _format_completed_steps(self) -> str:
        lines = []
        for s in self.steps:
            status = "❌" if s.get("error") else ("⏭️" if s.get("skipped") else "✅")
            lines.append(
                f"{status} Step {s['step']}: {s.get('tool','?')} → {s.get('result','')[:120]}"
            )
        return "\n".join(lines) if lines else "（无）"

    def _build_execution_log(self) -> str:
        lines = []
        for s in self.steps:
            tool = s.get("tool", "?")
            args = json.dumps(s.get("args", {}), ensure_ascii=False)
            result = s.get("result", "")
            thought = s.get("thought", "")
            status = "❌ 失败" if s.get("error") else ("⏭️ 跳过" if s.get("skipped") else "✅ 成功")
            lines.append(f"Step {s['step']}: {tool}({args}) [{status}]")
            if thought:
                lines.append(f"  理由: {thought}")
            lines.append(f"  结果: {result[:200]}")
        return "\n".join(lines)

    def _summarize_context(self, context: Dict[int, Any]) -> str:
        """将执行上下文压缩为简短文本，供重规划使用。"""
        parts = []
        for step_num, result in sorted(context.items()):
            if isinstance(result, dict):
                if "error" in result:
                    parts.append(f"Step{step_num}: 错误 — {result['error'][:100]}")
                elif "jobs" in result:
                    parts.append(f"Step{step_num}: {result.get('count', len(result['jobs']))}个岗位")
                elif "analyzed" in result:
                    parts.append(f"Step{step_num}: 分析{result.get('analyzed_count',0)}个岗位, 均分{result.get('avg_match_score','?')}")
                elif "conversations" in result:
                    parts.append(f"Step{step_num}: {result.get('count', 0)}个会话")
                elif "companies" in result:
                    parts.append(f"Step{step_num}: {result.get('total_companies', 0)}家公司")
                else:
                    summary = summarize_result(result)[:100]
                    parts.append(f"Step{step_num}: {summary}")
            else:
                parts.append(f"Step{step_num}: {str(result)[:100]}")
        return "\n".join(parts) if parts else "暂无上下文"

    async def _call_llm_safe(self, messages: list, temperature: float = 0.3) -> str:
        """调用 LLM，带指数退避重试（最多 3 次）。"""
        last_err = None
        for attempt in range(3):
            try:
                result = self.llm_chat(messages=messages, temperature=temperature)
                self._llm_calls += 1
                return result
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM 调用失败（重试3次）: {last_err}")

    def _violates_constraints(self, tool_name: str, constraints: List[str]) -> bool:
        """检查工具是否违反用户约束。"""
        if not constraints:
            return False
        if tool_name not in APPLY_TOOLS:
            return False

        constraint_text = " ".join(constraints)
        no_apply_keywords = [
            "不投递", "不要投递", "别投递", "不用投递", "不需投递",
            "不要投", "别投", "不投简历", "不提交简历", "不发送简历",
            "禁止投递", "不自动投递", "先不投", "暂时不投",
            "只找", "只看", "只搜", "仅搜索", "只查", "只浏览",
        ]
        for kw in no_apply_keywords:
            if kw in constraint_text:
                return True
        return False

    def _is_empty_result(self, tool_name: str, result: Any) -> bool:
        """判断工具返回是否为空（应触发重规划）。"""
        if tool_name not in SEARCH_TOOLS:
            return False
        if not isinstance(result, dict):
            return False
        if "error" in result:
            return False  # 错误由 is_error 处理

        empty_checks = [
            result.get("jobs", []) == [] and result.get("count", 0) == 0,
            result.get("companies", []) == [] and result.get("total_companies", 0) == 0,
            result.get("analyzed", []) == [] and result.get("analyzed_count", 0) == 0,
        ]
        return any(empty_checks)

    def _resolve_args(self, args: dict, context: Dict[int, Any]) -> dict:
        """解析参数中的 $N 引用，替换为前步结果的实际值。"""
        resolved = {}
        for key, value in args.items():
            if isinstance(value, str) and value.startswith("$"):
                try:
                    resolved[key] = self._resolve_ref(value, context)
                except Exception:
                    resolved[key] = value  # 解析失败保留原值
            else:
                resolved[key] = value
        return resolved

    def _resolve_ref(self, ref: str, context: Dict[int, Any]) -> Any:
        """解析单个 $N.key1.key2 引用。"""
        # 去掉 $ 前缀，按 . 分割路径
        path = ref[1:].split(".")
        if not path:
            raise ValueError(f"无效引用: {ref}")

        # 第一步必须是步号
        try:
            step_num = int(path[0])
        except ValueError:
            raise ValueError(f"引用第一步必须是步号: {ref}")

        if step_num not in context:
            raise ValueError(f"Step {step_num} 不存在于上下文中")

        current = context[step_num]
        for segment in path[1:]:
            # 支持 dict key 和 list index
            if isinstance(current, dict):
                if segment not in current:
                    raise ValueError(f"键 '{segment}' 不存在于 Step {step_num} 结果中")
                current = current[segment]
            elif isinstance(current, list):
                try:
                    idx = int(segment)
                    current = current[idx]
                except (ValueError, IndexError):
                    raise ValueError(f"索引 '{segment}' 无效")
            else:
                raise ValueError(f"无法从 {type(current).__name__} 中取 '{segment}'")

        return current

    async def _notify(self, step_record: dict):
        """通知每步进展（WebSocket 回调）。"""
        if self.on_step:
            try:
                await self.on_step(step_record)
            except Exception:
                pass
