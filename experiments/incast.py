"""Incast fairness: per-sender goodput spread as the fan-in grows (trend of paper Figure 13).

The question is not which sender is fastest but how far apart they end up. A protocol
that lets one connection capture the bottleneck looks excellent on a mean-goodput plot and
fails this one, so the y axis is the coefficient of variation across senders.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.common import sweep

from falcon.apps.incast import run_incast

SEEDS = [1, 2, 3, 4, 5]
FANINS = [2, 4, 8, 16, 32]


def per_sender_goodput(sim, n_senders: int) -> list[float]:
    """Bytes each connection delivered, in packets, in connection order."""
    counts: dict[int, int] = {c: 0 for c in range(1, n_senders + 1)}
    for ev in sim.bus.events:
        if ev["type"] == "pkt_recv" and ev.get("accepted") and ev["kind"] == "data":
            if ev["conn"] in counts:
                counts[ev["conn"]] += 1
    return [counts[c] for c in range(1, n_senders + 1)]


def fairness_cv(counts: list[float]) -> float:
    """Coefficient of variation: std / mean. 0 is perfectly fair."""
    mean = statistics.fmean(counts)
    return statistics.stdev(counts) / mean if mean else float("nan")


def main() -> None:
    def run_cv(_algo: str, fanin: float, seed: int) -> float:
        sim = run_incast(n_senders=int(fanin), seed=seed, n_packets=60, until=4.0)
        return fairness_cv(per_sender_goodput(sim, int(fanin)))

    def run_min(_algo: str, fanin: float, seed: int) -> float:
        sim = run_incast(n_senders=int(fanin), seed=seed, n_packets=60, until=4.0)
        return min(per_sender_goodput(sim, int(fanin)))

    sweep(
        [float(f) for f in FANINS],
        ["swift"],
        run_cv,
        SEEDS,
        "senders sharing the bottleneck",
        "goodput coefficient of variation",
        "Incast fairness across connections (lower is fairer, not paper numbers)",
        "incast_fairness.png",
        x_scale=1.0,
        x_suffix="connections",
    )
    sweep(
        [float(f) for f in FANINS],
        ["swift"],
        run_min,
        SEEDS,
        "senders sharing the bottleneck",
        "packets delivered by the unluckiest sender",
        "Incast: worst-sender progress (not paper numbers)",
        "incast_min_sender.png",
        x_scale=1.0,
        x_suffix="connections",
    )


if __name__ == "__main__":
    main()
