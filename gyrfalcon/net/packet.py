from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["data", "ack", "nack", "probe", "setup", "teardown", "xnack", "resource_nack"]


@dataclass
class Packet:
    conn: int
    psn: int
    kind: Kind = "data"
    flow: int = 0
    rsn: int | None = None
    retx: bool = False
    size: int = 1500
    payload: bytes = b""
    path_id: int = 0
    # timestamps for delay CC (paper section 4.2)
    t1: float | None = None  # data sent
    t2: float | None = None  # data arrived
    t3: float | None = None  # ack sent
    t4: float | None = None  # ack arrived
    xmit_ts: float | None = None  # send time of the packet that generated this ACK
    base_psn: int = 0
    bitmap: int = 0
    rx_buf: float = 0.0
    nack_psn: int | None = None
    orig_xmit_ts: float | None = None  # original send time of this data packet
    is_response: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
