"""Host congestion: ncwnd driven by receiver buffer occupancy (trend of paper Figure 14).

`path.slow()` delays packets in flight, so the receiver's buffer fills and the occupancy it
reports on ACKs rises. The FAE turns that into a smaller `ncwnd`, which is the sender's
window ceiling. Recovery is shown by slowing the path back and letting occupancy drain.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from experiments.common import RESULTS

from gyrfalcon.core.sim import Simulator
from gyrfalcon.fae.engine import Fae
from gyrfalcon.fae.policy import Policy
from gyrfalcon.metrics import metrics_from_events
from gyrfalcon.net.path import Path as NetPath
from gyrfalcon.pdl.reliability import FalconStylePdl

SEEDS = [1, 2, 3, 4, 5]


def run_once(seed: int, slow_factor: float = 12.0, until: float = 0.02):
    sim = Simulator(seed=seed)
    fwd = NetPath(sim, delay=5e-5, loss=0.0, bandwidth_bps=100e9, path_id=0)
    rev = NetPath(sim, delay=5e-5, loss=0.0, bandwidth_bps=100e9, path_id=1)
    fae = Fae(sim=sim, n_flows=1, conn=1, path_ids=[0])
    sess = FalconStylePdl(sim, fwd, rev, conn=1, n_packets=400, policy=fae.policy, get_policy=lambda: fae.policy)
    sim.schedule(2e-4, lambda: fwd.slow(slow_factor))
    sim.schedule(8e-3, lambda: fwd.unslow())
    sess.start()
    sim.run(until=until)
    return sim, fae


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    fig, (ax_ncwnd, ax_occ) = plt.subplots(2, 1, sharex=True, figsize=(8, 6))
    for seed in SEEDS:
        sim, _fae = run_once(seed)
        resp = [e for e in sim.bus.events if e["type"] == "fae_resp"]
        evts = [e for e in sim.bus.events if e["type"] == "fae_event"]
        if resp:
            ax_ncwnd.plot([r["t"] for r in resp], [r["ncwnd"] for r in resp], alpha=0.6, label=f"seed {seed}")
        if evts:
            ax_occ.plot([e["t"] for e in evts], [e.get("buffer_occ") or 0.0 for e in evts], alpha=0.6)

    ax_ncwnd.set_ylabel("ncwnd (from FAE)")
    ax_ncwnd.set_title("Slow receiver: ncwnd falls with buffer occupancy, then recovers (not paper numbers)")
    ax_ncwnd.legend(fontsize="small")
    ax_occ.set_ylabel("rx buffer occupancy")
    ax_occ.set_xlabel("simulated time (s)")
    fig.tight_layout()
    path = RESULTS / "host_congestion.png"
    fig.savefig(path)
    print("wrote", path)

    sim, _fae = run_once(SEEDS[0])
    print("delivered:", metrics_from_events(sim.bus.events)["packets_delivered"], "of 400")


if __name__ == "__main__":
    main()
