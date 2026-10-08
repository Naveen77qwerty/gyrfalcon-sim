"""Multipath: what a path failure costs the connection.

Compares an unkillable run against the same run with one path dropped mid-flight. The
number that matters is delivered packets: a datapath that cannot be re-pointed at a live
path simply stalls once the killed path is chosen, and the goodput curve says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.common import sweep

from gyrfalcon.harness import run_bulk_multipath
from gyrfalcon.metrics import metrics_from_events

SEEDS = [5, 6, 7, 8, 9]
# Inside the transfer's flight window. One RTT is ~100 us, so a later kill finds the transfer
# already done, drops nothing, and makes every series identical.
KILL_AT = 2e-5


def delivered(flow_id: int) -> float:
    def run(_n: str, kill: float, seed: int) -> float:
        sim = run_bulk_multipath(
            seed=seed,
            n_packets=120,
            n_paths=3,
            n_flows=2,
            loss=0.0,
            until=0.05,
            kill_path=int(kill) if kill >= 0 else None,
            kill_at=KILL_AT,
        )
        return float(metrics_from_events(sim.bus.events)["packets_delivered"])

    return run


def main() -> None:
    sweep(
        [0.0, 1.0, 2.0],
        ["multipath"],
        delivered(0),
        SEEDS,
        "path killed mid-flight (-1 = none)",
        "packets delivered (of 120)",
        "Multipath: delivery after a path failure (not paper numbers)",
        "multipath_recovery.png",
        x_scale=1.0,
        x_suffix="path id",
    )

    # Show the FAE's own timeline for one seed: this is the plot that proves the
    # datapath followed the repath rather than merely surviving.
    sim = run_bulk_multipath(
        seed=5, n_packets=120, n_paths=3, n_flows=2, until=0.05, kill_path=1, kill_at=KILL_AT
    )
    resp = [e for e in sim.bus.events if e["type"] == "fae_resp"]
    kills = [e for e in sim.bus.events if e["type"] == "path_kill"]
    print(f"path_kill events: {len(kills)}")
    print(f"fae_resp: {len(resp)}")
    if resp:
        print(f"first assignment: {resp[0]['path_for_flow']}")
        print(f"final assignment: {resp[-1]['path_for_flow']}")
    print(f"delivered: {metrics_from_events(sim.bus.events)['packets_delivered']} of 120")


if __name__ == "__main__":
    main()
