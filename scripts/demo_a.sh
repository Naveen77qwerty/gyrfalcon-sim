#!/usr/bin/env bash
# Scenario A: 1% loss, GBN vs Falcon-style on one path.
#
# Regenerates the logs, opens the dashboard, and leaves the two logs side by side so the
# spurious-retransmission difference is visible rather than asserted.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PORT=${PORT:-8000}
"$PY" scripts/demo_logs.py a
echo
echo "Scenario A written to results/demo/. Comparing GBN against Falcon-style?"
echo "  load a_1pct_gbn.jsonl and a_1pct_falcon.jsonl, then set view to 'side by side'."
echo "  dashboard: http://localhost:$PORT/   (make dashboard)"
