#!/usr/bin/env bash
# Scenario B: reordering and spurious retransmissions, plus the remote-disk workload.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PORT=${PORT:-8000}
"$PY" scripts/demo_logs.py b
echo
echo "Scenario B written to results/demo/. The interesting log is b_reorder_gbn.jsonl:"
echo "a reordering path makes GBN retransmit data the receiver already had."
echo "  dashboard: http://localhost:$PORT/   (make dashboard)"
