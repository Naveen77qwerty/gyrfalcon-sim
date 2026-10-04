"""Thin ULP mapping RDMA-like and NVMe-like ops to Push/Pull (paper Table 2).

Deliberately thin: it translates operations into transactions and nothing else. Ordering,
resource carving and backpressure all belong to the transaction layer.
"""

from __future__ import annotations

from falcon.tl.connection import Transaction, TransactionLayer


class Ulp:
    def __init__(self, tl: TransactionLayer) -> None:
        self.tl = tl

    def rdma_write(self, data: bytes) -> int | None:
        """WRITE maps to Push."""
        return self.tl.push(data)

    def rdma_send(self, data: bytes) -> int | None:
        """SEND/RECV map to Push."""
        return self.tl.push(data)

    def rdma_read(self, length: int) -> int | None:
        """READ maps to Pull."""
        return self.tl.pull(length)

    def rdma_atomic(self, length: int = 8) -> int | None:
        """ATOMICS map to Pull."""
        return self.tl.pull(length)

    def nvme_read(self, length: int, lba: int = 0) -> int | None:
        """NVMe Read maps to Pull."""
        return self.tl.pull(length, extra={"op": "nvme_read", "lba": lba, "length": length})

    def nvme_write(self, data: bytes, lba: int = 0) -> int | None:
        """NVMe Write maps to Push, then a Pull for the completion notification (Table 2).

        The `lba` has to travel with the transaction. Dropping it here makes every write in
        a workload land on LBA 0, which hides until a data-integrity check reads back a
        block the client never wrote.
        """
        self.tl.push(data, extra={"op": "nvme_write", "lba": lba})
        return self.tl.pull(4, extra={"op": "nvme_write_completion", "lba": lba})
