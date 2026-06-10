from __future__ import annotations

"""会话状态：长期 Preference 挂载 + session history + working memory。

V1 记忆分层：

    Preference Core Memory
        - 每个 user_id 一份，跨 session 留存
        - 由 API 层加载后挂到 user_profile，本类不负责持久化长期偏好

    工作记忆 (working_memory)
        - 当前 turn 内 tool/composer 都能读写的临时字典
        - 典型字段：last_parsed_query（RefineTool 用）、last_hits

    Session 记忆 (history + session_summary)
        - 当前 session 的对话原文，滑动窗口截断
        - 让 router 知道"再便宜点"指什么、"对比第一个和第二个"指哪些商品
        - 超出窗口的旧消息压成轻量 session_summary，只服务当前 session

注意：Phase 1 不做并发安全；FastAPI 单 worker / 每 session 串行调用足够。
"""

from dataclasses import dataclass, field
from typing import Any, TypedDict
from uuid import uuid4


# ---------------------------------------------------------------------------
# 工作记忆的类型契约
# ---------------------------------------------------------------------------
#
# working_memory 之前是个裸 dict[str, Any]，键名只在各 tool 的注释里口口相传，
# 改一个键名就会静默断链（refine 读不到 recommend 写的东西）。这里用 TypedDict
# 把"跨 turn 复用的状态"固化成一份**单一可信契约**：键名、类型、含义集中可查，
# IDE / 类型检查器也能在写错键时报警。
#
# 注意：TypedDict 只是"类型视图"，运行时仍是普通 dict——保持与现有 set/get
# 完全兼容，不引入任何新依赖（这正是选"轻量 State"而非 LangGraph 的原因）。


class HitRef(TypedDict):
    """上一轮返回给用户的商品引用（精简，仅够 refine/compare/detail 回指）。"""

    product_id: str
    title: str


class WorkingMemory(TypedDict, total=False):
    """跨 turn 复用的工作记忆。所有键都可选（total=False）。

    canonical keys：
        last_parsed_query: 上一轮**结构化**检索意图（ParsedQuery.to_dict()）。
                           refine 在它之上做无损约束叠加——这正是"跑鞋→Adidas"
                           不丢品类的关键。
        last_hits:         上一轮返回的商品引用列表，供 compare/product_detail
                           把"第一个/这款"映射回 product_id。
    """

    last_parsed_query: dict[str, Any]
    last_hits: list[HitRef]
    session_summary: str
    summary_updated_at_turn: int


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
    user_id: str | None = None
    user_profile: dict[str, Any] = field(default_factory=dict)
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
            overflow = self.history[: len(self.history) - self.history_window]
            self._update_session_summary(overflow)
            self.history = self.history[-self.history_window:]

    def recent_text(self, n: int = 4) -> str:
        """取最近 n 轮文本，给 router / composer 当上下文用。"""
        recent = self.history[-n:]
        lines = []
        summary = self.working_memory.get("session_summary")
        if summary:
            lines.append(f"会话摘要: {summary}")
        for m in recent:
            prefix = "用户" if m.role == "user" else "助手"
            lines.append(f"{prefix}: {m.content}")
        return "\n".join(lines)

    def _update_session_summary(self, overflow: list[Message]) -> None:
        if not overflow:
            return
        existing = str(self.working_memory.get("session_summary") or "").strip()
        snippets = []
        for msg in overflow:
            prefix = "用户" if msg.role == "user" else "助手"
            content = " ".join((msg.content or "").split())
            if content:
                snippets.append(f"{prefix}:{content[:80]}")
        combined = "；".join([s for s in [existing, *snippets] if s])
        # 轻量确定性摘要：不调用 LLM，避免 session 持久化路径引入额外失败点。
        self.working_memory["session_summary"] = combined[-500:]
        self.working_memory["summary_updated_at_turn"] = (
            int(self.working_memory.get("summary_updated_at_turn") or 0) + len(overflow)
        )

    def set(self, key: str, value: Any) -> None:
        self.working_memory[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.working_memory.get(key, default)

    # ---- 工作记忆的类型化读写（围绕 WorkingMemory 契约，避免裸键名手滑）----

    def remember_search(self, parsed_dict: dict[str, Any], hits: list[HitRef]) -> None:
        """recommend 每次成功检索后调用：把本轮结构化意图 + 命中写进工作记忆，
        供下一轮 refine/compare/product_detail 复用。"""
        self.working_memory["last_parsed_query"] = parsed_dict
        self.working_memory["last_hits"] = hits

    def recall_parsed(self) -> dict[str, Any] | None:
        """取上一轮结构化检索意图（ParsedQuery.to_dict()），没有则 None。"""
        return self.working_memory.get("last_parsed_query")

    def recall_hits(self) -> list[HitRef]:
        """取上一轮命中的商品引用，没有则空列表。"""
        return self.working_memory.get("last_hits") or []
