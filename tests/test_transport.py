"""Leader-follow + transient-retry transport behavior (SDK 0.4.0)."""

from __future__ import annotations

import httpx
import pytest

from yantrikdb.errors import NotLeaderError, TransientError
from conftest import mock_client, body_of


def test_write_to_follower_follows_leader_and_sticks():
    """A 307 not_leader on a follower is followed to the leader, the token
    survives the hop, and the client sticks to the leader for the next call."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "follower":
            return httpx.Response(
                307,
                json={"error": "not_leader", "leader_id": 2,
                      "leader_addr": "http://leader:7438"},
            )
        # leader
        assert request.headers.get("authorization") == "Bearer ydb_test", \
            "Authorization must be re-attached across the redirect hop"
        return httpx.Response(200, json={"rid": "rid-1"})

    c = mock_client(handler)
    assert c.remember("hello") == "rid-1"
    assert calls == ["follower", "leader"]
    assert c._leader_base == "http://leader:7438"

    # Second write goes straight to the leader — no redirect tax.
    calls.clear()
    assert c.remember("again") == "rid-1"
    assert calls == ["leader"]


def test_leaderless_307_raises_transient():
    """307 with no leader_addr (election in flight) is a transient condition,
    not a hard failure — and a write is never silently retried."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(307, json={"error": "not_leader", "leader_addr": None})

    c = mock_client(handler)
    with pytest.raises(TransientError):
        c.remember("x")
    assert calls == ["follower"], "a write must not be re-sent on a transient"


def test_redirect_hop_budget_exceeded_raises_not_leader():
    """A flapping 307 chain that keeps pointing elsewhere is bounded."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Every node claims someone *else* is the leader — infinite chase.
        nxt = f"http://n{len(request.url.host)}x:7438"
        return httpx.Response(
            307, json={"error": "not_leader", "leader_addr": nxt}
        )

    c = mock_client(handler)
    with pytest.raises(NotLeaderError):
        c.remember("x")


def test_readonly_retries_transient_then_succeeds():
    """recall (read-only) retries 503s; a write does not."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] <= 2:
            return httpx.Response(503, json={"error": "storage_failure"})
        return httpx.Response(200, json={"results": [], "total": 0})

    c = mock_client(handler)
    res = c.recall("anything")
    assert res.total == 0
    assert state["n"] == 3  # two 503s retried, third succeeded


def test_write_does_not_retry_transient():
    """A 503 on remember raises immediately — no double-write."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, json={"error": "commit_timeout"})

    c = mock_client(handler)
    with pytest.raises(TransientError):
        c.remember("x")
    assert len(calls) == 1, "the write must be attempted exactly once"


def test_sticky_leader_falls_back_to_seed_when_leader_unreachable():
    """If the discovered leader stops answering, the client re-seeds and
    rediscovers via the configured node instead of wedging."""
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "deadleader":
            raise httpx.ConnectError("connection refused")
        if host == "follower":
            return httpx.Response(
                307, json={"error": "not_leader",
                           "leader_addr": "http://newleader:7438"},
            )
        return httpx.Response(200, json={"rid": "ok"})

    c = mock_client(handler)
    c._leader_base = "http://deadleader:7438"  # stale sticky leader
    assert c.remember("x") == "ok"
    assert c._leader_base == "http://newleader:7438"
