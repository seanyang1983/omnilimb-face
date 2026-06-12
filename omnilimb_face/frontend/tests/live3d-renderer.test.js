/*
 * live3d-renderer.test.js — unit + property tests for task 5.1.
 *
 * Covers Live3DRenderer's task-5.1 scope under jsdom, using the shared
 * conformance scaffold's THREE / WebGL stubs (no real GPU needed), extended
 * locally with GLTFLoader / VRMLoaderPlugin stubs so VRM loading paths can be
 * exercised without three-vrm or a network:
 *
 *   - interface conformance: the six Renderer_Interface methods exist and are
 *     functions (Live3D does NOT require setAgentState) — Requirement 2.3;
 *   - construction with three.js + WebGL available succeeds and renders to the
 *     provided canvas with a transparent clear (alpha=0) — Requirement 5.3;
 *   - construction FAILS CLEANLY (throws) when three.js is missing, and when
 *     WebGL is unavailable — so RendererManager degrades (Requirement 9);
 *   - blink interval sampler `_nextBlinkInterval()` always returns ms in
 *     [2000,6000] (property test, >=100 runs) — Requirement 5.5;
 *   - setModel() FAILURE paths (invalid url / loader unavailable / loader error)
 *     return a failure result and show a visible on-canvas failure message
 *     WITHOUT throwing, and do NOT install a partial/broken model —
 *     Requirements 5.2 / 2.10;
 *   - setModel() SUCCESS path installs the VRM and clears any prior error;
 *   - setMouthOpen never throws and clamps finite numbers to [0,1].
 *
 * Real VRM rendering, the 10s timeout wall-clock, and the >=30 FPS target
 * (R5.1) require manual / visual verification.
 *
 * Requirements: 5.1 (load), 5.2 (failure), 5.3 (transparent), 5.4/5.5 (idle/blink), 2.3, 2.10
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const {
  assertRendererInterface,
  makeStubThree,
  stubCanvasWebGL,
} = require("./helpers/conformance");

/**
 * Build a minimal three-vrm-shaped VRM object the renderer can install/drive.
 * @returns {object}
 */
function makeStubVrm() {
  const setValues = [];
  return {
    scene: { rotation: { y: 0 }, position: { y: 0 } },
    update: () => {},
    expressionManager: {
      _values: setValues,
      setValue: (name, v) => setValues.push([name, v]),
    },
  };
}

/**
 * Make a GLTFLoader constructor stub whose `load(url, onLoad, onProgress, onError)`
 * behaves per `mode`:
 *   - "success": calls onLoad(gltf) with gltf.userData.vrm set (async microtask);
 *   - "error":   calls onError(new Error(...)) (async microtask);
 *   - "no-vrm":  calls onLoad(gltf) WITHOUT a vrm in userData;
 *   - "never":   never calls back (would hit the renderer's 10s timeout).
 * @param {string} mode
 * @returns {Function} a GLTFLoader constructor.
 */
function makeStubGLTFLoaderCtor(mode) {
  return function GLTFLoader() {
    return {
      register() {
        /* VRMLoaderPlugin registration recorded as a no-op */
      },
      load(url, onLoad, _onProgress, onError) {
        const fire = (fn, arg) => Promise.resolve().then(() => fn(arg));
        if (mode === "success") {
          const gltf = { userData: { vrm: makeStubVrm() } };
          fire(onLoad, gltf);
        } else if (mode === "no-vrm") {
          fire(onLoad, { userData: {} });
        } else if (mode === "error") {
          fire(onError, new Error("network failure: 404 not found"));
        }
        /* "never": intentionally no callback */
      },
    };
  };
}

/**
 * Load live3d-renderer.js into a fresh jsdom window with stubbed three.js +
 * (optionally) GLTFLoader + three-vrm, and a stub WebGL canvas.
 *
 * @param {object} [opts]
 * @param {boolean} [opts.withThree=true]   Provide a stub window.THREE.
 * @param {boolean} [opts.withWebGL=true]   Stub a WebGL context on the canvas.
 * @param {string}  [opts.loaderMode]       If set, install THREE.GLTFLoader +
 *   THREE_VRM.VRMLoaderPlugin stubs with this load behaviour.
 * @returns {{ window: Window, canvas: HTMLCanvasElement, THREE: object|null }}
 */
function loadLive3D(opts = {}) {
  const withThree = opts.withThree !== false;
  const withWebGL = opts.withWebGL !== false;
  const THREE = withThree ? makeStubThree() : null;

  const extraGlobals = {};
  if (THREE) {
    // Live3D needs a couple of constructors the base Orb stub omits.
    THREE.DirectionalLight = function DirectionalLight() {
      return { position: { set: () => {} } };
    };
    THREE.AmbientLight = function AmbientLight() {
      return {};
    };
    if (opts.loaderMode) {
      THREE.GLTFLoader = makeStubGLTFLoaderCtor(opts.loaderMode);
    }
    extraGlobals.THREE = THREE;
    if (opts.loaderMode) {
      extraGlobals.THREE_VRM = {
        VRMLoaderPlugin: function VRMLoaderPlugin(parser) {
          return { parser };
        },
        VRMUtils: { deepDispose: () => {} },
      };
    }
  }

  const window = loadFrontend(["live3d-renderer.js"], { extraGlobals });
  const canvas = window.document.getElementById("avatar");
  if (withWebGL) stubCanvasWebGL(canvas);
  return { window, canvas, THREE };
}

test("Live3DRenderer is exposed as a global constructor", () => {
  const { window } = loadLive3D();
  assert.equal(typeof window.Live3DRenderer, "function", "window.Live3DRenderer is a class");
});

test("construct with three.js + WebGL available succeeds and conforms to Renderer_Interface (R2.3)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    assertRendererInterface(assert, r, { label: "Live3DRenderer instance" });
    assert.equal(r.type, "Live3D");
  } finally {
    r.destroy();
  }
});

test("construct renders to a transparent canvas (alpha=0) (R5.3)", () => {
  const { window, canvas, THREE } = loadLive3D();

  const calls = { setClearAlpha: [], alphaRequested: null };
  const origRenderer = THREE.WebGLRenderer;
  THREE.WebGLRenderer = function (params) {
    calls.alphaRequested = params.alpha;
    assert.equal(params.alpha, true, "WebGLRenderer constructed with alpha:true (R5.3)");
    assert.equal(
      params.premultipliedAlpha,
      false,
      "WebGLRenderer constructed with premultipliedAlpha:false (R5.3)"
    );
    const r = origRenderer(params);
    const origAlpha = r.setClearAlpha;
    r.setClearAlpha = (a) => {
      calls.setClearAlpha.push(a);
      return origAlpha && origAlpha(a);
    };
    return r;
  };

  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    assert.equal(calls.alphaRequested, true);
    assert.deepEqual(
      calls.setClearAlpha,
      [0],
      "clear alpha set to 0 for a transparent canvas (R5.3)"
    );
  } finally {
    r.destroy();
  }
});

test("construct FAILS CLEANLY when three.js is missing (no silent no-op) (R9)", () => {
  const { window, canvas } = loadLive3D({ withThree: false });
  assert.equal(typeof window.Live3DRenderer, "function", "class still defined without THREE");
  assert.throws(
    () => new window.Live3DRenderer(canvas, { type: "Live3D" }),
    /three\.js|THREE/i,
    "constructor throws when window.THREE is absent so RendererManager degrades"
  );
});

test("construct FAILS CLEANLY when WebGL is unavailable (no silent no-op) (R9)", () => {
  const { window, canvas } = loadLive3D({ withWebGL: false });
  assert.throws(
    () => new window.Live3DRenderer(canvas, { type: "Live3D" }),
    /WebGL/i,
    "constructor throws when WebGL is unavailable so RendererManager degrades"
  );
});

test("_nextBlinkInterval() always returns ms in [2000,6000] (R5.5, >=100 runs)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    fc.assert(
      // The generator value is unused; each run re-samples the (random) interval.
      fc.property(fc.integer(), () => {
        const ms = r._nextBlinkInterval();
        assert.equal(typeof ms, "number");
        assert.ok(ms >= 2000 && ms <= 6000, `blink interval ${ms} within [2000,6000]`);
      }),
      { numRuns: 200 }
    );
  } finally {
    r.destroy();
  }
});

test("setModel(invalid url) returns failure + shows a visible on-canvas message, never throws (R5.2/2.10)", async () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    let res;
    await assert.doesNotReject(async () => {
      res = await r.setModel("");
    });
    assert.equal(res.ok, false, "invalid url returns a failure result");
    assert.equal(r._vrm, null, "no model installed on failure (no partial/broken model)");
    const errEl = window.document.querySelector(".live3d-error");
    assert.ok(errEl, "a visible failure message element is shown on the canvas");
    assert.ok(errEl.textContent && errEl.textContent.length > 0, "failure message has text");
  } finally {
    r.destroy();
  }
});

test("setModel when VRM loader unavailable returns failure + shows message, never throws (R5.2)", async () => {
  // three.js present (so constructor succeeds) but NO GLTFLoader / three-vrm.
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const res = await r.setModel("https://example.test/avatar.vrm");
    assert.equal(res.ok, false, "loader unavailable returns a failure result");
    assert.match(res.error, /loader unavailable/i);
    assert.equal(r._vrm, null, "no model installed (R5.2)");
    assert.ok(window.document.querySelector(".live3d-error"), "failure message shown");
  } finally {
    r.destroy();
  }
});

test("setModel on a loader error returns failure + shows the reason, never throws (R5.2/2.10)", async () => {
  const { window, canvas } = loadLive3D({ loaderMode: "error" });
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    let res;
    await assert.doesNotReject(async () => {
      res = await r.setModel("https://example.test/missing.vrm");
    });
    assert.equal(res.ok, false, "loader error returns a failure result");
    assert.match(res.error, /network failure|404/i, "failure reason is surfaced");
    assert.equal(r._vrm, null, "no partial/broken model installed (R5.2)");
    const errEl = window.document.querySelector(".live3d-error");
    assert.ok(errEl && /load failed/i.test(errEl.textContent), "on-canvas failure text shown");
  } finally {
    r.destroy();
  }
});

test("setModel on a parsed-but-not-VRM asset fails safely (R5.2)", async () => {
  const { window, canvas } = loadLive3D({ loaderMode: "no-vrm" });
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const res = await r.setModel("https://example.test/not-a.vrm");
    assert.equal(res.ok, false);
    assert.match(res.error, /not a valid VRM/i);
    assert.equal(r._vrm, null, "no model installed on a non-VRM asset");
  } finally {
    r.destroy();
  }
});

test("setModel SUCCESS installs the VRM and clears any prior error", async () => {
  const { window, canvas } = loadLive3D({ loaderMode: "success" });
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    // First a failure to ensure the error overlay is present...
    await r.setModel("");
    assert.ok(window.document.querySelector(".live3d-error"), "error overlay shown after failure");

    const res = await r.setModel("https://example.test/avatar.vrm");
    assert.equal(res.ok, true, "valid VRM load reports success");
    assert.equal(res.url, "https://example.test/avatar.vrm");
    assert.ok(r._vrm, "the VRM is installed");
    assert.equal(r._modelUrl, "https://example.test/avatar.vrm");
    assert.equal(
      window.document.querySelector(".live3d-error"),
      null,
      "the failure overlay is cleared on a successful load"
    );
  } finally {
    r.destroy();
  }
});

test("interface methods are safe / never throw on any input", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    assert.doesNotThrow(() => r.setExpression(3, "joy"));
    assert.doesNotThrow(() => r.setExpression(-1, null));
    assert.doesNotThrow(() => r.setExpression(NaN, undefined));
    assert.doesNotThrow(() => r.playMotion("Idle"));
    assert.doesNotThrow(() => r.playMotion(undefined));
    assert.doesNotThrow(() => r.resetMouth());
  } finally {
    r.destroy();
  }
});

test("setMouthOpen never throws and clamps finite numbers to [0,1]", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.double({ noDefaultInfinity: false, noNaN: false }),
          fc.constantFrom(NaN, Infinity, -Infinity, null, undefined, "x", {}, [])
        ),
        (v) => {
          r.setMouthOpen(v); // must never throw
          assert.ok(r._mouthTarget >= 0 && r._mouthTarget <= 1);
          if (typeof v === "number" && Number.isFinite(v)) {
            assert.equal(r._mouthTarget, Math.max(0, Math.min(1, v)));
          }
        }
      ),
      { numRuns: 100 }
    );
  } finally {
    r.destroy();
  }
});

test("destroy() stops the render loop and is idempotent / never throws", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  assert.doesNotThrow(() => r.destroy());
  assert.doesNotThrow(() => r.destroy(), "second destroy is safe");
  assert.equal(r._raf, null, "no pending animation frame after destroy");
});

// ===========================================================================
// Task 5.2 — mouth blendshape, single-active emotion mapping, post-update write
// ===========================================================================

/**
 * Build a recording VRM whose expressionManager records every setValue([name,v])
 * into `calls`, and whose `update(dt)` pushes a sentinel ["__update__", dt] into
 * the SAME array — so a test can assert that lip-sync mouth writes land AFTER the
 * frame's vrm.update (the documented three-vrm gotcha). Optionally restrict the
 * set of expressions the model "has" via `hasExpressions` (drives getExpression),
 * so Requirement 6.7 (no corresponding expression → keep current) is exercisable.
 *
 * @param {object} [opts]
 * @param {string[]|null} [opts.hasExpressions] If an array, getExpression(name)
 *   is truthy only for those names. If null/omitted, getExpression is NOT defined
 *   (the renderer then assumes availability).
 * @returns {{ vrm: object, calls: Array }}
 */
function makeRecordingVrm(opts = {}) {
  const calls = [];
  const em = {
    setValue: (name, v) => calls.push([name, v]),
  };
  if (Array.isArray(opts.hasExpressions)) {
    const set = new Set(opts.hasExpressions);
    em.getExpression = (name) => (set.has(name) ? { expressionName: name } : null);
  }
  return {
    calls,
    vrm: {
      scene: { rotation: { y: 0 }, position: { y: 0 } },
      update: (dt) => calls.push(["__update__", dt]),
      expressionManager: em,
    },
  };
}

/** Install a recording VRM directly onto a constructed Live3DRenderer. */
function installRecordingVrm(r, opts = {}) {
  const rec = makeRecordingVrm(opts);
  r._installVrm(rec.vrm);
  return rec;
}

test("setMouthOpen drives the VRM mouth blendshape with the clamped value (R6.1/6.2/6.3)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const rec = installRecordingVrm(r);
    rec.calls.length = 0;

    r.setMouthOpen(0.42);
    r._applyMouth();
    assert.deepEqual(
      rec.calls.filter((c) => c[0] !== "__update__" && c[0] !== "blink").pop(),
      ["aa", 0.42],
      "mouth weight written as the clamped openness on the resolved 'aa' preset"
    );

    // Clamp: v > 1 -> 1, v < 0 -> 0.
    r.setMouthOpen(2);
    r._applyMouth();
    assert.equal(rec.calls[rec.calls.length - 1][1], 1, "v>1 clamped to 1");

    r.setMouthOpen(-3);
    r._applyMouth();
    assert.equal(rec.calls[rec.calls.length - 1][1], 0, "v<0 clamped to 0");
  } finally {
    r.destroy();
  }
});

test("non-finite setMouthOpen keeps the current mouth value, never throws (R6.4)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    installRecordingVrm(r);
    r.setMouthOpen(0.6);
    for (const bad of [NaN, Infinity, -Infinity, null, undefined, "x", {}, []]) {
      assert.doesNotThrow(() => r.setMouthOpen(bad));
      assert.equal(r._mouthTarget, 0.6, "non-finite input keeps the prior mouth target");
    }
  } finally {
    r.destroy();
  }
});

test("the lip-sync mouth weight is written AFTER vrm.update(dt) each frame (R6.1 gotcha)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const rec = installRecordingVrm(r);
    r.setMouthOpen(0.75);
    rec.calls.length = 0;

    // Drive one real frame: _frame calls vrm.update(dt) then _applyBlink/_applyMouth.
    r._frame(r._lastNow + 16);

    const updateIdx = rec.calls.findIndex((c) => c[0] === "__update__");
    const mouthIdx = rec.calls.findIndex((c) => c[0] === "aa");
    assert.ok(updateIdx >= 0, "vrm.update was called during the frame");
    assert.ok(mouthIdx >= 0, "mouth weight was written during the frame");
    assert.ok(
      mouthIdx > updateIdx,
      "mouth write occurs AFTER vrm.update so idle/expression animation cannot override it"
    );
    assert.equal(rec.calls[mouthIdx][1], 0.75, "the written mouth weight is the lip-sync target");
  } finally {
    r.destroy();
  }
});

test("resetMouth() drives the mouth blendshape back to 0 (R6.5)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const rec = installRecordingVrm(r);
    r.setMouthOpen(0.9);
    r._applyMouth();
    r.resetMouth();
    assert.equal(r._mouthTarget, 0, "resetMouth zeros the lip-sync target");
    rec.calls.length = 0;
    r._applyMouth();
    assert.deepEqual(
      rec.calls[rec.calls.length - 1],
      ["aa", 0],
      "after resetMouth the mouth blendshape weight is written as 0"
    );
  } finally {
    r.destroy();
  }
});

test("setMouthOpen resolves a mouth preset the model actually has (probe)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    // Model lacks "aa" but has "ou": the renderer must drive "ou".
    const rec = installRecordingVrm(r, { hasExpressions: ["ou", "blink", "happy"] });
    assert.equal(r._mouthExpr, "ou", "probed the available mouth preset");
    rec.calls.length = 0;
    r.setMouthOpen(0.5);
    r._applyMouth();
    assert.deepEqual(rec.calls[rec.calls.length - 1], ["ou", 0.5]);
  } finally {
    r.destroy();
  }
});

test("setExpression maps an emotion name to a VRM preset and applies it (R6.6)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const rec = installRecordingVrm(r, {
      hasExpressions: ["happy", "angry", "sad", "surprised", "neutral", "aa", "blink"],
    });
    rec.calls.length = 0;

    r.setExpression(1, "joy"); // "joy" -> VRM "happy"
    assert.deepEqual(
      rec.calls.filter((c) => c[0] === "happy"),
      [["happy", 1]],
      "the mapped VRM emotion preset is set to weight 1"
    );
    assert.equal(r._activeExprPreset, "happy");
  } finally {
    r.destroy();
  }
});

test("setExpression zeros the previous emotion before applying the new one (single-active, R6.6)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const rec = installRecordingVrm(r, {
      hasExpressions: ["happy", "angry", "sad", "surprised", "neutral"],
    });
    r.setExpression(1, "happy");
    rec.calls.length = 0;

    r.setExpression(2, "angry");
    // The previous preset ("happy") must be zeroed BEFORE the new one is set.
    const zeroIdx = rec.calls.findIndex((c) => c[0] === "happy" && c[1] === 0);
    const setIdx = rec.calls.findIndex((c) => c[0] === "angry" && c[1] === 1);
    assert.ok(zeroIdx >= 0, "previous emotion weight is zeroed");
    assert.ok(setIdx >= 0, "new emotion weight is set to 1");
    assert.ok(zeroIdx < setIdx, "previous emotion is zeroed BEFORE the new one is applied");
    assert.equal(r._activeExprPreset, "angry");

    // Single-active invariant: at most one emotion preset has weight > 0.
    const nonZero = new Map();
    for (const [name, v] of rec.calls) {
      if (["happy", "angry", "sad", "surprised", "neutral", "relaxed"].includes(name)) {
        nonZero.set(name, v);
      }
    }
    const active = [...nonZero.entries()].filter(([, v]) => v > 0).map(([n]) => n);
    assert.deepEqual(active, ["angry"], "exactly one emotion preset is active");
  } finally {
    r.destroy();
  }
});

test("setExpression with an emotion the model lacks keeps the current expression (R6.7)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    // Model has "happy" but NOT "angry".
    const rec = installRecordingVrm(r, { hasExpressions: ["happy", "neutral", "aa"] });
    r.setExpression(1, "happy");
    rec.calls.length = 0;

    assert.doesNotThrow(() => r.setExpression(2, "angry"));
    assert.equal(
      rec.calls.filter((c) => c[0] === "angry").length,
      0,
      "no weight written for an emotion the model does not have"
    );
    assert.equal(r._activeExprPreset, "happy", "the current expression is unchanged (R6.7)");
  } finally {
    r.destroy();
  }
});

test("setExpression with an unknown/unmapped emotion name keeps current + never throws (R6.7)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    const rec = installRecordingVrm(r, {
      hasExpressions: ["happy", "neutral", "aa"],
    });
    r.setExpression(1, "happy");
    rec.calls.length = 0;

    // Unmapped name, missing name, and out-of-range index all keep current.
    assert.doesNotThrow(() => r.setExpression(7, "bogus-emotion"));
    assert.doesNotThrow(() => r.setExpression(3, undefined));
    assert.doesNotThrow(() => r.setExpression(-1, "happy"));
    assert.equal(
      rec.calls.length,
      0,
      "no VRM writes for unmapped/unknown expression requests"
    );
    assert.equal(r._activeExprPreset, "happy", "current expression preserved");
  } finally {
    r.destroy();
  }
});

test("an expression requested before the model loads is applied once the VRM installs (R6.6)", () => {
  const { window, canvas } = loadLive3D();
  const r = new window.Live3DRenderer(canvas, { type: "Live3D" });
  try {
    // No VRM yet: request is buffered (mapped name resolves to a preset).
    r.setExpression(0, "neutral");
    assert.ok(r._pendingExpression, "the expression is buffered before load");

    const rec = installRecordingVrm(r, { hasExpressions: ["neutral", "happy", "aa"] });
    assert.deepEqual(
      rec.calls.filter((c) => c[0] === "neutral"),
      [["neutral", 1]],
      "the buffered expression is applied on VRM install"
    );
    assert.equal(r._activeExprPreset, "neutral");
  } finally {
    r.destroy();
  }
});
