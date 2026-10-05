#!/usr/bin/env bash
# Scenario C: kill a path mid-transfer and watch the FAE repath its flows.
#
# Runs the failure and the control. The control is the point: without it there is nothing to
# show that the kill cost anything.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PORT=${PORT:-8000}
"$PY" scripts/demo_logs.py c
echo
echo "Scenario C written to results/demo/. Load c_no-kill.jsonl and c_kill1.jsonl side by"
echo "side: the same transfer, with and without a path failure at t=20us."
echo "  dashboard: http://localhost:$PORT/   (make dashboard)"
