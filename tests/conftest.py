import pytest

from lab_gateway.sim.lab import build_sim_lab

HITS = [("TOPHIT-01", "P1-A1", "BC-1001"), ("TOPHIT-02", "P1-A2", "BC-1002"),
        ("TOPHIT-03", "P1-A3", "BC-1003"), ("TOPHIT-04", "P1-A4", "BC-1004")]
SINGLETS = {"window_ppm": [1.40, 1.56], "count": 2}


def hplc_manifest(hits=HITS):
    return [{"sample_id": s, "position": p, "barcode": b} for s, p, b in hits]


def plate(analytes, control="STAB-CTRL"):
    wells, k = [], 0
    for a in list(analytes) + [control]:
        for t in (0, 5, 15, 30, 60, 120, 180, 240):
            for r in (1, 2, 3):
                wells.append({"sample_id": a, "position": f"{'ABCDEFGH'[k // 12]}{k % 12 + 1}",
                              "kind": "control" if a == control else "sample",
                              "attrs": {"analyte": a, "time_min": t, "replicate": r}})
                k += 1
    return wells


def nmr_manifest(sample="TOPHIT-01", holder=12, solvent="H2O/D2O 90/10"):
    return [{"sample_id": sample, "position": f"holder:{holder}",
             "attrs": {"solvent": solvent, "expect_singlets": SINGLETS}}]


def wait(gw, rid, rounds=100):
    for _ in range(rounds):
        v = gw.get_measurement(rid, wait_s=3600)
        if v["state"] in ("done", "failed", "cancelled", "needs_operator"):
            return v
    raise AssertionError(f"{rid} did not settle: {v['state']}")


@pytest.fixture
def clean(tmp_path):
    return build_sim_lab(tmp_path / "run", "clean")


@pytest.fixture
def demo(tmp_path):
    return build_sim_lab(tmp_path / "run", "demo")
