from __future__ import annotations

"""LLM query understanding：用 function calling 抽取结构化意图。

设计要点：
1. tool schema 的 enum 从 SQLite 注入，模型无法编造不存在的类目 / 品牌。
2. 同义词（"运动鞋"、"手机"、"ipad"）由 LLM 自然语言理解能力解决，
   不再需要规则版里那一堆 SUB_CATEGORY_ALIASES。
3. 否定语义（"我要 X 不要 Y"）由 LLM 上下文理解解决，不再需要窗口检测。
4. 调用失败 / 超时由编排器（query_understanding.py）兜底到规则版。
"""

import json
from typing import Any

from llm.client import get_client, get_model_id
from search.query_understanding import (
    BRAND_ALIASES,
    INGREDIENT_BLOCKLIST,
    ParsedQuery,
    load_taxonomy,
)


# 反向索引：variant → canonical，给 _filter_brands 做兜底映射。
# 即便 LLM 偶尔无视 enum 输出了变体（如 "Nike"），也能映射回 canonical（"耐克"）。
_VARIANT_TO_CANONICAL: dict[str, str] = {
    variant: canonical
    for canonical, variants in BRAND_ALIASES.items()
    for variant in variants
}


# ---------------------------------------------------------------------------
# 1. 构造 function schema
# ---------------------------------------------------------------------------

def _build_tool_schema(taxonomy: dict) -> dict:
    """根据当前 SQLite taxonomy 构造 function calling 的 tool schema。

    sub_category / brand 字段使用 enum 限定值域，从而把模型输出锁死在
    真实存在的取值上，避免 "vivo Pro Max" 这种幻觉品牌泄漏到下游。
    """
    sub_categories = sorted(taxonomy["sub_to_cat"].keys())
    categories = sorted({c for c in taxonomy["sub_to_cat"].values() if c})
    brands = list(taxonomy["brands"])

    return {
        "type": "function",
        "function": {
            "name": "extract_search_intent",
            "description": "从用户的中文电商查询中抽取结构化检索意图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": ["string", "null"],
                        "enum": [*categories, None],
                        "description": "用户想要的一级品类，必须严格选自候选列表；用户只给出大类（如\"美妆\"）时填这里，无法判断填 null。",
                    },
                    "sub_category": {
                        "type": ["string", "null"],
                        "enum": [*sub_categories, None],
                        "description": "用户想要的子类目，必须严格选自候选列表；无法判断时填 null。",
                    },
                    "brand_include": {
                        "type": "array",
                        "items": {"type": "string", "enum": brands},
                        "description": "用户明确表达想要的品牌，注意区分否定语境。",
                    },
                    "brand_exclude": {
                        "type": "array",
                        "items": {"type": "string", "enum": brands},
                        "description": "用户明确排除的品牌。'不要 X' 这种否定才放进来。",
                    },
                    "max_price": {
                        "type": ["number", "null"],
                        "description": "价格上限（人民币元）。如 '300 以内' → 300。",
                    },
                    "min_price": {
                        "type": ["number", "null"],
                        "description": "价格下限。如 '200 到 500' → 200。",
                    },
                    "negative_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "用户明确要排除的成分 / 品类 / 属性的【字面词】，下游在商品全文里做子串剔除。"
                            "必须填商品文本里会真实出现的词，例如 "
                            + "、".join(INGREDIENT_BLOCKLIST)
                            + " 等；也可以是这些之外的任意词（如 '花生'、'香菜'、'辣'）。"
                        ),
                    },
                    "soft_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "主观偏好词，如 '轻量'、'通勤'、'性价比'。用于召回后重排，不做硬过滤。",
                    },
                    "needs_clarification": {
                        "type": "boolean",
                        "description": "query 过于模糊（如'随便看看'）无法检索时为 true。",
                    },
                },
                "required": [
                    "category",
                    "sub_category",
                    "brand_include",
                    "brand_exclude",
                    "max_price",
                    "min_price",
                    "negative_ingredients",
                    "soft_terms",
                    "needs_clarification",
                ],
                "additionalProperties": False,
            },
        },
    }


SYSTEM_PROMPT = """你是电商搜索的需求解析器。严格遵守以下规则：

1. 你必须调用 extract_search_intent 函数，不要直接回答用户。
2. category / sub_category / brand 必须严格从 enum 候选中选，禁止编造或写中文翻译。
3. 否定语义只看局部范围：
   - "我要 Nike，不要太贵" → brand_include=["Nike"]，brand_exclude=[]
   - "推荐手机，不要华为" → sub_category="智能手机"，brand_exclude=["华为"]
4. negative_ingredients 是【开放式排除关键词】列表：用户说"不要X"/"不含X"/"无X"/"除了X"时，
   一律把 X 放进来。不限于固定清单，任何成分 / 品类 / 口味 / 属性都可以填
   （如 "花生"、"香菜"、"辣"、"咖啡"、"酒精"、"香精"）。下游按"商品标题与描述里是否
   出现该词"做字面子串过滤，所以务必填【商品文本里会真实出现的字面词】，
   不要引申成字面不同的术语：
   - "不要咖啡的饮料" → ["咖啡"]（不能为空，也不能写成"咖啡因"）
   - "不要花生的零食" → ["花生"]
   - "不放香菜" → ["香菜"]
   - "不要日系"/"不要韩系" → 直接填 ["日系"] / ["韩系"]
   特例：用户字面就是说成分（"无咖啡因"/"不含咖啡因"）时才填该成分本身 → ["咖啡因"]。
   注意：不要把用户正在搜索的主体品类填进来（搜"饮料"时别填"饮料"），只填要排除的部分。
5. 一级品类（category）同义词映射（用户只给大类、没给子品类时务必填 category）：
   - "美妆"/"化妆品"/"护肤"/"护肤品" → "美妆护肤"
   - "数码"/"电子产品"/"3C" → "数码电子"
   - "衣服"/"服装"/"运动装"/"运动" → "服饰运动"
   - "零食"/"吃的"/"食品"/"生活用品" → "食品生活"
6. 子品类（sub_category）同义词要映射到官方值：
   - "运动鞋"/"跑鞋" → "跑步鞋"
   - "手机" → "智能手机"
   - "ipad"/"平板" → "平板电脑"
   - "蓝牙耳机"/"降噪耳机"/"耳机" → "真无线耳机"
   - "洗面奶"/"洁面乳" → "洁面"
   - "口红"/"唇膏"/"唇彩" → "唇釉"
   - "防晒霜"/"防晒乳"/"隔离" → "防晒"
   - "爽肤水"/"水"/"柔肤水" → "化妆水"
   - "卸妆水"/"卸妆油" → "卸妆"
   - "精华液"/"安瓶" → "精华"
   - "粉饼"/"散粉"/"定妆粉" → "蜜粉"
   - "BB霜"/"气垫"/"粉底" → "粉底液"
   - 若用户说的子品类在 enum 里找不到对应官方值，宁可只填 category、把 sub_category 留 null，
     也不要硬套一个语义不符的子类（如"口红"绝不能映射到"眉笔"）。
7. needs_clarification 只在 query 完全没有任何商品线索时才设为 true（如\"随便看看\"、\"你好\"）；
   只要识别到 category / sub_category / brand / 价格 / 偏好中任意一项，就设为 false。
8. 数字提取要小心：
   - "300 以内" → max_price=300
   - "200 到 500" → min_price=200, max_price=500
   - "iPhone 15" 不是价格
9. 不确定的字段填 null 或空数组，不要瞎猜。
"""


# ---------------------------------------------------------------------------
# 2. 主入口
# ---------------------------------------------------------------------------

def parse_query_with_llm(
    query: str,
    timeout: float = 6.0,
) -> ParsedQuery:
    """调用豆包 Ark 的 function calling 抽取意图。

    失败时抛出异常，由编排层决定是否降级到规则版。
    """
    taxonomy = load_taxonomy()
    tool = _build_tool_schema(taxonomy)

    client = get_client()
    response = client.chat.completions.create(
        model=get_model_id(),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": "extract_search_intent"}},
        temperature=0.1,
        timeout=timeout,
    )

    arguments = _extract_tool_arguments(response)
    return _to_parsed_query(query, arguments, taxonomy)


def _extract_tool_arguments(response: Any) -> dict[str, Any]:
    """从 function calling 响应里取出参数字典。"""
    message = response.choices[0].message
    if not message.tool_calls:
        raise ValueError("LLM 没有调用 extract_search_intent，原始响应：" + str(message))
    raw = message.tool_calls[0].function.arguments
    return _loads_arguments(raw)


def _loads_arguments(raw: str) -> dict[str, Any]:
    """容错解析 function arguments。

    豆包 function calling 偶发返回畸形 JSON，已知三类污染：
    1. 多包一层 ```json code fence、前后夹带说明文字；
    2. 把内部工具标记 ``<parameter name="x">v</parameter>`` 泄漏进 JSON 串
       （表现为 ``"category" string="null">数码电子</parameter>``，缺冒号且夹 XML）；
    3. 上述两者叠加。
    直接 ``json.loads`` 会让整条检索链崩溃并降级到无过滤的纯向量召回（会召回到
    跨品类的无关商品）。这里做多级解析：原样 → 剥 code fence + 截取首个 JSON 对象
    → 修复 <parameter> 泄漏。全部失败才抛异常，交由上层降级。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    text = raw.strip()
    if text.startswith("```"):
        # 去掉 ```json ... ``` 围栏，只留中间内容
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]

    start = text.find("{")
    end = text.rfind("}")
    candidate = text[start : end + 1] if start != -1 and end != -1 and end > start else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # 最后一招：修复豆包 <parameter> 标记泄漏，再截取一次 JSON 对象重试。
        repaired = _repair_param_leak(candidate)
        r_start = repaired.find("{")
        r_end = repaired.rfind("}")
        if r_start != -1 and r_end != -1 and r_end > r_start:
            repaired = repaired[r_start : r_end + 1]
        return json.loads(repaired)


def _repair_param_leak(text: str) -> str:
    """修复豆包把内部 ``<parameter name="x">v</parameter>`` 标记混进 JSON 的污染。

    观察到的畸形样例（单行）::

        {"category" string="null">数码电子</parameter>
        <parameter name="sub_category": "真无线耳机", "max_price": 200.0,
         "min_price" string="null">null</parameter>
        <parameter name="negative_ingredients": [], "needs_clarification": false}

    修复策略（值本身都在，只是被 XML 包裹/缺冒号）：
    1. 去掉 ``<parameter name=`` 前缀，使 ``name="x"`` 还原为 ``"x"``；
    2. 把 ``"key" string="...">VALUE</parameter>`` 还原为 ``"key": VALUE``
       （VALUE 按 null/布尔/数字/字符串规范化）；
    3. 清掉残余 ``</parameter>``；
    4. 补回因标记剥离而缺失的逗号（值结束后直接换行接下一个 key 的情况）。
    """
    if "parameter" not in text:
        return text

    import re

    text = text.replace("<parameter name=", "")

    def _normalize_value(val: str) -> str:
        val = val.strip()
        if val in ("null", "true", "false"):
            return val
        if re.fullmatch(r"-?\d+(\.\d+)?", val):
            return val
        return '"' + val.replace('"', "") + '"'

    text = re.sub(
        r'("[A-Za-z_]+")\s+string="[^"]*">([^<]*)</parameter>',
        lambda m: f"{m.group(1)}: {_normalize_value(m.group(2))}",
        text,
    )
    text = text.replace("</parameter>", "")
    # 补逗号：值结束（引号 / 数字 / ] / null / 布尔）后紧跟换行 + 下一个 key
    text = re.sub(r'("|\]|\d|null|true|false)\s*\n\s*(")', r"\1, \2", text)
    return text


# 常见口味单字：长度虽 < 2 但语义明确、误伤风险低，豁免噪声护栏。
_SHORT_NEGATIVE_ALLOW: frozenset[str] = frozenset({"辣", "甜", "咸", "酸", "苦"})


def _clean_negatives(values: list[str] | None) -> list[str]:
    """归一化 LLM 给出的排除词（不再用白名单约束，支持任意"不要X"）。

    后置过滤是子串匹配，天然安全：匹配不到的词不会误伤商品。这里只做：
    - 去首尾空白、去空串、去重（保序）；
    - 丢掉长度 < 2 的单字噪声（如 "的"、"水" 太宽泛易误伤），
      但保留已知短词白名单（如 "糖"）和常见口味单字（辣/甜/咸/酸/苦）。
    """
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        term = (raw or "").strip()
        if not term or term in seen:
            continue
        if len(term) < 2 and term not in INGREDIENT_BLOCKLIST and term not in _SHORT_NEGATIVE_ALLOW:
            continue
        seen.add(term)
        out.append(term)
    return out


def _to_parsed_query(
    query: str,
    arguments: dict[str, Any],
    taxonomy: dict,
) -> ParsedQuery:
    """把 LLM 抽取结果包装成 ParsedQuery，并做最后一次 taxonomy 校正。

    虽然 enum 已经约束了 LLM 输出，这里再校验一次防御性兜底：
    即便模型违反 enum 输出了未知 sub_category / brand，也直接丢弃，
    不让脏数据流入下游 SQLite 硬过滤。
    """
    sub_category = arguments.get("sub_category")
    if sub_category and sub_category not in taxonomy["sub_to_cat"]:
        sub_category = None

    # 优先用 sub_category 反查父类；没有 sub 时退回 LLM 给的 category。
    known_categories = {c for c in taxonomy["sub_to_cat"].values() if c}
    if sub_category:
        category = taxonomy["sub_to_cat"].get(sub_category)
    else:
        category = arguments.get("category")
        if category not in known_categories:
            category = None

    known_brands = set(taxonomy["brands"])

    def _filter_brands(values: list[str] | None) -> list[str]:
        """规范化 + 去重：变体名映射回 canonical，未知品牌直接丢弃。"""
        if not values:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            canonical = _VARIANT_TO_CANONICAL.get(value, value)
            if canonical in known_brands and canonical not in seen:
                seen.add(canonical)
                out.append(canonical)
        return out

    return ParsedQuery(
        original_query=query,
        intent="product_search",
        category=category,
        sub_category=sub_category,
        max_price=_coerce_float(arguments.get("max_price")),
        min_price=_coerce_float(arguments.get("min_price")),
        brand_include=_filter_brands(arguments.get("brand_include")),
        brand_exclude=_filter_brands(arguments.get("brand_exclude")),
        negative_ingredients=_clean_negatives(arguments.get("negative_ingredients")),
        soft_terms=list(arguments.get("soft_terms") or []),
        retrieval_query=query,  # LLM 路径下保留原 query；soft_terms 留给召回后重排使用
        # 只要识别到任意一个商品线索就强制放行检索，避免 LLM 过于保守把可召回的 query 误判为模糊。
        # clarify 的最终决策权交给 Agent 层的 IntentRouter（信息更全、有历史上下文）。
        needs_clarification=bool(arguments.get("needs_clarification", False)) and not _has_any_signal(
            category=category,
            sub_category=sub_category,
            max_price=arguments.get("max_price"),
            min_price=arguments.get("min_price"),
            brand_include=arguments.get("brand_include"),
            soft_terms=arguments.get("soft_terms"),
        ),
    )


def _has_any_signal(**fields: Any) -> bool:
    """只要任意字段有内容（非 None / 非空列表 / 非空字符串），就认为 query 已经可检索。"""
    for value in fields.values():
        if value in (None, "", [], ()):
            continue
        return True
    return False


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
