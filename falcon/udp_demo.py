"""Minimal UDP backend demo harness using the same protocol state machines."""
from __future__ import annotations

from falcon.core.sim import Simulator
from falcon.fae.policy import Policy
from falcon.net.udp_path import UdpPath
from falcon.pdl.reliability import FalconStylePdl


def run_udp_falcon(
    local_addr: tuple[str, int],
    remote_addr: tuple[str, int],
    n_packets: int = 10,
    seed: int = 1,
    until: float = 0.1,
):
    sim = Simulator(seed=seed)
    fwd = UdpPath(sim, local_addr=local_addr, remote_addr=remote_addr, path_id=0)
    rev = UdpPath(sim, local_addr=remote_addr, remote_addr=local_addr, path_id=1)
    sess = FalconStylePdl(
        sim,
        fwd,
        rev,
        conn=1,
        n_packets=n_packets,
        policy=Policy(),
    )
    sess.start()
    sim.run(until=until)
    return sim, sess, fwd, rev
