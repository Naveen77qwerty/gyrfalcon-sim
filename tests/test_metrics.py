"""Metrics must be derived from deliveries, not from arrivals.

Regression cover for a bug that made Go-Back-N look four times faster than Falcon-style:
`pkt_recv` was emitted for every arrival, including duplicates of PSNs already delivered and
out-of-order packets Go-Back-N discards, so goodput scaled with retransmission volume.
"""

from __future__ import annotations

from gyrfalcon.core.clock import Clock
from gyrfalcon.core.event_bus import EventBus
from gyrfalcon.core.sim import Simulator
from gyrfalcon.harness import run_bulk
from gyrfalcon.metrics import metrics_from_events


def _recv(t: float, conn: int, psn: int, **kw: object) -> dict:
    return {"t": t, "type": "pkt_recv", "conn": conn, "psn": psn, "kind": "data", **kw}


def test_duplicate_arrivals_are_not_counted_as_deliveries():
    events = [
        _recv(0.001, 1, 0, accepted=True, dup=False),
        _recv(0.002, 1, 1, accepted=True, dup=False),
        _recv(0.003, 1, 0, accepted=False, dup=True),
        _recv(0.004, 1, 0, accepted=False, dup=True),
        {"t": 0.005, "type": "conn_state", "conn": 1, "state": "TEARDOWN"},
    ]
    m = metrics_from_events(events)
    assert m["packets_delivered"] == 2
    assert m["duplicate_arrivals"] == 2


def test_ooo_discarded_packets_are_not_deliveries():
    """Go-Back-N emits accepted=false for OOO arrivals; they must not count."""
    events = [
        _recv(0.001, 1, 0, accepted=True, dup=False),
        _recv(0.002, 1, 1, accepted=False, dup=False),
        _recv(0.003, 1, 2, accepted=False, dup=False),
        {"t": 0.004, "type": "conn_state", "conn": 1, "state": "TEARDOWN"},
    ]
    assert metrics_from_events(events)["packets_delivered"] == 1


def test_goodput_does_not_reward_retransmission_volume():
    """The regression itself: GBN retransmits far more, so arrival-counting goodput
    ranked it above a transport that actually delivered every byte."""
    kwargs = dict(seed=3, n_packets=120, loss=0.0, reorder=0.3, until=2.0)
    gbn = metrics_from_events(run_bulk("gbn", **kwargs).bus.events)
    fal = metrics_from_events(run_bulk("falcon", **kwargs).bus.events)
    assert gbn["retransmissions"] > 10 * fal["retransmissions"]
    assert gbn["packets_delivered"] == fal["packets_delivered"] == 120
    assert fal["goodput_bps"] > gbn["goodput_bps"]


def test_spurious_retx_requires_prior_delivery():
    """A retransmission sent before the original arrived was a reasonable decision."""
    events = [
        # PSN 7 is retransmitted first and only then arrives: a needed retransmission.
        {"t": 0.0010, "type": "pkt_send", "conn": 1, "psn": 7, "kind": "data", "retx": True},
        _recv(0.0020, 1, 7, accepted=True, dup=False),
        # PSN 9 is delivered first and only then retransmitted: that one was spurious.
        _recv(0.0025, 1, 9, accepted=True, dup=False),
        {"t": 0.0030, "type": "pkt_send", "conn": 1, "psn": 9, "kind": "data", "retx": True},
        _recv(0.0035, 1, 9, accepted=False, dup=True),
        {"t": 0.0050, "type": "conn_state", "conn": 1, "state": "TEARDOWN"},
    ]
    m = metrics_from_events(events)
    assert m["retransmissions"] == 2
    assert m["spurious_retransmissions"] == 1


def test_loss_does_not_make_everything_spurious():
    """The old rule ('was this PSN ever dropped?') reported 100% spurious under zero loss
    and hid real loss-triggered retransmissions behind any earlier drop."""
    kwargs = dict(seed=2, n_packets=250, loss=0.01, until=2.0)
    zero_loss = metrics_from_events(run_bulk("falcon", loss=0.0, n_packets=250, seed=2, until=2.0).bus.events)
    with_loss = metrics_from_events(run_bulk("falcon", **kwargs).bus.events)
    assert zero_loss["retransmissions"] == 0
    assert with_loss["drops"] > 0
    assert with_loss["spurious_retransmissions"] <= with_loss["retransmissions"]


def test_completion_time_uses_teardown_not_last_event():
    events = [
        _recv(0.001, 1, 0, accepted=True, dup=False),
        {"t": 0.002, "type": "conn_state", "conn": 1, "state": "TEARDOWN"},
        {"t": 9.999, "type": "pkt_recv", "conn": 1, "psn": 99, "kind": "ack", "dup": True},
    ]
    assert metrics_from_events(events)["completion_time"] == 0.002


def test_event_log_does_not_alias_live_state():
    """A record must not change after emission, or the log cannot be replayed.

    This is how the FAE's `path_for_flow` silently rewrote its own history: repathing
    mutated the live dict that earlier `fae_resp` records still pointed at.
    """
    sim = Simulator(seed=1)
    assignment = {0: 0, 1: 1}
    sim.bus.emit("fae_resp", conn=1, path_for_flow=assignment)
    assignment[0] = 1
    assert sim.bus.events[0]["path_for_flow"] == {0: 0, 1: 1}


def test_shipped_logs_validate_against_the_schema():
    from tests.test_event_schema import validate_events

    sim = run_bulk("falcon", seed=4, n_packets=60, loss=0.01, reorder=0.2, until=1.0)
    validate_events(sim.bus.events)