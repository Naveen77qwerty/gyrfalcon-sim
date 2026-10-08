# Falcon-Style Transport: Project Plan

A feature-level implementation of the ideas in *Falcon: A Reliable, Low Latency Hardware Transport* (SIGCOMM 2025), with a live inner-workings dashboard. Built for the 23CSE302 case study (topic 12) and as a GitHub project.

**Naming rule:** always call it "Falcon-style", never "Falcon implementation". We match the *method and features*, not Google's numbers.

---

## 0. Decisions to lock before writing code

| Decision               | Choice                                                                    | Why                                                                |
| ---------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Core language          | Python 3.11+                                                              | Fastest to build; the protocol logic is the point, not raw speed   |
| Simulation engine      | Own tiny discrete-event loop (heap of timed events)                       | Deterministic, no dependency, easy for Claude Code to reason about |
| Visualizer             | Static HTML/JS replaying a JSONL event log; later a WebSocket "live mode" | Replay first means the dashboard never couples to protocol code    |
| Live backend (Phase 4) | FastAPI + WebSocket                                                       | Needed for the chaos sliders                                       |
| Tests                  | pytest                                                                    | Every phase has acceptance tests                                   |
| Paper in repo          | `docs/falcon-paper.pdf`                                                 | Lets Claude Code cite section numbers                              |

**If you'd rather use Go or Rust:** the plan is identical, but expect roughly 2x the build time. Python is the realistic choice for the deadline.

### Architecture rules (these make the repo look senior)

1. **Protocol code is pure state machines. No I/O, no wall-clock, no randomness inside.** Time and RNG are injected.
2. **Every state change emits an event** to an event bus. The log is the single source of truth for stats, plots, and the dashboard.
3. **Deterministic runs:** same seed gives the same event log. This makes tests and demo scenarios reproducible.
4. **Mechanism vs management split (the paper's Table 3):** the packet-delivery layer (mechanism) never contains congestion-control or timeout *policy*. It sends signals to the FAE module and enforces what comes back.

### Repo layout

```
falcon-style/
  CLAUDE.md                  # rules for Claude Code (see section 7)
  docs/
    falcon-paper.pdf
    PLAN.md                  # this file
    feature-matrix.md        # paper feature -> section -> implemented/simplified/skipped
    event-schema.md
  gyrfalcon/
    core/                    # sim loop, clock, rng, event bus
    net/                     # link/path model: delay, loss, reorder, bandwidth
    baselines/               # go_back_n.py, selective_repeat.py
    pdl/                     # packet delivery layer (PSN, bitmap, RACK-TLP, flows, CC gating)
    tl/                      # transaction layer (RSN, ordering, resources, backpressure)
    ulp/                     # rdma-like and nvme-like mapping
    fae/                     # adaptive engine (events in, parameters out)
    apps/                    # remote_disk.py, incast.py
  experiments/               # one script per scenario/figure
  dashboard/                 # static web UI + live server
  tests/
  results/                   # plots + sample event logs
```

---

## Phase 1: Foundation, Emulator, and Loss Recovery

**Goal:** an impaired network, three transports on top of it, and plots showing the gap. This alone is a valid case-study demo.

### Build

- Event loop, clock, seeded RNG, event bus writing JSONL.
- Path model: one-way delay, jitter, bandwidth/serialization, random loss, reordering (hold a packet back and release it later), and a way to kill or slow a path (used in Phase 3-4).
- **Baselines:**
  - Go-Back-N: receiver drops out-of-order packets; one loss triggers retransmission of the whole outstanding window.
  - Selective Repeat: a NACK per out-of-order packet.
- **Falcon-style reliability (paper section 4.1):**
  - PSN space, receiver tracks a 128-bit bitmap Rx window, bitmap piggybacked on ACKs.
  - Receiver accepts out-of-order packets (buffering).
  - Sender uses **RACK**: ignore packets marked received; ignore packets transmitted after `xmit_ts` (so recovery waits about one RTT); retransmit packets sent before `xmit_ts` whose elapsed time exceeds `rack_rto`. Only applies within the bitmap range.
  - Sender uses **TLP**: after inactivity, retransmit the lowest unacknowledged PSN as a probe to provoke an ACK.
- Stub the timeout parameters as constants for now. In Phase 3 they move into the FAE.
- Metrics from the event log: goodput, retransmissions, **spurious retransmissions**, completion time.

### Experiments		

- `loss_sweep.py`: goodput vs drop rate 0 to 1% for GBN / SR / Falcon-style (mirrors the *trend* of Figure 10).
- `reorder_sweep.py`: goodput vs reorder rate (mirrors Figure 11a).
- `racktlp_vs_oood.py` (optional): RACK-TLP vs a simple out-of-order-distance rule under tail loss (Figure 11b).

### Acceptance checks

- Same seed produces a byte-identical event log.
- With 0% loss/reorder, all three transports deliver everything with 0 retransmissions.
- At 1% loss, Falcon-style goodput stays near the zero-loss value while GBN falls sharply. If it doesn't, the implementation has a bug, not the idea.
- Under pure reordering, Falcon-style shows far fewer spurious retransmissions than GBN/SR.
- Unit tests for bitmap update, RACK eligibility rules, and TLP firing.

### Claude Code prompts

```
Read CLAUDE.md and docs/falcon-paper.pdf section 4.1. Create the repo skeleton from the layout in docs/PLAN.md,
then implement gyrfalcon/core: a deterministic discrete-event loop (heap-based), an injectable clock, a seeded RNG
wrapper, and an event bus that appends JSON lines. Add pytest tests proving two runs with the same seed give
identical logs. No protocol code yet.
```

```
Implement gyrfalcon/net: a Path with one-way delay, jitter, bandwidth serialization, random loss, and reordering
(hold-back-and-release). Paths must be killable/slowable at runtime via a method. Emit events for send, drop,
reorder, deliver. Add tests for loss rate and reorder behavior with fixed seeds.
```

```
Implement gyrfalcon/baselines/go_back_n.py and selective_repeat.py as pure state machines (no I/O, time injected)
sharing one Sender/Receiver interface. Then implement gyrfalcon/pdl reliability per paper section 4.1: PSN, 128-bit
Rx bitmap on ACKs, receiver accepting OOO packets, sender-side RACK and TLP exactly as described (xmit_ts,
rack_rto, lowest-unacked probe). Timeouts are constants for now. Add unit tests for each rule.
```

```
Write experiments/loss_sweep.py and reorder_sweep.py that run all three transports across the sweep with fixed
seeds, compute goodput/retransmissions/spurious retransmissions from the event log, and save PNG plots to
results/. Add an acceptance test asserting Falcon-style beats GBN at 1% loss.
```

---

## Phase 2: Transaction Layer, ULP Mapping, and the Storage Use Case

**Goal:** the part of the paper almost nobody implements: ordering, resource carving, deadlock avoidance, backpressure. Plus the real-life use case teachers will see.

### Build

- **Transactions (section 4.4):** a Push or Pull transaction is a request plus its response. Each transaction is at most **one MTU** (a big read becomes many Pull transactions).
- **ULP mapping (Table 2):**
  - RDMA-like WRITE, SEND/RECV map to Push
  - RDMA-like READ, ATOMICS map to Pull
  - NVMe-like Read maps to Pull; Write maps to Push and Pull
  - Keep the ULP layer thin: it only translates operations into transactions and handles flow control.
- **RSN ordering:** each transaction has a request sequence number. Two connection modes:
  - *Ordered:* buffer out-of-order arrivals, deliver to the ULP in RSN order.
  - *Unordered:* deliver as they arrive.
- **Separate PSN spaces for requests and responses in each direction** (appendix A.1). This is the paper's answer to request-response deadlock; implement it and add a test that would deadlock without it.
- **Resource carving (Figure 6):** pools split Tx vs Rx, then request vs response, and the Rx request pool admits only **head-of-line** requests once occupancy passes a threshold. For ordered connections, a request is HoL when RSN equals last-in-order RSN + 1; in unordered mode all requests are HoL.
- **Resource lifecycle:** the initiator reserves Tx resources for the request *and* Rx resources for the expected response before sending. Rx resources release when delivered to the ULP; Tx resources release on ACK.
- **Backpressure:** Xon/Xoff signal to the ULP when resources run low; "Resource NACK" when incoming traffic can't be accepted.
- **Isolation (section 4.6):** per-connection threshold `T_c = alpha_c * FreeResources` (Dynamic Thresholds style). Start with a static `alpha_c`; the FAE makes it dynamic in Phase 3.
- Optional if time allows: RNR retry and the Complete-in-Error-and-Continue (CIE) notification.

### Application: Remote Disk (the real-life use case)

`apps/remote_disk.py`: a client issues 4 KB to 16 KB random reads and 1 MB writes to a "remote SSD" (a simple block store with an artificial service time) through the emulated network. This is the paper's Near Local Flash scenario in miniature. Report completion time and throughput under loss, per transport.

### Acceptance checks

- Ordered mode: with heavy reordering, the ULP still sees strictly increasing RSNs. Unordered mode: ULP sees arrival order and lower latency.
- A deliberate-deadlock test: with the resource carving disabled the scenario deadlocks; with it enabled it makes progress.
- Backpressure: a slow connection is throttled via Xon/Xoff while a second connection on the same host keeps full throughput.
- Remote-disk data integrity: every block read back equals what was written, across all loss rates.

### Claude Code prompts

```
Read docs/falcon-paper.pdf sections 4.4, 4.5, 4.6 and appendix A. Implement gyrfalcon/tl: Push/Pull transactions
capped at one MTU, RSN assignment, ordered and unordered connection modes, and separate PSN spaces for
requests and responses per direction. Pure state machine, events for every state change. Add tests for in-order
delivery under reordering.
```

```
Implement resource carving in gyrfalcon/tl exactly per Figure 6 and section 4.5: Tx/Rx pools, request/response
sub-pools, HoL-only admission to the Rx request pool beyond a threshold, the reserve-use-release lifecycle
(initiator reserves Tx for the request and Rx for the response), Xon/Xoff to the ULP, and Resource NACKs.
Write a test scenario that deadlocks when carving is disabled and passes when enabled.
```

```
Implement gyrfalcon/ulp with RDMA-like (WRITE, SEND, READ, ATOMIC) and NVMe-like (Read, Write) ops mapped to
Push/Pull per Table 2. Then build apps/remote_disk.py: a client doing random 4-16KB reads and 1MB writes to a
simulated SSD, running over the baselines and over Falcon-style. Verify data integrity under loss and plot
completion time vs loss rate.
```

---

## Phase 3: Multipath, Delay-Based Congestion Control, and the FAE Split

**Goal:** congestion-aware multipathing, Swift-style delay CC, and the mechanism/management split that is the paper's central design principle.

### Build

- **Flows within a connection (section 4.3):** each flow has its own path ID (stand-in for the IPv6 flow label), its own fcwnd, and its own unacked count. All flows share one PSN space and one Rx bitmap.
- **Connection-level gating plus flow-level scheduling:**
  - Transmit only when the connection window opens: `min(ncwnd, aggregate fcwnd)`.
  - Pick the flow with the **largest open window** = `fcwnd_flow - unacked_flow`.
- **Delay measurement without synchronized clocks (section 4.2):** timestamps `t1` (sent), `t2` (arrived at remote), `t3` (ACK sent), `t4` (ACK arrived). Fabric delay = `(t4 - t1) - (t3 - t2)`.
- **Swift-style fcwnd:** additive increase when delay is below target; multiplicative decrease proportional to overshoot, at most once per RTT.
- **ncwnd (NIC/host congestion):** driven by Rx buffer occupancy carried back on ACKs. Add a "slow receiver" knob (artificially slow delivery to the ULP) to exercise it. Pull responses are not subject to ncwnd, because the requester already reserved resources for them.
- **Per-flow ACK handling:** ACKs carry the flow index and the shared Rx bitmap; the sender uses its own state to attribute acked packets to flows. RACK-TLP runs per flow so reordering across paths doesn't cause spurious retransmits.
- **Path repathing (PLB/PRR idea):** move traffic away from congested paths; on outage, change a flow's path.
- **The FAE (Figure 9, Table 3):** a separate module. The datapath sends **events** (connection id, timestamps, buffer occupancy, number acked) and receives **responses** (fcwnd, ncwnd, pacing gap, RTO/RACK/TLP timeouts, flow path assignment, dynamic-threshold alpha). Move *all* policy here:
  - CC algorithm
  - RTO / RACK timeout / TLP threshold computation
  - path (re)assignment
  - `alpha_c = beta_c * alpha`, where `beta_c` rises with fcwnd/ncwnd and falls with fabric delay and buffer occupancy
- **Pluggable CC:** an interface so a second algorithm (for example a simple AIMD) can replace Swift-style without touching `pdl/`. This swap is your demo's party trick.
- **Pacing:** the Timing Wheel becomes simple software pacing using an inter-packet gap from the FAE. List it as "simplified".
- **Incast app (`apps/incast.py`):** N clients write to one server simultaneously.

### Experiments

- `multipath_vs_single.py`: latency and goodput vs offered load (mirrors Figures 15-16).
- `scheduler_policy.py`: largest-open-window vs round-robin (mirrors Figure 17).
- `incast_sweep.py`: latency and fairness as N grows (mirrors Figure 13).
- `host_congestion.py`: slow receiver; show ncwnd dropping and recovering (mirrors Figure 14).
- `cc_swap.py`: same scenario, two CC algorithms, zero changes to `pdl/`.

### Acceptance checks

- `grep` of `pdl/` shows no congestion-control or timeout-policy constants; they come only from FAE responses.
- Multipath sustains higher offered load than single-path in the multi-path topology.
- Killing one path mid-run: the flow on it reroutes and the transfer completes with bounded added latency.
- Swapping the CC module requires editing only `fae/`.
- Fairness in incast: per-sender goodput variance stays small.

### Claude Code prompts

```
Read docs/falcon-paper.pdf sections 3.2, 4.2, 4.3 and Table 3. Extend gyrfalcon/pdl with flows: per-flow path ID,
fcwnd, unacked count, shared PSN space and shared Rx bitmap. Implement connection-level gating by
min(ncwnd, aggregate fcwnd) and flow selection by largest open window. Per-flow RACK-TLP. Add tests for the
scheduling rule and for correct per-flow ack attribution.
```

```
Create gyrfalcon/fae as a separate module with an event-in / response-out interface only (no direct references to
pdl internals). PDL emits events with timestamps t1..t4, buffer occupancy, acked counts; FAE returns fcwnd,
ncwnd, pacing gap, RTO/RACK/TLP timeouts, path assignments, and dynamic-threshold alpha. Move ALL policy out of
pdl and tl into fae. Implement a Swift-style delay CC and a second simple AIMD CC behind one interface.
```

```
Implement the fabric delay formula (t4-t1)-(t3-t2), ncwnd driven by Rx buffer occupancy on ACKs, and path
repathing on congestion/outage. Then build experiments multipath_vs_single, scheduler_policy, incast_sweep,
host_congestion, and cc_swap, each saving plots to results/. Add the acceptance tests from docs/PLAN.md phase 3.
```

---

## Phase 4: Dashboard, Chaos Controls, and Demo Scenarios

**Goal:** show the inside. Teachers should be able to see how a connection is set up, where a loss happened, how it was detected, and what it cost.

### Event schema (define first, in `docs/event-schema.md`)

One JSON object per line, always with `t` (sim time), `type`, `conn`, and type-specific fields:

```
{"t": 0.001204, "type": "pkt_send",   "conn": 1, "flow": 2, "psn": 300, "rsn": 1, "retx": false}
{"t": 0.001410, "type": "pkt_drop",   "conn": 1, "flow": 2, "psn": 300, "reason": "random_loss"}
{"t": 0.001622, "type": "pkt_recv",   "conn": 1, "psn": 301, "ooo": true}
{"t": 0.001630, "type": "ack_send",   "conn": 1, "flow": 0, "base_psn": 299, "bitmap": "0b0110...", "rx_buf": 0.12}
{"t": 0.002100, "type": "rack_fire",  "conn": 1, "psn": 300, "elapsed": 0.0009}
{"t": 0.002101, "type": "pkt_send",   "conn": 1, "flow": 1, "psn": 300, "retx": true}
{"t": 0.002400, "type": "fae_resp",   "conn": 1, "fcwnd": {"0": 14, "1": 22}, "ncwnd": 40}
{"t": 0.003000, "type": "conn_state", "conn": 1, "state": "ESTABLISHED"}
```

Include events for resource reserve/release, Xon/Xoff, path kill/slow, and transaction start/complete.

### Connection lifecycle (our own design)

The paper doesn't specify a handshake. Design a simple one: SETUP, flow/path assignment, ESTABLISHED, TEARDOWN. State this openly in the README and feature matrix as "own design".

### Dashboard panels

1. **Connection panel:** state, PSN/RSN counters, mode (ordered/unordered), flows with per-flow fcwnd and unacked.
2. **Packet ladder (like Figure 5):** sender left, receiver right; arrows per packet; drops die mid-flight in red; reordered packets cross; retransmits in a distinct color.
3. **Loss recovery view:** the ACK bitmap as a grid of cells (received / missing / retransmitted). The story arc to show: hole appears, RACK or TLP timer fires, retransmit, hole fills.
4. **Stats panel:** goodput, retransmissions, spurious retransmissions, fabric delay, fcwnd and ncwnd over time, Rx buffer occupancy, per-path load.
5. **Resource panel:** the Tx/Rx x request/response pools filling and draining, with Xon/Xoff markers.
6. **Chaos controls (live mode):** loss and reorder sliders, kill/slow a path, switch transport (GBN / SR / Falcon-style), pause/step/replay speed.

### Build order

1. Replay mode: load a JSONL file, play/pause/scrub. Panels 1-4.
2. Resource panel.
3. Live mode: FastAPI + WebSocket streams events from a running sim; sliders post parameter changes.
4. Side-by-side mode: two transports on identical impairments (same seed), synced.

### Scripted demo scenarios (each is a one-command run)

- **Scenario A, "1% loss":** GBN vs Falcon-style, side by side, on the remote-disk workload.
- **Scenario B, "reordering":** show spurious retransmissions on SR/GBN and almost none on Falcon-style.
- **Scenario C, "path failure":** kill a path mid-transfer; watch the flow reroute.
- **Scenario D, "incast":** 50 senders to one server; show backpressure and fair sharing.

### Acceptance checks

- Dashboard replays any log produced by Phases 1-3 with no changes to protocol code.
- Scenario scripts run end to end with fixed seeds and produce the same visuals every time.
- A recorded screen capture exists for each scenario (backup for the presentation).

### Claude Code prompts

```
Write docs/event-schema.md defining every event type emitted so far (pkt_send/drop/recv, ack_send, rack_fire,
tlp_fire, fae_event/fae_resp, resource_reserve/release, xon/xoff, path_kill/slow, txn_start/complete,
conn_state). Audit the codebase so every state change emits a schema-conformant event, and add a validator test
that checks a sample log against the schema.
```

```
Build dashboard/ as a static HTML+JS app (no build step) that loads a JSONL event log and replays it with
play/pause/scrub/speed. Implement panels: connection, packet ladder (SVG, drops in red, retransmits in a
distinct color), ACK-bitmap grid, and stats charts. Keep it driven purely by the event log.
```

```
Add live mode: a FastAPI server that runs a scenario and streams events over WebSocket, with endpoints to
change loss/reorder, kill/slow a path, and switch transport. Add a side-by-side view running two transports on
the same seed. Create scripts/demo_a.sh to demo_d.sh for the four scenarios in docs/PLAN.md.
```

---

## Phase 5: Hardening, Documentation, and Presentation

**Goal:** make it credible for GitHub and bulletproof for the presentation.

### Build

- **Feature matrix (`docs/feature-matrix.md`):** a table of paper feature, paper section, status (implemented / simplified / skipped), and notes. Initial draft:

| Feature                                                  | Section  | Status                                          |
| -------------------------------------------------------- | -------- | ----------------------------------------------- |
| Push/Pull transactions, MTU-sized                        | 4.4      | Implemented                                     |
| Ordered/unordered connections, RSN                       | 4.4, A.2 | Implemented                                     |
| Separate req/resp PSN spaces                             | A.1      | Implemented                                     |
| 128-bit bitmap ACKs, RACK-TLP                            | 4.1      | Implemented                                     |
| Multipath, per-flow fcwnd, largest-open-window scheduler | 4.3      | Implemented                                     |
| Swift-style delay CC, fcwnd + ncwnd                      | 4.2      | Implemented (simplified constants)              |
| Resource carving, HoL admission, lifecycle               | 4.5      | Implemented (counters instead of SRAM)          |
| Dynamic-threshold isolation, Xon/Xoff                    | 4.6      | Implemented                                     |
| FAE event/response split                                 | 5.3      | Implemented (no real CPU cores, no prefetching) |
| Timing Wheel pacing                                      | 3.2      | Simplified (software pacing)                    |
| RNR retry, CIE error notification                        | 4.4      | Optional                                        |
| PSP/IPsec encryption, inline timestamps                  | 5.1      | Skipped (timestamps taken from sim clock)       |
| Connection handshake                                     | n/a      | Own design                                      |
| NVMe command set details                                 | 6.3      | Simplified (block read/write only)              |
| On-NIC cache, connection-cache cliff, die area           | 5.2      | Skipped (hardware-specific)                     |

- **UDP backend (stretch):** same state machines driven by real sockets; demo a file transfer through `tc netem` on Linux. Only start this if Phases 1-4 are solid.
- **Quality:** type hints, docstrings that cite paper sections, 80%+ test coverage on `pdl/`, `tl/`, `fae/`, CI via GitHub Actions running pytest.
- **README:** one-paragraph pitch, an architecture diagram mapped to Figure 2, a GIF of the dashboard, quickstart (`make demo-a`), results plots, the feature matrix, honest "what this is not" section (not Google's code, not hardware, numbers not comparable).
- **Presentation (20 min):** 3 min problem (why RoCE struggles on lossy Ethernet), 4 min architecture mapped to your code, 6 min live demo (scenarios A, B, C), 2 min feature matrix, 5 min Q&A. Prepare answers for: "Why not just use TCP?", "What if you had real hardware?", "What did you simplify and why?", "How do you know it's correct?" (answer: deterministic replays plus tests).
- **Report tie-in:** each report section can reference a concrete plot or dashboard screenshot from the repo. Write the analysis text yourselves, since AI-generated reports are not allowed.

### Claude Code prompts

```
Generate docs/feature-matrix.md from the table in docs/PLAN.md, but verify each row against the actual code and
mark anything that differs. Add a GitHub Actions workflow running pytest, and a Makefile with targets
demo-a..demo-d, test, and plots.
```

```
Write the README: pitch, architecture diagram (Mermaid) mapped to the paper's Figure 2 layers, quickstart,
embedded result plots from results/, the feature matrix, and an explicit 'limitations' section. Do not claim
this is Falcon or that numbers match the paper.
```

```
(Stretch) Add a UDP backend that drives the same pdl/tl/fae state machines through real sockets, and a
scripts/netem_demo.sh that transfers a file under tc netem loss and reorder. State machines must not change.
```

---

## 6. Working split for a team of 5 (optional)

| Owner | Scope                                                                |
| ----- | -------------------------------------------------------------------- |
| A     | `core/`, `net/`, baselines, Phase 1 experiments                  |
| B     | `pdl/` reliability (bitmap, RACK-TLP), then flows/multipath        |
| C     | `tl/` and `ulp/`, resource carving, remote-disk app              |
| D     | `fae/`, congestion control, incast and host-congestion experiments |
| E     | event schema, dashboard, demo scripts, README and slides             |

Rule: the event schema (Phase 4 section) is agreed on day one, so E can build against sample logs while everyone else builds the engine.

## 7. CLAUDE.md (put this in the repo root)

```
# Project rules
- This is a Falcon-style transport (SIGCOMM 2025 paper in docs/). Never call it "Falcon" or claim parity with
  the paper's numbers.
- Protocol code (core logic in pdl/, tl/, fae/, baselines/) is pure: no I/O, no wall-clock, no global RNG. Time
  and RNG are injected.
- Every state change emits an event conforming to docs/event-schema.md.
- Mechanism lives in pdl/ and tl/. Policy (CC, timeouts, path choice, DT alpha) lives only in fae/.
- Same seed must give identical event logs. Add a test when adding randomness.
- When implementing a paper feature, cite the section in the docstring. If you simplify something, add it to
  docs/feature-matrix.md.
- Write tests with each feature. Run pytest before declaring a task done.
- Ask before adding dependencies.
```

## 8. How to drive Claude Code through this

1. Create the repo, add the paper PDF and this file under `docs/`, add CLAUDE.md, and commit.
2. Work one prompt at a time, in order. Run the tests after each. Don't batch a phase into one prompt.
3. After each phase, ask Claude Code to re-read the paper section and review its own implementation against it. Fix mismatches before moving on.
4. Commit at the end of every phase with the plots in `results/`, so your GitHub history shows steady progress.
5. If a phase runs long, ship what passes its acceptance checks and move the rest to a "Roadmap" section in the README. A smaller, correct repo beats a bigger broken one.

## 9. Priority if time gets tight

1. **Must have for the case study:** Phase 1 complete, Phase 2 remote-disk demo, a replay-mode dashboard with the packet ladder and bitmap view.
2. **Strong:** Phase 3 multipath + FAE split.
3. **Nice:** live mode, side-by-side, UDP backend.
