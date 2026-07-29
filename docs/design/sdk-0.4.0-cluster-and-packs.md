# SDK 0.4.0 — cluster-correct transport + pack-context

**Status:** Draft → implementing · **Client:** yantrikdb-client · **Targets:** yantrikdb-server ≥ 0.14.0 (YRP replication, RFC 029/030/031)

## The gap

The SDK was last cut at v0.3.0 (Apr 2026), before the server's replication
era. Since then the server became a **Raft-shaped cluster** (YRP, the only
replication path since server v0.12.0) and grew **packs** (v0.14.0). The client
is unaware of both, and one of those gaps is a **silent correctness bug**, not
a missing feature.

### 1. Writes to a follower are silently dropped (correctness — the reason for this release)

When a write lands on a **follower**, the server tells the client where the
leader is via one of **two** signals — both carry the leader's HTTP base URL in
`leader_addr` (from `leader_hint()`), and the client follows both identically:

- **`503 Service Unavailable`** from the pre-commit `check_writable` guard —
  body `{"error":"read-only: not the leader (current leader: node N)",
  "leader_node_id":N,"leader_addr":"http://host:7438","raft_mode":…}`. **This is
  what a follower actually returns for a normal write** (verified live against
  the homelab cluster — the write is rejected *before* the commit path).
- **`307 Temporary Redirect`** from the commit path (`commit_error_to_app_error`)
  — body `{"error":"not_leader","leader_id":N,"leader_addr":"http://host:7438"}`.
  Fires if a write reaches the commit stage on a non-leader (a race).

The `Location` header is **not** populated yet (server PR 6.4), so following
relies on the body in both cases. A `503` with **no** `leader_addr` is a genuine
transient (storage blip, commit timeout, or an election with no leader elected).

The v0.3.0 transport builds `httpx.Client(...)` with `follow_redirects`
defaulted **off**, and `raise_for_status()` does **not** raise on 3xx. So
`_post` returns the `not_leader` body as if it were a success payload, and
`remember()` does `data["rid"]` on it → `KeyError`, or worse, a `.get()` path
returns `None` and the **write is silently lost**. A cluster (the default
topology now — the homelab, any multi-node deploy) is exactly where this bites.

Naive `follow_redirects=True` is **not** a correct fix: (a) there's no
`Location` header to follow, and (b) httpx **strips the `Authorization`
header on cross-host redirects** for security, so a followed redirect to a
different leader host would 401.

### 2. `pack-context` is invisible (the v0.14.0 headline feature)

`GET /v1/pack-context` is **token-authed** (normal tenant token, the SDK's
existing auth path) and returns
`{"pack_context": <str|null>, "packs_pending": [...], "packs_poisoned": [...]}`.
It's exactly what an agent wants — the mounted packs' constitution + coverage
to inject into its system prompt. The SDK exposes nothing.

### 3. No idempotency key, no transient-retry

The server accepts `idempotency_key` on `/v1/remember` — on both single-node
**and** YRP clusters (the keyed write rides consensus via
`remember_with_idempotency_yrp`; validated live). The client exposes neither the
key nor any retry.

## Design

Small, surgical, no new deps.

### Leader-hint follow (the fix)

`leader_addr` in the 307 body is a **full HTTP base URL** (the server's peer
`addr` config, used verbatim as a base — server `runtime.rs`). So the client
doesn't reconstruct anything:

- A private `_request(method, path, ...)` funnels every call. On a **307** or a
  **503 that carries `leader_addr`**, it reads that address and **replays the
  identical request** (method, body, and `Authorization` header — which we
  re-attach explicitly, sidestepping httpx's cross-host auth-strip) against
  `leader_addr + path`.
- **Sticky leader:** on a successful follow, the discovered base becomes the
  client's working base (`self._leader_base`), so subsequent calls go straight
  to the leader — no redirect tax per request. The originally-configured URL is
  retained as the fallback seed.
- **Bounded:** at most `_MAX_LEADER_HOPS` (2) follows per call. Beyond that (an
  election flapping leadership) → raise a clear `NotLeaderError`.
- **`leader_addr` absent** (election in progress, no leader yet) → treated as a
  transient condition (below), not an immediate hard error.

`follow_redirects` stays **off** on the httpx client — we follow deliberately,
with the token re-attached, never implicitly.

### Transient retry (503) — read-only only

`StorageFailure` / `CommitTimeout` / `Shutdown` and a leaderless 307 all surface
as retryable. Retry policy, deliberately conservative:

- **Read-only requests** (all GETs — `stats`, `personality`, `conflicts`,
  `health`, `pack-context`; and the read-only POST `recall`) → bounded
  exponential backoff (`_MAX_RETRIES` = 3, base 0.2s), then raise.
- **Writes** (`remember`, `relate`, `think`, …) → **not** auto-retried on 503.
  Note `think` is a write despite reading like a query — it consolidates and
  writes conflict rows, so re-running it is not idempotent. Silently
  re-POSTing a non-idempotent write is a correctness hazard (double-write). The
  error is raised; the caller retries, optionally with `idempotency_key` for
  at-least-once safety (works single-node and clustered).

This keeps the "no shortcuts on correctness" line: the client never turns one
memory into two behind the caller's back.

### `pack_context()` + `idempotency_key`

- `client.pack_context() -> PackContext` — GETs `/v1/pack-context`, returns a
  small dataclass `{context: str|None, pending: list[str], poisoned: list[str]}`.
  Convenience `pack_context_prompt()` returns just the `context` string (or `""`)
  for direct prompt injection.
- `remember(..., idempotency_key: str | None = None)` — pass-through. When set,
  the client adds it to the payload. Works on single-node **and** YRP clusters
  (the server replicates the keyed write through consensus). No client-side
  auto-retry is built on it — the caller decides when to retry — so behavior
  stays explicit. On a cluster the keyed write requires an embedding (server-side
  or client-supplied).

### Errors

New typed exceptions in `yantrikdb.errors` so callers can branch:
`YantrikError` (base) · `NotLeaderError` · `TransientError` · `IdempotencyConflict`.
`_request` maps status → exception; existing methods keep their return types.

## Non-goals (belong to the studio/CLI, not the agent SDK)

- Admin RBAC (login/users/tokens) and admin **pack management**
  (upload/mount/unmount). Those are the operator surface. The SDK stays the
  agent-facing memory client; it only *reads* pack-context.
- Async client — the SDK is sync today; an async twin is a separate follow-up.

## Test plan

Deterministic, no live server — httpx `MockTransport`:

1. **Leader-follow:** a mock that 307s the first POST with a `leader_addr`,
   then 200s → assert the write lands on the leader base, the `Authorization`
   header survived the hop, and the client stuck to the new base for the next
   call.
2. **Leaderless flap:** 307 with `leader_addr: null` repeated → `NotLeaderError`
   after the hop bound, not an infinite loop.
3. **Transient retry:** two 503s then 200 on `recall` → succeeds; the same on
   `remember` → raises without a second write.
4. **pack_context:** mock returns the documented body → dataclass parses,
   `pack_context_prompt()` returns the string.
5. **idempotency_key:** present in the payload only when passed.
