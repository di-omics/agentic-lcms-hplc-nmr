"""One design round, driven through the MCP server like any agent would drive it.

The planner here is a fixed policy, so the demo is reproducible; a model-based planner
(Claude through MCP) calls exactly the same tools. A simulated operator steps in twice,
for the two things an agent must not decide alone: a physical vial fix, and an injection
whose outcome nobody could establish.

  lab-demo --out runs/demo            (scenario "demo": faults on)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from .operator_cli import write_decision

TIME_POINTS = [0, 5, 15, 30, 60, 120, 180, 240]
DONE = ("done", "failed", "cancelled", "needs_operator")
EXPECT_SINGLETS = {"window_ppm": [1.40, 1.56], "count": 2}
NMR_HOLDER = {"TOPHIT-01": 12, "TOPHIT-03": 13, "TOPHIT-02": 14}


def plasma_plate(analytes: list[str], control: str = "STAB-CTRL") -> list[dict]:
    """The injection plate the plasma stability bench hands over: analyte x time x replicate."""
    wells, k = [], 0
    for a in analytes + [control]:
        for t in TIME_POINTS:
            for rep in (1, 2, 3):
                wells.append({"sample_id": a, "position": f"{'ABCDEFGH'[k // 12]}{k % 12 + 1}",
                              "kind": "control" if a == control else "sample",
                              "attrs": {"analyte": a, "time_min": t, "replicate": rep}})
                k += 1
    return wells


class Planner:
    def __init__(self, client: Client, run_dir: Path) -> None:
        self.client = client
        self.run_dir = run_dir
        self.log: list[dict] = []
        self.calls = 0

    async def call(self, tool: str, args: dict | None = None) -> dict:
        self.calls += 1
        res = await self.client.call_tool(tool, args or {})
        if res.structured_content is not None:
            data = res.structured_content
            return data.get("result", data) if set(data) == {"result"} else data
        return json.loads(res.content[0].text)

    def say(self, who: str, text: str, t_min: float | None = None, **data) -> None:
        self.log.append({"who": who, "t_min": t_min, "say": text, **({"data": data} if data else {})})
        print(f"[{'' if t_min is None else f'{t_min:7.1f} min'}] {who:8s} {text}", flush=True)

    async def wait(self, request_id: str) -> dict:
        while True:
            v = await self.call("get_measurement", {"request_id": request_id, "wait_s": 3600})
            if v["state"] in DONE:
                return v

    async def run(self) -> dict:
        tools = await self.client.list_tools()
        self.say("planner", "connected; tools: " + ", ".join(t.name for t in tools.tools))
        lab = await self.call("list_instruments")
        for i in lab["instruments"]:
            self.say("planner", f"{i['instrument']}: {i['vendor_software']} via {i['interface']}; "
                                f"ready={i['ready']}", lab["sim_time_min"])

        # 1. purity of the four top hits from the hall
        hits = {"TOPHIT-01": ("P1-A1", "BC-1001"), "TOPHIT-02": ("P1-A2", "BC-1002"),
                "TOPHIT-03": ("P1-A3", "BC-1003"), "TOPHIT-04": ("P1-A4", "BC-1004")}
        manifest = [{"sample_id": s, "position": p, "barcode": b} for s, (p, b) in hits.items()]
        v = await self.call("request_measurement", {"instrument": "HPLC-01", "method_version": "HPLC-PEPTIDE-PURITY@2",
                                                    "sample_manifest": manifest, "request_id": "r1-hplc-purity"})
        self.say("planner", f"purity requested for 4 top hits: {v['state']} ({v['note']})", v["sim_time_min"])
        v = await self.wait("r1-hplc-purity")
        self.say("gateway", f"purity run {v['state']}, usable={v['usable']}: {v['note']}", v["sim_time_min"])
        purity, to_stability = {}, []
        for s in v["samples"]:
            if s["status"] != "measured":
                self.say("planner", f"{s['sample_id']} not measured: {s['reason']}. Asking the operator to check the vial.",
                         v["sim_time_min"])
                continue
            purity[s["sample_id"]] = s["purity_pct"]
            if v["usable"] and s["purity_pct"] >= 90.0:
                to_stability.append(s["sample_id"])
            else:
                self.say("planner", f"{s['sample_id']} purity {s['purity_pct']} % is below 90 %: repurify before stability",
                         v["sim_time_min"])
        self.say("planner", f"purity >= 90 %: {', '.join(to_stability)}", v["sim_time_min"], purity=purity)

        # 2. plasma stability on the LC-MS; the first draft names the newest method version
        args = {"instrument": "LCMS-01", "method_version": "LCMS-PLASMA-STAB@3",
                "sample_manifest": plasma_plate(to_stability), "request_id": "r1-lcms-stability"}
        v = await self.call("request_measurement", args)
        self.say("gateway", f"{v['state']}: {'; '.join(v['reasons'])}", None)
        args["method_version"] = "LCMS-PLASMA-STAB@2"
        v = await self.call("request_measurement", args)
        self.say("gateway", f"stability batch ({len(args['sample_manifest'])} wells): {v['state']}. {v['note']}",
                 v["sim_time_min"])
        v = await self.wait("r1-lcms-stability")
        self.say("gateway", f"stability run {v['state']}, usable={v['usable']}: {v['note']}", v["sim_time_min"])
        t_half = {s["analyte"]: s["t_half_min"] for s in v["samples"] if s["kind"] == "sample" and s["usable"]}
        self.say("planner", "half-life in plasma (min): " + ", ".join(f"{k} {x}" for k, x in t_half.items()),
                 v["sim_time_min"], t_half=t_half)

        if not t_half:
            self.say("planner", "no usable stability result: the round stops here; nothing feeds the next design")
            return {"next_round": None, "reason": "stability run not usable"}

        # 3. NMR on the most stable hit
        best = max(t_half, key=t_half.get)
        nmr = [{"sample_id": best, "position": f"holder:{NMR_HOLDER[best]}",
                "attrs": {"solvent": "H2O/D2O 90/10", "expect_singlets": EXPECT_SINGLETS}}]
        v = await self.call("request_measurement", {"instrument": "NMR-01", "method_version": "NMR-1H-PEPTIDE@1",
                                                    "sample_manifest": nmr, "request_id": f"r1-nmr-{best.lower()}"})
        v = await self.wait(f"r1-nmr-{best.lower()}")
        failed = [c for c in v["qc"] if not c["pass"]]
        self.say("gateway", f"NMR {best} {v['state']}, usable={v['usable']}: "
                            + "; ".join(f"{c['name']} {c['value']} (limit {c['limit']})" for c in failed),
                 v["sim_time_min"])
        if not v["usable"]:
            self.say("planner", "the spectrum finished but is not usable; asking again with a full reshim", v["sim_time_min"])
            rid = f"r1-nmr-{best.lower()}-reshim"
            await self.call("request_measurement", {"instrument": "NMR-01", "method_version": "NMR-1H-PEPTIDE@1",
                                                    "sample_manifest": nmr, "request_id": rid,
                                                    "options": {"reshim": True}})
            v = await self.wait(rid)
        sc = v["samples"][0]
        self.say("gateway", f"NMR {best} usable={v['usable']}: DSS {sc['dss_linewidth_hz']} Hz, S/N {sc['snr']}; "
                            f"designed residue singlets at {sc['structure_check']['found_ppm']} ppm, "
                            f"confirmed={sc['structure_check']['confirmed']}", v["sim_time_min"])
        nmr_ok = {best: bool(v["usable"] and sc["structure_check"]["confirmed"])}

        # 4. the swapped vial: a person fixes it, then the hit goes through purity and stability
        self.say("operator", "found another peptide's tube in P1-A3; put TOPHIT-03 (BC-1003) in P1-A5")
        v = await self.call("request_measurement", {"instrument": "HPLC-01", "method_version": "HPLC-PEPTIDE-PURITY@2",
                                                    "sample_manifest": [{"sample_id": "TOPHIT-03", "position": "P1-A5",
                                                                         "barcode": "BC-1003"}],
                                                    "request_id": "r1-hplc-purity-03"})
        v = await self.wait("r1-hplc-purity-03")
        p3 = v["samples"][0]
        purity["TOPHIT-03"] = p3.get("purity_pct")
        self.say("gateway", f"TOPHIT-03 purity {p3.get('purity_pct')} %, usable={v['usable']}", v["sim_time_min"])

        rid = "r1-lcms-stability-03"
        v = await self.call("request_measurement", {"instrument": "LCMS-01", "method_version": "LCMS-PLASMA-STAB@2",
                                                    "sample_manifest": plasma_plate(["TOPHIT-03"]), "request_id": rid})
        self.say("gateway", f"stability batch for TOPHIT-03: {v['state']}. {v['note']}", v["sim_time_min"])
        v = await self.wait(rid)
        if v["state"] == "needs_operator":
            self.say("gateway", f"{v['state']}: {v['note']}", v["sim_time_min"])
            again = await self.call("request_measurement", {"instrument": "LCMS-01", "method_version": "LCMS-PLASMA-STAB@2",
                                                            "sample_manifest": plasma_plate(["TOPHIT-02"]),
                                                            "request_id": "r1-lcms-other"})
            self.say("planner", f"tried another LC-MS batch meanwhile: {again['state']}: {'; '.join(again['reasons'])}",
                     again["sim_time_min"])
            note = "SCIEX OS queue and acquisition history show no batch r1-lcms-stability-03; no injection happened"
            write_decision(self.run_dir, rid, "resubmit", "operator (simulated)", note)
            self.say("operator", f"checked SCIEX OS: {note}. Decision: resubmit")
            v = await self.wait(rid)
        t3 = next(s for s in v["samples"] if s["analyte"] == "TOPHIT-03")
        if v["usable"]:
            t_half["TOPHIT-03"] = t3["t_half_min"]
        self.say("gateway", f"TOPHIT-03 stability {v['state']}, usable={v['usable']}: half-life {t3['t_half_min']} min",
                 v["sim_time_min"])

        # 5. the decision for the next round, from usable results only
        ledger = await self.call("read_ledger", {"limit": 200})
        ranking = sorted(t_half, key=t_half.get, reverse=True)
        decision = {
            "ranking_by_plasma_half_life": [{"peptide": p, "t_half_min": t_half[p], "purity_pct": purity.get(p),
                                             "nmr_confirmed": nmr_ok.get(p)} for p in ranking],
            "next_round": f"build on {ranking[0]} (most stable, designed residue confirmed by NMR); "
                          f"queue NMR for {ranking[1]}",
            "held_back": {"TOPHIT-04": "purity 86.9 % < 90 %: repurify, then stability"},
            "not_used": ["r1-nmr-tophit-01 (finished, failed the linewidth check)"],
            "ledger": ledger["verified"],
        }
        self.say("planner", "next round: " + decision["next_round"], None, decision=decision)
        self.say("planner", f"ledger: {ledger['verified']['entries']} entries, chain intact={ledger['verified']['ok']}")
        return decision


async def run_demo(out: Path, scenario: str = "demo") -> dict:
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    env = {**os.environ, "LAB_GATEWAY_RUN_DIR": str(out.resolve()), "LAB_GATEWAY_SCENARIO": scenario}
    params = StdioServerParameters(command=sys.executable, args=["-m", "lab_gateway.mcp_server"], env=env)
    async with Client(params) as client:
        planner = Planner(client, out)
        decision = await planner.run()
    (out / "campaign.json").write_text(json.dumps({"scenario": scenario, "tool_calls": planner.calls,
                                                   "log": planner.log, "decision": decision}, indent=1))
    return decision


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="lab-demo")
    ap.add_argument("--out", default="runs/demo")
    ap.add_argument("--scenario", default="demo", choices=("demo", "clean"))
    a = ap.parse_args(argv)
    asyncio.run(run_demo(Path(a.out), a.scenario))


if __name__ == "__main__":
    main()
