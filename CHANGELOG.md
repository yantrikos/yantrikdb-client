# Changelog

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
