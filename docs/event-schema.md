# Event schema

Every log line is one JSON object. Required fields on every event: `t` (sim time, seconds), `type`, `conn`.

Optional common fields: `flow`, `psn`, `rsn`, `path`.

## Packet path

| type | extra fields |
| --- | --- |
| `pkt_send` | `flow`, `psn`, `rsn`, `retx`, `size`, `kind` (`data`/`ack`/`nack`/`probe`/`setup`/`teardown`/`xnack`) |
| `pkt_drop` | `flow`, `psn`, `reason` (`random_loss`/`path_killed`/`queue`) |
| `pkt_reorder` | `flow`, `psn`, `extra_delay` |
| `pkt_recv` | `flow`, `psn`, `ooo`, `kind` |
| `ack_send` | `flow`, `base_psn`, `bitmap`, `rx_buf`, `t1`, `t2`, `t3` |
| `ack_recv` | `flow`, `base_psn`, `bitmap` |
| `nack_send` | `psn` |
| `rack_fire` | `psn`, `elapsed` |
| `tlp_fire` | `psn` |

## Connection / FAE / resources / transactions

| type | extra fields |
| --- | --- |
| `conn_state` | `state` (`SETUP`/`ESTABLISHED`/`TEARDOWN`) |
| `fae_event` | `buffer_occ`, `acked`, `t1`, `t2`, `t3`, `t4`, `fabric_delay` |
| `fae_resp` | `fcwnd`, `ncwnd`, `pacing_gap`, `rack_rto`, `tlp_idle`, `alpha` |
| `resource_reserve` | `pool`, `n` |
| `resource_release` | `pool`, `n` |
| `xon` / `xoff` | `reason` |
| `resource_nack` | `pool` |
| `path_kill` / `path_slow` | `path`, `factor` (slow only) |
| `txn_start` / `txn_complete` | `rsn`, `kind` (`push`/`pull`) |
| `ulp_deliver` | `rsn`, `op` |

Example:

```
{"t": 0.001204, "type": "pkt_send", "conn": 1, "flow": 2, "psn": 300, "rsn": 1, "retx": false}
```
