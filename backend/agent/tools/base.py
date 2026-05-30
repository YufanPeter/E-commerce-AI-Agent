from __future__ import annotations

"""Tool 协议与统一返回契约。

为什么叫 Tool 而不是 Skill：
    当前每个实现都是“一次原子能力调用”（search/静态文本），
    本质就是 Tool。未来如果出现“为完成一个任务需要多个 Tool
    编排 + 决策”的模块，会在 Tool 之上另起一层 Skill（方案 B）。

为什么用 Protocol 而不是 ABC：
    Tool 实现可能来自不同子系统（recommend 包 SearchService、
    clarify 只用静态模板、fallback 完全无 IO），强行继承父类反而别扭。
    Protocol 只约束接口、不绑定继承，更灵活。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent.session import AgentSession


@dataclass(frozen=True)
class ToolResult:
    """所有 Tool 的统一返回契约。

    payload: 结构化数据，必须 JSON 可序列化。前端用它渲染卡片/表格。
    composer_hint: 给 AnswerComposer 的额外提示词片段（如“用对比口吻”），
                   不会直接展示给用户。
    narrative_override: 若设置，composer 跳过 LLM 直接用这段文本作为最终回答。
                        适合 clarify / fallback 这种无需 LLM 加工的固定话术。
    needs_composer: False 时 orchestrator 不会调 composer。
                    与 narrative_override 配合：固定回答场景。
    """

    tool_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    composer_hint: str | None = None
    narrative_override: str | None = None
    needs_composer: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "payload": self.payload,
            "composer_hint": self.composer_hint,
            "narrative_override": self.narrative_override,
            "needs_composer": self.needs_composer,
        }


@runtime_checkable
class Tool(Protocol):
    """Tool 接口契约。"""

    name: str

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        """执行 tool。

        query:   用户原句（或 router 改写后的 query）
        session: 当前会话上下文（含历史 / 偏好 / 工作记忆）
        slots:   router 抽取的额外参数（如 rewritten_query, intent_args）
        """
        ...
