# Feature matrix

What this simulator implements from `docs/falcon-paper.pdf`, and — more importantly — where it
simplifies. Anything marked **simplified** is a deliberate reduction, not an oversight, and the
reason is given so a reader can tell the difference without reading the code.

This is a Falcon-*style* teaching implementation. It does not reproduce the paper's numbers and
must not be described as if it does.

## Mechanism (`pdl/`, `tl/`)

| Paper section | Feature | Status | Notes |
|---|---|---|---|
| 3.2 | Reliable byte stream over PSN | full | `PdlSender`/`PdlReceiver`, SACK bitmap, shared PSN space |
| 4.3 | Per-flow path id, `fcwnd`, unacked count | full | flows share one PSN space and one Rx bitmap |
| 4.3 | Connection gating `min(ncwnd, Σ fcwnd)` | full | responses are exempt, as the requester pre-reserved their pools |
| 4.3 | Flow scheduling by largest open window | full | rule named in `Policy.scheduler`; round-robin available for comparison |
| 4.1 | RACK-style loss detection | full | `PdlReceiver` timestamps, per-packet |
| 4.1 | TLP probe on idle | full | `tlp_idle` from the FAE |
| 4.6 | Request/response, separate PSN spaces | full | the anti-deadlock property; `shared_psn=True` provided to show what it costs |
| 4.6 | Ordered vs unordered transactions | full | unordered delivers on arrival, ordered stalls on a gap |
| 4.6 | Resource carving and HoL admission | full | `tl/resources.py`, head-of-line requests are admitted first |
| 4.6 | Backpressure (`xoff`/`xon`) | full | admission retries are event-driven, never polled in a loop |
| 4.6 | Deadlock detection | **simplified** | stall watchdog on queue + pending-response depth; no full wait-for graph, no cycle extraction |
| 4.4 | Push / Pull transactions | full | `kind` travels in the packet, not inferred from length |
| Table 2 | RDMA and NVMe op mapping | full | `ulp/mapping.py`; NVMe write emits a completion Pull |
| 5.1 | Timing-wheel pacing | **simplified** | flat inter-packet gap from `Policy.pacing_gap`; no wheel, no timer resolution |
| — | NIC receive window (`ncwnd`) | full | driven by receiver buffer occupancy reported on ACKs |

## Policy (`fae/` only)

| Paper section | Feature | Status | Notes |
|---|---|---|---|
| 4.2 | Delay measurement without synced clocks | full | `(t4 - t1) - (t3 - t2)` |
| 4.2 | Swift-style delay CC | full | additive increase below target, multiplicative decrease on overshoot |
| 4.2 | Pluggable CC behind one interface | full | `AimdCc` swaps in at construction; `pdl/` is untouched |
| 4.3 | Path assignment and repathing on outage | **simplified** | displaced flows are spread across survivors; no congestion-aware PLB/PRR steering |
| 4.5 | ncwnd from receiver buffer occupancy | full | plus a slow-receiver knob (`Path.slow`) to exercise it |
| 4.5 | Dynamic threshold `alpha_c = beta_c · alpha` | **simplified** | `beta_c` is a linear function of fcwnd and buffer occupancy, not the paper's full expression |
| 4.2 | RTO / RACK / TLP computation | **simplified** | multipliers on a base value rather than a full RTT-variance estimator |

## Applications and experiments

| Item | Status | Notes |
|---|---|---|
| Remote disk (4-16 KB reads, up to 1 MB writes) | full | multi-chunk writes accumulate per LBA; integrity checked at every loss rate |
| Incast (N senders, one bottleneck) | full | per-sender goodput reported, not just the mean |
| Bulk transfer vs GBN / SR | full | |
| Loss sweep, reorder sweep | full | multi-seed with 95% CIs |
| Multipath recovery after a path kill | full | 120/120 delivered |
| Scheduler policy comparison | full | |
| CC swap comparison | full | |
| Host congestion / slow receiver | **simplified** | one slow-path factor; no per-queue NIC model |
| Fabric topology, queues, ECMP | **not implemented** | paths are independent links with no shared queueing fabric |
| NIC hardware, DMA, interrupts | **not implemented** | out of scope for a transport-level study |
| Live dashboard / chaos UI | **not implemented** | `dashboard/` is empty; scenarios are run from `experiments/` |

## Known measurement traps

Recorded because each of these produced a wrong number before it was fixed, and each would
quietly produce a wrong number again:

- **Deliveries are not `pkt_recv` events.** Count arrivals whose `accepted` is true. Counting
  every arrival inflates goodput under reordering.
- **A retransmission is spurious only if the PSN was delivered before the retransmission was
  sent.** Comparing `retx` counts against duplicate arrivals gives the wrong answer and can even
  come out negative.
- **Completion time is the last `TEARDOWN`, not the last data packet.** The connection still has
  to tear down.
- **Event records must not alias live state.** A `fae_resp` holding the FAE's actual
  `path_for_flow` dict rewrites its own history on every repath, which silently corrupts any
  timeline read back from the log.
- **One seed is not a result.** Every sweep in `experiments/` runs five seeds and plots a
  confidence interval.
