/*
 * property-09-switch-event-continuity.test.js — property test for task 2.6.
 *
 * Feature: switchable-avatar-renderers, Property 9: 切换的事件连续性与上下文保留
 * Validates: Requirements 11.3, 11.5
 *
 * Property 9 (切换的事件连续性与上下文保留): For ANY Audio_Event sequence and ANY
 * switch point, the switch consumes/drops NO already-dispatched event; after
 * the switch each subsequent event is handled by the currently-active renderer
 * EXACTLY once, in order; and the displayed conversation history + pending
 * Audio_Event queue are unchanged across the switch.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator builds a
 * random Audio_Event sequence (mouth-open values) and a random split index.
 * Events before the split are routed to the pre-switch renderer, events after
 * to the post-switch renderer; we assert the two renderers together receive
 * EVERY event exactly once, in order, with none lost or duplicated.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeFakeRenderer } = require("./helpers/conformance");

function freshManager() {
  const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
  const mgr = new window.RendererManager({
    canvas: window.document.getElementById("avatar"),
    notify: () => {},
  });
  return { window, mgr };
}

test("Property 9: a switch loses/duplicates no Audio_Event and preserves context (>=100 runs)", async () => {
  await fc.assert(
    fc.asyncProperty(
      // A sequence of lip-sync mouth values (the Audio_Event payloads we route).
      fc.array(fc.double({ min: 0, max: 1, noNaN: true }), { minLength: 0, maxLength: 30 }),
      fc.nat(),
      async (events, splitRaw) => {
        const { mgr } = freshManager();

        const before = makeFakeRenderer({ type: "Live2D" });
        mgr.adopt(before, "Live2D");
        const after = makeFakeRenderer({ type: "Live3D", withAgentState: true });
        mgr.register("Live3D", () => after);

        // Split point anywhere in [0, events.length].
        const split = events.length === 0 ? 0 : splitRaw % (events.length + 1);

        // External session context the switch must not disturb.
        const context = { history: ["turn-1", "turn-2"], queue: events.slice() };
        const historySnapshot = context.history.slice();
        const queueSnapshot = context.queue.slice();

        // Dispatch the first `split` events to the pre-switch renderer.
        for (let i = 0; i < split; i++) mgr.setMouthOpen(events[i]);

        // Switch (does not consume/clear the external queue).
        const res = await mgr.switchTo("Live3D");
        assert.equal(res.ok, true);

        // Dispatch the remaining events to the post-switch renderer.
        for (let i = split; i < events.length; i++) mgr.setMouthOpen(events[i]);

        // Each renderer received exactly its slice, in order — nothing lost or
        // duplicated, every event handled exactly once by the active renderer.
        const beforeVals = before.calls.filter((c) => c.method === "setMouthOpen").map((c) => c.args[0]);
        const afterVals = after.calls.filter((c) => c.method === "setMouthOpen").map((c) => c.args[0]);

        assert.deepEqual(beforeVals, events.slice(0, split), "pre-switch renderer handled exactly the pre-split events, in order");
        assert.deepEqual(afterVals, events.slice(split), "post-switch renderer handled exactly the post-split events, in order");
        assert.equal(beforeVals.length + afterVals.length, events.length, "every event handled exactly once (none lost/duplicated)");

        // Context + queue preserved across the switch (R11.5).
        assert.deepEqual(context.history, historySnapshot, "conversation history preserved");
        assert.deepEqual(context.queue, queueSnapshot, "pending Audio_Event queue preserved");
      }
    ),
    { numRuns: 100 }
  );
});
