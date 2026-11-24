"""The last rung: driving the vendor's operator screen.

Use it only when the installed instrument has a web or desktop operator screen and its
supported API or queue cannot do the job. The rules that keep it honest:

1. Address fields by their visible labels (Playwright get_by_label, or Windows UI
   Automation names), never by pixel positions.
2. Read every field back before pressing anything. A truncated or reformatted value stops
   the submission.
3. Confirm acceptance by finding the request_id in the instrument's own queue view.
4. If the screen cannot confirm acceptance reliably, prepare the run and leave Start to the
   operator (prepare_only=True). The gateway then watches the queue for the request_id.

PlaywrightDriver works with a sync Playwright page. It is not exercised in this
repository's tests; SimPageDriver (lab_gateway.sim.webapp) stands in for it.
"""

from __future__ import annotations

from typing import Protocol

from ..methods import ApprovedMethod, fingerprint_text
from ..models import Sample
from .base import (SubmitResult, VendorAdapter, VendorRejected, VendorStatus, matches_request,
                   vendor_name)


class PageDriver(Protocol):
    def open(self, view: str) -> None: ...
    def fill(self, label: str, value: str) -> None: ...
    def read(self, label: str) -> str: ...
    def click(self, button: str) -> None: ...
    def table(self, name: str) -> list[dict]: ...


class PlaywrightDriver:
    def __init__(self, page, base_url: str) -> None:
        self.page, self.base_url = page, base_url.rstrip("/")

    def open(self, view: str) -> None:
        self.page.goto(f"{self.base_url}/{view.lstrip('/')}")

    def fill(self, label: str, value: str) -> None:
        self.page.get_by_label(label, exact=True).fill(value)

    def read(self, label: str) -> str:
        return self.page.get_by_label(label, exact=True).input_value()

    def click(self, button: str) -> None:
        self.page.get_by_role("button", name=button, exact=True).click()

    def table(self, name: str) -> list[dict]:
        t = self.page.get_by_role("table", name=name)
        headers = [h.inner_text().strip() for h in t.get_by_role("columnheader").all()]
        rows = []
        for r in t.get_by_role("row").all()[1:]:
            cells = [c.inner_text().strip() for c in r.get_by_role("cell").all()]
            rows.append(dict(zip(headers, cells)))
        return rows


class UiFallbackAdapter(VendorAdapter):
    """NMR experiment entry through an operator web screen (fields: Title, Sample, Holder,
    Solvent, Experiment; button: Add to queue; table: Queue)."""

    kind = "nmr"
    vendor = "operator web screen"
    interface = "browser automation, labels only"

    def __init__(self, driver: PageDriver, *, prepare_only: bool, method_texts: dict[str, str]) -> None:
        self.driver = driver
        self.prepare_only = prepare_only
        self.method_texts = method_texts   # read from the vendor's method export, not the screen

    def readiness(self) -> dict:
        self.driver.open("status")
        state = self.driver.read("Spectrometer state")
        return {"ready": state == "Ready", "reasons": [] if state == "Ready" else [f"screen shows {state}"],
                "detail": {"state": state}}

    def method_fingerprint(self, method: ApprovedMethod) -> str:
        return fingerprint_text(self.method_texts[method.vendor_ref["experiment"]])

    def submit(self, request_id, attempt, samples, method, options) -> SubmitResult:
        s = samples[0]
        fields = {
            "Title": vendor_name(request_id, attempt),
            "Sample": s.sample_id,
            "Holder": s.position.split(":")[-1],
            "Solvent": s.attrs.get("solvent", ""),
            "Experiment": method.vendor_ref["experiment"],
        }
        self.driver.open("new-experiment")
        for label, value in fields.items():
            self.driver.fill(label, value)
        for label, value in fields.items():
            shown = self.driver.read(label)
            if shown != value:
                raise VendorRejected(f"read-back mismatch in '{label}': wrote {value!r}, screen shows {shown!r}; "
                                     "nothing was submitted")
        if self.prepare_only:
            return SubmitResult(vendor_ref=None, prepared_only=True,
                                note="form filled and read back; waiting for the operator to press Add to queue")
        self.driver.click("Add to queue")
        refs = self.find(request_id)
        if len(refs) != 1:
            from .base import ReplyLost
            raise ReplyLost("pressed Add to queue but the queue does not show the request yet")
        return SubmitResult(vendor_ref=refs[0], note="accepted; the queue shows the title")

    def find(self, request_id: str) -> list[str]:
        self.driver.open("queue")
        return [r["Id"] for r in self.driver.table("Queue") if matches_request(r["Title"], request_id)]

    def status(self, vendor_ref: str) -> VendorStatus:
        self.driver.open("queue")
        for r in self.driver.table("Queue"):
            if r["Id"] == vendor_ref:
                state = {"Queued": "queued", "Running": "running", "Completed": "complete",
                         "Error": "failed", "Cancelled": "aborted"}[r["State"]]
                return VendorStatus(state=state, done=int(state == "complete"), total=1)
        return VendorStatus(state="queued", message="not visible in the queue view")

    def cancel(self, vendor_ref: str) -> None:
        raise VendorRejected("cancel from the screen is left to the operator")

    def fetch(self, vendor_ref, samples, method) -> dict:
        raise VendorRejected("data comes from the vendor's data folder, not from the screen")
