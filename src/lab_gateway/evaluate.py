"""The QC gate. A completed run and a usable result are two separate decisions.

Each approved method carries its acceptance criteria. A run that finished on the
instrument but misses them comes back DONE with usable=False and the reasons, and the
planner may not feed it into the next design round.
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from . import analysis as an
from .methods import ApprovedMethod
from .models import Sample


def check(name: str, value, op: str, limit, unit: str = "") -> dict:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        ok = False
    elif op == "<=":
        ok = value <= limit
    elif op == ">=":
        ok = value >= limit
    elif op == "==":
        ok = value == limit
    elif op == "in":
        ok = limit[0] <= value <= limit[1]
    else:
        raise ValueError(op)
    shown = round(value, 3) if isinstance(value, float) else value
    return {"name": name, "value": shown, "limit": f"{op} {limit} {unit}".strip(), "pass": bool(ok)}


# ---------------------------------------------------------------- HPLC purity
def evaluate_hplc(fetched: dict, samples: list[Sample], method: ApprovedMethod) -> dict:
    p, q = method.params, method.qc
    inj = fetched["injections"]

    def main_peak(i):
        peaks = an.find_peaks(i["t"], i["y"], t_start=p["integration_start_min"])
        return max(peaks, key=lambda pk: pk.area) if peaks else None

    sst = [main_peak(i) for i in inj if i["type"] == "SystemSuitability" and i["injected"]]
    sst = [pk for pk in sst if pk is not None]
    checks = [check("SST injections integrated", len(sst), "==", method.template["sst_injections"])]
    if sst:
        mean_rt = float(np.mean([pk.rt for pk in sst]))
        mean_area = float(np.mean([pk.area for pk in sst]))
        checks += [
            check("SST retention time RSD", an.rsd_pct([pk.rt for pk in sst]), "<=", q["sst_rt_rsd_max_pct"], "%"),
            check("SST peak area RSD", an.rsd_pct([pk.area for pk in sst]), "<=", q["sst_area_rsd_max_pct"], "%"),
            check("SST USP tailing (worst)", max(pk.tailing for pk in sst), "<=", q["tailing_max"]),
            check("SST plate count (lowest)", min(pk.plates for pk in sst), ">=", q["plates_min"]),
        ]
        brackets = [main_peak(i) for i in inj if i["type"] == "BracketingStandard" and i["injected"]]
        if method.template.get("bracketing_standard"):
            drift = abs(brackets[0].rt - mean_rt) / mean_rt * 100 if brackets and brackets[0] else None
            checks.append(check("Bracketing standard RT drift", drift, "<=", q["bracket_rt_drift_max_pct"], "%"))
        blanks = [i for i in inj if i["type"] == "Blank" and i["injected"]]
        worst = 0.0
        for b in blanks:
            peaks = an.find_peaks(b["t"], b["y"], t_start=p["integration_start_min"])
            if peaks:
                worst = max(worst, max(pk.area for pk in peaks) / mean_area * 100)
        checks.append(check("Largest blank peak, % of standard", worst, "<=", q["blank_max_area_pct_of_standard"], "%"))
    usable = all(c["pass"] for c in checks)

    rows, detail = [], []
    by_id = {i["sample_id"]: i for i in inj if i.get("sample_id")}
    for s in samples:
        i = by_id.get(s.sample_id)
        if i is None or not i["injected"]:
            rows.append({"sample_id": s.sample_id, "vial": s.position, "status": "not_injected",
                         "reason": (i or {}).get("message", "no analysis row"), "usable": False})
            continue
        peaks = an.find_peaks(i["t"], i["y"], t_start=p["integration_start_min"])
        purity, main, table = an.area_percent(peaks, p["disregard_area_pct"])
        rows.append({"sample_id": s.sample_id, "vial": s.position, "status": "measured",
                     "purity_pct": round(purity, 2), "main_rt_min": round(main.rt, 3) if main else None,
                     "peaks": len(table), "usable": usable})
        detail.append({"sample_id": s.sample_id, "peaks": table})
    return {"usable": usable, "checks": checks, "samples": rows,
            "findings": {"purity_pct": {r["sample_id"]: r.get("purity_pct") for r in rows}},
            "detail": {"peak_tables": detail}}


# ---------------------------------------------------------------- LC-MS plasma stability
def evaluate_lcms(fetched: dict, samples: list[Sample], method: ApprovedMethod) -> dict:
    p, q = method.params, method.qc
    win = tuple(p["rt_window_min"])
    lloq = p["lloq_pct"]
    inj = fetched["injections"]
    quant = [i for i in inj if i["type"] in ("Unknown", "Quality Control")]
    # Retention time from the internal standard in each injection, then fixed-window areas.
    rt_of: dict[str, list[float]] = defaultdict(list)
    for i in quant:
        s = an.largest_peak(i["t"], i["xic_istd"], win)
        i["rt"] = s.rt if s else None
        if s:
            rt_of[i["analyte"]].append(s.rt)
    all_rts = [r for v in rt_of.values() for r in v]
    for i in inj:
        if i.get("rt") is None:
            pool = rt_of.get(i.get("carryover_of") or i.get("analyte"), all_rts)
            i["rt"] = float(np.median(pool)) if pool else float(np.mean(win))
        i["analyte_area"] = max(0.0, an.window_area(i["t"], i["xic_analyte"], i["rt"]))
        i["istd_area"] = max(0.0, an.window_area(i["t"], i["xic_istd"], i["rt"]))

    is_areas = np.array([i["istd_area"] for i in quant])
    med = float(np.median(is_areas)) if len(is_areas) else 0.0
    lo, hi = q["is_window_pct_of_median"]
    outside = [i["sample_name"] for i in quant if not lo <= (i["istd_area"] / med * 100 if med else 0) <= hi]
    checks = [
        check("Internal standard CV", an.rsd_pct(is_areas), "<=", q["is_cv_max_pct"], "%"),
        check(f"Injections with IS outside {lo}-{hi} % of median", len(outside), "==", 0),
    ]

    groups: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    kinds: dict[str, str] = {}
    for i in quant:
        ratio = i["analyte_area"] / i["istd_area"] if i["istd_area"] > 0 else float("nan")
        groups[i["analyte"]][i["time_min"]].append(ratio)
        kinds[i["analyte"]] = "control" if i["type"] == "Quality Control" else "sample"
        i["ratio"] = ratio

    rows, curves = [], {}
    t0_area = {}
    for analyte, by_t in sorted(groups.items()):
        t0 = float(np.nanmean(by_t[0])) if 0 in by_t else float("nan")
        t0_area[analyte] = float(np.mean([i["analyte_area"] for i in quant
                                          if i["analyte"] == analyte and i["time_min"] == 0]))
        pct = {t: float(np.nanmean(v)) / t0 * 100 for t, v in sorted(by_t.items())}
        fit_t = [t for t, v in pct.items() if v >= lloq]
        fit = an.fit_first_order(fit_t, [pct[t] for t in fit_t]) if len(fit_t) >= q["min_points_for_fit"] else None
        t_half = fit["t_half_min"] if fit else None
        rows.append({
            "analyte": analyte, "kind": kinds[analyte],
            "t_half_min": round(t_half, 1) if t_half else None,
            "beyond_window": bool(t_half and t_half > max(p["time_points_min"])),
            "r2": round(fit["r2"], 4) if fit else None,
            "pct_remaining": {str(t): (round(v, 2) if v >= lloq else "BLQ") for t, v in pct.items()},
        })
        curves[analyte] = {"t": list(pct), "pct": [round(v, 4) for v in pct.values()],
                           "per_injection": [{"t": t, "pct": round(r / t0 * 100, 4)}
                                             for t, rs in sorted(by_t.items()) for r in rs]}

    carry = []
    for i in inj:
        if i.get("carryover_of") in t0_area:
            lloq_area = lloq / 100 * t0_area[i["carryover_of"]]
            carry.append(i["analyte_area"] / lloq_area * 100)
    checks.append(check("Carryover in blank after T0, % of LLOQ response", max(carry) if carry else None,
                        "<=", q["carryover_max_pct_of_lloq"], "%"))
    controls = [r for r in rows if r["kind"] == "control"]
    ctl_t = controls[0]["t_half_min"] if controls else None
    checks.append(check("Plasma activity: control half-life", ctl_t, "<=", q["control_t_half_max_min"], "min"))
    usable = all(c["pass"] for c in checks)
    for r in rows:
        r["usable"] = usable and r["t_half_min"] is not None
    return {"usable": usable, "checks": checks, "samples": rows,
            "findings": {"t_half_min": {r["analyte"]: r["t_half_min"] for r in rows}},
            "detail": {"curves": curves, "is_median": med,
                       "injections": [{"name": i["sample_name"], "type": i["type"],
                                       "istd_pct_of_median": round(i["istd_area"] / med * 100, 2) if med else None}
                                      for i in inj]}}


# ---------------------------------------------------------------- NMR
def evaluate_nmr(fetched: dict, samples: list[Sample], method: ApprovedMethod) -> dict:
    p, q = method.params, method.qc
    acq = fetched["acqus"]
    proc = an.process_1d(np.asarray(fetched["fid"]), acq["sw_hz"], acq["sfo1_mhz"], acq["o1p_ppm"], lb_hz=p["lb_hz"])
    ppm, real, shift = an.reference_and_baseline(proc["ppm"], proc["real"], p["noise_region_ppm"])
    lw = an.linewidth_hz(ppm, real, 0.0, acq["sfo1_mhz"])
    s2n = an.snr(ppm, real, p["snr_region_ppm"], p["noise_region_ppm"])
    noise = an.nmr_noise(ppm, real, p["noise_region_ppm"])
    checks = [
        check("Lock", acq.get("lock"), "==", "locked"),
        check("DSS linewidth at half height", lw, "<=", q["ref_linewidth_max_hz"], "Hz"),
        check(f"Signal-to-noise, {p['snr_region_ppm'][0]}-{p['snr_region_ppm'][1]} ppm", s2n, ">=", q["snr_min"]),
    ]
    usable = all(c["pass"] for c in checks)
    s = samples[0]
    exp = s.attrs.get("expect_singlets")
    structure = None
    if exp:
        found = an.singlets_in(ppm, real, exp["window_ppm"], acq["sfo1_mhz"], threshold=20 * noise)
        structure = {"expected": f"{exp['count']} methyl singlets in {exp['window_ppm'][0]}-{exp['window_ppm'][1]} ppm",
                     "found_ppm": found, "confirmed": len(found) >= exp["count"]}
    row = {"sample_id": s.sample_id, "status": "measured", "usable": usable,
           "dss_linewidth_hz": round(lw, 3), "snr": round(s2n, 1), "structure_check": structure}
    return {"usable": usable, "checks": checks, "samples": [row],
            "findings": {"structure_confirmed": structure["confirmed"] if structure else None},
            "detail": {"phase0_deg": round(proc["phase0_deg"], 2), "reference_shift_ppm": round(shift, 5),
                       "acqus": acq},
            "arrays": {"ppm": ppm, "spectrum": real}}


EVALUATORS = {"hplc": evaluate_hplc, "lcms": evaluate_lcms, "nmr": evaluate_nmr}
