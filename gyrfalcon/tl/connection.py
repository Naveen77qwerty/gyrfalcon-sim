"""Transaction layer: Push/Pull, RSN, ordered/unordered, separate req/resp PSN spaces (4.4, A.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from gyrfalcon.core.sim import Simulator
from gyrfalcon.fae.policy import Policy
from gyrfalcon.net.packet import Packet
from gyrfalcon.net.path import Path
from gyrfalcon.pdl.reliability import PdlReceiver, PdlSender
from gyrfalcon.tl.resources import ResourcePools

MTU = 4096


@dataclass
class Transaction:
    rsn: int
    kind: str  # push | pull
    payload: bytes
    is_request: bool
    complete: bool = False
    response: bytes = b""
    extra: dict = field(default_factory=dict)


class TransactionLayer:
    """One initiator->target pair with dedicated request and response PSN spaces."""

    def __init__(
        self,
        sim: Simulator,
        req_path: Path,
        resp_path: Path,
        conn: int,
        ordered: bool = True,
        policy: Policy | None = None,
        carving: bool = True,
        shared_psn: bool = False,
        get_policy: Callable[[], Policy] | None = None,
        get_path: Callable[[int], Path] | None = None,
        mtu: int = MTU,
        stall_timeout: float = 5e-4,
    ) -> None:
        self.sim = sim
        self.conn = conn
        self.ordered = ordered
        self.mtu = mtu
        self.shared_psn = shared_psn
        self.resources = ResourcePools(sim, conn, carving=carving)
        self.next_rsn = 0
        self.last_in_order_rsn = -1
        self.ooo_txns: dict[int, Transaction] = {}
        self.ulp_seen: list[int] = []
        self.on_ulp: list[Callable[[Transaction], None]] = []
        self.pending: dict[int, Transaction] = {}
        self.target_handlers: list[Callable[[Transaction], bytes | None]] = []
        self.xnacks = 0
        self.deadlocked = False
        self.refusals = 0
        self.stall_timeout = stall_timeout
        # Queue of issues waiting on resources: (kind, payload, extra, issued, total, rsn).
        # Retrying on a fixed timer instead of on a release either spins or gives up before
        # the datapath has had any chance to free a pool.
        self._issue_queue: list[list] = []
        self._pending_responses: list[tuple[Transaction, bytes]] = []
        self._draining = False
        self._watchdog_gen = 0
        self._progress_at_refusal: int | None = None

        pol = policy or Policy()
        self.req_sender = PdlSender(
            sim, req_path, conn, mtu=mtu, policy=pol, is_response=False, get_policy=get_policy, get_path=get_path
        )
        self.req_receiver = PdlReceiver(sim, resp_path, conn, is_response=False)
        # Response uses the reverse path; separate PSN space unless shared_psn demo.
        self.resp_sender = PdlSender(
            sim, resp_path, conn, mtu=mtu, policy=pol, is_response=True, get_policy=get_policy
        )
        self.resp_receiver = PdlReceiver(sim, req_path, conn, is_response=True)

        if shared_psn:
            self.resp_sender = self.req_sender
            self.resp_receiver = self.req_receiver

        req_path.attach(self._on_req_path)
        resp_path.attach(self._on_resp_path)
        self.req_receiver.on_in_order.append(self._on_request_in_order)
        self.resp_receiver.on_in_order.append(self._on_response_in_order)
        self.req_sender.on_acked.append(self._on_req_acked)
        # The response sender has its own outstanding table, so its ACKs arrive on this
        # callback. Without this, tx_resp is reserved on every response and never freed.
        if self.resp_sender is not self.req_sender:
            self.resp_sender.on_acked.append(self._on_resp_acked)
        sim.bus.add_sink(self._on_bus)

    def _on_bus(self, ev: dict) -> None:
        # A release is the signal that capacity came back, so it is what retries queued
        # work. Any pool will do; the drains themselves decide whether they can proceed.
        #
        # The guard matters: issuing reserves resources, and a reserve can roll back and
        # release again, so without it the drain re-enters itself until the stack blows.
        if ev.get("type") != "resource_release" or ev.get("conn") != self.conn or self._draining:
            return
        self._draining = True
        try:
            self._drain_response_queue()
            self._drain_issue_queue()
        finally:
            self._draining = False

    def _on_req_path(self, pkt: Packet) -> None:
        if pkt.kind == "ack":
            if pkt.is_response:
                self.resp_sender.on_ack(pkt)
            else:
                self.req_sender.on_ack(pkt)
            return
        if pkt.is_response:
            self.resp_receiver.receive_data(pkt)
        else:
            self.req_receiver.receive_data(pkt)

    def _on_resp_path(self, pkt: Packet) -> None:
        if pkt.kind == "ack":
            if pkt.is_response:
                self.resp_sender.on_ack(pkt)
            else:
                self.req_sender.on_ack(pkt)
            return
        if pkt.is_response:
            self.resp_receiver.receive_data(pkt)
        else:
            self.req_receiver.receive_data(pkt)

    def push(self, payload: bytes, extra: dict | None = None) -> int | None:
        return self._queue_issue("push", payload, extra)[0]

    def pull(self, length: int, extra: dict | None = None) -> int | None:
        return self._queue_issue("pull", length.to_bytes(4, "big"), extra)[0]

    def issue_all(self, kind: str, payload: bytes, extra: dict | None = None) -> int:
        """Queue every MTU-sized chunk and issue as many as the pools allow right now.

        Returns the number issued immediately. The remainder stays queued and is issued as
        resources are released, so a write larger than the pools is never silently truncated
        at whatever capacity happened to be free.
        """
        return self._queue_issue(kind, payload, extra)[1]

    def _queue_issue(self, kind: str, payload: bytes, extra: dict | None) -> tuple[int | None, int]:
        total = len([payload[i : i + self.mtu] for i in range(0, max(len(payload), 1), self.mtu)])
        entry = [kind, payload, dict(extra or {}), 0, total, None]
        self._issue_queue.append(entry)
        self._drain_issue_queue()
        return entry[5], entry[3]

    def _drain_issue_queue(self) -> None:
        while self._issue_queue and not self.deadlocked:
            entry = self._issue_queue[0]
            kind, payload, extra, issued, total = entry[:5]
            while issued < total:
                first, got = self._issue(kind, payload[issued * self.mtu :], extra)
                if got == 0:
                    break
                if entry[5] is None:
                    entry[5] = first
                issued += got
                entry[3] = issued
            if issued >= total:
                self._issue_queue.pop(0)
                continue
            break
        if self._issue_queue or self._pending_responses:
            self._arm_stall_watchdog()
        else:
            self._watchdog_gen += 1

    def _issue(self, kind: str, payload: bytes, extra: dict | None = None) -> tuple[int | None, int]:
        """Send as many MTU-sized chunks as the resource pools allow.

        The push/pull distinction travels in the packet's `extra`, not in a payload-length
        guess: a 4-byte WRITE and a 4-byte READ are otherwise indistinguishable on the wire.
        """
        chunks = [payload[i : i + self.mtu] or b"\x00" for i in range(0, max(len(payload), 1), self.mtu)]
        first: int | None = None
        sent = 0
        for chunk in chunks:
            if not self.resources.reserve_initiator():
                self.refusals += 1
                break
            rsn = self.next_rsn
            self.next_rsn += 1
            if first is None:
                first = rsn
            sent += 1
            fields = dict(extra or {})
            fields["kind"] = kind
            txn = Transaction(rsn=rsn, kind=kind, payload=chunk, is_request=True, extra=dict(fields))
            self.pending[rsn] = txn
            self.sim.bus.emit("txn_start", conn=self.conn, rsn=rsn, kind=kind)
            self.req_sender.enqueue(chunk, rsn=rsn, extra=dict(fields))
        return first, sent

    def _arm_stall_watchdog(self) -> None:
        """Watch for the case section 4.5 is actually about: a circular wait.

        A full pool is normal backpressure. Queued work that is still stuck after a whole
        stall window during which *no* pool moved anywhere is a deadlock, because nothing
        that could free capacity is still in flight.
        """
        self._watchdog_gen += 1
        gen = self._watchdog_gen
        self._progress_at_refusal = self.resources.progress
        self.sim.schedule(self.stall_timeout, self._on_stall_watchdog, gen)

    def _on_stall_watchdog(self, gen: int) -> None:
        if gen != self._watchdog_gen or self.deadlocked:
            return
        if not self._issue_queue and not self._pending_responses:
            return
        if self.resources.progress == self._progress_at_refusal:
            self.deadlocked = True
            self.sim.bus.emit(
                "tl_deadlock",
                conn=self.conn,
                queued=len(self._issue_queue),
                pending_responses=len(self._pending_responses),
            )
            return
        self._drain_response_queue()
        self._drain_issue_queue()

    def _is_hol(self, rsn: int) -> bool:
        if not self.ordered:
            return True
        return rsn == self.last_in_order_rsn + 1

    def _on_request_in_order(self, pkt: Packet) -> None:
        rsn = pkt.rsn if pkt.rsn is not None else pkt.psn
        if not self.resources.admit_rx_request(self._is_hol(rsn)):
            # admit_rx_request already emitted the resource_nack and re-ACKed via the PDL.
            self.xnacks += 1
            return
        # `extra["kind"]` is authoritative. The length heuristic only covers packets from a
        # sender too old to set it, which is never this implementation but keeps the
        # fallback honest.
        kind = pkt.extra.get("kind") or ("pull" if len(pkt.payload) == 4 else "push")
        txn = Transaction(rsn=rsn, kind=kind, payload=pkt.payload, is_request=True, extra=dict(pkt.extra))
        self.ooo_txns[rsn] = txn
        self._deliver_requests()

    def _deliver_requests(self) -> None:
        if self.ordered:
            while self.last_in_order_rsn + 1 in self.ooo_txns:
                rsn = self.last_in_order_rsn + 1
                txn = self.ooo_txns.pop(rsn)
                self.last_in_order_rsn = rsn
                self._serve(txn)
        else:
            # Unordered delivers as it arrives. `ooo_txns` is insertion-ordered, so
            # iterating it directly is arrival order -- sorting here would quietly make
            # unordered connections behave like ordered ones.
            for rsn in list(self.ooo_txns):
                self._serve(self.ooo_txns.pop(rsn))

    def _serve(self, txn: Transaction) -> None:
        resp = b"ok"
        for h in self.target_handlers:
            out = h(txn)
            if out is not None:
                resp = out
        if not self.resources.reserve("tx_resp"):
            # Dropping the response here would strand the initiator, which is already
            # holding an rx_resp reservation for it. Queue instead of losing it.
            self.refusals += 1
            self._pending_responses.append((txn, resp))
            self.resources.release("rx_req")
            self._arm_stall_watchdog()
            return
        self._send_response(txn, resp)

    def _send_response(self, txn: Transaction, resp: bytes) -> None:
        self.resp_sender.enqueue(resp, rsn=txn.rsn)
        self.resources.release("rx_req")
        self.ulp_seen.append(txn.rsn)
        for cb in self.on_ulp:
            cb(txn)
        self.sim.bus.emit("ulp_deliver", conn=self.conn, rsn=txn.rsn, op=txn.kind)

    def _drain_response_queue(self) -> None:
        while self._pending_responses and not self.deadlocked:
            txn, resp = self._pending_responses[0]
            if not self.resources.reserve("tx_resp"):
                self.refusals += 1
                break
            self._pending_responses.pop(0)
            self._send_response(txn, resp)

    def _on_response_in_order(self, pkt: Packet) -> None:
        rsn = pkt.rsn if pkt.rsn is not None else pkt.psn
        txn = self.pending.get(rsn)
        if txn:
            txn.complete = True
            txn.response = pkt.payload
            self.sim.bus.emit("txn_complete", conn=self.conn, rsn=rsn, kind=txn.kind)
        self.resources.release("rx_resp")

    def _on_req_acked(self, psn: int, outstanding) -> None:
        self.resources.release("tx_req")

    def _on_resp_acked(self, psn: int, outstanding) -> None:
        self.resources.release("tx_resp")
