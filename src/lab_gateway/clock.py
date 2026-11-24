"""Clocks.

The gateway never calls time.sleep itself. With RealClock it waits on real instruments;
with SimClock a 4-hour LC-MS batch finishes as fast as the code can run.
"""

from __future__ import annotations

import time


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class SimClock:
    """Simulated seconds since the run started. sleep() jumps ahead instantly."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot sleep a negative time")
        self._t += float(seconds)
