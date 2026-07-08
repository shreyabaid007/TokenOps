"""Tests for proxy.cache — semantic prompt cache backed by Qdrant + Modal.

Both Modal and Qdrant are mocked. No network or container starts.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxy import cache, config

pytestmark = pytest.mark.asyncio


class _FakePoint:
    """Duck-typed stand-in for qdrant_client.models.ScoredPoint.
    lookup() reads only .payload, so .score is informational here."""

    def __init__(self, payload: dict, score: float) -> None:
        self.payload = payload
        self.score = score


class _FakeQueryResponse:
    """Duck-typed stand-in for qdrant_client's QueryResponse."""

    def __init__(self, points: list[_FakePoint]) -> None:
        self.points = points


def _rules(threshold: float) -> config.RoutingRules:
    return config.RoutingRules(
        id=1,
        cache_threshold=threshold,
        low_max_tokens=300,
        high_min_tokens=800,
    )


async def test_lookup_returns_payload_when_neighbour_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "current_rules", _rules(0.90))

    payload = {
        "prompt": "hello",
        "response": "world",
        "model": "claude-haiku-4-5",
        "tokens_in": 10,
        "tokens_out": 20,
    }
    fake_qdrant = AsyncMock()
    fake_qdrant.query_points = AsyncMock(
        return_value=_FakeQueryResponse([_FakePoint(payload, score=0.95)])
    )

    monkeypatch.setattr(cache, "_embed", AsyncMock(return_value=[0.1] * 384))
    monkeypatch.setattr(cache, "_qdrant", lambda: fake_qdrant)

    result = await cache.lookup("hello")

    assert result == payload
    # The active threshold from config.current_rules must reach Qdrant —
    # otherwise the agent's hot-reloaded threshold would have no effect.
    kwargs = fake_qdrant.query_points.call_args.kwargs
    assert kwargs["score_threshold"] == 0.90
    assert kwargs["collection_name"] == cache.COLLECTION_NAME


async def test_lookup_returns_none_when_no_neighbour_passes_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qdrant filters by score_threshold internally; sub-threshold matches
    never appear in the result. An empty result list is the miss signal."""
    monkeypatch.setattr(config, "current_rules", _rules(0.92))

    fake_qdrant = AsyncMock()
    fake_qdrant.query_points = AsyncMock(return_value=_FakeQueryResponse([]))

    monkeypatch.setattr(cache, "_embed", AsyncMock(return_value=[0.1] * 384))
    monkeypatch.setattr(cache, "_qdrant", lambda: fake_qdrant)

    result = await cache.lookup("hello")

    assert result is None


async def test_lookup_returns_none_when_modal_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 5: Modal failure is silent. The timeout path must yield None,
    must not raise, and must not trigger a downstream Qdrant call."""
    monkeypatch.setattr(cache, "MODAL_TIMEOUT_SEC", 0.05)

    async def slow_aio(_texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(10)
        return [[0.0] * 384]  # never reached

    fake_fn = MagicMock()
    fake_fn.remote.aio = AsyncMock(side_effect=slow_aio)
    monkeypatch.setattr(cache, "_embedder", lambda: fake_fn)

    fake_qdrant = AsyncMock()
    fake_qdrant.query_points = AsyncMock(
        side_effect=AssertionError("qdrant must not be called when embed fails")
    )
    monkeypatch.setattr(cache, "_qdrant", lambda: fake_qdrant)

    result = await cache.lookup("hello")

    assert result is None
    fake_qdrant.query_points.assert_not_called()
