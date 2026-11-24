"""LC-MS through SCIEX OS.

SCIEX OS has three client libraries, licensed through the SCIEX OS API program:
  Control API     remote control of SCIEX OS (device status, batches, the acquisition queue)
  Data API        reads .wiff2 data files
  Processing API  reads results from processing sessions (.qsession)

The method names in SciexOsClient below are this repository's, not SCIEX's: the API
reference is not public. Bind them to the licensed library at install time. Everything the
gateway needs is a small surface: is the system ready, submit a batch, read the queue and
history by batch name, abort, read the data.
"""

from __future__ import annotations

from typing import Protocol

from ..methods import ApprovedMethod
from ..models import Sample
from .base import (NotConfigured, SubmitResult, VendorAdapter, VendorRejected, VendorStatus,
                   matches_request, sha256_file, vendor_name)


class SciexOsClient(Protocol):
    def system_status(self) -> dict: ...
    def method_texts(self, lc_method: str, ms_method: str) -> tuple[str, str]: ...
    def submit_batch(self, batch: dict) -> str: ...
    def get_batch(self, batch_id: str) -> dict: ...
    def find_batches(self, name: str) -> list[dict]: ...
    def abort_batch(self, batch_id: str) -> None: ...
    def read_data(self, batch_id: str) -> dict: ...


class SciexOsControlApiClient:
    """Placeholder for the licensed SCIEX OS client libraries (Windows, .NET)."""

    def __getattr__(self, name: str):
        raise NotConfigured(
            "SCIEX OS Control, Data and Processing APIs are licensed through the SCIEX OS API "
            f"program. Bind '{name}' to them on the acquisition PC (for example via pythonnet).")


class SciexOsAdapter(VendorAdapter):
    kind = "lcms"
    vendor = "SCIEX OS"
    interface = "SCIEX OS Control API (batches), Data API (.wiff2)"

    def __init__(self, client: SciexOsClient) -> None:
        self.client = client

    def readiness(self) -> dict:
        s = self.client.system_status()
        reasons = [f"{d['name']} is {d['state']}" for d in s.get("devices", []) if d["state"] != "Ready"]
        return {"ready": s.get("state") == "Ready" and not reasons, "reasons": reasons, "detail": s}

    def method_fingerprint(self, method: ApprovedMethod) -> str:
        from ..methods import fingerprint_text
        lc, ms = self.client.method_texts(method.vendor_ref["lc_method"], method.vendor_ref["ms_method"])
        return fingerprint_text(lc, ms)

    def validate(self, samples: list[Sample], method: ApprovedMethod, options: dict) -> list[str]:
        p = method.params
        problems = []
        for s in samples:
            if s.kind not in ("sample", "control"):
                problems.append(f"{s.sample_id}: blanks come from the method template, list only samples and controls")
                continue
            a = s.attrs
            if not isinstance(a.get("analyte"), str):
                problems.append(f"{s.position}: attrs.analyte is required")
            if a.get("time_min") not in p["time_points_min"]:
                problems.append(f"{s.position}: attrs.time_min must be one of {p['time_points_min']}")
            if a.get("replicate") not in range(1, p["replicates"] + 1):
                problems.append(f"{s.position}: attrs.replicate must be 1 to {p['replicates']}")
        if not any(s.kind == "control" for s in samples):
            problems.append("the plate needs its plasma activity control (kind 'control')")
        return problems

    def _injection_list(self, samples: list[Sample], method: ApprovedMethod) -> list[dict]:
        t = method.template
        order = sorted(samples, key=lambda s: (s.kind == "control", s.attrs["analyte"],
                                               s.attrs["time_min"], s.attrs["replicate"]))
        rows: list[dict] = []
        if t.get("start_blank"):
            rows.append({"sample_name": "double_blank_start", "type": "Double Blank", "position": t["blank_position"]})
        last_t0_of: dict[str, int] = {}
        for s in order:
            rows.append({
                "sample_name": f"{s.sample_id}_T{s.attrs['time_min']}_R{s.attrs['replicate']}",
                "sample_id": s.sample_id,
                "type": "Unknown" if s.kind == "sample" else "Quality Control",
                "position": s.position,
                "analyte": s.attrs["analyte"],
                "time_min": s.attrs["time_min"],
                "replicate": s.attrs["replicate"],
            })
            if s.attrs["time_min"] == 0:
                last_t0_of[s.attrs["analyte"]] = len(rows) - 1
        if t.get("blank_after_t0"):
            # A double blank right after each analyte's highest-response injections checks carryover.
            for analyte, idx in sorted(last_t0_of.items(), key=lambda kv: -kv[1]):
                rows.insert(idx + 1, {"sample_name": f"double_blank_after_{analyte}", "type": "Double Blank",
                                      "position": t["blank_position"], "carryover_of": analyte})
        return rows

    def submit(self, request_id, attempt, samples, method, options) -> SubmitResult:
        batch = {
            "name": vendor_name(request_id, attempt),
            "lc_method": method.vendor_ref["lc_method"],
            "ms_method": method.vendor_ref["ms_method"],
            "data_file": f"{vendor_name(request_id, attempt)}.wiff2",
            "injection_time_s": method.params["injection_time_s"],
            "samples": self._injection_list(samples, method),
        }
        batch_id = self.client.submit_batch(batch)   # may raise ReplyLost or VendorRejected
        return SubmitResult(vendor_ref=batch_id, note=f"batch {batch['name']} with {len(batch['samples'])} injections")

    def find(self, request_id: str) -> list[str]:
        return [b["batch_id"] for b in self.client.find_batches(request_id) if matches_request(b["name"], request_id)]

    def status(self, vendor_ref: str) -> VendorStatus:
        b = self.client.get_batch(vendor_ref)
        state = {"Queued": "queued", "Acquiring": "running", "Complete": "complete",
                 "Failed": "failed", "Aborted": "aborted"}[b["state"]]
        return VendorStatus(state=state, done=b["acquired"], total=b["total"], message=b.get("message", ""))

    def cancel(self, vendor_ref: str) -> None:
        self.client.abort_batch(vendor_ref)

    def fetch(self, vendor_ref, samples, method) -> dict:
        data = self.client.read_data(vendor_ref)
        if not data.get("injections"):
            raise VendorRejected(f"batch {vendor_ref} has no data")
        return {"raw": [{"path": data["path"], "sha256": sha256_file(data["path"])}],
                "injections": data["injections"], "vendor_ref": vendor_ref}
