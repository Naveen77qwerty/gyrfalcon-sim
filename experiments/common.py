"""Shared experiment plumbing: multi-seed averaging and honest error bars.

Every sweep in this directory runs each configuration over several seeds. A single-seed
sweep is not a result -- it is one sample, and for the Falcon-style datapath the seed
decides which packets happen to collide.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Callable, Iterable

RESULTS = Path(__file__).resolve().parents[1] / "results"

# Confidence multiplier for n samples, from the t table at 95%.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}


def summarise(samples: Iterable[float]) -> tuple[float, float]:
    """Return (mean, half-width of the 95% confidence interval).

    With few seeds the t multiplier matters a lot; using 1.96 would understate the
    interval by more than a factor of two at n=3 and make noisy runs look conclusive.
    """
    xs = sorted(samples)
    n = len(xs)
    if n == 0:
        return (float("nan"), float("nan"))
    mean = statistics.fmean(xs)
    if n == 1:
        return (mean, 0.0)
    sd = statistics.stdev(xs)
    half = _T95.get(n, 1.96) * sd / math.sqrt(n)
    return (mean, half)


def sweep(
    x_values: list[float],
    series: list[str],
    runner: Callable[[str, float, int], float],
    seeds: list[int],
    x_label: str,
    y_label: str,
    title: str,
    filename: str,
    x_scale: float = 100.0,
    x_suffix: str = "%",
) -> dict[str, list[tuple[float, float]]]:
    """Run `runner(series_name, x_value, seed) -> scalar` for every x and seed, then plot.

    Returns the aggregated (mean, half-width) per series so callers can reuse the numbers
    without re-running the simulation.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    RESULTS.mkdir(exist_ok=True)
    xs = [v * x_scale for v in x_values]
    agg: dict[str, list[tuple[float, float]]] = {
        name: [summarise(runner(name, x, s) for s in seeds) for x in x_values] for name in series
    }

    fig, ax = plt.subplots()
    for name, pts in agg.items():
        ax.errorbar(
            xs,
            [p[0] for p in pts],
            yerr=[p[1] for p in pts],
            marker="o",
            capsize=3,
            label=name,
        )
    ax.set_xlabel(f"{x_label} ({x_suffix})")
    ax.set_ylabel(y_label)
    ax.set_title(title)
    ax.legend()
    fig.tight_layout()
    path = RESULTS / filename
    fig.savefig(path)
    print("wrote", path)
    return agg
