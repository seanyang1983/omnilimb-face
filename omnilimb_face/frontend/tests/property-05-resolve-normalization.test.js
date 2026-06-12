/*
 * property-05-resolve-normalization.test.js — property test for task 2.9.
 *
 * Feature: switchable-avatar-renderers, Property 5: 渲染器选择的归一化
 * Validates: Requirements 1.9, 1.10, 1.6
 *
 * Property 5 (渲染器选择的归一化): For ANY string input `s` (including
 * missing/arbitrary-invalid values), resolveRenderer(s) ALWAYS yields one of
 * {Live2D, Live3D}; when `s` is in that set the result
 * equals `s`; otherwise the result equals Live2D (the default).
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator samples both
 * the legal types and an arbitrary space of strings / non-strings (incl.
 * wrong case, padded, numbers, null/undefined, objects) so the totality and
 * default-to-Live2D invariants are exercised broadly.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");

const VALID_TYPES = ["Live2D", "Live3D"];

function loadManager() {
  const window = loadFrontend(["renderer-manager.js"]);
  return window.RendererManager;
}

test("Property 5: resolveRenderer is total and defaults invalid input to Live2D (>=100 runs)", () => {
  const RendererManager = loadManager();
  fc.assert(
    fc.property(
      fc.oneof(
        fc.constantFrom(...VALID_TYPES), // legal types
        fc.string(), // arbitrary strings (mostly invalid)
        fc.constantFrom("live2d", "LIVE2D", "DigitalHuman", "orb", " Live2D ", ""), // near-misses
        fc.constantFrom(undefined, null, 0, 1, true, false, NaN, {}, []) // non-strings
      ),
      (s) => {
        const out = RendererManager.resolveRenderer(s);
        // Totality: result is always one of the legal types.
        assert.ok(VALID_TYPES.indexOf(out) !== -1, "result is a legal renderer type for input: " + String(s));
        // Identity on legal values; default-to-Live2D otherwise.
        if (VALID_TYPES.indexOf(s) !== -1) {
          assert.equal(out, s, "legal input resolves to itself");
        } else {
          assert.equal(out, "Live2D", "invalid/missing input resolves to Live2D");
        }
      }
    ),
    { numRuns: 100 }
  );
});
