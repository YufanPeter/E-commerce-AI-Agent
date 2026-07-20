from __future__ import annotations

"""Build the product-image visual index offline.

Read ``backend/cdn/image_manifest.json``, encode each image with the multimodal
embedding service, and write ``backend/storage/image_index.json``. Rebuild this visual
reranking artifact when product images or the embedding model change.

Usage:
    cd backend && python -m rag.build_image_index
    python -m rag.build_image_index --force
"""

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm.vision import embed_image
from search.visual_index import DEFAULT_INDEX_PATH


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "backend" / "cdn" / "image_manifest.json"


def _load_manifest(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_existing(path: Path) -> dict[str, list[float]]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def build_index(
    manifest_path: Path = MANIFEST_PATH,
    out_path: Path = DEFAULT_INDEX_PATH,
    force: bool = False,
    max_workers: int = 8,
) -> dict[str, list[float]]:
    """Build or incrementally complete the visual index and persist it."""
    manifest = _load_manifest(manifest_path)
    existing = {} if force else _load_existing(out_path)

    todo = {pid: url for pid, url in manifest.items() if pid not in existing}
    logger.info(
        "Visual index: %d manifest entries, %d existing, %d to encode",
        len(manifest), len(existing), len(todo),
    )

    def _one(item: tuple[str, str]) -> tuple[str, list[float] | None]:
        pid, url = item
        try:
            return pid, embed_image(url)
        except Exception as exc:  # noqa: BLE001 - one image must not stop the batch
            logger.warning("Image encoding failed for product %s: %r", pid, exc)
            return pid, None

    result = dict(existing)
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for pid, vec in executor.map(_one, todo.items()):
                if vec is not None:
                    result[pid] = vec

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f)
    logger.info("Visual index written to %s with %d entries", out_path, len(result))
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Build the product-image visual index")
    parser.add_argument(
        "--force", action="store_true", help="Ignore the existing index and rebuild all"
    )
    parser.add_argument(
        "--workers", type=int, default=8, help="Number of concurrent encoding workers"
    )
    args = parser.parse_args()
    build_index(force=args.force, max_workers=args.workers)


if __name__ == "__main__":
    main()
