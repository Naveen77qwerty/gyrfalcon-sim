"""Dashboard checks that need no browser.

Two things are verified here: that `dashboard/app.js` is syntactically valid, and that its
log model produces the right answers on a real log. The panels themselves are canvas-free
SVG driven by that model, so a model bug shows up as a wrong panel and this is where it gets
caught.

Node is optional. When it is missing these tests skip rather than fail, because the
dashboard is a static asset and should not be a hard build dependency.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "app.js"
INDEX_HTML = ROOT / "dashboard" / "index.html"
NODE = shutil.which("node") or shutil.which("nodejs")

requires_node = pytest.mark.skipif(NODE is None, reason="node not installed")

HARNESS = r"""
const fs = require("fs");
// The panel code only touches the DOM from the DOMContentLoaded handler, so a stub window is
// enough to load app.js and exercise the log model headlessly.
global.window = { addEventListener() {} };
global.document = {
  getElementById: () => null,
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, classList: { add() {} } }),
  createElementNS: () => ({ setAttribute() {}, appendChild() {}, style: {} }),
};
const src = fs.readFileSync(process.argv[2], "utf8");
eval(src + "\nglobal.__parseLog = parseLog; global.__Log = Log;");

const log = global.__parseLog(fs.readFileSync(process.argv[3], "utf8"), "test.jsonl");
const out = {
  events: log.events.length,
  conns: log.conns,
  t0: log.t0,
  t1: log.t1,
  flights: log.flights.map((f) => [f.conn, f.psn, f.retx, f.fate, f.kind]),
  sends: log.sends.length,
  dataSends: log.dataSends.length,
  recvs: log.recvs.length,
  drops: log.drops.length,
};
process.stdout.write(JSON.stringify(out));
"""


def _harness(tmp_path: Path) -> Path:
    path = tmp_path / "harness.js"
    path.write_text(HARNESS)
    return path


def _load_model(log_path: Path, tmp_path: Path) -> dict:
    proc = subprocess.run(
        [NODE, str(_harness(tmp_path)), str(APP_JS), str(log_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@requires_node
def test_app_js_is_valid_javascript():
    proc = subprocess.run([NODE, "--check", str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_every_element_id_the_script_looks_up_exists_in_the_html():
    """`getElementById` returning null is the classic silent failure in a no-build app: the
    page loads, the controls do nothing, and nothing in the console says why."""
    html = INDEX_HTML.read_text()
    import re

    ids = set(re.findall(r'id="([^"]+)"', html))
    script = APP_JS.read_text()
    for wanted in set(re.findall(r'getElementById\("([^"]+)"\)', script)):
        assert wanted in ids, f"app.js looks up #{wanted}, which index.html does not define"
    for wanted in set(re.findall(r'url\(#([^)]+)\)', html)):
        assert f'"{wanted}"' in script or f"#{wanted}" in script, f"CSS references #{wanted}, unused"


@requires_node
def test_dashboard_model_reads_a_real_log(tmp_path):
    from gyrfalcon.apps.incast import run_incast

    log_path = tmp_path / "incast.jsonl"
    run_incast(n_senders=4, seed=1, n_packets=25, until=0.5).bus.write_jsonl(log_path)

    model = _load_model(log_path, tmp_path)
    assert model["events"] > 0
    assert model["conns"] == [1, 2, 3, 4]
    assert model["dataSends"] == model["recvs"] or model["recvs"] > 0
    assert model["t1"] >= model["t0"]
    assert len(model["flights"]) == model["dataSends"], "every data transmission must become one flight"


@requires_node
def test_dashboard_does_not_confuse_an_ack_with_a_data_packet(tmp_path):
    """ACKs and data share one PSN space in the log. Matching a drop on PSN alone makes an
    ACK that went missing look like lost application data."""
    from gyrfalcon.harness import run_bulk

    log_path = tmp_path / "loss.jsonl"
    run_bulk("falcon", seed=11, n_packets=150, loss=0.01, until=2.0).bus.write_jsonl(log_path)

    events = _events(log_path)
    shared = _psn_space_collisions(events)
    assert shared, "expected ACK and data PSNs to overlap in this log"

    model = _load_model(log_path, tmp_path)
    assert len(model["flights"]) == model["dataSends"]
    assert all(f[4] != "ack" for f in model["flights"]), "an ACK was tracked as a data flight"


def _events(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _psn_space_collisions(events: list[dict]) -> set[int]:
    data = {e["psn"] for e in events if e["type"] == "pkt_send" and e["kind"] == "data"}
    ack = {e["psn"] for e in events if e["type"] == "pkt_send" and e["kind"] == "ack"}
    return data & ack


@requires_node
def test_dashboard_pairs_each_packet_with_its_own_fate(tmp_path):
    """A retransmission is a new attempt, not a second copy of the first flight. Collapsing
    them makes every recovery look like a first-attempt success."""
    from gyrfalcon.harness import run_bulk

    log_path = tmp_path / "loss.jsonl"
    run_bulk("falcon", seed=11, n_packets=150, loss=0.01, until=2.0).bus.write_jsonl(log_path)

    model = _load_model(log_path, tmp_path)
    fates = {f[3] for f in model["flights"]}
    assert model["drops"] > 0, "expected loss in this scenario"
    assert "dropped" in fates
    assert "delivered" in fates
    assert any(f[2] for f in model["flights"]), "expected retransmissions"
    # A retransmitted PSN must appear once per attempt, not be collapsed into its original.
    psns = [f[1] for f in model["flights"]]
    retx_psns = _retx_psns(log_path)
    repeated = {p for p in psns if psns.count(p) > 1}
    assert repeated, "retransmissions were collapsed into their original flight"
    assert repeated <= retx_psns, f"PSNs repeated without a retransmission: {repeated - retx_psns}"


def _retx_psns(log_path: Path) -> set[int]:
    """PSNs the log shows being retransmitted."""
    return {e["psn"] for e in _events(log_path) if e["type"] == "pkt_send" and e.get("retx")}


CHROME = next((b for b in ("google-chrome", "chromium", "chromium-browser") if shutil.which(b)), None)
RENDER_CHECK = ROOT / "scripts" / "render_check.js"

requires_browser = pytest.mark.skipif(
    CHROME is None or NODE is None,
    reason="needs node and a chrome/chromium binary for a real render",
)


@requires_browser
@pytest.mark.slow
def test_every_panel_renders_for_every_demo_log(tmp_path):
    """Loads index.html in headless Chrome, replays each demo log through the file input, and
    asserts every panel ends up populated.

    `node --check` only proves the file parses. A wrong variable in a render path leaves a
    blank panel with nothing in the console, which is exactly the failure this catches.
    """
    from gyrfalcon.apps.incast import run_incast
    from gyrfalcon.apps.remote_disk import run_remote_disk
    from gyrfalcon.harness import run_bulk, run_bulk_multipath

    logs = {}
    for name, sim in [
        ("a_falcon", run_bulk("falcon", seed=21, n_packets=80, loss=0.01, until=1.0)),
        ("a_gbn", run_bulk("gbn", seed=21, n_packets=80, loss=0.01, until=1.0)),
        ("b_reorder_sr", run_bulk("sr", seed=13, n_packets=80, reorder=0.4, until=1.0)),
        ("b_disk", run_remote_disk(seed=13, loss=0.0, n_writes=1, n_reads=3, until=1.0)[0]),
        # Has a path_kill, which is emitted with conn=0 and used to break the per-connection
        # panels by making them pick a connection that never sent anything.
        ("c_kill", run_bulk_multipath(seed=5, n_packets=60, n_paths=3, n_flows=3, until=0.05, kill_path=1, kill_at=2e-5)),
        ("d_incast", run_incast(n_senders=6, seed=1, n_packets=20, until=0.4)),
    ]:
        path = tmp_path / f"{name}.jsonl"
        sim.bus.write_jsonl(path)
        logs[name] = path

    proc = subprocess.run(
        [NODE, str(RENDER_CHECK), *[str(p) for p in logs.values()]],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"render check failed:\n{proc.stdout}\n{proc.stderr}"
    for name in logs:
        assert f"ok   {name}.jsonl" in proc.stdout, f"{name} did not render:\n{proc.stdout}"


@requires_node
def test_dashboard_survives_a_log_with_no_events_of_a_kind(tmp_path):
    """The panels must render on a GBN log, which has no FAE and no resource pools."""
    from gyrfalcon.harness import run_bulk

    log_path = tmp_path / "gbn.jsonl"
    run_bulk("gbn", seed=2, n_packets=30, loss=0.02, until=1.0).bus.write_jsonl(log_path)

    model = _load_model(log_path, tmp_path)
    assert model["events"] > 0
    assert all(f[3] in {"delivered", "dropped", "reordered", "inflight"} for f in model["flights"])
    assert model["flights"], "no flights built"
