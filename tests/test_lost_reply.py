"""A lost reply never becomes a second injection."""

from conftest import plate, wait

from lab_gateway.gateway import Gateway
from lab_gateway.methods import MethodRegistry
from lab_gateway.operator_cli import pending, write_decision


def actions(gw, rid):
    return [e["action"] for e in gw.ledger.read() if e["request_id"] == rid]


def test_reply_lost_after_accept_resumes_without_resubmitting(clean):
    clean.lcms.reply_faults = ["lost_after_accept"]
    gw = clean.gateway
    v = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-lost")
    assert v["state"] == "submitted" and "nothing resubmitted" in v["note"]
    assert len(clean.lcms.batches) == 1
    assert actions(gw, "r-lost") == ["reserve", "submit.sent", "submit.reply_lost", "reconcile.found"]
    assert wait(gw, "r-lost")["usable"] is True


def test_reply_lost_before_accept_holds_the_instrument_for_an_operator(clean):
    clean.lcms.reply_faults = ["lost_before_accept"]
    gw = clean.gateway
    assert gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-gone")["state"] == "unknown"
    v = wait(gw, "r-gone")
    assert v["state"] == "needs_operator" and clean.lcms.batches == {}
    # the instrument stays held, and the agent cannot cancel its way out
    blocked = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-02"]), "r-next")
    assert blocked["state"] == "refused" and "needs_operator" in blocked["reasons"][0]
    assert "only an operator" in gw.cancel_measurement("r-gone", "impatient")["note"]
    assert pending(gw.run_dir)[0]["request_id"] == "r-gone"
    # the operator checks SCIEX OS and decides; the gateway applies it on its next poll
    write_decision(gw.run_dir, "r-gone", "resubmit", "Ana", "no batch in queue or history")
    v = wait(gw, "r-gone")
    assert v["state"] == "done" and v["usable"] is True and len(clean.lcms.batches) == 1
    entry = [e for e in gw.ledger.read() if e["action"] == "operator.resubmit"][0]
    assert entry["actor"] == "operator:Ana" and entry["detail"]["note"] == "no batch in queue or history"
    assert gw.ledger.verify()["ok"]


def test_two_vendor_entries_for_one_request_go_to_an_operator(clean):
    gw = clean.gateway
    # someone already re-entered the batch by hand in SCIEX OS with the same name
    clean.lcms.submit_batch({"name": "r-dup", "lc_method": "PlasmaStab_LC_v2", "ms_method": "PlasmaStab_MRM_v2",
                             "injection_time_s": 180, "samples": [{"sample_name": "x", "type": "Double Blank"}]})
    clean.lcms.reply_faults = ["lost_after_accept"]
    v = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-dup")
    assert v["state"] == "needs_operator" and "2 vendor entries" in v["note"]


def test_operator_abandon_releases_the_instrument(clean):
    clean.lcms.reply_faults = ["lost_before_accept"]
    gw = clean.gateway
    gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-drop")
    wait(gw, "r-drop")
    v = gw.operator_decision("r-drop", "abandon", "Ana", "plate discarded")
    assert v["state"] == "cancelled"
    assert gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-02"]), "r-after")["state"] == "submitted"


def test_gateway_restart_keeps_the_unknown_outcome(clean):
    clean.lcms.reply_faults = ["lost_before_accept"]
    gw = clean.gateway
    assert gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-crash")["state"] == "unknown"
    # the gateway process dies; a new one starts from the state file and the ledger on disk
    gw2 = Gateway(gw.adapters, MethodRegistry.load(), gw.run_dir, clean.clock)
    assert gw2.records["r-crash"]["state"] == "unknown"
    assert gw2.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-02"]), "r-x")["state"] == "refused"
    assert wait(gw2, "r-crash")["state"] == "needs_operator"
    gw2.operator_decision("r-crash", "resubmit", "Ana", "nothing in SCIEX OS")
    assert wait(gw2, "r-crash")["state"] == "done"
    assert gw2.ledger.verify()["ok"] and len(clean.lcms.batches) == 1
