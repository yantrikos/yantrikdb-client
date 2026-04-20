# Changelog

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
