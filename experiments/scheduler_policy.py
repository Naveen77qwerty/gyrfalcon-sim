"""Scheduler policy: largest-open-window vs round-robin (trend of paper Figure 17).

Both rules live in `fae` as a name on `Policy`; `pdl` only reads it. That is the point of
the comparison: swapping the scheduler is a one-word change in the policy, not a datapath
edit.
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
SCHEDULERS = ["largest_open", "round_robin"]


def run(sched: str, n_flows: float, seed: int) -> float:
    sim = run_bulk_multipath(
        seed=seed,
        n_packets=150,
        n_paths=3,
        n_flows=int(n_flows),
        loss=0.0,
        reorder=0.15,
        until=0.1,
        scheduler=sched,
    )
    return metrics_from_events(sim.bus.events)["goodput_bps"] / 1e6


def main() -> None:
    sweep(
        [2.0, 4.0, 8.0],
        SCHEDULERS,
        run,
        SEEDS,
        "flows in the connection",
        "goodput (Mbps, sim)",
        "Flow scheduling: largest open window vs round-robin (not paper numbers)",
        "scheduler_policy.png",
        x_scale=1.0,
        x_suffix="flows",
    )


if __name__ == "__main__":
    main()
