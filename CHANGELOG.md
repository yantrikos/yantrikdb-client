# Changelog

## 0.4.0 — 2026-07-29

First update since the server's replication era. Makes the client **cluster-
correct** and exposes **packs**. Targets yantrikdb-server ≥ 0.14.0.

### Fixed (correctness — the reason for this release)
- **Writes to a cluster follower are no longer silently dropped.** A write
  that lands on a follower gets a `307 not_leader` whose body carries the
  leader's HTTP base URL. The old transport (redirects off, `raise_for_status`
  doesn't fire on 3xx) parsed that redirect body as a success payload and
  lost the write. The client now **follows the leader hint** — replaying the
  request against the leader with the `Authorization` header re-attached (httpx
  strips it on cross-host redirects, so naive `follow_redirects=True` would
  401) — and **sticks** to the discovered leader so later writes skip the
  redirect. Bounded to 2 hops; a stale sticky leader that stops answering
  re-seeds from the configured URL.

### New
- **`client.pack_context()`** → `PackContext{context, pending, poisoned}`, and
  **`client.pack_context_prompt()`** for the raw string. Fetches the mounted
  packs' constitution + coverage (server v0.14.0, `GET /v1/pack-context`,
  normal tenant token) so an agent can inject what pack knowledge it currently
  carries into its system prompt. `pending`/`poisoned` digests tell you when a
  node hasn't yet reconciled (or has quarantined) a pack, so you never
  advertise coverage a node can't serve.
- **`remember(..., idempotency_key=...)`** — pass-through for safe retries;
  a repeated store returns the original RID instead of writing twice. Supported
  on both single-node and YRP-clustered servers (validated live against a
  cluster — the keyed write replicates through consensus). On a cluster the
  write must carry an embedding (server-side embedder, or pass `embedding=[...]`).
  Reusing a key with *different* text raises `IdempotencyConflict`.
- **Automatic retry of transient `503`s for read-only calls** (GETs + `recall`)
  with bounded exponential backoff. Writes are **never** silently retried —
  re-sending a non-idempotent write is a double-write hazard; it raises so the
  caller decides.
- **Typed errors** (`yantrikdb.errors`): `YantrikError` (base), `NotLeaderError`,
  `TransientError`, `IdempotencyConflict`.

### Tests
- First test suite in the repo: `tests/` covers leader-follow + stickiness for
  **both** the 307 and the 503 `read-only` signals, leaderless/flap handling,
  the read-only-retry vs. no-write-retry split, sticky-leader failover,
  `pack_context` parsing, and `idempotency_key` pass-through/conflict — all
  deterministic via httpx `MockTransport`.
- **Validated live against the homelab YRP cluster** (`tests/integration_smoke.py`):
  a write aimed at a *follower* was followed to the leader and confirmed
  replicated (queried the leader directly), `pack_context` returned, and
  `idempotency_key` proved idempotent-replay + conflict on the cluster. Live
  testing caught two wrong assumptions before release — the follower's real
  signal is a **503** (not only a 307), and `idempotency_key` **is** supported
  on YRP clusters (not single-node-only).

### Unchanged
- Every existing method keeps its signature and return type. Default embedder,
  the `[embed]` / `[embed-tiny]` extras, and the character-substrate helpers
  are untouched. A single-node deployment sees no behavioral change beyond the
  new methods.

## 0.3.0 — 2026-04-21

### New
- **`[embed-tiny]` extra — model2vec static-embedding backend.** ~30MB,
  pure numpy, no torch, no onnxruntime. Installs in seconds on Python 3.13,
  where the `[embed]` (sentence-transformers → fastembed → onnxruntime)
  path can trigger a 30+ minute source compile because no cp313 wheels
  exist yet.
- `_embed()` auto-routes to the right backend by name convention:
  names starting with `minishlab/` or containing `potion` use model2vec;
  everything else uses sentence-transformers.
- Exported constants: `DEFAULT_EMBEDDER` (`"all-MiniLM-L6-v2"`) and
  `ALT_EMBEDDER_TINY` (`"minishlab/potion-base-8M"`).
- Friendlier error messages when the required embedder backend isn't
  installed — they point at both `[embed]` and `[embed-tiny]`.

### Unchanged
- Default embedder remains `"all-MiniLM-L6-v2"` (384 dim), matching
  YantrikDB's server-side default HNSW dim. Fresh default client +
  fresh default server still `just work`.

### Opt-in: model2vec on Python 3.13
If Python 3.13 makes `[embed]` impractical, use the lightweight path —
but note it **requires a matching server config**:

```bash
pip install yantrikdb-client[embed-tiny]
```

```python
from yantrikdb import ALT_EMBEDDER_TINY, connect
client = connect(url, token=..., embedder=ALT_EMBEDDER_TINY)
```

```toml
# yantrikdb server config (server-side dim must match client embedder)
[embedding]
strategy = "client_only"
dim = 256   # potion-base-8M outputs 256-dim vectors
```

Without the matching `dim` setting, `remember()` will fail with a 500
(HNSW dimension-mismatch panic server-side) on first insert.

### Validation
End-to-end against a real YantrikDB server with real model2vec embeddings
on Python 3.13 (no monkey-patching):
- remember() 6 items, avg 156ms/call
- recall('auth and passwords') → auth-related fixtures both in top-2
  (scores 1.20 + 1.06 vs ≤0.55 for unrelated), 2× margin
- recall_typed(memory_type='episodic') returns only episodic, no contamination
- reflect() composes rule + constraint correctly

### Why this release
- YantrikDB plugin went live on Cursor Directory (300k+ devs). Python 3.13
  is increasingly the default on fresh setups (Debian 13, Ubuntu 24.10+).
  Giving those users a supported install path — even as an opt-in — matters.
- The default path (`[embed]` + MiniLM) was deliberately kept intact to
  avoid surprising existing users with silent dim mismatches.

## 0.2.1 — 2026-04-20

### Bugfix
- **`reflect()` leaked DB-wide conflicts into every prompt.** When
  `include_conflicts=True` (the previous default), `reflect().render()`
  would dump every open conflict in the whole database into the
  rendered context, ignoring the `namespace` argument. Surfaced during
  a teach-Qwen POC where a single namespace had accumulated ~50
  cross-session conflicts and each reflection call injected the full
  list into the LLM prompt. Fix:
  - `conflicts()` now accepts `namespace`, `status`, and `limit` kwargs
    and passes them to the server so filtering is actually server-side.
  - `reflect()` now passes its `namespace` through to `conflicts()`.
  - `reflect()` gains `max_conflicts: int = 5` to cap the list even
    when callers opt into conflict surfacing.

### Breaking default change (SemVer patch-level because 0.2.0 was
hours old with no known production users, and the old default was a
bug):
- **`reflect(include_conflicts=...)` default changed from `True` to
  `False`.** Most reasoning-context callers don't want a conflict list
  injected into their LLM prompt. If you were relying on the old
  default, pass `include_conflicts=True` explicitly.

## 0.2.0 — 2026-04-20

### New
- Character-substrate primitives over `memory_type` conventions:
  `remember_self`, `remember_rule`, `remember_hypothesis`,
  `remember_constraint`, `remember_goal`, `remember_arc`,
  `record_signal`.
- Typed retrieval: `recall_typed(query, memory_type)`.
- `reflect(question)` composing seven parallel type-filtered recalls
  into a `Reflection` dataclass with `.render()` for LLM prompts.
- Lazy auto-embedder: `connect(url, token=..., embedder="all-MiniLM-L6-v2")`
  uses sentence-transformers if installed. Pass `embedder=None` to
  disable and supply embeddings manually. Install via
  `pip install yantrikdb-client[embed]`.
- Exports: `Reflection`, `CHARACTER_TYPES`.

### Validated via
- n=10 blind-judged benchmark (GPT-5.4 judge): character condition
  wins memory-citation 100% non-tie, discrimination 90% non-tie,
  temptation-resistance 100% non-tie versus stateless at tied raw
  accuracy.

## 0.1.0
- Initial release.
