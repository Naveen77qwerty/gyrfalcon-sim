"""Live-mode tests: knob validation, chaos effects, and the equivalence invariant.

The invariant that matters most here is the first one. Live mode advances the simulation in
slices on a background thread instead of running it to completion, and it ships events over a
socket. If any of that changed the run, every live number would be a number from a
different simulation than the offline experiments produce, and the two could not be compared.
So the primary test asserts equality rather than mere plausibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from gyrfalcon.apps.live import KnobError, LiveRun, knob_defaults, validate_knobs
from gyrfalcon.core.sim import Simulator
from gyrfalcon.harness import build_bulk, run_bulk, run_bulk_multipath

IDLE_TIMEOUT = 10.0


def drive(knobs: dict | None = None, timeout: float = IDLE_TIMEOUT):
    """Run a LiveRun to completion and return `(run, messages)`."""
    async def go():
        run = LiveRun(knobs)
        run.start()
        deadline = asyncio.get_running_loop().time() + timeout
        while not run.done and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        messages = []
        while not run.queue.empty():
            messages.append(run.queue.get_nowait())
        run.stop()
        return run, messages

    return asyncio.run(go())


def streamed(messages: list[dict]) -> list[dict]:
    return [ev for msg in messages for ev in msg.get("events", [])]


async def settle(run, messages, predicate, timeout: float = IDLE_TIMEOUT,
                 require_done: bool = False):
    """Drain the queue until `predicate(messages)` holds.

    Waiting on `run.done` alone is not enough after a restart: the previous run already
    reported done, so the wait would return before the rebuild had emitted anything. Waiting on
    a message only the new run can produce (its reset marker, or an event from its log) is what
    actually means "the swap happened". `require_done` additionally waits for the run to finish
    before returning, which the caller wants before asserting on the whole log.

    Everything here is condition-based rather than sleep-based: the driver thread ships events
    in slices on its own schedule, so any fixed delay is a guess about how far it has got.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        while not run.queue.empty():
            messages.append(run.queue.get_nowait())
        if predicate(messages) and (run.done or not require_done):
            return messages
        await asyncio.sleep(0.02)
    while not run.queue.empty():
        messages.append(run.queue.get_nowait())
    return messages


def after_gbn_reset(messages: list[dict]) -> list[dict]:
    """The events on screen: everything after the newest `transport=gbn` reset marker."""
    cuts = [i for i, m in enumerate(messages)
            if m.get("reset") and m["knobs"]["transport"] == "gbn"]
    if not cuts:
        return []
    return streamed(messages[cuts[-1] + 1:])


# --------------------------------------------------------------------- equivalence


def test_a_live_run_matches_the_offline_log_byte_for_byte():
    run, messages = drive({"n_packets": 120, "loss": 0.02})
    offline = run_bulk_multipath(seed=1, n_packets=120, loss=0.02).bus.events
    assert streamed(messages) == offline


@pytest.mark.parametrize(
    "knobs,offline",
    [
        ({"n_packets": 60, "transport": "gbn", "loss": 0.01},
         lambda: run_bulk("gbn", seed=1, n_packets=60, loss=0.01).bus.events),
        ({"n_packets": 60, "transport": "sr", "loss": 0.05},
         lambda: run_bulk("sr", seed=1, n_packets=60, loss=0.05).bus.events),
        ({"n_packets": 60, "loss": 0.05, "algo": "aimd"},
         lambda: run_bulk_multipath(seed=1, n_packets=60, loss=0.05, algo="aimd").bus.events),
        ({"n_packets": 80, "loss": 0.0, "reorder": 0.3},
         lambda: run_bulk_multipath(seed=1, n_packets=80, loss=0.0, reorder=0.3).bus.events),
    ],
    ids=["gbn", "sr", "aimd", "reorder"],
)
def test_every_transport_reproduces_its_offline_log(knobs, offline):
    run, messages = drive(knobs)
    assert streamed(messages) == offline()


def test_stepping_the_simulator_does_not_change_it():
    """`Simulator.step` slices the loop; it must not perturb it."""
    reference = Simulator(seed=7)
    _sim, _sess, _fwd, _rev = build_bulk("falcon", seed=7, n_packets=60, loss=0.03)
    sliced = Simulator(seed=7)
    _s2, _ss, _f, _r = build_bulk("falcon", seed=7, n_packets=60, loss=0.03)
    sliced.bus.events.clear()

    while not reference.finished:
        reference.step(17)
    while not sliced.finished:
        sliced.step(5)
    assert sliced.bus.events == reference.bus.events
    assert sliced.rng.random() == reference.rng.random(), "RNG stream diverged"


# ------------------------------------------------------------------ knob validation


@pytest.mark.parametrize(
    "bad",
    [
        {"loss": 0.9},
        {"loss": -1},
        {"reorder": 2},
        {"slow": 0},
        {"n_packets": 0},
        {"n_paths": 99},
        {"transport": "quic"},
        {"algo": "bbr"},
        {"scheduler": "random"},
        {"seed": -1},
        {"nonsense": 1},
        {"loss": "quite a lot"},
    ],
)
def test_bad_knobs_are_rejected_at_the_edge(bad):
    with pytest.raises(KnobError):
        validate_knobs(bad)


def test_good_knobs_pass_validation():
    clean = validate_knobs({"loss": 0.02, "reorder": 0.5, "slow": 4,
                            "transport": "sr", "n_packets": 10})
    assert clean == {"loss": 0.02, "reorder": 0.5, "slow": 4.0,
                     "transport": "sr", "n_packets": 10}


def test_a_rejected_batch_changes_nothing():
    """Validation happens before application, so a bad knob in a batch cannot half-apply."""
    base = knob_defaults()
    with pytest.raises(KnobError):
        validate_knobs({"loss": 0.2, "reorder": 99})
    # The rejected batch's values are not reflected anywhere; defaults are untouched.
    assert base["loss"] == 0.01
    assert base["reorder"] == 0.0


def test_construction_survives_a_bad_start_request():
    run = LiveRun({"loss": 5.0})
    assert run.errors, "a rejected start request must be recorded, not swallowed"
    assert run.knobs["loss"] == knob_defaults()["loss"]


# ------------------------------------------------------------------- chaos effects


def test_every_event_reaches_the_client():
    run, messages = drive({"n_packets": 100, "loss": 0.02})
    assert len(streamed(messages)) == run.total_events
    assert len(streamed(messages)) > 0


def test_the_run_reports_done_only_once_it_is_fully_shipped():
    run, messages = drive({"n_packets": 80, "loss": 0.01})
    assert run.done
    assert run.sim.finished
    assert run._shipped == len(run.sim.bus.events)


def test_no_no_op_unslow_event_is_emitted():
    """`unslow` at factor 1.0 would put a meaningless event at t=0 of every live log."""
    run, messages = drive({"n_packets": 40, "loss": 0.0, "slow": 1.0})
    assert not [e for e in streamed(messages) if e["type"] == "path_slow"]


def test_raising_the_loss_knob_actually_raises_drops():
    clean, messages = drive({"n_packets": 200, "loss": 0.0})
    lossy, lossy_messages = drive({"n_packets": 200, "loss": 0.25})

    def drops(msgs):
        return len([e for e in streamed(msgs) if e["type"] == "pkt_drop"])

    assert drops(messages) == 0
    assert drops(lossy_messages) > drops(messages)
    # And the knob the server reports matches what it applied.
    assert lossy.knobs["loss"] == 0.25


def test_kill_path_takes_a_forward_path_down_and_says_so():
    async def go():
        run = LiveRun({"n_packets": 400, "loss": 0.0})
        run.start()
        await asyncio.sleep(0.05)
        reply = await run.post({"cmd": "kill_path", "index": 0})
        await asyncio.sleep(0.1)
        messages = []
        while not run.queue.empty():
            messages.append(run.queue.get_nowait())
        run.stop()
        return run, reply, messages

    run, reply, messages = asyncio.run(go())
    assert reply["ok"] is True
    assert reply["killed"] == "fwd0"
    assert any(p.killed for p in run.paths)
    kills = [e for e in streamed(messages) if e["type"] == "path_kill"]
    assert len(kills) == 1
    assert kills[0]["conn"] == 0, "path events are not connection-scoped"


def test_killing_a_dead_path_is_refused():
    async def go():
        run = LiveRun({"n_packets": 400})
        run.start()
        await asyncio.sleep(0.05)
        await run.post({"cmd": "kill_path", "index": 0})
        second = await run.post({"cmd": "kill_path", "index": 0})
        run.stop()
        return second

    reply = asyncio.run(go())
    assert reply["ok"] is False
    assert "already down" in reply["error"]


def test_the_slow_knob_emits_path_slow_once():
    async def go():
        run = LiveRun({"n_packets": 400, "slow": 4.0})
        run.start()
        await asyncio.sleep(0.05)
        reply = await run.post({"cmd": "set", "knob": "slow", "value": 4.0})
        await asyncio.sleep(0.05)
        run.stop()
        return reply

    reply = asyncio.run(go())
    assert reply["ok"] is True
    assert reply["restart"] is False, "retuning the fabric does not need a new run"


def test_an_unknown_command_is_refused_rather_than_ignored():
    async def go():
        run = LiveRun({"n_packets": 40})
        run.start()
        reply = await run.post({"cmd": "rm -rf"})
        run.stop()
        return reply

    reply = asyncio.run(go())
    assert reply["ok"] is False
    assert "unknown cmd" in reply["error"]


def test_switching_transport_rebuilds_the_run():
    async def go():
        run = LiveRun({"n_packets": 60})
        run.start()
        # Swap only once the first run has actually streamed an event. The old fixed
        # `await asyncio.sleep(0.05)` was a guess about how fast the driver thread starts,
        # and it made the test race its own setup instead of the behaviour under test.
        started = await settle(run, [], lambda ms: bool(streamed(ms)))
        reply = await run.post({"cmd": "set", "knob": "transport", "value": "gbn"})
        # Wait for a pkt_send in the post-reset log itself rather than asserting on whatever
        # happens to have been drained when the reset marker shows up: the gbn run ships in
        # slices, so a snapshot taken at the reset can miss the sends that follow it.
        messages = await settle(
            run, started,
            lambda ms: any(e["type"] == "pkt_send" for e in after_gbn_reset(ms)),
            require_done=True,
        )
        run.stop()
        return run, reply, messages

    run, reply, messages = asyncio.run(go())
    assert reply["ok"] is True
    assert reply["restart"] is True, "a transport swap cannot retune a running session"
    # Each run opens with a reset marker, and the client discards everything before it. Only
    # what follows the gbn reset is the log on screen.
    cuts = [i for i, m in enumerate(messages)
            if m.get("reset") and m["knobs"]["transport"] == "gbn"]
    assert cuts, "the swap should have rebuilt the run"
    events = streamed(messages[cuts[-1] + 1:])
    assert run.knobs["transport"] == "gbn"
    assert [e for e in events if e["type"] == "pkt_send"], "the rebuilt run never started sending"
    assert not [e for e in events if e["type"] == "fae_resp"], "GBN has no FAE"
    assert not [e for e in events if e["type"] == "fae_event"], "GBN has no FAE"



def test_streamed_events_are_json_serialisable():
    """The socket payload is JSON; a log that cannot cross the wire is not usable live."""
    run, messages = drive({"n_packets": 60, "loss": 0.02})
    for msg in messages:
        json.dumps(msg)
    assert streamed(messages)


# ------------------------------------------------------------------- ASGI surface


def _ws_scope(path: str = "/ws") -> dict:
    return {
        "type": "websocket", "path": path, "raw_path": path.encode(),
        "headers": [(b"host", b"testserver")], "method": "GET", "root_path": "",
        "scheme": "ws", "query_string": b"", "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80), "http_version": "1.1", "subprotocols": [],
    }


def _http_scope(path: str) -> dict:
    return {
        "type": "http", "path": path, "raw_path": path.encode(),
        "headers": [(b"host", b"testserver")], "method": "GET", "root_path": "",
        "scheme": "http", "query_string": b"", "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80), "http_version": "1.1",
    }


def test_the_socket_route_is_not_silently_demoted_to_a_query_param():
    """Regression: the route answered every handshake with a 403 and logged nothing.

    `from __future__ import annotations` turns `sock: WebSocket` into the string "WebSocket",
    which FastAPI resolves against *module* globals. The FastAPI imports are function-local, so
    the name is not there, and FastAPI quietly treats the socket as a query parameter. Driving
    the ASGI app directly is the cheapest way to see it: the first message out must be
    `websocket.accept`, not `websocket.close`.
    """
    from gyrfalcon.apps.live import build_app

    app = build_app()
    sent: list[dict] = []
    connected = asyncio.Event()
    inbox: asyncio.Queue = asyncio.Queue()

    async def receive():
        if connected.is_set():
            return await inbox.get()
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "websocket.accept":
            connected.set()
            inbox.put_nowait({"type": "websocket.receive",
                              "text": '{"cmd":"start","knobs":{"n_packets":30}}'})
        if message["type"] == "websocket.close":
            connected.set()

    async def go():
        task = asyncio.create_task(app(_ws_scope(), receive, send))
        await asyncio.sleep(0.8)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert sent, "the app never responded at all"
    assert sent[0]["type"] == "websocket.accept", (
        f"handshake refused: {sent[0]}"
    )
    assert any(m["type"] == "websocket.send" for m in sent), "no events streamed"


def test_the_static_mount_cannot_shadow_the_socket_route():
    """Routes are matched in declaration order, so `/ws` has to be registered before the mount."""
    from gyrfalcon.apps.live import build_app

    paths = [getattr(r, "path", None) for r in build_app().routes]
    assert "/ws" in paths, "the websocket route is missing"
    assert paths.index("/ws") < paths.index(""), "the catch-all mount shadows /ws"


def test_the_health_route_reports_the_defaults():
    from gyrfalcon.apps.live import build_app

    app = build_app()
    body: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            body["status"] = message["status"]
        elif message["type"] == "http.response.body":
            body["raw"] = message.get("body", b"")

    asyncio.run(app(_http_scope("/api/health"), receive, send))
    assert body["status"] == 200
    assert json.loads(body["raw"])["defaults"] == knob_defaults()
