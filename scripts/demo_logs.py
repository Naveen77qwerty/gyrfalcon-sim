"""Scripted demo scenarios (plan Phase 4): each writes a replayable JSONL log.

Run one directly (`python scripts/demo_logs.py a`) or via `make demo-a`. Every scenario is
seeded, so the log and the visuals it produces are identical on every run -- which is the
only reason a recorded walkthrough is worth having.

Deviation from the plan, stated openly: Scenario A is specified "on the remote-disk workload",
but the baselines are packet-level only and have no transaction layer, so a side-by-side needs
the PDL bulk workload. The disk workload is exercised by `experiments/remote_disk.py` and by
Scenario B's Falcon-style run instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "results" / "demo"

from falcon.apps.incast import run_incast
from falcon.apps.remote_disk import run_remote_disk
from falcon.harness import run_bulk, run_bulk_multipath
from falcon.metrics import metrics_from_events


def _save(sim, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.jsonl"
    sim.bus.write_jsonl(path)
    return path


def _summary(sim) -> str:
    m = metrics_from_events(sim.bus.events)
    return (
        f"delivered {m['packets_delivered']:>4}  retx {m['retransmissions']:>4}  "
        f"spurious {m['spurious_retransmissions']:>4}  goodput {m['goodput_bps'] / 1e6:>7.1f} Mbps"
    )


def scenario_a(seed: int = 21) -> None:
    """A, '1% loss': GBN vs Falcon-style, same seed, same impairment."""
    print("Scenario A -- 1% loss, GBN vs Falcon-style")
    for tr in ("gbn", "falcon"):
        sim = run_bulk(tr, seed=seed, n_packets=200, loss=0.01, until=2.0)
        print(f"  {tr:<7} {_summary(sim)}")
        print(f"          -> {_save(sim, f'a_1pct_{tr}')}")


def scenario_b(seed: int = 13) -> None:
    """B, 'reordering': spurious retransmissions on the baselines, near zero on Falcon-style."""
    print("Scenario B -- 40% reordering, spurious retransmissions")
    for tr in ("gbn", "sr", "falcon"):
        sim = run_bulk(tr, seed=seed, n_packets=200, reorder=0.4, loss=0.0, until=2.0)
        print(f"  {tr:<7} {_summary(sim)}")
        print(f"          -> {_save(sim, f'b_reorder_{tr}')}")

    sim, _store, _tl = run_remote_disk(seed=seed, loss=0.0, n_writes=1, n_reads=4, until=2.0)
    print(f"  disk    {_summary(sim)}")
    print(f"          -> {_save(sim, 'b_reorder_disk')}")


def scenario_c(seed: int = 5, kill_path: int = 1) -> None:
    """C, 'path failure': kill a path mid-transfer and watch the flow reroute.

    The kill has to land while packets are still in flight. One RTT here is ~100 us, so a
    kill at 200 us finds the transfer already finished, drops nothing, and produces a log
    identical to the no-kill run -- a demo that demonstrates nothing.
    """
    kill_at = 2e-5
    print(f"Scenario C -- multipath, kill path {kill_path} at t={kill_at:g}")
    for kill in (None, kill_path):
        label = "no-kill" if kill is None else f"kill{kill}"
        sim = run_bulk_multipath(
            seed=seed, n_packets=120, n_paths=3, n_flows=3, until=0.05, kill_path=kill, kill_at=kill_at
        )
        print(f"  {label:<9} {_summary(sim)}")
        print(f"            -> {_save(sim, f'c_{label}')}")
    sim = run_bulk_multipath(
        seed=seed, n_packets=120, n_paths=3, n_flows=3, until=0.05, kill_path=kill_path, kill_at=kill_at
    )
    resp = [e for e in sim.bus.events if e["type"] == "fae_resp"]
    killed = [e for e in sim.bus.events if e["type"] == "pkt_drop" and e.get("reason") == "path_killed"]
    kill_t = next(e["t"] for e in sim.bus.events if e["type"] == "path_kill")
    before = [r for r in resp if r["t"] < kill_t]
    print(f"  packets lost to the kill: {len(killed)}")
    # With an early kill the FAE may never publish a pre-kill assignment, so say so rather
    # than printing the post-kill one under a "before" label.
    print(f"  assignment before kill: {before[-1]['path_for_flow'] if before else 'never published (kill landed first)'}")
    print(f"  assignment after  kill: {resp[-1]['path_for_flow']}")


def scenario_d(seed: int = 1, n_senders: int = 50) -> None:
    """D, 'incast': many senders, one bottleneck; backpressure and fair sharing."""
    print(f"Scenario D -- incast, {n_senders} senders on one bottleneck")
    sim = run_incast(n_senders=n_senders, seed=seed, n_packets=40, until=0.5)
    counts: dict[int, int] = {}
    for ev in sim.bus.events:
        if ev["type"] == "pkt_recv" and ev.get("accepted"):
            counts[ev["conn"]] = counts.get(ev["conn"], 0) + 1
    got = sorted(counts.values())
    print(f"  {_summary(sim)}")
    print(f"  per-sender delivered: min {got[0]}, max {got[-1]}, spread {got[-1] - got[0]}")
    print(f"  -> {_save(sim, 'd_incast')}")


SCENARIOS = {"a": scenario_a, "b": scenario_b, "c": scenario_c, "d": scenario_d}


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(SCENARIOS) if which == "all" else [which]
    for n in names:
        if n not in SCENARIOS:
            raise SystemExit(f"unknown scenario {n!r}; choose from {', '.join(SCENARIOS)} or 'all'")
        SCENARIOS[n]()
        print()


if __name__ == "__main__":
    main()
