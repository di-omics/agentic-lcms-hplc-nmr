"""NMR through Bruker SpinPilot on TopSpin 5.

SpinPilot is the automation layer over TopSpin: experiments for samples in a sample
changer, a queue, a browser view where scientists watch queue and history, and a Python
API for experiment creation and workflow automation (documented in the TopSpin help).
TopSpin still acquires: sample in, lock, tune (atma), shim (topshim), receiver gain (rga),
zg, sample out.

The method names in SpinPilotClient are this repository's; bind them to the SpinPilot
Python API on the spectrometer workstation. On TopSpin 4 without SpinPilot, the same
adapter shape can drive TopSpin's own Python 3 interface, which executes TopSpin
commands (sx, lock, atma, topshim, rga, zg, efp, apk) through TopSpin's embedded service.
"""

from __future__ import annotations

from typing import Protocol

from ..methods import ApprovedMethod, fingerprint_text
from ..models import Sample
from .base import (NotConfigured, SubmitResult, VendorAdapter, VendorStatus, matches_request,
                   sha256_file, vendor_name)


class SpinPilotClient(Protocol):
    def system_status(self) -> dict: ...
    def experiment_text(self, experiment: str) -> str: ...
    def create_experiment(self, spec: dict) -> str: ...
    def get_experiment(self, exp_id: str) -> dict: ...
    def find_experiments(self, title: str) -> list[dict]: ...
    def cancel_experiment(self, exp_id: str) -> None: ...
    def read_dataset(self, exp_id: str) -> dict: ...


class SpinPilotApiClient:
    def __getattr__(self, name: str):
        raise NotConfigured(
            f"Bind '{name}' to the SpinPilot Python API on the TopSpin 5 workstation "
            "(see the SpinPilot section of the TopSpin help).")


def holder_of(position: str) -> int | None:
    if position.startswith("holder:") and position[7:].isdigit():
        return int(position[7:])
    return None


class SpinPilotAdapter(VendorAdapter):
    kind = "nmr"
    vendor = "Bruker SpinPilot / TopSpin 5"
    interface = "SpinPilot Python API (experiments), TopSpin dataset"

    def __init__(self, client: SpinPilotClient) -> None:
        self.client = client

    def readiness(self) -> dict:
        s = self.client.system_status()
        reasons = [] if s.get("ready") else [s.get("message", "spectrometer not ready")]
        return {"ready": bool(s.get("ready")), "reasons": reasons, "detail": s}

    def method_fingerprint(self, method: ApprovedMethod) -> str:
        return fingerprint_text(self.client.experiment_text(method.vendor_ref["experiment"]))

    def validate(self, samples: list[Sample], method: ApprovedMethod, options: dict) -> list[str]:
        lo, hi = method.template["holders"]
        problems = []
        for s in samples:
            h = holder_of(s.position)
            if h is None or not lo <= h <= hi:
                problems.append(f"{s.sample_id}: position must be holder:{lo} to holder:{hi}")
            if s.attrs.get("solvent") not in method.template["solvents"]:
                problems.append(f"{s.sample_id}: attrs.solvent must be one of {method.template['solvents']}")
            if s.kind != "sample":
                problems.append(f"{s.sample_id}: one sample tube per NMR request")
        return problems

    def submit(self, request_id, attempt, samples, method, options) -> SubmitResult:
        s = samples[0]
        spec = {
            "title": vendor_name(request_id, attempt),
            "sample_name": s.sample_id,
            "holder": holder_of(s.position),
            "solvent": s.attrs["solvent"],
            "experiment": method.vendor_ref["experiment"],
            "parameters": {"ns": method.params["ns"], "d1": method.params["d1_s"]},
            "shim": "topshim full, from scratch" if options.get("reshim") else "topshim",
        }
        exp_id = self.client.create_experiment(spec)
        return SubmitResult(vendor_ref=exp_id, note=f"experiment {exp_id} '{spec['title']}' for holder {spec['holder']}")

    def find(self, request_id: str) -> list[str]:
        return [e["id"] for e in self.client.find_experiments(request_id) if matches_request(e["title"], request_id)]

    def status(self, vendor_ref: str) -> VendorStatus:
        e = self.client.get_experiment(vendor_ref)
        state = {"Queued": "queued", "Running": "running", "Completed": "complete",
                 "Error": "failed", "Cancelled": "aborted"}[e["state"]]
        return VendorStatus(state=state, done=int(state == "complete"), total=1,
                            message=e.get("message") or e.get("step", ""))

    def cancel(self, vendor_ref: str) -> None:
        self.client.cancel_experiment(vendor_ref)

    def fetch(self, vendor_ref, samples, method) -> dict:
        d = self.client.read_dataset(vendor_ref)
        raw = [{"path": str(p), "sha256": sha256_file(p)} for p in d["files"]]
        return {"raw": raw, "fid": d["fid"], "acqus": d["acqus"], "vendor_ref": vendor_ref,
                "dataset": str(d["path"])}
