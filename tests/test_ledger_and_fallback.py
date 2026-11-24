"""The ledger catches edits; the browser fallback never submits what it cannot read back."""

import json

import numpy as np
import pytest
from conftest import nmr_manifest

from lab_gateway import analysis as an
from lab_gateway.adapters.ui_fallback import UiFallbackAdapter
from lab_gateway.clock import SimClock
from lab_gateway.gateway import Gateway
from lab_gateway.ledger import Ledger
from lab_gateway.methods import MethodRegistry
from lab_gateway.sim.vendor_methods import NMR_TEMPLATES
from lab_gateway.sim.webapp import SimOperatorScreen, SimPageDriver


# ---------------------------------------------------------------- ledger
def test_ledger_chain_detects_an_edited_line(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl", SimClock())
    for i in range(5):
        led.append("submit.sent", actor="gateway", request_id=f"r{i}", detail={"n": i})
    assert led.verify() == {"ok": True, "entries": 5, "head": led.read()[-1]["hash"]}
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    e = json.loads(lines[2])
    e["detail"]["n"] = 99
    lines[2] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    (tmp_path / "ledger.jsonl").write_text("\n".join(lines) + "\n")
    assert Ledger(tmp_path / "ledger.jsonl", SimClock()).verify()["broken_at_seq"] == 3


def test_ledger_chain_detects_a_deleted_line(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl", SimClock())
    for i in range(4):
        led.append("reserve", actor="gateway", request_id=f"r{i}")
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    (tmp_path / "ledger.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert led.verify()["ok"] is False


# ---------------------------------------------------------------- UI fallback
def ui_gateway(tmp_path, prepare_only):
    clock = SimClock()
    screen = SimOperatorScreen(clock)
    adapter = UiFallbackAdapter(SimPageDriver(screen), prepare_only=prepare_only, method_texts=NMR_TEMPLATES)
    return Gateway({"NMR-UI": adapter}, MethodRegistry.load(), tmp_path / "ui", clock), screen


def test_prepare_only_waits_for_the_operator_to_press_start(tmp_path):
    gw, screen = ui_gateway(tmp_path, prepare_only=True)
    v = gw.request_measurement("NMR-UI", "NMR-1H-PEPTIDE@1", nmr_manifest(), "r-ui")
    assert v["state"] == "prepared" and screen.queue == []
    assert gw.get_measurement("r-ui")["state"] == "prepared"
    screen.operator_presses_add()                       # the person checks the screen and presses it
    v = gw.get_measurement("r-ui")
    assert v["state"] == "submitted" and v["vendor_ref"] == "Q001"
    assert "reconcile.operator_started" in [e["action"] for e in gw.ledger.read()]


def test_read_back_mismatch_stops_before_anything_is_pressed(tmp_path):
    gw, screen = ui_gateway(tmp_path, prepare_only=False)
    long_id = "r-ui-" + "x" * 30                      # longer than the Title field keeps
    v = gw.request_measurement("NMR-UI", "NMR-1H-PEPTIDE@1", nmr_manifest(), long_id)
    assert v["state"] == "failed" and "read-back mismatch in 'Title'" in v["note"]
    assert screen.queue == []


def test_confirmed_submission_through_the_screen(tmp_path):
    gw, screen = ui_gateway(tmp_path, prepare_only=False)
    v = gw.request_measurement("NMR-UI", "NMR-1H-PEPTIDE@1", nmr_manifest(), "r-ui-ok")
    assert v["state"] == "submitted" and screen.queue[0]["Title"] == "r-ui-ok"


# ---------------------------------------------------------------- processing on known signals
def test_integration_tailing_and_plates_on_a_known_peak():
    t = np.arange(0, 10, 1 / 300)
    sigma = 0.05
    y = 100 * np.exp(-0.5 * ((t - 5) / sigma) ** 2) + np.random.default_rng(1).normal(0, 0.01, t.size)
    p = max(an.find_peaks(t, y, t_start=1), key=lambda pk: pk.area)
    area = 100 * sigma * np.sqrt(2 * np.pi) * 60
    assert p.area == pytest.approx(area, rel=0.01)
    assert p.tailing == pytest.approx(1.0, abs=0.02)
    assert p.plates == pytest.approx(5.54 * (5 / (2.3548 * sigma)) ** 2, rel=0.02)


def test_first_order_fit_recovers_the_half_life():
    t = np.array([0, 5, 15, 30, 60, 120])
    fit = an.fit_first_order(t, 100 * 0.5 ** (t / 42.0))
    assert fit["t_half_min"] == pytest.approx(42.0, rel=1e-6) and fit["r2"] == pytest.approx(1.0)


def test_nmr_linewidth_is_natural_plus_line_broadening():
    sw, sfo1, td = 7201.56, 600.13, 16384
    t = np.arange(td) / sw
    fid = np.exp((2j * np.pi * (0 - 4.7) * sfo1 - np.pi * 0.5) * t) * np.exp(1j * 0.4)
    proc = an.process_1d(fid, sw, sfo1, 4.7, lb_hz=0.3)
    assert proc["phase0_deg"] == pytest.approx(np.degrees(0.4), abs=0.5)
    assert an.linewidth_hz(proc["ppm"], proc["real"], 0.0, sfo1) == pytest.approx(0.8, abs=0.05)
