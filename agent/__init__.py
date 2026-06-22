"""Agent 模块 — Plan-then-Execute Agent 核心框架。

提供:
- ToolRegistry: 工具注册表
- ToolContext: 工具运行时上下文（依赖注入）
- register_all: 一键注册所有工具
- AgentLoop: Plan-then-Execute 主循环（规划→执行→重规划→总结）
- 系统 Prompt 模板
"""

from .tools import ToolRegistry, ToolContext, register_all
from .loop import AgentLoop
from .prompts import AGENT_SYSTEM_PROMPT

__all__ = ["ToolRegistry", "ToolContext", "register_all", "AgentLoop", "AGENT_SYSTEM_PROMPT"]
