#!/usr/bin/env node
/* Render the dashboard in headless Chrome and report what each panel actually contains.
 *
 * Syntax-checking app.js is not enough: a typo in a render path leaves the page blank with
 * nothing in the console that a reader would notice. This loads index.html for real, pushes a
 * real event log through it, and fails if any panel is empty or any JS error was raised.
 *
 * Usage: node scripts/render_check.js <log.jsonl> [...] [--shots DIR]
 * Exits non-zero on any failure. Requires google-chrome on PATH.
 *
 * `--shots DIR` also writes a PNG per log. The render check proves the panels are populated;
 * a screenshot is what a reader without this repository can look at, so it is worth producing
 * from the same verified session rather than from a separate one that could drift.
 */
"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const DASH = path.join(ROOT, "dashboard");
const SHOT_FLAG = process.argv.indexOf("--shots");
const SHOTS = SHOT_FLAG >= 0 ? process.argv[SHOT_FLAG + 1] : null;
const LOGS = process.argv.slice(2).filter((a, i, all) =>
  a !== "--shots" && all[i - 1] !== "--shots");
const PORT = 8731;

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const rel = decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "") || "index.html";
      const file = path.join(DASH, rel);
      if (!file.startsWith(DASH) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream" });
      res.end(fs.readFileSync(file));
    });
    server.listen(PORT, "127.0.0.1", () => resolve(server));
  });
}

function chrome() {
  const bin = ["google-chrome", "chromium", "chromium-browser"].find((b) => {
    try { require("child_process").execSync(`command -v ${b}`); return true; } catch { return false; }
  });
  if (!bin) return null;
  const proc = spawn(bin, [
    "--headless=new", "--disable-gpu", "--no-sandbox", "--remote-debugging-port=9333",
    `--user-data-dir=/tmp/dash-profile-${process.pid}`, "about:blank",
  ], { stdio: "ignore" });
  return proc;
}

async function cdpTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const body = await new Promise((res, rej) => {
        http.get("http://127.0.0.1:9333/json/list", (r) => {
          let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => res(b));
        }).on("error", rej);
      });
      const targets = JSON.parse(body);
      const page = targets.find((t) => t.type === "page");
      if (page) return page;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("chrome devtools endpoint never came up");
}

class Session {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.errors = []; }
  static async open(url) {
    // Node 22 ships a global WebSocket, so this needs no dependency.
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
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " " + (r.exceptionDetails.exception || {}).description);
    return r.result.value;
  }
}

async function main() {
  if (!LOGS.length) { console.error("usage: render_check.js <log.jsonl> [...]"); process.exit(2); }
  const server = await serve();
  const proc = chrome();
  if (!proc) { console.error("no chrome on PATH; skipping render check"); server.close(); process.exit(0); }

  let failures = 0;
  const loaded = [];
  try {
    const page = await cdpTarget();
    const s = await Session.open(page.webSocketDebuggerUrl);
    await s.send("Runtime.enable");
    await s.send("Page.enable");
    // A fixed viewport for the whole session. Left at Chrome's default the shots are 780x437,
    // which crops the dashboard mid-panel -- the picture then looks broken in a way the
    // assertions do not catch.
    await s.send("Emulation.setDeviceMetricsOverride",
      { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
    await s.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/index.html` });
    await new Promise((r) => setTimeout(r, 1200));

    const ready = await s.evalJs("typeof window.view !== 'undefined'");
    if (!ready) throw new Error("window.view was never created; app.js did not run");

    for (const log of LOGS) {
      const text = fs.readFileSync(log, "utf8");
      const report = await s.evalJs(`(async () => {
        const v = window.view;
        v.pause();
        // Clear prior logs: loadFiles appends, so without this every iteration re-renders
        // log 0 and the whole check silently passes on the first file.
        v.logs = []; v.t = 0;
        document.getElementById("layout").value = "1";
        v.setLayout(1);
        // Feed the log exactly as the file input would.
        const dt = new DataTransfer();
        dt.items.add(new File([${JSON.stringify(text)}], ${JSON.stringify(path.basename(log))}));
        const input = document.getElementById("file");
        input.files = dt.files;
        input.dispatchEvent(new Event("change"));
        await new Promise((r) => setTimeout(r, 250));
        if (!v.logs.length) return { error: "no log was parsed" };
        v.t = v.logs[0].t1;
        v.render();
        // Scope every query to the first .side: with side-by-side on, ids would be
        // duplicated across clones and would resolve to the wrong column.
        const side = document.querySelector("#stage .side");
        const cards = side.querySelectorAll(".stat-cards .card");
        return {
          name: v.logs[0].name,
          events: v.logs[0].events.length,
          flights: v.logs[0].flights.length,
          sides: document.querySelectorAll("#stage .side").length,
          connText: side.querySelector(".conn-body").textContent.trim().length,
          bitmapCells: side.querySelectorAll(".bitmap .cell").length,
          cards: cards.length,
          cardsBad: Array.from(cards)
            .filter((c) => (c.querySelector(".v").textContent || "").includes("NaN")).length,
          ladderShapes: side.querySelector(".ladder").childElementCount,
          chartShapes: side.querySelector(".charts").childElementCount,
          poolShapes: side.querySelector(".pools").childElementCount,
          streamRows: side.querySelectorAll(".stream .ev").length,
          sideName: side.querySelector(".side-name").textContent,
        };
      })()`);

      const name = path.basename(log);
      if (report.error) { console.log(`FAIL ${name}: ${report.error}`); failures++; continue; }
      const problems = [];
      if (!report.connText) problems.push("connection panel empty");
      if (!report.bitmapCells) problems.push("bitmap panel empty");
      if (!report.cards) problems.push("no stat cards");
      if (report.cardsBad) problems.push(`${report.cardsBad} stat card(s) show NaN`);
      if (!report.ladderShapes) problems.push("ladder empty");
      if (!report.streamRows) problems.push("event stream empty");
      if (!report.flights) problems.push("no flights built");
      if (report.sides !== 1) problems.push(`single view rendered ${report.sides} sides`);

      if (problems.length) { console.log(`FAIL ${name}: ${problems.join("; ")}`); failures++; }
      else {
        console.log(`ok   ${name.padEnd(22)} ${String(report.events).padStart(6)} events  ` +
          `${String(report.flights).padStart(5)} flights  ${String(report.bitmapCells).padStart(3)} cells  ` +
          `${String(report.cards).padStart(2)} cards  ladder ${String(report.ladderShapes).padStart(4)}  ` +
          `charts ${String(report.chartShapes).padStart(4)}  pools ${String(report.poolShapes).padStart(4)}  ` +
          `stream ${String(report.streamRows).padStart(4)}`);
      }
      if (!problems.length && SHOTS) {
        const shot = await s.send("Page.captureScreenshot",
          { format: "png", captureBeyondViewport: true });
        const out = path.join(SHOTS, `${path.basename(log, ".jsonl")}.png`);
        fs.mkdirSync(SHOTS, { recursive: true });
        fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
        console.log(`     shot ${out}`);
      }
      loaded.push(report.name);
    }

    // Side-by-side is the one view that can break in a way single-view cannot: the two
    // clones share nothing, so a stray global lookup renders the left column from the right
    // log. Load two logs and check each column reports its own log.
    if (LOGS.length >= 2) {
      const pair = [LOGS[0], LOGS[1]];
      const report = await s.evalJs(`(() => {
        const v = window.view;
        v.pause();
        v.logs = [];
        v.loadText(${JSON.stringify(fs.readFileSync(pair[0], "utf8"))}, "left.jsonl");
        v.loadText(${JSON.stringify(fs.readFileSync(pair[1], "utf8"))}, "right.jsonl");
        v.setLayout(2);
        v.t = Math.min(v.logs[0].t1, v.logs[1].t1);
        v.render();
        const sides = document.querySelectorAll("#stage .side");
        return {
          sides: sides.length,
          names: Array.from(sides).map((s) => s.querySelector(".side-name").textContent),
          cells: Array.from(sides).map((s) => s.querySelectorAll(".bitmap .cell").length),
          cards: Array.from(sides).map((s) => s.querySelectorAll(".stat-cards .card").length),
          streams: Array.from(sides).map((s) => s.querySelectorAll(".stream .ev").length),
        };
      })()`);
      const problems = [];
      if (report.sides !== 2) problems.push(`expected 2 sides, got ${report.sides}`);
      if (new Set(report.names).size !== 2) problems.push(`sides share a log name: ${report.names}`);
      if (report.cells.some((c) => !c)) problems.push(`bitmap empty in a column: ${report.cells}`);
      if (report.cards.some((c) => c !== 8)) problems.push(`stat cards wrong per column: ${report.cards}`);
      if (report.streams.some((c) => !c)) problems.push(`event stream empty in a column: ${report.streams}`);
      if (problems.length) { console.log(`FAIL side-by-side: ${problems.join("; ")}`); failures++; }
      else console.log(`ok   side-by-side            2 columns  ${report.names.join("  vs  ")}`);
      if (SHOTS) {
        await s.send("Emulation.setDeviceMetricsOverride",
          { width: 2100, height: 1200, deviceScaleFactor: 1, mobile: false });
        await new Promise((r) => setTimeout(r, 400));
        const shot = await s.send("Page.captureScreenshot",
          { format: "png", captureBeyondViewport: true });
        fs.mkdirSync(SHOTS, { recursive: true });
        fs.writeFileSync(path.join(SHOTS, "side-by-side.png"), Buffer.from(shot.data, "base64"));
        console.log(`     shot ${path.join(SHOTS, "side-by-side.png")}`);
        await s.send("Emulation.clearDeviceMetricsOverride");
      }
      await s.evalJs("window.view.setLayout(1)");
    }

    if (new Set(loaded).size !== LOGS.length) {
      console.log(`FAIL expected ${LOGS.length} distinct logs, rendered ${new Set(loaded).size}`);
      failures++;
    }

    if (s.errors.length) {
      console.log(`FAIL ${s.errors.length} JS error(s):`);
      for (const e of [...new Set(s.errors)].slice(0, 5)) console.log(`     ${e}`);
      failures += s.errors.length;
    }
  } catch (err) {
    console.error("render check could not run:", err.message);
    failures++;
  } finally {
    proc.kill();
    server.close();
  }
  process.exit(failures ? 1 : 0);
}

main();
