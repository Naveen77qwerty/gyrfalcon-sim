"""Heap-based discrete-event loop. Deterministic given the same seed and schedule."""

from __future__ import annotations

import heapq
from collections.abc import Callable
from typing import Any

from gyrfalcon.core.clock import Clock
from gyrfalcon.core.event_bus import EventBus
from gyrfalcon.core.rng import StreamRng

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

    @property
    def pending(self) -> int:
        """Events still queued. Lets a caller tell "idle" from "finished"."""
        return len(self._heap)

    @property
    def finished(self) -> bool:
        return not self._heap

    def step(self, max_events: int = 1000, until: float | None = None) -> int:
        """Run at most `max_events` queued events and return how many ran.

        The live dashboard needs to advance the simulation in slices so it can ship events to
        a socket in between. This is the same loop `run` uses, unchanged in ordering, RNG
        consumption, and time setting -- slicing changes only how often control returns, so a
        log from a stepped simulation is identical to one from an unstepped run of the same seed.
        """
        n = 0
        while self._heap and not self.stopped and n < max_events:
            t, seq, fn, args = heapq.heappop(self._heap)
            if until is not None and t > until:
                heapq.heappush(self._heap, (t, seq, fn, args))
                self.clock.set(until)
                break
            self.clock.set(t)
            fn(*args)
            n += 1
        return n

    def run(self, until: float | None = None, max_events: int = 2_000_000) -> None:
        self.stopped = False
        total = 0
        while not self.stopped and total < max_events:
            n = self.step(max_events - total, until=until)
            total += n
            if n == 0:
                return
