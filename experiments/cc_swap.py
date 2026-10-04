"""CC swap: the same scenario under Swift-style delay CC and plain AIMD (paper 4.2).

The claim being tested is structural, not numeric: `pdl/` is byte-for-byte the same in
both runs, and only the algorithm named at construction changes. The plot is a secondary
check that the swap actually altered behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.common import sweep

from falcon.harness import run_bulk_multipath
from falcon.metrics import metrics_from_events

SEEDS = [3, 4, 5, 6, 7]
ALGOS = ["swift", "aimd"]


def main() -> None:
    deltas = {}
    for algo in ALGOS:
        # Per-flow window trajectory is where the two algorithms actually differ.
        def traj(_name: str, _x: float, seed: int, algo: str = algo) -> float:
            sim = run_bulk_multipath(seed=seed, n_packets=300, n_paths=2, n_flows=2, until=0.1, algo=algo)
            resp = [e for e in sim.bus.events if e["type"] == "fae_resp"]
            return resp[-1]["fcwnd"]["0"] if resp else 0.0

        deltas[algo] = sweep(
            [0.0],
            [algo],
            traj,
            SEEDS,
            "scenario",
            "final fcwnd per flow",
            "CC swap: final per-flow window (not paper numbers)",
            f"cc_swap_{algo}.png",
            x_scale=1.0,
            x_suffix="",
        )[algo][0][0]

    print("\nsame scenario, same seeds, same pdl/:")
    for algo, w in deltas.items():
        print(f"  {algo:<6} mean final fcwnd = {w:.1f}")
    if deltas["swift"] == deltas["aimd"]:
        print("  WARNING: the swap had no effect; check the FAE is receiving events")


if __name__ == "__main__":
    main()
