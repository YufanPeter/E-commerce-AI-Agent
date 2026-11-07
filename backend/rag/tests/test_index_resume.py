from __future__ import annotations

import json

import pytest

from rag import build_chroma, build_image_index
from rag.build_chroma import Chunk, upsert_chunks


class FakeCollection:
    def __init__(self, failures: int = 0) -> None:
        self.ids: list[str] = []
        self.upserted_batches: list[list[str]] = []
        self.failures = failures

    def get(self, ids, include):
        assert include == []
        return {"ids": [chunk_id for chunk_id in ids if chunk_id in self.ids]}

    def upsert(self, ids, documents, metadatas):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("429")
        self.upserted_batches.append(list(ids))
        self.ids.extend(ids)


def _chunks(count: int) -> list[Chunk]:
    return [Chunk(id=f"c{i}", document=f"doc {i}", metadata={"i": i}) for i in range(count)]


def test_resume_only_embeds_missing_chunks():
    collection = FakeCollection()
    collection.ids = ["c0", "c1", "c4"]
    written = upsert_chunks(collection, _chunks(6), batch_size=3, resume=True)
    assert written == 3
    assert collection.upserted_batches == [["c2"], ["c3", "c5"]]


def test_resume_after_complete_build_makes_no_upserts():
    collection = FakeCollection()
    upsert_chunks(collection, _chunks(5), batch_size=2)
    collection.upserted_batches.clear()
    assert upsert_chunks(collection, _chunks(5), batch_size=2, resume=True) == 0
    assert collection.upserted_batches == []


def test_transient_upsert_failure_is_retried(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(build_chroma.time, "sleep", delays.append)
    collection = FakeCollection(failures=2)
    assert upsert_chunks(collection, _chunks(2), batch_size=2) == 2
    assert delays == [5.0, 10.0]


def test_persistent_upsert_failure_raises_and_can_resume(monkeypatch):
    monkeypatch.setattr(build_chroma.time, "sleep", lambda _: None)
    collection = FakeCollection()
    upsert_chunks(collection, _chunks(2), batch_size=2)
    collection.failures = 99
    with pytest.raises(RuntimeError):
        upsert_chunks(collection, _chunks(4), batch_size=2, resume=True)
    collection.failures = 0
    assert upsert_chunks(collection, _chunks(4), batch_size=2, resume=True) == 2
    assert sorted(collection.ids) == ["c0", "c1", "c2", "c3"]


def test_image_index_checkpoints_and_refills_failures(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({f"p{i}": f"https://img/{i}.jpg" for i in range(5)}))
    out = tmp_path / "nested" / "index.json"
    monkeypatch.setattr(build_image_index, "_local_images_by_product_id", dict)

    def flaky(url: str) -> list[float]:
        if url.endswith("3.jpg"):
            raise RuntimeError("429")
        return [1.0]

    monkeypatch.setattr(build_image_index, "embed_image_primary", flaky)
    writes: list[int] = []
    real_write = build_image_index._write_index

    def recording_write(path, index):
        writes.append(len(index))
        real_write(path, index)

    monkeypatch.setattr(build_image_index, "_write_index", recording_write)
    first = build_image_index.build_index(manifest, out, max_workers=1, checkpoint_every=2)
    assert sorted(first) == ["p0", "p1", "p2", "p4"]
    assert writes == [2, 4, 4]
    assert not out.with_name("index.json.tmp").exists()

    calls: list[str] = []

    def healthy(url: str) -> list[float]:
        calls.append(url)
        return [2.0]

    monkeypatch.setattr(build_image_index, "embed_image_primary", healthy)
    second = build_image_index.build_index(manifest, out, max_workers=1)
    assert calls == ["https://img/3.jpg"]
    assert len(json.loads(out.read_text())) == len(second) == 5
    assert second["p0"] == [1.0]
