"""Goodput and spurious retransmissions vs reorder rate (trend of paper Figure 11a).

Reordering is where the baselines are supposed to fall apart, so both goodput and the
spurious-retransmission count are plotted: a protocol can hold its goodput steady while
burning bandwidth on packets that were never actually lost.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.common import sweep

from falcon.harness import run_bulk
from falcon.metrics import metrics_from_events

SEEDS = [13, 14, 15, 16, 17]
RATES = [0.0, 0.1, 0.2, 0.3, 0.4]
TITLE = "Falcon-style vs baselines under reordering (not paper numbers)"


def main() -> None:
    def run(tr: str, reorder: float, seed: int) -> float:
        sim = run_bulk(tr, seed=seed, n_packets=200, reorder=reorder, loss=0.0, until=2.0)
        return metrics_from_events(sim.bus.events)["goodput_bps"] / 1e6

    def run_spurious(tr: str, reorder: float, seed: int) -> float:
        sim = run_bulk(tr, seed=seed, n_packets=200, reorder=reorder, loss=0.0, until=2.0)
        return float(metrics_from_events(sim.bus.events)["spurious_retransmissions"])

    sweep(RATES, ["gbn", "sr", "falcon"], run, SEEDS, "reorder rate", "goodput (Mbps, sim)", TITLE, "reorder_sweep.png")
    sweep(
        RATES,
        ["gbn", "sr", "falcon"],
        run_spurious,
        SEEDS,
        "reorder rate",
        "spurious retransmissions",
        TITLE,
        "reorder_spurious.png",
    )


if __name__ == "__main__":
    main()
