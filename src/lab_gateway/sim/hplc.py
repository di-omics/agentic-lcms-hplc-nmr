"""A simulated Agilent Sample Scheduler for OpenLab with one HPLC.

Seen from the LIMS side: a LIMS commands folder that a LIMS agent scans every minute,
a trash folder for invalid commands, an analysis table, and exported chromatograms.
The barcode rule mirrors the behavior the adapter is written against: with a barcode
section the vial is checked and a mismatch is not injected; without one, it is injected.
"""

from __future__ import annotations

import csv
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from .signals import bigauss
from .truth import PEPTIDES, impurity_profile, rng_for
from .vendor_methods import HPLC_METHODS


class SimSampleScheduler:
    def __init__(self, clock, root: Path, tray: dict[str, dict], *, instrument: str = "HPLC-01",
                 run_time_min: float = 18.0, scan_interval_s: float = 60.0, barcode_read_s: float = 15.0) -> None:
        self.clock = clock
        self.root = Path(root)
        self.commands_dir = self.root / "LIMS_Commands"
        self.trash_dir = self.root / "Trash"
        self.results_dir = self.root / "Results"
        for d in (self.commands_dir, self.trash_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.tray = tray
        self.instrument = instrument
        self.methods = dict(HPLC_METHODS)
        self.run_time_min = run_time_min
        self.scan_interval_s = scan_interval_s
        self.barcode_read_s = barcode_read_s
        self.rows: list[dict] = []
        self.queue_end = 0.0
        self.last_scan = -math.inf

    # ---------------------------------------------------------------- OpenLabSite
    def instrument_status(self) -> dict:
        self._tick()
        busy = any(r["status"] in ("Waiting", "Running") for r in self.rows)
        return {"instrument": self.instrument, "model": "1290 Infinity II LC (simulated)",
                "state": "Running" if busy else "Idle"}

    def method_text(self, method: str) -> str:
        return self.methods[method]

    def analyses(self, sequence_name: str) -> list[dict]:
        self._tick()
        keys = ("sequence", "order", "name", "type", "sample_id", "vial", "status", "message", "result_file")
        return [{k: r[k] for k in keys} for r in self.rows if r["sequence"] == sequence_name]

    def sequence_names(self) -> list[str]:
        self._tick()
        return sorted({r["sequence"] for r in self.rows})

    # ---------------------------------------------------------------- LIMS agent
    def _tick(self) -> None:
        now = self.clock.now()
        if now - self.last_scan >= self.scan_interval_s:
            self.last_scan = now
            self._scan(now)
        self._advance(now)

    def _reject(self, f: Path, reason: str) -> None:
        f.replace(self.trash_dir / f.name)
        (self.trash_dir / f"{f.stem}.reason.txt").write_text(reason + "\n")

    def _scan(self, now: float) -> None:
        for f in sorted(self.commands_dir.glob("*.xml")):
            try:
                root = ET.parse(f).getroot()
            except ET.ParseError as e:
                self._reject(f, f"not valid XML: {e}")
                continue
            if root.tag == "SampleSchedulerAction":
                if root.get("type") == "CancelAnalyses":
                    for r in self.rows:
                        if r["sequence"] == root.get("sequence") and r["status"] == "Waiting":
                            r["status"], r["message"] = "Cancelled", "cancelled by LIMS action"
                f.unlink()
                continue
            seq = root.find("Sequence")
            problem = None
            if root.tag != "SampleSchedulerCommand" or seq is None:
                problem = "unknown command"
            elif seq.get("method") not in self.methods:
                problem = f"method {seq.get('method')} not found"
            elif seq.get("instrument") != self.instrument:
                problem = f"instrument {seq.get('instrument')} not configured"
            elif any(a.get("vial") not in self.tray for a in seq.findall("Analysis")):
                problem = "a vial position is not on the tray"
            if problem:
                self._reject(f, problem)
                continue
            start = max(now, self.queue_end)
            for a in seq.findall("Analysis"):
                bc = a.find("Barcode")
                expected = bc.get("expected") if bc is not None else None
                physical = self.tray[a.get("vial")]
                mismatch = expected is not None and expected != physical["barcode"]
                end = start + (self.barcode_read_s if mismatch else self.run_time_min * 60)
                self.rows.append({
                    "sequence": seq.get("name"), "order": int(a.get("order")), "name": a.get("name"),
                    "type": a.get("type"), "sample_id": a.get("sampleId"), "vial": a.get("vial"),
                    "expected_barcode": expected, "mismatch": mismatch, "start": start, "end": end,
                    "status": "Waiting", "message": "", "result_file": None,
                })
                start = end
            self.queue_end = start
            f.unlink()

    def _advance(self, now: float) -> None:
        for r in self.rows:
            if r["status"] not in ("Waiting", "Running"):
                continue
            if now >= r["end"]:
                if r["mismatch"]:
                    r["status"] = "Error"
                    r["message"] = (f"barcode mismatch at {r['vial']}: expected {r['expected_barcode']}, "
                                    f"read {self.tray[r['vial']]['barcode']}; not injected")
                else:
                    r["result_file"] = self._write_chromatogram(r)
                    r["status"] = "Completed"
            elif now >= r["start"]:
                r["status"] = "Running"

    # ---------------------------------------------------------------- the detector
    def _write_chromatogram(self, r: dict) -> str:
        contents = self.tray[r["vial"]]["contents"]
        rng = rng_for("hplc", r["sequence"], r["order"], contents)
        t = np.arange(0, self.run_time_min, 1 / (5 * 60.0))         # 5 Hz
        y = 0.35 * t / self.run_time_min + 0.04 * np.sin(2 * np.pi * t / 7.0) + rng.normal(0, 0.02, t.size)
        y += -0.9 * np.exp(-((t - 0.82) / 0.03) ** 2) + 0.6 * np.exp(-((t - 0.95) / 0.05) ** 2)   # injection front
        if contents != "BLANK":
            pep = PEPTIDES[contents]
            jitter = rng.normal(0, 0.006)
            total = 2000.0 * (1 + rng.normal(0, 0.004)) if contents == "REF-STD" else 2400.0 * (1 + rng.normal(0, 0.01))
            species = [(pep["rt_min"], pep["purity_pct"] / 100.0)]
            if contents != "REF-STD":
                species += [(pep["rt_min"] + off, frac) for off, frac in impurity_profile(contents)]
            for rt, frac in species:
                y += bigauss(t, rt + jitter, total * frac, 0.035, 0.046)
        path = self.results_dir / f"{r['sequence']}_{r['order']:02d}_{r['name']}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_min", "mAU"])
            w.writerows(zip(np.round(t, 5), np.round(y, 4)))
        return str(path)
