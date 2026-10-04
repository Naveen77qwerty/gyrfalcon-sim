"""Go-Back-N baseline: receiver drops out-of-order; one loss retransmits the window."""

from __future__ import annotations

from falcon.core.sim import Simulator
from falcon.fae.policy import Policy
from falcon.net.packet import Packet
from falcon.net.path import Path


class GoBackN:
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
        self.rcv_nxt = 0
        self._rto_seq = 0
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
        self._arm_rto()

    def _send(self, psn: int, retx: bool) -> None:
        pkt = Packet(
            conn=self.conn,
            psn=psn,
            kind="data",
            retx=retx,
            size=self.mtu,
            orig_xmit_ts=self.sim.now(),
        )
        self.data_path.send(pkt)

    def _arm_rto(self) -> None:
        if self.snd_una >= self.n_packets:
            return
        self._rto_seq += 1
        seq = self._rto_seq
        self.sim.schedule(self.policy.rto, self._on_rto, seq)

    def _on_rto(self, seq: int) -> None:
        if seq != self._rto_seq or self.snd_una >= self.snd_nxt:
            return
        for psn in range(self.snd_una, self.snd_nxt):
            self._send(psn, retx=True)
        self._arm_rto()

    def _on_data(self, pkt: Packet) -> None:
        # Go-Back-N receiver semantics: out-of-order packets are discarded, so only the
        # in-order one counts as accepted. `accepted` lets metrics see that difference.
        ooo = pkt.psn != self.rcv_nxt
        accepted = not ooo
        self.sim.bus.emit(
            "pkt_recv",
            conn=self.conn,
            flow=0,
            psn=pkt.psn,
            ooo=ooo,
            kind="data",
            dup=False,
            accepted=accepted,
            is_response=False,
        )
        if accepted:
            self.rcv_nxt += 1
        # Go-Back-N has no SACK bitmap, so it acks the cumulative base only.
        self.sim.bus.emit(
            "ack_send",
            conn=self.conn,
            flow=0,
            base_psn=self.rcv_nxt,
            bitmap=0,
            rx_buf=0.0,
            # Delay measurement (t1..t3) is a Falcon-style mechanism; the baselines do not
            # implement it, so the fields are present but null rather than absent.
            t1=None,
            t2=None,
            t3=None,
        )
        ack = Packet(conn=self.conn, psn=self.rcv_nxt, kind="ack", base_psn=self.rcv_nxt, size=64)
        self.ack_path.send(ack)

    def _on_ack(self, pkt: Packet) -> None:
        self.sim.bus.emit("ack_recv", conn=self.conn, flow=0, base_psn=pkt.base_psn, bitmap=0)
        if pkt.base_psn > self.snd_una:
            self.snd_una = pkt.base_psn
            if self.snd_una >= self.n_packets:
                self.done = True
                self.sim.bus.emit("conn_state", conn=self.conn, state="TEARDOWN")
                return
            self._fill()
