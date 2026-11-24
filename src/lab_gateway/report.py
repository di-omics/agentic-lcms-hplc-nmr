"""Build the run page and its figures from a run folder.

  python -m lab_gateway.report runs/demo docs/demo
"""

from __future__ import annotations

import csv
import json
import sys
from html import escape
from pathlib import Path

import numpy as np

from .svgplot import Axes, Canvas, envelope, legend, nice_ticks

ENTITY_SLOT = {"TOPHIT-01": 1, "TOPHIT-02": 2, "TOPHIT-03": 3, "STAB-CTRL": 4}
LABELS = {
    "r1-hplc-purity": "purity, 4 top hits",
    "r1-lcms-stability": "plasma stability, 2 hits + control",
    "r1-nmr-tophit-01": "1H NMR",
    "r1-nmr-tophit-01-reshim": "1H NMR again",
    "r1-hplc-purity-03": "purity, TOPHIT-03",
    "r1-lcms-stability-03": "stability, TOPHIT-03",
}
MARKS = {"submit.reply_lost": "reply lost", "reconcile.found": "found in SCIEX OS queue",
         "reconcile.unresolved": "operator needed", "operator.resubmit": "operator: resubmit"}


def load(run: Path) -> dict:
    return {
        "state": json.loads((run / "state.json").read_text()),
        "campaign": json.loads((run / "campaign.json").read_text()),
        "ledger": [json.loads(x) for x in (run / "ledger.jsonl").read_text().splitlines() if x.strip()],
        "trace": [json.loads(x) for x in (run / "trace.jsonl").read_text().splitlines() if x.strip()],
    }


def detail(run: Path, rid: str) -> dict:
    return json.loads((run / "results" / rid / "result.json").read_text())


# ---------------------------------------------------------------- figures
def fig_timeline(d: dict) -> str:
    recs = list(d["state"].values())
    t_end = max(h["t_min"] for r in recs for h in r["history"]) / 60
    cv = Canvas(900, 250, "The round on a timeline", "Each bar is one request on one instrument, in simulated hours.")
    ax = Axes(cv, 100, 30, 770, 160, (0, max(1.0, float(np.ceil(t_end)))), (0, 3))
    rows = {"HPLC-01": 0, "LCMS-01": 1, "NMR-01": 2}
    base = ax.y0 + ax.h
    for v in nice_ticks(*ax.xlim, n=8):
        x = ax.X(v)
        cv.add(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{ax.y0}" y2="{base}"/>')
        cv.text(x, base + 16, f"{v:g}", "s muted", "middle")
    cv.text(ax.x0 + ax.w / 2, base + 34, "simulated hours since the round started", "s muted", "middle")
    for name, i in rows.items():
        cv.text(ax.x0 - 10, ax.y0 + i * 55 + 30, name, "t ink2", "end")
    flip = {}
    for r in recs:
        i = rows[r["instrument"]]
        y = ax.y0 + i * 55 + 18
        h = r["history"]
        for a, b in zip(h, h[1:]):
            x1, x2 = ax.X(a["t_min"] / 60), ax.X(b["t_min"] / 60)
            held = a["state"] in ("unknown", "needs_operator")
            tip = f"{r['request_id']}: {a['state']} from {a['t_min']} to {b['t_min']} min. {a['note']}"
            if held:
                cv.add(f'<rect class="o held" x="{x1:.1f}" y="{y:.1f}" width="{max(x2 - x1, 2):.1f}" height="18" '
                       f'stroke-dasharray="3 2" stroke-width="1.5"><title>{escape(tip)}</title></rect>')
                if a["state"] == "needs_operator":
                    cv.text(x1 - 4, y + 13, "held for operator", "s ink2", "end")
            else:
                cv.add(f'<rect class="f1" x="{x1:.1f}" y="{y:.1f}" width="{max(x2 - x1, 2):.1f}" height="18" rx="2">'
                       f'<title>{escape(tip)}</title></rect>')
        k = flip.get(i, 0)
        flip[i] = k + 1
        x_start = ax.X(h[0]["t_min"] / 60)
        usable = (r.get("result") or {}).get("usable")
        tail = "" if usable is None else ("" if usable else ", not usable")
        ly = y - 5 if k % 2 == 0 else y + 31
        cv.text(x_start, ly, LABELS.get(r["request_id"], r["request_id"]) + tail, "s ink2")
    for e in d["ledger"]:
        if e["action"] in MARKS:
            i = rows[e["instrument"]]
            x = ax.X(e["t"] / 3600)
            y = ax.y0 + i * 55 + 18
            cv.add(f'<path class="ink" d="M{x:.1f},{y - 2:.1f} l4,-6 h-8 z"><title>{escape(MARKS[e["action"]])} '
                   f'at {e["t"] / 60:.0f} min</title></path>')
    return cv.svg()


def fig_hplc(run: Path, d: dict) -> str:
    panels = []
    for rid in ("r1-hplc-purity", "r1-hplc-purity-03"):
        rec = d["state"][rid]
        for s in rec["result"]["samples"]:
            if s["status"] != "measured":
                continue
            raw = [x["path"] for x in rec["result"]["raw"] if x["path"].endswith(f"_{s['sample_id']}.csv")][0]
            with open(raw) as f:
                rows = list(csv.DictReader(f))
            t = np.array([float(x["time_min"]) for x in rows])
            y = np.array([float(x["mAU"]) for x in rows])
            panels.append((s["sample_id"], s["purity_pct"], t, y))
    panels.sort(key=lambda p: p[0])
    ph = 78
    cv = Canvas(900, 40 + ph * len(panels) + 40, "HPLC purity at 214 nm",
                "One chromatogram per top hit, zoomed so the impurity peaks show; main peaks run off scale.")
    xlim = (10.6, 14.4)
    for k, (sid, purity, t, y) in enumerate(panels):
        ax = Axes(cv, 100, 30 + k * ph, 640, ph - 22, xlim, (-0.5, 12))
        base = ax.y0 + ax.h
        cv.add(f'<line class="axis" x1="{ax.x0}" x2="{ax.x0 + ax.w}" y1="{base}" y2="{base}"/>')
        m = (t >= xlim[0]) & (t <= xlim[1])
        ex, ey = envelope(t[m], y[m], *xlim, bins=640)
        ax.line(ex, ey, "s1", 1.5, tip=f"{sid}: {purity} % main-peak area at 214 nm")
        cv.text(ax.x0 - 10, ax.y0 + 24, sid, "t ink2", "end")
        cv.text(ax.x0 + ax.w + 14, ax.y0 + 20, f"purity {purity:.2f} %", "t ink")
        cv.text(ax.x0 + ax.w + 14, ax.y0 + 38, "main peak off scale", "s muted")
    base = 30 + len(panels) * ph - 22
    for v in nice_ticks(*xlim, n=8):
        x = 100 + (v - xlim[0]) / (xlim[1] - xlim[0]) * 640
        cv.text(x, base + 16, f"{v:g}", "s muted", "middle")
    cv.text(420, base + 34, "retention time, min (y: 0 to 12 mAU)", "s muted", "middle")
    return cv.svg()


def fig_stability(run: Path, d: dict) -> str:
    curves = dict(detail(run, "r1-lcms-stability")["detail"]["curves"])
    curves["TOPHIT-03"] = detail(run, "r1-lcms-stability-03")["detail"]["curves"]["TOPHIT-03"]
    t_half = {}
    for rid in ("r1-lcms-stability", "r1-lcms-stability-03"):
        for s in d["state"][rid]["result"]["samples"]:
            t_half.setdefault(s["analyte"], s["t_half_min"])
    cv = Canvas(900, 340, "Intact peptide in plasma", "Percent remaining against time, log scale, 3 replicates per point.")
    ax = Axes(cv, 70, 36, 600, 250, (0, 250), (0.3, 130), ylog=True)
    ax.frame("minutes in plasma at 37 C", "% remaining (analyte / internal standard, T0 = 100)",
             xticks=[0, 15, 30, 60, 120, 180, 240], yticks=[0.5, 1, 3, 10, 30, 100])
    ax.hline(0.5, "LLOQ 0.5 %")
    order = ["TOPHIT-01", "TOPHIT-03", "TOPHIT-02", "STAB-CTRL"]
    for name in order:
        c = curves[name]
        n = ENTITY_SLOT[name]
        pts = [(t, p) for t, p in zip(c["t"], c["pct"]) if p >= 0.5]
        ax.line([p[0] for p in pts], [p[1] for p in pts], f"s{n}", 2.0)
        for q in c["per_injection"]:
            if q["pct"] >= 0.5:
                ax.dot(q["t"], q["pct"], f"f{n}", 4.0, tip=f"{name}, {q['t']} min: {q['pct']:.1f} %")
        lx, ly = pts[-1]
        label = f"{name}  half-life {t_half[name]:.0f} min"
        cv.text(ax.X(lx) + 10, ax.Y(ly) + 4, label, "s ink")
    lx0, ly0 = ax.X(135), ax.Y(9)
    legend(cv, lx0, ly0, [(ENTITY_SLOT[n], n + (" (control)" if n == "STAB-CTRL" else "")) for n in order])
    cv.text(lx0, ly0 + 80, "Points below the LLOQ are not drawn and not fitted.", "s muted")
    return cv.svg()


def fig_istd(run: Path) -> str:
    inj = detail(run, "r1-lcms-stability")["detail"]["injections"]
    pts = [(k, i["istd_pct_of_median"]) for k, i in enumerate(inj) if i["type"] != "Double Blank"]
    cv = Canvas(900, 200, "Internal standard response",
                "Each injection's internal standard area as a percent of the batch median.")
    ax = Axes(cv, 70, 30, 780, 120, (-1, len(inj)), (0, 170))
    ax.hband(50, 150, "acceptance band 50 to 150 % of median")
    ax.frame("injection order in batch r1-lcms-stability", "IS area, % of median", yticks=[0, 50, 100, 150])
    for k, v in pts:
        ax.dot(k, v, "f1", 4.0, tip=f"{inj[k]['name']}: {v:.1f} %")
    return cv.svg()


def fig_nmr(run: Path, d: dict) -> str:
    good = "r1-nmr-tophit-01-reshim"
    ppm = np.load(run / "results" / good / "ppm.npy")
    spec = np.load(run / "results" / good / "spectrum.npy")
    ref = (np.abs(ppm) > 0.15) & ((ppm < 4.5) | (ppm > 4.95)) & (ppm > 0.5) & (ppm < 9.0)
    norm = spec / spec[ref].max()
    cv = Canvas(900, 320, "1H NMR of TOPHIT-01",
                "Water-suppressed 1H spectrum at 600 MHz with the DSS line before and after a full reshim.")
    ax = Axes(cv, 40, 36, 610, 230, (0.5, 9.0), (-0.05, 1.15), xflip=True)
    ax.frame("chemical shift, ppm", "intensity (tallest peptide peak = 1)", xticks=list(range(1, 10)),
             yticks=[0, 0.5, 1.0], ygrid=False)
    ex, ey = envelope(ppm, norm, 0.5, 9.0, bins=900)
    ax.line(ex, ey, "s1", 1.0, tip="1H spectrum, TOPHIT-01, after full reshim")
    for p in d["state"][good]["result"]["samples"][0]["structure_check"]["found_ppm"]:
        h = float(norm[np.argmin(np.abs(ppm - p))])
        cv.add(f'<line class="axis" x1="{ax.X(p):.1f}" x2="{ax.X(1.6):.1f}" y1="{ax.Y(h * 0.9):.1f}" '
               f'y2="{ax.Y(0.95):.1f}"/>')
    cv.text(ax.X(1.62), ax.Y(0.97), "designed residue: 2 methyl singlets", "s ink", "end")
    cv.text(ax.X(4.72), ax.Y(0.47), "water (suppressed)", "s muted", "middle")
    # DSS line shape, before and after
    bx = Axes(cv, 700, 36, 170, 150, (-4, 4), (-0.05, 1.1))
    bx.frame("Hz from DSS", "DSS line, own height = 1", xticks=[-4, -2, 0, 2, 4], yticks=[0, 1], ygrid=False)
    for rid, n in (("r1-nmr-tophit-01-reshim", 1), ("r1-nmr-tophit-01", 2)):
        p2 = np.load(run / "results" / rid / "ppm.npy")
        s2 = np.load(run / "results" / rid / "spectrum.npy")
        hz = p2 * 600.13
        m = np.abs(hz) <= 4
        bx.line(hz[m], s2[m] / s2[m].max(), f"s{n}", 2.0)
    lw = {rid: d["state"][rid]["result"]["samples"][0]["dss_linewidth_hz"]
          for rid in ("r1-nmr-tophit-01-reshim", "r1-nmr-tophit-01")}
    legend(cv, 700, 240, [(1, f"after full reshim, {lw['r1-nmr-tophit-01-reshim']:.2f} Hz"),
                          (2, f"first try, {lw['r1-nmr-tophit-01']:.2f} Hz (limit 1.0)")])
    return cv.svg()


# ---------------------------------------------------------------- page
CSS = """
:root{--bg:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--rule:#e1e0d9;--chip:#f4f3ef;--bad:#a8341d}
@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--rule:#2c2c2a;--chip:#242422;--bad:#e66767}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:300 16px/1.55 'Helvetica Neue',Helvetica,Arial,sans-serif}
main{max-width:940px;margin:0 auto;padding:48px 16px 96px}
h1{font-weight:300;font-size:34px;line-height:1.2;margin:0 0 12px}
h2{font-weight:400;font-size:20px;margin:56px 0 8px}
p{margin:8px 0;color:var(--ink2);max-width:760px}
.lead{font-size:18px;color:var(--ink)}
.tiles{display:flex;gap:28px;flex-wrap:wrap;margin:28px 0 8px}
.tile b{display:block;font-weight:300;font-size:30px;color:var(--ink)}
.tile span{font-size:13px;color:var(--muted)}
figure{margin:16px 0 4px;overflow-x:auto}
figure svg{max-width:100%;height:auto}
figcaption{font-size:13px;color:var(--muted);margin-top:4px}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}
th{font-weight:400;text-align:left;color:var(--muted);border-bottom:1px solid var(--rule);padding:6px 10px 6px 0}
td{border-bottom:1px solid var(--rule);padding:6px 10px 6px 0;vertical-align:top;color:var(--ink2)}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
td:first-child{white-space:nowrap}
.fail{color:var(--bad)}
code{font:13px ui-monospace,Menlo,monospace;background:var(--chip);padding:1px 4px;border-radius:3px}
details{margin:12px 0}
summary{cursor:pointer;color:var(--ink2)}
.foot{margin-top:64px;font-size:13px;color:var(--muted)}
"""


def table(headers: list[str], rows: list[list], num: set[int] = frozenset()) -> str:
    h = "".join(f"<th>{escape(x)}</th>" for x in headers)
    body = []
    for r in rows:
        cells = []
        for k, c in enumerate(r):
            cls = ' class="num"' if k in num else ""
            cells.append(f"<td{cls}>{c}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{h}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def qc_rows(result: dict) -> list[list]:
    out = []
    for c in result["checks"]:
        verdict = "pass" if c["pass"] else '<span class="fail">fail</span>'
        out.append([escape(c["name"]), escape(str(c["value"])), escape(c["limit"]), verdict])
    return out


def build(run: Path, out: Path) -> Path:
    d = load(run)
    out.mkdir(parents=True, exist_ok=True)
    figs = {"timeline": fig_timeline(d), "hplc": fig_hplc(run, d), "stability": fig_stability(run, d),
            "istd": fig_istd(run), "nmr": fig_nmr(run, d)}
    (out / "figures").mkdir(exist_ok=True)
    for k, svg in figs.items():
        (out / "figures" / f"{k}.svg").write_text(svg)
    st, camp = d["state"], d["campaign"]
    sim_h = max(h["t_min"] for r in st.values() for h in r["history"]) / 60
    led_ok = camp["decision"]["ledger"]

    log_rows = [[f"{x['t_min']:.0f}" if x["t_min"] is not None else "", escape(x["who"]), escape(x["say"])]
                for x in camp["log"]]
    p1 = st["r1-hplc-purity"]["result"]
    purity_rows = []
    for rid in ("r1-hplc-purity", "r1-hplc-purity-03"):
        for s in st[rid]["result"]["samples"]:
            purity_rows.append([s["sample_id"], s["vial"],
                                f"{s['purity_pct']:.2f} %" if s.get("purity_pct") is not None else "",
                                escape(s.get("reason", "measured")), rid])
    s1 = st["r1-lcms-stability"]["result"]
    stab_rows = []
    for rid in ("r1-lcms-stability", "r1-lcms-stability-03"):
        for s in st[rid]["result"]["samples"]:
            if rid.endswith("-03") and s["kind"] == "control":
                continue
            stab_rows.append([s["analyte"], s["kind"], f"{s['t_half_min']:.0f} min", f"{s['r2']:.3f}",
                              escape(s["pct_remaining"]["240"] if isinstance(s["pct_remaining"]["240"], str)
                                     else f"{s['pct_remaining']['240']:.1f} %"), rid])
    n_bad, n_good = st["r1-nmr-tophit-01"]["result"], st["r1-nmr-tophit-01-reshim"]["result"]
    led_rows = [[str(e["seq"]), f"{e['t'] / 60:.0f}", escape(e["actor"]), f"<code>{escape(e['action'])}</code>",
                 escape(e["request_id"] or ""), escape(_short(e["detail"]))] for e in d["ledger"]]
    trace_rows = [[f"{x['sim_time_min']:.0f}", f"<code>{escape(x['tool'])}</code>", escape(_short(x["args"])),
                   escape(_short(x["result"]))] for x in d["trace"]]
    dec = camp["decision"]
    rank_rows = [[r["peptide"], f"{r['t_half_min']:.0f} min", f"{r['purity_pct']:.2f} %",
                  {True: "confirmed", False: "not confirmed", None: "not run yet"}[r["nmr_confirmed"]]]
                 for r in dec["ranking_by_plasma_half_life"]]

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LC-MS, HPLC, NMR round</title><style>{CSS}</style></head><body><main>
<h1>Agentic LC-MS, HPLC and NMR: one round in simulation</h1>
<p class="lead">A planning agent asks for measurements through 5 MCP tools. The gateway turns each request into
the vendor's own unit of work, an Agilent Sample Scheduler XML command, a SCIEX OS batch or a Bruker SpinPilot
experiment, and the vendor software keeps control of the instrument.</p>
<p>Every instrument and every number here is simulated. No instrument or vendor software was connected.</p>
<div class="tiles">
<div class="tile"><b>{len(st)}</b><span>measurements</span></div>
<div class="tile"><b>{camp['tool_calls']}</b><span>MCP tool calls</span></div>
<div class="tile"><b>{sim_h:.1f} h</b><span>simulated lab time</span></div>
<div class="tile"><b>{led_ok['entries']}</b><span>ledger entries, chain intact: {'yes' if led_ok['ok'] else 'NO'}</span></div>
</div>

<h2>The round</h2>
<p>Purity first, then plasma stability for the hits that pass, then NMR on the most stable one. Four things go
wrong on purpose: a swapped vial, two lost replies from SCIEX OS, and a drifted shim. The open bar is the time
the LC-MS was held while nobody knew whether the batch had started.</p>
<figure>{figs['timeline']}<figcaption>Bars are requests; triangles mark lost replies, the reconcile and the operator's
decision. Hover for details.</figcaption></figure>
<details open><summary>What the planner, the gateway and the operator said</summary>
{table(['min', 'who', 'what'], log_rows, {0})}</details>

<h2>Decision for the next round</h2>
<p>{escape(dec['next_round'])}. Only results with <code>usable=true</code> are in this ranking.</p>
{table(['peptide', 'plasma half-life', 'purity', 'NMR structure check'], rank_rows, {1, 2})}
<p>Held back: TOPHIT-04, purity 86.9 % (repurify first). Not used: the first NMR spectrum, which finished but
failed the linewidth check.</p>

<h2>HPLC purity (Agilent OpenLab CDS through Sample Scheduler)</h2>
<p>The adapter writes one XML command per request into the LIMS commands folder, with a barcode check on every
sample vial. The vial in P1-A3 held another peptide's tube, so it was not injected and was reported, not hidden.</p>
<figure>{figs['hplc']}<figcaption>214 nm chromatograms, zoomed to show impurities.</figcaption></figure>
{table(['sample', 'vial', 'purity', 'status', 'request'], purity_rows, {2})}
<details><summary>System suitability, r1-hplc-purity</summary>{table(['check', 'value', 'limit', ''], qc_rows(p1), {1})}</details>

<h2>LC-MS plasma stability (SCIEX OS batch)</h2>
<p>8 time points from 0 to 240 min, 3 replicates, internal standard in the quench, and a control that plasma
breaks down fast: if the control survives, the plasma was not active and the run is not usable.</p>
<figure>{figs['stability']}<figcaption>Lines join replicate means; dots are single injections.</figcaption></figure>
{table(['peptide', 'kind', 'half-life', 'fit r2', 'at 240 min', 'request'], stab_rows, {2, 3, 4})}
<figure>{figs['istd']}<figcaption>Every injection inside the band; blanks carry no internal standard by design.</figcaption></figure>
<details><summary>Acceptance checks, r1-lcms-stability</summary>{table(['check', 'value', 'limit', ''], qc_rows(s1), {1})}</details>

<h2>NMR structure follow-up (Bruker SpinPilot on TopSpin 5)</h2>
<p>The first spectrum finished but its reference line was too broad, so the gateway returned it as not usable.
The planner asked again with a full reshim. LC-MS says the mass is right; NMR shows the designed residue's two
methyl singlets.</p>
<figure>{figs['nmr']}<figcaption>Left: the usable spectrum. Right: the DSS line from both tries.</figcaption></figure>
{table(['try', 'check', 'value', 'limit', ''], [['first'] + r for r in qc_rows(n_bad)] + [['reshim'] + r for r in qc_rows(n_good)], {2})}

<h2>When a reply is lost</h2>
<p><b>Found.</b> The first stability batch reached SCIEX OS and the reply did not come back. The gateway held the
LC-MS, searched the SCIEX OS queue and history for the request id, found batch B0001, and kept monitoring.
Nothing was submitted twice.</p>
<p><b>Not found.</b> The TOPHIT-03 batch never reached SCIEX OS. After a 10 minute grace period the gateway
stopped and asked for a person. A new LC-MS request was refused while the instrument was held. The operator
checked SCIEX OS, recorded the decision under their name, and the gateway resubmitted.</p>

<h2>Ledger</h2>
<p>Every call that changed an instrument, written before the call is made, each entry chained to the one before.</p>
<details><summary>{len(d['ledger'])} entries</summary>{table(['#', 'min', 'actor', 'action', 'request', 'detail'], led_rows, {0, 1})}</details>
<details><summary>{len(d['trace'])} MCP tool calls</summary>{table(['min', 'tool', 'arguments', 'result'], trace_rows, {0})}</details>

<p class="foot">Built from <code>lab-demo --out runs/demo</code> by <code>python -m lab_gateway.report</code>.
Simulated data; method names, timings and peptides are illustrative.</p>
</main></body></html>
"""
    page = out / "index.html"
    page.write_text(html)
    return page


def _short(obj, n: int = 140) -> str:
    s = json.dumps(obj, separators=(", ", ": "))
    return s if len(s) <= n else s[: n - 3] + "..."


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    run = Path(argv[0] if argv else "runs/demo")
    out = Path(argv[1] if len(argv) > 1 else "docs/demo")
    print(build(run, out))


if __name__ == "__main__":
    main()
