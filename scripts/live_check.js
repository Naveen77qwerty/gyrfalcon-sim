#!/usr/bin/env node
/* Drive the dashboard's live mode in headless Chrome against a running server.
 *
 * The unit tests exercise `LiveRun` in-process; they cannot tell whether the browser's socket
 * handling, the reset-marker contract, or the live render path actually work. This loads the
 * real page, ticks "live mode", and asserts the panels fill from a socket that nobody is
 * pushing into by hand.
 *
 * Usage: node scripts/live_check.js http://127.0.0.1:8811
 * Exits non-zero on failure. Requires google-chrome on PATH.
 */
"use strict";

const http = require("http");
const { spawn } = require("child_process");

const BASE = process.argv[2] || "http://127.0.0.1:8811";
const DEBUG_PORT = 9334;

function chrome() {
  const bin = ["google-chrome", "chromium", "chromium-browser"].find((b) => {
    try { require("child_process").execSync(`command -v ${b}`); return true; } catch { return false; }
  });
  if (!bin) return null;
  return spawn(bin, [
    "--headless=new", "--disable-gpu", "--no-sandbox",
    `--remote-debugging-port=${DEBUG_PORT}`,
    `--user-data-dir=/tmp/live-profile-${process.pid}`, "about:blank",
  ], { stdio: "ignore" });
}

async function cdpTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const body = await new Promise((res, rej) => {
        http.get(`http://127.0.0.1:${DEBUG_PORT}/json/list`, (r) => {
          let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => res(b));
        }).on("error", rej);
      });
      const page = JSON.parse(body).find((t) => t.type === "page");
      if (page) return page;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("chrome devtools endpoint never came up");
}

class Session {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.errors = []; }
  static async open(url) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", () => reject(new Error("devtools socket failed")), { once: true });
    });
    const s = new Session(ws);
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
      if (msg.id && s.pending.has(msg.id)) {
        const { resolve, reject } = s.pending.get(msg.id);
        s.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method === "Runtime.exceptionThrown") {
        s.errors.push(msg.params.exceptionDetails.text + " " +
          (msg.params.exceptionDetails.exception || {}).description);
      } else if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
        s.errors.push(msg.params.args.map((a) => a.value || a.description).join(" "));
      }
    });
    return s;
  }
  send(method, params) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params: params || {} }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async evalJs(expr) {
    const r = await this.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.text + " " + (r.exceptionDetails.exception || {}).description);
    }
    return r.result.value;
  }
}

async function main() {
  const proc = chrome();
  if (!proc) { console.log("no chrome on PATH; skipping live check"); process.exit(0); }

  let failures = 0;
  const fail = (msg) => { console.log(`FAIL ${msg}`); failures++; };
  const ok = (msg) => console.log(`ok   ${msg}`);

  try {
    const page = await cdpTarget();
    const s = await Session.open(page.webSocketDebuggerUrl);
    await s.send("Runtime.enable");
    await s.send("Page.enable");
    await s.send("Page.navigate", { url: `${BASE}/index.html` });
    await new Promise((r) => setTimeout(r, 1200));

    if (!await s.evalJs("typeof window.view !== 'undefined'")) {
      throw new Error("window.view was never created; app.js did not run");
    }

    // 1. The chaos controls must be hidden until live mode is on, so a replay session does not
    //    look like it is driving anything.
    const hiddenAtFirst = await s.evalJs(
      "getComputedStyle(document.querySelector('.chaos')).display");
    if (hiddenAtFirst !== "none") fail(`chaos controls visible before live mode (${hiddenAtFirst})`);
    else ok("chaos controls hidden in replay mode");

    // 2. Turn live mode on and wait for a socket-delivered log.
    await s.evalJs("document.getElementById('live-toggle').click(); true");
    const report = await s.evalJs(`(async () => {
      const v = window.view;
      const deadline = Date.now() + 15000;
      while (v.logs.length === 0 && Date.now() < deadline) await new Promise((r) => setTimeout(r, 50));
      if (!v.logs.length) return { error: "no log arrived over the socket" };
      const deadline2 = Date.now() + 15000;
      while (!v.logs[0].flights.length && Date.now() < deadline2) await new Promise((r) => setTimeout(r, 50));
      const side = document.querySelector("#stage .side");
      return {
        status: document.getElementById("live-status").textContent,
        events: v.logs[0].events.length,
        flights: v.logs[0].flights.length,
        liveClass: document.body.classList.contains("is-live"),
        sideName: side.querySelector(".side-name").textContent,
        bitmapCells: side.querySelectorAll(".bitmap .cell").length,
        cards: side.querySelectorAll(".stat-cards .card").length,
        ladderShapes: side.querySelector(".ladder").childElementCount,
        streamRows: side.querySelectorAll(".stream .ev").length,
        faeSeries: side.querySelector(".charts").childElementCount,
      };
    })()`);

    if (report.error) fail(report.error);
    else {
      if (report.status !== "live") fail(`socket status is "${report.status}", expected live`);
      else ok("websocket connected");
      if (!report.liveClass) fail("body did not get is-live, so chaos controls stay hidden");
      else ok("chaos controls revealed in live mode");
      if (!report.events) fail("no events streamed");
      else ok(`${report.events} events streamed into the log`);
      if (!report.flights) fail("no flights built from the streamed log");
      else ok(`${report.flights} flights`);
      if (!report.bitmapCells) fail("bitmap panel empty");
      else ok(`${report.bitmapCells} bitmap cells`);
      if (report.cards !== 8) fail(`expected 8 stat cards, got ${report.cards}`);
      else ok("8 stat cards");
      if (!report.ladderShapes) fail("ladder empty");
      else ok(`ladder ${report.ladderShapes} shapes`);
      if (!report.streamRows) fail("event stream empty");
      else ok(`event stream ${report.streamRows} rows`);
      if (!report.faeSeries) fail("charts panel empty on a falcon-style run");
      else ok(`charts ${report.faeSeries} shapes`);
    }

    // 3. Move a knob and confirm the run is rebuilt rather than silently ignored.
    const swap = await s.evalJs(`(async () => {
      const v = window.view;
      const before = v.logs[0] ? v.logs[0].events.length : 0;
      await v.send({ cmd: "set", knob: "loss", value: 0.2 });
      await new Promise((r) => setTimeout(r, 300));
      return { before, loss: v.liveLog ? v.liveLog.name : null };
    })()`);
    if (!swap.loss || !swap.loss.includes("20.0")) {
      fail(`loss knob not reflected in the panel title: ${JSON.stringify(swap.loss)}`);
    } else ok(`loss knob retunes the live run (${swap.loss})`);

    const killed = await s.evalJs(`(async () => {
      await window.view.send({ cmd: "kill_path", index: 0 });
      await new Promise((r) => setTimeout(r, 400));
      return window.view.logs[0].events.filter((e) => e.type === "path_kill").length;
    })()`);
    if (!killed) fail("kill_path produced no path_kill event in the streamed log");
    else ok(`kill_path reached the stream (${killed} path_kill)`);

    // 4. Transport switch: the swap must actually change what the panels show.
    const tswitch = await s.evalJs(`(async () => {
      const v = window.view;
      const sel = document.getElementById("transport");
      sel.value = "gbn";
      sel.dispatchEvent(new Event("change"));
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 100));
        if (v.liveLog && v.liveLog.flights.length && !v.liveLog.events.some((e) => e.type === "fae_resp")) break;
      }
      const side = document.querySelector("#stage .side");
      return {
        fae: v.liveLog.events.some((e) => e.type === "fae_resp"),
        events: v.liveLog.events.length,
        chartsNote: side.querySelector(".charts").textContent.trim(),
      };
    })()`);
    if (tswitch.fae) fail("switching to GBN left FAE events on screen");
    else ok("transport switch rebuilt the run (no FAE on a GBN log)");
    if (!tswitch.events) fail("no events after the transport switch");
    else ok(`${tswitch.events} events on the GBN run`);
    if (!tswitch.chartsNote.includes("baseline")) {
      fail(`charts panel should say it has no FAE, said: "${tswitch.chartsNote}"`);
    } else ok("charts panel explains the missing FAE");

    if (s.errors.length) {
      fail(`${s.errors.length} JS error(s): ${[...new Set(s.errors)].slice(0, 3).join(" | ")}`);
    } else ok("no JS errors");
  } catch (err) {
    fail(`live check could not run: ${err.message}`);
  } finally {
    proc.kill();
  }
  process.exit(failures ? 1 : 0);
}

main();
