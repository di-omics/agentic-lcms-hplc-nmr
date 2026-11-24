"""Nothing reaches an instrument unless every check passes, and a request_id runs once."""

from conftest import hplc_manifest, plate, wait

from lab_gateway.methods import MethodRegistry
from lab_gateway.sim.vendor_methods import HPLC_METHODS, LCMS_METHODS, NMR_TEMPLATES
from lab_gateway.methods import fingerprint_text


def test_registry_matches_methods_stored_in_vendor_software():
    reg = MethodRegistry.load()
    hplc = reg.get("HPLC-PEPTIDE-PURITY@2")
    assert hplc.fingerprint == fingerprint_text(HPLC_METHODS[hplc.vendor_ref["method"]])
    lcms = reg.get("LCMS-PLASMA-STAB@2")
    assert lcms.fingerprint == fingerprint_text(LCMS_METHODS[lcms.vendor_ref["lc_method"]],
                                                LCMS_METHODS[lcms.vendor_ref["ms_method"]])
    nmr = reg.get("NMR-1H-PEPTIDE@1")
    assert nmr.fingerprint == fingerprint_text(NMR_TEMPLATES[nmr.vendor_ref["experiment"]])


def test_draft_method_is_refused_and_nothing_is_sent(clean):
    gw = clean.gateway
    v = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@3", plate(["TOPHIT-01"]), "r-draft")
    assert v["state"] == "refused" and v["sent_to_instrument"] is False
    assert "draft" in v["reasons"][0]
    assert gw.ledger.read() == [] and clean.lcms.batches == {}


def test_unknown_method_wrong_kind_and_unknown_instrument(clean):
    gw = clean.gateway
    assert "not in the approved" in gw.request_measurement("HPLC-01", "HPLC-X@9", hplc_manifest(), "r-a")["reasons"][0]
    assert "is a lcms method" in gw.request_measurement("HPLC-01", "LCMS-PLASMA-STAB@2", hplc_manifest(), "r-b")["reasons"][0]
    assert "unknown instrument" in gw.request_measurement("GC-01", "HPLC-PEPTIDE-PURITY@2", hplc_manifest(), "r-c")["reasons"][0]


def test_manifest_problems_are_reported_before_anything_is_sent(clean):
    gw = clean.gateway
    no_barcode = [{"sample_id": "TOPHIT-01", "position": "P1-A1"}]
    v = gw.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", no_barcode, "r-nobc")
    assert v["state"] == "refused" and "barcode is required" in v["reasons"][0]
    dup = hplc_manifest()[:2]
    dup[1]["position"] = dup[0]["position"]
    assert "appears twice" in gw.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", dup, "r-dup")["reasons"][0]
    on_std = [{"sample_id": "X", "position": "P1-F1", "barcode": "BC-9"}]
    assert "standard or blank" in gw.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", on_std, "r-std")["reasons"][0]
    no_ctrl = [w for w in plate(["TOPHIT-01"]) if w["kind"] == "sample"]
    assert "activity control" in gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", no_ctrl, "r-noctl")["reasons"][0]
    assert not list(clean.hplc.commands_dir.glob("*.xml"))


def test_option_not_allowed_by_the_method(clean):
    v = clean.gateway.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", hplc_manifest(), "r-opt",
                                          options={"flow_ml_min": 0.8})
    assert v["state"] == "refused" and "not allowed" in v["reasons"][0]


def test_method_edited_on_the_instrument_is_caught(clean):
    clean.lcms.methods["PlasmaStab_LC_v2"] += "flow_ml_min=0.80\n"   # someone edits the method in SCIEX OS
    v = clean.gateway.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-edited")
    assert v["state"] == "refused" and "differs from approved" in v["reasons"][0]
    assert clean.lcms.batches == {}


def test_dry_run_checks_everything_and_sends_nothing(clean):
    v = clean.gateway.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", hplc_manifest(), "r-dry", dry_run=True)
    assert v["state"] == "checked" and v["sent_to_instrument"] is False
    assert clean.gateway.records == {} and not list(clean.hplc.commands_dir.glob("*.xml"))


def test_same_request_id_runs_once(clean):
    gw = clean.gateway
    first = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-once")
    again = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-once")
    assert first["vendor_ref"] == again["vendor_ref"] and "nothing sent" in again["note"]
    assert len(clean.lcms.batches) == 1
    other = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-02"]), "r-once")
    assert other["state"] == "refused" and "different request" in other["reasons"][0]


def test_one_request_holds_an_instrument_at_a_time(clean):
    gw = clean.gateway
    gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-first")
    v = gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-02"]), "r-second")
    assert v["state"] == "refused" and "held by r-first" in v["reasons"][0]
    assert wait(gw, "r-first")["state"] == "done"
    assert gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-02"]), "r-second")["state"] == "submitted"


def test_cancel_a_running_batch(clean):
    gw = clean.gateway
    gw.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-cancel")
    gw.get_measurement("r-cancel", wait_s=600)
    v = gw.cancel_measurement("r-cancel", "plate dropped")
    assert v["state"] == "cancelled"
    assert clean.lcms.get_batch(v["vendor_ref"])["state"] == "Aborted"
    actions = [e["action"] for e in gw.ledger.read()]
    assert actions[-2:] == ["cancel.sent", "release"]
