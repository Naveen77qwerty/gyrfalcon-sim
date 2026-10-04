"""Shared bulk-transfer runner for GBN, SR, and Falcon-style PDL."""

from __future__ import annotations

from falcon.baselines.go_back_n import GoBackN
from falcon.baselines.selective_repeat import SelectiveRepeat
from falcon.core.sim import Simulator
from falcon.fae.policy import Policy
from falcon.net.path import Path
from falcon.pdl.reliability import FalconStylePdl

TRANSPORTS = ("gbn", "sr", "falcon")


def make_paths(
    sim: Simulator,
    loss: float = 0.0,
    reorder: float = 0.0,
    delay: float = 5e-5,
    jitter: float = 0.0,
    reorder_delay: float = 2e-4,
    bandwidth_bps: float = 100e9,
) -> tuple[Path, Path]:
    fwd = Path(
        sim,
        name="fwd",
        delay=delay,
        jitter=jitter,
        bandwidth_bps=bandwidth_bps,
        loss=loss,
        reorder=reorder,
        reorder_delay=reorder_delay,
        path_id=0,
    )
    rev = Path(
        sim,
        name="rev",
        delay=delay,
        jitter=jitter,
        bandwidth_bps=bandwidth_bps,
        loss=0.0,
        reorder=0.0,
        path_id=1,
    )
    return fwd, rev


def run_bulk(
    transport: str,
    seed: int = 1,
    n_packets: int = 200,
    loss: float = 0.0,
    reorder: float = 0.0,
    until: float = 1.0,
    policy: Policy | None = None,
) -> Simulator:
    sim = Simulator(seed=seed)
    fwd, rev = make_paths(sim, loss=loss, reorder=reorder)
    pol = policy or Policy()
    if transport == "gbn":
        sess = GoBackN(sim, fwd, rev, n_packets=n_packets, policy=pol)
    elif transport == "sr":
        sess = SelectiveRepeat(sim, fwd, rev, n_packets=n_packets, policy=pol)
    elif transport == "falcon":
        sess = FalconStylePdl(sim, fwd, rev, n_packets=n_packets, policy=pol)
    else:
        raise ValueError(transport)
    sess.start()
    sim.run(until=until)
    return sim
