/*
 * property-17-no-active-renderer.test.js — property test for task 2.8.
 *
 * Feature: switchable-avatar-renderers, Property 17: 无激活渲染器时忽略信号
 * Validates: Requirements 2.7
 *
 * Property 17 (无激活渲染器时忽略信号): For ANY Audio_Event / interface-signal
 * sequence, when the RendererManager has NO active renderer (active == null),
 * every signal is ignored — no Renderer_Interface method is invoked on any
 * renderer — and nothing throws.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator builds a
 * random sequence of interface calls (setMouthOpen/setExpression/resetMouth/
 * playMotion/setModel/setAgentState with random arguments). A sentinel
 * recording renderer is created but NEVER adopted, so its `calls` array must
 * stay empty — proving no method was routed while active is null.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeFakeRenderer } = require("./helpers/conformance");

// A signal is a [method, arg] pair the manager would forward to an active renderer.
const signalArb = fc.oneof(
  fc.tuple(fc.constant("setMouthOpen"), fc.oneof(fc.double(), fc.constantFrom(NaN, null, undefined, "x"))),
  fc.tuple(fc.constant("setExpression"), fc.integer()),
  fc.tuple(fc.constant("resetMouth"), fc.constant(undefined)),
  fc.tuple(fc.constant("playMotion"), fc.string()),
  fc.tuple(fc.constant("setModel"), fc.string()),
  fc.tuple(fc.constant("setAgentState"), fc.constantFrom("idle", "listening", "thinking", "speaking", "bogus"))
);

test("Property 17: with no active renderer every signal is a safe no-op; nothing is routed (>=100 runs)", () => {
  fc.assert(
    fc.property(fc.array(signalArb, { minLength: 0, maxLength: 40 }), (signals) => {
      const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
      const mgr = new window.RendererManager({
        canvas: window.document.getElementById("avatar"),
        notify: () => {},
      });

      // A sentinel renderer that is NEVER adopted — proves nothing is routed.
      const sentinel = makeFakeRenderer({ type: "Live3D", withAgentState: true });

      assert.equal(mgr.active, null, "manager starts with no active renderer");

      for (const [method, arg] of signals) {
        assert.doesNotThrow(() => {
          const out = mgr[method](arg);
          if (method === "setModel") {
            assert.equal(out, undefined, "setModel returns undefined with no active renderer");
          }
        }, `${method} must be a safe no-op when active == null`);
      }

      // The un-adopted sentinel never received any forwarded call.
      assert.equal(sentinel.calls.length, 0, "no Renderer_Interface method was routed while active == null");
      assert.equal(mgr.active, null, "active remains null");
    }),
    { numRuns: 100 }
  );
});
