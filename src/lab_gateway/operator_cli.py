"""lab-operator: the person at the instrument resolves what the gateway could not.

  lab-operator pending --run-dir runs/demo
  lab-operator decide  --run-dir runs/demo --request-id R --decision resubmit|adopt|abandon \
                       --operator NAME --note "what you checked" [--vendor-ref B0002]

The decision is written to the run's decisions folder; the gateway applies it on its next
poll and records it in the ledger under the operator's name.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .gateway import OPERATOR_DECISIONS


def write_decision(run_dir: Path, request_id: str, decision: str, operator: str, note: str,
                   vendor_ref: str | None = None) -> Path:
    if decision not in OPERATOR_DECISIONS:
        raise ValueError(f"decision must be one of {OPERATOR_DECISIONS}")
    if decision == "adopt" and not vendor_ref:
        raise ValueError("adopt needs --vendor-ref")
    if not note.strip():
        raise ValueError("say what you checked in --note")
    path = Path(run_dir) / "decisions" / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"decision": decision, "operator": operator, "note": note, "vendor_ref": vendor_ref}))
    return path


def pending(run_dir: Path) -> list[dict]:
    state = json.loads((Path(run_dir) / "state.json").read_text())
    return [{"request_id": r["request_id"], "instrument": r["instrument"], "state": r["state"],
             "note": r["history"][-1]["note"]}
            for r in state.values() if r["state"] in ("unknown", "needs_operator")]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="lab-operator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--run-dir", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--run-dir", required=True)
    d.add_argument("--request-id", required=True)
    d.add_argument("--decision", required=True, choices=OPERATOR_DECISIONS)
    d.add_argument("--operator", required=True)
    d.add_argument("--note", required=True)
    d.add_argument("--vendor-ref")
    a = ap.parse_args(argv)
    if a.cmd == "pending":
        print(json.dumps(pending(Path(a.run_dir)), indent=1))
    else:
        print(write_decision(Path(a.run_dir), a.request_id, a.decision, a.operator, a.note, a.vendor_ref))


if __name__ == "__main__":
    main()
