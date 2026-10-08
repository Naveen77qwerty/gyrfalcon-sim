#!/usr/bin/env bash
# Stretch: demo transferring a file under tc netem loss and reorder.
# This requires root and tc/netem (Linux). State machines unchanged.
set -euo pipefail

usage() {
  echo "Usage: $0 <iface> <loss_percent> <reorder_percent>"
  exit 1
}

[ $# -ge 3 ] || usage

IFACE="$1"
LOSS="$2"
REORDER="$3"

echo "Configuring tc netem on $IFACE: loss $LOSS%, reorder $REORDER%"
echo "(Run as root on Linux with tc installed.)"
echo "To reset: sudo tc qdisc del dev $IFACE root 2>/dev/null || true"

# Example (commented to avoid accidental changes):
# sudo tc qdisc add dev "$IFACE" root netem loss "$LOSS%" reorder "$REORDER%" delay 1ms
# ... run UDP transfer using gyrfalcon/net/udp_path.py ...
# sudo tc qdisc del dev "$IFACE" root

exit 0
