"""FAE: event-in / parameter-out split, and the policy it owns (paper 4.2, Table 3)."""

from __future__ import annotations

from collections import Counter

from falcon.apps.incast import run_incast
from falcon.core.sim import Simulator
from falcon.fae.engine import AimdCc, Fae, SwiftCc
from falcon.fae.policy import Policy
from falcon.harness import run_bulk_multipath
from falcon.metrics import metrics_from_events
from falcon.net.path import Path
from falcon.pdl.reliability import FalconStylePdl


def _events(sim: Simulator, etype: str) -> list[dict]:
    return [e for e in sim.bus.events if e["type"] == etype]


def test_fae_replies_when_it_is_given_a_simulator():
    """Regression: `run_incast` built its FAE without `sim`, so `add_sink` never ran and
    every sender silently used a frozen constant policy. 320 events in, 0 responses out."""
    sim = run_incast(n_senders=4, seed=1, n_packets=30, until=1.0)
    assert len(_events(sim, "fae_event")) > 0
    assert len(_events(sim, "fae_resp")) > 0


def test_fae_responses_are_scoped_to_their_connection():
    sim = run_incast(n_senders=4, seed=1, n_packets=30, until=1.0)
    conns = {e["conn"] for e in _events(sim, "fae_resp")}
    assert conns == {1, 2, 3, 4}


def test_fae_emits_responses_on_change_not_per_event():
    """One response per fae_event would double the log and hide the parameter timeline."""
    sim = run_incast(n_senders=4, seed=1, n_packets=30, until=1.0)
    assert len(_events(sim, "fae_resp")) < len(_events(sim, "fae_event")) / 5


def test_fae_actually_moves_its_policy():
    sim = Simulator(seed=1)
    fae = Fae(sim=sim, conn=1)
    start = fae.policy.rack_rto
    for _ in range(5):
        fae.on_event({"type": "fae_event", "conn": 1, "t": 0.0, "fabric_delay": 1.0e-3, "buffer_occ": 0.0})
    assert fae.policy.rack_rto > start


def test_repath_moves_flows_off_a_dead_path():
    """No flow is ever left on the path that just died. Which survivor it lands on is a
    policy choice; today that is the lowest-numbered live path. Repathing a path nothing
    is using reports that it moved nothing."""
    fae = Fae(sim=Simulator(seed=1), n_flows=2, path_ids=[0, 1, 2])
    assert fae.policy.path_for_flow == {0: 0, 1: 1}
    for dead in (0, 1, 2, 1, 0):
        was_using = dead in fae.policy.path_for_flow.values()
        assert fae.repath(dead) is was_using
        assert dead not in fae.policy.path_for_flow.values()
        assert set(fae.policy.path_for_flow.values()) <= {0, 1, 2}


def test_repath_ignores_paths_the_engine_does_not_use():
    fae = Fae(sim=Simulator(seed=1), n_flows=1, path_ids=[0])
    assert fae.repath(7) is False
    assert fae.repath(0) is False, "no alternative path exists, so nothing should move"


def test_path_kill_drives_repath_end_to_end():
    sim = run_bulk_multipath(seed=5, n_packets=120, n_paths=2, n_flows=2, until=0.5, kill_path=0, kill_at=2e-5)
    kills = _events(sim, "path_kill")
    resps = _events(sim, "fae_resp")
    assert [k["path"] for k in kills] == [0]
    # The kill can land before the FAE's first response, so the pre-kill assignment is not
    # necessarily in the log. What must hold is that no flow was left on the dead path.
    assert resps, "the FAE never responded, so it never saw the kill"
    assert 0 not in resps[-1]["path_for_flow"].values(), resps[-1]["path_for_flow"]
    assert metrics_from_events(sim.bus.events)["packets_delivered"] == 120


def test_a_path_kill_must_actually_cost_something():
    """Guards the demo. A kill timed outside the transfer's flight window drops nothing and
    produces a log identical to the no-kill run, so the reroute story never happens."""
    killed = run_bulk_multipath(seed=5, n_packets=120, n_paths=3, n_flows=3, until=0.05, kill_path=1, kill_at=2e-5)
    intact = run_bulk_multipath(seed=5, n_packets=120, n_paths=3, n_flows=3, until=0.05, kill_at=2e-5)

    lost = [e for e in _events(killed, "pkt_drop") if e.get("reason") == "path_killed"]
    assert lost, "nothing was lost, so this scenario demonstrates nothing"
    assert metrics_from_events(killed.bus.events)["packets_delivered"] == 120, "the transfer must still finish"
    assert metrics_from_events(killed.bus.events)["goodput_bps"] < metrics_from_events(intact.bus.events)["goodput_bps"]


def test_cc_swap_changes_behaviour_without_touching_pdl():
    """The plan's 'party trick': the algorithm is selected by name at construction and the
    datapath is untouched. Compared on the fcwnd trajectory, which is where the algorithms
    actually differ -- on an unimpaired path both simply grow the window."""
    def trajectory(algo: str) -> list[float]:
        sim = run_bulk_multipath(seed=3, n_packets=300, until=1.0, loss=0.01, algo=algo)
        return [r["fcwnd"]["0"] for r in _events(sim, "fae_resp")]

    swift = trajectory("swift")
    aimd = trajectory("aimd")
    assert swift and aimd
    assert swift != aimd, "swapping the CC module changed nothing"


def test_cc_interfaces_are_interchangeable():
    for cc in (SwiftCc(), AimdCc()):
        pol = Policy(fcwnd={0: 32.0})
        cc.on_sample(pol, 5e-5, 0.0, 0.0)
        assert pol.fcwnd[0] > 32.0, "below target must grow the window"
        cc.on_sample(pol, 1e-2, 0.0, 1.0)
        assert pol.fcwnd[0] < 32.0 + 1e-9, "above target must shrink it"


def test_the_two_ccs_actually_disagree():
    """Swapping the module has to change something, or the pluggability claim is decoration.

    They only diverge once delay rises: with a flat signal both take the additive-increase
    branch and end at the same window. That is also why an unimpaired simulated path cannot
    show the difference -- there is nothing for either controller to react to.
    """
    def final_window(cc) -> float:
        pol = Policy(fcwnd={0: 32.0})
        for i in range(40):
            delay = 5e-5 if i < 10 else (3e-4 if i < 20 else 5e-5)
            cc.on_sample(pol, delay, 0.0, i * 1e-4)
        return pol.fcwnd[0]

    swift = final_window(SwiftCc())
    aimd = final_window(AimdCc())
    assert swift != aimd, "swift and aimd produced identical windows"
    # AIMD halves on overshoot while Swift backs off proportionally, so AIMD must be harsher.
    assert aimd < swift, (aimd, swift)


def test_more_flows_than_paths_is_supported():
    """A connection normally has more flows than the fabric has links. One-path-per-flow
    indexing crashed the FAE constructor instead of sharing paths."""
    fae = Fae(sim=Simulator(seed=1), n_flows=8, path_ids=[0, 1, 2])
    assert set(fae.policy.path_for_flow) == set(range(8))
    assert set(fae.policy.path_for_flow.values()) == {0, 1, 2}
    assert len(fae.policy.fcwnd) == 8


def test_repath_spreads_displaced_flows_instead_of_stacking_them():
    """Moving every displaced flow onto one alternative converts an outage into a
    congestion collapse on that path."""
    fae = Fae(sim=Simulator(seed=1), n_flows=6, path_ids=[0, 1, 2])
    assert fae.repath(0) is True
    counts = Counter(fae.policy.path_for_flow.values())
    assert set(counts) == {1, 2}, counts
    assert max(counts.values()) - min(counts.values()) <= 1, counts


def test_round_robin_scheduler_is_selectable_from_policy():
    """Swapping schedulers is an fae/ change: pdl reads the name, it does not branch on a
    hardcoded rule."""
    sim = Simulator(seed=1)
    pol = Policy(fcwnd={0: 4.0, 1: 4.0}, scheduler="round_robin")
    sess = FalconStylePdl(sim, Path(sim, delay=5e-5, path_id=0), Path(sim, delay=5e-5, path_id=1), policy=pol)
    sender = sess.sender
    assert [sender._pick_flow() for _ in range(4)] == [1, 0, 1, 0], "round robin must alternate"

    pol.scheduler = "largest_open"
    pol.fcwnd = {0: 32.0, 1: 4.0}
    assert sender._pick_flow() == 0, "largest open window must win on window size"

    sender.unacked_per_flow[0] = 30
    assert sender._pick_flow() == 1, "flow 0 is now full, so flow 1 must be picked"


def test_pdl_holds_no_congestion_policy():
    """CLAUDE.md: policy constants live only in fae/. `pdl` may read a Policy, never
    write one, and must not know which CC algorithm is in use."""
    import re
    from pathlib import Path

    writes_policy = re.compile(r"\b\w*[Pp]olicy\w*\.\w+\s*=(?!=)")
    for f in (Path(__file__).resolve().parents[1] / "falcon" / "pdl").rglob("*.py"):
        text = f.read_text()
        assert not writes_policy.search(text), f"{f.name} writes to a Policy"
        for token in ("SwiftCc", "AimdCc", "fae.engine", "fae.policy import Fae"):
            assert token not in text, f"{f.name} references {token}; policy belongs to fae/"