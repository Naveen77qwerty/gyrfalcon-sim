"""Metrics derived only from the event log.

Two rules drive everything here, both forced by what `pkt_recv` actually means (see
docs/event-schema.md): a packet that arrived is not a packet that was delivered, and a
retransmission is only spurious if the data had *already* reached the receiver.

Counting arrivals instead of deliveries is the easiest way to make a transport look fast
for the wrong reason: Go-Back-N retransmits its whole window, so a naive arrival count
grows with retransmission volume and can exceed a transport that actually delivers every
byte. A spurious-retransmission count built from "was this PSN ever dropped" is wrong in
the other direction: under zero loss it reports every retransmission as spurious, and
under loss it hides a genuine spurious retransmission behind any earlier drop of the same
PSN.
"""

from __future__ import annotations

from typing import Any

DATA_KINDS = (None, "data", "probe")


def _is_data_send(ev: dict[str, Any]) -> bool:
    return ev["type"] == "pkt_send" and ev.get("kind") in DATA_KINDS


def _is_accepted_recv(ev: dict[str, Any]) -> bool:
    return (
        ev["type"] == "pkt_recv"
        and ev.get("kind") in DATA_KINDS
        and bool(ev.get("accepted", True))
        and not ev.get("dup", False)
    )


def _is_duplicate_recv(ev: dict[str, Any]) -> bool:
    return ev["type"] == "pkt_recv" and ev.get("kind") in DATA_KINDS and bool(ev.get("dup", False))


def metrics_from_events(events: list[dict[str, Any]], payload_bytes: int = 1500) -> dict[str, Any]:
    sends = [e for e in events if _is_data_send(e)]
    drops = [e for e in events if e["type"] == "pkt_drop"]
    retx = [e for e in sends if e.get("retx")]
    dup_arrivals = [e for e in events if _is_duplicate_recv(e)]

    # First delivery time per PSN. Keyed without `flow`: a PSN is delivered once per
    # connection no matter which flow carried it, so including flow would double-count
    # after a repath.
    first_recv: dict[tuple[Any, Any], float] = {}
    for e in events:
        if not _is_accepted_recv(e):
            continue
        key = (e.get("conn"), e.get("psn"))
        if key not in first_recv:
            first_recv[key] = e["t"]

    # A retransmission is spurious when the PSN was accepted before the retransmission
    # went out. Comparing timestamps rather than PSN membership is what makes this correct
    # both with and without loss.
    spurious = 0
    for e in retx:
        recv_t = first_recv.get((e.get("conn"), e.get("psn")))
        if recv_t is not None and recv_t <= e["t"]:
            spurious += 1

    # Completion is when the transfer finished, not when the last bookkeeping event fired.
    teardowns = [e["t"] for e in events if e["type"] == "conn_state" and e.get("state") == "TEARDOWN"]
    if teardowns:
        t_end = max(teardowns)
    elif first_recv:
        t_end = max(first_recv.values())
    else:
        t_end = 0.0

    delivered_bytes = len(first_recv) * payload_bytes
    goodput = (delivered_bytes * 8 / t_end) if t_end > 0 else 0.0
    return {
        "packets_sent": len(sends),
        "packets_delivered": len(first_recv),
        "retransmissions": len(retx),
        "spurious_retransmissions": spurious,
        "duplicate_arrivals": len(dup_arrivals),
        "drops": len(drops),
        "completion_time": t_end,
        "goodput_bps": goodput,
    }