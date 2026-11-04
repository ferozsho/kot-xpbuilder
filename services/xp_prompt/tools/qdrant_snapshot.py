#!/usr/bin/env python3
"""Qdrant snapshot helper for the backup and restore drills (P11-F08).

The vector store is a durable store: it holds authorized embeddings with their
tenant and source metadata, and a restore that forgets them makes a deployment
answer differently while looking healthy. Qdrant's supported export is a
collection snapshot, so the backup drives that API instead of copying files out
of a live volume.

Run inside the service container, which already knows where Qdrant is:

    qdrant_snapshot.py list
    qdrant_snapshot.py download <collection>     # snapshot bytes to stdout
"""

from __future__ import annotations

import json
import sys
import urllib.request

from xp_prompt.config import Settings


def _call(url: str, *, method: str = "GET") -> bytes:
    request = urllib.request.Request(
        url, method=method, data=b"" if method == "POST" else None
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def collections(base: str) -> list[str]:
    payload = json.loads(_call(f"{base}/collections") or b"{}")
    return [
        str(item.get("name"))
        for item in payload.get("result", {}).get("collections", [])
        if item.get("name")
    ]


def main() -> int:
    settings = Settings.from_env()
    base = settings.qdrant_url.rstrip("/")
    if len(sys.argv) < 2:
        print("usage: qdrant_snapshot.py list|download <collection>", file=sys.stderr)
        return 2
    action = sys.argv[1]
    if action == "list":
        for name in collections(base):
            print(name)
        return 0
    if action == "download" and len(sys.argv) == 3:
        name = sys.argv[2]
        created = json.loads(
            _call(f"{base}/collections/{name}/snapshots", method="POST") or b"{}"
        )
        snapshot = str(created.get("result", {}).get("name") or "")
        if not snapshot:
            print(f"no snapshot was created for {name}", file=sys.stderr)
            return 1
        body = _call(f"{base}/collections/{name}/snapshots/{snapshot}")
        sys.stdout.buffer.write(body)
        return 0
    print(f"unknown action {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
