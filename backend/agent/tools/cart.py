from __future__ import annotations

"""CartTool：对话式购物车与下单（"把刚才那款加进来"/"删掉第二个"/"下单吧"）。

这是"高级"档需求：对话式 CRUD + 多轮状态管理 + 业务闭环。

架构：
    intent_router 把购物相关的"管车/下单"意图粗分到 cart，CartTool 再用
    **统一工具调用层**（agent.llm_actions.dispatch_action）把这句话细分到
    具体动作（add/remove/set_quantity/view/checkout）并抽参数。
    动作选定后，用确定性代码操作 CartStore（SQLite），副作用可控可测。

指代消解复用 reference 模块：
    - "把刚才那款/这个加进来" → last_focus_product_id（product_detail 写的）
      或 last_hits[0]
    - "删掉第二个" → 购物车当前明细的第 2 行（注意：是购物车的顺序，
      不是推荐列表的顺序）

降级：LLM 动作分发失败时，退回一个安全默认——展示当前购物车，
      让用户看到状态而不是报错。
"""

import logging
from collections import OrderedDict
import re
from typing import Any

from agent.llm_actions import ActionSpec, dispatch_action
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.resolve import resolve_one
from agent.tools.reference import resolve_indices, resolve_by_name, extract_name_query
from store.cart_store import CartNotFoundError, CartStore, DEFAULT_ADDRESS
from store.product_store import ProductStore


logger = logging.getLogger(__name__)

# 用户在"选规格"环节放弃时的取消词。命中则清掉待选状态、不加购。
_CANCEL_WORDS = ("算了", "不加了", "不要了", "取消", "先不", "不用了", "不买了")


_ACTIONS = [
    ActionSpec(
        name="add",
        description="把某款商品加入购物车（'加入购物车'/'把这个/刚才那款加进来'/'来一件'）",
        parameters={
            "index": {
                "type": "integer",
                "description": "加入上一轮推荐里的第几个，从 1 开始；说'这款/刚才那个'时省略。",
            },
            "quantity": {"type": "integer", "description": "数量，默认 1。"},
        },
    ),
    ActionSpec(
        name="set_quantity",
        description="修改购物车里某件商品的数量（'第一件改成两个'/'多加一件'）",
        parameters={
            "cart_index": {"type": "integer", "description": "购物车里第几件，从 1 开始。"},
            "quantity": {"type": "integer", "description": "改成的目标数量。"},
        },
    ),
    ActionSpec(
        name="remove",
        description="从购物车删除某件商品（'删掉第二个'/'把 XX 去掉'/'不要第一件了'）",
        parameters={
            "cart_index": {"type": "integer", "description": "购物车里第几件，从 1 开始。"},
        },
    ),
    ActionSpec(
        name="view",
        description="查看当前购物车（'看看购物车'/'车里有啥'/'一共多少钱'）",
        parameters={},
    ),
    ActionSpec(
        name="checkout",
        description="提交订单/下单（'下单吧'/'结算'/'就买这些'）",
        parameters={
            "address": {"type": "string", "description": "收货地址；说'用默认的'时省略。"},
        },
    ),
]

_PURPOSE = "管理用户的购物车（加购、改数量、删除、查看）并完成下单结算"


class CartTool:
    name: str = "cart"

    def __init__(
        self,
        cart_store: CartStore | None = None,
        product_store: ProductStore | None = None,
    ) -> None:
        self._store = cart_store or CartStore()
        # 全库商品检索：用户"点名"加购（把小米/oppo 加进来）但当前推荐列表里
        # 没有该商品时，直接去整库按关键词定位，而不是回退到上一轮聚焦的商品。
        self._products = product_store or ProductStore()

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        # router 常把"把小米加进来"verbose 改写成注入了列表里其它商品名的
        # query（如带上 OPPO Reno…），名称/规格定位若用改写句会错配成别的
        # 商品。这里一律用用户原句做"定位"，只把改写句留给动作分类。
        raw_query = self._user_text(session, query)

        # 上一轮在等用户选规格（多 SKU 商品）：这一轮优先当作"规格回答"处理，
        # 而不是再走一次动作分发——否则"暗夜黑 42码"会被误判成新指令。
        pending = session.get("pending_cart")
        if pending:
            return self._resolve_pending_sku(raw_query, session, pending)

        # 上一轮在等用户选"加哪一款"（指代定位不到唯一商品时）：这一轮当作
        # "选商品回答"处理（序号 / 品牌名 / 取消）。
        pending_add = session.get("pending_add")
        if pending_add:
            return self._resolve_pending_add(raw_query, session, pending_add)

        action, args = self._decide(query, session)
        logger.info("cart: action=%s args=%s", action, args)
        try:
            if action == "add":
                return self._add(raw_query, session, args)
            if action == "set_quantity":
                return self._set_quantity(args)
            if action == "remove":
                return self._remove(args)
            if action == "checkout":
                return self._checkout(args)
            return self._view()  # view 及未知动作的安全兜底
        except CartNotFoundError as exc:
            return self._plain(str(exc) + "。要不先看看购物车或换一件？")

    # ------------------------------ 动作分发 ------------------------------

    @staticmethod
    def _user_text(session: AgentSession, fallback: str) -> str:
        """取本轮用户原句（history 里最后一条 user 消息）。

        orchestrator 在路由前已 add_user，所以这里能拿到未被 router 改写的
        原话；用于名称/规格定位，避免被改写句注入的其它商品名误导。"""
        for msg in reversed(session.history):
            if msg.role == "user":
                return msg.content
        return fallback

    def _decide(self, query: str, session: AgentSession) -> tuple[str, dict[str, Any]]:
        """用统一工具调用层选动作；LLM 失败时退回 view（展示状态而非报错）。"""
        try:
            decision = dispatch_action(query, session, _ACTIONS, purpose=_PURPOSE)
            return decision.action, decision.args
        except Exception as exc:  # noqa: BLE001
            logger.warning("cart action dispatch failed, fallback to view: %r", exc)
            return "view", {}

    # ------------------------------ 各动作 ------------------------------

    def _add(self, query: str, session: AgentSession, args: dict[str, Any]) -> ToolResult:
        """定位要加车的商品并进入加购流程。

        定位优先级：显式序号 → 当前列表名称命中 → 全库点名检索 →
        上一轮详情聚焦 → 唯一候选默认；都不满足且候选有多个时列出反问，
        绝不静默抓第一个、也绝不在找不到时回退成无关商品。
        """
        last_hits = session.recall_hits()
        qty = _as_int(args.get("quantity"), default=1)

        # 原句里显式点到的品牌（最可靠的意图信号）。这一步要在「显式序号」之前算：
        # router 常把"把小米加进来"改写成注入了屏幕上其它商品名（如 OPPO Reno）的
        # query，动作分发 LLM 读到改写句后会吐出指向那件商品的 index。若无脑采信这个
        # index，就会把"加小米"错配成"加 OPPO"。所以——原句点了名的品牌优先于 index。
        brand_hits = self._products.match_brands_in_text(query)
        brand_ids = {c.product_id for c in brand_hits}

        # 1) 显式序号：args.index 或 query 里的"第N个"
        idx = args.get("index")
        if idx is None:
            parsed = resolve_indices(query, len(last_hits))
            idx = (parsed[0] + 1) if parsed else None
        if idx is not None:
            i = int(idx) - 1
            if 0 <= i < len(last_hits):
                target_id = last_hits[i]["product_id"]
                # 仅当 index 与原句点名的品牌不矛盾时才采信：原句没点品牌（纯说"第N个"）
                # 或点的就是这件商品 → 用 index；否则判定 index 是改写句注入的幻觉，弃用。
                if not brand_ids or target_id in brand_ids:
                    return self._begin_add(target_id, qty, session, query)

        # 2) 当前推荐列表里名称/品牌命中（快路径：屏幕上就有，直接定位）
        named = resolve_by_name(query, last_hits)
        if len(named) == 1:
            return self._begin_add(last_hits[named[0]]["product_id"], qty, session, query)
        if len(named) > 1:
            # 同品牌多品类（「华为耳机」命中华为耳机+华为手机）→ 先让 LLM 按子品类
            # 语义挑唯一一款，挑不出再列候选反问，避免反复追问。
            subset = [last_hits[i] for i in named]
            picked = resolve_one(query, self._enrich_candidates(subset))
            if picked is not None:
                return self._begin_add(subset[picked]["product_id"], qty, session, query)
            return self._ask_which_product(subset, session)

        # 3) 全库点名检索（text2sql 思路）：先拿原句直接和库里品牌名做子串匹配，
        #    "请你把小米加入购物车"这种带语气前缀也能稳稳命中"小米"；品牌没命中
        #    再退回抽词按 title 搜型号/品类（如"北面冲锋衣"）。覆盖所有品类。
        keyword = extract_name_query(query)
        found = brand_hits
        if not found and keyword:
            found = self._products.search_by_keyword(keyword)
        if len(found) == 1:
            return self._begin_add(found[0].product_id, qty, session, query)
        if len(found) > 1:
            hits = [{"product_id": c.product_id, "title": c.title} for c in found]
            # 把检索结果回写工作记忆，使后续"第N个/某品牌"能接着定位。
            session.set("last_hits", hits)
            return self._ask_which_product(hits, session)

        # 4) 上一轮详情聚焦的商品（"这个加进来"——没有任何点名词时才用）
        focus = session.get("last_focus_product_id")
        if focus and not keyword:
            return self._begin_add(focus, qty, session, query)

        # 5) 用户明确点了名（有 keyword/品牌词）却全库都没找到 → 不要静默回退成
        #    屏幕上那件无关商品，如实告知没找到，避免"加小米却加成 OPPO"。
        if keyword or brand_ids:
            return self._plain(
                f"没有找到和「{keyword or query}」匹配的商品哦，"
                "换个说法或者先让我帮你推荐几款？"
            )

        # 6) 只有一个候选时才默认加；多个候选无法定位 → 反问列出，避免误加
        if len(last_hits) == 1:
            return self._begin_add(last_hits[0]["product_id"], qty, session, query)
        if last_hits:
            return self._ask_which_product(last_hits, session)

        return self._plain(
            "想把哪款加进来呀？可以说「把第一个加入购物车」，"
            "或者先让我推荐几款。"
        )

    def _ask_which_product(
        self, hits: list[dict[str, Any]], session: AgentSession
    ) -> ToolResult:
        """指代定位不到唯一商品时，列出候选让用户选（序号或品牌名）。"""
        candidates = [
            {"product_id": h["product_id"], "title": h.get("title", "")} for h in hits
        ]
        session.set("pending_add", {"candidates": candidates})
        lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(candidates)]
        text = (
            "你想加入哪一款呢？\n"
            + "\n".join(lines)
            + "\n告诉我序号或品牌名就行，例如「第一个」或「华为那款」。"
        )
        return ToolResult(
            tool_name=self.name,
            payload={"action": "ask_which_product", "candidates": candidates},
            narrative_override=text,
            needs_composer=False,
        )

    def _enrich_candidates(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """给候选补上 brand/category/sub_category，供 LLM 语义消歧用依据。

        富化失败（取详情异常）不影响主流程——LLM 仍可只凭 title 消歧。"""
        enriched: list[dict[str, Any]] = []
        for cand in candidates:
            item = dict(cand)
            try:
                detail = self._products.get_product_detail(cand["product_id"])
            except Exception:  # noqa: BLE001
                detail = None
            if detail is not None:
                item.setdefault("title", detail.title)
                item["brand"] = detail.brand
                item["category"] = detail.category
                item["sub_category"] = detail.sub_category
            enriched.append(item)
        return enriched

    def _resolve_pending_add(
        self, query: str, session: AgentSession, pending_add: dict[str, Any]
    ) -> ToolResult:
        """用户对"加哪一款"的回答：定位到唯一商品后进入加购；仍模糊则继续问。"""
        if self._is_cancel(query):
            session.set("pending_add", None)
            return self._plain("好的，先不加了。还有什么我可以帮你的？")

        candidates: list[dict[str, Any]] = pending_add.get("candidates", [])
        if not candidates:
            session.set("pending_add", None)
            return self._plain("想加哪款呢？先让我帮你推荐几款吧～")

        # 用户改点了候选之外的别的品牌商品 → 放弃当前候选，重新定位。
        switched = self._switch_intent(
            query, session, [c["product_id"] for c in candidates]
        )
        if switched is not None:
            return switched

        # 统一分层定位：序号 → 名称唯一 → LLM 语义消歧（「华为耳机」选真无线耳机那款）。
        picked = resolve_one(query, self._enrich_candidates(candidates))
        if picked is None:
            # 仍定位不到：保留候选，再问一次
            return self._ask_which_product(candidates, session)

        session.set("pending_add", None)
        return self._begin_add(candidates[picked]["product_id"], 1, session, query)

    def _switch_intent(
        self, query: str, session: AgentSession, current_ids: list[str]
    ) -> ToolResult | None:
        """检测用户是否在 pending（选规格/选商品）中途改点了别的品牌商品。

        判据：原句里直接出现某个【品牌】名（子串匹配，不抽词），且命中的商品
        不在当前 pending 商品集合里。规格值如 256GB/宇宙橙不等于任何品牌名，
        不会误判成换商品。命中则清掉 pending 重新定位；否则返回 None 继续 pending。"""
        # 直接拿原句和库里品牌名做子串匹配，"请你把小米加入"这类带语气前缀也稳。
        found = self._products.match_brands_in_text(query)
        if not found:
            return None
        found_ids = {c.product_id for c in found}
        if found_ids & set(current_ids):
            return None  # 说的还是当前商品（同品牌）→ 不算换
        session.set("pending_cart", None)
        session.set("pending_add", None)
        return self._add(query, session, {})

    def _begin_add(
        self, product_id: str, qty: int, session: AgentSession, query: str
    ) -> ToolResult:
        """加购入口：单规格直接加；多规格先尝试从这句话解析规格，
        仍无法唯一确定则记录待选状态并反问用户。"""
        skus = self._store.list_skus(product_id)
        if not skus:
            return self._plain("这款暂时没有可购买的规格了，换一款看看？")

        if len(skus) == 1:
            return self._commit_add(product_id, skus[0]["sku_id"], qty)

        # 多规格：先看用户这句话里是否已经点明了规格（如"暗夜黑 42码"）
        matched = self._filter_skus(query, skus)
        if len(matched) == 1:
            return self._commit_add(product_id, matched[0]["sku_id"], qty)

        # 仍不能唯一确定 → 记录待选状态，按维度反问。
        # spec_text 累积用户已经说过的规格词，供后续逐维度追问时叠加过滤。
        title = self._store.product_title(product_id)
        session.set("pending_cart", {
            "product_id": product_id,
            "title": title,
            "quantity": qty,
            "spec_text": query,
        })
        return self._ask_spec(product_id, title, matched if matched else skus)

    def _resolve_pending_sku(
        self, query: str, session: AgentSession, pending: dict[str, Any]
    ) -> ToolResult:
        """用户对"选规格"的回答：解析出唯一 SKU 后加购；仍模糊则继续问。"""
        if self._is_cancel(query):
            session.set("pending_cart", None)
            return self._plain("好的，先不加了。还有什么我可以帮你的？")

        # 用户在选规格中途改点了别的品牌商品（"把oppo加入"）→ 放弃当前 pending，
        # 重新定位，而不是把这句话硬当成给旧商品选规格。
        switched = self._switch_intent(query, session, [pending["product_id"]])
        if switched is not None:
            return switched

        product_id = pending["product_id"]
        qty = _as_int(pending.get("quantity"), default=1)
        skus = self._store.list_skus(product_id)
        if not skus:
            session.set("pending_cart", None)
            return self._plain("这款规格信息好像丢失了，麻烦重新说一次要加哪款～")

        # 叠加历史已说过的规格 + 本轮回答，一起过滤（逐维度收敛的关键）。
        combined = f"{pending.get('spec_text', '')} {query}".strip()
        matched = self._filter_skus(combined, skus)
        if len(matched) != 1:
            picked = self._match_ordinal(query, matched if matched else skus)
            if picked is not None:
                matched = [picked]

        if len(matched) == 1:
            session.set("pending_cart", None)
            return self._commit_add(product_id, matched[0]["sku_id"], qty)

        # 还没缩到唯一：记住累积规格，基于已过滤集合继续追问剩余维度
        pending = {**pending, "spec_text": combined}
        session.set("pending_cart", pending)
        return self._ask_spec(product_id, pending["title"], matched if matched else skus)

    def _commit_add(self, product_id: str, sku_id: str, qty: int) -> ToolResult:
        """确定 SKU 后真正写入购物车并产出确认结果。

        加购是确定性操作，无需 LLM 再润色一段话——直接给简短确认，
        既省一次 LLM 调用、也避免"选完规格还要等一段废话"的体验。"""
        line = self._store.add_product(product_id, sku_id=sku_id, quantity=qty)
        snapshot = self._snapshot()
        spec = "、".join(f"{k}{v}" for k, v in line.options.items()) if line.options else ""
        spec_text = f"（{spec}）" if spec else ""
        text = (
            f"已把「{line.title}」{spec_text}加入购物车，数量 {line.quantity}。"
            f"购物车现共 {snapshot['cart']['item_count']} 件，"
            f"合计 {snapshot['cart']['total']} 元。"
        )
        return ToolResult(
            tool_name=self.name,
            payload={"action": "add", "added": line.to_dict(), **snapshot},
            narrative_override=text,
            needs_composer=False,
        )

    def _ask_spec(
        self, product_id: str, title: str, skus: list[dict[str, Any]]
    ) -> ToolResult:
        """按"可变维度"反问用户选规格（避免罗列几十个 SKU 组合）。"""
        dims = self._varying_dims(skus)
        if not dims:
            # 理论不该发生（多 SKU 但无可变维度）；兜底用第一个 SKU。
            return self._commit_add(product_id, skus[0]["sku_id"], 1)

        # 选项交给前端的交互卡片渲染，文案只保留一句引导语，避免文字与卡片重复罗列。
        text = f"「{title}」有多个规格可选，下面选一下你想要的款式吧～"
        # dimensions：有序数组（name + values），前端按此顺序稳定渲染可点击 chip。
        # 同时保留 options（dict）做向后兼容。
        dimensions = [{"name": dim, "values": values} for dim, values in dims.items()]
        return ToolResult(
            tool_name=self.name,
            payload={
                "action": "ask_spec",
                "product_id": product_id,
                "title": title,
                "dimensions": dimensions,
                "options": dims,
            },
            narrative_override=text,
            needs_composer=False,
        )

    # ------------------------------ 规格解析 ------------------------------

    @staticmethod
    def _varying_dims(skus: list[dict[str, Any]]) -> "OrderedDict[str, list[str]]":
        """提取在给定 SKU 集合里取值多于一个的维度（颜色/尺码/容量…）。

        纯数字型维度（尺码/容量）按数值升序排列，便于阅读；其它维度保持出现顺序。"""
        dim_values: "OrderedDict[str, list[str]]" = OrderedDict()
        for sku in skus:
            for key, val in sku["options"].items():
                dim_values.setdefault(key, [])
                if val not in dim_values[key]:
                    dim_values[key].append(val)
        return OrderedDict(
            (k, CartTool._sort_values(vs)) for k, vs in dim_values.items() if len(vs) > 1
        )

    @staticmethod
    def _sort_values(values: list[str]) -> list[str]:
        """若每个取值都含数字（尺码/容量），按数字升序；否则保持原顺序。"""
        keyed: list[tuple[int, str]] = []
        for v in values:
            match = re.search(r"\d+", v)
            if not match:
                return values
            keyed.append((int(match.group()), v))
        return [v for _, v in sorted(keyed, key=lambda x: x[0])]

    def _filter_skus(
        self, query: str, skus: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """按用户这句话里能识别到的规格值过滤 SKU（覆盖所有品类）。

        没识别到任何规格 → 原样返回（触发反问）；识别到几个维度 →
        取交集，返回满足全部约束的 SKU。

        两段式匹配，确定性优先：
        ① 精确整值匹配（主路径）：用户这句里**原样包含**某个规格取值的完整字符串
           即采纳。前端规格卡发的就是库里的 canonical 值（如 '12GB+512GB'/'M码'/
           '幻影紫'），所以点选场景永远走这条、零歧义、永不死循环。同维度多个值都
           整串命中时取**最长**的那个（'12GB+512GB' 胜过假想子串），杜绝共享前缀误配。
        ② 模糊打分回退（仅当某维度精确匹配没命中）：用户打字/语音的近似说法
           （如只说 '512' 或 '深灰'）才退回 token 打分，取唯一最高分；并列则不约束。
        """
        dims = self._varying_dims(skus)
        constraints: dict[str, str] = {}
        for dim, values in dims.items():
            chosen = self._pick_dim_value(values, query)
            if chosen is not None:
                constraints[dim] = chosen
        if not constraints:
            return list(skus)
        return [
            sku for sku in skus
            if all(sku["options"].get(d) == v for d, v in constraints.items())
        ]

    @staticmethod
    def _pick_dim_value(values: list[str], query: str) -> str | None:
        """在某维度的候选取值里，选出用户这句话指定的那个；选不出返回 None。"""
        q = query or ""
        q_lower = q.lower()
        # ① 精确整值命中（取最长，避免子串型误配）。大小写不敏感。
        exact = [
            v for v in values
            if v and (v in q or v.lower() in q_lower)
        ]
        if exact:
            exact.sort(key=len, reverse=True)
            # 最长唯一 → 采纳；若并列最长（理论上 canonical 值不会，稳妥起见）则不约束
            if len(exact) == 1 or len(exact[0]) > len(exact[1]):
                return exact[0]
            return None
        # ② 模糊打分回退：取唯一最高分，并列视为歧义不约束。
        best_val: str | None = None
        best_score = 0
        tie = False
        for val in values:
            score = CartTool._value_match_score(val, q)
            if score > best_score:
                best_score, best_val, tie = score, val, False
            elif score == best_score and score > 0:
                tie = True
        if best_val is not None and best_score > 0 and not tie:
            return best_val
        return None

    @staticmethod
    def _value_match_score(value: str, query: str) -> int:
        """用户话里命中了该规格取值的多少个 token（数字/中文片段/英文整词）。

        分数越高代表匹配越完整，用于同维度多取值的消歧。

        匹配按"整 token"进行，避免松散的片段误配（例如规格值
        '灰色 Asphalt Grey' 里的 'sp' 撞上商品名 'Osprey' 的 'sp'）：
        - 数字型（尺码/容量，如 '42码'/'256GB'/'20L'）：作为独立数字整段命中。
        - 中文片段（如 '灰色'/'暗夜黑实战色'）：整段中文子串命中；
          较长（≥3 字）的中文值额外允许 ≥2 字连续片段命中。
        - 英文单词（如 'Black'）：作为完整单词（≥3 字母）命中，
          不做任意 2 字母片段匹配。"""
        value = (value or "").strip()
        if not value:
            return 0
        q = query.lower()
        score = 0
        # 0) 字母尺码（M码/L码/XL码/S/XXL 等）：字母前缀 ≤3，整段带左右边界命中。
        #    左边界 (?<![a-z]) 关键——否则 'L码' 会命中 'XL码'、'S' 命中 'XS'。
        #    右边界：带'码'时'码'即右锚；裸字母要求右侧非字母。尺码识别成功记 1 分。
        size_match = re.fullmatch(r"([A-Za-z]{1,3})(码)?", value)
        if size_match:
            token = value.lower()
            right_guard = "" if size_match.group(2) else r"(?![a-z])"
            if re.search(rf"(?<![a-z]){re.escape(token)}{right_guard}", q):
                score += 1
            return score
        # 1) 数字 token：要求是独立数字（前后非数字），避免 28 命中 280
        for num in re.findall(r"\d+", value):
            if re.search(rf"(?<!\d){re.escape(num)}(?!\d)", query):
                score += 1
        # 2) 中文连续片段：≥2 字才整段命中（避免 "码"/"色" 这类单字单位误配）；
        #    ≥3 字的长值额外允许 ≥2 字连续片段命中（如 "暗夜黑" 命中 "暗夜黑实战色"）
        for run in re.findall(r"[\u4e00-\u9fff]+", value):
            if len(run) >= 2 and run in query:
                score += 1
            elif len(run) >= 3:
                for i in range(len(run) - 1):
                    if run[i:i + 2] in query:
                        score += 1
                        break
        # 3) 英文整词（≥3 字母）
        for word in re.findall(r"[a-zA-Z]{3,}", value):
            if word.lower() in q:
                score += 1
        return score


    @staticmethod
    def _match_ordinal(
        query: str, skus: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """规格较少时支持"第二个/第2种"这类序号选择。"""
        if len(skus) > 9:
            return None
        idxs = resolve_indices(query, len(skus))
        if idxs and 0 <= idxs[0] < len(skus):
            return skus[idxs[0]]
        return None

    @staticmethod
    def _is_cancel(query: str) -> bool:
        return any(word in query for word in _CANCEL_WORDS)

    def _set_quantity(self, args: dict[str, Any]) -> ToolResult:
        line = self._target_line(args.get("cart_index"))
        qty = _as_int(args.get("quantity"), default=None)
        if qty is None:
            return self._plain("要改成几件呢？比如「第一件改成 2 个」。")
        updated = self._store.set_quantity(line.cart_item_id, qty)
        snapshot = self._snapshot()
        if updated is None:
            hint = f"已把「{line.title}」从购物车移除。"
        else:
            hint = f"已把「{updated.title}」数量改为 {updated.quantity}。"
        return ToolResult(
            tool_name=self.name,
            payload={"action": "set_quantity", **snapshot},
            composer_hint=hint + f"请确认并带出当前合计 {snapshot['cart']['total']} 元。",
        )

    def _remove(self, args: dict[str, Any]) -> ToolResult:
        line = self._target_line(args.get("cart_index"))
        self._store.remove_item(line.cart_item_id)
        snapshot = self._snapshot()
        return ToolResult(
            tool_name=self.name,
            payload={"action": "remove", "removed": line.to_dict(), **snapshot},
            composer_hint=(
                f"已把「{line.title}」从购物车删除。"
                f"请确认并带出当前还剩 {snapshot['cart']['item_count']} 件、"
                f"合计 {snapshot['cart']['total']} 元。"
            ),
        )

    def _view(self) -> ToolResult:
        snapshot = self._snapshot()
        if not snapshot["cart"]["lines"]:
            return self._plain("你的购物车还是空的，先让我帮你推荐几款吧～")
        return ToolResult(
            tool_name=self.name,
            payload={"action": "view", **snapshot},
            composer_hint=(
                "请用自然口吻汇报购物车：逐件说商品名、数量、小计，"
                f"最后给出合计 {snapshot['cart']['total']} 元，并询问是否下单。"
            ),
        )

    def _checkout(self, args: dict[str, Any]) -> ToolResult:
        address = args.get("address") or DEFAULT_ADDRESS
        order = self._store.build_order(address=address)
        return ToolResult(
            tool_name=self.name,
            payload={"action": "checkout", "order": order.to_dict()},
            composer_hint=(
                f"下单成功！订单号 {order.order_id}，共 {order.to_dict()['item_count']} 件、"
                f"合计 {order.total} 元，寄往：{order.address}。"
                "请用愉快的口吻确认下单完成。"
            ),
        )

    # ------------------------------ 辅助 ------------------------------

    def _target_line(self, cart_index: Any):
        """把"购物车第 N 件"定位到具体 CartLine；缺省取第一件。"""
        lines = self._store.list_items()
        if not lines:
            raise CartNotFoundError("购物车是空的")
        i = (int(cart_index) - 1) if cart_index is not None else 0
        if not (0 <= i < len(lines)):
            raise CartNotFoundError(f"购物车里没有第 {cart_index} 件")
        return lines[i]

    def _snapshot(self) -> dict[str, Any]:
        lines = self._store.list_items()
        return {
            "cart": {
                "lines": [line.to_dict() for line in lines],
                "item_count": sum(line.quantity for line in lines),
                "total": round(sum(line.subtotal for line in lines), 2),
            }
        }

    def _plain(self, text: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"action": "noop"},
            narrative_override=text,
            needs_composer=False,
        )


def _as_int(value: Any, default: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
