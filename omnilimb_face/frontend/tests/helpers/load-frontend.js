/*
 * load-frontend.js — clean, build-free loader for the plain-JS front-end.
 *
 * The front-end ships WITHOUT a build step. Each script (avatar.js,
 * renderer-manager.js, ...) is an IIFE of the shape:
 *
 *     (function (global) { ...; global.Foo = Foo; })(window);
 *
 * i.e. it reads the bare `window` global and attaches its exports onto it.
 * To exercise those exports from tests we must run the UNMODIFIED source in a
 * context where `window` (and the few browser globals the code touches —
 * `performance`, `requestAnimationFrame`, `document`, ...) resolve correctly.
 *
 * Approach (no source changes, no bundler):
 *   1. Spin up a jsdom window (with `pretendToBeVisual: true` so it provides
 *      `requestAnimationFrame` / `cancelAnimationFrame`).
 *   2. Make the jsdom window its own `window` self-reference and turn it into a
 *      vm context, so bare identifiers like `window` / `performance` /
 *      `requestAnimationFrame` resolve to the jsdom window's properties.
 *   3. Evaluate each requested front-end file IN THAT CONTEXT with Node's `vm`.
 *
 * The result is the populated jsdom `window` object, from which tests read the
 * attached classes (e.g. `window.CanvasAvatar`, `window.RendererManager`).
 *
 * This keeps the whole test setup self-contained under frontend/tests/ and does
 * not change how the plugin serves the plain JS at runtime.
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");

// Absolute path to the front-end directory (the parent of frontend/tests/).
const FRONTEND_DIR = path.resolve(__dirname, "..", "..");

/**
 * Resolve a front-end source file name (e.g. "avatar.js") to its absolute path.
 * @param {string} file File name relative to the front-end directory.
 * @returns {string} Absolute path.
 */
function frontendPath(file) {
  return path.join(FRONTEND_DIR, file);
}

/**
 * Create a fresh jsdom window suitable for loading front-end IIFE scripts, and
 * evaluate the given files into it (in order). Each call returns an ISOLATED
 * window so tests do not leak global state between cases.
 *
 * @param {string[]} [files] Front-end source files to evaluate, relative to the
 *   front-end directory (e.g. ["renderer-manager.js", "avatar.js"]). Order
 *   matters when one file depends on another's globals.
 * @param {object} [options]
 * @param {object} [options.extraGlobals] Extra properties to assign onto the
 *   window BEFORE evaluating the files (e.g. a stubbed `PIXI` or `THREE`). Used
 *   by fakes that stub WebGL/three.
 * @param {string} [options.html] Initial HTML document body. Defaults to a page
 *   containing a single <canvas id="avatar">.
 * @returns {Window} The populated jsdom window.
 */
function loadFrontend(files = [], options = {}) {
  const { extraGlobals = {}, html } = options;

  const dom = new JSDOM(
    html ||
      '<!DOCTYPE html><html><body><canvas id="avatar" width="320" height="480"></canvas></body></html>',
    { pretendToBeVisual: true, runScripts: "outside-only" }
  );
  const { window } = dom;

  // jsdom provides window.performance; guard for older builds just in case.
  if (!window.performance || typeof window.performance.now !== "function") {
    let t0 = Date.now();
    window.performance = { now: () => Date.now() - t0 };
  }

  // jsdom does not implement HTMLCanvasElement.getContext without the native
  // `canvas` package. Provide a no-op 2D context stub so canvas-based renderers
  // (CanvasAvatar) construct and run cleanly in tests; "webgl" returns null by
  // default (use stubCanvasWebGL from conformance.js to opt into a WebGL stub).
  const ctx2d = make2dContextStub();
  const HTMLCanvasElement = window.HTMLCanvasElement;
  if (HTMLCanvasElement && HTMLCanvasElement.prototype) {
    HTMLCanvasElement.prototype.getContext = function getContext(type) {
      return type === "2d" ? ctx2d : null;
    };
  }

  // Assign any caller-provided stubs (PIXI / THREE / etc.) onto the window
  // before evaluating, so the IIFEs (which receive `window` as their `global`)
  // can see them.
  for (const [key, value] of Object.entries(extraGlobals)) {
    window[key] = value;
  }

  // We evaluate the IIFE scripts in a vm context whose GLOBAL OBJECT is a plain
  // sandbox (not the jsdom window itself). jsdom defines `window`, `document`,
  // etc. via prototype accessors, which V8 does NOT treat as own global
  // properties for bare-identifier resolution — evaluating directly against the
  // window therefore throws "window is not defined". Instead we expose the
  // window (and the handful of browser globals the front-end code references as
  // bare identifiers) as OWN properties of the sandbox. The scripts attach
  // their exports onto `global` === `window`, so reads from the returned window
  // see them; and class closures resolve `performance` / `requestAnimationFrame`
  // against this sandbox at call time.
  const raf = window.requestAnimationFrame
    ? window.requestAnimationFrame.bind(window)
    : (cb) => setTimeout(() => cb(Date.now()), 16);
  const caf = window.cancelAnimationFrame
    ? window.cancelAnimationFrame.bind(window)
    : (id) => clearTimeout(id);

  const sandbox = {
    window,
    document: window.document,
    performance: window.performance,
    requestAnimationFrame: raf,
    cancelAnimationFrame: caf,
    setTimeout: window.setTimeout ? window.setTimeout.bind(window) : setTimeout,
    clearTimeout: window.clearTimeout ? window.clearTimeout.bind(window) : clearTimeout,
    setInterval: window.setInterval ? window.setInterval.bind(window) : setInterval,
    clearInterval: window.clearInterval ? window.clearInterval.bind(window) : clearInterval,
    console,
  };
  sandbox.globalThis = sandbox;
  sandbox.self = window;

  const context = vm.createContext(sandbox);

  for (const file of files) {
    const abs = frontendPath(file);
    const code = fs.readFileSync(abs, "utf8");
    vm.runInContext(code, context, { filename: abs });
  }

  return window;
}

/**
 * Build a no-op CanvasRenderingContext2D-like stub. Every drawing method is a
 * no-op and every getter returns a benign value, so canvas-based renderers can
 * run their draw loops in jsdom (which has no real 2D backend) without throwing.
 * @returns {object}
 */
function make2dContextStub() {
  const noop = function () {};
  const ctx = {
    canvas: null,
    fillStyle: "#000",
    strokeStyle: "#000",
    lineWidth: 1,
    font: "10px sans-serif",
    textAlign: "start",
    textBaseline: "alphabetic",
    globalAlpha: 1,
    save: noop,
    restore: noop,
    beginPath: noop,
    closePath: noop,
    moveTo: noop,
    lineTo: noop,
    quadraticCurveTo: noop,
    bezierCurveTo: noop,
    arc: noop,
    ellipse: noop,
    rect: noop,
    fill: noop,
    stroke: noop,
    clip: noop,
    fillRect: noop,
    strokeRect: noop,
    clearRect: noop,
    fillText: noop,
    strokeText: noop,
    measureText: () => ({ width: 0 }),
    translate: noop,
    rotate: noop,
    scale: noop,
    setTransform: noop,
    resetTransform: noop,
    drawImage: noop,
    createLinearGradient: () => ({ addColorStop: noop }),
    createRadialGradient: () => ({ addColorStop: noop }),
    getImageData: () => ({ data: new Uint8ClampedArray(4) }),
    putImageData: noop,
    isStub: true,
  };
  return ctx;
}

module.exports = { loadFrontend, frontendPath, FRONTEND_DIR };