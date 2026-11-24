"""Approved methods.

A method version pins three things together: the vendor method the instrument runs, the
sequence template around the samples (standards, blanks), and the acceptance criteria that
decide whether a finished run is usable. The agent names a method version; it never sends
pump, gradient or pulse parameters. Before every submission the gateway compares the
fingerprint of the method stored in the vendor software with the fingerprint recorded at
approval, so a method edited on the instrument is caught before it runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REGISTRY = Path(__file__).with_name("approved_methods.json")


def fingerprint_text(*texts: str) -> str:
    joined = "\n--\n".join(texts)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovedMethod:
    method_version: str
    kind: str
    status: str
    vendor_ref: dict
    fingerprint: str
    max_samples: int
    template: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    qc: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    summary: str = ""

    def public(self) -> dict:
        return {
            "method_version": self.method_version,
            "status": self.status,
            "summary": self.summary,
            "max_samples": self.max_samples,
            "options": self.options,
            "acceptance_criteria": self.qc,
        }


class MethodRegistry:
    def __init__(self, methods: list[ApprovedMethod]) -> None:
        self._by_version = {m.method_version: m for m in methods}

    @classmethod
    def load(cls, path: Path = DEFAULT_REGISTRY) -> "MethodRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([ApprovedMethod(**m) for m in raw["methods"]])

    def get(self, method_version: str) -> ApprovedMethod | None:
        return self._by_version.get(method_version)

    def for_kind(self, kind: str) -> list[ApprovedMethod]:
        return [m for m in self._by_version.values() if m.kind == kind]
