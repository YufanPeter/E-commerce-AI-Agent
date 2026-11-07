from __future__ import annotations

"""记忆抽取评测：一句话进来，应该记住什么、不该记住什么。

    precision   写进记忆的条目里，有多少是对的
    recall      该记住的条目里，记住了多少
    误写率      不该记任何东西的句子里，被写入记忆的比例（对用户伤害最大的错误）

模式：
    rules       只用确定性规则（不打网络）
    full        规则没抽到时再用模型（和线上一致）

用法：
    cd backend && python -m evals.run_memory
    cd backend && python -m evals.run_memory evals/memory_heldout.jsonl
"""

import json
import logging
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

CASES_PATH = Path(__file__).with_name("memory_cases.jsonl")
REPORT_DIR = Path(__file__).with_name("reports")


def run_case(case: dict[str, Any], use_llm: bool) -> dict[str, Any]:
    from agent import memory_service
    from agent.memory_llm import extract_with_llm, might_disclose
    from store.user_memory_store import UserMemoryStore

    with tempfile.TemporaryDirectory() as tmp:
        store = UserMemoryStore(db_path=Path(tmp) / "memory.sqlite3")
        user = "eval"
        text = case["text"]
        events = memory_service.process_utterance(store, user, text, "s")
        suggested = store.list_items(user, statuses=("suggested",))
        if use_llm and not events and not suggested and might_disclose(text):
            memory_service._apply_extracted(store, user, extract_with_llm(text), text, "s")
        rows = store.list_items(user)
    got = [{"kind": r["kind"], "value": r["value"], "status": r["status"]} for r in rows]
    expected = case["expect"]

    def matches(want: dict[str, str], have: dict[str, str]) -> bool:
        return (
            want["kind"] == have["kind"]
            and want["status"] == have["status"]
            and (want["value"] == "*" or want["value"] == have["value"])
        )

    hit = sum(1 for want in expected if any(matches(want, have) for have in got))
    correct = sum(1 for have in got if any(matches(want, have) for want in expected))
    return {
        "id": case["id"], "text": case["text"], "expected": expected, "got": got,
        "hit": hit, "correct": correct, "ok": hit == len(expected) and correct == len(got),
    }


def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else CASES_PATH
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    report: dict[str, Any] = {}
    for mode, use_llm in (("rules", False), ("full", True)):
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda c: run_case(c, use_llm), cases))
        expected = sum(len(r["expected"]) for r in rows)
        written = sum(len(r["got"]) for r in rows)
        negatives = [r for r in rows if not r["expected"]]
        report[mode] = {
            "cases": len(rows),
            "precision": round(sum(r["correct"] for r in rows) / written, 3) if written else None,
            "recall": round(sum(r["hit"] for r in rows) / expected, 3) if expected else None,
            "false_write_rate": round(sum(1 for r in negatives if r["got"]) / len(negatives), 3),
            "all_correct": round(sum(1 for r in rows if r["ok"]) / len(rows), 3),
            "wrong": [{"text": r["text"], "expected": r["expected"], "got": r["got"]} for r in rows if not r["ok"]],
        }
        print(mode, {k: v for k, v in report[mode].items() if k != "wrong"})
        for row in report[mode]["wrong"]:
            print("  ✗", row["text"], "| 期望", [(e["kind"], e["value"], e["status"]) for e in row["expected"]], "| 实际", [(g["kind"], g["value"], g["status"]) for g in row["got"]])
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"{path.stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
