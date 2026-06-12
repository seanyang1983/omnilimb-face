/*
 * property-19-live3d-blink-interval.test.js — property test for task 5.4.
 *
 * Feature: switchable-avatar-renderers, Property 19: Live3D 眨眼间隔区间
 * Validates: Requirements 5.5
 *
 * Property 19 (Live3D 眨眼间隔区间): For ANY consecutive blink-interval sample
 * produced by the idle blink scheduler, every interval falls within the
 * [2000, 6000] millisecond range.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): each run re-samples the
 * renderer's blink-interval scheduler (`_nextBlinkInterval()`), so across the
 * runs many independent random intervals are checked against the bound. Live3D
 * is constructed against the shared THREE / WebGL stubs (no real GPU).
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeStubThree, stubCanvasWebGL } = require("./helpers/conformance");

function loadLive3D() {
  const THREE = makeStubThree();
  THREE.DirectionalLight = function () { return { position: { set: () => {} } }; };
  THREE.AmbientLight = function () { return {}; };
  const window = loadFrontend(["live3d-renderer.js"], { extraGlobals: { THREE } });
  const canvas = window.document.getElementById("avatar");
  stubCanvasWebGL(canvas);
  return new window.Live3DRenderer(canvas, { type: "Live3D" });
}

test("Property 19: every blink interval sample lies within [2000,6000] ms (>=100 runs)", () => {
  const r = loadLive3D();
  try {
    fc.assert(
      // The generated integer is unused; it just drives independent re-sampling.
      fc.property(fc.integer(), () => {
        const ms = r._nextBlinkInterval();
        assert.equal(typeof ms, "number", "interval is a number");
        assert.ok(Number.isFinite(ms), "interval is finite");
        assert.ok(ms >= 2000 && ms <= 6000, `blink interval ${ms} within [2000,6000]`);
      }),
      { numRuns: 200 }
    );
  } finally {
    r.destroy();
  }
});
