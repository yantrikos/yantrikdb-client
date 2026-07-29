"""pack_context() + idempotency_key behavior (SDK 0.4.0)."""

from __future__ import annotations

import httpx
import pytest

from yantrikdb.errors import IdempotencyConflict
from conftest import mock_client, body_of


def test_pack_context_parses_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/pack-context"
        assert request.method == "GET"
        return httpx.Response(200, json={
            "pack_context": "# Constitution\nYou know WordPress.",
            "packs_pending": ["deadbeef" * 8],
            "packs_poisoned": [],
        })

    c = mock_client(handler)
    ctx = c.pack_context()
    assert ctx.context.startswith("# Constitution")
    assert ctx.pending == ["deadbeef" * 8]
    assert ctx.poisoned == []
    assert ctx.prompt == ctx.context


def test_pack_context_prompt_empty_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "pack_context": None, "packs_pending": [], "packs_poisoned": [],
        })

    c = mock_client(handler)
    assert c.pack_context_prompt() == ""


def test_idempotency_key_passed_only_when_set():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(body_of(request))
        return httpx.Response(200, json={"rid": "r1"})

    c = mock_client(handler)
    c.remember("no key")
    c.remember("with key", idempotency_key="abc-123")
    assert "idempotency_key" not in seen[0]
    assert seen[1]["idempotency_key"] == "abc-123"


def test_idempotency_conflict_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "stored": False, "idempotency_conflict": True,
        })

    c = mock_client(handler)
    with pytest.raises(IdempotencyConflict):
        c.remember("different text", idempotency_key="abc-123")
