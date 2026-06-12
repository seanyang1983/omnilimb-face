/*
 * property-16-setexpression-single-active.test.js — property test for task 5.3.
 *
 * Feature: switchable-avatar-renderers, Property 16: setExpression 的单活表情不变量
 * Validates: Requirements 6.6
 *
 * Property 16 (单活表情不变量): For ANY sequence of valid Emotion_Index values
 * (whose mapped expression exists in the VRM model), after applying them in
 * order, at ANY point at most ONE expression's blendshape weight is greater
 * than 0 — i.e. the previous expression's weight is zeroed before the new one
 * is applied.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator builds random
 * sequences of emotion names drawn from the renderer's emotion vocabulary
 * (mapping to the VRM presets the stub model exposes). A recording VRM captures
 * every expressionManager.setValue(preset, weight); after each setExpression we
 * replay the recorded writes to reconstruct the live weight map and assert the
 * single-active invariant. No real GPU / three-vrm is needed.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeStubThree, stubCanvasWebGL } = require("./helpers/conformance");

// Emotion preset universe the stub VRM exposes (matches the renderer's map).
const PRESETS = ["neutral", "relaxed", "happy", "angry", "sad", "surprised"];
// Emotion names (setExpression `name` arg) that map onto those presets.
const EMOTION_NAMES = [
  "neutral", "calm", "relaxed", "happy", "joy", "smile",
  "angry", "mad", "sad", "sorrow", "surprised", "shock", "fear",
];

function loadLive3D() {
  const THREE = makeStubThree();
  THREE.DirectionalLight = function () { return { position: { set: () => {} } }; };
  THREE.AmbientLight = function () { return {}; };
  const window = loadFrontend(["live3d-renderer.js"], { extraGlobals: { THREE } });
  const canvas = window.document.getElementById("avatar");
  stubCanvasWebGL(canvas);
  return new window.Live3DRenderer(canvas, { type: "Live3D" });
}

/** A recording VRM whose model "has" all emotion presets + a mouth preset. */
function makeRecordingVrm() {
  const calls = [];
  const has = new Set(PRESETS.concat(["aa", "blink"]));
  const em = {
    setValue: (name, v) => calls.push([name, v]),
    getExpression: (name) => (has.has(name) ? { expressionName: name } : null),
  };
  return {
    calls,
    vrm: {
      scene: { rotation: { y: 0 }, position: { y: 0 } },
      update: () => {},
      expressionManager: em,
    },
  };
}

/** Reconstruct the current emotion-preset weight map from recorded setValue calls. */
function liveWeights(calls) {
  const w = new Map();
  for (const [name, v] of calls) {
    if (PRESETS.indexOf(name) !== -1) w.set(name, v);
  }
  return w;
}

test("Property 16: at most one emotion preset has weight > 0 after any sequence (>=100 runs)", () => {
  fc.assert(
    fc.property(
      fc.array(fc.constantFrom(...EMOTION_NAMES), { minLength: 0, maxLength: 30 }),
      (names) => {
        const r = loadLive3D();
        try {
          const rec = makeRecordingVrm();
          r._installVrm(rec.vrm);

          let idx = 0;
          for (const name of names) {
            r.setExpression(idx++, name); // index >= 0, valid mapped names
            // Single-active invariant holds after EVERY application.
            const active = [...liveWeights(rec.calls).entries()].filter(([, v]) => v > 0);
            assert.ok(active.length <= 1, `at most one active expression, saw: ${active.map((a) => a[0]).join(",")}`);
          }

          // Final state also satisfies the invariant; and the single active
          // preset (if any) equals the renderer's tracked active preset.
          const finalActive = [...liveWeights(rec.calls).entries()].filter(([, v]) => v > 0).map((a) => a[0]);
          assert.ok(finalActive.length <= 1);
          if (finalActive.length === 1) {
            assert.equal(finalActive[0], r._activeExprPreset, "live active weight matches tracked active preset");
          }
        } finally {
          r.destroy();
        }
      }
    ),
    { numRuns: 100 }
  );
});
