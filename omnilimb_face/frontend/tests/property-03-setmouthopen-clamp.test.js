/*
 * property-03-setmouthopen-clamp.test.js — property test for task 4.3.
 *
 * Feature: switchable-avatar-renderers, Property 3: setMouthOpen 的 clamp / 非数值 / 线性不变量
 * Validates: Requirements 2.9, 6.1, 6.2, 6.3, 6.4, 6.5, 12.3, 12.4
 *
 * Property 3 (setMouthOpen clamp/非数值/线性): For ANY renderer instance and ANY
 * input `v`: after setMouthOpen(v), if `v` is a finite number the effective
 * mouth target equals clamp(v, 0, 1) (v<0→0, v>1→1, in-range linear == v); if
 * `v` is NaN/null/undefined/non-number the mouth target keeps its prior value;
 * nothing ever throws; and resetMouth() always sets the mouth target to 0.
 *
 * This is a PROPERTY test (fast-check, >=100 runs). It is applied ACROSS the
 * renderers that implement the unified mouth-target contract (Live3DRenderer
 * and the conformant RecordingFakeRenderer), each constructed
 * against the shared THREE / WebGL stubs so no real GPU is needed. The legacy
 * CanvasAvatar intentionally resets to 0 on non-numbers (Requirement 4
 * regression protection), so it is out of this property's scope.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeFakeRenderer, makeStubThree, stubCanvasWebGL } = require("./helpers/conformance");

/** Factory descriptors: each yields { renderer, mouthOf(r), destroy() }. */
const RENDERER_FACTORIES = [
  {
    label: "Live3DRenderer",
    make() {
      const THREE = makeStubThree();
      THREE.DirectionalLight = function () { return { position: { set: () => {} } }; };
      THREE.AmbientLight = function () { return {}; };
      const window = loadFrontend(["live3d-renderer.js"], { extraGlobals: { THREE } });
      const canvas = window.document.getElementById("avatar");
      stubCanvasWebGL(canvas);
      const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
      return { renderer: r, mouthOf: () => r._mouthTarget, destroy: () => r.destroy() };
    },
  },
  {
    label: "RecordingFakeRenderer",
    make() {
      const r = makeFakeRenderer({ type: "Live2D" });
      return { renderer: r, mouthOf: () => r.mouthOpen, destroy: () => {} };
    },
  },
];

const anyInput = fc.oneof(
  fc.double({ noDefaultInfinity: false, noNaN: false }),
  fc.constantFrom(NaN, Infinity, -Infinity, null, undefined, "x", {}, [])
);

for (const desc of RENDERER_FACTORIES) {
  test(`Property 3: setMouthOpen clamp/keep/linear + resetMouth==0 holds for ${desc.label} (>=100 runs)`, () => {
    const { renderer, mouthOf, destroy } = desc.make();
    try {
      fc.assert(
        fc.property(anyInput, (v) => {
          const prior = mouthOf();
          assert.doesNotThrow(() => renderer.setMouthOpen(v), "setMouthOpen never throws");
          const after = mouthOf();
          // Target always within [0,1].
          assert.ok(after >= 0 && after <= 1, "mouth target stays within [0,1]");
          if (typeof v === "number" && Number.isFinite(v)) {
            assert.equal(after, Math.max(0, Math.min(1, v)), "finite input → clamp(v,0,1), linear in range");
          } else {
            assert.equal(after, prior, "non-finite input keeps the prior mouth target");
          }
          // resetMouth always returns the target to 0.
          assert.doesNotThrow(() => renderer.resetMouth());
          assert.equal(mouthOf(), 0, "resetMouth sets the mouth target to 0");
        }),
        { numRuns: 100 }
      );
    } finally {
      destroy();
    }
  });
}
