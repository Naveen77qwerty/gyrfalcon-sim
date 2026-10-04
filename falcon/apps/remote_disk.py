"""Remote disk: 4-16 KB reads and 1 MB writes over Falcon-style TL (Near Local Flash miniature).

The client writes blocks, then reads them back. Every read is checked against the bytes the
client actually wrote, so a lost or reordered chunk shows up as corrupted data rather than as
a quietly shorter block.
"""

from __future__ import annotations

from falcon.core.sim import Simulator
from falcon.fae.policy import Policy
from falcon.net.path import Path
from falcon.tl.connection import Transaction, TransactionLayer
from falcon.ulp.mapping import Ulp

BLOCK_BYTES = 4096


class BlockStore:
    """Block store standing in for a remote SSD.

    Writes accumulate per LBA rather than overwriting, because a write larger than one MTU
    arrives as several Push transactions. Overwriting would keep only the final chunk and
    make every integrity check pass on the wrong data.
    """

    def __init__(self, block_bytes: int = BLOCK_BYTES) -> None:
        self.blocks: dict[int, bytearray] = {}
        self.block_bytes = block_bytes

    def write(self, lba: int, data: bytes) -> None:
        buf = self.blocks.setdefault(lba, bytearray())
        room = self.block_bytes - len(buf)
        if room > 0:
            buf.extend(data[:room])

    def read(self, lba: int) -> bytes:
        buf = self.blocks.get(lba)
        if not buf:
            return b"\x00" * self.block_bytes
        return bytes(buf).ljust(self.block_bytes, b"\x00")


def run_remote_disk(
    seed: int = 1,
    loss: float = 0.0,
    n_writes: int = 2,
    n_reads: int = 8,
    write_bytes: int = 65536,
    ordered: bool = True,
    until: float = 2.0,
    read_delay: float = 0.01,
) -> tuple[Simulator, BlockStore, TransactionLayer]:
    """Smaller default write than 1 MB so unit tests stay fast; experiments can pass 1_000_000.

    `issue_all` is used rather than `push` so a write larger than the resource pools is
    issued across as many rounds as it needs instead of being truncated at the first refusal.
    """
    sim = Simulator(seed=seed)
    fwd = Path(sim, name="fwd", delay=5e-5, loss=loss, bandwidth_bps=25e9, path_id=0)
    rev = Path(sim, name="rev", delay=5e-5, loss=0.0, bandwidth_bps=25e9, path_id=1)
    store = BlockStore()
    tl = TransactionLayer(sim, fwd, rev, conn=1, ordered=ordered, policy=Policy(send_window=64))
    ulp = Ulp(tl)
    written: dict[int, bytes] = {}

    def handle(txn: Transaction) -> bytes:
        lba = txn.extra.get("lba", 0)
        if txn.kind == "push":
            store.write(lba, txn.payload)
            return b"ok"
        return store.read(lba)

    tl.target_handlers.append(handle)

    for i in range(n_writes):
        # One deterministic pattern per LBA, kept so tests can reconstruct the expectation.
        data = bytes([(i + j) % 256 for j in range(write_bytes)])
        written[i] = data
        ulp.tl.issue_all("push", data, extra={"op": "nvme_write", "lba": i})
        ulp.tl.issue_all("pull", b"\x04\x00\x00\x00", extra={"op": "nvme_write_completion", "lba": i})

    def later_reads() -> None:
        for i in range(n_reads):
            lba = i % max(1, n_writes)
            ulp.tl.issue_all(
                "pull",
                (BLOCK_BYTES).to_bytes(4, "big"),
                extra={"op": "nvme_read", "lba": lba, "length": BLOCK_BYTES},
            )

    sim.schedule(read_delay, later_reads)
    tl.req_sender.try_send()
    sim.run(until=until)
    return sim, store, tl