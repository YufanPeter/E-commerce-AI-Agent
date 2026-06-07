from __future__ import annotations

"""视觉相似索引：商品图向量的离线索引 + 在线打分。

「拍照找货」B+ 混合方案的视觉重排半边：
- 离线：``build_image_index`` 把 100 张商品图各编码成一个 2048 维向量，
  落盘到 ``backend/storage/image_index.json``（``{product_id: vector}``）。
- 在线：``VisualIndex`` 加载这些向量（启动时一次），给定用户图片向量，
  对**文本检索召回的候选集**算余弦相似度，作为重排信号之一。

为什么不建 ANN / 向量库：只有 100 个 SKU，候选集更是只有个位数，
直接内存里暴力算余弦即可，零额外基础设施。向量在加载时预归一化，
余弦退化成点积，打分非常快。
"""

import json
import logging
import math
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_PATH = PROJECT_ROOT / "backend" / "storage" / "image_index.json"


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


class VisualIndex:
    """内存中的商品图向量索引，提供余弦相似度打分。"""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        # 预归一化：余弦 = 点积，省去在线重复算模长。
        self._vectors: dict[str, list[float]] = {
            pid: _normalize(vec) for pid, vec in vectors.items()
        }

    def __len__(self) -> int:
        return len(self._vectors)

    def has(self, product_id: str) -> bool:
        return product_id in self._vectors

    def score(self, query_vec: list[float], product_id: str) -> float | None:
        """给定查询向量，返回它与某商品图的余弦相似度；该商品无索引时返回 None。"""
        target = self._vectors.get(product_id)
        if target is None:
            return None
        q = _normalize(query_vec)
        return sum(a * b for a, b in zip(q, target))

    def score_many(
        self, query_vec: list[float], product_ids: list[str]
    ) -> dict[str, float]:
        """对一批候选商品打分（缺索引的商品跳过，不进结果）。"""
        q = _normalize(query_vec)
        out: dict[str, float] = {}
        for pid in product_ids:
            target = self._vectors.get(pid)
            if target is not None:
                out[pid] = sum(a * b for a, b in zip(q, target))
        return out

    def top_k(self, query_vec: list[float], k: int = 10) -> list[tuple[str, float]]:
        """全库视觉最近邻（暴力扫描，100 条量级足够快）。"""
        q = _normalize(query_vec)
        scored = [
            (pid, sum(a * b for a, b in zip(q, vec)))
            for pid, vec in self._vectors.items()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]


def load_visual_index(path: Path = DEFAULT_INDEX_PATH) -> VisualIndex:
    """从磁盘加载视觉索引；文件不存在时返回空索引（视觉重排自动失效，不报错）。"""
    if not path.exists():
        logger.warning(
            "视觉索引 %s 不存在，视觉重排将退化为不参与（请先运行 build_image_index）。",
            path,
        )
        return VisualIndex({})
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info("视觉索引已加载：%d 条商品图向量", len(data))
    return VisualIndex(data)


@lru_cache(maxsize=1)
def get_visual_index() -> VisualIndex:
    """进程内单例，供 orchestrator 在图搜重排时复用。"""
    return load_visual_index()
