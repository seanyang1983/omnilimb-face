/*
 * property-08-persistence-roundtrip.test.js — property test for task 2.10.
 *
 * Feature: switchable-avatar-renderers, Property 8: 渲染器选择的持久化往返
 * Validates: Requirements 1.7
 *
 * Property 8 (渲染器选择的持久化往返): For ANY legal renderer type `t`, selecting
 * `t` (writing it into SETTINGS and persisting to localStorage) and then
 * reloading SETTINGS from storage yields `SETTINGS.renderer === t`.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator samples the
 * legal types and (to broaden the input space) interleaves unrelated
 * pre-existing settings fields that must survive the round-trip untouched. An
 * in-memory localStorage-like backend stands in for the browser store.
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

/** A minimal in-memory localStorage-like backend. */
function makeStorage() {
  const map = new Map();
  return {
    setItem(k, v) {
      map.set(k, String(v));
    },
    getItem(k) {
      return map.has(k) ? map.get(k) : null;
    },
    removeItem(k) {
      map.delete(k);
    },
  };
}

test("Property 8: persist(t) → reload yields SETTINGS.renderer === t for every legal type (>=100 runs)", () => {
  const RendererManager = loadManager();
  fc.assert(
    fc.property(
      fc.constantFrom(...VALID_TYPES),
      // Arbitrary unrelated settings that must round-trip untouched.
      fc.record({
        showLog: fc.boolean(),
        avatarScale: fc.double({ min: 0.1, max: 4, noNaN: true }),
        bgMode: fc.constantFrom("transparent", "color", "image"),
      }),
      (t, extra) => {
        const storage = makeStorage();
        const settings = Object.assign({ renderer: "Live2D" }, extra);

        const res = RendererManager.persistRenderer(t, { settings, storage, storageKey: "k" });
        assert.equal(res.ok, true);
        assert.equal(res.persisted, true);
        assert.equal(res.type, t);

        // Reload exactly as app.js does: parse the persisted blob, normalize.
        const reloaded = JSON.parse(storage.getItem("k"));
        assert.equal(reloaded.renderer, t, "persisted renderer round-trips to t");
        assert.equal(RendererManager.resolveRenderer(reloaded.renderer), t);
        assert.equal(RendererManager.loadRenderer({ storage, storageKey: "k" }), t);

        // Unrelated fields preserved across the round-trip.
        assert.equal(reloaded.showLog, extra.showLog);
        assert.equal(reloaded.bgMode, extra.bgMode);
      }
    ),
    { numRuns: 100 }
  );
});
