"""
Unit tests for ``document_processor/quick_code/preferences.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from document_processor.quick_code.contracts import PreferencePair
from document_processor.quick_code.preferences import ORPOExporter


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Mocks
# ─────────────────────────────────────────────────────────────────────


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def insert_one(self, doc: dict[str, Any]) -> Any:
        self.docs.append(doc)

        class _Res:
            inserted_id = "fake"

        return _Res()


class _FakeMongo:
    def __init__(self) -> None:
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        if name not in self._collections:
            self._collections[name] = _FakeCollection()
        return self._collections[name]


class _FakeKafkaProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    def send(self, topic: str, *, value: bytes) -> Any:
        self.sent.append((topic, value))
        return True


# ─────────────────────────────────────────────────────────────────────
# Disabled exporter
# ─────────────────────────────────────────────────────────────────────


def test_disabled_exporter_returns_empty():
    ex = ORPOExporter(enabled=False)
    out = _run(ex.export(prompt="task", chosen="X", rejected="Y"))
    assert out == []


# ─────────────────────────────────────────────────────────────────────
# Pair construction
# ─────────────────────────────────────────────────────────────────────


def test_export_returns_pair_with_reward_delta():
    ex = ORPOExporter(enabled=True)
    pairs = _run(
        ex.export(
            prompt="task",
            rejected="bad",
            chosen="good",
            reward_chosen=1.0,
            reward_rejected=0.2,
        )
    )
    assert len(pairs) == 1
    p = pairs[0]
    assert isinstance(p, PreferencePair)
    assert p.reward_delta == pytest.approx(0.8)
    assert p.metadata["reward_chosen"] == 1.0
    assert p.metadata["reward_rejected"] == 0.2


def test_export_skips_identical_pair():
    ex = ORPOExporter(enabled=True)
    out = _run(ex.export(prompt="task", rejected="same", chosen="same"))
    assert out == []


def test_export_skips_empty_inputs():
    ex = ORPOExporter(enabled=True)
    assert _run(ex.export(prompt="", rejected="X", chosen="Y")) == []
    assert _run(ex.export(prompt="task", rejected="", chosen="Y")) == []
    assert _run(ex.export(prompt="task", rejected="X", chosen="")) == []


# ─────────────────────────────────────────────────────────────────────
# Mongo sink
# ─────────────────────────────────────────────────────────────────────


def test_export_writes_to_mongo_when_enabled():
    mongo = _FakeMongo()
    ex = ORPOExporter(enabled=True, mongo_db=mongo, mongo_collection="orpo")
    _run(ex.export(prompt="task", rejected="bad", chosen="good"))
    assert len(mongo["orpo"].docs) == 1
    assert mongo["orpo"].docs[0]["prompt"] == "task"


def test_mongo_failure_does_not_abort():
    class Boom:
        def __getitem__(self, name):
            raise RuntimeError("mongo down")

    ex = ORPOExporter(enabled=True, mongo_db=Boom())
    out = _run(ex.export(prompt="task", rejected="X", chosen="Y"))
    # Persistence failed but the pair is still returned to the
    # caller for audit.
    assert len(out) == 1


# ─────────────────────────────────────────────────────────────────────
# Kafka sink
# ─────────────────────────────────────────────────────────────────────


def test_kafka_publish_called():
    producer = _FakeKafkaProducer()
    ex = ORPOExporter(
        enabled=True,
        kafka_topic="topic.orpo",
        kafka_producer=producer,
    )
    _run(ex.export(prompt="task", rejected="X", chosen="Y"))
    assert producer.sent
    topic, payload = producer.sent[0]
    assert topic == "topic.orpo"
    assert b"task" in payload


def test_kafka_failure_does_not_abort():
    class Broken:
        def send(self, *a, **kw):
            raise RuntimeError("kafka down")

    ex = ORPOExporter(
        enabled=True, kafka_topic="t", kafka_producer=Broken()
    )
    out = _run(ex.export(prompt="task", rejected="X", chosen="Y"))
    assert len(out) == 1


# ─────────────────────────────────────────────────────────────────────
# export_from_bundle helper
# ─────────────────────────────────────────────────────────────────────


def test_export_from_bundle_uses_rlef_composite():
    ex = ORPOExporter(enabled=True)
    bundle = {
        "rlef_reward": {"composite": 0.92, "breakdown": {"pass": 0.8}},
        "verification": {"score": 75.0},
        "session_id": "sess-123",
    }
    out = _run(
        ex.export_from_bundle(
            bundle, prompt="task", rejected="X", chosen="Y"
        )
    )
    assert len(out) == 1
    pair = out[0]
    assert pair.reward_delta == pytest.approx(0.92)
    assert pair.metadata["session_id"] == "sess-123"
    assert pair.metadata["rlef"] == {"pass": 0.8}
