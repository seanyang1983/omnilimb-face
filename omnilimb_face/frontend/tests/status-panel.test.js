/*
 * status-panel.test.js — unit tests for task 9.1 (StatusPanel / Telemetry).
 *
 * Covers the Status_Panel + Telemetry collection implemented as a pure,
 * build-free module in frontend/status-panel.js (window.StatusPanel),
 * Requirement 13:
 *
 *   - buildRows renders ALL metrics from a telemetry object: renderer type,
 *     model name+source, STT model,
 *     TTS voice, Connection_Status, Network_Latency, and Agent_State
 *     (R13.1, 13.2, 13.3, 13.4, 13.5, 13.6);
 *   - missing/unavailable metrics fall back to the placeholder ("未知") while the
 *     other metrics still render, and rendering NEVER throws — including for a
 *     non-object telemetry and disconnected/reconnecting connection states
 *     (R13.9, 13.11);
 *   - a Connection_Status change is reflected on the next render (R13.4/13.8),
 *     and the wire status strings (open/connecting/closed) normalize to the enum;
 *   - the Network_Latency refresh is driven by an INDEPENDENT setInterval whose
 *     period is strictly ≤5000ms (R13.5) — asserted here with an injected fake
 *     scheduler that captures the delay and lets us drive ticks manually.
 *
 * The optional property test P12 (task 9.2) drives buildRows/render with
 * arbitrary telemetry objects separately. These use the build-free jsdom loader
 * so no real browser/timers are required.
 *
 * Requirements: 13.1–13.11
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { loadFrontend } = require("./helpers/load-frontend");

/** Load just StatusPanel (it needs no other front-end globals). */
function loadSP() {
  const window = loadFrontend(["status-panel.js"]);
  return { StatusPanel: window.StatusPanel, window };
}

/**
 * A fake scheduler capturing the setInterval delay + callback so a test can
 * assert the period and drive ticks deterministically (fake timers).
 */
function makeScheduler() {
  const intervals = [];
  return {
    intervals,
    setInterval(fn, delay) {
      const id = intervals.length + 1;
      intervals.push({ id, fn, delay, cleared: false });
      return id;
    },
    clearInterval(id) {
      const iv = intervals.find((x) => x.id === id);
      if (iv) iv.cleared = true;
    },
    tick(id) {
      const iv = intervals.find((x) => x.id === id);
      if (iv && !iv.cleared) iv.fn();
    },
  };
}

// --- buildRows renders all metrics (R13.1-13.7) -------------------------

test("buildRows renders every metric from a full telemetry object (R13.1-13.6)", () => {
  const { StatusPanel } = loadSP();
  const rows = StatusPanel.buildRows({
    renderer: "Live2D",
    model: { name: "Mao", url: "https://cdn/Mao.model3.json" },
    stt: "base",
    tts: "zh-CN-XiaoxiaoNeural",
    connection: "connected",
    latencyMs: 42.6,
    agentState: "thinking",
  });
  const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));

  assert.equal(byKey["渲染器"].value, "Live2D");
  assert.equal(byKey["渲染器"].available, true);
  assert.ok(byKey["模型 / 来源"].value.includes("Mao"));
  assert.ok(byKey["模型 / 来源"].value.includes("https://cdn/Mao.model3.json"));
  assert.equal(byKey["STT 模型"].value, "base");
  assert.equal(byKey["TTS 语音"].value, "zh-CN-XiaoxiaoNeural");
  assert.equal(byKey["连接状态"].value, "已连接");
  assert.equal(byKey["网络延迟"].value, "43 ms"); // rounded
  assert.equal(byKey["智能体状态"].value, "思考");
  // The engine/GPU rows (formerly Digital_Human-only) are never rendered.
  assert.equal(byKey["引擎可用性"], undefined);
  assert.equal(byKey["GPU / ROCm"], undefined);
});

// --- missing metrics -> placeholder, never throws (R13.9/13.11) ---------

test("buildRows: missing metrics fall back to placeholder; others still render (R13.11)", () => {
  const { StatusPanel } = loadSP();
  const rows = StatusPanel.buildRows({ renderer: "Live3D", stt: "small" });
  const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));

  assert.equal(byKey["渲染器"].value, "Live3D");
  assert.equal(byKey["STT 模型"].value, "small");
  // Everything missing renders the placeholder and is flagged unavailable.
  for (const k of ["模型 / 来源", "TTS 语音", "连接状态", "网络延迟", "智能体状态"]) {
    assert.equal(byKey[k].value, StatusPanel.PLACEHOLDER, k + " should be placeholder");
    assert.equal(byKey[k].available, false);
  }
});

test("buildRows never throws on hostile/empty telemetry (R13.9/13.11)", () => {
  const { StatusPanel } = loadSP();
  const hostile = [
    undefined,
    null,
    {},
    "not-an-object",
    42,
    [],
    { renderer: 123, model: "x", stt: {}, tts: [], connection: "bogus", latencyMs: NaN, agentState: "??" },
    { renderer: "Digital_Human", digitalHuman: "nope" },
    { connection: "disconnected" },
    { connection: "reconnecting" },
  ];
  for (const t of hostile) {
    assert.doesNotThrow(() => {
      const rows = StatusPanel.buildRows(t);
      const html = StatusPanel.toHtml(rows);
      assert.equal(typeof html, "string");
      // An invalid renderer/connection/etc. must surface as a placeholder.
      const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));
      assert.ok(byKey["渲染器"]); // the renderer row always exists
    }, "must not throw for telemetry=" + JSON.stringify(t));
  }
});

test("toHtml escapes arbitrary strings so the grid cannot break (R13.11)", () => {
  const { StatusPanel } = loadSP();
  const html = StatusPanel.toHtml(
    StatusPanel.buildRows({ model: { name: "<script>alert(1)</script>", url: 'a"b' } })
  );
  assert.ok(!html.includes("<script>alert(1)</script>"), "raw script must be escaped");
  assert.ok(html.includes("&lt;script&gt;"), "angle brackets escaped");
});

// --- disconnected/reconnecting render without throwing (R13.9) ----------

test("disconnected/reconnecting connection states render their label, no throw (R13.9)", () => {
  const { StatusPanel, window } = loadSP();
  const container = window.document.getElementById("avatar"); // any element
  const panel = new StatusPanel({ container, scheduler: makeScheduler() });

  for (const [state, label] of [
    ["disconnected", "已断开"],
    ["reconnecting", "重连中"],
    ["connecting", "连接中"],
    ["connected", "已连接"],
  ]) {
    assert.doesNotThrow(() => panel.setConnection(state));
    const rows = StatusPanel.buildRows(panel.telemetry);
    const conn = rows.find((r) => r.key === "连接状态");
    assert.equal(conn.value, label, state + " -> " + label);
  }
});

// --- Connection_Status change is reflected (R13.4/13.8) -----------------

test("setConnection reflects a status change immediately and normalizes wire states (R13.4/13.8)", () => {
  const { StatusPanel } = loadSP();
  const panel = new StatusPanel({ scheduler: makeScheduler() });

  // Wire status strings normalize to the Connection_Status enum.
  panel.setConnection("open");
  assert.equal(panel.telemetry.connection, "connected");
  let conn = StatusPanel.buildRows(panel.telemetry).find((r) => r.key === "连接状态");
  assert.equal(conn.value, "已连接");

  // A change is reflected on the very next render.
  panel.setConnection("closed");
  assert.equal(panel.telemetry.connection, "disconnected");
  conn = StatusPanel.buildRows(panel.telemetry).find((r) => r.key === "连接状态");
  assert.equal(conn.value, "已断开");

  // Leaving the connected state clears the (now-unknown) latency to a placeholder.
  assert.equal(panel.telemetry.latencyMs, null);
});

// --- latency refresh uses an INDEPENDENT interval, strictly <=5000ms (R13.5) ---

test("latency refresh is driven by an independent setInterval with period <=5000ms (R13.5)", () => {
  const { StatusPanel } = loadSP();
  const scheduler = makeScheduler();
  let pings = 0;
  let nowVal = 10000;
  const panel = new StatusPanel({
    scheduler,
    sendPing: () => {
      pings++;
    },
    now: () => nowVal,
    // intentionally not provided -> default 3000ms (<=5000ms)
  });

  panel.start();
  // Exactly one independent interval was registered.
  assert.equal(scheduler.intervals.length, 1);
  const iv = scheduler.intervals[0];
  // Strictly enforced: the refresh period must be <= 5000ms (R13.5).
  assert.ok(iv.delay <= StatusPanel.MAX_LATENCY_INTERVAL_MS, "period " + iv.delay + " must be <=5000");
  assert.equal(iv.delay, 3000, "default refresh period is 3000ms");

  // Driving the interval sends a ping each tick (decoupled from any LLM/TTS work).
  scheduler.tick(iv.id);
  scheduler.tick(iv.id);
  assert.equal(pings, 2, "each tick sends one ping");

  // A pong with the echoed timestamp yields the round-trip latency.
  nowVal = 10075;
  panel.recordPong(10000);
  assert.equal(panel.telemetry.latencyMs, 75);
  const lat = StatusPanel.buildRows(panel.telemetry).find((r) => r.key === "网络延迟");
  assert.equal(lat.value, "75 ms");

  // stop() clears the interval (no leak).
  panel.stop();
  assert.equal(iv.cleared, true);
});

test("intervalMs above the hard cap is clamped to 5000ms (R13.5)", () => {
  const { StatusPanel } = loadSP();
  const scheduler = makeScheduler();
  const panel = new StatusPanel({ scheduler, intervalMs: 99999 });
  panel.start();
  assert.equal(scheduler.intervals[0].delay, 5000, "period clamped to the 5000ms cap");
  panel.stop();
});

// --- instance render writes escaped HTML to its container, never throws --

test("render writes the telemetry grid into its container and never throws (R13.9/13.11)", () => {
  const { StatusPanel, window } = loadSP();
  const container = window.document.getElementById("avatar");
  const panel = new StatusPanel({ container, scheduler: makeScheduler() });

  assert.doesNotThrow(() => panel.update({ renderer: "Live3D", agentState: "speaking" }));
  assert.ok(container.innerHTML.includes("Live3D"));
  assert.ok(container.innerHTML.includes("说话"));
  // Missing metrics still rendered as placeholders.
  assert.ok(container.innerHTML.includes(StatusPanel.PLACEHOLDER));
});
