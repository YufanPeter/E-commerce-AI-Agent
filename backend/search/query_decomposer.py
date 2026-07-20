from __future__ import annotations

"""Multi-intent decomposition for shopping queries.

Split several shopping needs in one utterance into independent subrequests, each of
which runs through ``SearchService.search``.

Example:
        "我想去三亚旅游，推荐衣服和防晒吗？"
            → [ SubRequest(label="防晒", query="三亚旅游 海边 防晒"),
                SubRequest(label="衣服", query="三亚旅游 夏季 速干衣") ]

Decomposition is separate from query understanding: one decides how many needs exist,
while the other maps one need to a structured ``ParsedQuery``.

Each subrequest still produces a complete ``ParsedQuery``, preserving all downstream
contracts. Decomposition only decides whether ``RecommendTool`` should fan out.

Decomposition is optional. LLM failure, timeout, or malformed output falls back to one
subrequest containing the original query.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from llm.client import get_client, get_model_id
# Reuse the tolerant function-call JSON parser.
from search.llm_parser import _loads_arguments


logger = logging.getLogger(__name__)


# Bound fan-out so the model cannot over-split one need and dilute retrieval quality.
MAX_SUB_REQUESTS = 4


@dataclass(frozen=True)
class SubRequest:
    """One independent shopping subrequest.

    ``label`` is used for grouped display and composition. ``query`` includes sufficient
    scenario context for standalone retrieval.
    """

    label: str
    query: str

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "query": self.query}


_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "split_shopping_requests",
        "description": "把用户一句话里的购物需求拆成若干互相独立的子需求。",
        "parameters": {
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "该子需求的简短品类名，2-6 字，如'防晒'、'衣服'、'充电宝'。",
                            },
                            "query": {
                                "type": "string",
                                "description": (
                                    "用于检索该子需求的完整子查询。规则："
                                    "①【必须保留用户说的核心品类词本身】，只在它前面补场景语境"
                                    "（地点/季节/用途），绝不能把核心词替换成别的品类。"
                                    "如用户说'防晒'就写'三亚海边旅游 防晒霜'，"
                                    "【严禁】擅自改成'防晒衣''防晒帽'这类跨品类的词；"
                                    "②衣服子查询写'三亚夏季旅游 速干衣 短袖'。"
                                ),
                            },
                        },
                        "required": ["label", "query"],
                        "additionalProperties": False,
                    },
                    "description": "子需求列表。单一需求时只返回 1 个元素。",
                },
            },
            "required": ["requests"],
            "additionalProperties": False,
        },
    },
}


SYSTEM_PROMPT = """你是电商导购的"需求拆解器"。唯一职责：判断用户一句话里是否包含【多个不同品类】的购物需求，并拆成独立子需求。

严格规则：
1. 必须调用 split_shopping_requests 函数，不要直接回答用户。
2. 只在用户明确想要【两个或以上不同品类】时才拆分，例如：
   - "推荐衣服和防晒" → 2 个：衣服、防晒
   - "露营要带的帐篷、睡袋和炉子" → 3 个：帐篷、睡袋、炉子
3. 单一品类的需求【绝不拆分】，只返回 1 个元素，query 原样（可补场景语境）：
   - "推荐一款适合油皮的洗面奶" → 1 个：洗面奶
   - "500 以内的降噪耳机" → 1 个：耳机
   - "便宜点的红色 Nike 跑鞋" → 1 个：跑鞋（颜色/品牌/价格是【约束】不是新品类）
4. 不要把同一品类的不同属性拆成多个需求（"黑色和白色的 T 恤"是 1 个需求，不是 2 个）。
5. 【忠于用户原话】：子需求的核心品类词必须就是用户说的那个词，只能在前面补场景
   语境（地点/季节/用途），绝不能漂移成别的品类。
   - 用户说"防晒" → label="防晒"，query="三亚海边旅游 防晒霜"（防晒=防晒霜，属美妆护肤）；
     【错误示范】把它改写成"防晒衣""防晒帽"——那是服饰，偏离了用户本意。
   - 用户说"衣服" → 保持"衣服/速干衣/短袖"，不要替换成"鞋""包"。
6. 【不要无中生有】：用户已经明确列出了要哪些品类时，就【严格按用户列的来】，
   一个不多一个不少，不要再额外推断添加用户没提的品类。
   - "推荐衣服和防晒" → 只出 2 个：衣服、防晒（【不要】自作主张加"背包""泳装"）。
   只有当用户【只给场景、完全没点明任何品类】时（如"我要去三亚旅游"，后面没说要啥），
   才可以合理推断该场景下最核心的 1-3 个品类。
7. 拆分数量控制在 1-4 个，宁可少而精，不要把需求切得过碎。
8. label 用简短品类名（2-6 字），query 要完整且自带场景，能独立喂给搜索引擎。
"""


def decompose_query(query: str, timeout: float = 6.0) -> list[SubRequest]:
    """Split a query into one or more subrequests.

    A single need returns the original query and uses the normal retrieval path. Multiple
    needs return independent requests for fan-out and merge.

    Every failure is contained and falls back to one request.
    """
    fallback = [SubRequest(label=query, query=query)]
    try:
        client = get_client()
        response = client.chat.completions.create(
            model=get_model_id(),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "split_shopping_requests"}},
            temperature=0.1,
            timeout=timeout,
        )
        requests = _extract_requests(response)
    except Exception:  # noqa: BLE001 - decomposition is optional
        logger.warning("decompose_query failed, fallback to single request", exc_info=True)
        return fallback

    subs = _normalize(requests, original_query=query)
    return subs or fallback


def _extract_requests(response: Any) -> list[dict[str, Any]]:
    message = response.choices[0].message
    if not message.tool_calls:
        raise ValueError("The decomposition LLM did not call split_shopping_requests")
    raw = message.tool_calls[0].function.arguments
    args = _loads_arguments(raw)
    requests = args.get("requests")
    if not isinstance(requests, list):
        raise ValueError(f"requests 不是列表：{requests!r}")
    return requests


def _normalize(requests: list[dict[str, Any]], original_query: str) -> list[SubRequest]:
    """Remove empty or duplicate output and enforce the request limit."""
    seen_queries: set[str] = set()
    out: list[SubRequest] = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        sub_query = str(item.get("query") or "").strip()
        # Fall back from a missing query to its label; skip when both are empty.
        sub_query = sub_query or label
        label = label or sub_query
        if not sub_query or sub_query in seen_queries:
            continue
        seen_queries.add(sub_query)
        out.append(SubRequest(label=label, query=sub_query))
        if len(out) >= MAX_SUB_REQUESTS:
            break
    return out
