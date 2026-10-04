"""Constant policy used until the FAE is fully wired in Phase 3. Paper Table 3 split."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Policy:
    rack_rto: float = 1.2e-4
    tlp_idle: float = 2.5e-4
    rto: float = 4.0e-4
    send_window: int = 32
    ncwnd: float = 64.0
    fcwnd: dict[int, float] = field(default_factory=lambda: {0: 32.0})
    pacing_gap: float = 0.0
    alpha: float = 1.0
    path_for_flow: dict[int, int] = field(default_factory=lambda: {0: 0})


class ConstantEngine:
    """Stub FAE: returns fixed parameters. Replaced by real FAE in Phase 3."""

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()

    def on_event(self, ev: dict) -> Policy:
        return self.policy
