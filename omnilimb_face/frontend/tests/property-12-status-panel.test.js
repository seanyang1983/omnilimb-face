/*
 * property-12-status-panel.test.js — property test for task 9.2.
 *
 * Feature: switchable-avatar-renderers, Property 12: Status_Panel 渲染的完整性与健壮性
 * Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.6, 13.7, 13.9, 13.11
 *
 * Property 12 (Status_Panel 渲染的完整性与健壮性): For ANY telemetry state object
 * (arbitrary / missing fields, disconnected/reconnecting connection states,
 * hostile/wrong-typed values), the panel renders each metric — renderer type,
 * model/source, STT, TTS, Connection_Status, and Agent_State — with EITHER a
 * valid value (within its legal enum/label set) OR a placeholder ("未知" / "—"); a
 * missing metric shows the placeholder while the OTHER metrics still render; and
 * rendering NEVER throws.
 *
 * This is a PROPERTY test (fast-check, >=100 runs). The generator produces
 * arbitrary telemetry objects: each field is independently sampled from its valid
 * domain, a hostile/wrong-typed domain, or omitted entirely; the whole telemetry
 * is itself sometimes a non-object (null/undefined/string/number/array) so the
 * top-level robustness guard is exercised too. It drives the pure renderers
 * window.StatusPanel.buildRows(telemetry) / toHtml(rows) exposed by
 * frontend/status-panel.js.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");

/** Load just StatusPanel (it needs no other front-end globals). */
function loadSP() {
  const window = loadFrontend(["status-panel.js"]);
  return window.StatusPanel;
}

const StatusPanel = loadSP();
const PLACEHOLDER = StatusPanel.PLACEHOLDER; // "未知"
const CONNECTION_LABELS = StatusPanel.CONNECTION_LABELS;
const AGENT_LABELS = StatusPanel.AGENT_LABELS;

// The set of acceptable placeholders per the property ("未知" / "—").
const PLACEHOLDERS = new Set([PLACEHOLDER, "—"]);

// ----- generators ---------------------------------------------------------

// Valid domains for the enum-bound metrics.
const validRenderer = fc.constantFrom("Live2D", "Live3D");
// Connection accepts both the enum and the wire status strings the panel maps.
const validConnection = fc.constantFrom(
  "connected", "connecting", "reconnecting", "disconnected",
  "open", "closed", "error"
);
const validAgent = fc.constantFrom("idle", "listening", "thinking", "speaking");

// A broad "hostile / wrong-typed" scalar space: arbitrary strings (incl. the
// markup-bearing ones), numbers, booleans, null/NaN/Infinity, objects/arrays.
const hostileScalar = fc.oneof(
  fc.string(),
  fc.constantFrom("<script>alert(1)</script>", 'a"b<>&', " ", ""),
  fc.integer(),
  fc.double(),
  fc.boolean(),
  fc.constantFrom(null, undefined, NaN, Infinity, -Infinity),
  fc.object(),
  fc.array(fc.anything())
);

// Field generators: each mixes the valid domain with the hostile space so the
// "valid value OR placeholder" invariant is exercised on both sides.
const rendererField = fc.oneof(validRenderer, hostileScalar);
const connectionField = fc.oneof(validConnection, hostileScalar);
const agentField = fc.oneof(validAgent, hostileScalar);
const stringField = fc.oneof(fc.string(), hostileScalar);
const latencyField = fc.oneof(
  fc.double(),
  fc.double({ min: 0, max: 100000 }),
  hostileScalar
);
const modelField = fc.oneof(
  fc.record(
    { name: fc.oneof(fc.string(), fc.constant(undefined)), url: fc.oneof(fc.string(), fc.constant(undefined)) },
    { requiredKeys: [] }
  ),
  hostileScalar
);

// A structured telemetry object where EVERY field is optional (may be omitted),
// so "missing metric -> placeholder while others render" is covered.
const structuredTelemetry = fc.record(
  {
    renderer: rendererField,
    model: modelField,
    stt: stringField,
    tts: stringField,
    connection: connectionField,
    latencyMs: latencyField,
    agentState: agentField,
  },
  { requiredKeys: [] }
);

// The top-level telemetry is sometimes not even an object (R13.11 hostile guard).
const anyTelemetry = fc.oneof(
  structuredTelemetry,
  fc.constantFrom(null, undefined),
  fc.string(),
  fc.integer(),
  fc.array(fc.anything())
);

// ----- helpers ------------------------------------------------------------

/** Mirror buildRows' top-level guard: a non-object telemetry is treated as {}. */
function asObject(t) {
  return t && typeof t === "object" ? t : {};
}

/** Generic per-row invariant: a row is EITHER a valid value OR a placeholder. */
function assertRowShape(r) {
  assert.equal(typeof r.key, "string");
  assert.equal(typeof r.available, "boolean");
  if (r.available) {
    // A valid (available) metric is a non-empty string value.
    assert.equal(typeof r.value, "string");
    assert.ok(r.value.length > 0, "available value must be non-empty: " + r.key);
  } else {
    // A missing/unavailable metric shows the placeholder.
    assert.ok(
      PLACEHOLDERS.has(r.value),
      "unavailable metric must be a placeholder, got: " + JSON.stringify(r.value)
    );
  }
}

// ----- the property -------------------------------------------------------

test("Property 12: Status_Panel renders every metric as a valid value or placeholder and never throws (>=100 runs)", () => {
  fc.assert(
    fc.property(anyTelemetry, (telemetry) => {
      // 1) Rendering NEVER throws (R13.9/13.11), for any telemetry.
      let rows;
      let html;
      assert.doesNotThrow(() => {
        rows = StatusPanel.buildRows(telemetry);
        html = StatusPanel.toHtml(rows);
      }, "buildRows/toHtml must not throw for telemetry=" + JSON.stringify(telemetry));

      assert.ok(Array.isArray(rows), "buildRows returns an array");
      assert.equal(typeof html, "string", "toHtml returns a string");

      // 2) Every row satisfies "valid value OR placeholder" (R13.11).
      for (const r of rows) assertRowShape(r);

      const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));
      const t = asObject(telemetry);
      const renderer = StatusPanel.normalizeRenderer(t.renderer);

      // 3) The always-present metrics exist regardless of input (R13.1/13.3/13.4/13.6).
      //    Renderer, STT, TTS, Connection_Status and Agent_State are always rows.
      for (const key of ["渲染器", "STT 模型", "TTS 语音", "连接状态", "智能体状态", "网络延迟"]) {
        assert.ok(byKey[key], "metric row must always be present: " + key);
      }

      // 4) Renderer type: valid -> echoed value; else placeholder (R13.1).
      if (renderer !== null) {
        assert.equal(byKey["渲染器"].value, renderer);
        assert.equal(byKey["渲染器"].available, true);
      } else {
        assert.equal(byKey["渲染器"].value, PLACEHOLDER);
        assert.equal(byKey["渲染器"].available, false);
      }

      // 5) Connection_Status: valid -> its label; else placeholder (R13.4/13.9).
      const conn = StatusPanel.normalizeConnection(t.connection);
      if (conn !== null) {
        assert.equal(byKey["连接状态"].value, CONNECTION_LABELS[conn]);
        assert.equal(byKey["连接状态"].available, true);
      } else {
        assert.equal(byKey["连接状态"].value, PLACEHOLDER);
        assert.equal(byKey["连接状态"].available, false);
      }

      // 6) Agent_State: valid -> its label; else placeholder (R13.6).
      const agent = StatusPanel.normalizeAgentState(t.agentState);
      if (agent !== null) {
        assert.equal(byKey["智能体状态"].value, AGENT_LABELS[agent]);
        assert.equal(byKey["智能体状态"].available, true);
      } else {
        assert.equal(byKey["智能体状态"].value, PLACEHOLDER);
        assert.equal(byKey["智能体状态"].available, false);
      }

      // 7) The model/source row is always present; the former Digital_Human-only
      //    engine + GPU rows are never rendered (R13.2).
      assert.ok(byKey["模型 / 来源"], "model/source row present (R13.2)");
      assert.equal(byKey["数字人引擎"], undefined, "no engine row");
      assert.equal(byKey["引擎可用性"], undefined, "no engine availability row");
      assert.equal(byKey["GPU / ROCm"], undefined, "no GPU row");

      // 8) HTML escaping robustness: every "<" in the output belongs to one of
      //    the panel's own <div> tags — hostile string values cannot inject markup
      //    (R13.11). Stripping the grid's own div tags must leave no "<".
      const stripped = html.replace(/<\/?div[^>]*>/g, "");
      assert.ok(!stripped.includes("<"), "no unescaped '<' may leak into the grid HTML");
      assert.ok(!stripped.includes(">"), "no unescaped '>' may leak into the grid HTML");
    }),
    { numRuns: 100 }
  );
});
