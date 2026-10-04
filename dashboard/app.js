/* Event-log replay dashboard.
 *
 * Reads a JSONL log produced by `EventBus.write_jsonl` and rebuilds every panel from it.
 * There is no protocol logic here and no second source of truth: if a panel shows
 * something, it came out of the log. That is what makes a recorded log reviewable and
 * what lets the same file drive the tests.
 *
 * The log is replayed, not re-simulated. Scrubbing is instant because every panel is a
 * fold over the event prefix up to the playhead.
 */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ log model */

/** One parsed log plus the derived views the panels need. */
class Log {
  constructor(name, events) {
    this.name = name;
    this.events = events;
    this.t0 = events.length ? events[0].t : 0;
    this.t1 = events.length ? events[events.length - 1].t : 0;
    this.sends = events.filter((e) => e.type === "pkt_send");
    this.dataSends = this.sends.filter((e) => e.kind !== "ack" && e.kind !== "nack");
    this.recvs = events.filter((e) => e.type === "pkt_recv");
    this.drops = events.filter((e) => e.type === "pkt_drop" && e.kind !== "ack" && e.kind !== "nack");
    this.conns = [...new Set(events.map((e) => e.conn))].sort((a, b) => a - b);
    this.buildFlights();
  }

  /**
   * Join each transmission to its fate: delivered, dropped, reordered, or still in flight.
   *
   * Two traps this has to avoid. Matching on PSN alone is wrong because ACK and data share
   * one PSN space in the log, and matching a drop onto the wrong one of the two silently
   * blames the datapath for an ACK that went missing -- so drops are matched on
   * (conn, kind, psn) too. And a retransmission of the same PSN is a distinct flight, not a
   * second delivery of the first, so each send opens its own flight rather than replacing an
   * earlier one. Collapsing them makes every recovery look like a first-attempt success.
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
          if (match(f, ev) && f.fate === "inflight") {
            f.fate = "dropped";
            f.tEnd = ev.t;
            break;
          }
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

  /** Events up to and including index `i`. */
  upto(i) { return this.events.slice(0, i + 1); }

  /**
   * The connection to show in the per-connection panels: whichever sent the most data.
   *
   * Not `conns[0]`. Events that are not connection-scoped -- `path_kill`, `path_slow` -- are
   * emitted with `conn: 0`, so a log with any path event puts 0 first in the list and every
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
  // A log must be time-ordered to be replayable; sorting here would hide a broken emitter
  // rather than surface it, so only warn and keep order.
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

/* ------------------------------------------------------------------- the view */

class View {
  constructor() {
    this.logs = [];
    this.index = 0;
    this.pos = 0;
    this.playing = false;
    this.speed = 1;
    this.window = 0.004; // seconds of log visible in the ladder
    this.timer = null;

    this.fileInput = document.getElementById("file");
    this.playBtn = document.getElementById("play");
    this.stepBtn = document.getElementById("step");
    this.speedSel = document.getElementById("speed");
    this.scrub = document.getElementById("scrub");
    this.clock = document.getElementById("clock");

    this.fileInput.addEventListener("change", (e) => this.loadFiles(e.target.files));
    this.playBtn.addEventListener("click", () => this.toggle());
    this.stepBtn.addEventListener("click", () => { this.pause(); this.advance(1); });
    this.speedSel.addEventListener("change", (e) => { this.speed = Number(e.target.value); });
    this.scrub.addEventListener("input", (e) => { this.pause(); this.pos = Number(e.target.value); this.render(); });

    document.addEventListener("keydown", (e) => {
      if (!this.logs.length) return;
      if (e.code === "Space") { e.preventDefault(); this.toggle(); }
      if (e.code === "ArrowRight") { e.preventDefault(); this.pause(); this.advance(e.shiftKey ? 25 : 1); }
      if (e.code === "ArrowLeft") { e.preventDefault(); this.pause(); this.advance(e.shiftKey ? -25 : -1); }
    });
  }

  get log() { return this.logs[this.index]; }

  async loadFiles(files) {
    for (const f of files) {
      const log = parseLog(await f.text(), f.name);
      if (log.events.length) this.logs.push(log);
    }
    if (!this.logs.length) return;
    this.index = Math.min(this.index, this.logs.length - 1);
    this.pos = 0;
    const ready = ["play", "step", "speed", "scrub"];
    ready.forEach((id) => { document.getElementById(id).disabled = false; });
    this.scrub.max = String(this.log.events.length - 1);
    this.render();
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  play() {
    if (!this.logs.length) return;
    if (this.pos >= this.log.events.length - 1) this.pos = 0;
    this.playing = true;
    this.playBtn.innerHTML = "&#10073;&#10073; pause";
    const perEvent = Math.max(0.02, (this.log.t1 - this.log.t0) / Math.max(1, this.log.events.length));
    this.timer = setInterval(() => this.advance(1), (perEvent * 1000) / this.speed);
  }

  pause() {
    this.playing = false;
    this.playBtn.innerHTML = "&#9654; play";
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  advance(n) {
    this.pos = Math.max(0, Math.min(this.log.events.length - 1, this.pos + n));
    this.render();
  }

  /* ----------------------------------------------------------------- rendering */

  render() {
    const log = this.log;
    if (!log) return;
    this.scrub.value = String(this.pos);
    const upto = log.upto(this.pos);
    const now = upto.length ? upto[upto.length - 1].t : log.t0;
    this.clock.textContent = `t = ${fmt(now)} s`;

    this.renderConn(upto, now);
    this.renderLadder(now);
    this.renderBitmap(upto);
    this.renderStats(upto);
    this.renderPools(upto, now);
    this.renderStream(upto);
  }

  renderConn(upto, now) {
    const body = document.getElementById("conn-body");
    clear(body);

    const states = upto.filter((e) => e.type === "conn_state");
    const last = states[states.length - 1];
    const state = last ? last.state : "-";
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
      const flows = Object.keys(latest.fcwnd || {});
      rows.push(["flows", flows.length]);
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
        const sent = this.log.sends.filter((e) => String(e.flow) === String(f) && e.t <= now).length;
        const retx = this.log.sends.filter((e) => String(e.flow) === String(f) && e.t <= now && e.retx).length;
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

  renderLadder(now) {
    const svg = document.getElementById("ladder");
    clear(svg);
    const W = 1000, H = 260;
    const ySend = 70, yRecv = 190;
    const span = this.window;
    const lo = now - span * 0.35;
    const hi = now + span * 0.65;
    const x = (t) => ((t - lo) / (hi - lo)) * W;

    el("line", { x1: 0, y1: ySend, x2: W, y2: ySend, class: "lane" }, svg);
    el("line", { x1: 0, y1: yRecv, x2: W, y2: yRecv, class: "lane" }, svg);
    const sl = el("text", { x: 4, y: ySend - 8, class: "lane-label" }, svg);
    sl.textContent = "sender";
    const rl = el("text", { x: 4, y: yRecv + 18, class: "lane-label" }, svg);
    rl.textContent = "receiver";

    for (const f of this.log.flights) {
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
    for (const e of this.log.events) {
      if (e.t > hi) break;
      if (e.type !== "rack_fire" && e.type !== "tlp_fire") continue;
      const px = x(e.t);
      el("line", { x1: px, y1: ySend - 14, x2: px, y2: yRecv + 14, class: `marker ${e.type === "rack_fire" ? "rack" : "tlp"}` }, svg);
      const t = el("text", { x: px + 3, y: ySend - 18, class: "chart-label" }, svg);
      t.textContent = e.type === "rack_fire" ? `RACK psn ${e.psn}` : `TLP psn ${e.psn}`;
    }

    el("line", { x1: x(now), y1: 8, x2: x(now), y2: H - 8, class: "playhead" }, svg);
    el("text", { x: 6, y: H - 4, class: "axis-label" }, svg).textContent = `${fmt(lo)} s`;
    const rt = el("text", { x: W - 6, y: H - 4, "text-anchor": "end", class: "axis-label" }, svg);
    rt.textContent = `${fmt(hi)} s`;
  }

  renderBitmap(upto) {
    const box = document.getElementById("bitmap");
    clear(box);
    const recv = new Map();
    for (const e of upto) {
      if (e.type !== "pkt_recv") continue;
      const k = `${e.conn}:${e.psn}`;
      if (e.accepted && !recv.has(k)) recv.set(k, e.t);
    }
    const retxPsn = new Set(upto.filter((e) => e.type === "pkt_send" && e.retx).map((e) => `${e.conn}:${e.psn}`));
    const timerPsn = new Set(upto.filter((e) => e.type === "rack_fire" || e.type === "tlp_fire").map((e) => `${e.conn}:${e.psn}`));

    const conn = this.log.primaryConn;
    const sent = upto.filter((e) => e.type === "pkt_send" && e.conn === conn);
    if (!sent.length) { box.className = "empty"; box.textContent = "No packets sent yet."; return; }
    box.className = "bitmap";

    const base = Math.min(...sent.map((e) => e.psn));
    const top = Math.min(base + 64, Math.max(...sent.map((e) => e.psn)) + 1);
    for (let p = base; p < top; p++) {
      const k = `${conn}:${p}`;
      const cell = h("div", "cell");
      cell.textContent = String(p);
      cell.title = `psn ${p}`;
      if (recv.has(k)) cell.classList.add("recv");
      else if (timerPsn.has(k)) cell.classList.add("rack");
      else if (retxPsn.has(k)) cell.classList.add("retx");
      else if (sent.some((e) => e.psn === p)) cell.classList.add("hole");
      box.appendChild(cell);
    }

    const legend = h("div", "legend");
    const items = [
      ["rgba(61,220,151,0.5)", "received"],
      ["transparent", "not yet sent"],
      ["rgba(255,92,92,0.35)", "timer fired, hole open"],
      ["rgba(255,176,46,0.4)", "retransmitted"],
    ];
    for (const [color, label] of items) {
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

  renderStats(upto) {
    const cards = document.getElementById("stat-cards");
    clear(cards);
    const log = this.log;
    const t0 = log.t0;
    const now = upto.length ? upto[upto.length - 1].t : t0;
    const elapsed = Math.max(1e-9, now - t0);

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
      } else if (e.type === "pkt_drop") {
        drops++;
      }
    }
    const done = new Set(upto.filter((e) => e.type === "conn_state" && e.state === "TEARDOWN").map((e) => e.conn));

    const rows = [
      ["goodput", `${mbps(bytes, elapsed)} Mbps`, ""],
      ["delivered", String(accepted.size), "good"],
      ["drops", String(drops), drops ? "warn" : ""],
      ["retransmits", String(retx), retx ? "warn" : ""],
      ["spurious", String(spurious), spurious ? "bad" : "good"],
      ["duplicate arrivals", String(dups), dups ? "warn" : ""],
      ["teardowns", String(done.size), ""],
      ["elapsed", `${(elapsed * 1e3).toFixed(2)} ms`, ""],
    ];
    for (const [k, v, cls] of rows) {
      const card = h("div", `card ${cls}`);
      card.appendChild(h("div", "k", k));
      card.appendChild(h("div", "v", v));
      cards.appendChild(card);
    }

    const svg = document.getElementById("charts");
    clear(svg);
    const W = 1000, H = 300, pad = 26;
    const lo = t0, hi = Math.max(t0 + 1e-9, log.t1);
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
    if (!series.length) return;

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
      const t = el("text", { x: W - pad + 4, y: ly, class: "chart-label" }, svg);
      t.textContent = s.label;
      ly += 12;
    }
  }

  renderPools(upto, now) {
    const svg = document.getElementById("pools");
    clear(svg);
    const log = this.log;
    const t0 = log.t0;
    const hi = Math.max(t0 + 1e-9, log.t1);
    const W = 1000, H = 220, pad = 24;
    const X = (t) => pad + ((t - t0) / (hi - t0)) * (W - 2 * pad);

    const events = upto.filter((e) => e.type === "resource_reserve" || e.type === "resource_release");
    if (!events.length) { svg.appendChild(el("text", { x: 8, y: 20, class: "chart-label" })).textContent = "No resource events in this log."; return; }

    const depth = new Map();
    for (const e of events) {
      const pool = e.pool;
      const d = depth.get(pool) || 0;
      depth.set(pool, Math.max(0, d + (e.type === "resource_reserve" ? (e.n || 1) : -(e.n || 1))));
    }
    const pools = [...depth.keys()];
    const rowH = (H - 2 * pad) / Math.max(1, pools.length);

    pools.forEach((pool, idx) => {
      const y = pad + idx * rowH;
      const isResp = pool.includes("resp");
      el("text", { x: 2, y: y + rowH / 2, class: "chart-label" }, svg).textContent = pool.slice(0, 9);
      el("line", { x1: pad, y1: y + rowH - 2, x2: W - pad, y2: y + rowH - 2, class: "axis" }, svg);

      let d = 0;
      let prev = t0;
      const top = pad + idx * rowH + rowH - 4;
      for (const e of events) {
        if (e.pool !== pool) continue;
        const hgt = Math.max(1, (d / 32) * (rowH - 8));
        if (e.t > prev) {
          el("rect", {
            x: X(prev), y: top - hgt, width: Math.max(0.6, X(e.t) - X(prev)), height: hgt,
            class: `poolbar ${isResp ? "resp" : ""}`, opacity: 0.75,
          }, svg);
        }
        d = Math.max(0, d + (e.type === "resource_reserve" ? (e.n || 1) : -(e.n || 1)));
        prev = e.t;
      }
      el("rect", {
        x: X(prev), y: top - Math.max(1, (d / 32) * (rowH - 8)),
        width: Math.max(0.6, X(now) - X(prev)),
        height: Math.max(1, (d / 32) * (rowH - 8)),
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
    const box = document.getElementById("stream");
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

window.addEventListener("DOMContentLoaded", () => { window.view = new View(); });
