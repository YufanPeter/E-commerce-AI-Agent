from __future__ import annotations

"""多需求拆解（multi-intent decomposition）。

职责（窄而清晰）：
    把【单句内】包含多个购物需求的 query 拆成若干互相独立的子需求，
    每个子需求随后各自走一遍标准检索管线（SearchService.search）。

    典型场景是"场景化购物"——一句话隐含多个品类：
        "我想去三亚旅游，推荐衣服和防晒吗？"
            → [ SubRequest(label="防晒", query="三亚旅游 海边 防晒"),
                SubRequest(label="衣服", query="三亚旅游 夏季 速干衣") ]

为什么和 query_understanding 分开：
    - understand_query 解决"一个需求 → 结构化 ParsedQuery"；
    - decompose 解决"一句话 → 几个需求"。两者是正交的两层，
      混在一起会让单个 LLM 任务过载、难调试。拆开后各管一件事。

为什么不动 ParsedQuery 契约：
    每个子需求最终仍是一个独立、完整的 ParsedQuery，下游 where_builder /
    retriever / reranker / 前端卡片格式全部零改动。decompose 只是在
    RecommendTool 入口多了一次"要不要 fan-out"的判断。

降级原则：
    decompose 是【锦上添花】，绝不能成为新的失败点。LLM 不可用 / 超时 /
    畸形输出时，一律退化为"单需求 = 原 query"，让 recommend 走回老路，
    保证可用性不退步。
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from llm.client import get_client, get_model_id
# 复用 llm_parser 里已经打磨过的容错 JSON 解析（豆包 function calling 偶发畸形输出）。
from search.llm_parser import _loads_arguments


logger = logging.getLogger(__name__)


# 拆解出的子需求上限：场景化购物再多也很少超过 4 个品类，
# 设上限防止 LLM 把一个需求过度切碎（如把"防晒"拆成"防晒霜/防晒衣/防晒帽"），
# 反而稀释每类的召回质量、拖慢响应（每个子需求都要一次完整检索）。
MAX_SUB_REQUESTS = 4


@dataclass(frozen=True)
class SubRequest:
    """单个子需求。

    label: 人类可读的需求名（如"防晒"、"衣服"），用于分组展示和 composer 话术。
    query: 喂给 SearchService 的检索子查询，应自带场景语境（如"三亚海边 防晒"），
           让向量召回更精准。
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
    """把 query 拆成子需求列表。

    返回至少含 1 个元素的列表：
        - 单需求（最常见）：返回 [SubRequest(label=query, query=query)]，
          调用方据此走原单路检索，零额外开销。
        - 多需求：返回多个 SubRequest，调用方 fan-out 检索后合并。

    任何异常（LLM 失败 / 超时 / 畸形输出 / 空结果）都【吞掉】并退化为单需求，
    保证 recommend 主链路永不因 decompose 失败而中断。
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
    except Exception:  # noqa: BLE001 - decompose 是增强项，失败一律退化为单需求
        logger.warning("decompose_query failed, fallback to single request", exc_info=True)
        return fallback

    subs = _normalize(requests, original_query=query)
    return subs or fallback


def _extract_requests(response: Any) -> list[dict[str, Any]]:
    message = response.choices[0].message
    if not message.tool_calls:
        raise ValueError("decompose LLM 没有调用 split_shopping_requests")
    raw = message.tool_calls[0].function.arguments
    args = _loads_arguments(raw)
    requests = args.get("requests")
    if not isinstance(requests, list):
        raise ValueError(f"requests 不是列表：{requests!r}")
    return requests


def _normalize(requests: list[dict[str, Any]], original_query: str) -> list[SubRequest]:
    """清洗 LLM 输出：去空、去重、截断到上限。"""
    seen_queries: set[str] = set()
    out: list[SubRequest] = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        sub_query = str(item.get("query") or "").strip()
        # query 缺失时退回 label，label 也缺失则跳过。
        sub_query = sub_query or label
        label = label or sub_query
        if not sub_query or sub_query in seen_queries:
            continue
        seen_queries.add(sub_query)
        out.append(SubRequest(label=label, query=sub_query))
        if len(out) >= MAX_SUB_REQUESTS:
            break
    return out
