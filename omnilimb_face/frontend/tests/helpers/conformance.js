/*
 * conformance.js — SHARED renderer-conformance scaffold + reusable fakes.
 *
 * Every renderer in this feature (the existing CanvasAvatar / Live2DAvatar and
 * the Live3DRenderer) implements
 * the same implicit Renderer_Interface duck-typing contract documented in
 * renderer-manager.js. This module is the single place that:
 *
 *   1. Names the interface surface (the six required methods + the optional
 *      setAgentState) so every per-renderer test agrees on the contract.
 *   2. Provides assertions that check a renderer (class OR instance) exposes
 *      those methods as functions — the minimum bar for Property 7 / task 1.3.
 *   3. Ships reusable FAKES (a recording fake renderer + WebGL/three/PIXI stubs)
 *      so later per-renderer property tests can construct renderers that depend
 *      on WebGL/three WITHOUT a real GPU, and can drive a renderer with random
 *      inputs while recording the resulting calls.
 *
 * It is intentionally framework-light: the assertion helpers take a Node
 * `assert` object so they compose with the built-in `node:test` runner (or any
 * runner) without coupling to one.
 */

"use strict";

// The six methods every renderer MUST implement (Renderer_Interface).
// (Requirement 2.1; conformance asserted for 2.2/2.3/2.4/2.5.)
const REQUIRED_METHODS = Object.freeze([
  "setMouthOpen",
  "setExpression",
  "resetMouth",
  "setModel",
  "playMotion",
  "destroy",
]);

// Optional method — a renderer may visualise Agent_State; others may omit it
// and the RendererManager guards calls with `typeof === "function"`.
// (Requirement 2.5/2.11.)
const OPTIONAL_METHODS = Object.freeze(["setAgentState"]);

// The two legal renderer type labels (Requirement 1.1). Mirrors
// RendererManager.RENDERER_TYPES; duplicated here so the scaffold is usable
// without loading the front-end.
const RENDERER_TYPES = Object.freeze([
  "Live2D",
  "Live3D",
]);

/**
 * Resolve the object whose own/inherited methods describe a renderer's surface.
 * Accepts either a constructor/class (inspect its prototype) or an instance.
 * @param {Function|object} target A renderer class or instance.
 * @returns {object} The object to probe for methods.
 */
function methodSurface(target) {
  if (typeof target === "function") return target.prototype || target;
  return target;
}

/**
 * Pure check: does `target` expose the renderer interface?
 * @param {Function|object} target Renderer class or instance.
 * @param {object} [opts]
 * @param {boolean} [opts.requireSetAgentState=false] Require the optional
 *   setAgentState too.
 * @returns {{ ok: boolean, missing: string[], present: string[] }}
 */
function checkRendererInterface(target, opts = {}) {
  const surface = methodSurface(target);
  const requireSetAgentState = !!opts.requireSetAgentState;
  const required = requireSetAgentState
    ? REQUIRED_METHODS.concat(OPTIONAL_METHODS)
    : REQUIRED_METHODS;

  const missing = [];
  const present = [];
  for (const name of required) {
    if (surface && typeof surface[name] === "function") present.push(name);
    else missing.push(name);
  }
  return { ok: missing.length === 0, missing, present };
}

/**
 * Assert that `target` conforms to the renderer interface, using a Node-style
 * `assert` object. Throws (via assert) with a clear message on the first gap.
 *
 * @param {object} assert A Node `assert` (or compatible) module/object.
 * @param {Function|object} target Renderer class or instance.
 * @param {object} [opts]
 * @param {string} [opts.label="renderer"] Friendly name for assertion messages.
 * @param {boolean} [opts.requireSetAgentState=false] Also require setAgentState.
 */
function assertRendererInterface(assert, target, opts = {}) {
  const label = opts.label || "renderer";
  const { ok, missing, present } = checkRendererInterface(target, opts);
  assert.ok(
    ok,
    `${label} must implement Renderer_Interface; missing method(s): ` +
      `[${missing.join(", ")}] (present: [${present.join(", ")}])`
  );
  // Re-affirm each required method is a function for granular failure output.
  const surface = methodSurface(target);
  for (const name of present) {
    assert.strictEqual(
      typeof surface[name],
      "function",
      `${label}.${name} must be a function`
    );
  }
}

// ---------------------------------------------------------------------------
// Reusable fakes
// ---------------------------------------------------------------------------

/**
 * A fully-conformant recording fake renderer. Implements every required method
 * (and optionally setAgentState) as a SAFE no-op that records its calls, so
 * later property tests can:
 *   - feed it random inputs and assert no method throws,
 *   - inspect `calls` to assert routing/ordering invariants,
 *   - inject construction/ready failures via the options below.
 *
 * Safe-by-contract: no method throws on any input. setMouthOpen clamps finite
 * numbers to [0,1] and ignores non-numbers (keeping the last value), mirroring
 * the documented Renderer_Interface semantics so the fake is a faithful stand-in.
 */
class RecordingFakeRenderer {
  /**
   * @param {object} [opts]
   * @param {boolean} [opts.withAgentState=true] Implement the optional
   *   setAgentState method (set false to simulate a renderer that omits it).
   * @param {string}  [opts.type="Live2D"] Type label for routing assertions.
   * @param {boolean} [opts.failSetModel=false] Make setModel report failure.
   * @param {boolean} [opts.throwOnConstruct=false] Throw from the constructor
   *   (callers pass this through a factory to simulate init failure).
   */
  constructor(opts = {}) {
    if (opts.throwOnConstruct) {
      throw new Error("RecordingFakeRenderer: simulated construction failure");
    }
    this.type = opts.type || "Live2D";
    this._withAgentState = opts.withAgentState !== false;
    this._failSetModel = !!opts.failSetModel;
    this.calls = [];
    this.mouthOpen = 0; // last applied target, after clamp / ignore rules
    this.expressionIndex = null;
    this.agentState = null;
    this.destroyed = false;

    if (!this._withAgentState) {
      // Remove the optional method so `typeof === "function"` guards see it gone.
      this.setAgentState = undefined;
    }
  }

  _record(method, args) {
    this.calls.push({ method, args });
  }

  setMouthOpen(v) {
    this._record("setMouthOpen", [v]);
    if (typeof v === "number" && Number.isFinite(v)) {
      this.mouthOpen = Math.max(0, Math.min(1, v));
    }
    // non-number / NaN / Infinity: keep current value (no throw).
  }

  setExpression(index, name) {
    this._record("setExpression", [index, name]);
    if (typeof index === "number" && Number.isInteger(index) && index >= 0) {
      this.expressionIndex = index;
    }
  }

  resetMouth() {
    this._record("resetMouth", []);
    this.mouthOpen = 0;
  }

  setModel(url) {
    this._record("setModel", [url]);
    if (this._failSetModel || !url || typeof url !== "string") {
      return { ok: false, error: "fake setModel failure" };
    }
    return { ok: true, url };
  }

  playMotion(group) {
    this._record("playMotion", [group]);
  }

  destroy() {
    this._record("destroy", []);
    this.destroyed = true;
  }

  setAgentState(state) {
    this._record("setAgentState", [state]);
    if (RENDERER_TYPES && ["idle", "listening", "thinking", "speaking"].includes(state)) {
      this.agentState = state;
    }
  }
}

/**
 * Factory for a recording fake renderer (convenience wrapper around the class).
 * @param {object} [opts] See RecordingFakeRenderer.
 * @returns {RecordingFakeRenderer}
 */
function makeFakeRenderer(opts = {}) {
  return new RecordingFakeRenderer(opts);
}

/**
 * Minimal stub of a WebGL rendering context — enough for renderers to probe
 * "is WebGL available?" without a real GPU. Returned by makeStubCanvas's
 * getContext for "webgl"/"webgl2"/"experimental-webgl".
 * @returns {object}
 */
function makeStubWebGLContext() {
  return {
    // A tiny, inert surface; real renderers only check for truthiness/avail.
    getParameter: () => 0,
    getExtension: () => null,
    createShader: () => ({}),
    createProgram: () => ({}),
    viewport: () => {},
    clearColor: () => {},
    clear: () => {},
    enable: () => {},
    disable: () => {},
    isStub: true,
  };
}

/**
 * Wrap a jsdom <canvas> so its getContext returns a stub WebGL context for
 * WebGL types while delegating "2d" to jsdom. Lets three/Orb-style renderers
 * construct against a fake WebGL surface in tests.
 * @param {HTMLCanvasElement} canvas A jsdom canvas element.
 * @returns {HTMLCanvasElement} The same canvas with a patched getContext.
 */
function stubCanvasWebGL(canvas) {
  const original = canvas.getContext.bind(canvas);
  const gl = makeStubWebGLContext();
  canvas.getContext = (type, ...rest) => {
    if (type === "webgl" || type === "webgl2" || type === "experimental-webgl") {
      return gl;
    }
    try {
      return original(type, ...rest);
    } catch (_e) {
      return null;
    }
  };
  return canvas;
}

/**
 * Minimal THREE stub for constructing Orb/Live3D-style renderers without the
 * real three.js library or a GPU. Only the handful of symbols those renderers
 * touch are stubbed; extend as later tasks need more. Marked `isStub` so tests
 * can assert they ran against the stub.
 * @returns {object}
 */
function makeStubThree() {
  class StubVec3 {
    constructor(x = 0, y = 0, z = 0) {
      this.x = x;
      this.y = y;
      this.z = z;
    }
    set(x, y, z) {
      this.x = x;
      this.y = y;
      this.z = z;
      return this;
    }
  }
  const noop = function () {};
  function ctor() {
    return {};
  }
  return {
    isStub: true,
    Scene: function Scene() {
      return { add: noop, remove: noop };
    },
    PerspectiveCamera: function PerspectiveCamera() {
      return { position: new StubVec3(), lookAt: noop, updateProjectionMatrix: noop };
    },
    WebGLRenderer: function WebGLRenderer() {
      return {
        domElement: { style: {} },
        setSize: noop,
        setClearAlpha: noop,
        setPixelRatio: noop,
        render: noop,
        dispose: noop,
      };
    },
    Points: ctor,
    BufferGeometry: function BufferGeometry() {
      return { setAttribute: noop, dispose: noop };
    },
    BufferAttribute: ctor,
    Float32BufferAttribute: ctor,
    PointsMaterial: function PointsMaterial() {
      return { dispose: noop };
    },
    Color: ctor,
    Vector3: StubVec3,
    Clock: function Clock() {
      return { getDelta: () => 0.016, getElapsedTime: () => 0 };
    },
    AdditiveBlending: 2,
  };
}

module.exports = {
  REQUIRED_METHODS,
  OPTIONAL_METHODS,
  RENDERER_TYPES,
  methodSurface,
  checkRendererInterface,
  assertRendererInterface,
  RecordingFakeRenderer,
  makeFakeRenderer,
  makeStubWebGLContext,
  stubCanvasWebGL,
  makeStubThree,
};
