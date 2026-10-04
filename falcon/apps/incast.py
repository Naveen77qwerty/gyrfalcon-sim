"""Incast: N clients write to one server (paper Figure 13 trend)."""

from __future__ import annotations

from falcon.core.sim import Simulator
from falcon.fae.engine import Fae
from falcon.net.path import Path
from falcon.pdl.reliability import FalconStylePdl


def run_incast(
    n_senders: int = 8,
    seed: int = 1,
    n_packets: int = 40,
    loss: float = 0.0,
    until: float = 1.0,
    algo: str = "swift",
) -> Simulator:
    """N connections share one bottleneck, each with its own FAE-driven congestion window.

    `sim` must reach the Fae: without it the engine never subscribes to the bus and every
    sender silently falls back to a frozen constant policy.
    """
    sim = Simulator(seed=seed)
    bottleneck_bps = 25e9
    for i in range(n_senders):
        fwd = Path(sim, delay=5e-5, loss=loss, bandwidth_bps=bottleneck_bps / n_senders, path_id=i)
        rev = Path(sim, delay=5e-5, loss=0.0, bandwidth_bps=bottleneck_bps, path_id=100 + i)
        fae = Fae(sim=sim, algo=algo, n_flows=1, conn=i + 1, path_ids=[i])
        sess = FalconStylePdl(
            sim,
            fwd,
            rev,
            conn=i + 1,
            n_packets=n_packets,
            policy=fae.policy,
            get_policy=lambda f=fae: f.policy,
        )
        sess.start()
    sim.run(until=until)
    return sim
