"""Resource carving (paper Figure 6 / section 4.5) and DT isolation (section 4.6)."""

from __future__ import annotations

from gyrfalcon.core.sim import Simulator

POOLS = ("tx_req", "tx_resp", "rx_req", "rx_resp")


class ResourcePools:
    """Counters stand in for SRAM. Initiator reserves Tx request + Rx response before send."""

    def __init__(
        self,
        sim: Simulator,
        conn: int,
        capacity: int = 32,
        hol_threshold: float = 0.5,
        alpha_c: float = 1.0,
        carving: bool = True,
        xoff_level: float = 0.8,
        xon_level: float = 0.4,
    ) -> None:
        self.sim = sim
        self.conn = conn
        self.capacity = {p: capacity for p in POOLS}
        self.used = {p: 0 for p in POOLS}
        self.hol_threshold = hol_threshold
        self.alpha_c = alpha_c
        self.carving = carving
        self.xoff_level = xoff_level
        self.xon_level = xon_level
        self.xoff = False
        # Bumped by every state change. The transaction layer compares it across refused
        # reservations: a refusal with no progress since the last one is a stall, whereas a
        # refusal after progress is ordinary backpressure.
        self.progress = 0

    def free(self, pool: str) -> int:
        return self.capacity[pool] - self.used[pool]

    def occupancy(self, pool: str) -> float:
        return self.used[pool] / max(1, self.capacity[pool])

    def isolation_limit(self, pool: str) -> int:
        """T_c = alpha_c * FreeResources (static alpha until FAE makes it dynamic)."""
        return max(1, int(self.alpha_c * self.free(pool)))

    def reserve(self, pool: str, n: int = 1) -> bool:
        if self.used[pool] + n > self.capacity[pool]:
            return False
        self.used[pool] += n
        self.progress += 1
        self.sim.bus.emit("resource_reserve", conn=self.conn, pool=pool, n=n)
        self._backpressure()
        return True

    def release(self, pool: str, n: int = 1, *, rollback: bool = False) -> None:
        """Free `n` from `pool`.

        `rollback=True` undoes a partial reserve. It deliberately does not emit and does
        not count as progress: net occupancy is unchanged, so no capacity became available
        to anyone, and reporting it as a release would let a refused reservation look like
        the trigger to retry -- which recurses forever.
        """
        self.used[pool] = max(0, self.used[pool] - n)
        if rollback:
            return
        self.progress += 1
        self.sim.bus.emit("resource_release", conn=self.conn, pool=pool, n=n)
        self._backpressure()

    def reserve_initiator(self) -> bool:
        """Reserve Tx for the request *and* Rx for the response before sending (section 4.5)."""
        if not self.reserve("tx_req"):
            return False
        if not self.reserve("rx_resp"):
            self.release("tx_req", rollback=True)
            return False
        return True

    def admit_rx_request(self, is_hol: bool) -> bool:
        if self.used["rx_req"] >= self.capacity["rx_req"]:
            self.sim.bus.emit("resource_nack", conn=self.conn, pool="rx_req")
            return False
        if self.carving and self.occupancy("rx_req") >= self.hol_threshold and not is_hol:
            self.sim.bus.emit("resource_nack", conn=self.conn, pool="rx_req")
            return False
        return self.reserve("rx_req")

    def _backpressure(self) -> None:
        occ = self.occupancy("tx_req")
        if not self.xoff and occ >= self.xoff_level:
            self.xoff = True
            self.sim.bus.emit("xoff", conn=self.conn, reason="tx_req")
        elif self.xoff and occ <= self.xon_level:
            self.xoff = False
            self.sim.bus.emit("xon", conn=self.conn, reason="tx_req")
