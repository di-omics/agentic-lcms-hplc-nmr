"""A simulated operator web screen for the UI fallback, driven through the same
label-based calls a Playwright page would get. The Title field silently keeps only 24
characters, like a form with a maxlength, which is what read-back is there to catch."""

from __future__ import annotations


class SimOperatorScreen:
    MAXLEN = {"Title": 24}

    def __init__(self, clock) -> None:
        self.clock = clock
        self.view = "status"
        self.form: dict[str, str] = {}
        self.queue: list[dict] = []
        self.state = "Ready"

    def operator_presses_add(self) -> None:
        """What the person at the instrument does when the adapter ran in prepare-only mode."""
        self.queue.append({"Id": f"Q{len(self.queue) + 1:03d}", "Title": self.form.get("Title", ""),
                           "Sample": self.form.get("Sample", ""), "State": "Queued"})


class SimPageDriver:
    def __init__(self, screen: SimOperatorScreen) -> None:
        self.s = screen

    def open(self, view: str) -> None:
        self.s.view = view
        if view == "new-experiment":
            self.s.form = {}

    def fill(self, label: str, value: str) -> None:
        if self.s.view != "new-experiment":
            raise LookupError(f"no field labelled {label!r} on {self.s.view}")
        self.s.form[label] = value[: self.s.MAXLEN.get(label, 256)]

    def read(self, label: str) -> str:
        if self.s.view == "status" and label == "Spectrometer state":
            return self.s.state
        return self.s.form.get(label, "")

    def click(self, button: str) -> None:
        if self.s.view == "new-experiment" and button == "Add to queue":
            self.s.operator_presses_add()
        else:
            raise LookupError(f"no button {button!r} on {self.s.view}")

    def table(self, name: str) -> list[dict]:
        if name != "Queue":
            raise LookupError(name)
        return [dict(r) for r in self.s.queue]
