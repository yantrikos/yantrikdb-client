"""Typed exceptions for the YantrikDB client.

These let callers branch on *why* a request failed — a leadership change vs. a
transient storage blip vs. an idempotency conflict — instead of catching a raw
``httpx.HTTPStatusError`` and re-parsing the body.
"""

from __future__ import annotations

from typing import Optional


class YantrikError(Exception):
    """Base class for all client-raised errors."""

    def __init__(self, message: str, *, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class NotLeaderError(YantrikError):
    """A write could not be routed to a leader.

    Raised when the cluster returned ``307 not_leader`` but the client could
    not complete the redirect within its hop budget — typically because a
    leader election is in flight (``leader_addr`` was absent) or leadership is
    flapping. The write did **not** apply; retry once the cluster settles.
    """

    def __init__(self, message: str, *, leader_addr: str | None = None, **kw):
        super().__init__(message, **kw)
        self.leader_addr = leader_addr


class TransientError(YantrikError):
    """A transient, retryable server condition (503).

    Storage blip, commit timeout, or a node shutting down. Read-only calls
    retry these automatically; writes surface them so the caller decides
    (idempotency is the caller's to guarantee).
    """


class IdempotencyConflict(YantrikError):
    """The same ``idempotency_key`` was reused with different content.

    The server refused to overwrite the earlier record. This is a client bug —
    a key must map to exactly one payload.
    """


__all__ = [
    "YantrikError",
    "NotLeaderError",
    "TransientError",
    "IdempotencyConflict",
]
