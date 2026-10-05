"""Shared bulk-transfer runner for GBN, SR, and Falcon-style PDL.

Split into `build_*` (construct a session, schedule its first events, do not run) and
`run_*` (build, then run to completion). Everything offline uses `run_*`; the live dashboard
uses `build_*` so it can advance the simulation in slices and ship events between them. Both
paths go through the same construction code, so a live log and an experiment log for the same
seed are the same run.
"""

from __future__ import annotations

from falcon.baselines.go_back_n import GoBackN
from falcon.baselines.selective_repeat import SelectiveRepeat
from falcon.core.sim import Simulator
from falcon.fae.engine import Fae
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


def build_bulk(
    transport: str,
    seed: int = 1,
    n_packets: int = 200,
    loss: float = 0.0,
    reorder: float = 0.0,
    policy: Policy | None = None,
):
    """Construct a single-path session and arm it. Does not run.

    Returns `(sim, session, forwards, rev)`. `forwards` is returned rather than discovered by
    walking attributes so a caller that wants to retune impairment mid-run has a real handle
    on the path objects.
    """
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
    return sim, sess, [fwd], rev


def run_bulk(
    transport: str,
    seed: int = 1,
    n_packets: int = 200,
    loss: float = 0.0,
    reorder: float = 0.0,
    until: float = 1.0,
    policy: Policy | None = None,
) -> Simulator:
    sim, _sess, _forwards, _rev = build_bulk(
        transport, seed=seed, n_packets=n_packets, loss=loss, reorder=reorder, policy=policy
    )
    sim.run(until=until)
    return sim


def build_bulk_multipath(
    seed: int = 1,
    n_packets: int = 200,
    n_paths: int = 2,
    n_flows: int = 2,
    loss: float = 0.0,
    reorder: float = 0.0,
    algo: str = "swift",
    scheduler: str = "largest_open",
    kill_path: int | None = None,
    kill_at: float = 0.001,
    start: bool = True,
):
    """Construct a multipath Falcon-style session. Does not run.

    The PDL is handed a callable that maps flow -> Path, so it never learns the FAE exists;
    when the FAE repaths its flows mid-run the sender picks the new path up on its own.

    Returns `(sim, session, forwards, rev)`.
    """
    sim = Simulator(seed=seed)
    path_ids = list(range(n_paths))
    forwards = [
        Path(sim, name=f"fwd{pid}", delay=5e-5, loss=loss, reorder=reorder,
             bandwidth_bps=100e9 / n_paths, path_id=pid)
        for pid in path_ids
    ]
    rev = Path(sim, name="rev", delay=5e-5, loss=0.0, bandwidth_bps=100e9, path_id=100)
    fae = Fae(sim=sim, algo=algo, n_flows=n_flows, conn=1, path_ids=path_ids)
    fae.policy.scheduler = scheduler

    sess = FalconStylePdl(
        sim,
        forwards[0],
        rev,
        conn=1,
        n_packets=n_packets,
        policy=fae.policy,
        get_policy=lambda: fae.policy,
        get_path=lambda flow: forwards[fae.policy.path_for_flow.get(flow, 0)],
    )
    # The receiver listens on every forward path; only the path the sender picks is used.
    for p in forwards:
        p.attach(sess.receiver.receive_data)
    rev.attach(sess.sender.on_ack)

    if kill_path is not None:
        sim.schedule(kill_at, forwards[kill_path].kill)
    if start:
        sess.start()
    return sim, sess, forwards, rev


def run_bulk_multipath(
    seed: int = 1,
    n_packets: int = 200,
    n_paths: int = 2,
    n_flows: int = 2,
    loss: float = 0.0,
    reorder: float = 0.0,
    until: float = 1.0,
    algo: str = "swift",
    kill_path: int | None = None,
    kill_at: float = 0.001,
    scheduler: str = "largest_open",
) -> Simulator:
    """Falcon-style over several parallel paths with a live FAE driving every parameter.

    `kill_path` drops that path mid-run; the FAE sees the `path_kill` event, repaths its
    flows, and this harness resolves the new assignment through `get_path`. The PDL never
    learns that the FAE exists -- it is handed a callable that maps flow -> Path.
    """
    sim, _sess, _forwards, _rev = build_bulk_multipath(
        seed=seed, n_packets=n_packets, n_paths=n_paths, n_flows=n_flows, loss=loss,
        reorder=reorder, algo=algo, kill_path=kill_path, kill_at=kill_at,
        scheduler=scheduler,
    )
    sim.run(until=until)
    return sim
