"""The whole round through a real MCP stdio connection, as an agent would run it."""

import asyncio
import json

from lab_gateway.campaign import run_demo


def test_demo_round_over_mcp(tmp_path):
    out = tmp_path / "demo"
    decision = asyncio.run(run_demo(out, "demo"))
    ranking = [r["peptide"] for r in decision["ranking_by_plasma_half_life"]]
    assert ranking == ["TOPHIT-01", "TOPHIT-03", "TOPHIT-02"]
    assert decision["ledger"]["ok"] is True
    assert decision["ranking_by_plasma_half_life"][0]["nmr_confirmed"] is True

    state = json.loads((out / "state.json").read_text())
    assert state["r1-lcms-stability"]["history"][1]["state"] == "unknown"        # reply lost after accept
    assert state["r1-lcms-stability-03"]["attempt"] == 2                          # operator resubmitted
    assert state["r1-nmr-tophit-01"]["result"]["usable"] is False                 # finished, not usable
    assert state["r1-hplc-purity"]["result"]["samples"][2]["status"] == "not_injected"

    actions = [json.loads(line)["action"] for line in (out / "ledger.jsonl").read_text().splitlines()]
    assert actions.count("reconcile.found") == 1 and actions.count("operator.resubmit") == 1
    tools = {json.loads(line)["tool"] for line in (out / "trace.jsonl").read_text().splitlines()}
    assert tools == {"list_instruments", "request_measurement", "get_measurement", "read_ledger"}
