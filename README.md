# yantrikdb-client

Python SDK for [YantrikDB](https://github.com/yantrikos/yantrikdb) — a
cognitive memory database with persistent typed memory, contradiction
handling, and reflection.

## Install

```bash
# Base (bring your own embeddings)
pip install yantrikdb-client

# Default: sentence-transformers MiniLM (384 dim). Matches the default
# YantrikDB server HNSW dim. Works on Python <= 3.12 smoothly; on Python
# 3.13 may trigger a long onnxruntime source compile via the fastembed
# dep chain.
pip install 'yantrikdb-client[embed]'

# Lightweight: model2vec static embedding (~30MB, pure numpy, no torch,
# no onnxruntime, installs in seconds on Python 3.13+). Opt-in — the
# server must be configured with a matching [embedding] dim = 256.
pip install 'yantrikdb-client[embed-tiny]'
```

### Python 3.13 opt-in (model2vec)

If Python 3.13 makes the default `[embed]` install impractical, use the
lightweight path:

```python
from yantrikdb import ALT_EMBEDDER_TINY, connect
client = connect(url, token=..., embedder=ALT_EMBEDDER_TINY)
```

And on the server:

```toml
[embedding]
strategy = "client_only"
dim = 256   # potion-base-8M outputs 256-dim vectors
```

**Client embedder output dim MUST match the server's HNSW dim.** Otherwise
`remember()` will return a 500 on first insert (server panics on
dimension mismatch — default server dim is 384).

## Quick start

```python
from yantrikdb import connect

client = connect("http://localhost:7438", token="ydb_...")

# Basic memory
client.remember("Alice prefers dark mode", domain="preference")
results = client.recall("what does Alice prefer?")

# Character-substrate primitives (v0.2.0+)
client.remember_self("I overtrust single-source reports under time pressure")
client.remember_rule(
    condition="single-source high-stakes claim",
    action="state uncertainty and request corroboration",
)
client.remember_constraint(
    label="truthfulness_over_pleasing",
    description="Disclose uncertainty even when unwelcome",
    priority=0.95,
)

# Reflect — compose a structured meta-state view for an LLM prompt
reflection = client.reflect(
    "How should I handle this high-stakes single-source claim?",
)
print(reflection.render())
```

## What's in 0.3.0

- **`[embed-tiny]` extra**: model2vec static embedding backend — ~30MB,
  pure numpy, no torch, no onnxruntime. Works on Python 3.13+. Now the
  default.
- Auto-routing: embedder name selects the backend automatically (model2vec
  for `minishlab/...` and `*potion*` names, sentence-transformers otherwise).

## What's in 0.2.0

- **Character-substrate primitives**: `remember_self`, `remember_rule`,
  `remember_hypothesis`, `remember_constraint`, `remember_goal`,
  `remember_arc`, `record_signal`
- **Typed recall**: `recall_typed(query, memory_type)` for filtered
  retrieval
- **Reflect API**: `reflect(question)` composes parallel type-filtered
  recalls + open conflicts into a `Reflection` with `.render()` for
  LLM prompts
- **Auto-embedder**: client-side embedding via sentence-transformers.

## API surface

- `connect(url, *, token, embedder=...)` — returns a `YantrikClient`
- `YantrikClient.remember(text, ...)` — store a memory
- `YantrikClient.recall(query, ...)` — semantic search
- `YantrikClient.relate(entity, target, relationship)` — knowledge graph edge
- `YantrikClient.think(...)` — trigger consolidation / conflict scan
- `YantrikClient.reflect(question, ...)` — structured meta-state view
- Typed helpers: `remember_self/rule/hypothesis/constraint/goal/arc`,
  `record_signal`, `recall_typed`
- `YantrikClient.session(...)` — context manager for cognitive sessions

## License

MIT
