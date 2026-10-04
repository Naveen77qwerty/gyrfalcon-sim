"""Heap-based discrete-event loop. Deterministic given the same seed and schedule."""

from __future__ import annotations

import heapq
from collections.abc import Callable
from typing import Any

from falcon.core.clock import Clock
from falcon.core.event_bus import EventBus
from falcon.core.rng import StreamRng

Callback = Callable[..., None]


class Simulator:
    def __init__(self, seed: int = 1) -> None:
        self.clock = Clock()
        self.rng = StreamRng(seed)
        self.bus = EventBus(self.clock)
        self._heap: list[tuple[float, int, Callback, tuple[Any, ...]]] = []
        self._seq = 0
        self.stopped = False

    def now(self) -> float:
        return self.clock.now()

    def schedule(self, delay: float, fn: Callback, *args: Any) -> None:
        if delay < 0:
            raise ValueError("delay must be >= 0")
        self._seq += 1
        heapq.heappush(self._heap, (self.clock.now() + delay, self._seq, fn, args))

    def stop(self) -> None:
        self.stopped = True

    def run(self, until: float | None = None) -> None:
        self.stopped = False
        while self._heap and not self.stopped:
            t, seq, fn, args = heapq.heappop(self._heap)
            if until is not None and t > until:
                heapq.heappush(self._heap, (t, seq, fn, args))
                self.clock.set(until)
                return
            self.clock.set(t)
            fn(*args)
