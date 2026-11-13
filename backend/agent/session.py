from __future__ import annotations

"""Session state combining long-term preferences, history, and working memory.

V1 memory layers:

    Preference Core Memory
        - One record per user ID, retained across sessions.
        - Loaded by the API into ``user_profile``; this class does not persist it.

    Working memory
        - ``task``: the typed ``ShoppingTask`` every tool reads and updates.
        - ``working_memory``: the rolling session summary, plus ``task`` when persisted.

    Session memory
        - Raw conversation messages retained in a sliding window.
        - Gives the router enough context to resolve follow-up references.
        - Older messages are compressed into a lightweight session-only summary.

Phase 1 assumes one FastAPI worker and serialized calls per session.
"""

from dataclasses import asdict, dataclass, field, fields
from typing import Any, TypedDict
from uuid import uuid4


class HitRef(TypedDict):
    """Compact product reference retained from an earlier turn."""

    product_id: str
    title: str


@dataclass
class ActiveView:
    """The ordered list the shopper is looking at, which "the second one" counts through."""

    kind: str = "search"  # search, candidates, compare, cart or orders
    items: list[HitRef] = field(default_factory=list)
    version: int = 0


@dataclass
class PendingAction:
    """A clarifying question waiting for the shopper's answer."""

    kind: str  # cart_sku, cart_pick, detail_pick or compare_pick
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ShoppingTask:
    """Everything one conversation is in the middle of.

    Tools change this state through the methods below, so a new list cannot leave a
    focus, comparison or question behind that belongs to the previous one.
    """

    constraints: Any = None  # ParsedQuery.to_dict() of the latest search
    active_view: ActiveView = field(default_factory=ActiveView)
    shopping_items: list[HitRef] = field(default_factory=list)  # kept while the cart is on screen
    focus_product_id: str | None = None
    pending: PendingAction | None = None
    comparison: dict[str, Any] | None = None
    last_added: dict[str, Any] | None = None
    seen_product_ids: list[str] = field(default_factory=list)
    multi_search: bool = False

    def show(self, kind: str, items: list[HitRef]) -> None:
        """Make a new list current and drop whatever referred to the old one."""
        view = self.active_view
        if kind == "cart":
            if view.kind != "cart":
                self.shopping_items = view.items
            self.active_view = ActiveView(kind, list(items), view.version + 1)
            return
        self.active_view = ActiveView(kind, list(items), view.version + 1)
        self.shopping_items = []
        if self.focus_product_id not in {item.get("product_id") for item in items}:
            self.focus_product_id = None
        self.pending = None
        self.last_added = None
        if kind != "compare":
            self.comparison = None

    def add_targets(self) -> list[HitRef]:
        """Products "add the second one" counts through: never the cart's own lines."""
        return self.shopping_items if self.active_view.kind == "cart" else self.active_view.items

    def focus_on(self, product_id: str | None) -> None:
        self.focus_product_id = product_id

    def ask(self, kind: str, data: dict[str, Any]) -> None:
        self.pending = PendingAction(kind, data)

    def pending_data(self, *kinds: str) -> dict[str, Any] | None:
        """The waiting question's data when it is one of ``kinds``."""
        return self.pending.data if self.pending is not None and self.pending.kind in kinds else None

    def end_pending(self, *kinds: str) -> None:
        """Close the waiting question, or only a question of one of ``kinds``."""
        if self.pending is not None and (not kinds or self.pending.kind in kinds):
            self.pending = None

    def added_to_cart(self, product_id: str, sku_id: str) -> None:
        self.pending = None
        self.comparison = None
        self.focus_product_id = product_id
        self.last_added = {"product_id": product_id, "sku_id": sku_id}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "ShoppingTask":
        if not isinstance(data, dict):
            return cls()
        values = {key: value for key, value in data.items() if key in {f.name for f in fields(cls)}}
        try:
            view = values.pop("active_view", None) or {}
            pending = values.pop("pending", None)
            return cls(active_view=ActiveView(**view), pending=PendingAction(**pending) if pending else None, **values)
        except TypeError:  # A record from an older shape starts a fresh task.
            return cls()


# Retain the latest N user/assistant messages to bound context-token growth.
DEFAULT_HISTORY_WINDOW = 10


@dataclass
class Message:
    """Conversation message using the standard chat role convention."""

    role: str          # "user" / "assistant" / "system"
    content: str


@dataclass
class AgentSession:
    """State for one user conversation."""

    session_id: str = field(default_factory=lambda: uuid4().hex)
    user_id: str | None = None
    user_profile: dict[str, Any] = field(default_factory=dict)
    history: list[Message] = field(default_factory=list)
    # Session summary and other plain values; the shopping state lives in ``task``.
    working_memory: dict[str, Any] = field(default_factory=dict)
    history_window: int = DEFAULT_HISTORY_WINDOW
    # Products the shopper bought, added or opened lately, across conversations. Loaded per
    # request from the event log like ``user_profile`` and never saved with the session.
    product_trail: list[dict[str, Any]] = field(default_factory=list)
    # Searches the shopper made lately, loaded and dropped the same way.
    search_trail: list[dict[str, Any]] = field(default_factory=list)
    # What this message just taught the long-term memory, in words the reply can repeat
    # ("you don't buy MAYBELLINE"). Set per request, never saved.
    turn_memory: list[str] = field(default_factory=list)
    task: ShoppingTask = field(init=False)

    def __post_init__(self) -> None:
        self.task = ShoppingTask.from_dict(self.working_memory.pop("task", None))

    def persisted_memory(self) -> dict[str, Any]:
        """Working memory in the JSON shape the session store saves."""
        return {**self.working_memory, "task": self.task.to_dict()}

    def add_user(self, content: str) -> None:
        self._append(Message(role="user", content=content))

    def add_assistant(self, content: str) -> None:
        self._append(Message(role="assistant", content=content))

    def _append(self, msg: Message) -> None:
        self.history.append(msg)
        # This class retains user/assistant messages only; system messages live elsewhere.
        if len(self.history) > self.history_window:
            overflow = self.history[: len(self.history) - self.history_window]
            self._update_session_summary(overflow)
            self.history = self.history[-self.history_window:]

    def history_for_router(self) -> list["Message"]:
        """路由器可见的上下文：除本轮刚 add_user 的那条之外的历史消息。

        orchestrator 在路由前已经 add_user(query)，所以"只有一条且是本轮 user"
        等价于首轮——这时路由结果只取决于 query，可安全缓存。"""
        if self.working_memory.get("session_summary"):
            return list(self.history) or [Message(role="system", content="summary")]
        if len(self.history) == 1 and self.history[0].role == "user":
            return []
        return list(self.history)

    def recent_text(self, n: int = 4) -> str:
        """Return recent text for router and composer context."""
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
        # Use a deterministic summary to avoid adding an LLM failure point to persistence.
        self.working_memory["session_summary"] = combined[-500:]
        self.working_memory["summary_updated_at_turn"] = (
            int(self.working_memory.get("summary_updated_at_turn") or 0) + len(overflow)
        )

    def remember_search(
        self,
        parsed_dict: dict[str, Any],
        hits: list[HitRef],
        keep_seen: bool = False,
    ) -> None:
        """recommend 每次成功检索后调用：记下本轮结构化意图和结果列表。

        keep_seen: "看更多"传 True，在同一批搜索里累计已展示商品；其他检索都算新的一批。"""
        task = self.task
        seen = list(task.seen_product_ids) if keep_seen else []
        for hit in hits:
            product_id = hit.get("product_id")
            if product_id and product_id not in seen:
                seen.append(product_id)
        task.show("search", hits)
        task.constraints = parsed_dict
        task.multi_search = False
        task.seen_product_ids = seen

    def recall_seen(self) -> list[str]:
        """当前这批搜索里已经展示过的商品 ID，"看更多"靠它去重。"""
        return self.task.seen_product_ids

    def recall_parsed(self) -> Any:
        """Return the previous structured retrieval intent, if any."""
        return self.task.constraints

    def recall_hits(self) -> list[HitRef]:
        """Return the products currently on screen, in display order."""
        return self.task.active_view.items
