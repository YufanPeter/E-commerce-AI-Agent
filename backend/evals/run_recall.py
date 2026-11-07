from __future__ import annotations

"""商品召回评测：跑 evals/recall_cases.jsonl，按配置和题型输出指标。

配置（逐层打开，看每一层到底贡献了多少）：
    vector    只有向量召回：不解析、不过滤
    filter    LLM 解析 + 硬过滤 + 向量排序（USE_RERANK=0 的本地默认）
    rerank    filter + 云端精排（线上默认）
    final     rerank + RecommendTool 的预算加成、多样性配额，取用户真正看到的前 5 个

指标：
    recall@k / hit@k / mrr   有标准答案的题
    violation@5              前 5 个里违反否定或过滤约束的比例
    abstain                  库外题正确返回空结果的比例
    parse_ok                 query 解析字段全对的比例（structured 题）
    evidence hit@k           证据题：目标 chunk 在商品内检索的前 k 条里（对齐 product_detail）

用法（需要百炼 key，会真实调用 LLM / embedding / rerank）：
    cd backend && python -m evals.run_recall
    cd backend && python -m evals.run_recall --configs filter,rerank --groups semantic,alias
    cd backend && python -m evals.run_recall --cases evals/recall_heldout.jsonl --label heldout

recall_cases.jsonl 用来定位问题和调规则，recall_heldout.jsonl 不参与调规则，用来报告可信的分数。
调规则时不要看留出集的错题，否则它就不再是留出集。
"""

import argparse
import json
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

CASES_PATH = Path(__file__).with_name("recall_cases.jsonl")
REPORT_DIR = Path(__file__).with_name("reports")
DB_PATH = BACKEND_DIR / "storage" / "ecommerce_agent.sqlite3"
PRODUCT_CONFIGS = ("vector", "filter", "rerank", "final")


# ---------------------------------------------------------------------------
# 数据与标准答案
# ---------------------------------------------------------------------------

def load_catalog(db_path: Path = DB_PATH) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT p.product_id, p.title, p.brand, p.category, p.sub_category, r.min_price, r.max_price "
            "FROM products p JOIN product_price_ranges r ON r.product_id = p.product_id"
        ).fetchall()
    finally:
        conn.close()
    return {row["product_id"]: dict(row) for row in rows}


def matches_filter(product: dict[str, Any], gold_filter: dict[str, Any]) -> bool:
    if gold_filter.get("category") and product["category"] != gold_filter["category"]:
        return False
    if gold_filter.get("sub_category") and product["sub_category"] != gold_filter["sub_category"]:
        return False
    if gold_filter.get("brand_include") and product["brand"] not in gold_filter["brand_include"]:
        return False
    # 与线上 where 同一口径：有一个规格落在预算内就算符合
    if gold_filter.get("max_price") is not None and product["min_price"] > gold_filter["max_price"]:
        return False
    if gold_filter.get("min_price") is not None and product["max_price"] < gold_filter["min_price"]:
        return False
    return True


def gold_for(case: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> set[str]:
    if case.get("relevant"):
        return set(case["relevant"])
    if case.get("gold_filter"):
        gold = {pid for pid, product in catalog.items() if matches_filter(product, case["gold_filter"])}
        return {pid for pid in gold if not violates(catalog[pid], case.get("forbid"))}
    return set()


def violates(product: dict[str, Any] | None, forbid: dict[str, list[str]] | None) -> bool:
    if not product or not forbid:
        return False
    return any(product.get(field) in values for field, values in forbid.items())


def load_cases(path: Path, catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for case in cases:
        unknown = [pid for pid in case.get("relevant", []) if pid not in catalog]
        if case.get("product_id") and case["product_id"] not in catalog:
            unknown.append(case["product_id"])
        if unknown:
            raise ValueError(f"{case['id']} 标注了不存在的商品：{unknown}")
    return cases


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def recall_at(ranked: list[str], gold: set[str], k: int) -> float:
    return len(set(ranked[:k]) & gold) / len(gold) if gold else 0.0


def hit_at(ranked: list[str], gold: set[str], k: int) -> float:
    return 1.0 if set(ranked[:k]) & gold else 0.0


def reciprocal_rank(ranked: list[str], gold: set[str]) -> float:
    for index, pid in enumerate(ranked, start=1):
        if pid in gold:
            return 1.0 / index
    return 0.0


def score_product_case(
    case: dict[str, Any],
    ranked: list[str],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    out: dict[str, Any] = {"returned": len(ranked)}
    if case.get("expect_empty"):
        out["abstain"] = 1.0 if not ranked else 0.0
        return out
    gold = gold_for(case, catalog)
    if gold:
        out.update(
            gold_size=len(gold),
            recall5=recall_at(ranked, gold, 5),
            recall10=recall_at(ranked, gold, 10),
            hit1=hit_at(ranked, gold, 1),
            hit5=hit_at(ranked, gold, 5),
            mrr=reciprocal_rank(ranked, gold),
        )
    top5 = [catalog[pid] for pid in ranked[:5] if pid in catalog]
    if top5 and (case.get("forbid") or case.get("gold_filter")):
        bad = 0
        for product in top5:
            if violates(product, case.get("forbid")):
                bad += 1
            elif case.get("gold_filter") and not matches_filter(product, case["gold_filter"]):
                bad += 1
        out["violation5"] = bad / len(top5)
    return out


def parse_ok(case: dict[str, Any], parsed: dict[str, Any] | None) -> float | None:
    expect = case.get("expect")
    if not expect or parsed is None:
        return None
    for field, want in expect.items():
        got = parsed.get(field)
        if field == "brand_include":
            if not set(got or []) & set(want):
                return 0.0
        elif field in {"max_price", "min_price"}:
            if got is None or float(got) != float(want):
                return 0.0
        elif got != want:
            return 0.0
    return 1.0


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("recall5", "recall10", "hit1", "hit5", "mrr", "violation5", "abstain", "parse_ok", "latency_ms")
    summary: dict[str, Any] = {"n": len(rows)}
    for field in fields:
        values = [row[field] for row in rows if row.get(field) is not None]
        if values:
            summary[field] = mean(values)
    return summary


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

@dataclass
class Pipelines:
    retriever: Any
    filter_service: Any
    rerank_service: Any
    recommend: Any


def build_pipelines() -> Pipelines:
    from agent.tools.recommend import RecommendTool
    from rag.retriever import ChromaRetriever
    from search.search_service import SearchService

    retriever = ChromaRetriever()
    rerank_service = SearchService(retriever=retriever, use_rerank=True)
    return Pipelines(
        retriever=retriever,
        filter_service=SearchService(retriever=retriever, use_rerank=False),
        rerank_service=rerank_service,
        recommend=RecommendTool(search_service=rerank_service),
    )


def run_product_config(config: str, query: str, pipes: Pipelines) -> tuple[list[str], dict[str, Any] | None]:
    from agent.session import AgentSession
    from search.search_service import _aggregate_by_distance

    if config == "vector":
        chunks = pipes.retriever.search(query=query, top_k=50, where=None)
        return [hit.product_id for hit in _aggregate_by_distance(chunks)][:10], None
    if config == "filter":
        result = pipes.filter_service.search(query, top_k_products=10)
        return [hit.product_id for hit in result.hits], result.parsed.to_dict()
    if config == "rerank":
        result = pipes.rerank_service.search(query, top_k_products=10)
        return [hit.product_id for hit in result.hits], result.parsed.to_dict()
    if config == "final":
        tool_result = pipes.recommend.run(query, AgentSession(), {})
        return (
            [card["product_id"] for card in tool_result.payload.get("products", [])],
            (tool_result.payload.get("debug") or {}).get("parsed"),
        )
    raise ValueError(config)


def evaluate_product_case(case: dict[str, Any], config: str, pipes: Pipelines, catalog: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        ranked, parsed = run_product_config(config, case["query"], pipes)
        error = None
    except Exception as exc:  # noqa: BLE001 - 单题失败要记录，不能中断整轮
        ranked, parsed, error = [], None, f"{type(exc).__name__}: {exc}"
    row = {
        "id": case["id"],
        "group": case["group"],
        "query": case["query"],
        "config": config,
        "ranked": ranked[:10],
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "parsed": {k: v for k, v in (parsed or {}).items() if k != "hard_filters" and v not in (None, [], "", False)},
    }
    if error:
        row["error"] = error
    row.update(score_product_case(case, ranked, catalog))
    parsed_score = parse_ok(case, parsed)
    if parsed_score is not None:
        row["parse_ok"] = parsed_score
    return row


def evaluate_evidence_case(case: dict[str, Any], pipes: Pipelines, catalog: dict[str, Any]) -> dict[str, Any]:
    """对齐 product_detail：先锁定商品，再在该商品的 chunk 里找证据；同时看开放式能否定位到商品。"""
    product = catalog[case["product_id"]]
    query = f"{product['title']} {case['query']}"
    chunks = pipes.retriever.search(query=query, top_k=12, where={"product_id": case["product_id"]})
    scoped = [chunk.chunk_id for chunk in chunks]
    reranked: list[str] = []
    try:
        reranked = [
            item.chunk.chunk_id
            for item in pipes.rerank_service._get_reranker().rerank(query=case["query"], chunks=chunks)
        ]
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("evidence rerank failed: %r", exc)
    open_result = pipes.rerank_service.search(case["query"], top_k_products=5)
    open_ranked = [hit.product_id for hit in open_result.hits]
    gold = {case["chunk_id"]}
    return {
        "id": case["id"],
        "group": "evidence",
        "kind": case["kind"],
        "query": case["query"],
        "chunk_id": case["chunk_id"],
        "scoped_rank": scoped.index(case["chunk_id"]) + 1 if case["chunk_id"] in scoped else None,
        "reranked_rank": reranked.index(case["chunk_id"]) + 1 if case["chunk_id"] in reranked else None,
        "vector_hit1": hit_at(scoped, gold, 1),
        "vector_hit3": hit_at(scoped, gold, 3),
        "vector_hit6": hit_at(scoped, gold, 6),
        "rerank_hit1": hit_at(reranked, gold, 1),
        "rerank_hit3": hit_at(reranked, gold, 3),
        "rerank_hit6": hit_at(reranked, gold, 6),
        "open_product_hit1": hit_at(open_ranked, {case["product_id"]}, 1),
        "open_product_hit5": hit_at(open_ranked, {case["product_id"]}, 5),
        "open_ranked": open_ranked,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# 召回评测 {report['generated_at']}", "", f"题目 {report['case_count']} 道。", ""]
    for config, groups in report["by_config"].items():
        lines += [
            f"## {config}", "",
            "| 题型 | 题数 | R@5 | R@10 | Hit@1 | Hit@5 | MRR | 违规@5 | 拒答 | 解析全对 | 耗时ms |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for group, s in groups.items():
            cell = lambda key: "" if s.get(key) is None else f"{s[key]:.2f}"
            latency = "" if s.get("latency_ms") is None else f"{s['latency_ms']:.0f}"
            lines.append(
                f"| {group} | {s['n']} | {cell('recall5')} | {cell('recall10')} | {cell('hit1')} | {cell('hit5')} "
                f"| {cell('mrr')} | {cell('violation5')} | {cell('abstain')} | {cell('parse_ok')} | {latency} |"
            )
        lines.append("")
    if report.get("evidence"):
        lines += ["## 证据定位（锁定商品后找 chunk）", "", "| 类型 | 题数 | 向量 Hit@1 | Hit@3 | Hit@6 | 精排 Hit@1 | Hit@3 | Hit@6 | 开放定位商品 Hit@1 | Hit@5 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for kind, s in report["evidence"].items():
            lines.append(
                f"| {kind} | {s['n']} | {s['vector_hit1']:.2f} | {s['vector_hit3']:.2f} | {s['vector_hit6']:.2f} "
                f"| {s['rerank_hit1']:.2f} | {s['rerank_hit3']:.2f} | {s['rerank_hit6']:.2f} "
                f"| {s['open_product_hit1']:.2f} | {s['open_product_hit5']:.2f} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="商品召回评测")
    parser.add_argument("--configs", default=",".join(PRODUCT_CONFIGS))
    parser.add_argument("--groups", default="")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--cases", default=str(CASES_PATH), help="题目文件；留出集用 evals/recall_heldout.jsonl")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    catalog = load_catalog()
    cases = load_cases(Path(args.cases), catalog)
    if args.groups:
        wanted = set(args.groups.split(","))
        cases = [case for case in cases if case["group"] in wanted]
    pipes = build_pipelines()
    configs = [c for c in args.configs.split(",") if c]
    product_cases = [case for case in cases if case["group"] != "evidence"]
    evidence_cases = [case for case in cases if case["group"] == "evidence"]

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(evaluate_product_case, case, config, pipes, catalog)
            for config in configs for case in product_cases
        ]
        rows = [future.result() for future in futures]
        evidence_rows = [future.result() for future in [
            pool.submit(evaluate_evidence_case, case, pipes, catalog) for case in evidence_cases
        ]]

    by_config: dict[str, dict[str, Any]] = {}
    for config in configs:
        config_rows = [row for row in rows if row["config"] == config]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in config_rows:
            grouped[row["group"]].append(row)
        by_config[config] = {group: summarize(items) for group, items in grouped.items()}
        by_config[config]["ALL"] = summarize(config_rows)

    evidence_summary: dict[str, Any] = {}
    for kind in ("faq", "review"):
        items = [row for row in evidence_rows if row["kind"] == kind]
        if items:
            evidence_summary[kind] = {"n": len(items)} | {
                key: mean([row[key] for row in items])
                for key in ("vector_hit1", "vector_hit3", "vector_hit6", "rerank_hit1", "rerank_hit3", "rerank_hit6", "open_product_hit1", "open_product_hit5")
            }

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "label": args.label,
        "case_count": len(cases),
        "by_config": by_config,
        "evidence": evidence_summary,
        "rows": rows,
        "evidence_rows": evidence_rows,
        "errors": [row for row in rows if row.get("error")],
    }
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    markdown = render_markdown(report)
    (REPORT_DIR / f"{args.label}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"错误 {len(report['errors'])} 条；明细见 {REPORT_DIR / (args.label + '.json')}")


if __name__ == "__main__":
    main()
