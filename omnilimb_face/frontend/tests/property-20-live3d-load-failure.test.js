/*
 * property-20-live3d-load-failure.test.js — property test for task 5.5.
 *
 * Feature: switchable-avatar-renderers, Property 20: Live3D 加载失败的安全态
 * Validates: Requirements 5.2, 2.10
 *
 * Property 20 (Live3D 加载失败的安全态): For ANY VRM load-failure reason (network
 * failure, unparseable / non-VRM asset, invalid url, loader unavailable):
 * Live3DRenderer enters a failure state that shows visible failure text, does
 * NOT set/render a partial or broken model (the model reference stays
 * unloaded), preserves the rest of the page's functionality, and throws no
 * uncaught error (the setModel promise resolves to a failure result, never
 * rejects).
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator samples the
 * failure mode and an arbitrary url, installing the matching GLTFLoader/three-vrm
 * stub behaviour so each failure path is exercised without a network or GPU.
 * (The 10s wall-clock timeout itself is covered by manual verification per the
 * design; the network-failure mode stands in for the timeout's safe-state path.)
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeStubThree, stubCanvasWebGL } = require("./helpers/conformance");

/** GLTFLoader stub whose load() fails per `mode` ("error" | "no-vrm"). */
function makeStubGLTFLoaderCtor(mode) {
  return function GLTFLoader() {
    return {
      register() {},
      load(url, onLoad, _onProgress, onError) {
        const fire = (fn, arg) => Promise.resolve().then(() => fn(arg));
        if (mode === "error") fire(onError, new Error("network failure: 404 not found"));
        else if (mode === "no-vrm") fire(onLoad, { userData: {} });
      },
    };
  };
}

/**
 * Load Live3D with stubs appropriate to the failure `mode`:
 *   - "invalid-url"       : valid loader present, but setModel("") is called;
 *   - "loader-unavailable": no GLTFLoader / three-vrm installed;
 *   - "error" / "no-vrm"  : GLTFLoader stub that errors / returns a non-VRM.
 */
function loadLive3D(mode) {
  const THREE = makeStubThree();
  THREE.DirectionalLight = function () { return { position: { set: () => {} } }; };
  THREE.AmbientLight = function () { return {}; };
  const extraGlobals = { THREE };
  if (mode === "error" || mode === "no-vrm") {
    THREE.GLTFLoader = makeStubGLTFLoaderCtor(mode);
    extraGlobals.THREE_VRM = {
      VRMLoaderPlugin: function (parser) { return { parser }; },
      VRMUtils: { deepDispose: () => {} },
    };
  }
  const window = loadFrontend(["live3d-renderer.js"], { extraGlobals });
  const canvas = window.document.getElementById("avatar");
  stubCanvasWebGL(canvas);
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  return { window, r };
}

test("Property 20: every VRM load-failure reason yields a safe failure state (>=100 runs)", async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom("invalid-url", "loader-unavailable", "error", "no-vrm"),
      fc.oneof(fc.webUrl(), fc.constantFrom("", "not-a-url", "https://x/a.vrm")),
      async (mode, url) => {
        const effectiveUrl = mode === "invalid-url" ? "" : (url || "https://x/a.vrm");
        const { window, r } = loadLive3D(mode);
        try {
          assert.equal(r._vrm, null, "no model installed before load");

          let res;
          await assert.doesNotReject(async () => {
            res = await r.setModel(effectiveUrl);
          }, "setModel never rejects (no uncaught error)");

          // Failure result, no partial/broken model installed (R5.2/2.10).
          assert.equal(res.ok, false, "failure returns ok:false");
          assert.ok(res.error && String(res.error).length > 0, "a failure reason is returned");
          assert.equal(r._vrm, null, "no partial/broken model installed");
          assert.equal(r._modelUrl == null || r._modelUrl === undefined, true, "no model url recorded on failure");

          // Visible on-canvas failure text is shown.
          const errEl = window.document.querySelector(".live3d-error");
          assert.ok(errEl, "a visible failure message element is shown");
          assert.ok(errEl.textContent && errEl.textContent.length > 0, "failure message has text");

          // The rest of the renderer's functionality is preserved (no throws).
          assert.doesNotThrow(() => r.setMouthOpen(0.5));
          assert.doesNotThrow(() => r.resetMouth());
          assert.doesNotThrow(() => r.setExpression(-1, "happy"));
          assert.doesNotThrow(() => r.playMotion("Idle"));
        } finally {
          r.destroy();
        }
      }
    ),
    { numRuns: 100 }
  );
});
