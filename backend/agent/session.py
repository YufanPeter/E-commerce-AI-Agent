from __future__ import annotations

"""会话状态：对话历史 + 工作记忆。

三层记忆的最小实现（Phase 1 只做短期 + 工作记忆，长期记忆留到 Phase 5）：

    工作记忆 (working_memory)
        - 当前 turn 内 tool/composer 都能读写的临时字典
        - 典型字段：last_parsed_query（RefineTool 用）、last_hits
        - 不持久化，每个 turn 开始时由 orchestrator 重置 turn 级字段

    短期记忆 (history)
        - 当前 session 的对话原文，滑动窗口截断
        - 让 router 知道"再便宜点"指什么、"对比第一个和第二个"指哪些商品

    长期记忆（未实现）
        - 跨 session 的用户偏好，存 SQLite，Phase 5 接入

注意：Phase 1 不做并发安全；FastAPI 单 worker / 每 session 串行调用足够。
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


# 历史滑窗大小：保留最近 N 条 user/assistant 消息。
# 超过则丢弃最旧的，避免 token 累积。10 条足够覆盖"3 轮往返"+ 系统消息。
DEFAULT_HISTORY_WINDOW = 10


@dataclass
class Message:
    """对话消息。role 与 OpenAI chat 约定一致，方便直接喂给 LLM。"""

    role: str          # "user" / "assistant" / "system"
    content: str


@dataclass
class AgentSession:
    """单个用户会话。"""

    session_id: str = field(default_factory=lambda: uuid4().hex)
    history: list[Message] = field(default_factory=list)
    # 工作记忆：跨 turn 累积的轻量上下文。键名见各 tool 文档。
    # 典型键：last_parsed_query / last_hits / pinned_filters
    working_memory: dict[str, Any] = field(default_factory=dict)
    history_window: int = DEFAULT_HISTORY_WINDOW

    def add_user(self, content: str) -> None:
        self._append(Message(role="user", content=content))

    def add_assistant(self, content: str) -> None:
        self._append(Message(role="assistant", content=content))

    def _append(self, msg: Message) -> None:
        self.history.append(msg)
        # 滑窗只在 user/assistant 上生效，system 消息不在这里维护。
        if len(self.history) > self.history_window:
            # 一次截掉一条，保持窗口大小；不做摘要（Phase 2 再加）。
            self.history = self.history[-self.history_window:]

    def recent_text(self, n: int = 4) -> str:
        """取最近 n 轮文本，给 router / composer 当上下文用。"""
        recent = self.history[-n:]
        lines = []
        for m in recent:
            prefix = "用户" if m.role == "user" else "助手"
            lines.append(f"{prefix}: {m.content}")
        return "\n".join(lines)

    def set(self, key: str, value: Any) -> None:
        self.working_memory[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.working_memory.get(key, default)
