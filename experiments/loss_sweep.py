"""Goodput vs drop rate for GBN / SR / Falcon-style (trend of paper Figure 10).

Multi-seed with 95% confidence intervals: the single-seed version of this sweep was
flatter enough to suggest the algorithms were indistinguishable, which they are not.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.common import sweep

from gyrfalcon.harness import run_bulk
from gyrfalcon.metrics import metrics_from_events

SEEDS = [11, 12, 13, 14, 15]


def main() -> None:
    rates = [0.0, 0.002, 0.005, 0.01, 0.02]

    def run(tr: str, loss: float, seed: int) -> float:
        sim = run_bulk(tr, seed=seed, n_packets=250, loss=loss, until=2.0)
        return metrics_from_events(sim.bus.events)["goodput_bps"] / 1e6

    sweep(
        rates,
        ["gbn", "sr", "falcon"],
        run,
        SEEDS,
        x_label="drop rate",
        y_label="goodput (Mbps, sim)",
        title="Falcon-style vs baselines under loss (not paper numbers)",
        filename="loss_sweep.png",
    )

    # One representative run's event log per transport, so a reader can audit the metrics.
    out = ROOT / "results"
    for tr in ("gbn", "sr", "falcon"):
        sim = run_bulk(tr, seed=SEEDS[0], n_packets=250, loss=0.01, until=2.0)
        sim.bus.write_jsonl(out / f"loss_{tr}_0.01.jsonl")
    print("wrote reference event logs")


if __name__ == "__main__":
    main()
