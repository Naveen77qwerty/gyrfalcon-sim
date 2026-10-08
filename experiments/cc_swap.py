"""CC swap: the same scenario under Swift-style delay CC and plain AIMD (paper 4.2).

The structural claim is the real one -- `pdl/` is unchanged and only the algorithm named at
construction differs.

The numeric result is reported as it comes out. Two things are worth knowing before reading
the numbers. First, on an unimpaired path both controllers only ever take the
additive-increase branch and end up identical, so this scenario slows the path to give them
something to react to. Second, the slowdown has to stay mild: at 40x both windows collapse to
the floor of 1 and become indistinguishable again, just at the other extreme.

AIMD wins this particular race. That is not a bug and not hidden -- with a constant propagation
delay and no queueing fabric there is nothing for delay-based control to exploit, and a harsher
multiplicative decrease costs it throughput. It is reported because the swap demonstrably
changes behaviour, which is what pluggability has to mean.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.common import sweep

from gyrfalcon.core.sim import Simulator
from gyrfalcon.fae.engine import Fae
from gyrfalcon.metrics import metrics_from_events
from gyrfalcon.net.path import Path as NetPath
from gyrfalcon.pdl.reliability import FalconStylePdl

SEEDS = [3, 4, 5, 6, 7]
ALGOS = ["swift", "aimd"]
SLOW_AT = 2e-4
SLOW_FACTOR = 2.0


def run_once(algo: str, seed: int) -> Simulator:
    sim = Simulator(seed=seed)
    fwd = NetPath(sim, delay=5e-5, loss=0.002, reorder=0.1, bandwidth_bps=100e9, path_id=0)
    rev = NetPath(sim, delay=5e-5, loss=0.0, bandwidth_bps=100e9, path_id=1)
    fae = Fae(sim=sim, algo=algo, n_flows=1, conn=1, path_ids=[0])
    sess = FalconStylePdl(
        sim, fwd, rev, conn=1, n_packets=400, policy=fae.policy, get_policy=lambda: fae.policy
    )
    sim.schedule(SLOW_AT, lambda: fwd.slow(SLOW_FACTOR))
    sess.start()
    sim.run(until=0.02)
    return sim


def measure(algo: str, seed: int) -> tuple[float, float, float]:
    """(mean window, goodput Mbps, packets delivered) over one seed."""
    sim = run_once(algo, seed)
    windows = [e["fcwnd"]["0"] for e in sim.bus.events if e["type"] == "fae_resp"]
    m = metrics_from_events(sim.bus.events)
    return (statistics.fmean(windows) if windows else 0.0, m["goodput_bps"] / 1e6, float(m["packets_delivered"]))


def main() -> None:
    def mean_window(algo: str, _x: float, seed: int) -> float:
        return measure(algo, seed)[0]

    sweep(
        [0.0],
        ALGOS,
        mean_window,
        SEEDS,
        "scenario",
        "mean fcwnd",
        f"CC swap under a 2x path slowdown (not paper numbers)",
        "cc_swap_window.png",
        x_scale=1.0,
        x_suffix="",
    )

    print(f"\nsame scenario, same seeds, same pdl/, path slowed {SLOW_FACTOR:g}x at t={SLOW_AT:g}:")
    summary = {}
    for algo in ALGOS:
        rows = [measure(algo, s) for s in SEEDS]
        summary[algo] = tuple(statistics.fmean(r[i] for r in rows) for i in range(3))
        print(f"  {algo:<6} mean fcwnd {summary[algo][0]:6.2f}   "
              f"goodput {summary[algo][1]:7.1f} Mbps   delivered {summary[algo][2]:5.1f}/400")

    if summary["swift"] == summary["aimd"]:
        print("  WARNING: the swap had no effect. Either the FAE is not receiving events, or")
        print("           the scenario never made delay rise or overshot so hard that both")
        print("           windows saturated at the same floor.")


if __name__ == "__main__":
    main()
