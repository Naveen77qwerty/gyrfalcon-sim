"""Transaction layer: ordering, resource lifecycle, backpressure (paper 4.4, 4.5, 4.6, A.1)."""

from __future__ import annotations

import math

from gyrfalcon.core.sim import Simulator
from gyrfalcon.fae.policy import Policy
from gyrfalcon.net.path import Path
from gyrfalcon.tl.connection import Transaction, TransactionLayer


def _tl(**kw) -> tuple[Simulator, TransactionLayer, Path, Path]:
    sim = Simulator(seed=kw.pop("seed", 1))
    fwd = Path(sim, name="fwd", delay=5e-5, loss=kw.pop("loss", 0.0), reorder=kw.pop("reorder", 0.0), path_id=0)
    rev = Path(sim, name="rev", delay=5e-5, loss=0.0, path_id=1)
    tl = TransactionLayer(sim, fwd, rev, conn=1, policy=Policy(send_window=64), **kw)
    return sim, tl, fwd, rev


def _echo(txn: Transaction) -> bytes:
    return b"resp-" + bytes([txn.rsn % 251])


def test_pools_drain_to_zero_after_a_transfer():
    """Regression: tx_resp was reserved per response and never released, and rx_req
    leaked whenever the response pool was briefly full. Both ended at 32/32 used."""
    sim, tl, _f, _r = _tl(loss=0.01)
    tl.target_handlers.append(_echo)
    for i in range(8):
        tl.issue_all("push", bytes([i]) * 4096, extra={"lba": i})
    sim.run(until=1.0)
    assert not tl.deadlocked
    assert all(v == 0 for v in tl.resources.used.values()), tl.resources.used


def test_push_pull_kind_survives_the_wire():
    """A 4-byte Push and a 4-byte Pull are indistinguishable by payload alone, so the
    kind has to travel in the packet instead of being guessed from the length."""
    sim, tl, _f, _r = _tl(loss=0.01)
    seen: list[tuple[int, str]] = []
    tl.target_handlers.append(lambda txn: (seen.append((txn.rsn, txn.kind)), b"ok")[1])
    tiny_push = tl.issue_all("push", b"\x01\x02\x03\x04", extra={"lba": 3})
    assert tiny_push == 1
    sim.run(until=0.5)
    assert seen and seen[0] == (0, "push"), seen


def test_pull_is_served_as_pull():
    sim, tl, _f, _r = _tl()
    seen: list[tuple[int, str]] = []
    tl.target_handlers.append(lambda txn: (seen.append((txn.rsn, txn.kind)), b"data")[1])
    tl.issue_all("pull", (4096).to_bytes(4, "big"), extra={"lba": 1})
    sim.run(until=0.5)
    assert seen and seen[0][1] == "pull", seen


def test_ordered_delivery_is_strictly_increasing_under_reordering():
    sim, tl, _f, _r = _tl(reorder=0.4)
    tl.target_handlers.append(_echo)
    for i in range(16):
        tl.issue_all("push", bytes([i]) * 512, extra={"lba": i})
    sim.run(until=1.0)
    assert tl.ulp_seen == sorted(tl.ulp_seen)
    assert tl.ulp_seen == list(range(16)), tl.ulp_seen


def test_ordered_waits_for_a_gap_but_unordered_does_not():
    """Ordered delivery must stall on a missing RSN; unordered must not.

    Driven directly rather than over the network: the PDL already restores PSN order, so
    with one-packet transactions an end-to-end run cannot tell the two modes apart. The
    difference only shows when a transaction is missing, which is what this injects.
    """
    for ordered, blocked in ((True, True), (False, False)):
        sim, tl, _f, _r = _tl(ordered=ordered)
        served: list[int] = []
        tl.target_handlers.append(lambda txn: (served.append(txn.rsn), b"ok")[1])
        # RSN 0 never arrived, so RSN 1 is not head-of-line.
        tl.ooo_txns[1] = Transaction(rsn=1, kind="push", payload=b"a", is_request=True)
        tl._deliver_requests()
        assert (served == []) is blocked, (ordered, served)
        assert not tl._pending_responses, "a blocked request must not consume tx_resp"


def test_unordered_delivers_everything_under_reordering():
    sim, tl, _f, _r = _tl(ordered=False, reorder=0.4, loss=0.01)
    tl.target_handlers.append(_echo)
    for i in range(16):
        tl.issue_all("push", bytes([i]) * 512, extra={"lba": i})
    sim.run(until=1.0)
    assert sorted(tl.ulp_seen) == list(range(16))
    assert not tl.deadlocked


def test_requests_and_responses_use_separate_psn_spaces():
    """Paper appendix A.1: separate request/response spaces are the answer to
    request-response deadlock, so the two counters must advance independently."""
    sim, tl, _f, _r = _tl()
    tl.target_handlers.append(_echo)
    for i in range(4):
        tl.issue_all("push", bytes([i]) * 4096, extra={"lba": i})
    sim.run(until=1.0)
    assert tl.req_sender is not tl.resp_sender
    assert tl.req_sender.snd_nxt == 4
    assert tl.resp_sender.snd_nxt == 4
    assert tl.resp_receiver.rcv_nxt == 4


def test_shared_psn_space_is_opt_in():
    sim, tl, _f, _r = _tl(shared_psn=True)
    assert tl.req_sender is tl.resp_sender
    assert tl.req_receiver is tl.resp_receiver


def test_backpressure_releases_and_reacquires():
    sim, tl, _f, _r = _tl()
    tl.target_handlers.append(_echo)
    for i in range(60):
        tl.issue_all("push", bytes([i]) * 4096, extra={"lba": i})
    sim.run(until=2.0)
    types = [e["type"] for e in sim.bus.events]
    assert "xoff" in types and "xon" in types, "pools of 32 should have forced backpressure"
    assert not tl.deadlocked


def test_carving_admits_only_head_of_line_requests():
    sim, tl, _f, _r = _tl(ordered=True)
    # Fill rx_req past the HoL threshold with a non-HoL request: it must be refused.
    pools = tl.resources
    pools.used["rx_req"] = int(pools.capacity["rx_req"] * pools.hol_threshold)
    assert pools.admit_rx_request(is_hol=True)
    assert not pools.admit_rx_request(is_hol=False)


def test_rollback_does_not_look_like_freed_capacity():
    """A compensating release must not emit, or a refused reservation would re-trigger
    the retry drain and recurse forever."""
    sim, tl, _f, _r = _tl()
    pools = tl.resources
    pools.used["rx_resp"] = pools.capacity["rx_resp"]
    pools.reserve("tx_req")
    before = pools.progress
    n_before = len(sim.bus.events)
    assert pools.reserve_initiator() is False
    assert pools.used["tx_req"] == 1, "the tx_req reservation must be handed back"
    assert pools.progress == before + 1, "a rollback is not progress"
    assert not [e for e in sim.bus.events[n_before:] if e["type"] == "resource_release"]


def test_large_write_is_not_truncated_by_the_pools():
    sim, tl, _f, _r = _tl()
    tl.target_handlers.append(_echo)
    chunks = math.ceil(1_000_000 / tl.mtu)
    tl.issue_all("push", b"\xab" * 1_000_000, extra={"lba": 0})
    sim.run(until=5.0)
    started = [e for e in sim.bus.events if e["type"] == "txn_start"]
    assert len(started) == chunks, len(started)
    assert not tl._issue_queue
    assert not tl.deadlocked


def test_same_seed_gives_identical_transaction_log():
    def run() -> str:
        sim, tl, _f, _r = _tl(seed=8, loss=0.02, reorder=0.1)
        tl.target_handlers.append(_echo)
        for i in range(8):
            tl.issue_all("push", bytes([i]) * 2048, extra={"lba": i})
        sim.run(until=1.0)
        return sim.bus.dumps()

    assert run() == run()