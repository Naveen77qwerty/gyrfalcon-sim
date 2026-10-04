"""Impaired path model: delay, jitter, serialization, loss, reorder, kill/slow."""

from __future__ import annotations

from collections.abc import Callable

from falcon.core.sim import Simulator
from falcon.net.packet import Packet


class Path:
    """One-way path. Kill/slow are used in Phase 3-4 chaos and repathing demos."""

    def __init__(
        self,
        sim: Simulator,
        name: str = "fwd",
        delay: float = 0.0001,
        jitter: float = 0.0,
        bandwidth_bps: float = 100e9,
        loss: float = 0.0,
        reorder: float = 0.0,
        reorder_delay: float = 0.0002,
        path_id: int = 0,
    ) -> None:
        self.sim = sim
        self.name = name
        self.delay = delay
        self.jitter = jitter
        self.bandwidth_bps = bandwidth_bps
        self.loss = loss
        self.reorder = reorder
        self.reorder_delay = reorder_delay
        self.path_id = path_id
        self.killed = False
        self.slow_factor = 1.0
        self._deliver: Callable[[Packet], None] | None = None

    def attach(self, deliver: Callable[[Packet], None]) -> None:
        self._deliver = deliver

    def kill(self) -> None:
        self.killed = True
        self.sim.bus.emit("path_kill", conn=0, path=self.path_id)

    def slow(self, factor: float) -> None:
        self.slow_factor = factor
        self.sim.bus.emit("path_slow", conn=0, path=self.path_id, factor=factor)

    def revive(self) -> None:
        self.killed = False
        self.slow_factor = 1.0

    def send(self, pkt: Packet) -> None:
        pkt.path_id = self.path_id
        conn = pkt.conn
        self.sim.bus.emit(
            "pkt_send",
            conn=conn,
            flow=pkt.flow,
            psn=pkt.psn,
            rsn=pkt.rsn,
            retx=pkt.retx,
            size=pkt.size,
            kind=pkt.kind,
            path=self.path_id,
            is_response=pkt.is_response,
        )
        if self.killed:
            self.sim.bus.emit(
                "pkt_drop",
                conn=conn,
                flow=pkt.flow,
                psn=pkt.psn,
                reason="path_killed",
                path=self.path_id,
            )
            return
        ser = (pkt.size * 8) / max(self.bandwidth_bps, 1.0)
        dly = (self.delay + ser) * self.slow_factor
        if self.jitter:
            dly += self.sim.rng.uniform(0.0, self.jitter)
        if self.sim.rng.random() < self.loss:
            self.sim.bus.emit(
                "pkt_drop",
                conn=conn,
                flow=pkt.flow,
                psn=pkt.psn,
                reason="random_loss",
                path=self.path_id,
            )
            return
        extra = 0.0
        if self.sim.rng.random() < self.reorder:
            extra = self.reorder_delay
            self.sim.bus.emit(
                "pkt_reorder",
                conn=conn,
                flow=pkt.flow,
                psn=pkt.psn,
                extra_delay=extra,
                path=self.path_id,
            )
        self.sim.schedule(dly + extra, self._arrive, pkt)

    def _arrive(self, pkt: Packet) -> None:
        if self.killed:
            self.sim.bus.emit(
                "pkt_drop",
                conn=pkt.conn,
                flow=pkt.flow,
                psn=pkt.psn,
                reason="path_killed",
                path=self.path_id,
            )
            return
        if self._deliver:
            self._deliver(pkt)
