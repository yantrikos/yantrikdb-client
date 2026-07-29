"""YantrikDB Python client — talks to the HTTP gateway."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .errors import IdempotencyConflict, NotLeaderError, TransientError
from .types import (
    Edge,
    Memory,
    RecallResult,
    Reflection,
    SessionSummary,
    Stats,
    ThinkResult,
)


DEFAULT_EMBEDDER = "all-MiniLM-L6-v2"
"""Default embedder — sentence-transformers MiniLM (384 dim).

Chosen because it matches YantrikDB's default server-side HNSW index
dimension (384). Fresh client + fresh server `just work`.

For Python 3.13+ (where sentence-transformers' fastembed/onnxruntime
dep chain compiles from source), opt into the lightweight static-embedding
backend:

    pip install yantrikdb-client[embed-tiny]
    connect(url, token=..., embedder="minishlab/potion-base-8M")

But note: potion-base-8M emits 256-dim vectors, so the server must be
configured to match:

    [embedding]
    dim = 256

See README and CHANGELOG for the full opt-in migration steps.
"""

ALT_EMBEDDER_TINY = "minishlab/potion-base-8M"
"""Recommended lightweight (model2vec) embedder — pair with [embed-tiny] extra
and server `dim = 256`. See DEFAULT_EMBEDDER docstring."""


@dataclass
class PackContext:
    """The mounted packs' shared context for the caller's tenant.

    Returned by :meth:`YantrikClient.pack_context`. ``context`` is the packs'
    combined constitution + coverage block — inject it into an agent's system
    prompt so the model knows what knowledge it currently carries. ``pending``
    and ``poisoned`` are digests of packs the manifest says should be mounted
    but that this node has not yet reconciled (fetching/mounting) or has
    quarantined (a bad pack) — surfaced so an agent never assumes coverage it
    can't actually serve.
    """

    context: str | None
    pending: list[str]
    poisoned: list[str]

    @property
    def prompt(self) -> str:
        """The context string ready for prompt injection (empty if none)."""
        return self.context or ""


def connect(
    url: str = "http://localhost:7438",
    *,
    token: str,
    embedder: str | None = DEFAULT_EMBEDDER,
) -> YantrikClient:
    """Connect to a YantrikDB server.

    Args:
        url: Server URL. Supports:
            - http://host:port (HTTP gateway, default)
            - yantrik://host:port (wire protocol port — auto-adjusts to HTTP +1)
        token: Authentication token (ydb_...).
        embedder: Embedder model name for client-side embedding. The backend
            is selected automatically from the name:
            - sentence-transformers names (default 'all-MiniLM-L6-v2', 384 dim)
              — requires `yantrikdb-client[embed]`, pulls torch. Matches
              the default server HNSW dim.
            - model2vec names (e.g. 'minishlab/potion-base-8M', 256 dim) —
              requires `yantrikdb-client[embed-tiny]`, pure numpy, py3.13-friendly.
              The server must be configured with matching `[embedding] dim`.
            Pass None to disable auto-embedding and supply `embedding=[...]`
            explicitly on every remember()/recall() call.

            IMPORTANT: client embedder output dim MUST match the server's
            HNSW dim or remember() will hit a 500 (server panic on insert).
    """
    parsed = urlparse(url)
    if parsed.scheme in ("yantrik", "yantrik+tls"):
        port = (parsed.port or 7437) + 1
        http_url = f"http://{parsed.hostname}:{port}"
    else:
        http_url = url.rstrip("/")

    return YantrikClient(http_url, token, embedder=embedder)


class YantrikClient:
    """Client for YantrikDB HTTP gateway."""

    def __init__(self, base_url: str, token: str, *, embedder: str | None = None):
        # The URL the caller configured. Retained as the fallback seed if a
        # discovered leader later becomes unreachable.
        self._seed_base = base_url.rstrip("/")
        self._base = self._seed_base
        # The node we currently believe is the leader. Updated on every 307
        # `not_leader` redirect so subsequent writes go straight there (sticky
        # leader) instead of paying the redirect tax each call.
        self._leader_base = self._seed_base
        self._token = token
        self._auth = {"Authorization": f"Bearer {token}"}
        # follow_redirects stays OFF: the cluster's 307 carries the leader in
        # the *body* (no Location header yet), and httpx would strip our
        # Authorization header on a cross-host redirect. We follow deliberately
        # in `_send_following_leader`, re-attaching the token every hop.
        self._client = httpx.Client(
            headers=self._auth,
            timeout=30.0,
            follow_redirects=False,
        )
        self._embedder_name = embedder
        self._embedder = None  # lazy

    def _embed(self, text: str) -> list[float] | None:
        """Lazily load the configured embedder and encode text.

        Backend is routed by embedder-name convention:
          - names containing 'minishlab/' or 'potion' → model2vec (pure numpy)
          - anything else → sentence-transformers

        Returns None if auto-embedding is disabled (embedder=None).
        """
        if self._embedder_name is None:
            return None
        if self._embedder is None:
            self._embedder = self._load_embedder(self._embedder_name)
        return self._embedder(text)

    def _load_embedder(self, name: str):
        """Resolve backend and return a callable(text) -> list[float]."""
        is_model2vec = name.startswith("minishlab/") or "potion" in name.lower()
        if is_model2vec:
            try:
                from model2vec import StaticModel
            except ImportError as e:
                raise RuntimeError(
                    f"Embedder '{name}' requires the model2vec backend.\n"
                    "Install it with:\n"
                    "  pip install yantrikdb-client[embed-tiny]\n"
                    "Or pass embedder=None to connect() and supply embeddings manually."
                ) from e
            model = StaticModel.from_pretrained(name)

            def encode(text: str) -> list[float]:
                vec = model.encode(text)
                # Normalize 2D (batch) → 1D single result
                if hasattr(vec, "ndim") and vec.ndim == 2:
                    vec = vec[0]
                return [float(x) for x in vec.tolist()]

            return encode

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                f"Embedder '{name}' requires the sentence-transformers backend.\n"
                "Install options:\n"
                "  pip install yantrikdb-client[embed-tiny]  (recommended — ~30MB, py3.13-friendly)\n"
                "  pip install yantrikdb-client[embed]        (sentence-transformers + torch, ~800MB)\n"
                "Or pass embedder=None to connect() and supply embeddings manually."
            ) from e
        model = SentenceTransformer(name)

        def encode(text: str) -> list[float]:
            vec = model.encode(text, convert_to_numpy=True, normalize_embeddings=False)
            return [float(x) for x in vec.tolist()]

        return encode

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Transport (leader-following, retrying) ────────────
    #
    # Every call funnels through `_request`. Two concerns are handled here so
    # no endpoint method has to:
    #   1. Leader redirects (307 not_leader) — follow to the leader carried in
    #      the body, re-attaching the token, and stick to it (see __init__).
    #   2. Transient 503s — retried for read-only calls only. A non-idempotent
    #      write is NEVER silently re-sent (that would double-write); it raises
    #      and the caller decides.

    _MAX_LEADER_HOPS = 2
    _MAX_RETRIES = 3
    _RETRY_BASE = 0.2  # seconds; exponential

    @staticmethod
    def _safe_json(r: httpx.Response) -> dict:
        try:
            body = r.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    def _send_following_leader(self, method: str, path: str, json: dict | None) -> dict:
        """Send one logical request, following `not_leader` redirects.

        The 307 body carries `leader_addr` as a full HTTP base URL. We swap to
        it, remember it (sticky), and replay the identical request there.
        """
        hops = 0
        base = self._leader_base
        while True:
            url = base.rstrip("/") + path
            try:
                r = self._client.request(method, url, json=json, headers=self._auth)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # A sticky leader that went away: fall back to the configured
                # seed once and let it redirect us to the new leader.
                if base != self._seed_base:
                    base = self._seed_base
                    self._leader_base = self._seed_base
                    continue
                raise TransientError(f"cannot reach server: {e}") from e

            if r.status_code == 307:
                body = self._safe_json(r)
                leader = body.get("leader_addr")
                if not leader:
                    # Election in progress — no leader to point at yet.
                    raise TransientError(
                        "no leader elected yet", status=307, body=body
                    )
                if hops >= self._MAX_LEADER_HOPS:
                    raise NotLeaderError(
                        "leader redirect exceeded hop budget",
                        leader_addr=leader,
                        status=307,
                        body=body,
                    )
                base = leader.rstrip("/")
                self._leader_base = base  # sticky
                hops += 1
                continue

            if r.status_code == 503:
                body = self._safe_json(r)
                raise TransientError(
                    body.get("error", "service unavailable"),
                    status=503,
                    body=body,
                )

            r.raise_for_status()
            return r.json()

    def _request(
        self, method: str, path: str, *, json: dict | None = None, read_only: bool = False
    ) -> dict:
        attempt = 0
        while True:
            try:
                return self._send_following_leader(method, path, json)
            except TransientError:
                if read_only and attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE * (2 ** attempt))
                    attempt += 1
                    continue
                raise

    def _post(self, path: str, json: dict, *, read_only: bool = False) -> dict:
        return self._request("POST", path, json=json, read_only=read_only)

    def _get(self, path: str) -> dict:
        return self._request("GET", path, read_only=True)

    def _delete(self, path: str, json: dict | None = None) -> dict:
        return self._request("DELETE", path, json=json)

    # ── Memory ────────────────────────────────────────────

    def remember(
        self,
        text: str,
        *,
        importance: float = 0.5,
        memory_type: str = "semantic",
        domain: str = "",
        source: str = "user",
        namespace: str = "",
        metadata: dict | None = None,
        valence: float = 0.0,
        half_life: float = 168.0,
        certainty: float = 1.0,
        emotional_state: str | None = None,
        embedding: list[float] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Store a memory. Returns the memory RID.

        Args:
            idempotency_key: if set, the server dedupes repeated stores with the
                same key — a safe retry returns the original RID rather than
                writing twice. NOTE: single-node servers only; a clustered
                server currently rejects this with a 400 (see server RFC 029).
                Reusing a key with *different* text raises
                :class:`~yantrikdb.errors.IdempotencyConflict`.
        """
        payload: dict[str, Any] = {
            "text": text,
            "importance": importance,
            "memory_type": memory_type,
            "domain": domain,
            "source": source,
            "namespace": namespace,
            "metadata": metadata or {},
            "valence": valence,
            "half_life": half_life,
            "certainty": certainty,
        }
        if emotional_state:
            payload["emotional_state"] = emotional_state
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        if embedding is None:
            embedding = self._embed(text)
        if embedding:
            payload["embedding"] = embedding

        data = self._post("/v1/remember", payload)
        if data.get("idempotency_conflict"):
            raise IdempotencyConflict(
                f"idempotency_key {idempotency_key!r} was reused with different content",
                status=200,
                body=data,
            )
        return data["rid"]

    def recall(
        self,
        query: str,
        *,
        top_k: int = 10,
        domain: str | None = None,
        source: str | None = None,
        namespace: str | None = None,
        memory_type: str | None = None,
        include_consolidated: bool = False,
        expand_entities: bool = True,
    ) -> RecallResult:
        """Semantic recall. Returns ranked results with explanations."""
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "include_consolidated": include_consolidated,
            "expand_entities": expand_entities,
        }
        if domain:
            payload["domain"] = domain
        if source:
            payload["source"] = source
        if namespace:
            payload["namespace"] = namespace
        if memory_type:
            payload["memory_type"] = memory_type

        # Auto-embed the query for vector recall
        emb = self._embed(query)
        if emb is not None:
            payload["query_embedding"] = emb

        data = self._post("/v1/recall", payload, read_only=True)
        results = [Memory(**r) for r in data["results"]]
        return RecallResult(results=results, total=data["total"])

    def forget(self, rid: str) -> bool:
        """Tombstone a memory. Returns True if found."""
        data = self._post("/v1/forget", {"rid": rid})
        return data.get("found", False)

    # ── Graph ─────────────────────────────────────────────

    def relate(
        self,
        entity: str,
        target: str,
        relationship: str,
        *,
        weight: float = 1.0,
    ) -> str:
        """Create a knowledge graph edge. Returns edge ID."""
        data = self._post("/v1/relate", {
            "entity": entity,
            "target": target,
            "relationship": relationship,
            "weight": weight,
        })
        return data["edge_id"]

    # ── Session ───────────────────────────────────────────

    @contextmanager
    def session(
        self,
        namespace: str = "default",
        client_id: str = "",
        metadata: dict | None = None,
    ):
        """Context manager for cognitive sessions."""
        data = self._post("/v1/sessions", {
            "namespace": namespace,
            "client_id": client_id,
            "metadata": metadata or {},
        })
        sid = data["session_id"]
        try:
            yield _Session(self, sid)
        finally:
            self._delete(f"/v1/sessions/{sid}")

    # ── Cognition ─────────────────────────────────────────

    def think(
        self,
        *,
        run_consolidation: bool = True,
        run_conflict_scan: bool = True,
        run_pattern_mining: bool = False,
        run_personality: bool = False,
        consolidation_limit: int = 50,
    ) -> ThinkResult:
        """Trigger the cognitive loop."""
        data = self._post("/v1/think", {
            "run_consolidation": run_consolidation,
            "run_conflict_scan": run_conflict_scan,
            "run_pattern_mining": run_pattern_mining,
            "run_personality": run_personality,
            "consolidation_limit": consolidation_limit,
        })
        return ThinkResult(**data)

    # ── Info ──────────────────────────────────────────────

    def stats(self) -> Stats:
        """Get engine statistics."""
        data = self._get("/v1/stats")
        return Stats(**data)

    def personality(self) -> list[dict]:
        """Get derived personality traits."""
        data = self._get("/v1/personality")
        return data.get("traits", [])

    def conflicts(
        self,
        *,
        namespace: str | None = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[dict]:
        """List conflicts, optionally filtered by namespace.

        Args:
            namespace: if given, only return conflicts whose memories
                are in this namespace. Prior to v0.2.1 this kwarg did
                not exist and callers got DB-wide conflicts regardless
                of scope — see reflect() bugfix in CHANGELOG.
            status: "open" (default), "resolved", or "all"
            limit: server-side limit; default 100
        """
        params = {"status": status, "limit": str(limit)}
        if namespace is not None:
            params["namespace"] = namespace
        path = "/v1/conflicts"
        # Build query string manually to respect the existing _get signature
        from urllib.parse import urlencode
        data = self._get(f"{path}?{urlencode(params)}")
        return data.get("conflicts", [])

    def health(self) -> dict:
        """Check server health."""
        return self._get("/v1/health")

    # ── Packs ─────────────────────────────────────────────

    def pack_context(self) -> PackContext:
        """Fetch the mounted packs' constitution + coverage for this tenant.

        An agent injects :attr:`PackContext.prompt` into its system prompt so
        the model knows what pack knowledge it currently carries. Uses the
        normal tenant token (same auth as recall/remember). Packs whose mount
        hasn't reconciled on this node yet appear in ``pending``; quarantined
        ones in ``poisoned`` — so you never advertise coverage a node can't
        actually serve.
        """
        data = self._get("/v1/pack-context")
        return PackContext(
            context=data.get("pack_context"),
            pending=data.get("packs_pending") or [],
            poisoned=data.get("packs_poisoned") or [],
        )

    def pack_context_prompt(self) -> str:
        """Convenience: just the pack-context string for prompt injection."""
        return self.pack_context().prompt

    # ── Character-substrate primitives ────────────────────────
    # Typed helpers over memory_type conventions. Engine storage is
    # identical to other memory types; these helpers give agents
    # intent-legible authoring and recall of the classes of state
    # that make longitudinal identity work.

    def remember_self(
        self,
        content: str,
        *,
        confidence: float = 0.8,
        namespace: str = "",
        domain: str = "",
        source: str = "agent_reflection",
        metadata: dict | None = None,
    ) -> str:
        """Store a self-model claim — what the agent is / tends to do /
        can't do / values. Example: `remember_self("I overtrust recent
        single-source reports")`.

        `confidence` goes into the `certainty` field; the agent should
        lower it when evidence contradicts and raise it on corroboration.
        """
        return self.remember(
            content, memory_type="self_model",
            importance=0.7, certainty=confidence,
            namespace=namespace, domain=domain, source=source,
            metadata={**(metadata or {}), "class": "self_model"},
        )

    def remember_rule(
        self,
        condition: str,
        action: str,
        *,
        confidence: float = 0.7,
        namespace: str = "",
        source: str = "agent_reflection",
        metadata: dict | None = None,
    ) -> str:
        """Store a policy rule: "when <condition>, <action>".

        Rules are the agent's learned heuristics. They should be
        revised when outcomes disconfirm them — record a learning
        signal and reduce the rule's certainty.
        """
        text = f"WHEN {condition} THEN {action}"
        return self.remember(
            text, memory_type="rule",
            importance=0.7, certainty=confidence,
            namespace=namespace, source=source,
            metadata={**(metadata or {}), "class": "rule",
                      "condition": condition, "action": action},
        )

    def remember_hypothesis(
        self,
        statement: str,
        *,
        confidence: float = 0.4,
        namespace: str = "",
        source: str = "agent_reflection",
        metadata: dict | None = None,
    ) -> str:
        """Store a tentative belief ("maybe A causes B"). Distinct from
        observation (fact) and rule (policy). Agents should elevate to
        belief on corroboration or retract on disconfirmation."""
        return self.remember(
            statement, memory_type="hypothesis",
            importance=0.5, certainty=confidence,
            namespace=namespace, source=source,
            metadata={**(metadata or {}), "class": "hypothesis"},
        )

    def remember_constraint(
        self,
        label: str,
        description: str,
        *,
        priority: float = 0.9,
        namespace: str = "",
        source: str = "user_authored",
        metadata: dict | None = None,
    ) -> str:
        """Store a user- or agent-authored commitment that should
        survive friction: "truthfulness over user-pleasing",
        "no irreversible action without confirmation".

        `priority` governs which constraint wins in conflicts. The
        engine does not enforce; it stores and ranks — enforcement
        is the caller's responsibility via recall before action.
        """
        text = f"[{label}] {description}"
        return self.remember(
            text, memory_type="constraint",
            importance=priority, certainty=1.0,
            namespace=namespace, source=source,
            metadata={**(metadata or {}), "class": "constraint",
                      "label": label, "priority": priority},
        )

    def remember_goal(
        self,
        label: str,
        description: str,
        *,
        deadline: float | None = None,
        priority: float = 0.7,
        namespace: str = "",
        source: str = "user_authored",
        metadata: dict | None = None,
    ) -> str:
        """Store a goal-state memory. Unlike constraints, goals are
        bounded objectives that can be achieved, abandoned, or
        superseded. Use `record_signal` on outcome to feed back."""
        text = f"[GOAL {label}] {description}"
        meta = {**(metadata or {}), "class": "goal", "label": label,
                "status": "active", "priority": priority}
        if deadline is not None:
            meta["deadline"] = deadline
        return self.remember(
            text, memory_type="goal",
            importance=priority, certainty=1.0,
            namespace=namespace, source=source, metadata=meta,
        )

    def remember_arc(
        self,
        name: str,
        description: str,
        *,
        status: str = "open",
        namespace: str = "",
        source: str = "agent_narrative",
        metadata: dict | None = None,
    ) -> str:
        """Store a narrative arc / open thread: a storyline the agent
        is currently inside. Self-model is "who I am"; narrative arc
        is "what story I'm in the middle of". Status transitions:
        open → tension → resolved | abandoned."""
        text = f"[ARC {name}] {description}"
        return self.remember(
            text, memory_type="narrative_arc",
            importance=0.6, certainty=1.0,
            namespace=namespace, source=source,
            metadata={**(metadata or {}), "class": "narrative_arc",
                      "name": name, "status": status},
        )

    def record_signal(
        self,
        kind: str,
        content: str,
        *,
        valence: float = 0.0,
        about_rid: str | None = None,
        namespace: str = "",
        source: str = "outcome_feedback",
        metadata: dict | None = None,
    ) -> str:
        """Record a learning signal — reward, punishment, confirmation,
        disconfirmation, source-trust delta, regret, calibration error.

        `valence` is the affective charge (+1 strong positive, -1
        strong negative). `about_rid`, if given, links the signal to
        the memory it updates — enabling "which rule just earned a
        reinforcement" queries.

        `kind` is a free-form tag: "reward", "disconfirm", "regret",
        "trust_up", "trust_down", "calibration_error", etc.
        """
        text = f"[{kind}] {content}"
        meta = {**(metadata or {}), "class": "learning_signal", "kind": kind}
        if about_rid is not None:
            meta["about_rid"] = about_rid
        return self.remember(
            text, memory_type="learning_signal",
            importance=0.5, valence=valence, certainty=1.0,
            namespace=namespace, source=source, metadata=meta,
        )

    def recall_typed(
        self,
        query: str,
        memory_type: str,
        *,
        top_k: int = 5,
        namespace: str | None = None,
    ) -> list[Memory]:
        """Convenience: recall filtered to a single character-substrate
        memory_type. Thin wrapper over `recall()` for legibility."""
        return self.recall(
            query, top_k=top_k, namespace=namespace,
            memory_type=memory_type, expand_entities=False,
        ).results

    def reflect(
        self,
        question: str,
        *,
        namespace: str | None = None,
        top_k_per_type: int = 5,
        include_conflicts: bool = False,
        max_conflicts: int = 5,
    ) -> Reflection:
        """Compose a meta-state view by running parallel type-filtered
        recalls against the same question. Returns a structured
        `Reflection` ready to render into an LLM prompt.

        This is the key operation for "agent reads its own memory to
        reflect". The agent sees WHO IT IS (self-model), WHAT IT
        BELIEVES (rules + hypotheses), WHAT IT'S COMMITTED TO
        (constraints + goals), WHAT STORY IT'S IN (arcs), and WHAT
        JUST HAPPENED (recent signals) — all relevant to `question`.

        Args:
            question: semantic query threaded through every type-filtered recall
            namespace: if given, scopes ALL recalls AND the conflicts query
                (the latter is new in v0.2.1 — prior versions leaked DB-wide
                conflicts into every reflection regardless of namespace)
            top_k_per_type: recall depth per memory type
            include_conflicts: whether to fetch open conflicts. Default
                CHANGED to False in v0.2.1 (was True in v0.2.0). Most
                reasoning-context callers don't want a conflict list
                injected into their LLM prompt; opt in explicitly if you
                do. When True, conflicts are namespace-scoped and capped
                at max_conflicts.
            max_conflicts: when include_conflicts is True, cap the list
                at this many most-recent conflicts. Prevents dumping
                hundreds of accumulated conflicts into an LLM prompt.
        """
        def pull(mt: str) -> list[Memory]:
            return self.recall_typed(
                question, mt, top_k=top_k_per_type, namespace=namespace,
            )
        reflection = Reflection(
            question=question,
            self_model=pull("self_model"),
            rules=pull("rule"),
            hypotheses=pull("hypothesis"),
            constraints=pull("constraint"),
            goals=pull("goal"),
            arcs=pull("narrative_arc"),
            recent_signals=pull("learning_signal"),
        )
        if include_conflicts:
            try:
                conflicts = self.conflicts(namespace=namespace, limit=max_conflicts)
                reflection.open_conflicts = conflicts[:max_conflicts]
            except Exception:
                reflection.open_conflicts = []
        return reflection


class _Session:
    """A cognitive session — memories created within are linked."""

    def __init__(self, client: YantrikClient, session_id: str):
        self._client = client
        self.session_id = session_id

    def remember(self, text: str, **kwargs) -> str:
        return self._client.remember(text, **kwargs)

    def recall(self, query: str, **kwargs) -> RecallResult:
        return self._client.recall(query, **kwargs)
