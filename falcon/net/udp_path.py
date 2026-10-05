"""UDP-based path for running the same state machines over real sockets."""
from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from typing import Any

from falcon.net.packet import Packet


def _packet_to_dict(pkt: Packet) -> dict[str, Any]:
    return {
        "conn": pkt.conn,
        "psn": pkt.psn,
        "kind": pkt.kind,
        "flow": pkt.flow,
        "rsn": pkt.rsn,
        "retx": pkt.retx,
        "size": pkt.size,
        "payload": pkt.payload.decode("latin-1") if pkt.payload else "",
        "path_id": pkt.path_id,
        "t1": pkt.t1,
        "t2": pkt.t2,
        "t3": pkt.t3,
        "t4": pkt.t4,
        "xmit_ts": pkt.xmit_ts,
        "base_psn": pkt.base_psn,
        "bitmap": pkt.bitmap,
        "rx_buf": pkt.rx_buf,
        "nack_psn": pkt.nack_psn,
        "orig_xmit_ts": pkt.orig_xmit_ts,
        "is_response": pkt.is_response,
        "extra": pkt.extra,
    }


def _dict_to_packet(d: dict[str, Any]) -> Packet:
    payload = d.get("payload", "")
    return Packet(
        conn=d.get("conn", 0),
        psn=d.get("psn", 0),
        kind=d.get("kind", "data"),
        flow=d.get("flow", 0),
        rsn=d.get("rsn"),
        retx=bool(d.get("retx", False)),
        size=d.get("size", 1500),
        payload=payload.encode("latin-1") if isinstance(payload, str) else payload,
        path_id=d.get("path_id", 0),
        t1=d.get("t1"),
        t2=d.get("t2"),
        t3=d.get("t3"),
        t4=d.get("t4"),
        xmit_ts=d.get("xmit_ts"),
        base_psn=d.get("base_psn", 0),
        bitmap=d.get("bitmap", 0),
        rx_buf=d.get("rx_buf", 0.0),
        nack_psn=d.get("nack_psn"),
        orig_xmit_ts=d.get("orig_xmit_ts"),
        is_response=bool(d.get("is_response", False)),
        extra=d.get("extra") or {},
    )


class UdpPath:
    def __init__(
        self,
        sim,
        local_addr: tuple[str, int],
        remote_addr: tuple[str, int],
        name: str = "udp",
        path_id: int = 0,
        **_kwargs,
    ) -> None:
        self.sim = sim
        self.name = name
        self.path_id = path_id
        self.local_addr = local_addr
        self.remote_addr = remote_addr
        self._deliver: Callable[[Packet], None] | None = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(local_addr)
        self._sock.settimeout(0.01)
        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def attach(self, deliver: Callable[[Packet], None]) -> None:
        self._deliver = deliver

    def kill(self) -> None:
        self.sim.bus.emit("path_kill", conn=0, path=self.path_id)

    def slow(self, factor: float) -> None:
        self.sim.bus.emit("path_slow", conn=0, path=self.path_id, factor=factor)

    def unslow(self) -> None:
        self.sim.bus.emit("path_slow", conn=0, path=self.path_id, factor=1.0)

    def revive(self) -> None:
        pass

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
        try:
            data = json.dumps(_packet_to_dict(pkt)).encode("utf-8")
            self._sock.sendto(data, self.remote_addr)
        except Exception:
            pass

    def _recv_loop(self) -> None:
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                d = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            pkt = _dict_to_packet(d)
            if self._deliver:
                try:
                    self._deliver(pkt)
                except Exception:
                    pass

    def close(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass
