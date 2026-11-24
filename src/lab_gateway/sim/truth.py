"""What is physically in each vial, well and tube. The simulated instruments measure this;
the agent never reads it. Peptides carry numbers only. Every value is made up."""

from __future__ import annotations

import hashlib

import numpy as np

PEPTIDES = {
    # purity at 214 nm, HPLC main-peak RT, LC-MS RT, plasma half-life, designed-residue singlets (ppm)
    "TOPHIT-01": {"purity_pct": 97.6, "rt_min": 12.42, "lc_rt_min": 1.64, "t_half_min": 310.0, "singlets_ppm": [1.44, 1.51]},
    "TOPHIT-02": {"purity_pct": 96.1, "rt_min": 12.18, "lc_rt_min": 1.59, "t_half_min": 95.0, "singlets_ppm": []},
    "TOPHIT-03": {"purity_pct": 95.3, "rt_min": 12.71, "lc_rt_min": 1.70, "t_half_min": 140.0, "singlets_ppm": [1.45, 1.50]},
    "TOPHIT-04": {"purity_pct": 86.9, "rt_min": 12.95, "lc_rt_min": 1.73, "t_half_min": 160.0, "singlets_ppm": []},
    "TOPHIT-09": {"purity_pct": 93.0, "rt_min": 13.30, "lc_rt_min": 1.81, "t_half_min": 60.0, "singlets_ppm": []},
    "STAB-CTRL": {"purity_pct": 99.0, "rt_min": 9.80, "lc_rt_min": 1.21, "t_half_min": 12.0, "singlets_ppm": []},
    "REF-STD": {"purity_pct": 99.5, "rt_min": 11.20, "lc_rt_min": 1.50, "t_half_min": None, "singlets_ppm": []},
}


def rng_for(*parts) -> np.random.Generator:
    """Deterministic randomness per measurement, so a rerun reproduces the same data."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


def impurity_profile(peptide: str) -> list[tuple[float, float]]:
    """(RT offset from the main peak in min, area fraction) for each impurity."""
    p = PEPTIDES[peptide]
    impurity = 100.0 - p["purity_pct"]
    r = rng_for("impurities", peptide)
    if peptide == "TOPHIT-04":   # one large truncation product
        shares = [0.62, 0.14, 0.11, 0.08, 0.05]
        offsets = [-0.42, -0.71, -0.24, 0.31, 0.88]
    else:
        shares = list(r.dirichlet([3, 2, 2, 1]))
        offsets = [-0.55, -0.31, 0.28, 0.92]
    return [(o + r.normal(0, 0.01), impurity / 100 * s) for o, s in zip(offsets, shares)]
