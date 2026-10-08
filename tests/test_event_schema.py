"""Validator for docs/event-schema.md.

The plan calls for a test that checks a real log against the schema, so that adding an
event type without documenting it -- or documenting a field nobody emits -- fails loudly
instead of quietly producing a dashboard that renders nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REQUIRED = ("t", "type", "conn")

# type -> fields the schema says must be present. `pkt_send`/`pkt_recv` carry the most
# because the dashboard's packet ladder is driven entirely by them.
FIELDS: dict[str, tuple[str, ...]] = {
    "pkt_send": ("flow", "psn", "retx", "size", "kind", "path", "is_response"),
    "pkt_drop": ("psn", "kind", "reason"),
    "pkt_reorder": ("psn", "extra_delay", "path"),
    "pkt_recv": ("flow", "psn", "ooo", "kind", "dup", "accepted", "is_response"),
    "ack_send": ("base_psn", "bitmap", "rx_buf", "t1", "t2", "t3"),
    "ack_recv": ("base_psn", "bitmap"),
    "nack_send": ("psn",),
    "rack_fire": ("psn", "elapsed"),
    "tlp_fire": ("psn",),
    "conn_state": ("state",),
    "fae_event": ("buffer_occ", "acked", "t1", "t2", "t3", "t4", "fabric_delay"),
    "fae_resp": ("fcwnd", "ncwnd", "pacing_gap", "rack_rto", "tlp_idle", "alpha", "path_for_flow"),
    "resource_reserve": ("pool", "n"),
    "resource_release": ("pool", "n"),
    "xon": ("reason",),
    "xoff": ("reason",),
    "resource_nack": ("pool",),
    "path_kill": ("path",),
    "path_slow": ("path", "factor"),
    "txn_start": ("rsn", "kind"),
    "txn_complete": ("rsn", "kind"),
    "ulp_deliver": ("rsn", "op"),
    "tl_deadlock": ("queued", "pending_responses"),
}

# Events the simulator may add without a schema update; keep this list empty.
UNDOCUMENTED_OK: set[str] = set()

STATES = {"SETUP", "ESTABLISHED", "TEARDOWN"}
DROP_REASONS = {"random_loss", "path_killed", "queue"}


def validate_events(events: list[dict[str, Any]]) -> None:
    """Raise AssertionError naming the first event that violates the schema."""
    for i, ev in enumerate(events):
        for field in REQUIRED:
            assert field in ev, f"event {i} ({ev.get('type')}) missing required field {field!r}"
        etype = ev["type"]
        assert etype not in UNDOCUMENTED_OK, f"event {i} uses undocumented type {etype!r}"
        assert etype in FIELDS, f"event {i} has type {etype!r} which the schema does not describe"
        for field in FIELDS[etype]:
            assert field in ev, f"event {i} ({etype}) missing schema field {field!r}: {ev}"
        if etype == "conn_state":
            assert ev["state"] in STATES, f"event {i}: bad state {ev['state']!r}"
        if etype == "pkt_drop":
            assert ev["reason"] in DROP_REASONS, f"event {i}: bad drop reason {ev['reason']!r}"
        assert isinstance(ev["t"], (int, float)), f"event {i} has non-numeric t"
    times = [ev["t"] for ev in events]
    assert times == sorted(times), "event log is not in non-decreasing time order"


def test_schema_matches_the_documented_table():
    """Keep the parser honest against the markdown table it came from."""
    doc = Path(__file__).resolve().parents[1] / "docs" / "event-schema.md"
    text = doc.read_text()
    for etype in FIELDS:
        assert f"`{etype}`" in text, f"{etype} is not in event-schema.md"
    for field in ("accepted", "dup"):
        assert f"`{field}`" in text, f"pkt_recv field {field} is not documented"


def test_every_shipped_scenario_log_validates():
    """A real log against the real schema. Catches an event emitted somewhere the table
    never mentioned, which otherwise shows up as a blank panel much later."""
    from gyrfalcon.apps.incast import run_incast
    from gyrfalcon.apps.remote_disk import run_remote_disk
    from gyrfalcon.harness import run_bulk, run_bulk_multipath

    logs = [
        run_bulk("falcon", seed=1, n_packets=40, loss=0.01, reorder=0.2, until=0.5).bus.events,
        run_bulk("gbn", seed=1, n_packets=20, loss=0.02, until=0.5).bus.events,
        run_bulk("sr", seed=1, n_packets=20, loss=0.02, reorder=0.3, until=0.5).bus.events,
        run_incast(n_senders=3, seed=1, n_packets=10, until=0.3).bus.events,
        run_remote_disk(seed=1, loss=0.01, n_writes=1, n_reads=2, until=0.5)[0].bus.events,
        run_bulk_multipath(seed=1, n_packets=40, n_paths=2, n_flows=2, until=0.05, kill_path=0).bus.events,
    ]
    for i, log in enumerate(logs):
        assert log, f"scenario {i} produced no events"
        validate_events(log)


def test_feature_matrix_covers_every_simplification_claim():
    """CLAUDE.md requires simplifications to be recorded. This only checks the table is
    populated and names the areas it claims to cover -- it cannot check the claims are true."""
    matrix = Path(__file__).resolve().parents[1] / "docs" / "feature-matrix.md"
    text = matrix.read_text()
    for section in ("Mechanism", "Policy", "Applications and experiments", "Known measurement traps"):
        assert f"## {section}" in text, f"feature matrix has no {section} section"
    for keyword in ("simplified", "not implemented"):
        assert keyword in text, f"feature matrix records nothing as '{keyword}'"
    assert "does not reproduce the paper's numbers" in text, "must not imply parity with the paper"


def test_fae_response_carries_the_policy_fields_the_matrix_claims():
    """The matrix advertises fcwnd/ncwnd/pacing/alpha/path assignment on every response."""
    from gyrfalcon.core.sim import Simulator
    from gyrfalcon.fae.engine import Fae

    sim = Simulator(seed=1)
    fae = Fae(sim=sim, n_flows=1, conn=1)
    fae.on_event({"type": "fae_event", "conn": 1, "fabric_delay": 1e-3, "buffer_occ": 0.0})
    resp = [e for e in sim.bus.events if e["type"] == "fae_resp"][-1]
    for f in ("fcwnd", "ncwnd", "pacing_gap", "alpha", "path_for_flow", "rack_rto", "tlp_idle"):
        assert f in resp, f"fae_resp is missing {f}"


def test_validator_rejects_a_bad_event():
    try:
        validate_events([{"t": 0.0, "type": "pkt_send", "conn": 1}])
    except AssertionError as exc:
        assert "missing schema field" in str(exc)
    else:
        raise AssertionError("validator accepted a pkt_send with no fields")


def test_validator_rejects_undocumented_type():
    try:
        validate_events([{"t": 0.0, "type": "not_a_real_event", "conn": 1}])
    except AssertionError as exc:
        assert "does not describe" in str(exc)
    else:
        raise AssertionError("validator accepted an unknown event type")


def test_validator_rejects_out_of_order_log():
    try:
        validate_events(
            [
                {"t": 0.2, "type": "conn_state", "conn": 1, "state": "SETUP"},
                {"t": 0.1, "type": "conn_state", "conn": 1, "state": "TEARDOWN"},
            ]
        )
    except AssertionError as exc:
        assert "non-decreasing" in str(exc)
    else:
        raise AssertionError("validator accepted a time-travelling log")