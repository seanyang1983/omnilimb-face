/*
 * smoke.test.js — proves the test harness runs end-to-end.
 *
 * This is a MINIMAL smoke test for task 1.2 (harness + scaffold setup only). It
 * demonstrates that:
 *   - jsdom + the build-free loader can evaluate the UNMODIFIED front-end IIFE
 *     scripts and expose their globals (RendererManager, CanvasAvatar, ...);
 *   - the shared renderer-conformance scaffold can assert a renderer exposes the
 *     six interface methods (the foundation reused by task 1.3's property test);
 *   - the reusable fakes work and conform;
 *   - fast-check is wired up and runs (>=100 iterations) against the harness.
 *
 * The full interface-conformance PROPERTY test across every renderer lives in
 * task 1.3 (Property 7); this file only smoke-checks the plumbing.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const {
  REQUIRED_METHODS,
  assertRendererInterface,
  checkRendererInterface,
  makeFakeRenderer,
  RecordingFakeRenderer,
  makeStubThree,
  stubCanvasWebGL,
} = require("./helpers/conformance");

test("harness: front-end IIFE scripts load into jsdom and expose globals", () => {
  const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
  assert.equal(typeof window.RendererManager, "function", "RendererManager class is exposed");
  assert.equal(typeof window.CanvasAvatar, "function", "CanvasAvatar class is exposed");
  assert.equal(typeof window.Live2DAvatar, "function", "Live2DAvatar class is exposed");
  assert.deepEqual(
    Array.from(window.RendererManager.RENDERER_TYPES),
    ["Live2D", "Live3D"],
    "RENDERER_TYPES constant is intact"
  );
});

test("scaffold: existing renderer classes conform to Renderer_Interface", () => {
  const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
  // Assert against the class prototypes (no construction / no real WebGL needed).
  assertRendererInterface(assert, window.CanvasAvatar, { label: "CanvasAvatar" });
  assertRendererInterface(assert, window.Live2DAvatar, { label: "Live2DAvatar" });
});

test("scaffold: a constructed CanvasAvatar instance conforms", () => {
  const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
  const canvas = window.document.getElementById("avatar");
  const avatar = new window.CanvasAvatar(canvas);
  try {
    assertRendererInterface(assert, avatar, { label: "CanvasAvatar instance" });
  } finally {
    avatar.destroy();
  }
});

test("fakes: recording fake renderer conforms and records calls", () => {
  const fake = makeFakeRenderer({ type: "Live3D", withAgentState: true });
  // A fake that also exposes the optional setAgentState.
  assertRendererInterface(assert, fake, {
    label: "RecordingFakeRenderer",
    requireSetAgentState: true,
  });

  fake.setMouthOpen(0.5);
  fake.setExpression(2, "joy");
  fake.resetMouth();
  fake.playMotion("Tap");
  fake.setAgentState("speaking");
  fake.destroy();

  assert.deepEqual(
    fake.calls.map((c) => c.method),
    ["setMouthOpen", "setExpression", "resetMouth", "playMotion", "setAgentState", "destroy"]
  );
  assert.equal(fake.destroyed, true);
});

test("fakes: a renderer that omits setAgentState fails the optional check", () => {
  const fake = makeFakeRenderer({ withAgentState: false });
  // The six required methods are still present...
  assertRendererInterface(assert, fake, { label: "no-agent-state fake" });
  // ...but requiring the optional method should report it missing.
  const result = checkRendererInterface(fake, { requireSetAgentState: true });
  assert.equal(result.ok, false);
  assert.deepEqual(result.missing, ["setAgentState"]);
});

test("fakes: THREE stub and WebGL-stubbed canvas are available for later renderer tests", () => {
  const window = loadFrontend([]);
  const THREE = makeStubThree();
  assert.equal(THREE.isStub, true);
  assert.equal(typeof THREE.WebGLRenderer, "function");

  const canvas = window.document.getElementById("avatar");
  stubCanvasWebGL(canvas);
  const gl = canvas.getContext("webgl");
  assert.ok(gl && gl.isStub, "stubbed canvas returns a stub WebGL context");
});

test("fast-check: setMouthOpen never throws and clamps finite numbers to [0,1]", () => {
  fc.assert(
    fc.property(
      // Any value: finite numbers, NaN/Infinity, strings, null, undefined, objects.
      fc.oneof(
        fc.double({ noDefaultInfinity: false, noNaN: false }),
        fc.constantFrom(NaN, Infinity, -Infinity, null, undefined, "x", {}, [])
      ),
      (v) => {
        const r = new RecordingFakeRenderer();
        r.setMouthOpen(v); // must never throw for any input
        // Invariant: the applied mouth target is always within [0,1].
        assert.ok(r.mouthOpen >= 0 && r.mouthOpen <= 1);
        if (typeof v === "number" && Number.isFinite(v)) {
          assert.equal(r.mouthOpen, Math.max(0, Math.min(1, v)));
        }
      }
    ),
    { numRuns: 100 }
  );
});
