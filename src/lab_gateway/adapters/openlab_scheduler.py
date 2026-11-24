"""HPLC through Agilent Sample Scheduler for OpenLab.

Sample Scheduler is the LIMS-facing side of OpenLab CDS. Its documented LIMS route is
folder based: the LIMS writes XML command files into a LIMS commands folder, the
scheduler's LIMS agent scans it, turns valid commands into analyses and moves invalid
ones to a trash folder. XML actions (cancel, create a sequence) use the same route.

The element and attribute names written below are placeholders shaped like such a
command. Bind them to the schema in the Sample Scheduler Command Guide for the installed
version before use.

Barcodes: the adapter always writes a barcode section with a do-not-inject-on-mismatch
rule for every sample vial, and never relies on the scheduler's default for an omitted
section (the simulator injects in that case; confirm the default of the installed
version). Commissioning tests the mismatch path on a system with a barcode reader.
"""

from __future__ import annotations

import csv
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Protocol

from ..methods import ApprovedMethod, fingerprint_text
from ..models import Sample
from .base import (NotConfigured, SubmitResult, VendorAdapter, VendorStatus, matches_request,
                   sha256_file, vendor_name)

VIAL_RE = re.compile(r"^P1-[A-F][1-8]$")
XML_NOTE = (" Placeholder element names shaped like a Sample Scheduler LIMS command. "
            "Bind to the Command Guide schema for the installed version. ")


class OpenLabSite(Protocol):
    def instrument_status(self) -> dict: ...
    def method_text(self, method: str) -> str: ...
    def analyses(self, sequence_name: str) -> list[dict]: ...
    def sequence_names(self) -> list[str]: ...


class OpenLabSiteNotConfigured:
    def __getattr__(self, name: str):
        raise NotConfigured(
            "Point the adapter at the Sample Scheduler LIMS commands and trash folders, the "
            "analysis table (database view or the results the scheduler returns to the LIMS), "
            f"the OpenLab instrument status and the method store. Missing: '{name}'.")


class OpenLabSchedulerAdapter(VendorAdapter):
    kind = "hplc"
    vendor = "Agilent OpenLab CDS"
    interface = "Sample Scheduler for OpenLab, LIMS XML commands"

    def __init__(self, commands_dir: Path, trash_dir: Path, site: OpenLabSite, instrument_name: str) -> None:
        self.commands_dir = Path(commands_dir)
        self.trash_dir = Path(trash_dir)
        self.site = site
        self.instrument_name = instrument_name

    # ---- checks -------------------------------------------------------------------
    def readiness(self) -> dict:
        s = self.site.instrument_status()
        ok = s.get("state") in ("Idle", "Running")
        return {"ready": ok, "reasons": [] if ok else [f"instrument state is {s.get('state')}"], "detail": s}

    def method_fingerprint(self, method: ApprovedMethod) -> str:
        return fingerprint_text(self.site.method_text(method.vendor_ref["method"]))

    def validate(self, samples: list[Sample], method: ApprovedMethod, options: dict) -> list[str]:
        reserved = {method.template["blank_position"], method.template["standard_position"]}
        problems = []
        for s in samples:
            if s.kind not in ("sample", "control"):
                problems.append(f"{s.sample_id}: standards and blanks come from the method template")
            if not VIAL_RE.match(s.position):
                problems.append(f"{s.sample_id}: vial {s.position} is not a tray position P1-A1 to P1-F8")
            if s.position in reserved:
                problems.append(f"{s.sample_id}: vial {s.position} holds the method's standard or blank")
            if not s.barcode:
                problems.append(f"{s.sample_id}: a barcode is required, so a swapped vial is never injected")
        return problems

    # ---- the XML command ------------------------------------------------------------
    def build_command(self, name: str, samples: list[Sample], method: ApprovedMethod) -> bytes:
        t = method.template
        root = ET.Element("SampleSchedulerCommand", {"schema": "placeholder-1", "requestId": name,
                                                       "limsConnection": "lab-gateway"})
        root.insert(0, ET.Comment(XML_NOTE))
        seq = ET.SubElement(root, "Sequence", {"name": name, "instrument": self.instrument_name,
                                                "method": method.vendor_ref["method"],
                                                "project": method.vendor_ref["project"]})
        order = 0

        def add(kind: str, label: str, vial: str, sample: Sample | None = None) -> None:
            nonlocal order
            order += 1
            attrs = {"order": str(order), "name": label, "type": kind, "vial": vial}
            if sample is not None:
                attrs["sampleId"] = sample.sample_id
            a = ET.SubElement(seq, "Analysis", attrs)
            if sample is not None:
                ET.SubElement(a, "Barcode", {"expected": sample.barcode, "onMismatch": "DoNotInject"})

        add("Blank", "blank", t["blank_position"])
        for i in range(1, t["sst_injections"] + 1):
            add("SystemSuitability", f"SST_{i}", t["standard_position"])
        for s in samples:
            add("Sample" if s.kind == "sample" else "Control", s.sample_id, s.position, s)
        if t.get("bracketing_standard"):
            add("BracketingStandard", "BRACKET_1", t["standard_position"])
        ET.indent(root)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"

    def _atomic_write(self, path: Path, data: bytes) -> None:
        # The scanner only picks up *.xml, so it never sees a half-written file.
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def submit(self, request_id, attempt, samples, method, options) -> SubmitResult:
        name = vendor_name(request_id, attempt)
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        target = self.commands_dir / f"{name}.xml"
        if not target.exists():
            self._atomic_write(target, self.build_command(name, samples, method))
        return SubmitResult(vendor_ref=name, note=f"XML command {target.name} written to the LIMS commands folder")

    # ---- reading back -----------------------------------------------------------------
    def find(self, request_id: str) -> list[str]:
        names = set()
        for folder in (self.commands_dir, self.trash_dir):
            if folder.exists():
                names |= {p.stem for p in folder.glob("*.xml") if not p.stem.endswith(".cancel")}
        names |= set(self.site.sequence_names())
        return sorted(n for n in names if matches_request(n, request_id))

    def status(self, vendor_ref: str) -> VendorStatus:
        rows = self.site.analyses(vendor_ref)
        if not rows:
            trashed = self.trash_dir / f"{vendor_ref}.xml"
            if trashed.exists():
                reason_file = self.trash_dir / f"{vendor_ref}.reason.txt"
                reason = reason_file.read_text().strip() if reason_file.exists() else "moved to trash"
                return VendorStatus(state="failed", message=f"command rejected by the LIMS agent: {reason}")
            return VendorStatus(state="queued", message="waiting for the LIMS agent to pick up the command")
        notes = {r["sample_id"]: r["message"] for r in rows if r.get("sample_id") and r["status"] == "Error"}
        done = sum(r["status"] in ("Completed", "Error", "Cancelled") for r in rows)
        if any(r["status"] == "Cancelled" for r in rows) and done == len(rows):
            state = "aborted"
        elif done == len(rows):
            state = "complete"
        elif any(r["status"] in ("Running", "Completed", "Error") for r in rows):
            state = "running"
        else:
            state = "queued"
        return VendorStatus(state=state, done=done, total=len(rows), sample_notes=notes)

    def cancel(self, vendor_ref: str) -> None:
        pending = self.commands_dir / f"{vendor_ref}.xml"
        try:
            pending.unlink()          # not picked up yet: withdraw it
            return
        except FileNotFoundError:
            pass                      # already consumed: send a cancel action
        action = ET.Element("SampleSchedulerAction", {"schema": "placeholder-1", "type": "CancelAnalyses",
                                                       "sequence": vendor_ref})
        self._atomic_write(self.commands_dir / f"{vendor_ref}.cancel.xml",
                           ET.tostring(action, encoding="utf-8", xml_declaration=True) + b"\n")

    def fetch(self, vendor_ref, samples, method) -> dict:
        injections, raw = [], []
        for r in self.site.analyses(vendor_ref):
            inj = {"name": r["name"], "type": r["type"], "sample_id": r.get("sample_id"), "vial": r["vial"],
                   "injected": r["status"] == "Completed", "message": r.get("message", "")}
            if inj["injected"]:
                t, y = [], []
                with open(r["result_file"], newline="") as f:
                    for row in csv.DictReader(f):
                        t.append(float(row["time_min"]))
                        y.append(float(row["mAU"]))
                inj["t"], inj["y"] = t, y
                raw.append({"path": str(r["result_file"]), "sha256": sha256_file(r["result_file"])})
            injections.append(inj)
        return {"raw": raw, "injections": injections, "vendor_ref": vendor_ref}
