/*
 * property-01-switch-structural-invariant.test.js — property test for task 2.4.
 *
 * Feature: switchable-avatar-renderers, Property 1: 渲染器切换的结构不变量
 * Validates: Requirements 1.2, 1.3, 1.4, 11.1
 *
 * Property 1 (渲染器切换的结构不变量): For ANY currently-active renderer type
 * `from` and target type `to` —
 *   - when `to !== from`: switchTo(to) calls the OLD instance's destroy()
 *     exactly once, constructs a NEW instance that takes over the same canvas
 *     container, and `manager.active.type === to`;
 *   - when `to === from`: destroy() is NOT called and the instance is NOT
 *     replaced (`manager.active` reference is unchanged).
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator samples every
 * (from, to) pair across the legal renderer types. Each renderer is the
 * shared conforming recording fake, so the structural invariant is checked
 * without a real GPU. Routing/continuity (P9) and failure rollback (P2) are
 * separate properties; here we pin the structural switch contract only.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeFakeRenderer, RENDERER_TYPES } = require("./helpers/conformance");

/** Build a fresh RendererManager with a notify sink for each property case. */
function freshManager() {
  const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
  const notices = [];
  const mgr = new window.RendererManager({
    canvas: window.document.getElementById("avatar"),
    notify: (msg, meta) => notices.push({ msg, meta }),
  });
  return { window, mgr, notices };
}

test("Property 1: switchTo preserves the structural invariant for every (from,to) (>=100 runs)", async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom(...RENDERER_TYPES),
      fc.constantFrom(...RENDERER_TYPES),
      async (from, to) => {
        const { mgr } = freshManager();

        // Adopt the pre-switch (from) renderer instance.
        const fromInstance = makeFakeRenderer({ type: from, withAgentState: from === "Live3D" });
        mgr.adopt(fromInstance, from);

        // Register a factory for the target type that yields a fresh instance.
        let constructed = 0;
        let lastBuilt = null;
        mgr.register(to, () => {
          constructed += 1;
          lastBuilt = makeFakeRenderer({ type: to, withAgentState: to === "Live3D" });
          return lastBuilt;
        });

        const result = await mgr.switchTo(to);

        if (to === from) {
          // Same-type: no rebuild, no destroy, same instance reference (R1.3).
          assert.equal(result.switched, false, "same-type switch must not rebuild");
          assert.equal(mgr.active, fromInstance, "active is the SAME instance");
          assert.equal(mgr.type, from, "type unchanged");
          assert.equal(constructed, 0, "factory not invoked for same-type switch");
          assert.equal(fromInstance.destroyed, false, "old instance not destroyed");
          assert.equal(
            fromInstance.calls.filter((c) => c.method === "destroy").length,
            0,
            "destroy() never called on a same-type switch"
          );
        } else {
          // Different type: exactly one destroy on old, new instance adopted,
          // type updated to `to` (R1.2, R1.4, R11.1).
          assert.equal(result.ok, true);
          assert.equal(result.switched, true, "different-type switch rebuilds");
          assert.equal(constructed, 1, "exactly one new instance constructed");
          assert.equal(mgr.active, lastBuilt, "the NEW instance is active");
          assert.notEqual(mgr.active, fromInstance, "active replaced");
          assert.equal(mgr.active.type, to, "manager.active.type === to");
          assert.equal(mgr.type, to, "manager.type === to");
          assert.equal(fromInstance.destroyed, true, "old instance destroyed (R1.4)");
          assert.equal(
            fromInstance.calls.filter((c) => c.method === "destroy").length,
            1,
            "old instance destroy() called EXACTLY once"
          );
        }
      }
    ),
    { numRuns: 100 }
  );
});
