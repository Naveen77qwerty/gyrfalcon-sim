"""Injectable simulation clock. Protocol code must not call time.time()."""


class Clock:
    def __init__(self) -> None:
        self._now: float = 0.0

    def now(self) -> float:
        return self._now

    def set(self, t: float) -> None:
        if t < self._now:
            raise ValueError("clock cannot move backwards")
        self._now = t
