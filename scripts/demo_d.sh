#!/usr/bin/env bash
# Scenario D: 50 senders sharing one bottleneck link. Fairness, not throughput.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PORT=${PORT:-8000}
"$PY" scripts/demo_logs.py d
echo
echo "Scenario D written to results/demo/. The number that matters is the spread across"
echo "senders, not the aggregate: d_incast.jsonl should show every sender getting the same."
echo "  dashboard: http://localhost:$PORT/   (make dashboard)"
