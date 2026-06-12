/*
 * property-02-init-failure-rollback.test.js — property test for task 2.5.
 *
 * Feature: switchable-avatar-renderers, Property 2: 初始化失败回退切换前渲染器并保留上下文
 * Validates: Requirements 1.5, 11.6, 11.4
 *
 * Property 2 (初始化失败回退保留上下文): For ANY pre-switch renderer `from` and
 * ANY target `to` that fails during construction (throws) OR fails readiness
 * (never ready within 3000ms / rejects): after switchTo settles,
 *   - `manager.active` is restored to the SAME pre-switch instance;
 *   - the displayed conversation history and the pending Audio_Event queue are
 *     unchanged (the manager never touches an external context/queue);
 *   - exactly ONE "switch-failed" notice is surfaced.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator samples the
 * (from, to) types and the failure MODE (construct-throw, ready-reject,
 * ready-timeout). A short readiness timeout keeps timeout cases fast. The
 * pre-switch renderer must never be destroyed so it can be restored intact.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeFakeRenderer, RENDERER_TYPES } = require("./helpers/conformance");

function freshManager(readyTimeoutMs) {
  const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
  const notices = [];
  const mgr = new window.RendererManager({
    canvas: window.document.getElementById("avatar"),
    notify: (msg, meta) => notices.push({ msg, meta }),
    readyTimeoutMs: readyTimeoutMs,
  });
  return { window, mgr, notices };
}

test("Property 2: a failing target rolls back to the SAME pre-switch renderer + preserves context (>=100 runs)", async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom(...RENDERER_TYPES),
      // Pick a target distinct from `from` (a same-type switch never rebuilds).
      fc.constantFrom(...RENDERER_TYPES),
      fc.constantFrom("construct-throw", "ready-reject", "ready-timeout"),
      async (from, toRaw, mode) => {
        // Ensure to !== from so a real switch is attempted.
        const to = toRaw === from ? RENDERER_TYPES[(RENDERER_TYPES.indexOf(from) + 1) % RENDERER_TYPES.length] : toRaw;

        const { mgr, notices } = freshManager(30);

        const fromInstance = makeFakeRenderer({ type: from, withAgentState: from === "Live3D" });
        mgr.adopt(fromInstance, from);

        // An external "session context" the manager must never mutate.
        const context = { history: ["u: hi", "a: hello"], queue: [0.1, 0.2, 0.3] };
        const historySnapshot = context.history.slice();
        const queueSnapshot = context.queue.slice();

        mgr.register(to, () => {
          if (mode === "construct-throw") {
            throw new Error("simulated construction failure for " + to);
          }
          const bad = makeFakeRenderer({ type: to, withAgentState: to === "Live3D" });
          if (mode === "ready-reject") {
            bad.whenReady = () => Promise.reject(new Error("init blew up"));
          } else {
            // ready-timeout: a readiness promise that never settles.
            bad.whenReady = () => new Promise(() => {});
          }
          return bad;
        });

        const result = await mgr.switchTo(to);

        // Switch reported failure and did not switch.
        assert.equal(result.ok, false, "switch reports failure");
        assert.equal(result.switched, false);

        // Rolled back to the SAME pre-switch instance and type (R1.5/11.6).
        assert.equal(mgr.active, fromInstance, "active restored to the same pre-switch instance");
        assert.equal(mgr.type, from, "pre-switch type preserved");
        assert.equal(fromInstance.destroyed, false, "pre-switch renderer never destroyed");

        // Context/queue untouched by the manager (R11.5/11.6).
        assert.deepEqual(context.history, historySnapshot, "conversation history unchanged");
        assert.deepEqual(context.queue, queueSnapshot, "pending Audio_Event queue unchanged");

        // Exactly one switch-failed notice.
        const failed = notices.filter((n) => n.meta && n.meta.kind === "switch-failed");
        assert.equal(failed.length, 1, "exactly one switch-failed notice surfaced");
      }
    ),
    { numRuns: 100 }
  );
});
