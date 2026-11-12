"""Simulated clock: runs `speed` simulated seconds per real second and can be paused (the
live stream pauses it while no browser is connected)."""
from __future__ import annotations

import time
from typing import Callable


class SimClock:
    def __init__(self, start_ts: float, speed: float = 60.0, time_fn: Callable[[], float] = time.monotonic):
        self.start_ts = float(start_ts)
        self.speed = float(speed)
        self._time = time_fn
        self._sim_anchor = self.start_ts
        self._real_anchor: float | None = None      # None = paused

    @property
    def paused(self) -> bool:
        return self._real_anchor is None

    def now(self) -> float:
        if self._real_anchor is None:
            return self._sim_anchor
        return self._sim_anchor + (self._time() - self._real_anchor) * self.speed

    def pause(self) -> None:
        if self._real_anchor is not None:
            self._sim_anchor = self.now()
            self._real_anchor = None

    def resume(self) -> None:
        if self._real_anchor is None:
            self._real_anchor = self._time()

    def reset(self, start_ts: float | None = None) -> None:
        was_running = not self.paused
        if start_ts is not None:
            self.start_ts = float(start_ts)
        self._sim_anchor = self.start_ts
        self._real_anchor = self._time() if was_running else None

    def real_seconds(self, sim_seconds: float) -> float:
        return sim_seconds / self.speed
