"""Live-mode server: streams a running simulation to the dashboard over a WebSocket.

Replay mode reads a finished log; live mode runs the simulator in this process and pushes
events as they are produced, so the same panels can be driven by knobs mid-run. This is the
only place that does I/O against a socket. Nothing under `pdl/`, `tl/`, or `fae/` knows it
exists: the server drives `Simulator.step` and mutates `Path` and `Policy` knobs, which are
ordinary objects the offline harness already hands out.

Three properties this module is responsible for, each covered by `tests/test_live.py`:

- Stepping does not change the simulation. Events are pulled with `Simulator.step`, so a live
  log must equal an offline run of the same seed.
- Knobs are validated at the edge. Values arrive from a browser, so an unknown key, an
  out-of-range loss, or an unimplemented transport is rejected before it can reach the
  simulator, and a rejection leaves the run untouched.
- Chaos is visible. Every accepted command is echoed back with the resulting knobs, because a
  live run whose parameters cannot be read is not evidence of anything.

Deliberately no `from __future__ import annotations` here. The FastAPI imports are function-local
so that importing this module for `LiveRun` or `validate_knobs` does not require a web stack, and
that only works if annotations are evaluated when the `def` runs, inside `build_app`, where the
names actually exist. With the future import, `sock: WebSocket` becomes the string "WebSocket",
FastAPI resolves it against *module* globals, does not find it, and silently demotes the socket
to a query parameter: the route then answers every handshake with a 403 and no error anywhere.
Do not add the future import back to this module.
"""

import asyncio
import contextlib
import json
import threading
from pathlib import Path as FsPath
from typing import Any

from gyrfalcon.core.sim import Simulator
from gyrfalcon.harness import build_bulk, build_bulk_multipath
from gyrfalcon.net.path import Path

DASHBOARD_DIR = FsPath(__file__).resolve().parent.parent.parent / "dashboard"

TRANSPORTS = ("falcon", "gbn", "sr")

# Events per slice and per flush. Small enough that the dashboard animates, large enough that
# a socket write is not per-event.
STEP_EVENTS = 400
FLUSH_EVENTS = 300


def knob_defaults() -> dict[str, Any]:
    return {
        "loss": 0.01,
        "reorder": 0.0,
        "slow": 1.0,
        "transport": "falcon",
        "seed": 1,
        "n_packets": 400,
        "n_paths": 2,
        "n_flows": 2,
        "algo": "swift",
        "scheduler": "largest_open",
    }


# Bounds are checked here rather than clamped silently at the point of use: a slider sent
# loss=7.0 should be told it is wrong, not quietly turned into 0.5.
_LIMITS: dict[str, tuple[float, float]] = {
    "loss": (0.0, 0.5),
    "reorder": (0.0, 1.0),
    "slow": (1.0, 20.0),
}
_CHOICES: dict[str, tuple[str, ...]] = {
    "transport": TRANSPORTS,
    "algo": ("swift", "aimd"),
    "scheduler": ("largest_open", "round_robin"),
}
_INT_RANGE: dict[str, tuple[int, int]] = {
    "seed": (0, 1 << 30),
    "n_packets": (1, 4000),
    "n_paths": (1, 8),
    "n_flows": (1, 64),
}


class KnobError(ValueError):
    """A knob arrived from the client that must not be applied."""


def validate_knobs(knobs: dict[str, Any]) -> dict[str, Any]:
    """Return a validated copy, raising `KnobError` on the first bad value.

    Nothing is applied until every value passes, so a rejected batch cannot leave the run
    half-reconfigured.
    """
    clean: dict[str, Any] = {}
    for key, value in knobs.items():
        if key not in knob_defaults():
            raise KnobError(f"unknown knob {key!r}")
        if key in _LIMITS:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise KnobError(f"{key} must be a number, got {value!r}") from None
            lo, hi = _LIMITS[key]
            if not lo <= number <= hi:
                raise KnobError(f"{key} must be in [{lo}, {hi}], got {number}")
            clean[key] = number
        elif key in _CHOICES:
            text = str(value)
            if text not in _CHOICES[key]:
                raise KnobError(f"{key} must be one of {_CHOICES[key]}, got {text!r}")
            clean[key] = text
        else:
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise KnobError(f"{key} must be an integer, got {value!r}") from None
            lo, hi = _INT_RANGE[key]
            if not lo <= number <= hi:
                raise KnobError(f"{key} must be in [{lo}, {hi}], got {number}")
            clean[key] = number
    return clean


class LiveRun:
    """One simulation, stepped on a dedicated thread, drained into an asyncio queue.

    The simulator and everything it owns are single-threaded by nature, so exactly one thread
    ever touches them. Other tasks reach the run by posting commands to the inbox and awaiting
    a reply, which keeps that rule in one place instead of scattering locks over the session.
    """

    def __init__(self, knobs: dict[str, Any] | None = None) -> None:
        self.knobs = knob_defaults()
        self.errors: list[str] = []
        if knobs:
            try:
                self.knobs.update(validate_knobs(knobs))
            except KnobError as err:
                self.errors.append(str(err))
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.inbox: asyncio.Queue[tuple[dict[str, Any], asyncio.Future]] = asyncio.Queue()
        self.sim: Simulator | None = None
        self.paths: list[Path] = []
        self._shipped = 0
        # Batches handed to the loop but not yet put on the queue. `_shipped` moves on the
        # driver thread the instant a batch is scheduled, so without counting the in-flight
        # ones `done` would report True while the matching messages are still on their way.
        self._ship_lock = threading.Lock()
        self._inflight = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = None
        self._running = False
        self._restart = False

    # ------------------------------------------------------------------ construction

    def _construct(self) -> None:
        """Build a fresh simulation from the current knobs. Arms it but does not run."""
        k = self.knobs
        if k["transport"] == "falcon":
            sim, _sess, forwards, _rev = build_bulk_multipath(
                seed=k["seed"],
                n_packets=k["n_packets"],
                n_paths=k["n_paths"],
                n_flows=min(k["n_flows"], k["n_paths"]),
                loss=k["loss"],
                reorder=k["reorder"],
                algo=k["algo"],
                scheduler=k["scheduler"],
            )
        else:
            sim, _sess, forwards, _rev = build_bulk(
                k["transport"],
                seed=k["seed"],
                n_packets=k["n_packets"],
                loss=k["loss"],
                reorder=k["reorder"],
            )
        self.sim = sim
        self.paths = [p for p in forwards if p.name != "rev"]
        self._shipped = 0
        self._push_to_paths()

    def _push_to_paths(self) -> None:
        """Retune the live paths from the current knobs.

        Only forward paths carry loss and reordering. The reverse path is the ACK path and is
        left clean on purpose: ACK loss would otherwise be mistaken for datapath loss in the
        panels, which is the exact confusion the `kind` field exists to prevent.
        """
        for path in self.paths:
            if path.name == "rev":
                continue
            path.loss = self.knobs["loss"]
            path.reorder = self.knobs["reorder"]
        want = self.knobs["slow"]
        for path in self.paths:
            if path.name == "rev":
                continue
            if want > 1.0 and path.slow_factor != want:
                path.slow(want)
            elif want <= 1.0 and path.slow_factor != 1.0:
                # Only emit when it actually changes. Unconditionally calling `unslow` puts a
                # meaningless "slowed by 1x" event at t=0 in every log, which then shows up in
                # the dashboard's event stream as if something happened.
                path.unslow()

    # ------------------------------------------------------------------ driver thread

    def start(self) -> None:
        import threading

        self._loop = asyncio.get_running_loop()
        self._running = True
        self._restart = True
        self._thread = threading.Thread(target=self._drive, name="live-sim", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def post(self, msg: dict[str, Any]) -> asyncio.Future:
        """Queue a command for the driver thread; the future resolves with its reply."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.inbox.put_nowait((msg, fut))
        return fut

    def _drive(self) -> None:
        """The one thread that owns the simulation."""
        while self._running:
            if self._pump_inbox():
                continue
            if self._restart:
                self._restart = False
                self._construct()
                self._emit({"reset": True, "transport": self.knobs["transport"],
                            "knobs": dict(self.knobs)})
            if self.sim is None:
                continue
            if self.sim.step(STEP_EVENTS) == 0:
                # Heap drained: the run is finished. Flush what is left, or the client keeps
                # a log that is short by up to one slice. Then keep serving commands so the
                # client can still retune and ask for a restart.
                self._flush()
                continue
            self._flush()

    def _pump_inbox(self) -> bool:
        """Apply every queued command. Returns True if a restart is now pending."""
        restart = False
        while True:
            try:
                msg, fut = self.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return restart
            if self._handle(msg, fut):
                restart = True

    def _handle(self, msg: dict[str, Any], fut: asyncio.Future) -> bool:
        """Apply one command. Returns True if it asked for a fresh run."""
        cmd = msg.get("cmd")
        restart = False
        applied = False
        try:
            if cmd == "set":
                knob = msg.get("knob")
                clean = validate_knobs({knob: msg.get("value")})
                key = next(iter(clean))
                self.knobs[key] = clean[key]
                self._push_to_paths()
                applied = True
                # A transport or CC swap cannot be applied to a running session, so it
                # rebuilds instead. Everything else retunes in place.
                if key in ("transport", "algo", "scheduler", "n_packets", "n_paths", "n_flows", "seed"):
                    # Raise the flag the driver loop actually watches. Returning it to the
                    # caller is only a report; without this the transport selector would
                    # appear to work while the old transport kept running.
                    self._restart = True
                    restart = True
                self._reply(fut, {"ok": True, "knobs": dict(self.knobs), "restart": restart})
            elif cmd == "kill_path":
                if not self.paths:
                    self._reply(fut, {"ok": False, "error": "no paths"})
                else:
                    index = int(msg.get("index", 0))
                    victim = self.paths[index % len(self.paths)]
                    if not victim.killed:
                        victim.kill()
                        applied = True
                        self._reply(fut, {"ok": True, "killed": victim.name})
                    else:
                        self._reply(fut, {"ok": False, "error": f"{victim.name} is already down"})
            elif cmd in ("start", "restart"):
                if msg.get("knobs"):
                    self.knobs.update(validate_knobs(msg["knobs"]))
                self._restart = True
                restart = True
                applied = True
                self._reply(fut, {"ok": True, "knobs": dict(self.knobs), "restart": True})
            else:
                self._reply(fut, {"ok": False, "error": f"unknown cmd {cmd!r}"})
        except (KnobError, TypeError, ValueError) as err:
            self.errors.append(str(err))
            self._reply(fut, {"ok": False, "error": str(err)})
        if applied:
            # Echo the knobs even when nothing will be stepped again. Once a run finishes no
            # further event batches arrive, so without this a slider moved after completion
            # would leave the client showing the previous value indefinitely -- a live panel
            # that reports the wrong conditions is worse than no panel.
            self._emit({"control": True, "knobs": dict(self.knobs),
                        "transport": self.knobs["transport"]})
        return restart

    @property
    def total_events(self) -> int:
        return len(self.sim.bus.events) if self.sim is not None else 0

    @property
    def done(self) -> bool:
        """True once the run has completed and every event has been shipped.

        Needs both halves: an empty heap means the simulation stopped, but the tail of the log
        may still be sitting unshipped, and reporting completion before that would let a client
        read a log that is short by up to one slice. The in-flight count closes the other gap:
        `_shipped` advances on the driver thread when a batch is *scheduled*, so without it
        `done` could be True while the batch was still waiting for the loop to put it on the
        queue -- a consumer draining on `done` would then see a log missing its last slices.
        """
        return (
            self.sim is not None
            and self.sim.finished
            and self._shipped >= len(self.sim.bus.events)
            and self._inflight == 0
        )

    def _flush(self) -> None:
        """Hand the next slice of events to the asyncio side, oldest first.

        Slices the bus's own list by index instead of mirroring it through a sink. That is not
        just simpler: the FAE registers itself as a sink, so an `fae_resp` raised while handling
        an `fae_event` reaches a *later* sink before the outer `fae_event` reaches an *earlier*
        one. A sink-based stream therefore reorders those two, and the live log stops matching
        the log the same seed produces offline. Reading by index keeps the streamed log
        byte-identical to `write_jsonl`, which is the property worth having.
        """
        if self.sim is None:
            return
        events = self.sim.bus.events
        if self._shipped >= len(events):
            return
        batch = events[self._shipped:self._shipped + FLUSH_EVENTS]
        # Schedule before advancing `_shipped`: `done` must never see a fully shipped run
        # whose last batch is still in flight.
        self._emit({"events": batch, "knobs": dict(self.knobs),
                    "transport": self.knobs["transport"]})
        self._shipped += len(batch)

    def _emit(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._ship_lock:
            self._inflight += 1

        def deliver() -> None:
            self.queue.put_nowait(payload)
            with self._ship_lock:
                self._inflight -= 1

        loop.call_soon_threadsafe(deliver)

    def _reply(self, fut: asyncio.Future, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or fut.done() or loop.is_closed():
            return
        loop.call_soon_threadsafe(fut.set_result, payload)

    def collect(self) -> dict[str, Any]:
        """Drain the queue on the loop thread. For tests; the websocket path uses a task."""
        drained: list[dict[str, Any]] = []
        while not self.queue.empty():
            drained.append(self.queue.get_nowait())
        return {"messages": drained, "total": self.total_events}


async def _pump(sock, run: LiveRun) -> None:
    while True:
        item = await run.queue.get()
        await sock.send_text(json.dumps(item))


def build_app():
    """Create the FastAPI app.

    Imported lazily inside the function so that importing this module for `LiveRun` or
    `validate_knobs` does not require the web dependencies to be installed.
    """
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="Falcon-style transport, live mode")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "transport": list(TRANSPORTS), "defaults": knob_defaults()}

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        run = LiveRun()
        run.start()
        pump = asyncio.create_task(_pump(sock, run))
        try:
            while True:
                raw = await sock.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await sock.send_text(json.dumps({"ok": False, "error": "bad json"}))
                    continue
                await run.post(msg)
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            run.stop()

    # Mounted last so it does not shadow /api or /ws. Serving the directory rather than listing
    # files means /index.html and every future asset resolve; hand-listing the three current
    # files silently 404s on /index.html, which loads an empty page with no error.
    app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    return app


def main() -> None:  # pragma: no cover - process entry point
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the dashboard in live mode.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
