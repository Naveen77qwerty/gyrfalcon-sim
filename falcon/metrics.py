"""Metrics derived only from the event log."""

from __future__ import annotations

from typing import Any


def metrics_from_events(events: list[dict[str, Any]], payload_bytes: int = 1500) -> dict[str, Any]:
    data_sends = [e for e in events if e["type"] == "pkt_send" and e.get("kind") in (None, "data", "probe")]
    drops = [e for e in events if e["type"] == "pkt_drop"]
    recvs = [e for e in events if e["type"] == "pkt_recv" and e.get("kind") in (None, "data", "probe")]
    retx = [e for e in data_sends if e.get("retx")]
    dropped_psns = {(e.get("conn"), e.get("psn"), e.get("flow")) for e in drops}
    spurious = 0
    for e in retx:
        key = (e.get("conn"), e.get("psn"), e.get("flow"))
        if key not in dropped_psns:
            spurious += 1
    t_end = events[-1]["t"] if events else 0.0
    delivered = len(recvs)
    goodput = (delivered * payload_bytes * 8 / t_end) if t_end > 0 else 0.0
    return {
        "packets_sent": len(data_sends),
        "packets_delivered": delivered,
        "retransmissions": len(retx),
        "spurious_retransmissions": spurious,
        "drops": len(drops),
        "completion_time": t_end,
        "goodput_bps": goodput,
    }
