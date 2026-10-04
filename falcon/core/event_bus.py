"""JSONL event bus. The log is the source of truth for stats and the dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, IO

from falcon.core.clock import Clock

_CONTAINER = (dict, list)


class EventBus:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.events: list[dict[str, Any]] = []
        self._sinks: list[Callable[[dict[str, Any]], None]] = []

    def add_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        self._sinks.append(sink)

    def emit(self, etype: str, conn: int = 0, **fields: Any) -> dict[str, Any]:
        """Append one event and hand it to every sink.

        Container values are copied one level deep. Without this, an emitter that passes a
        live object -- the FAE's `policy.path_for_flow`, say -- lets a later mutation
        rewrite records that were emitted earlier. A log that changes after the fact cannot
        be the source of truth for stats or for a replayable dashboard.
        """
        rec: dict[str, Any] = {"t": self._clock.now(), "type": etype, "conn": conn}
        for key, value in fields.items():
            rec[key] = type(value)(value) if isinstance(value, _CONTAINER) else value
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
