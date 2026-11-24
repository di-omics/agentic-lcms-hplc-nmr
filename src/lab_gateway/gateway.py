"""The gateway: what the agent's tools call.

request_measurement -> checks -> reservation -> ledger "submit.sent" -> vendor adapter
get_measurement     -> vendor status -> data -> QC gate -> result with usable true/false

A lost reply never leads to a blind retry. The request goes to UNKNOWN, the instrument stays
reserved, and the gateway searches the vendor's own queue and history for the request_id.
Found once: monitoring resumes. Not found within the grace period, or found twice: an
operator decides (adopt, resubmit or abandon), and the decision goes in the ledger.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from .adapters.base import NotConfigured, ReplyLost, VendorAdapter, VendorRejected
from .evaluate import EVALUATORS
from .ledger import Ledger
from .methods import MethodRegistry
from .models import (HOLDS_INSTRUMENT, REQUEST_ID_RE, TERMINAL, ManifestError, Sample, State,
                     parse_manifest, request_fingerprint)

OPERATOR_DECISIONS = ("adopt", "resubmit", "abandon")


class Gateway:
    def __init__(self, adapters: dict[str, VendorAdapter], methods: MethodRegistry, run_dir: Path, clock,
                 *, unknown_grace_s: float = 600.0, poll_s: float = 60.0, max_wait_s: float = 3600.0) -> None:
        self.adapters = adapters
        self.methods = methods
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "results").mkdir(exist_ok=True)
        (self.run_dir / "decisions").mkdir(exist_ok=True)
        self.clock = clock
        self.ledger = Ledger(self.run_dir / "ledger.jsonl", clock)
        self.unknown_grace_s = unknown_grace_s
        self.poll_s = poll_s
        self.max_wait_s = max_wait_s
        self._lock = threading.RLock()
        self.records: dict[str, dict] = {}
        self._load()

    # ================================================================ agent-facing tools
    def list_instruments(self) -> dict:
        with self._lock:
            out = []
            for iid, a in self.adapters.items():
                try:
                    ready = a.readiness()
                except NotConfigured as e:
                    ready = {"ready": False, "reasons": [str(e)]}
                holder = self._holder(iid)
                out.append({
                    "instrument": iid, "kind": a.kind, "vendor_software": a.vendor, "interface": a.interface,
                    "ready": ready["ready"], "reasons": ready["reasons"],
                    "held_by": holder["request_id"] if holder else None,
                    "methods": [m.public() for m in self.methods.for_kind(a.kind)],
                })
            return {"instruments": out, "sim_time_min": self._now_min()}

    def request_measurement(self, instrument: str, method_version: str, sample_manifest: list[dict],
                            request_id: str, options: dict | None = None, dry_run: bool = False) -> dict:
        options = dict(options or {})
        with self._lock:
            if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
                return self._refused(request_id, ["request_id must be 3 to 64 letters, digits, '.', '_' or '-'"])
            try:
                samples = parse_manifest(sample_manifest)
            except ManifestError as e:
                return self._refused(request_id, [str(e)])
            fp = request_fingerprint(instrument, method_version, samples, options)
            existing = self.records.get(request_id)
            if existing is not None:
                if existing["fingerprint"] == fp:
                    return self._view(existing, note="request_id already known: returned the existing record, nothing sent")
                return self._refused(request_id, ["request_id already used for a different request; choose a new one"])
            problems = self._check(instrument, method_version, samples, options)
            if problems:
                return self._refused(request_id, problems)
            if dry_run:
                return {"request_id": request_id, "state": "checked", "sent_to_instrument": False,
                        "reasons": [], "note": "all checks passed; nothing reserved or sent",
                        "sim_time_min": self._now_min()}
            holder = self._holder(instrument)
            if holder is not None:
                return self._refused(request_id, [f"{instrument} is held by {holder['request_id']} "
                                                  f"({holder['state']}); try again when it finishes"])
            rec = {
                "request_id": request_id, "instrument": instrument, "kind": self.adapters[instrument].kind,
                "method_version": method_version, "options": options, "fingerprint": fp,
                "manifest": [s.to_dict() for s in samples], "state": State.RESERVED.value,
                "vendor_ref": None, "attempt": 0, "unknown_since": None,
                "created_t": self.clock.now(), "history": [], "progress": {"done": 0, "total": 0},
                "sample_notes": {}, "result": None,
            }
            self.records[request_id] = rec
            self._event(rec, State.RESERVED, f"{instrument} reserved")
            self.ledger.append("reserve", actor="gateway", instrument=instrument, request_id=request_id,
                               detail={"method_version": method_version, "samples": len(samples)})
            self._submit(rec)
            self._save()
            return self._view(rec)

    def get_measurement(self, request_id: str, wait_s: float = 0.0) -> dict:
        with self._lock:
            rec = self.records.get(request_id)
            if rec is None:
                return {"request_id": request_id, "error": "unknown request_id"}
            start = rec["state"]
            self._refresh(rec)
            deadline = self.clock.now() + min(max(0.0, float(wait_s)), self.max_wait_s)
            while rec["state"] == start and State(rec["state"]) not in TERMINAL and self.clock.now() < deadline:
                self.clock.sleep(min(self.poll_s, deadline - self.clock.now()))
                self._refresh(rec)
            self._save()
            return self._view(rec)

    def cancel_measurement(self, request_id: str, reason: str) -> dict:
        with self._lock:
            rec = self.records.get(request_id)
            if rec is None:
                return {"request_id": request_id, "error": "unknown request_id"}
            st = State(rec["state"])
            if st in (State.UNKNOWN, State.NEEDS_OPERATOR):
                return self._view(rec, note="outcome unknown: only an operator can resolve this request")
            if st in TERMINAL:
                return self._view(rec, note="already finished; nothing to cancel")
            if st in (State.SUBMITTED, State.RUNNING) and rec["vendor_ref"]:
                self.ledger.append("cancel.sent", actor="gateway", instrument=rec["instrument"],
                                   request_id=request_id, detail={"vendor_ref": rec["vendor_ref"], "reason": reason})
                self.adapters[rec["instrument"]].cancel(rec["vendor_ref"])
            self._finish(rec, State.CANCELLED, f"cancelled: {reason}")
            self._save()
            return self._view(rec)

    def read_ledger(self, request_id: str | None = None, limit: int = 50) -> dict:
        entries = self.ledger.read()
        if request_id:
            entries = [e for e in entries if e["request_id"] == request_id]
        slim = [{k: e[k] for k in ("seq", "t", "actor", "action", "instrument", "request_id", "detail")}
                for e in entries[-limit:]]
        return {"verified": self.ledger.verify(), "entries": slim}

    # ================================================================ operator, not an agent tool
    def operator_decision(self, request_id: str, decision: str, operator: str, note: str = "",
                          vendor_ref: str | None = None) -> dict:
        with self._lock:
            rec = self.records.get(request_id)
            if rec is None:
                return {"request_id": request_id, "error": "unknown request_id"}
            if rec["state"] not in (State.UNKNOWN.value, State.NEEDS_OPERATOR.value):
                return self._view(rec, note="no decision needed in this state")
            if decision not in OPERATOR_DECISIONS:
                return self._view(rec, note=f"decision must be one of {OPERATOR_DECISIONS}")
            if decision == "adopt" and not vendor_ref:
                return self._view(rec, note="adopt needs the vendor_ref the operator found")
            self.ledger.append(f"operator.{decision}", actor=f"operator:{operator}", instrument=rec["instrument"],
                               request_id=request_id, detail={"note": note, "vendor_ref": vendor_ref})
            if decision == "adopt":
                rec["vendor_ref"] = vendor_ref
                self._event(rec, State.SUBMITTED, f"operator {operator} matched it to {vendor_ref}")
            elif decision == "resubmit":
                self._event(rec, State.RESERVED, f"operator {operator} confirmed nothing ran; resubmitting")
                self._submit(rec)
            else:
                self._finish(rec, State.CANCELLED, f"operator {operator} abandoned it: {note}")
            self._save()
            return self._view(rec)

    # ================================================================ internals
    def _check(self, instrument: str, method_version: str, samples: list[Sample], options: dict) -> list[str]:
        adapter = self.adapters.get(instrument)
        if adapter is None:
            return [f"unknown instrument {instrument!r}; known: {sorted(self.adapters)}"]
        method = self.methods.get(method_version)
        if method is None:
            return [f"{method_version} is not in the approved method registry"]
        if method.kind != adapter.kind:
            return [f"{method_version} is a {method.kind} method; {instrument} is {adapter.kind}"]
        if method.status != "approved":
            return [f"{method_version} is {method.status}, not approved; nothing was sent"]
        problems = []
        if len(samples) > method.max_samples:
            problems.append(f"{len(samples)} samples; {method_version} allows {method.max_samples}")
        for k, v in options.items():
            if k not in method.options or v not in method.options[k]:
                problems.append(f"option {k}={v!r} is not allowed by {method_version}")
        problems += adapter.validate(samples, method, options)
        if problems:
            return problems
        try:
            on_instrument = adapter.method_fingerprint(method)
        except NotConfigured as e:
            return [str(e)]
        except KeyError:
            return [f"{method.vendor_ref} not found in the vendor software"]
        if on_instrument != method.fingerprint:
            return [f"the method stored in the vendor software differs from approved {method_version} "
                    f"(fingerprint {on_instrument[:19]} vs {method.fingerprint[:19]})"]
        ready = adapter.readiness()
        if not ready["ready"]:
            return [f"{instrument} not ready: " + "; ".join(ready["reasons"])]
        return []

    def _submit(self, rec: dict) -> None:
        adapter = self.adapters[rec["instrument"]]
        method = self.methods.get(rec["method_version"])
        samples = [Sample(**s) for s in rec["manifest"]]
        rec["attempt"] += 1
        self.ledger.append("submit.sent", actor="gateway", instrument=rec["instrument"], request_id=rec["request_id"],
                           detail={"attempt": rec["attempt"], "interface": adapter.interface,
                                   "method_version": rec["method_version"], "samples": len(samples)})
        try:
            res = adapter.submit(rec["request_id"], rec["attempt"], samples, method, rec["options"])
        except ReplyLost as e:
            self.ledger.append("submit.reply_lost", actor="gateway", instrument=rec["instrument"],
                               request_id=rec["request_id"], detail={"error": str(e)})
            rec["unknown_since"] = self.clock.now()
            self._event(rec, State.UNKNOWN, f"reply lost: {e}. Instrument held; searching the vendor queue")
            self._reconcile(rec)
            return
        except VendorRejected as e:
            self.ledger.append("submit.rejected", actor="gateway", instrument=rec["instrument"],
                               request_id=rec["request_id"], detail={"error": str(e)})
            self._finish(rec, State.FAILED, f"vendor rejected it: {e}")
            return
        if res.prepared_only:
            self.ledger.append("submit.prepared", actor="gateway", instrument=rec["instrument"],
                               request_id=rec["request_id"], detail={"note": res.note})
            self._event(rec, State.PREPARED, res.note)
            return
        rec["vendor_ref"] = res.vendor_ref
        self.ledger.append("submit.accepted", actor="gateway", instrument=rec["instrument"],
                           request_id=rec["request_id"], detail={"vendor_ref": res.vendor_ref, "note": res.note})
        self._event(rec, State.SUBMITTED, f"accepted: {res.note}")

    def _reconcile(self, rec: dict) -> None:
        refs = self.adapters[rec["instrument"]].find(rec["request_id"])
        if len(refs) == 1:
            rec["vendor_ref"] = refs[0]
            self.ledger.append("reconcile.found", actor="gateway", instrument=rec["instrument"],
                               request_id=rec["request_id"],
                               detail={"vendor_ref": refs[0], "searched": "vendor queue and acquisition history"})
            self._event(rec, State.SUBMITTED, f"found {refs[0]} in the vendor queue: monitoring resumed, nothing resubmitted")
        elif len(refs) > 1:
            self.ledger.append("reconcile.ambiguous", actor="gateway", instrument=rec["instrument"],
                               request_id=rec["request_id"], detail={"vendor_refs": refs})
            self._event(rec, State.NEEDS_OPERATOR, f"{len(refs)} vendor entries carry this request_id; operator must reconcile")
        elif self.clock.now() - rec["unknown_since"] >= self.unknown_grace_s:
            self.ledger.append("reconcile.unresolved", actor="gateway", instrument=rec["instrument"],
                               request_id=rec["request_id"],
                               detail={"grace_s": self.unknown_grace_s, "searched": "vendor queue and acquisition history"})
            self._event(rec, State.NEEDS_OPERATOR, "not in the vendor queue or history after the grace period; "
                                                   "an operator must reconcile before another injection")

    def _refresh(self, rec: dict) -> None:
        st = State(rec["state"])
        if st in TERMINAL:
            return
        if st == State.NEEDS_OPERATOR:
            self._apply_decision_file(rec)
            return
        if st == State.UNKNOWN:
            self._apply_decision_file(rec)
            if rec["state"] == State.UNKNOWN.value:
                self._reconcile(rec)
            return
        adapter = self.adapters[rec["instrument"]]
        if st == State.PREPARED:
            refs = adapter.find(rec["request_id"])
            if len(refs) == 1:
                rec["vendor_ref"] = refs[0]
                self.ledger.append("reconcile.operator_started", actor="gateway", instrument=rec["instrument"],
                                   request_id=rec["request_id"], detail={"vendor_ref": refs[0]})
                self._event(rec, State.SUBMITTED, f"operator pressed Start; queue shows {refs[0]}")
            return
        vs = adapter.status(rec["vendor_ref"])
        rec["progress"] = {"done": vs.done, "total": vs.total}
        rec["sample_notes"].update(vs.sample_notes)
        if vs.state == "running" and st != State.RUNNING:
            self._event(rec, State.RUNNING, vs.message or "acquiring")
        elif vs.state == "complete":
            self._complete(rec)
        elif vs.state == "failed":
            self._finish(rec, State.FAILED, vs.message or "vendor reported a failure")
        elif vs.state == "aborted":
            self._finish(rec, State.CANCELLED, vs.message or "aborted in the vendor software")

    def _complete(self, rec: dict) -> None:
        adapter = self.adapters[rec["instrument"]]
        method = self.methods.get(rec["method_version"])
        samples = [Sample(**s) for s in rec["manifest"]]
        fetched = adapter.fetch(rec["vendor_ref"], samples, method)
        ev = EVALUATORS[rec["kind"]](fetched, samples, method)
        arrays = ev.pop("arrays", None)
        out = self.run_dir / "results" / rec["request_id"]
        out.mkdir(parents=True, exist_ok=True)
        if arrays:
            for name, arr in arrays.items():
                np.save(out / f"{name}.npy", np.asarray(arr))
        detail = ev.pop("detail", {})
        rec["result"] = {**ev, "raw": fetched["raw"],
                         "provenance": {"instrument": rec["instrument"], "vendor_software": adapter.vendor,
                                        "interface": adapter.interface, "vendor_ref": rec["vendor_ref"],
                                        "method_version": rec["method_version"],
                                        "method_fingerprint": method.fingerprint}}
        (out / "result.json").write_text(json.dumps({**rec["result"], "detail": detail, "fetched_injections":
                                                     _strip_traces(fetched.get("injections"))}, indent=1))
        n_bad = sum(1 for c in ev["checks"] if not c["pass"])
        note = "QC passed: usable" if ev["usable"] else f"completed, but {n_bad} QC check(s) failed: not usable"
        self._finish(rec, State.DONE, note)

    def _finish(self, rec: dict, state: State, note: str) -> None:
        held = State(rec["state"]) in HOLDS_INSTRUMENT
        self._event(rec, state, note)
        if held:
            self.ledger.append("release", actor="gateway", instrument=rec["instrument"], request_id=rec["request_id"],
                               detail={"state": state.value, "note": note})

    def _apply_decision_file(self, rec: dict) -> None:
        f = self.run_dir / "decisions" / f"{rec['request_id']}.json"
        if f.exists():
            d = json.loads(f.read_text())
            f.rename(f.with_suffix(".applied.json"))
            self.operator_decision(rec["request_id"], d["decision"], d["operator"], d.get("note", ""),
                                   d.get("vendor_ref"))

    def _holder(self, instrument: str) -> dict | None:
        for r in self.records.values():
            if r["instrument"] == instrument and State(r["state"]) in HOLDS_INSTRUMENT:
                return r
        return None

    def _event(self, rec: dict, state: State, note: str) -> None:
        rec["state"] = state.value
        rec["history"].append({"t_min": self._now_min(), "state": state.value, "note": note})

    def _now_min(self) -> float:
        return round(self.clock.now() / 60.0, 2)

    def _refused(self, request_id, reasons: list[str]) -> dict:
        return {"request_id": request_id, "state": "refused", "sent_to_instrument": False, "reasons": reasons,
                "sim_time_min": self._now_min()}

    def _view(self, rec: dict, note: str | None = None) -> dict:
        v = {
            "request_id": rec["request_id"], "instrument": rec["instrument"],
            "method_version": rec["method_version"], "state": rec["state"],
            "vendor_ref": rec["vendor_ref"], "sim_time_min": self._now_min(),
            "note": note or rec["history"][-1]["note"],
        }
        if rec["progress"]["total"]:
            v["progress"] = f"{rec['progress']['done']}/{rec['progress']['total']} injections"
        if rec["state"] == State.NEEDS_OPERATOR.value:
            v["needs"] = "an operator decision: lab-operator decide --request-id ... (adopt, resubmit or abandon)"
        if rec["result"]:
            r = rec["result"]
            v.update({"usable": r["usable"], "qc": r["checks"], "samples": r["samples"],
                      "findings": r["findings"], "provenance": r["provenance"],
                      "raw_files": len(r["raw"])})
        return v

    def _save(self) -> None:
        tmp = self.run_dir / "state.json.part"
        tmp.write_text(json.dumps(self.records, indent=1))
        tmp.replace(self.run_dir / "state.json")

    def _load(self) -> None:
        f = self.run_dir / "state.json"
        if f.exists():
            self.records = json.loads(f.read_text())


def _strip_traces(injections):
    if not injections:
        return None
    keep = []
    for i in injections:
        keep.append({k: v for k, v in i.items() if k not in ("t", "y", "xic_analyte", "xic_istd")})
    return keep
