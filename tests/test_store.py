"""Vector store tests, centred on the freeze.

The freeze is the harness's defence against scoring an item against itself, so
it gets tested like a control: it must bite, and it must bite at the store
rather than depending on a caller remembering to check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlassay.retrieval.store import FrozenIndexError, VectorStore


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:
    return VectorStore(tmp_path / "v.db", dim=4, embed_model="probe")


class TestSearch:
    def test_nearest_comes_back_first(self, store: VectorStore) -> None:
        store.add("example", ["a", "b"], ["alpha", "beta"], [[1, 0, 0, 0], [0, 1, 0, 0]])
        hits = store.search("example", [1, 0, 0, 0], k=2)
        assert [h.doc_id for h in hits] == ["a", "b"]
        assert hits[0].distance < hits[1].distance

    def test_empty_index_returns_nothing_rather_than_raising(self, store: VectorStore) -> None:
        # The no-retrieval arm depends on "retrieved nothing" being a
        # representable state, not an error.
        assert store.search("schema", [1, 0, 0, 0]) == []

    def test_payload_survives_the_round_trip(self, store: VectorStore) -> None:
        store.add("value", ["v1"], ["ACME"], [[1, 0, 0, 0]], [{"table": "customers", "value": "ACME"}])
        assert store.search("value", [1, 0, 0, 0])[0].payload["value"] == "ACME"

    def test_reinserting_a_doc_id_updates_rather_than_duplicates(self, store: VectorStore) -> None:
        store.add("example", ["a"], ["first"], [[1, 0, 0, 0]])
        store.add("example", ["a"], ["second"], [[0, 1, 0, 0]])
        assert store.count("example") == 1
        assert store.search("example", [0, 1, 0, 0])[0].text == "second"


class TestFreeze:
    def test_freeze_blocks_writes(self, store: VectorStore) -> None:
        store.add("example", ["a"], ["alpha"], [[1, 0, 0, 0]])
        store.freeze("example", "train split only")
        with pytest.raises(FrozenIndexError, match="closed to writes"):
            store.add("example", ["b"], ["beta"], [[0, 1, 0, 0]])

    def test_freeze_does_not_block_reads(self, store: VectorStore) -> None:
        store.add("example", ["a"], ["alpha"], [[1, 0, 0, 0]])
        store.freeze("example", "train split only")
        assert len(store.search("example", [1, 0, 0, 0])) == 1

    def test_freeze_is_per_index(self, store: VectorStore) -> None:
        store.freeze("example", "train split only")
        store.add("schema", ["t"], ["Table t (a INTEGER)"], [[1, 0, 0, 0]])
        assert store.count("schema") == 1

    def test_freeze_records_its_reason(self, store: VectorStore) -> None:
        store.freeze("example", "train split only")
        frozen, reason = store.is_frozen("example")
        assert frozen is True
        assert "train split only" in reason

    def test_refreezing_keeps_the_original_record(self, store: VectorStore) -> None:
        store.freeze("example", "first reason")
        store.freeze("example", "second reason")
        _, reason = store.is_frozen("example")
        assert "first reason" in reason

    def test_freeze_is_checked_before_validation(self, store: VectorStore) -> None:
        # A frozen index must report *frozen*, not a length mismatch in a
        # write that was never going to be allowed.
        store.freeze("example", "closed")
        with pytest.raises(FrozenIndexError):
            store.add("example", ["a", "b"], ["only one text"], [[1, 0, 0, 0]])


class TestWidthGuard:
    def test_wrong_width_vector_is_refused(self, store: VectorStore) -> None:
        with pytest.raises(ValueError, match="width"):
            store.add("example", ["a"], ["alpha"], [[1, 0, 0]])
