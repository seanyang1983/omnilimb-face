/*
 * orb-renderer.test.js — unit tests for the Orb renderer (particle sphere +
 * sound-wave律动 + Agent_State 角标).
 *
 * Constructs window.OrbRenderer against the shared THREE / WebGL stubs (no real
 * GPU) and asserts:
 *   - it is exposed as a global constructor and conforms to Renderer_Interface
 *     (the six methods + the optional setAgentState);
 *   - setMouthOpen clamps finite numbers to [0,1] and keeps the prior value on
 *     non-finite input, never throwing; resetMouth returns to the rest baseline
 *     (mouth 0, deform scale 1);
 *   - setAgentState switches the visual config + label for each valid state and
 *     ignores invalid states (visual + label unchanged, never throws);
 *   - setExpression / playMotion / setModel are safe no-ops (no observable
 *     state change, never throw);
 *   - destroy() is idempotent and never throws;
 *   - a missing three.js dependency makes the constructor throw (clean failure
 *     so RendererManager can degrade, R9 / 12.7).
 *
 * Requirements: 1.1, 2.1, 2.5, 6.1–6.5, 12.2, 12.5, 12.6, 12.9
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { loadFrontend } = require("./helpers/load-frontend");
const {
  assertRendererInterface,
  makeStubThree,
  stubCanvasWebGL,
} = require("./helpers/conformance");

/** Build an OrbRenderer against the stub THREE + a WebGL-stubbed canvas. */
function makeOrb() {
  const window = loadFrontend(["orb-renderer.js"], { extraGlobals: { THREE: makeStubThree() } });
  const canvas = window.document.getElementById("avatar");
  stubCanvasWebGL(canvas);
  const orb = new window.OrbRenderer(canvas, { type: "Orb" });
  return { window, canvas, orb };
}

test("OrbRenderer is exposed as a global constructor", () => {
  const window = loadFrontend(["orb-renderer.js"], { extraGlobals: { THREE: makeStubThree() } });
  assert.equal(typeof window.OrbRenderer, "function");
});

test("OrbRenderer conforms to Renderer_Interface incl. the optional setAgentState (R2.1/2.5)", () => {
  const { orb } = makeOrb();
  try {
    assertRendererInterface(assert, orb, { label: "OrbRenderer", requireSetAgentState: true });
    assert.equal(orb.type, "Orb");
  } finally {
    orb.destroy();
  }
});

test("setMouthOpen clamps to [0,1]; non-finite keeps the prior value; never throws (R6.1-6.4)", () => {
  const { orb } = makeOrb();
  try {
    orb.setMouthOpen(0.5);
    assert.equal(orb._mouthTarget, 0.5);
    orb.setMouthOpen(2);
    assert.equal(orb._mouthTarget, 1, "above-range clamps to 1");
    orb.setMouthOpen(-3);
    assert.equal(orb._mouthTarget, 0, "below-range clamps to 0");

    orb.setMouthOpen(0.7);
    for (const bad of [NaN, Infinity, -Infinity, null, undefined, "x", {}, []]) {
      assert.doesNotThrow(() => orb.setMouthOpen(bad));
      assert.equal(orb._mouthTarget, 0.7, "non-finite input keeps the prior value");
    }
  } finally {
    orb.destroy();
  }
});

test("resetMouth returns the orb to the rest baseline (mouth 0, deform scale 1) (R6.5)", () => {
  const { orb } = makeOrb();
  try {
    orb.setMouthOpen(1);
    orb._deformScale = 1.18;
    orb.resetMouth();
    assert.equal(orb._mouthTarget, 0);
    assert.equal(orb._deformScale, 1);
  } finally {
    orb.destroy();
  }
});

test("setAgentState switches visual config + label for each valid state (R12.2/12.5)", () => {
  const { orb } = makeOrb();
  try {
    for (const [state, label] of [
      ["idle", "空闲"],
      ["listening", "聆听"],
      ["thinking", "思考"],
      ["speaking", "说话"],
    ]) {
      orb.setAgentState(state);
      assert.equal(orb._agentState, state);
      assert.equal(orb._stateVisual.label, label);
      assert.ok(orb._label, "a state label overlay exists");
      assert.equal(orb._label.textContent, label, state + " -> " + label);
    }
  } finally {
    orb.destroy();
  }
});

test("setAgentState ignores invalid states: visual + label unchanged, never throws (R12.6)", () => {
  const { orb } = makeOrb();
  try {
    orb.setAgentState("speaking");
    const before = { state: orb._agentState, visual: orb._stateVisual, label: orb._label.textContent };
    for (const bad of ["bogus", "", null, undefined, 42, {}]) {
      assert.doesNotThrow(() => orb.setAgentState(bad));
      assert.equal(orb._agentState, before.state, "state unchanged for invalid input");
      assert.equal(orb._stateVisual, before.visual, "visual config unchanged");
      assert.equal(orb._label.textContent, before.label, "label unchanged");
    }
  } finally {
    orb.destroy();
  }
});

test("setExpression / playMotion / setModel are safe no-ops (R12.9)", () => {
  const { orb } = makeOrb();
  try {
    orb.setAgentState("listening");
    orb.setMouthOpen(0.4);
    const snap = { mouth: orb._mouthTarget, state: orb._agentState, visual: orb._stateVisual };
    assert.doesNotThrow(() => orb.setExpression(2, "joy"));
    assert.doesNotThrow(() => orb.playMotion("Tap"));
    assert.doesNotThrow(() => orb.setModel("https://x/m.json"));
    assert.equal(orb._mouthTarget, snap.mouth, "no-op calls leave mouth target unchanged");
    assert.equal(orb._agentState, snap.state, "no-op calls leave agent state unchanged");
    assert.equal(orb._stateVisual, snap.visual, "no-op calls leave visual unchanged");
  } finally {
    orb.destroy();
  }
});

test("destroy() is idempotent and never throws", () => {
  const { orb } = makeOrb();
  assert.doesNotThrow(() => orb.destroy());
  assert.doesNotThrow(() => orb.destroy(), "second destroy is a safe no-op");
});

test("missing three.js makes the constructor throw (clean failure for degrade, R9/12.7)", () => {
  // Load orb-renderer.js WITHOUT injecting THREE → window.THREE is absent.
  const window = loadFrontend(["orb-renderer.js"]);
  const canvas = window.document.getElementById("avatar");
  assert.throws(
    () => new window.OrbRenderer(canvas, { type: "Orb" }),
    /three\.js/,
    "constructor throws when three.js is missing"
  );
});
