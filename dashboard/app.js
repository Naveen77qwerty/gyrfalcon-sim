/* Event-log replay dashboard.
 *
 * Reads JSONL logs produced by `EventBus.write_jsonl` and rebuilds every panel from them.
 * There is no protocol logic here and no second source of truth: if a panel shows something,
 * it came out of a log. That is what makes a recorded log reviewable, and it is why the same
 * files drive the tests.
 *
 * Layout is N independent `Side` instances sharing one clock, so "side by side" is just two
 * logs rendered against the same simulated time. Nothing is re-simulated; scrubbing is a fold
 * over an event prefix.
 */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ log model */

/** One parsed log plus the derived views the panels need. */
class Log {
  constructor(name, events) {
    this.name = name;
    this.setEvents(events);
  }

  setEvents(events) {
    this.events = events;
    this.t0 = events.length ? events[0].t : 0;
    this.t1 = events.length ? events[events.length - 1].t : 0;
    this.reindex();
  }

  /** Recompute the cached views. Called on load and after each live batch. */
  reindex() {
    this.sends = this.events.filter((e) => e.type === "pkt_send");
    this.dataSends = this.sends.filter((e) => e.kind !== "ack" && e.kind !== "nack");
    this.recvs = this.events.filter((e) => e.type === "pkt_recv");
    this.drops = this.events.filter((e) => e.type === "pkt_drop" && e.kind !== "ack" && e.kind !== "nack");
    this.conns = [...new Set(this.events.map((e) => e.conn))].sort((a, b) => a - b);
    this.buildFlights();
  }

  /**
   * Join each transmission to its fate: delivered, dropped, reordered, or still in flight.
   *
   * Two traps this has to avoid. Matching on PSN alone is wrong because ACK and data share
   * one PSN space in the log, so a lost ACK would be blamed on the datapath -- drops are
   * matched on (conn, kind, psn) too. And a retransmission of the same PSN is a distinct
   * flight, not a second copy of the first, so each send opens its own flight. Collapsing
   * them makes every recovery look like a first-attempt success.
   */
  buildFlights() {
    this.flights = [];
    const isFlight = (e) => e.kind !== "ack" && e.kind !== "nack";
    const match = (f, e) => f.conn === e.conn && f.psn === e.psn && f.kind === e.kind;

    for (const ev of this.events) {
      if (ev.type === "pkt_send") {
        if (!isFlight(ev)) continue;
        this.flights.push({
          conn: ev.conn, psn: ev.psn, flow: ev.flow, path: ev.path,
          kind: ev.kind, isResp: !!ev.is_response,
          retx: !!ev.retx, tSend: ev.t, tEnd: null, fate: "inflight",
        });
      } else if (ev.type === "pkt_drop") {
        // Newest still-open flight for this PSN is the one that died.
        for (let i = this.flights.length - 1; i >= 0; i--) {
          const f = this.flights[i];
          if (match(f, ev) && f.fate === "inflight") { f.fate = "dropped"; f.tEnd = ev.t; break; }
        }
      } else if (ev.type === "pkt_recv") {
        for (let i = this.flights.length - 1; i >= 0; i--) {
          const f = this.flights[i];
          if (match(f, ev) && f.fate === "inflight") {
            f.fate = ev.accepted ? "delivered" : "reordered";
            f.tEnd = ev.t;
            break;
          }
        }
      }
    }
  }

  /** Index of the last event at or before `t`, or -1. Binary search; logs are time-ordered. */
  indexAt(t) {
    let lo = 0, hi = this.events.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.events[mid].t <= t) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return best;
  }

  upto(t) { return this.events.slice(0, this.indexAt(t) + 1); }

  /**
   * The connection to show in the per-connection panels: whichever sent the most data.
   *
   * Not `conns[0]`. Events that are not connection-scoped -- `path_kill`, `path_slow` -- are
   * emitted with `conn: 0`, so any log containing a path event puts 0 first and every
   * per-connection panel then renders empty.
   */
  get primaryConn() {
    const counts = new Map();
    for (const e of this.dataSends) counts.set(e.conn, (counts.get(e.conn) || 0) + 1);
    if (!counts.size) return this.conns[0];
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0][0];
  }
}

/* --------------------------------------------------------------- log ingestion */

function parseLog(text, name) {
  const events = [];
  let bad = 0;
  for (const line of text.split("\n")) {
    const s = line.trim();
    if (!s) continue;
    try {
      const ev = JSON.parse(s);
      if (typeof ev.t === "number" && typeof ev.type === "string") events.push(ev);
      else bad++;
    } catch (e) { bad++; }
  }
  // A log must be time-ordered to be replayable. Sorting here would hide a broken emitter
  // rather than surface it, so warn and keep the order we were given.
  const ordered = events.every((e, i) => i === 0 || e.t >= events[i - 1].t);
  if (!ordered) console.warn(`${name}: log is not in non-decreasing time order`);
  if (bad) console.warn(`${name}: skipped ${bad} unparseable line(s)`);
  return new Log(name, events);
}

/* --------------------------------------------------------------- draw helpers */

function el(tag, attrs, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function h(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function fmt(t) { return t.toFixed(6); }

function mbps(bytes, seconds) {
  if (!seconds) return "0";
  return ((bytes * 8) / seconds / 1e6).toFixed(1);
}

/* ------------------------------------------------------- one side of the layout */

const LADDER_WINDOW = 0.004; // seconds of log visible in the ladder

class Side {
  /** @param root  a `.side` element cloned from #side-template */
  constructor(root, view) {
    this.root = root;
    this.view = view;
    this.q = (sel) => root.querySelector(sel);
    this.connBody = this.q(".conn-body");
    this.ladder = this.q(".ladder");
    this.bitmap = this.q(".bitmap");
    this.cards = this.q(".stat-cards");
    this.charts = this.q(".charts");
    this.pools = this.q(".pools");
    this.stream = this.q(".stream");
    this.nameEl = this.q(".side-name");
    this.metaEl = this.q(".side-meta");
  }

  render(log, t) {
    const upto = log.upto(t);
    this.nameEl.textContent = log.name;
    this.metaEl.textContent = `${log.events.length} events  ${fmt(log.t0)}-${fmt(log.t1)} s`;
    this.renderConn(log, upto, t);
    this.renderLadder(log, t);
    this.renderBitmap(upto);
    this.renderStats(log, upto, t);
    this.renderPools(log, upto, t);
    this.renderStream(upto);
  }

  renderConn(log, upto, now) {
    const body = this.connBody;
    clear(body);
    if (!upto.length) { body.className = "conn-body empty"; body.textContent = "No events yet."; return; }
    body.className = "conn-body";

    const states = upto.filter((e) => e.type === "conn_state");
    const state = states.length ? states[states.length - 1].state : "-";
    const fae = upto.filter((e) => e.type === "fae_resp");
    const latest = fae[fae.length - 1];

    const table = h("table");
    const stateRow = h("tr");
    stateRow.appendChild(h("th", null, "state"));
    const stateCell = h("td");
    stateCell.appendChild(h("span", `pill ${state}`, state));
    stateRow.appendChild(stateCell);
    table.appendChild(stateRow);

    const rows = [
      ["events replayed", upto.length],
      ["connections", new Set(upto.map((e) => e.conn)).size],
      ["sim time", `${fmt(now)} s`],
    ];
    if (latest) {
      rows.push(["flows", Object.keys(latest.fcwnd || {}).length]);
      rows.push(["scheduler", latest.scheduler || "largest_open"]);
      rows.push(["path per flow", JSON.stringify(latest.path_for_flow || {})]);
    }
    for (const [k, v] of rows) {
      const tr = h("tr");
      tr.appendChild(h("th", null, k));
      tr.appendChild(h("td", "num", String(v)));
      table.appendChild(tr);
    }
    body.appendChild(table);

    if (latest) {
      const ft = h("table");
      const head = h("tr");
      ["flow", "fcwnd", "path", "sent", "retx"].forEach((k) => head.appendChild(h("th", null, k)));
      ft.appendChild(head);
      for (const f of Object.keys(latest.fcwnd || {})) {
        const sent = log.sends.filter((e) => String(e.flow) === String(f) && e.t <= now).length;
        const retx = log.sends.filter((e) => String(e.flow) === String(f) && e.t <= now && e.retx).length;
        const tr = h("tr");
        tr.appendChild(h("td", null, f));
        tr.appendChild(h("td", "num", Number(latest.fcwnd[f]).toFixed(1)));
        tr.appendChild(h("td", "num", String((latest.path_for_flow || {})[f] ?? "-")));
        tr.appendChild(h("td", "num", String(sent)));
        tr.appendChild(h("td", "num", String(retx)));
        ft.appendChild(tr);
      }
      body.appendChild(ft);
    }
  }

  renderLadder(log, now) {
    const svg = this.ladder;
    clear(svg);
    const W = 1000, H = 260;
    const ySend = 70, yRecv = 190;
    const span = LADDER_WINDOW;
    const lo = now - span * 0.35;
    const hi = now + span * 0.65;
    const x = (t) => ((t - lo) / (hi - lo)) * W;

    el("line", { x1: 0, y1: ySend, x2: W, y2: ySend, class: "lane" }, svg);
    el("line", { x1: 0, y1: yRecv, x2: W, y2: yRecv, class: "lane" }, svg);
    el("text", { x: 4, y: ySend - 8, class: "lane-label" }, svg).textContent = "sender";
    el("text", { x: 4, y: yRecv + 18, class: "lane-label" }, svg).textContent = "receiver";

    for (const f of log.flights) {
      if (f.tSend > hi) break;
      if ((f.tEnd !== null && f.tEnd < lo) || (f.tEnd === null && f.tSend < lo - span)) continue;
      const end = f.tEnd === null ? Math.min(f.tSend + span * 0.2, hi) : f.tEnd;
      const cls = f.retx ? "retx" : (f.isResp || f.kind !== "data" ? "resp" : "data");
      el("line", {
        x1: x(f.tSend), y1: ySend, x2: x(end), y2: yRecv,
        class: `flowline ${cls}`,
        opacity: f.fate === "inflight" ? 0.45 : 1,
      }, svg);
      if (f.fate === "dropped") {
        const mx = (x(f.tSend) + x(end)) / 2, my = (ySend + yRecv) / 2;
        el("line", { x1: mx - 4, y1: my - 4, x2: mx + 4, y2: my + 4, class: "dropx" }, svg);
        el("line", { x1: mx + 4, y1: my - 4, x2: mx - 4, y2: my + 4, class: "dropx" }, svg);
      }
      if (f.fate === "reordered") {
        el("circle", { cx: x(end), cy: yRecv, r: 2.5, fill: "var(--retx)" }, svg);
      }
    }

    // Timer markers: the moment recovery actually started.
    for (const e of log.events) {
      if (e.t > hi) break;
      if (e.type !== "rack_fire" && e.type !== "tlp_fire") continue;
      const px = x(e.t);
      el("line", { x1: px, y1: ySend - 14, x2: px, y2: yRecv + 14, class: `marker ${e.type === "rack_fire" ? "rack" : "tlp"}` }, svg);
      const t = el("text", { x: px + 3, y: ySend - 18, class: "chart-label" }, svg);
      t.textContent = e.type === "rack_fire" ? `RACK psn ${e.psn}` : `TLP psn ${e.psn}`;
    }

    el("line", { x1: x(now), y1: 8, x2: x(now), y2: H - 8, class: "playhead" }, svg);
    el("text", { x: 6, y: H - 4, class: "axis-label" }, svg).textContent = `${fmt(lo)} s`;
    el("text", { x: W - 6, y: H - 4, "text-anchor": "end", class: "axis-label" }, svg).textContent = `${fmt(hi)} s`;
  }

  renderBitmap(upto) {
    const box = this.bitmap;
    clear(box);
    const conn = upto.length ? this.view.connOf(upto) : null;
    const recv = new Map();
    for (const e of upto) {
      if (e.type !== "pkt_recv" || e.conn !== conn) continue;
      const k = `${e.conn}:${e.psn}`;
      if (e.accepted && !recv.has(k)) recv.set(k, e.t);
    }
    const retxPsn = new Set(upto.filter((e) => e.type === "pkt_send" && e.retx && e.conn === conn).map((e) => `${e.conn}:${e.psn}`));
    const timerPsn = new Set(upto.filter((e) => (e.type === "rack_fire" || e.type === "tlp_fire") && e.conn === conn).map((e) => `${e.conn}:${e.psn}`));
    const sent = upto.filter((e) => e.type === "pkt_send" && e.kind !== "ack" && e.kind !== "nack" && e.conn === conn);

    if (!sent.length) {
      box.className = "bitmap empty";
      box.textContent = "No packets sent yet.";
      return;
    }
    box.className = "bitmap";

    const base = Math.min(...sent.map((e) => e.psn));
    const top = Math.min(base + 64, Math.max(...sent.map((e) => e.psn)) + 1);
    for (let p = base; p < top; p++) {
      const k = `${conn}:${p}`;
      const cell = h("div", "cell", String(p));
      cell.title = `psn ${p}`;
      if (recv.has(k)) cell.classList.add("recv");
      else if (timerPsn.has(k)) cell.classList.add("rack");
      else if (retxPsn.has(k)) cell.classList.add("retx");
      else if (sent.some((e) => e.psn === p)) cell.classList.add("hole");
      box.appendChild(cell);
    }

    const legend = h("div", "legend");
    for (const [color, label] of [
      ["rgba(61,220,151,0.5)", "received"],
      ["transparent", "not yet sent"],
      ["rgba(255,92,92,0.35)", "timer fired, hole open"],
      ["rgba(255,176,46,0.4)", "retransmitted"],
    ]) {
      const span = h("span");
      const swatch = h("i");
      swatch.style.background = color;
      swatch.style.border = "1px solid var(--line)";
      span.appendChild(swatch);
      span.appendChild(document.createTextNode(label));
      legend.appendChild(span);
    }
    box.appendChild(legend);
  }

  renderStats(log, upto, now) {
    const cards = this.cards;
    clear(cards);
    const elapsed = Math.max(1e-9, now - log.t0);

    const accepted = new Set();
    let bytes = 0, drops = 0, retx = 0, spurious = 0, dups = 0;
    for (const e of upto) {
      if (e.type === "pkt_send") {
        if (e.retx) {
          retx++;
          if (accepted.has(`${e.conn}:${e.psn}`)) spurious++;
        }
      } else if (e.type === "pkt_recv") {
        if (e.dup) dups++;
        if (e.accepted) {
          const k = `${e.conn}:${e.psn}`;
          if (!accepted.has(k)) { accepted.add(k); bytes += e.size || 1500; }
        }
      } else if (e.type === "pkt_drop" && e.kind !== "ack" && e.kind !== "nack") {
        drops++;
      }
    }
    const done = new Set(upto.filter((e) => e.type === "conn_state" && e.state === "TEARDOWN").map((e) => e.conn));

    for (const [k, v, cls] of [
      ["goodput", `${mbps(bytes, elapsed)} Mbps`, ""],
      ["delivered", String(accepted.size), "good"],
      ["drops", String(drops), drops ? "warn" : ""],
      ["retransmits", String(retx), retx ? "warn" : ""],
      ["spurious", String(spurious), spurious ? "bad" : "good"],
      ["duplicate arrivals", String(dups), dups ? "warn" : ""],
      ["teardowns", String(done.size), ""],
      ["elapsed", `${(elapsed * 1e3).toFixed(2)} ms`, ""],
    ]) {
      const card = h("div", `card ${cls}`);
      card.appendChild(h("div", "k", k));
      card.appendChild(h("div", "v", v));
      cards.appendChild(card);
    }

    const svg = this.charts;
    clear(svg);
    const W = 1000, H = 300, pad = 26;
    const lo = log.t0, hi = Math.max(log.t0 + 1e-9, log.t1);
    const X = (t) => pad + ((t - lo) / (hi - lo)) * (W - 2 * pad);

    const series = [];
    const fae = upto.filter((e) => e.type === "fae_resp");
    if (fae.length) {
      const fc = fae.map((e) => Object.values(e.fcwnd || {}).reduce((a, b) => a + b, 0));
      series.push({ pts: fae.map((e, i) => [X(e.t), fc[i]]), cls: "window-line", label: "sum fcwnd" });
      series.push({ pts: fae.map((e) => [X(e.t), e.ncwnd]), cls: "ser-line", label: "ncwnd" });
    }
    const evts = upto.filter((e) => e.type === "fae_event");
    if (evts.length) {
      series.push({
        pts: evts.map((e) => [X(e.t), (e.buffer_occ || 0) * 64]),
        cls: "occ-line", label: "rx occupancy (x64)",
      });
    }
    if (!series.length) {
      // The baselines have no FAE, so there is no window series to draw. Say so rather
      // than leaving an empty box, which reads as a broken panel.
      const note = el("text", { x: 8, y: 20, class: "chart-label" }, svg);
      note.textContent = this.view.hasFae
        ? "no FAE samples at this point in the log"
        : "no FAE in this log (baseline transport)";
      return;
    }

    const maxY = Math.max(1, ...series.flatMap((s) => s.pts.map((p) => p[1])));
    const Y = (v) => H - pad - (v / maxY) * (H - 2 * pad);
    el("line", { x1: pad, y1: H - pad, x2: W - pad, y2: H - pad, class: "axis" }, svg);
    el("text", { x: 2, y: pad, class: "chart-label" }, svg).textContent = maxY.toFixed(0);
    el("text", { x: 2, y: H - pad, class: "chart-label" }, svg).textContent = "0";

    for (const s of series) {
      if (s.pts.length < 2) continue;
      el("path", {
        d: s.pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${Y(p[1]).toFixed(1)}`).join(""),
        class: s.cls,
      }, svg);
    }
    let ly = pad;
    for (const s of series) {
      el("text", { x: W - pad + 4, y: ly, class: "chart-label" }, svg).textContent = s.label;
      ly += 12;
    }
  }

  renderPools(log, upto, now) {
    const svg = this.pools;
    clear(svg);
    const t0 = log.t0;
    const hi = Math.max(t0 + 1e-9, log.t1);
    const W = 1000, H = 220, pad = 24;
    const X = (t) => pad + ((t - t0) / (hi - t0)) * (W - 2 * pad);

    const events = upto.filter((e) => e.type === "resource_reserve" || e.type === "resource_release");
    if (!events.length) {
      el("text", { x: 8, y: 20, class: "chart-label" }, svg).textContent = "No resource events in this log.";
      return;
    }

    const pools = [...new Set(events.map((e) => e.pool))];
    const rowH = (H - 2 * pad) / Math.max(1, pools.length);

    pools.forEach((pool, idx) => {
      const y = pad + idx * rowH;
      const isResp = pool.includes("resp");
      const top = y + rowH - 4;
      el("text", { x: 2, y: y + rowH / 2, class: "chart-label" }, svg).textContent = pool.slice(0, 9);
      el("line", { x1: pad, y1: y + rowH - 2, x2: W - pad, y2: y + rowH - 2, class: "axis" }, svg);

      let d = 0;
      let prev = t0;
      for (const e of events) {
        if (e.pool !== pool) continue;
        if (e.t > prev) {
          el("rect", {
            x: X(prev), y: top - barH(d, rowH), width: Math.max(0.6, X(e.t) - X(prev)), height: barH(d, rowH),
            class: `poolbar ${isResp ? "resp" : ""}`, opacity: 0.75,
          }, svg);
        }
        d = Math.max(0, d + (e.type === "resource_reserve" ? (e.n || 1) : -(e.n || 1)));
        prev = e.t;
      }
      el("rect", {
        x: X(prev), y: top - barH(d, rowH),
        width: Math.max(0.6, X(now) - X(prev)), height: barH(d, rowH),
        class: `poolbar ${isResp ? "resp" : ""}`, opacity: 0.75,
      }, svg);
    });

    for (const e of upto) {
      if (e.type !== "xoff" && e.type !== "xon") continue;
      el("line", { x1: X(e.t), y1: pad - 8, x2: X(e.t), y2: H - pad, class: `marker ${e.type}` }, svg);
    }
    el("line", { x1: X(now), y1: pad - 12, x2: X(now), y2: H - pad, class: "playhead" }, svg);
  }

  renderStream(upto) {
    const box = this.stream;
    clear(box);
    for (const e of upto.slice(-400)) {
      const row = h("div", "ev");
      if (e.type === "pkt_drop") row.classList.add("drop");
      if (e.type === "pkt_send" && e.retx) row.classList.add("retx");
      if (e.type.startsWith("fae")) row.classList.add("fae");
      row.appendChild(h("span", "t", fmt(e.t)));
      row.appendChild(h("span", "ty", e.type));
      const detail = Object.entries(e)
        .filter(([k]) => k !== "t" && k !== "type" && k !== "conn")
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
      row.appendChild(h("span", null, `c${e.conn} ${detail.join(" ")}`));
      box.appendChild(row);
    }
    box.scrollTop = box.scrollHeight;
  }
}

/** Pool depth to bar height. The 32 matches the default pool capacity in tl/resources.py. */
function barH(depth, rowH) {
  return Math.max(1, (depth / 32) * (rowH - 8));
}

/* ------------------------------------------------------------------- the view */

class View {
  constructor() {
    this.logs = [];
    this.sides = [];
    this.t = 0;
    this.playing = false;
    this.speed = 1;
    this.timer = null;
    this.layout = 1;
    this.ws = null;
    this.liveLog = null;

    this.stage = document.getElementById("stage");
    this.template = document.getElementById("side-template");
    this.fileInput = document.getElementById("file");
    this.playBtn = document.getElementById("play");
    this.stepBtn = document.getElementById("step");
    this.speedSel = document.getElementById("speed");
    this.layoutSel = document.getElementById("layout");
    this.scrub = document.getElementById("scrub");
    this.clock = document.getElementById("clock");

    this.liveToggle = document.getElementById("live-toggle");
    this.liveStatus = document.getElementById("live-status");
    this.liveRestart = document.getElementById("live-restart");
    this.killBtn = document.getElementById("kill");

    this.fileInput.addEventListener("change", (e) => this.loadFiles(e.target.files));
    this.playBtn.addEventListener("click", () => this.toggle());
    this.stepBtn.addEventListener("click", () => { this.pause(); this.nudge(1); });
    this.speedSel.addEventListener("change", (e) => { this.speed = Number(e.target.value); });
    this.layoutSel.addEventListener("change", (e) => this.setLayout(Number(e.target.value)));
    this.scrub.addEventListener("input", (e) => {
      this.pause();
      this.seekFraction(Number(e.target.value) / 1000);
    });
    this.liveToggle.addEventListener("change", () => this.toggleLive());
    // Restart over the existing socket. Reconnecting would build a fresh server-side run with
    // default knobs, silently throwing away whatever the sliders were set to.
    this.liveRestart.addEventListener("click", () => this.send({ cmd: "restart" }));
    this.killBtn.addEventListener("click", () => this.send({ cmd: "kill_path" }));

    for (const [id, fmt2] of [["loss", (v) => `${v.toFixed(1)}%`],
                              ["reorder", (v) => `${v}%`],
                              ["slow", (v) => `${v}\u00d7`]]) {
      const input = document.getElementById(id);
      const out = document.getElementById(`${id}-out`);
      const sync = () => { out.textContent = fmt2(Number(input.value)); };
      input.addEventListener("input", () => { sync(); this.send({ cmd: "set", knob: id, value: Number(input.value) }); });
      sync();
    }
    // The server rebuilds on a transport change and emits a fresh reset marker, so this must
    // not reconnect. The old socket carries the selected knobs; a new one would not.
    document.getElementById("transport").addEventListener("change", (e) => {
      this.send({ cmd: "set", knob: "transport", value: e.target.value });
    });

    document.addEventListener("keydown", (e) => {
      if (!this.logs.length) return;
      if (e.code === "Space") { e.preventDefault(); this.toggle(); }
      if (e.code === "ArrowRight") { e.preventDefault(); this.pause(); this.nudge(e.shiftKey ? 25 : 1); }
      if (e.code === "ArrowLeft") { e.preventDefault(); this.pause(); this.nudge(e.shiftKey ? -25 : -1); }
    });
  }

  /** Connection a side should report on, derived from whatever it has replayed so far. */
  connOf(upto) {
    const counts = new Map();
    for (const e of upto) {
      if (e.type !== "pkt_send" || e.kind === "ack" || e.kind === "nack") continue;
      counts.set(e.conn, (counts.get(e.conn) || 0) + 1);
    }
    if (counts.size) return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0][0];
    return upto.length ? upto[0].conn : 0;
  }

  get span() {
    const ref = this.logs[0];
    return ref ? Math.max(1e-9, ref.t1 - ref.t0) : 1;
  }

  /** Whether the log being shown drives the FAE at all; baselines do not. */
  get hasFae() {
    return this.logs.some((l) => l.events.some((e) => e.type === "fae_resp"));
  }

  setLayout(n) {
    this.layout = n;
    // Asking for two columns with one log loaded would render a second empty column;
    // clamp to what we actually have.
    this.rebuildSides();
    this.render();
  }

  rebuildSides() {
    clear(this.stage);
    this.sides = [];
    const want = Math.max(1, Math.min(this.layout, this.logs.length || 1));
    this.stage.classList.toggle("compare", want > 1);
    for (let i = 0; i < want; i++) {
      const root = this.template.content.firstElementChild.cloneNode(true);
      this.stage.appendChild(root);
      this.sides.push(new Side(root, this));
    }
  }

  async loadFiles(files) {
    for (const f of files) {
      const log = parseLog(await f.text(), f.name);
      if (log.events.length) this.logs.push(log);
    }
    if (!this.logs.length) return;
    this.t = this.logs[0].t0;
    for (const id of ["play", "step", "speed", "layout"]) document.getElementById(id).disabled = false;
    this.rebuildSides();
    this.render();
  }

  /** Programmatic equivalent of picking a file, used by the render check. */
  loadText(text, name) {
    const log = parseLog(text, name);
    if (!log.events.length) return null;
    this.logs.push(log);
    return log;
  }

  seekFraction(frac) {
    const ref = this.logs[0];
    if (!ref) return;
    this.t = ref.t0 + frac * this.span;
    this.render();
  }

  nudge(n) {
    const ref = this.logs[0];
    if (!ref) return;
    // Step by event, so "step" always lands somewhere visible.
    const i = ref.indexAt(this.t);
    const next = Math.max(0, Math.min(ref.events.length - 1, i + n));
    this.t = ref.events[next].t;
    this.render();
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  play() {
    const ref = this.logs[0];
    if (!ref) return;
    if (this.t >= ref.t1) this.t = ref.t0;
    this.playing = true;
    this.playBtn.innerHTML = "&#10073;&#10073; pause";
    const perEvent = (this.span * 1000) / Math.max(1, ref.events.length);
    this.timer = setInterval(() => {
      this.t += (perEvent * this.speed) / 1000;
      if (this.t >= ref.t1) { this.t = ref.t1; this.pause(); }
      this.render();
    }, 60);
  }

  pause() {
    this.playing = false;
    this.playBtn.innerHTML = "&#9654; play";
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /* -------------------------------------------------------------- live mode */

  async toggleLive() {
    if (this.liveToggle.checked) await this.startLive(false);
    else this.stopLive();
  }

  async startLive(restart) {
    this.stopLive();
    this.liveStatus.textContent = "connecting";
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws`;
    try {
      this.ws = new WebSocket(url);
    } catch (err) {
      this.liveStatus.textContent = `offline (${err.message})`;
      return;
    }
    this.ws.onopen = async () => {
      this.setLiveState(true);
      this.liveRestart.disabled = false;
      this.killBtn.disabled = false;
      for (const id of ["play", "step", "speed"]) document.getElementById(id).disabled = true;
      if (restart) await this.send({ cmd: "restart" });
      else await this.send({ cmd: "start", knobs: this.knobs() });
    };
    this.ws.onclose = () => {
      this.setLiveState(false);
      this.liveRestart.disabled = true;
      this.killBtn.disabled = true;
    };
    this.ws.onerror = () => { this.liveStatus.textContent = "socket error"; };
    this.ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      // Forward unconditionally. A control echo carries knobs but no events, and it is the only
      // thing that tells the client a knob changed after the run had already finished.
      this.ingest(msg);
    };
  }

  /** Show or hide the live-only chrome. The class gates the chaos controls in CSS. */
  setLiveState(on) {
    document.body.classList.toggle("is-live", on);
    this.liveStatus.dataset.state = on ? "live" : "offline";
    this.liveStatus.textContent = on ? "live" : "offline";
  }

  stopLive() {
    if (this.ws) { try { this.ws.close(); } catch { /* already closed */ } }
    this.ws = null;
    this.liveLog = null;
  }

  knobs() {
    return {
      loss: Number(document.getElementById("loss").value) / 100,
      reorder: Number(document.getElementById("reorder").value) / 100,
      slow: Number(document.getElementById("slow").value),
      transport: document.getElementById("transport").value,
    };
  }

  async send(obj) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify(obj));
  }

  ingest(msg) {
    // Any message may carry the authoritative knobs, including a control echo sent when a knob
    // changes after the run has already finished and no further events are coming. Reading them
    // only from event batches would leave the panel claiming the old value indefinitely.
    if (msg.knobs) this.liveKnobs = msg.knobs;
    if (msg.transport) this.liveTransport = msg.transport;

    if (msg.reset) {
      this.liveLog = new Log("live", []);
    }
    if (!msg.events || !msg.events.length) {
      if (this.liveLog) this.renameLive();
      return;
    }
    if (!this.liveLog) this.liveLog = new Log("live", []);
    this.liveLog.events.push(...msg.events);
    this.liveLog.setEvents(this.liveLog.events);
    this.renameLive();
    // Replace rather than append, so the live run is always the log on screen.
    this.logs = [this.liveLog];
    if (this.sides.length !== 1) this.rebuildSides();
    this.t = this.liveLog.t1;
    this.render();
  }

  /** Label the log with the knobs actually in force, not the ones last typed into a slider. */
  renameLive() {
    const k = this.liveKnobs || {};
    const bits = [`live: ${this.liveTransport || k.transport || "?"}`];
    if (k.loss !== undefined) bits.push(`loss ${(k.loss * 100).toFixed(1)}%`);
    if (k.reorder) bits.push(`reorder ${(k.reorder * 100).toFixed(0)}%`);
    if (k.slow && k.slow > 1) bits.push(`slow ${k.slow}x`);
    this.liveLog.name = bits.join("  ");
  }

  /* --------------------------------------------------------------- rendering */

  render() {
    const logs = this.logs.slice(0, this.sides.length);
    if (!logs.length) return;
    for (let i = 0; i < this.sides.length; i++) {
      const log = logs[i];
      if (!log) continue;
      this.sides[i].render(log, Math.min(this.t, log.t1));
    }
    this.clock.textContent = `t = ${fmt(this.t)} s`;
    if (!this.scrub.matches(":active")) {
      const frac = (this.t - logs[0].t0) / this.span;
      this.scrub.value = String(Math.round(Math.max(0, Math.min(1, frac)) * 1000));
    }
  }
}

window.addEventListener("DOMContentLoaded", () => { window.view = new View(); });
