"""The contract every vendor adapter meets.

An adapter translates one gateway request into one vendor unit of work (a SCIEX OS batch,
a Sample Scheduler XML command, a SpinPilot experiment) and back. Two rules keep it safe:

1. The request_id goes into a field the vendor software shows and stores (batch name,
   command file name, experiment title). After a lost reply, find() searches the vendor's
   own queue and history for it. That is what makes "did it start?" answerable.
2. submit() raises ReplyLost when the call went out and no answer came back. It never
   guesses, and the gateway never retries blindly.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..methods import ApprovedMethod
from ..models import Sample


class ReplyLost(Exception):
    """The call was sent and no reply arrived. The vendor may or may not have the work."""


class VendorRejected(Exception):
    """The vendor software refused the request."""


class NotConfigured(Exception):
    """The real vendor binding is not installed or licensed on this machine."""


@dataclass
class SubmitResult:
    vendor_ref: str | None
    prepared_only: bool = False
    note: str = ""


@dataclass
class VendorStatus:
    state: str                     # queued | running | complete | failed | aborted
    done: int = 0
    total: int = 0
    sample_notes: dict = field(default_factory=dict)
    message: str = ""


def vendor_name(request_id: str, attempt: int) -> str:
    """Name written into the vendor system. Attempt 2+ only happens on an operator's say-so."""
    return request_id if attempt <= 1 else f"{request_id}~{attempt}"


def matches_request(name: str, request_id: str) -> bool:
    return name == request_id or name.startswith(request_id + "~")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


class VendorAdapter(ABC):
    kind: str = ""
    vendor: str = ""
    interface: str = ""

    @abstractmethod
    def readiness(self) -> dict:
        """{"ready": bool, "reasons": [...], "detail": {...}} read from the vendor software."""

    @abstractmethod
    def method_fingerprint(self, method: ApprovedMethod) -> str:
        """Fingerprint of the method as currently stored in the vendor software."""

    def validate(self, samples: list[Sample], method: ApprovedMethod, options: dict) -> list[str]:
        return []

    @abstractmethod
    def submit(self, request_id: str, attempt: int, samples: list[Sample],
               method: ApprovedMethod, options: dict) -> SubmitResult:
        """Hand the work to the vendor. May raise ReplyLost or VendorRejected."""

    @abstractmethod
    def find(self, request_id: str) -> list[str]:
        """Vendor refs, in the queue or the acquisition history, that carry this request_id."""

    @abstractmethod
    def status(self, vendor_ref: str) -> VendorStatus: ...

    @abstractmethod
    def cancel(self, vendor_ref: str) -> None: ...

    @abstractmethod
    def fetch(self, vendor_ref: str, samples: list[Sample], method: ApprovedMethod) -> dict:
        """Raw data pointers (path + sha256) and the parsed data the QC gate needs."""
