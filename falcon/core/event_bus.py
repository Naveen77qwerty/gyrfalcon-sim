"""JSONL event bus. The log is the source of truth for stats and the dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, IO

from falcon.core.clock import Clock


class EventBus:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.events: list[dict[str, Any]] = []
        self._sinks: list[Callable[[dict[str, Any]], None]] = []

    def add_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        self._sinks.append(sink)

    def emit(self, etype: str, conn: int = 0, **fields: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {"t": self._clock.now(), "type": etype, "conn": conn}
        rec.update(fields)
        self.events.append(rec)
        for sink in self._sinks:
            sink(rec)
        return rec

    def dumps(self) -> str:
        lines = [json.dumps(e, sort_keys=True, separators=(",", ":")) for e in self.events]
        return "\n".join(lines) + ("\n" if lines else "")

    def write_jsonl(self, path: str | Path | IO[str]) -> None:
        text = self.dumps()
        if hasattr(path, "write"):
            path.write(text)  # type: ignore[union-attr]
            return
        Path(path).write_text(text)
