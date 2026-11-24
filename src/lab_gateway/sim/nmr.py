"""A simulated Bruker SpinPilot on TopSpin 5 with a 600 MHz spectrometer and sample changer.

Experiments run through the TopSpin steps (sample in, lock, atma, topshim, rga, zg,
sample out) on a schedule. The dataset is a complex FID plus acquisition parameters,
standing in for a TopSpin dataset folder. Faults on purpose: the first N experiments
shimmed with plain topshim have a drifted field (broad lines) unless a full reshim is
requested; a tube whose solvent does not match fails at the lock step.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..adapters.base import VendorRejected, matches_request
from .signals import expand_multiplet, fid_from_lines
from .truth import PEPTIDES, rng_for
from .vendor_methods import NMR_TEMPLATES

SFO1 = 600.13
SW_PPM = 12.0
O1P = 4.70
TD = 16384
NATURAL_LW = 0.35
GOOD_SHIM_LW = 0.10
DRIFTED_SHIM_LW = 1.00


def peptide_lines(contents: str, seed_parts, sfo1: float = SFO1) -> list[tuple[float, float, float]]:
    """(ppm, amplitude, extra linewidth Hz). A simplified peptide 1H spectrum plus DSS."""
    r = rng_for("nmr-lines", contents, *seed_parts[:1])
    out: list[tuple[float, float, float]] = []

    def add(ppm, n_h, couplings=(), lw=0.0):
        out.extend((p, a, lw) for p, a in expand_multiplet(ppm, n_h, list(couplings), sfo1))

    # DSS reference (half the peptide concentration)
    add(0.000, 4.5)
    add(0.63, 1.0, [(8.0, 2)], 0.3)
    add(1.75, 1.0, [(8.0, 4)], 0.3)
    add(2.91, 1.0, [(8.0, 2)], 0.3)
    add(4.70, 25.0, (), 20.0)                       # residual water after excitation sculpting
    n = 30
    for _ in range(n - 1):
        add(r.uniform(7.75, 8.55), 1, [(r.uniform(6.0, 8.5), 1)], 2.0)      # backbone amides
    for _ in range(4):
        add(r.uniform(6.85, 7.65), 2, [(8.0, 1)], 0.5)                     # aromatic rings
    for ppm in (7.55, 6.88, 7.48, 6.81):
        add(ppm, 1, (), 3.0)                                               # side-chain NH2
    for _ in range(n):
        add(r.uniform(3.85, 4.55), 1, [(7.0, 1), (5.5, 1)], 0.8)           # alpha H
    for _ in range(int(1.3 * n)):
        add(r.uniform(1.62, 3.30), 2, [(7.0, 2)], 1.0)                     # side-chain CH2 / CH
    for _ in range(6):
        add(r.uniform(0.84, 1.00), 3, [(6.8, 1)], 0.3)                     # methyl doublets
    for _ in range(3):
        add(r.uniform(1.18, 1.32), 3, [(7.0, 1)], 0.3)
    for ppm in PEPTIDES[contents]["singlets_ppm"]:
        add(ppm, 3, (), 0.2)                                               # designed residue methyls
    return out


class SimSpinPilot:
    def __init__(self, clock, root: Path, holders: dict[int, dict], *, shim_drift_runs: int = 0,
                 noise_sd: float = 0.12) -> None:
        self.clock = clock
        self.data_dir = Path(root) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.holders = holders
        self.templates = dict(NMR_TEMPLATES)
        self.shim_drift_runs = shim_drift_runs
        self.noise_sd = noise_sd
        self.experiments: dict[str, dict] = {}
        self.queue_end = 0.0

    # ---------------------------------------------------------------- Python API surface
    def system_status(self) -> dict:
        return {"ready": True, "spectrometer": "AVANCE NEO 600 (simulated)", "probe": "5 mm TCI (simulated)",
                "sample_changer": "SampleXpress, 60 holders (simulated)",
                "queue": sum(1 for e in self.experiments.values() if self._state(e)[0] in ("Queued", "Running"))}

    def experiment_text(self, experiment: str) -> str:
        return self.templates[experiment]

    def create_experiment(self, spec: dict) -> str:
        if spec["experiment"] not in self.templates:
            raise VendorRejected(f"experiment {spec['experiment']} not defined")
        tube = self.holders.get(spec["holder"])
        if tube is None:
            raise VendorRejected(f"holder {spec['holder']} is empty")
        full = "full" in spec["shim"]
        drift = False
        if not full and self.shim_drift_runs > 0:
            self.shim_drift_runs -= 1
            drift = True
        aq = TD / (SW_PPM * SFO1)
        acq_s = (spec["parameters"]["ns"] + 4) * (spec["parameters"]["d1"] + aq)
        steps = [("sample in", 60), ("lock", 30), ("tune and match (atma)", 60),
                 ("shim (topshim full)" if full else "shim (topshim)", 240 if full else 90),
                 ("receiver gain (rga)", 20), ("acquire (zg)", acq_s), ("sample out", 40)]
        start = max(self.clock.now(), self.queue_end)
        eid = f"E{len(self.experiments) + 1:04d}"
        lock_ok = tube["solvent"] == spec["solvent"]
        self.experiments[eid] = {"id": eid, "title": spec["title"], "spec": spec, "tube": tube,
                                 "start": start, "steps": steps, "drift": drift, "lock_ok": lock_ok,
                                 "cancelled_at": None, "dataset": None}
        self.queue_end = start + (sum(d for _, d in steps[:2]) if not lock_ok else sum(d for _, d in steps))
        return eid

    def _state(self, e: dict) -> tuple[str, str]:
        now = self.clock.now()
        if e["cancelled_at"] is not None and now >= e["cancelled_at"]:
            return "Cancelled", ""
        if now < e["start"]:
            return "Queued", ""
        t = e["start"]
        for name, dur in e["steps"]:
            t += dur
            if name == "lock" and not e["lock_ok"] and now >= t:
                return "Error", f"lock failed: no 2H lock signal for {e['spec']['solvent']}"
            if now < t:
                return "Running", name
        return "Completed", ""

    def get_experiment(self, exp_id: str) -> dict:
        e = self.experiments[exp_id]
        state, step = self._state(e)
        return {"id": exp_id, "title": e["title"], "state": state, "step": step,
                "message": step if state == "Error" else ""}

    def find_experiments(self, title: str) -> list[dict]:
        return [{"id": e["id"], "title": e["title"], "state": self._state(e)[0]}
                for e in self.experiments.values() if matches_request(e["title"], title)]

    def cancel_experiment(self, exp_id: str) -> None:
        self.experiments[exp_id]["cancelled_at"] = self.clock.now()

    def read_dataset(self, exp_id: str) -> dict:
        e = self.experiments[exp_id]
        if self._state(e)[0] != "Completed":
            raise VendorRejected(f"experiment {exp_id} is {self._state(e)[0]}")
        folder = self.data_dir / e["title"] / "1"
        if e["dataset"] is None:
            folder.mkdir(parents=True, exist_ok=True)
            sw_hz = SW_PPM * SFO1
            shim = DRIFTED_SHIM_LW if e["drift"] else GOOD_SHIM_LW
            lines = [(p, a, NATURAL_LW + shim + lw) for p, a, lw in peptide_lines(e["tube"]["contents"], [exp_id])]
            rng = rng_for("nmr-noise", e["title"], exp_id)
            fid = fid_from_lines(lines, sw_hz=sw_hz, sfo1=SFO1, o1p=O1P, td=TD, phase0_deg=17.0,
                                 noise_sd=self.noise_sd * 8 / np.sqrt(e["spec"]["parameters"]["ns"]), rng=rng)
            np.save(folder / "fid.npy", fid)
            acqus = {"pulprog": "zgesgp", "ns": e["spec"]["parameters"]["ns"], "d1_s": e["spec"]["parameters"]["d1"],
                     "sw_hz": sw_hz, "sfo1_mhz": SFO1, "o1p_ppm": O1P, "td_complex": TD, "solvent": e["spec"]["solvent"],
                     "lock": "locked", "shim": e["spec"]["shim"], "temperature_k": 298.0,
                     "holder": e["spec"]["holder"], "sample_name": e["spec"]["sample_name"]}
            (folder / "acqus.json").write_text(json.dumps(acqus, indent=1))
            e["dataset"] = str(folder)
        files = [folder / "fid.npy", folder / "acqus.json"]
        return {"path": folder, "files": files, "fid": np.load(folder / "fid.npy"),
                "acqus": json.loads((folder / "acqus.json").read_text())}
