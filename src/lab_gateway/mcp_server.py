"""The MCP server: the lab as the planning agent sees it.

Five tools. Requesting a measurement is idempotent by request_id; following it is a long
poll; there is no tool for pumps, gradients, pulse programs or sample movement, because
the vendor software keeps control of those. Operator decisions are deliberately not a
tool: the agent cannot resolve its own unknown outcomes (see lab-operator).

Run it:   lab-gateway-mcp          (stdio; simulated lab unless configured otherwise)
Env:      LAB_GATEWAY_RUN_DIR      where the ledger, state and results go (default runs/latest)
          LAB_GATEWAY_SCENARIO     demo | clean (simulated faults on or off)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .sim.lab import build_sim_lab

INSTRUCTIONS = """Measurements on an HPLC (purity), an LC-MS (plasma stability) and an NMR (structure).
- Call list_instruments first: it lists each instrument's approved method versions and their acceptance criteria.
- Every request needs a request_id you choose. Reusing it returns the same request; it never runs twice.
- get_measurement with wait_s long-polls until the state changes.
- unknown or needs_operator means the instrument is held until a person resolves it. Do not work around it.
- A run can finish and still be unusable. Use only results with usable=true for design decisions."""

server = MCPServer(name="lab-gateway", instructions=INSTRUCTIONS)
_lab = None


def lab():
    global _lab
    if _lab is None:
        run_dir = Path(os.environ.get("LAB_GATEWAY_RUN_DIR", "runs/latest"))
        _lab = build_sim_lab(run_dir, os.environ.get("LAB_GATEWAY_SCENARIO", "demo"))
    return _lab


def _summary(result: dict) -> dict:
    keys = ("state", "usable", "reasons", "note", "vendor_ref", "progress", "needs", "findings", "sim_time_min")
    return {k: result[k] for k in keys if k in result}


def _traced(tool: str, args: dict, result: dict) -> dict:
    slim = dict(args)
    if "sample_manifest" in slim:
        m = slim.pop("sample_manifest")
        slim["samples"] = len(m) if isinstance(m, list) else m
    line = {"sim_time_min": round(lab().clock.now() / 60, 2), "tool": tool, "args": slim, "result": _summary(result)}
    with open(lab().gateway.run_dir / "trace.jsonl", "a") as f:
        f.write(json.dumps(line) + "\n")
    return result


READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)


@server.tool(annotations=READ)
def list_instruments() -> dict:
    """List the instruments, the vendor software and interface behind each, readiness, which
    request holds each one, and the approved method versions with their acceptance criteria."""
    return _traced("list_instruments", {}, lab().gateway.list_instruments())


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                         idempotent_hint=True, open_world_hint=False))
def request_measurement(instrument: str, method_version: str, sample_manifest: list[dict[str, Any]],
                        request_id: str, options: dict[str, Any] | None = None, dry_run: bool = False) -> dict:
    """Request a measurement under an approved method version.

    sample_manifest: [{"sample_id", "position", "kind": sample|control, "barcode", "attrs": {...}}].
    Standards and blanks come from the method's own template; list only your samples.
    request_id: your idempotency key (3-64 chars). The same id with the same content returns
    the existing request; nothing is sent twice. dry_run=true runs every check and sends nothing."""
    args = {"instrument": instrument, "method_version": method_version, "sample_manifest": sample_manifest,
            "request_id": request_id, "options": options, "dry_run": dry_run}
    return _traced("request_measurement", args, lab().gateway.request_measurement(
        instrument, method_version, sample_manifest, request_id, options, dry_run))


@server.tool(annotations=READ)
def get_measurement(request_id: str, wait_s: float = 0.0) -> dict:
    """State, progress and (when done) the QC verdict, findings and provenance of a request.
    wait_s: wait up to this many seconds for the state to change (long poll)."""
    return _traced("get_measurement", {"request_id": request_id, "wait_s": wait_s},
                   lab().gateway.get_measurement(request_id, wait_s))


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                         idempotent_hint=True, open_world_hint=False))
def cancel_measurement(request_id: str, reason: str) -> dict:
    """Cancel a queued or running request. Requests with an unknown outcome are an operator's call."""
    return _traced("cancel_measurement", {"request_id": request_id, "reason": reason},
                   lab().gateway.cancel_measurement(request_id, reason))


@server.tool(annotations=READ)
def read_ledger(request_id: str | None = None, limit: int = 30) -> dict:
    """The record of every call that changed an instrument, with a check that the chain is intact."""
    return _traced("read_ledger", {"request_id": request_id, "limit": limit},
                   lab().gateway.read_ledger(request_id, limit))


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
