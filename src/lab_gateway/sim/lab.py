"""Wire the gateway to the three simulated vendor systems.

Scenarios:
  demo   the round shown in docs/: a swapped vial on the HPLC tray (the right tube waits
         in P1-A5), two lost LC-MS replies (one after SCIEX OS accepted the batch, one
         before), and a drifted shim on the first NMR experiment.
  clean  the same lab with no faults and the right tube in P1-A3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..adapters.openlab_scheduler import OpenLabSchedulerAdapter
from ..adapters.sciex_os import SciexOsAdapter
from ..adapters.spinpilot import SpinPilotAdapter
from ..clock import SimClock
from ..gateway import Gateway
from ..methods import MethodRegistry
from .hplc import SimSampleScheduler
from .lcms import SimSciexOs
from .nmr import SimSpinPilot


def hplc_tray(swapped: bool) -> dict[str, dict]:
    tray = {
        "P1-F1": {"barcode": "STD-REF-01", "contents": "REF-STD"},
        "P1-F2": {"barcode": "BLK-01", "contents": "BLANK"},
        "P1-A1": {"barcode": "BC-1001", "contents": "TOPHIT-01"},
        "P1-A2": {"barcode": "BC-1002", "contents": "TOPHIT-02"},
        "P1-A3": {"barcode": "BC-1003", "contents": "TOPHIT-03"},
        "P1-A4": {"barcode": "BC-1004", "contents": "TOPHIT-04"},
        "P1-A5": {"barcode": "BC-1003", "contents": "TOPHIT-03"},
    }
    if swapped:   # someone put another peptide's tube in P1-A3
        tray["P1-A3"] = {"barcode": "BC-1009", "contents": "TOPHIT-09"}
    return tray


NMR_HOLDERS = {
    12: {"contents": "TOPHIT-01", "solvent": "H2O/D2O 90/10"},
    13: {"contents": "TOPHIT-03", "solvent": "H2O/D2O 90/10"},
    14: {"contents": "TOPHIT-02", "solvent": "H2O/D2O 90/10"},
}


@dataclass
class SimLab:
    clock: SimClock
    gateway: Gateway
    hplc: SimSampleScheduler
    lcms: SimSciexOs
    nmr: SimSpinPilot


def build_sim_lab(run_dir: Path, scenario: str = "demo") -> SimLab:
    if scenario not in ("demo", "clean"):
        raise ValueError(f"unknown scenario {scenario!r}")
    demo = scenario == "demo"
    run_dir = Path(run_dir)
    clock = SimClock()
    vendor = run_dir / "vendor"
    hplc = SimSampleScheduler(clock, vendor / "openlab", hplc_tray(swapped=demo))
    lcms = SimSciexOs(clock, vendor / "sciex",
                      reply_faults=["lost_after_accept", "lost_before_accept"] if demo else [])
    nmr = SimSpinPilot(clock, vendor / "spinpilot", NMR_HOLDERS, shim_drift_runs=1 if demo else 0)
    adapters = {
        "HPLC-01": OpenLabSchedulerAdapter(hplc.commands_dir, hplc.trash_dir, hplc, "HPLC-01"),
        "LCMS-01": SciexOsAdapter(lcms),
        "NMR-01": SpinPilotAdapter(nmr),
    }
    gw = Gateway(adapters, MethodRegistry.load(), run_dir, clock)
    return SimLab(clock, gw, hplc, lcms, nmr)
