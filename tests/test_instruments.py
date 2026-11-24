"""Each adapter against its simulated vendor system, and the QC gate on its data."""

import xml.etree.ElementTree as ET

import pytest
from conftest import HITS, hplc_manifest, nmr_manifest, plate, wait

from lab_gateway.models import Sample
from lab_gateway.sim import truth


# ---------------------------------------------------------------- HPLC / Sample Scheduler
def test_purity_matches_what_is_in_the_vials(clean):
    clean.gateway.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", hplc_manifest(), "r-p")
    v = wait(clean.gateway, "r-p")
    assert v["usable"] is True and all(c["pass"] for c in v["qc"])
    for s in v["samples"]:
        assert s["purity_pct"] == pytest.approx(truth.PEPTIDES[s["sample_id"]]["purity_pct"], abs=0.3)


def test_every_sample_vial_gets_a_barcode_check_in_the_xml(clean):
    adapter = clean.gateway.adapters["HPLC-01"]
    method = clean.gateway.methods.get("HPLC-PEPTIDE-PURITY@2")
    xml = adapter.build_command("r-x", [Sample(s, p, "sample", b) for s, p, b in HITS], method)
    seq = ET.fromstring(xml).find("Sequence")
    samples = [a for a in seq.findall("Analysis") if a.get("type") == "Sample"]
    assert len(samples) == 4
    assert all(a.find("Barcode").get("onMismatch") == "DoNotInject" for a in samples)
    types = [a.get("type") for a in seq.findall("Analysis")]
    assert types[:6] == ["Blank"] + ["SystemSuitability"] * 5 and types[-1] == "BracketingStandard"


def test_swapped_vial_is_not_injected(demo):
    v = demo.gateway.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", hplc_manifest(), "r-swap")
    v = wait(demo.gateway, "r-swap")
    row = next(s for s in v["samples"] if s["sample_id"] == "TOPHIT-03")
    assert row["status"] == "not_injected" and "expected BC-1003, read BC-1009" in row["reason"]
    assert v["usable"] is True        # the run's QC holds; the one vial is reported, not hidden


def test_why_the_barcode_section_is_always_written(demo):
    """Without a barcode section the scheduler injects whatever sits in the vial."""
    sched = demo.hplc
    cmd = (b"<?xml version='1.0' encoding='utf-8'?><SampleSchedulerCommand><Sequence name='hand-made' "
           b"instrument='HPLC-01' method='PeptidePurity_214nm_v2.amx' project='p'>"
           b"<Analysis order='1' name='TOPHIT-03' type='Sample' sampleId='TOPHIT-03' vial='P1-A3'/>"
           b"</Sequence></SampleSchedulerCommand>")
    (sched.commands_dir / "hand-made.xml").write_bytes(cmd)
    assert sched.analyses("hand-made")[0]["status"] in ("Waiting", "Running")   # the LIMS agent picked it up
    demo.clock.sleep(19 * 60)
    row = sched.analyses("hand-made")[0]
    assert row["status"] == "Completed"     # the wrong peptide (BC-1009) was injected


def test_invalid_command_goes_to_trash_and_the_request_fails(clean):
    clean.hplc.tray.pop("P1-F2")             # the blank vial position is no longer configured
    clean.gateway.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", hplc_manifest(), "r-trash")
    v = wait(clean.gateway, "r-trash")
    assert v["state"] == "failed" and "not on the tray" in v["note"]
    assert (clean.hplc.trash_dir / "r-trash.xml").exists()


def test_cancel_before_the_scan_withdraws_the_command(clean):
    clean.hplc.last_scan = 0.0
    clean.clock.sleep(1)                     # inside the scan interval: the file is still pending
    gw = clean.gateway
    gw.request_measurement("HPLC-01", "HPLC-PEPTIDE-PURITY@2", hplc_manifest(), "r-withdraw")
    assert (clean.hplc.commands_dir / "r-withdraw.xml").exists()
    assert gw.cancel_measurement("r-withdraw", "wrong plate")["state"] == "cancelled"
    clean.clock.sleep(120)
    assert clean.hplc.analyses("r-withdraw") == []


# ---------------------------------------------------------------- LC-MS / SCIEX OS
def test_half_lives_match_the_simulated_truth(clean):
    clean.gateway.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01", "TOPHIT-02"]), "r-s")
    v = wait(clean.gateway, "r-s")
    assert v["usable"] is True
    got = v["findings"]["t_half_min"]
    for pep in ("TOPHIT-01", "TOPHIT-02", "STAB-CTRL"):
        assert got[pep] == pytest.approx(truth.PEPTIDES[pep]["t_half_min"], rel=0.08)
    ctl = next(s for s in v["samples"] if s["analyte"] == "STAB-CTRL")
    assert ctl["pct_remaining"]["240"] == "BLQ"


def test_a_completed_run_with_dead_plasma_is_not_usable(clean, monkeypatch):
    monkeypatch.setitem(truth.PEPTIDES["STAB-CTRL"], "t_half_min", 900.0)   # the control barely degrades
    clean.gateway.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-dead")
    v = wait(clean.gateway, "r-dead")
    assert v["state"] == "done" and v["usable"] is False
    bad = [c["name"] for c in v["qc"] if not c["pass"]]
    assert bad == ["Plasma activity: control half-life"]
    assert all(s["usable"] is False for s in v["samples"])


def test_carryover_failure_is_caught(clean):
    clean.lcms.carryover_frac = 5e-3          # ten times the LLOQ fraction
    clean.gateway.request_measurement("LCMS-01", "LCMS-PLASMA-STAB@2", plate(["TOPHIT-01"]), "r-carry")
    v = wait(clean.gateway, "r-carry")
    assert v["usable"] is False and "Carryover" in [c["name"] for c in v["qc"] if not c["pass"]][0]


# ---------------------------------------------------------------- NMR / SpinPilot
def test_drifted_shim_finishes_but_is_not_usable_until_reshimmed(demo):
    gw = demo.gateway
    gw.request_measurement("NMR-01", "NMR-1H-PEPTIDE@1", nmr_manifest(), "r-n1")
    v = wait(gw, "r-n1")
    assert v["state"] == "done" and v["usable"] is False
    lw = next(c for c in v["qc"] if c["name"].startswith("DSS linewidth"))
    assert lw["pass"] is False and lw["value"] > 1.0
    gw.request_measurement("NMR-01", "NMR-1H-PEPTIDE@1", nmr_manifest(), "r-n2", options={"reshim": True})
    v = wait(gw, "r-n2")
    assert v["usable"] is True and v["samples"][0]["dss_linewidth_hz"] < 1.0


def test_structure_check_sees_the_designed_residue_only_where_it_is(clean):
    gw = clean.gateway
    gw.request_measurement("NMR-01", "NMR-1H-PEPTIDE@1", nmr_manifest("TOPHIT-01", 12), "r-yes")
    yes = wait(gw, "r-yes")["samples"][0]["structure_check"]
    gw.request_measurement("NMR-01", "NMR-1H-PEPTIDE@1", nmr_manifest("TOPHIT-02", 14), "r-no")
    no = wait(gw, "r-no")["samples"][0]["structure_check"]
    assert yes["confirmed"] is True and yes["found_ppm"] == [1.44, 1.51]
    assert no["confirmed"] is False


def test_wrong_lock_solvent_fails_at_the_lock_step(clean):
    clean.gateway.request_measurement("NMR-01", "NMR-1H-PEPTIDE@1", nmr_manifest(solvent="DMSO-d6"), "r-lock")
    v = wait(clean.gateway, "r-lock")
    assert v["state"] == "failed" and "lock failed" in v["note"]


def test_nmr_position_and_solvent_are_validated(clean):
    v = clean.gateway.request_measurement("NMR-01", "NMR-1H-PEPTIDE@1",
                                          [{"sample_id": "X", "position": "rack 3", "attrs": {"solvent": "CDCl3"}}], "r-bad")
    assert v["state"] == "refused" and len(v["reasons"]) == 2
