from __future__ import annotations

"""离线构建商品图视觉索引。

读取 ``backend/cdn/image_manifest.json``（``{product_id: 图片URL}``），逐张调用
百炼多模态 embedding 编码成向量，写到
``backend/storage/image_index_dashscope.json``
（``{product_id: vector}``）。这是「拍照找货」视觉重排的离线产物，只需在
数据/模型变更时重跑一次。

运行：
    cd backend && python -m rag.build_image_index
    # 或指定增量/全量
    python -m rag.build_image_index --force
"""

import argparse
import base64
import json
import logging
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm.vision import embed_image_primary
from search.visual_index import DEFAULT_INDEX_PATH


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "backend" / "cdn" / "image_manifest.json"


def _local_images_by_product_id() -> dict[str, Path]:
    """扫描仓库本地商品图，优先用于构建，避免云模型下载 CDN URL 失败。"""
    images: dict[str, Path] = {}
    for path in (PROJECT_ROOT / "data").glob("*/images/*"):
        if path.is_file():
            product_id = path.stem.removesuffix("_live")
            images[product_id] = path
    return images


def _as_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _load_manifest(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_existing(path: Path) -> dict[str, list[float]]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _write_index(path: Path, index: dict[str, list[float]]) -> None:
    """先写临时文件再替换，进程中断时不会留下损坏的索引。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(index, f)
    tmp_path.replace(path)


def build_index(
    manifest_path: Path = MANIFEST_PATH,
    out_path: Path = DEFAULT_INDEX_PATH,
    force: bool = False,
    max_workers: int = 8,
    checkpoint_every: int = 1000,
) -> dict[str, list[float]]:
    """构建（或增量补全）视觉索引并落盘，返回完整索引字典。

    每编码 checkpoint_every 条落盘一次；中断或单条失败后重跑即可补全。
    """
    manifest = _load_manifest(manifest_path)
    local_images = _local_images_by_product_id()
    existing = {} if force else _load_existing(out_path)

    todo = {pid: url for pid, url in manifest.items() if pid not in existing}
    logger.info(
        "视觉索引：manifest %d 条，已存在 %d 条，本次需编码 %d 条",
        len(manifest), len(existing), len(todo),
    )

    def _one(item: tuple[str, str]) -> tuple[str, list[float] | None]:
        pid, url = item
        try:
            image_input = (
                _as_data_uri(local_images[pid])
                if pid in local_images
                else url
            )
            return pid, embed_image_primary(image_input)
        except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整批
            logger.warning("商品 %s 图片编码失败：%r", pid, exc)
            return pid, None

    result = dict(existing)
    failed = 0
    since_checkpoint = 0
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for pid, vec in executor.map(_one, todo.items()):
                if vec is None:
                    failed += 1
                    continue
                result[pid] = vec
                since_checkpoint += 1
                if since_checkpoint >= checkpoint_every:
                    _write_index(out_path, result)
                    since_checkpoint = 0
                    logger.info(
                        "视觉索引进度：%d/%d，失败 %d 条",
                        len(result), len(manifest), failed,
                    )

    _write_index(out_path, result)
    logger.info(
        "视觉索引已写入 %s（共 %d 条，本次失败 %d 条，重跑可补全）",
        out_path, len(result), failed,
    )
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="构建商品图视觉索引")
    parser.add_argument(
        "--force", action="store_true", help="忽略已有索引，全量重建"
    )
    parser.add_argument(
        "--workers", type=int, default=8, help="并发编码线程数"
    )
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST_PATH, help="product_id → 图片 URL 的 JSON"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_INDEX_PATH, help="视觉索引输出路径"
    )
    parser.add_argument(
        "--checkpoint-every", type=int, default=1000, help="每编码多少条落盘一次"
    )
    args = parser.parse_args()
    build_index(
        manifest_path=args.manifest,
        out_path=args.out,
        force=args.force,
        max_workers=args.workers,
        checkpoint_every=args.checkpoint_every,
    )


if __name__ == "__main__":
    main()
