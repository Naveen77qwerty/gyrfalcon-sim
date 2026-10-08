"""Falcon Adaptive Engine: events in, Policy out. No references to PDL internals (Table 3)."""

from __future__ import annotations

from gyrfalcon.core.sim import Simulator
from gyrfalcon.fae.policy import Policy


class CongestionControl:
    def on_sample(self, policy: Policy, fabric_delay: float | None, buffer_occ: float, now: float) -> None:
        raise NotImplementedError


class SwiftCc(CongestionControl):
    """Swift-style delay CC: AI below target, MD on overshoot, at most once per RTT (section 4.2)."""

    def __init__(self, target: float = 1.0e-4) -> None:
        self.target = target
        self._last_md = -1.0

    def on_sample(self, policy: Policy, fabric_delay: float | None, buffer_occ: float, now: float) -> None:
        rtt = fabric_delay if fabric_delay is not None else self.target
        for fid in list(policy.fcwnd):
            w = policy.fcwnd[fid]
            if fabric_delay is None or fabric_delay <= self.target:
                policy.fcwnd[fid] = min(256.0, w + 1.0 / max(w, 1.0))
            else:
                if now - self._last_md >= max(rtt, 1e-6):
                    overshoot = (fabric_delay - self.target) / self.target
                    policy.fcwnd[fid] = max(1.0, w * (1.0 - min(0.5, 0.2 * overshoot)))
                    self._last_md = now
        policy.rack_rto = max(1.2e-4, 1.25 * (rtt if rtt else self.target))
        policy.tlp_idle = 2.0 * policy.rack_rto
        policy.rto = 4.0 * policy.rack_rto
        policy.ncwnd = max(4.0, 64.0 * (1.0 - min(1.0, buffer_occ)))
        policy.alpha = max(0.25, 1.0 + (sum(policy.fcwnd.values()) / 64.0) - buffer_occ)
        policy.pacing_gap = 0.0


class AimdCc(CongestionControl):
    def __init__(self, high_delay: float = 2.0e-4) -> None:
        self.high_delay = high_delay
        self._last_md = -1.0

    def on_sample(self, policy: Policy, fabric_delay: float | None, buffer_occ: float, now: float) -> None:
        rtt = fabric_delay if fabric_delay is not None else 1e-4
        for fid in list(policy.fcwnd):
            w = policy.fcwnd[fid]
            if fabric_delay is not None and fabric_delay > self.high_delay:
                if now - self._last_md >= max(rtt, 1e-6):
                    policy.fcwnd[fid] = max(1.0, w * 0.5)
                    self._last_md = now
            else:
                policy.fcwnd[fid] = min(256.0, w + 1.0 / max(w, 1.0))
        policy.ncwnd = max(4.0, 64.0 * (1.0 - min(1.0, buffer_occ)))
        policy.rack_rto = max(1.2e-4, 1.25 * rtt)
        policy.tlp_idle = 2.0 * policy.rack_rto
        policy.pacing_gap = 0.0


class Fae:
    """Event-in / Policy-out engine for one connection (paper Table 3).

    Subscribes to the event bus rather than being called by the PDL, so the datapath never
    references this module. Filtered by `conn` because a simulator may host many engines
    and every `fae_event` lands on every one of them.
    """

    def __init__(
        self,
        sim: Simulator | None = None,
        algo: str = "swift",
        n_flows: int = 1,
        conn: int = 0,
        path_ids: list[int] | None = None,
        fcwnd_init: float = 16.0,
    ) -> None:
        self.sim = sim
        self.conn = conn
        self.algo = algo
        self.path_ids = list(range(n_flows)) if path_ids is None else list(path_ids)
        self.policy = Policy()
        self.policy.fcwnd = {i: fcwnd_init for i in range(n_flows)}
        # Flows outnumber paths all the time (a connection has more flows than the fabric
        # has links), so paths are shared round-robin rather than one-per-flow.
        self.policy.path_for_flow = {i: self.path_ids[i % len(self.path_ids)] for i in range(n_flows)}
        self.cc: CongestionControl = SwiftCc() if algo == "swift" else AimdCc()
        self._last_resp: tuple | None = None
        if sim is not None:
            sim.bus.add_sink(self._on_bus)

    def _on_bus(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "fae_event":
            if ev.get("conn") == self.conn:
                self.on_event(ev)
        elif etype == "path_kill":
            # Not connection-scoped: any engine with a flow on the dead path must move it.
            self.repath(int(ev.get("path", 0)))

    def repath(self, dead: int) -> bool:
        """Move every flow off `dead` and re-spread them across the live paths.

        Dumping the displaced flows onto a single alternative is legal but concentrates the
        load on one path, which is the outage turning into a congestion collapse. Spreading
        them keeps per-path load roughly where it was before the failure.
        """
        if dead not in self.path_ids:
            return False
        alternatives = [p for p in self.path_ids if p != dead]
        if not alternatives:
            return False
        affected = [fid for fid, pid in self.policy.path_for_flow.items() if pid == dead]
        if not affected:
            return False
        for n, fid in enumerate(affected):
            self.policy.path_for_flow[fid] = alternatives[n % len(alternatives)]
        self._emit_resp()
        return True

    def on_event(self, ev: dict) -> Policy:
        self.cc.on_sample(
            self.policy,
            ev.get("fabric_delay"),
            float(ev.get("buffer_occ") or 0.0),
            float(ev.get("t") or (self.sim.now() if self.sim else 0.0)),
        )
        self._emit_resp()
        return self.policy

    def _snapshot(self) -> tuple:
        p = self.policy
        return (
            tuple(sorted(p.fcwnd.items())),
            p.ncwnd,
            p.pacing_gap,
            p.rack_rto,
            p.tlp_idle,
            p.rto,
            p.alpha,
            tuple(sorted(p.path_for_flow.items())),
        )

    def _emit_resp(self) -> None:
        """Emit only on change, so the log holds a parameter timeline instead of one
        record per ACK. `force` bypasses the check for tests that need every response."""
        if self.sim is None:
            return
        snap = self._snapshot()
        if snap == self._last_resp:
            return
        self._last_resp = snap
        self.sim.bus.emit(
            "fae_resp",
            conn=self.conn,
            fcwnd={str(k): v for k, v in self.policy.fcwnd.items()},
            ncwnd=self.policy.ncwnd,
            pacing_gap=self.policy.pacing_gap,
            rack_rto=self.policy.rack_rto,
            tlp_idle=self.policy.tlp_idle,
            alpha=self.policy.alpha,
            path_for_flow=self.policy.path_for_flow,
        )
