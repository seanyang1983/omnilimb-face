/*
 * property-04-interface-noop-safety.test.js — property test for task 4.4.
 *
 * Feature: switchable-avatar-renderers, Property 4: 接口方法安全性与 no-op 语义
 * Validates: Requirements 2.8, 2.12, 6.7, 12.9
 *
 * Property 4 (接口方法安全性与 no-op 语义): For ANY renderer instance and ANY
 * interface-method call that exercises a capability the renderer does NOT
 * support (e.g. an out-of-range / unmapped setExpression index, or playMotion
 * on a renderer without motion groups): the call returns
 * immediately, modifies NO observable rendering state, and throws nothing.
 *
 * This is a PROPERTY test (fast-check, >=100 runs) applied to Live3DRenderer
 * (playMotion + out-of-range setExpression, R6.7), constructed against the
 * shared THREE / WebGL stubs (no real GPU). For every random call sequence we
 * snapshot the renderer's observable state before and after and assert it is
 * unchanged.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeStubThree, stubCanvasWebGL } = require("./helpers/conformance");

const argFor = {
  index: fc.integer({ min: -8, max: 16 }),
  negIndex: fc.integer({ min: -8, max: -1 }),
  name: fc.oneof(fc.string(), fc.constantFrom(null, undefined, "joy", "bogus")),
  group: fc.oneof(fc.string(), fc.constantFrom(undefined, "Tap")),
  url: fc.oneof(fc.string(), fc.constantFrom("", "https://x/m.vrm")),
  mouth: fc.oneof(fc.double({ noNaN: false }), fc.constantFrom(NaN, null, undefined, "x")),
  state: fc.oneof(fc.string(), fc.constantFrom("idle", "speaking", "bogus", null)),
};

const RENDERER_DESCRIPTORS = [
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
      return { renderer: r, destroy: () => r.destroy() };
    },
    snapshot: (r) => ({ active: r._activeExprPreset, pending: r._pendingExpression, mouth: r._mouthTarget }),
    // playMotion is a no-op; an out-of-range (negative) setExpression keeps the
    // current expression (R6.7) — both must not change observable state.
    noopCalls: [
      ["playMotion", () => argFor.group],
      ["setExpression", () => argFor.negIndex, () => argFor.name],
    ],
  },
];

for (const desc of RENDERER_DESCRIPTORS) {
  test(`Property 4: unsupported-capability calls are safe no-ops for ${desc.label} (>=100 runs)`, () => {
    const { renderer, destroy } = desc.make();
    try {
      // A call is: pick one of the no-op methods, draw its random args.
      const callArb = fc.constantFrom(...desc.noopCalls).chain((spec) => {
        const [method, ...argGens] = spec;
        const args = argGens.map((g) => g());
        return (args.length ? fc.tuple(...args) : fc.constant([])).map((vals) => ({ method, args: vals }));
      });

      fc.assert(
        fc.property(fc.array(callArb, { minLength: 1, maxLength: 24 }), (calls) => {
          const before = desc.snapshot(renderer);
          for (const { method, args } of calls) {
            assert.doesNotThrow(() => renderer[method](...args), `${desc.label}.${method} must never throw`);
          }
          const after = desc.snapshot(renderer);
          assert.deepEqual(after, before, `${desc.label}: no-op calls must not change observable state`);
        }),
        { numRuns: 100 }
      );
    } finally {
      destroy();
    }
  });
}
