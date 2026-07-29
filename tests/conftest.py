"""Shared test helpers.

The client builds its own ``httpx.Client`` internally. For deterministic
transport tests we swap in an ``httpx.MockTransport`` after construction —
keeping the same headers and ``follow_redirects=False`` the client relies on,
so the leader-follow logic runs exactly as in production but against a scripted
server.
"""

from __future__ import annotations

import json as _json

import httpx

from yantrikdb.client import YantrikClient


def mock_client(handler, *, url: str = "http://follower:7438", token: str = "ydb_test"):
    """A YantrikClient whose transport is driven by ``handler(request)``.

    ``embedder=None`` so no embedding backend is loaded (tests need no torch).
    Retry backoff is zeroed so read-only retry tests don't actually sleep.
    """
    c = YantrikClient(url, token, embedder=None)
    c._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=c._auth,
        follow_redirects=False,
    )
    c._RETRY_BASE = 0.0  # instance override — no real sleeps
    return c


def body_of(request: httpx.Request) -> dict:
    """Decode a request's JSON body (empty dict if none)."""
    if not request.content:
        return {}
    return _json.loads(request.content)
