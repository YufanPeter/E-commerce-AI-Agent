from __future__ import annotations

"""推荐解说评测：每句解说有没有落在商品事实上。

    证据覆盖      有证据的商品占比
    事实贴合      解说的字词有多少来自证据和标题（字符二元组重合度）
    无据数字      解说里的数字在证据、标题和规格里都找不到的比例，这是编造的硬信号
    套话率        解说含「性价比高」这类放到任何商品上都成立的话的比例
    点名率        解说里出现品牌或价格的比例，这两项卡片已展示，规定不写
    差评提及      商品有相关差评时，解说带上它的比例

--composer-ref 用指定 git 版本的 composer，用来和改动前对比。

用法：
    cd backend && python -m evals.run_narrative
    cd backend && python -m evals.run_narrative --composer-ref 7eee1cb --label before
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

CASES_PATH = Path(__file__).with_name("recall_cases.jsonl")
REPORT_DIR = Path(__file__).with_name("reports")
GENERIC_PHRASES = (
    "性价比高", "性价比不错", "性能够用", "品质不错", "品质优良", "质量不错", "值得入手", "值得购买", "不容错过",
    "日常使用", "轻松应对", "得心应手", "很不错", "表现不错", "表现出色", "深受", "满足日常", "满足需求", "好评如潮",
)
RISK_WORDS = ("反映", "吐槽", "不过", "但有", "也有用户", "有用户", "差评", "偏短", "偏小", "需要注意")


def bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\s，。、！？,.!?；;：:「」\"\'（）()]+", "", text or "")
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def score_item(description: str, card: dict[str, Any], sku_text: str) -> dict[str, Any]:
    evidence = card.get("evidence") or {}
    source = " ".join(p.get("text", "") for p in evidence.get("points") or [])
    if evidence.get("risk"):
        source += " " + evidence["risk"]
    source_all = f"{source} {card.get('title', '')} {sku_text}"
    grams = bigrams(description)
    unsupported = numbers(description) - numbers(source_all)
    brand = (card.get("brand") or "").split()[0] if card.get("brand") else ""
    return {
        "has_evidence": bool(evidence.get("points")),
        "grounded": len(grams & bigrams(source_all)) / len(grams) if grams else 0.0,
        "unsupported_number": bool(unsupported),
        "generic": any(phrase in description for phrase in GENERIC_PHRASES),
        "names_brand_or_price": bool(brand and brand in description) or "¥" in description or bool(re.search(r"\d+\s*元", description)),
        "has_risk": bool(evidence.get("risk")),
        "mentions_risk": any(word in description for word in RISK_WORDS),
        "length": len(description),
    }


def load_composer(ref: str | None) -> Any:
    from agent.composer import AnswerComposer

    if not ref:
        return AnswerComposer()
    source = subprocess.run(
        ["git", "show", f"{ref}:backend/agent/composer.py"],
        capture_output=True, text=True, check=True, cwd=BACKEND_DIR,
    ).stdout
    module = types.ModuleType("composer_at_ref")
    sys.modules["composer_at_ref"] = module
    exec(compile(source, "composer_at_ref", "exec"), module.__dict__)  # noqa: S102 - 只执行本仓库历史版本
    return module.AnswerComposer()


def run_case(query: str, recommend: Any, composer: Any, products: Any) -> list[dict[str, Any]]:
    from agent.session import AgentSession

    session = AgentSession()
    tool_result = recommend.run(query, session, {})
    cards = {card["product_id"]: card for card in tool_result.payload.get("products", [])}
    if not cards:
        return []
    data = json.loads(composer.compose(tool_result, session, timeout=30.0))
    rows = []
    for item in data.get("items") or []:
        card = cards.get(item.get("productId"))
        if not card:
            continue
        skus = products.get_skus(card["product_id"])
        sku_text = " ".join(str(v) for sku in skus for v in sku.properties.values())
        rows.append({
            "query": query,
            "product_id": card["product_id"],
            "title": card["title"][:24],
            "description": item.get("description", ""),
            "evidence": card.get("evidence"),
            **score_item(item.get("description", ""), card, sku_text),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="推荐解说评测")
    parser.add_argument("--composer-ref", default=None)
    parser.add_argument("--label", default="narrative")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)

    from agent.tools.recommend import RecommendTool
    from store.product_store import ProductStore

    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    queries = [case["query"] for case in cases if case["group"] == "semantic"][: args.limit]
    recommend = RecommendTool()
    recommend._get_service()  # 在主线程初始化，Chroma 客户端的懒加载不是线程安全的
    composer = load_composer(args.composer_ref)
    products = ProductStore()
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda q: run_case(q, recommend, composer, products), queries))
    rows = [row for result in results for row in result]

    def rate(key: str, subset: list[dict[str, Any]] | None = None) -> float:
        items = rows if subset is None else subset
        return round(sum(bool(row[key]) for row in items) / len(items), 3) if items else 0.0

    with_risk = [row for row in rows if row["has_risk"]]
    summary = {
        "queries": len(queries),
        "items": len(rows),
        "证据覆盖": rate("has_evidence"),
        "事实贴合": round(sum(row["grounded"] for row in rows) / len(rows), 3) if rows else 0.0,
        "无据数字": rate("unsupported_number"),
        "套话率": rate("generic"),
        "点名率": rate("names_brand_or_price"),
        "有差评的商品数": len(with_risk),
        "差评提及": rate("mentions_risk", with_risk),
        "平均字数": round(sum(row["length"] for row in rows) / len(rows), 1) if rows else 0.0,
    }
    print(args.label, summary)
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"{args.label}.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
