"""A simulated SCIEX OS with an LC-MS/MS system, as seen through its Control and Data APIs.

System status, a batch queue with acquisition history (searchable by batch name), abort,
and data files read through a Data-API-like call. Faults are injected on purpose:
  lost_after_accept   the batch is queued and acquiring, but the reply never arrives
  lost_before_accept  the connection drops before SCIEX OS sees the batch
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..adapters.base import ReplyLost, VendorRejected, matches_request
from .signals import bigauss
from .truth import PEPTIDES, rng_for
from .vendor_methods import LCMS_METHODS

T0_AREA = 4.0e5        # analyte response at T0, counts x s
ISTD_AREA = 3.0e5      # internal standard response
NOISE = 40.0           # counts


class SimSciexOs:
    def __init__(self, clock, root: Path, *, reply_faults: list[str] | None = None,
                 carryover_frac: float = 2e-4) -> None:
        self.clock = clock
        self.data_dir = Path(root) / "Data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.methods = dict(LCMS_METHODS)
        self.reply_faults = list(reply_faults or [])
        self.carryover_frac = carryover_frac
        self.batches: dict[str, dict] = {}
        self.queue_end = 0.0

    # ---------------------------------------------------------------- Control API
    def system_status(self) -> dict:
        active = sum(1 for b in self.batches.values() if self._state(b) in ("Queued", "Acquiring"))
        return {"state": "Ready", "queue_batches": active,
                "devices": [{"name": "ExionLC AD (simulated)", "state": "Ready"},
                            {"name": "Triple Quad 7500 (simulated)", "state": "Ready"}]}

    def method_texts(self, lc_method: str, ms_method: str) -> tuple[str, str]:
        return self.methods[lc_method], self.methods[ms_method]

    def submit_batch(self, batch: dict) -> str:
        if batch["lc_method"] not in self.methods or batch["ms_method"] not in self.methods:
            raise VendorRejected("acquisition method not found")
        if not batch["samples"]:
            raise VendorRejected("empty batch")
        fault = self.reply_faults.pop(0) if self.reply_faults else None
        if fault == "lost_before_accept":
            raise ReplyLost("connection to SCIEX OS dropped before the batch was acknowledged")
        now = self.clock.now()
        bid = f"B{len(self.batches) + 1:04d}"
        start = max(now, self.queue_end)
        self.batches[bid] = {"batch_id": bid, "name": batch["name"], "samples": batch["samples"],
                             "inj_s": float(batch["injection_time_s"]), "start": start, "aborted_at": None,
                             "data_path": None, "submitted": now}
        self.queue_end = start + len(batch["samples"]) * float(batch["injection_time_s"])
        if fault == "lost_after_accept":
            raise ReplyLost("connection dropped right after Submit; SCIEX OS may already be acquiring")
        return bid

    def _acquired(self, b: dict) -> int:
        stop = self.clock.now() if b["aborted_at"] is None else min(self.clock.now(), b["aborted_at"])
        return int(max(0, min(len(b["samples"]), (stop - b["start"]) // b["inj_s"])))

    def _state(self, b: dict) -> str:
        n = self._acquired(b)
        if b["aborted_at"] is not None and self.clock.now() >= b["aborted_at"]:
            return "Aborted"
        if self.clock.now() < b["start"]:
            return "Queued"
        return "Complete" if n == len(b["samples"]) else "Acquiring"

    def get_batch(self, batch_id: str) -> dict:
        b = self.batches[batch_id]
        return {"batch_id": batch_id, "name": b["name"], "state": self._state(b),
                "acquired": self._acquired(b), "total": len(b["samples"])}

    def find_batches(self, name: str) -> list[dict]:
        return [{"batch_id": b["batch_id"], "name": b["name"], "state": self._state(b)}
                for b in self.batches.values() if matches_request(b["name"], name)]

    def abort_batch(self, batch_id: str) -> None:
        self.batches[batch_id]["aborted_at"] = self.clock.now()

    # ---------------------------------------------------------------- Data API
    def read_data(self, batch_id: str) -> dict:
        b = self.batches[batch_id]
        if self._state(b) != "Complete":
            raise VendorRejected(f"batch {batch_id} is {self._state(b)}")
        if b["data_path"] is None:
            b["data_path"] = self._write(b)
        data = json.loads(Path(b["data_path"]).read_text())
        return {"path": b["data_path"], "injections": data["injections"]}

    def _write(self, b: dict) -> str:
        t = np.arange(0, 3.0, 0.005)
        injections, last_area = [], {}
        for k, s in enumerate(b["samples"]):
            rng = rng_for("lcms", b["name"], k, s["sample_name"])
            vol = 1 + rng.normal(0, 0.05)                     # shared by analyte and IS: the ratio cancels it
            if s["type"] == "Double Blank":
                analyte = s.get("carryover_of")
                rt = PEPTIDES[analyte]["lc_rt_min"] if analyte else 1.5
                area = self.carryover_frac * last_area.get(analyte, 0.0)
                istd = 0.0
            else:
                pep = PEPTIDES[s["analyte"]]
                rt = pep["lc_rt_min"] + rng.normal(0, 0.004)
                frac = 0.5 ** (s["time_min"] / pep["t_half_min"])
                area = T0_AREA * frac * vol * (1 + rng.normal(0, 0.02))
                istd = ISTD_AREA * vol * (1 + rng.normal(0, 0.02))
                last_area[s["analyte"]] = area
            ya = 20 + bigauss(t, rt, area, 0.018, 0.024) + rng.normal(0, NOISE, t.size)
            yi = 20 + bigauss(t, rt + 0.002, istd, 0.018, 0.024) + rng.normal(0, NOISE, t.size)
            injections.append({**{k2: s.get(k2) for k2 in ("sample_name", "sample_id", "type", "position", "analyte",
                                                         "time_min", "replicate", "carryover_of")},
                               "t": np.round(t, 4).tolist(), "xic_analyte": np.round(ya, 1).tolist(),
                               "xic_istd": np.round(yi, 1).tolist()})
        path = self.data_dir / f"{b['name']}.wiff2.json"
        path.write_text(json.dumps({"note": "simulated stand-in for a .wiff2 file read through the Data API",
                                    "batch": b["name"], "injections": injections}))
        return str(path)
