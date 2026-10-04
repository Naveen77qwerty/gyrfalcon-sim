"""Remote disk: read completion and data integrity as loss rises.

Two questions. First, does a remote NVMe read still complete when the path is lossy.
Second, and the one that matters more, does the block read back byte-identical to what was
written. A protocol can complete every transaction and still corrupt data, so integrity is
checked independently and reported as a hard pass/fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.common import sweep

from falcon.apps.remote_disk import BLOCK_BYTES, run_remote_disk

SEEDS = [3, 4, 5, 6, 7]
LOSSES = [0.0, 0.001, 0.005, 0.01, 0.03]


def expected(lba: int, n: int = BLOCK_BYTES) -> bytes:
    return bytes([(lba + j) % 256 for j in range(n)])


def check_once(loss: float, seed: int) -> tuple[float, bool]:
    sim, store, tl = run_remote_disk(
        seed=seed, loss=loss, n_writes=2, n_reads=8, write_bytes=65536, until=4.0
    )
    intact = all(store.read(lba) == expected(lba) for lba in (0, 1))
    starts = sum(1 for e in sim.bus.events if e["type"] == "txn_start")
    completes = sum(1 for e in sim.bus.events if e["type"] == "txn_complete")
    completion_ratio = completes / starts if starts else 0.0
    if not intact:
        print(f"  DATA CORRUPTION at loss={loss} seed={seed}")
    if tl.deadlocked:
        print(f"  DEADLOCK at loss={loss} seed={seed}")
    return completion_ratio, intact and not tl.deadlocked


def main() -> None:
    ratios: list[float] = []
    for loss in LOSSES:
        for seed in SEEDS:
            ratio, ok = check_once(loss, seed)
            ratios.append(ratio)
            print(f"loss={loss:<6} seed={seed}  completed={ratio:6.1%}  intact={ok}")
    print(f"\nall runs intact: {all(check_once(l, s)[1] for l in LOSSES for s in SEEDS)}")

    sweep(
        LOSSES,
        ["remote-disk"],
        lambda _n, loss, seed: check_once(loss, seed)[0],
        SEEDS,
        "drop rate",
        "transactions completed",
        "Remote disk: transaction completion under loss (not paper numbers)",
        "remote_disk_completion.png",
    )


if __name__ == "__main__":
    main()
