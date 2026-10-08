"""Selective Repeat baseline: NACK per out-of-order packet; receiver buffers OOO."""

from __future__ import annotations

from gyrfalcon.core.sim import Simulator
from gyrfalcon.fae.policy import Policy
from gyrfalcon.net.packet import Packet
from gyrfalcon.net.path import Path


class SelectiveRepeat:
    def __init__(
        self,
        sim: Simulator,
        data_path: Path,
        ack_path: Path,
        conn: int = 1,
        n_packets: int = 100,
        mtu: int = 1500,
        policy: Policy | None = None,
    ) -> None:
        self.sim = sim
        self.data_path = data_path
        self.ack_path = ack_path
        self.conn = conn
        self.n_packets = n_packets
        self.mtu = mtu
        self.policy = policy or Policy()
        self.snd_una = 0
        self.snd_nxt = 0
        self.acked: set[int] = set()
        self.rcv_buf: set[int] = set()
        self.rcv_nxt = 0
        self._timers: dict[int, int] = {}
        self._timer_gen = 0
        self.done = False
        data_path.attach(self._on_data)
        ack_path.attach(self._on_ack)

    def start(self) -> None:
        self.sim.bus.emit("conn_state", conn=self.conn, state="ESTABLISHED")
        self._fill()

    def _fill(self) -> None:
        w = self.policy.send_window
        while self.snd_nxt < self.n_packets and self.snd_nxt < self.snd_una + w:
            self._send(self.snd_nxt, retx=False)
            self.snd_nxt += 1

    def _send(self, psn: int, retx: bool) -> None:
        pkt = Packet(conn=self.conn, psn=psn, kind="data", retx=retx, size=self.mtu)
        self.data_path.send(pkt)
        self._timer_gen += 1
        self._timers[psn] = self._timer_gen
        self.sim.schedule(self.policy.rto, self._on_rto, psn, self._timer_gen)

    def _on_rto(self, psn: int, gen: int) -> None:
        if self._timers.get(psn) != gen or psn in self.acked:
            return
        self._send(psn, retx=True)

    def _on_data(self, pkt: Packet) -> None:
        ooo = pkt.psn != self.rcv_nxt
        # Selective Repeat keeps every received PSN in rcv_buf, so membership is an
        # exact duplicate check: both out-of-order buffering and re-delivery land here.
        dup = pkt.psn in self.rcv_buf
        self.sim.bus.emit(
            "pkt_recv",
            conn=self.conn,
            flow=0,
            psn=pkt.psn,
            ooo=ooo,
            kind="data",
            dup=dup,
            accepted=not dup,
            is_response=False,
        )
        self.rcv_buf.add(pkt.psn)
        while self.rcv_nxt in self.rcv_buf:
            self.rcv_nxt += 1
        ack = Packet(conn=self.conn, psn=pkt.psn, kind="ack", base_psn=self.rcv_nxt, size=64)
        ack.extra["acked_psn"] = pkt.psn
        self.sim.bus.emit(
            "ack_send",
            conn=self.conn,
            flow=0,
            base_psn=self.rcv_nxt,
            bitmap=0,
            rx_buf=0.0,
            # Delay measurement (t1..t3) is a Falcon-style mechanism; the baselines
            # do not implement it, so the fields are present but null rather than absent.
            t1=None,
            t2=None,
            t3=None,
        )
        self.ack_path.send(ack)
        if ooo:
            nack = Packet(conn=self.conn, psn=self.rcv_nxt, kind="nack", nack_psn=self.rcv_nxt, size=64)
            self.sim.bus.emit("nack_send", conn=self.conn, psn=self.rcv_nxt)
            self.ack_path.send(nack)

    def _on_ack(self, pkt: Packet) -> None:
        if pkt.kind == "nack":
            psn = pkt.nack_psn if pkt.nack_psn is not None else pkt.psn
            if psn not in self.acked and psn < self.snd_nxt:
                self._send(psn, retx=True)
            return
        acked_psn = pkt.extra.get("acked_psn", pkt.psn)
        self.sim.bus.emit("ack_recv", conn=self.conn, flow=0, base_psn=pkt.base_psn, bitmap=0)
        self.acked.add(acked_psn)
        self._timers.pop(acked_psn, None)
        while self.snd_una in self.acked:
            self.snd_una += 1
        if self.snd_una >= self.n_packets:
            self.done = True
            self.sim.bus.emit("conn_state", conn=self.conn, state="TEARDOWN")
            return
        self._fill()
