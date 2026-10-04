"""Applications: remote disk integrity and incast fairness."""

from __future__ import annotations

import pytest

from falcon.apps.incast import run_incast
from falcon.apps.remote_disk import BLOCK_BYTES, BlockStore, run_remote_disk
from falcon.metrics import metrics_from_events


def _pattern(lba: int, n: int) -> bytes:
    return bytes([(lba + j) % 256 for j in range(n)])


def test_block_store_assembles_multi_chunk_writes():
    """A write larger than one MTU arrives as several Push transactions. Overwriting per
    chunk would keep only the last one, and every later read would return the wrong data
    while still looking like a successful read."""
    store = BlockStore()
    store.write(0, b"\x01" * 4096)
    store.write(0, b"\x02" * 4096)
    assert len(store.read(0)) == BLOCK_BYTES
    assert store.read(0) == b"\x01" * 4096, "a full block must ignore the overflow"
    store.write(1, b"\xaa" * 100)
    assert store.read(1) == b"\xaa" * 100 + b"\x00" * (BLOCK_BYTES - 100)
    assert store.read(99) == b"\x00" * BLOCK_BYTES


def test_nvme_write_keeps_its_lba():
    """Regression: `nvme_write` accepted an lba and dropped it, so every write in the
    workload landed on LBA 0."""
    _sim, store, tl = run_remote_disk(seed=1, loss=0.0, n_writes=3, n_reads=3, write_bytes=8192, until=1.0)
    assert sorted(store.blocks) == [0, 1, 2]
    for lba in (0, 1, 2):
        assert store.read(lba) == _pattern(lba, BLOCK_BYTES)


@pytest.mark.parametrize("loss", [0.0, 0.001, 0.01, 0.05])
def test_remote_disk_data_integrity_under_loss(loss: float):
    """Every block read back must equal what the client wrote, at every loss rate."""
    _sim, store, tl = run_remote_disk(seed=3, loss=loss, n_writes=2, n_reads=8, write_bytes=65536, until=2.0)
    assert not tl.deadlocked
    for lba in (0, 1):
        assert store.read(lba) == _pattern(lba, BLOCK_BYTES), f"lba {lba} corrupted at loss {loss}"


def test_remote_disk_drains_its_pools():
    _sim, _store, tl = run_remote_disk(seed=1, loss=0.01, until=2.0)
    assert all(v == 0 for v in tl.resources.used.values()), tl.resources.used
    assert not tl._issue_queue and not tl._pending_responses


def test_remote_disk_reads_complete():
    _sim, _store, tl = run_remote_disk(seed=1, loss=0.01, n_reads=8, until=2.0)
    completes = [e for e in _sim.bus.events if e["type"] == "txn_complete"]
    starts = [e for e in _sim.bus.events if e["type"] == "txn_start"]
    assert len(completes) == len(starts) > 0


def test_remote_disk_same_seed_same_log():
    def run() -> str:
        return run_remote_disk(seed=6, loss=0.02, until=2.0)[0].bus.dumps()

    assert run() == run()


def test_incast_every_sender_finishes():
    sim = run_incast(n_senders=8, seed=1, n_packets=40, until=1.0)
    done = {e["conn"] for e in sim.bus.events if e["type"] == "conn_state" and e["state"] == "TEARDOWN"}
    assert done == set(range(1, 9))


def test_incast_goodput_is_fair_across_senders():
    """Paper Figure 13 trend: per-sender goodput variance should stay small."""
    sim = run_incast(n_senders=8, seed=1, n_packets=60, until=2.0)
    per_conn: dict[int, list[float]] = {}
    for ev in sim.bus.events:
        if ev["type"] == "pkt_recv" and ev.get("accepted") and ev["kind"] == "data":
            per_conn.setdefault(ev["conn"], []).append(ev["t"])
    counts = [len(v) for v in per_conn.values()]
    assert len(counts) == 8
    assert max(counts) - min(counts) <= 2, counts