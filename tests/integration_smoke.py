"""Live integration smoke — exercises the SDK against a REAL clustered server.

Unlike the unit tests (httpx MockTransport), this points the client at an
actual YantrikDB cluster and proves the v0.4.0 behavior end-to-end — most
importantly that a write aimed at a *follower* is followed to the leader and
truly replicates (the silent-drop bug this release fixes).

Not part of the default pytest run (needs a live cluster). Run manually:

    YDB_FOLLOWER=http://<follower>:7438 \
    YDB_LEADER=http://<leader>:7438 \
    YDB_TOKEN=ydb_... \
    python tests/integration_smoke.py
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx

from yantrikdb import connect
from yantrikdb.errors import IdempotencyConflict, YantrikError


def main() -> int:
    follower = os.environ["YDB_FOLLOWER"]
    leader = os.environ["YDB_LEADER"]
    token = os.environ["YDB_TOKEN"]

    # embedder=None → the server embeds text-only writes/queries server-side.
    client = connect(follower, token=token, embedder=None)
    marker = f"sdk-smoke-{uuid.uuid4().hex[:12]}"
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        ok = ok and cond

    print(f"1. remember() aimed at the FOLLOWER {follower} (leader-follow):")
    rid = client.remember(
        f"Integration smoke marker {marker}", domain="sdk_smoke", importance=0.4
    )
    check("write returned an rid", bool(rid), rid)
    check("client stuck to the leader after the 307", client._leader_base == leader,
          f"_leader_base={client._leader_base}")

    print("2. recall() the write back:")
    res = client.recall(marker, top_k=5)
    found = any(marker in (m.text or "") for m in res.results)
    check("recalled the just-written memory", found, f"{len(res.results)} results")

    print("3. verify the write actually REPLICATED (query the leader directly):")
    r = httpx.post(
        f"{leader}/v1/recall",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": marker, "top_k": 5},
        timeout=15,
    )
    leader_found = r.status_code == 200 and any(
        marker in (m.get("text") or "") for m in r.json().get("results", [])
    )
    check("write is present on the leader (replicated, not dropped)", leader_found)

    print("4. pack_context():")
    ctx = client.pack_context()
    check("pack_context returns a PackContext", hasattr(ctx, "pending"),
          f"context={ctx.context!r} pending={ctx.pending} poisoned={ctx.poisoned}")

    print("5. idempotency_key on a YRP CLUSTER (supported — replicated keyed op):")
    key = f"smoke-key-{marker}"
    rid_a = client.remember(f"idem body {marker}", idempotency_key=key)
    rid_b = client.remember(f"idem body {marker}", idempotency_key=key)
    check("same key + same text is idempotent (same rid)", rid_a == rid_b,
          f"{rid_a} == {rid_b}")
    try:
        client.remember(f"DIFFERENT body {marker}", idempotency_key=key)
        check("same key + different text raises IdempotencyConflict", False,
              "no exception raised")
    except IdempotencyConflict:
        check("same key + different text raises IdempotencyConflict", True)

    client.close()
    print("\n" + ("ALL PASSED" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
