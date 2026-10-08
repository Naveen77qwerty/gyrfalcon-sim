"""Parameter carrier shared by the datapath and the FAE (paper Table 3).

Every field is a *policy* knob. The PDL and TL read them but never choose them; the FAE is
the only thing that writes them, which is what keeps the mechanism/management split honest.
"""

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
    scheduler: str = "largest_open"
    """Which flow to send next. Named here rather than hardcoded in `pdl` so that swapping
    scheduling policy is an `fae/` change (paper 4.3 flow-level scheduling).

    Supported: "largest_open" (flow with the largest `fcwnd - unacked`) and "round_robin".
    """

