/*
 * renderer-settings.test.js — unit tests for task 2.3.
 *
 * Covers the `resolveRenderer` normalization plus the SETTINGS persistence /
 * startup-recovery helpers added to RendererManager:
 *   - resolveRenderer: valid → itself; missing/invalid/non-string → "Live2D"
 *     (Requirement 1.6, 1.9, 1.10);
 *   - persistRenderer: writes the (normalized) selection into SETTINGS and the
 *     storage backend; round-trips back through loadRenderer (Requirement 1.7);
 *   - persistRenderer setItem failure: keeps the selection active in memory and
 *     surfaces exactly one "设置保存失败" notice without throwing (Requirement 1.8);
 *   - loadRenderer: startup recovery from SETTINGS / storage, normalized to a
 *     valid type or Live2D (Requirement 1.9, 1.10).
 *
 * These complement the optional property tests P5/P8 (tasks 2.9/2.10). They use
 * the build-free jsdom loader so no real browser/localStorage is required.
 *
 * Requirements: 1.1, 1.6, 1.7, 1.8, 1.9, 1.10
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { loadFrontend } = require("./helpers/load-frontend");

const VALID_TYPES = ["Live2D", "Live3D"];

/** Load just the RendererManager (its static helpers need no DOM/renderers). */
function loadManager() {
  const window = loadFrontend(["renderer-manager.js"]);
  return window.RendererManager;
}

/**
 * A minimal in-memory localStorage-like backend. `failOnSet` makes setItem
 * throw (e.g. QuotaExceededError / disabled storage) to exercise R1.8.
 */
function makeStorage(opts = {}) {
  const map = new Map();
  return {
    failOnSet: !!opts.failOnSet,
    setItem(key, value) {
      if (this.failOnSet) throw new Error("QuotaExceededError");
      map.set(key, String(value));
    },
    getItem(key) {
      return map.has(key) ? map.get(key) : null;
    },
    removeItem(key) {
      map.delete(key);
    },
    _dump() {
      return map;
    },
  };
}

// --- resolveRenderer normalization (R1.6/1.9/1.10) ----------------------

test("resolveRenderer: each valid type resolves to itself (R1.9)", () => {
  const RendererManager = loadManager();
  for (const t of VALID_TYPES) {
    assert.equal(RendererManager.resolveRenderer(t), t);
  }
});

test("resolveRenderer: missing / invalid / non-string inputs resolve to Live2D (R1.6/1.10)", () => {
  const RendererManager = loadManager();
  const invalids = [
    undefined,
    null,
    "",
    "live2d", // wrong case
    "LIVE2D",
    "DigitalHuman", // missing underscore
    "orb",
    "Hologram",
    " Live2D ", // padded
    0,
    1,
    true,
    false,
    {},
    [],
    NaN,
  ];
  for (const v of invalids) {
    assert.equal(
      RendererManager.resolveRenderer(v),
      "Live2D",
      "invalid input must normalize to Live2D: " + String(v)
    );
  }
});

test("resolveRenderer: result is ALWAYS one of the valid types", () => {
  const RendererManager = loadManager();
  const samples = [...VALID_TYPES, undefined, null, "", "x", 42, {}, "Orbit"];
  for (const v of samples) {
    assert.ok(
      VALID_TYPES.indexOf(RendererManager.resolveRenderer(v)) !== -1,
      "result must be a valid renderer type for input: " + String(v)
    );
  }
});

// --- persistRenderer round-trip (R1.7) ----------------------------------

test("persistRenderer: writes the selection into SETTINGS and persists it (R1.7)", () => {
  const RendererManager = loadManager();
  const storage = makeStorage();
  const settings = { renderer: "Live2D", showLog: true };

  const res = RendererManager.persistRenderer("Live3D", {
    settings,
    storage,
    storageKey: "k",
  });

  assert.equal(res.ok, true);
  assert.equal(res.persisted, true);
  assert.equal(res.type, "Live3D");
  assert.equal(settings.renderer, "Live3D", "SETTINGS.renderer updated in memory");

  // Persisted JSON contains the selection and preserves unrelated fields.
  const persisted = JSON.parse(storage.getItem("k"));
  assert.equal(persisted.renderer, "Live3D");
  assert.equal(persisted.showLog, true, "unrelated settings preserved");
});

test("persistRenderer: normalizes an invalid selection to Live2D before persisting", () => {
  const RendererManager = loadManager();
  const storage = makeStorage();
  const settings = { renderer: "Live3D" };

  const res = RendererManager.persistRenderer("bogus", {
    settings,
    storage,
    storageKey: "k",
  });

  assert.equal(res.type, "Live2D");
  assert.equal(settings.renderer, "Live2D");
  assert.equal(JSON.parse(storage.getItem("k")).renderer, "Live2D");
});

test("persist round-trip: every valid type survives persist → reload (R1.7)", () => {
  const RendererManager = loadManager();
  for (const t of VALID_TYPES) {
    const storage = makeStorage();
    const settings = Object.assign({}, { renderer: "Live2D" });

    RendererManager.persistRenderer(t, { settings, storage, storageKey: "k" });

    // Reload the way app.js does: parse the stored blob, then resolve.
    const reloaded = JSON.parse(storage.getItem("k"));
    assert.equal(reloaded.renderer, t);
    assert.equal(RendererManager.resolveRenderer(reloaded.renderer), t);
    // And loadRenderer reads it straight from storage to the same value.
    assert.equal(
      RendererManager.loadRenderer({ storage, storageKey: "k" }),
      t
    );
  }
});

// --- persistRenderer setItem failure (R1.8) -----------------------------

test("persistRenderer: setItem failure keeps the selection active + emits one 设置保存失败 notice (R1.8)", () => {
  const RendererManager = loadManager();
  const storage = makeStorage({ failOnSet: true });
  const settings = { renderer: "Live2D" };
  const notices = [];

  let res;
  assert.doesNotThrow(() => {
    res = RendererManager.persistRenderer("Live3D", {
      settings,
      storage,
      storageKey: "k",
      notify: (msg, meta) => notices.push({ msg, meta }),
    });
  }, "persistRenderer must never throw on storage failure");

  assert.equal(res.ok, false);
  assert.equal(res.persisted, false);
  // Duck-type the error: persistRenderer runs in the jsdom vm realm, so its
  // `new Error(...)` is a different constructor than this Node realm's `Error`
  // (cross-realm `instanceof Error` is always false). Assert the surfaced
  // failure carries a message instead.
  assert.ok(res.error && typeof res.error.message === "string");
  // R1.8: the selected renderer stays active (kept in memory) despite the
  // failed persistence.
  assert.equal(res.type, "Live3D");
  assert.equal(settings.renderer, "Live3D", "selection kept active in memory");
  // Exactly one user-visible "设置保存失败" notice.
  assert.equal(notices.length, 1);
  assert.ok(notices[0].msg.indexOf("设置保存失败") !== -1, "notice mentions save failure");
  assert.equal(notices[0].meta.kind, "settings-save-failed");
});

test("persistRenderer: missing/invalid storage backend fails safely with a notice (R1.8)", () => {
  const RendererManager = loadManager();
  const settings = { renderer: "Live2D" };
  const notices = [];

  const res = RendererManager.persistRenderer("Live3D", {
    settings,
    storage: null,
    notify: (msg, meta) => notices.push({ msg, meta }),
  });

  assert.equal(res.ok, false);
  assert.equal(settings.renderer, "Live3D", "selection still kept active in memory");
  assert.equal(notices.length, 1);
  assert.equal(notices[0].meta.kind, "settings-save-failed");
});

// --- loadRenderer startup recovery (R1.9/1.10) --------------------------

test("loadRenderer: prefers SETTINGS.renderer and normalizes it (R1.9/1.10)", () => {
  const RendererManager = loadManager();
  assert.equal(RendererManager.loadRenderer({ settings: { renderer: "Live3D" } }), "Live3D");
  assert.equal(RendererManager.loadRenderer({ settings: { renderer: "bogus" } }), "Live2D");
  assert.equal(RendererManager.loadRenderer({ settings: {} }), "Live2D");
});

test("loadRenderer: reads + normalizes from storage when no settings provided (R1.9/1.10)", () => {
  const RendererManager = loadManager();
  const storage = makeStorage();
  storage.setItem("k", JSON.stringify({ renderer: "Live3D" }));
  assert.equal(RendererManager.loadRenderer({ storage, storageKey: "k" }), "Live3D");

  // Invalid persisted value → Live2D.
  storage.setItem("k", JSON.stringify({ renderer: "nope" }));
  assert.equal(RendererManager.loadRenderer({ storage, storageKey: "k" }), "Live2D");

  // Missing key → Live2D.
  assert.equal(RendererManager.loadRenderer({ storage, storageKey: "absent" }), "Live2D");
});

test("loadRenderer: malformed stored JSON falls back to Live2D without throwing (R1.10)", () => {
  const RendererManager = loadManager();
  const storage = makeStorage();
  storage.setItem("k", "{not valid json");
  let out;
  assert.doesNotThrow(() => {
    out = RendererManager.loadRenderer({ storage, storageKey: "k" });
  });
  assert.equal(out, "Live2D");
});
