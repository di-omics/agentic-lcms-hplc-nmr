"""A record of every call that changes an instrument.

Append-only JSON lines. Each entry carries the hash of the entry before it, so an edited
or deleted line breaks the chain and verify() says where. Besides vendor calls, the ledger
holds reservations, reconciliation findings and operator decisions: everything you would
need to explain later why an injection happened. Reads (status polls) are not recorded.

Write-ahead rule: the gateway writes "submit.sent" BEFORE it calls the vendor, so a crash
or a lost reply can never leave an injection with no trace.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(entry_without_hash: dict) -> str:
    return hashlib.sha256(canonical(entry_without_hash).encode("ascii")).hexdigest()


class Ledger:
    def __init__(self, path: Path, clock) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._head = GENESIS
        self._seq = 0
        existing = self.read()
        if existing:
            self._head = existing[-1]["hash"]
            self._seq = existing[-1]["seq"]

    def append(
        self,
        action: str,
        *,
        actor: str,
        instrument: str | None = None,
        request_id: str | None = None,
        detail: dict | None = None,
    ) -> dict:
        self._seq += 1
        entry = {
            "seq": self._seq,
            "t": round(float(self.clock.now()), 3),
            "wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": actor,
            "action": action,
            "instrument": instrument,
            "request_id": request_id,
            "detail": detail or {},
            "prev": self._head,
        }
        entry["hash"] = _digest(entry)
        with open(self.path, "a", encoding="ascii") as f:
            f.write(canonical(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._head = entry["hash"]
        return entry

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="ascii").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def verify(self) -> dict:
        prev = GENESIS
        entries = self.read()
        for i, entry in enumerate(entries, start=1):
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry.get("seq") != i or entry.get("prev") != prev or _digest(body) != entry.get("hash"):
                return {"ok": False, "entries": len(entries), "broken_at_seq": i}
            prev = entry["hash"]
        return {"ok": True, "entries": len(entries), "head": prev}
