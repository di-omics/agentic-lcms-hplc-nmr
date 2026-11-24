"""Request states and the sample manifest."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum

from .ledger import canonical


class State(str, Enum):
    RESERVED = "reserved"              # instrument held for this request, nothing sent yet
    SUBMITTED = "submitted"            # the vendor software accepted it and gave a run id
    UNKNOWN = "unknown"                # the call went out, the reply never came back
    NEEDS_OPERATOR = "needs_operator"  # the gateway could not establish what happened
    PREPARED = "prepared"              # UI fallback: filled in, waiting for a person to press Start
    RUNNING = "running"
    DONE = "done"                      # acquired and QC-evaluated (usable or not)
    FAILED = "failed"                  # the vendor reported an error
    CANCELLED = "cancelled"


TERMINAL = {State.DONE, State.FAILED, State.CANCELLED}
HOLDS_INSTRUMENT = {State.RESERVED, State.SUBMITTED, State.UNKNOWN, State.NEEDS_OPERATOR,
                    State.PREPARED, State.RUNNING}

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
SAMPLE_KINDS = {"sample", "control", "standard", "blank"}


class ManifestError(ValueError):
    pass


@dataclass
class Sample:
    sample_id: str
    position: str
    kind: str = "sample"
    barcode: str | None = None
    attrs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_manifest(items: list[dict]) -> list[Sample]:
    if not isinstance(items, list) or not items:
        raise ManifestError("sample_manifest must be a non-empty list")
    samples: list[Sample] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ManifestError(f"entry {i} is not an object")
        unknown = set(item) - {"sample_id", "position", "kind", "barcode", "attrs"}
        if unknown:
            raise ManifestError(f"entry {i} has unknown fields: {sorted(unknown)}")
        sid, pos = item.get("sample_id"), item.get("position")
        if not isinstance(sid, str) or not sid:
            raise ManifestError(f"entry {i} needs a sample_id")
        if not isinstance(pos, str) or not pos:
            raise ManifestError(f"entry {i} ({sid}) needs a position")
        kind = item.get("kind", "sample")
        if kind not in SAMPLE_KINDS:
            raise ManifestError(f"entry {i} ({sid}) has kind {kind!r}; use one of {sorted(SAMPLE_KINDS)}")
        if pos in seen:
            raise ManifestError(f"position {pos} appears twice")
        seen.add(pos)
        attrs = item.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise ManifestError(f"entry {i} ({sid}) attrs must be an object")
        barcode = item.get("barcode")
        if barcode is not None and not isinstance(barcode, str):
            raise ManifestError(f"entry {i} ({sid}) barcode must be a string")
        samples.append(Sample(sid, pos, kind, barcode, dict(attrs)))
    return samples


def request_fingerprint(instrument: str, method_version: str, samples: list[Sample], options: dict) -> str:
    body = {
        "instrument": instrument,
        "method_version": method_version,
        "options": options,
        "samples": [s.to_dict() for s in samples],
    }
    return hashlib.sha256(canonical(body).encode("ascii")).hexdigest()
