# Event schema

Every log line is one JSON object. Required fields on every event: `t` (sim time, seconds), `type`, `conn`.

Optional common fields: `flow`, `psn`, `rsn`, `path`.

## Packet path

| type | extra fields |
| --- | --- |
| `pkt_send` | `flow`, `psn`, `rsn`, `retx`, `size`, `kind` (`data`/`ack`/`nack`/`probe`/`setup`/`teardown`/`xnack`), `path`, `is_response` |
| `pkt_drop` | `flow`, `psn`, `kind`, `reason` (`random_loss`/`path_killed`/`queue`) |
| `pkt_reorder` | `flow`, `psn`, `extra_delay`, `path` |
| `pkt_recv` | `flow`, `psn`, `ooo`, `kind`, `accepted`, `dup`, `is_response` |
| `ack_send` | `flow`, `base_psn`, `bitmap`, `rx_buf`, `t1`, `t2`, `t3` |
| `ack_recv` | `flow`, `base_psn`, `bitmap` |
| `nack_send` | `psn` |
| `rack_fire` | `psn`, `elapsed` |
| `tlp_fire` | `psn` |

### Reading `pkt_recv` correctly

`pkt_recv` means the packet arrived at the receiver, **not** that it was taken as new data.
Two booleans disambiguate:

- `accepted`: the receiver took this PSN into its buffer for the first time. This is the only
  case that adds to goodput.
- `dup`: the PSN was already delivered or already buffered, so this is a duplicate arrival.
  Falcon-style always re-ACKs these (that is what lets RACK/TLP recover), and Go-Back-N emits
  `accepted: false` for out-of-order packets because it discards them.

Counting `pkt_recv` without filtering on `accepted` inflates goodput for every transport, and
inflates it *most* for the transport that retransmoves the most.

## Connection / FAE / resources / transactions

| type | extra fields |
| --- | --- |
| `conn_state` | `state` (`SETUP`/`ESTABLISHED`/`TEARDOWN`) |
| `fae_event` | `buffer_occ`, `acked`, `t1`, `t2`, `t3`, `t4`, `fabric_delay` |
| `fae_resp` | `fcwnd`, `ncwnd`, `pacing_gap`, `rack_rto`, `tlp_idle`, `alpha`, `path_for_flow` |
| `resource_reserve` | `pool`, `n` |
| `resource_release` | `pool`, `n` |
| `xon` / `xoff` | `reason` |
| `resource_nack` | `pool` |
| `path_kill` / `path_slow` | `path`, `factor` (slow only) |
| `txn_start` / `txn_complete` | `rsn`, `kind` (`push`/`pull`) |
| `ulp_deliver` | `rsn`, `op` |
| `tl_deadlock` | `queued`, `pending_responses` |

`fae_resp` is emitted only when the FAE's parameters actually change, not once per
`fae_event`. A run therefore contains a few hundred `fae_event` records and a much smaller
number of `fae_resp` records, which is what makes the parameter timeline readable.

Example:

```
{"t": 0.001204, "type": "pkt_send", "conn": 1, "flow": 2, "psn": 300, "rsn": 1, "retx": false}
{"t": 0.001410, "type": "pkt_recv",  "conn": 1, "psn": 301, "ooo": true,  "accepted": true,  "dup": false}
{"t": 0.001622, "type": "pkt_recv",  "conn": 1, "psn": 300, "ooo": true,  "accepted": true,  "dup": false}
{"t": 0.001630, "type": "ack_send",  "conn": 1, "base_psn": 299, "bitmap": "0b0110...", "rx_buf": 0.12}
{"t": 0.002100, "type": "rack_fire", "conn": 1, "psn": 300, "elapsed": 0.0009}
{"t": 0.002101, "type": "pkt_send",  "conn": 1, "psn": 300, "retx": true}
{"t": 0.002400, "type": "fae_resp",   "conn": 1, "fcwnd": {"0": 14}, "ncwnd": 40}
{"t": 0.003000, "type": "conn_state", "conn": 1, "state": "ESTABLISHED"}
```
