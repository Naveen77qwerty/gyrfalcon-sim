"""Falcon-style packet delivery layer: PSN, 128-bit Rx bitmap, RACK-TLP (section 4.1).

Timeouts and windows are injected by Policy (FAE). This module does not choose them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gyrfalcon.core.sim import Simulator
from gyrfalcon.fae.policy import Policy
from gyrfalcon.net.packet import Packet
from gyrfalcon.net.path import Path
from gyrfalcon.pdl.bitmap import BITMAP_BITS, get_bit, set_bit


@dataclass
class Outstanding:
    psn: int
    flow: int
    xmit_ts: float
    payload: bytes
    size: int
    rsn: int | None
    is_response: bool
    acked: bool = False
    in_flight: bool = True


class PdlReceiver:
    """Accepts OOO packets, tracks a 128-bit Rx window, piggybacks bitmap on ACKs."""

    def __init__(self, sim: Simulator, ack_path: Path, conn: int, is_response: bool = False) -> None:
        self.sim = sim
        self.ack_path = ack_path
        self.conn = conn
        self.is_response = is_response
        self.rcv_nxt = 0
        self.bitmap = 0
        self.buffered: dict[int, Packet] = {}
        self.rx_occupancy = 0.0
        self.delivered_in_order: list[Packet] = []
        self.on_in_order: list[Callable[[Packet], None]] = []

    def receive_data(self, pkt: Packet) -> None:
        # `dup` marks a PSN the receiver already delivered; `accepted` marks one it took
        # into its Rx buffer. Both matter: metrics must not count either as new data.
        dup = pkt.psn < self.rcv_nxt
        ooo = pkt.psn != self.rcv_nxt
        off = pkt.psn - self.rcv_nxt
        in_window = not dup and off < BITMAP_BITS
        already = dup or pkt.psn in self.buffered
        accepted = in_window and not already
        self.sim.bus.emit(
            "pkt_recv",
            conn=self.conn,
            flow=pkt.flow,
            psn=pkt.psn,
            ooo=ooo,
            kind=pkt.kind,
            dup=dup or already,
            accepted=accepted,
            is_response=self.is_response,
        )
        if dup:
            # Re-ACK anyway: this is what lets RACK/TLP recover without a full RTO.
            self._send_ack(pkt)
            return
        if not in_window:
            self._send_ack(pkt)
            return
        if pkt.psn not in self.buffered:
            self.buffered[pkt.psn] = pkt
            self.bitmap = set_bit(self.bitmap, off)
            self.rx_occupancy = min(1.0, len(self.buffered) / 64.0)
        while self.rcv_nxt in self.buffered:
            ordered = self.buffered.pop(self.rcv_nxt)
            self.rcv_nxt += 1
            self.bitmap >>= 1
            self.delivered_in_order.append(ordered)
            for cb in self.on_in_order:
                cb(ordered)
        self.rx_occupancy = min(1.0, len(self.buffered) / 64.0)
        self._send_ack(pkt)

    def _send_ack(self, data: Packet) -> None:
        now = self.sim.now()
        ack = Packet(
            conn=self.conn,
            psn=data.psn,
            kind="ack",
            flow=data.flow,
            size=64,
            base_psn=self.rcv_nxt,
            bitmap=self.bitmap,
            rx_buf=self.rx_occupancy,
            t1=data.t1,
            t2=now,
            t3=now,
            xmit_ts=data.orig_xmit_ts if data.orig_xmit_ts is not None else data.t1,
            is_response=self.is_response,
        )
        self.sim.bus.emit(
            "ack_send",
            conn=self.conn,
            flow=data.flow,
            base_psn=self.rcv_nxt,
            bitmap=format(self.bitmap, "#0130b"),
            rx_buf=self.rx_occupancy,
            t1=ack.t1,
            t2=ack.t2,
            t3=ack.t3,
        )
        self.ack_path.send(ack)


class PdlSender:
    """RACK-TLP sender. Ignores packets marked received; ignores those sent after xmit_ts."""

    def __init__(
        self,
        sim: Simulator,
        data_path: Path,
        conn: int,
        n_packets: int | None = None,
        mtu: int = 1500,
        policy: Policy | None = None,
        is_response: bool = False,
        get_policy: Callable[[], Policy] | None = None,
        get_path: Callable[[int], Path] | None = None,
    ) -> None:
        self.sim = sim
        self.data_path = data_path
        self.conn = conn
        self.n_packets = n_packets
        self.mtu = mtu
        self._policy = policy or Policy()
        self.get_policy = get_policy
        self.get_path = get_path
        self.is_response = is_response
        self.snd_una = 0
        self.snd_nxt = 0
        self.outstanding: dict[int, Outstanding] = {}
        self.app_queue: list[tuple[bytes, int | None, int, dict]] = []
        self.finished = False
        self._tlp_gen = 0
        self._last_tlp_psn: int | None = None
        self.unacked_per_flow: dict[int, int] = {}
        self._last_flow = 0
        self.on_acked: list[Callable] = []

    @property
    def policy(self) -> Policy:
        if self.get_policy:
            return self.get_policy()
        return self._policy

    def enqueue(self, payload: bytes, rsn: int | None = None, flow: int = 0, extra: dict | None = None) -> None:
        self.app_queue.append((payload, rsn, flow, extra or {}))
        self.try_send()

    def start_bulk(self, n: int) -> None:
        self.n_packets = n
        self.try_send()

    def try_send(self) -> None:
        pol = self.policy
        agg_fcwnd = sum(pol.fcwnd.values()) if pol.fcwnd else float(pol.send_window)
        conn_win = int(min(pol.ncwnd, agg_fcwnd, pol.send_window))
        if self.is_response:
            conn_win = int(max(pol.send_window, agg_fcwnd))
        in_flight = sum(1 for o in self.outstanding.values() if not o.acked)
        while in_flight < max(conn_win, 1):
            if self.n_packets is not None:
                if self.snd_nxt >= self.n_packets:
                    break
                payload, rsn, flow, extra = b"x" * self.mtu, None, self._pick_flow(), {}
            else:
                if not self.app_queue:
                    break
                payload, rsn, flow, extra = self.app_queue.pop(0)
                flow = self._pick_flow() if flow == 0 else flow
            self._transmit(self.snd_nxt, payload, rsn, flow, retx=False, extra=extra)
            self.snd_nxt += 1
            in_flight += 1
        self._arm_tlp()

    def _pick_flow(self) -> int:
        """Flow-level scheduling (paper 4.3): pick by the policy's rule, not a hardcoded one.

        Largest open window is the default -- `fcwnd_flow - unacked_flow` -- which sends on
        whichever flow has the most room. Round-robin is the comparison case. The rule name
        comes from `Policy` in `fae/`, so swapping schedulers does not touch this file.
        """
        pol = self.policy
        if not pol.fcwnd:
            return 0
        if pol.scheduler == "round_robin":
            return self._rr_flow()
        best = 0
        best_open = -1.0
        for fid, fcwnd in pol.fcwnd.items():
            unacked = self.unacked_per_flow.get(fid, 0)
            openw = fcwnd - unacked
            if openw > best_open:
                best_open = openw
                best = fid
        return best

    def _rr_flow(self) -> int:
        """Next flow in id order after the last one used."""
        flow_ids = sorted(self.policy.fcwnd)
        if not flow_ids:
            return 0
        last = self._last_flow
        self._last_flow = flow_ids[0] if last == flow_ids[-1] else flow_ids[flow_ids.index(last) + 1]
        return self._last_flow

    def _path_for(self, flow: int) -> Path:
        if self.get_path:
            return self.get_path(flow)
        return self.data_path

    def _transmit(
        self,
        psn: int,
        payload: bytes,
        rsn: int | None,
        flow: int,
        retx: bool,
        probe: bool = False,
        extra: dict | None = None,
    ) -> None:
        now = self.sim.now()
        pkt = Packet(
            conn=self.conn,
            psn=psn,
            kind="probe" if probe else "data",
            flow=flow,
            rsn=rsn,
            retx=retx,
            size=max(len(payload), 64) if payload else self.mtu,
            payload=payload,
            t1=now,
            orig_xmit_ts=now,
            is_response=self.is_response,
            extra=dict(extra or {}),
        )
        if psn in self.outstanding:
            prev = self.outstanding[psn]
            if prev.flow != flow:
                self.unacked_per_flow[prev.flow] = max(0, self.unacked_per_flow.get(prev.flow, 0) - 1)
                self.unacked_per_flow[flow] = self.unacked_per_flow.get(flow, 0) + 1
            prev.xmit_ts = now
            prev.flow = flow
            prev.in_flight = True
        else:
            self.outstanding[psn] = Outstanding(
                psn=psn,
                flow=flow,
                xmit_ts=now,
                payload=payload,
                size=pkt.size,
                rsn=rsn,
                is_response=self.is_response,
            )
            self.unacked_per_flow[flow] = self.unacked_per_flow.get(flow, 0) + 1
        self._path_for(flow).send(pkt)

    def on_ack(self, pkt: Packet) -> None:
        now = self.sim.now()
        pkt.t4 = now
        fabric = None
        if pkt.t1 is not None and pkt.t2 is not None and pkt.t3 is not None:
            fabric = (pkt.t4 - pkt.t1) - (pkt.t3 - pkt.t2)
        self.sim.bus.emit(
            "ack_recv",
            conn=self.conn,
            flow=pkt.flow,
            base_psn=pkt.base_psn,
            bitmap=pkt.bitmap,
        )
        self.sim.bus.emit(
            "fae_event",
            conn=self.conn,
            buffer_occ=pkt.rx_buf,
            acked=max(0, pkt.base_psn - self.snd_una),
            t1=pkt.t1,
            t2=pkt.t2,
            t3=pkt.t3,
            t4=pkt.t4,
            fabric_delay=fabric,
        )
        newly: list[int] = []
        if pkt.base_psn > self.snd_una:
            for psn in range(self.snd_una, pkt.base_psn):
                if psn in self.outstanding and not self.outstanding[psn].acked:
                    newly.append(psn)
                    self._mark_acked(psn)
            self.snd_una = pkt.base_psn
        for off in range(BITMAP_BITS):
            if get_bit(pkt.bitmap, off):
                psn = pkt.base_psn + off
                o = self.outstanding.get(psn)
                if o and not o.acked:
                    newly.append(psn)
                    self._mark_acked(psn)
        if pkt.xmit_ts is not None:
            self._rack(now, pkt.xmit_ts, pkt.base_psn)
        if self.n_packets is not None and self.snd_una >= self.n_packets:
            self.finished = True
            self.sim.bus.emit("conn_state", conn=self.conn, state="TEARDOWN")
        else:
            self.try_send()
        self._arm_tlp()
        for psn in newly:
            for cb in self.on_acked:
                cb(psn, self.outstanding.get(psn))

    def _mark_acked(self, psn: int) -> None:
        o = self.outstanding.get(psn)
        if not o or o.acked:
            return
        o.acked = True
        o.in_flight = False
        self.unacked_per_flow[o.flow] = max(0, self.unacked_per_flow.get(o.flow, 0) - 1)

    def _rack(self, now: float, xmit_ts: float, base_psn: int) -> None:
        rto = self.policy.rack_rto
        for psn, o in list(self.outstanding.items()):
            if o.acked:
                continue
            if psn < base_psn or psn >= base_psn + BITMAP_BITS:
                continue
            if o.xmit_ts > xmit_ts:
                continue
            elapsed = now - o.xmit_ts
            if elapsed > rto:
                self.sim.bus.emit("rack_fire", conn=self.conn, psn=psn, elapsed=elapsed)
                self._transmit(psn, o.payload, o.rsn, o.flow, retx=True)

    def _arm_tlp(self) -> None:
        self._tlp_gen += 1
        gen = self._tlp_gen
        self.sim.schedule(self.policy.tlp_idle, self._tlp, gen)

    def _tlp(self, gen: int) -> None:
        if gen != self._tlp_gen:
            return
        if self.n_packets is not None and self.snd_una >= self.n_packets:
            return
        lowest = None
        for psn, o in self.outstanding.items():
            if not o.acked:
                lowest = psn if lowest is None else min(lowest, psn)
        if lowest is None:
            return
        o = self.outstanding[lowest]
        self.sim.bus.emit("tlp_fire", conn=self.conn, psn=lowest)
        self._transmit(lowest, o.payload, o.rsn, o.flow, retx=True, probe=True)
        self._arm_tlp()


class FalconStylePdl:
    """Bulk-transfer helper wiring one sender + receiver over two paths."""

    def __init__(
        self,
        sim: Simulator,
        data_path: Path,
        ack_path: Path,
        conn: int = 1,
        n_packets: int = 100,
        mtu: int = 1500,
        policy: Policy | None = None,
        get_policy: Callable[[], Policy] | None = None,
        get_path: Callable[[int], Path] | None = None,
    ) -> None:
        self.sim = sim
        self.conn = conn
        self.n_packets = n_packets
        self.sender = PdlSender(
            sim,
            data_path,
            conn,
            n_packets=n_packets,
            mtu=mtu,
            policy=policy,
            get_policy=get_policy,
            get_path=get_path,
        )
        self.receiver = PdlReceiver(sim, ack_path, conn)
        data_path.attach(self.receiver.receive_data)
        ack_path.attach(self.sender.on_ack)

    @property
    def done(self) -> bool:
        return self.sender.finished

    def start(self) -> None:
        self.sim.bus.emit("conn_state", conn=self.conn, state="SETUP")
        self.sim.bus.emit("conn_state", conn=self.conn, state="ESTABLISHED")
        self.sender.start_bulk(self.n_packets)
