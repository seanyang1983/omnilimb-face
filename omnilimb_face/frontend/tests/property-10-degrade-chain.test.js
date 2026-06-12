/*
 * property-10-degrade-chain.test.js — property test for task 2.7.
 *
 * Feature: switchable-avatar-renderers, Property 10: 降级链的有序兜底
 * Validates: Requirements 9.1, 9.5, 9.6
 *
 * Property 10 (降级链的有序兜底): For ANY combination of success/failure across
 * the chain levels (Live2D → CanvasAvatar → 纯文本/语音), the final active mode
 * equals the FIRST level that succeeds; when both Live2D and CanvasAvatar fail
 * the manager enters text/voice mode and surfaces a "形象渲染不可用" notice; in
 * EVERY case the current conversation is never interrupted (degrade reports ok)
 * and a non-fallback renderer (Live3D) never appears as a fallback result.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator samples
 * whether each level's factory succeeds or throws. Live3D is always registered
 * to prove it is never selected as a fallback target (it depends on WebGL).
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeFakeRenderer } = require("./helpers/conformance");

test("Property 10: degrade falls through to the first successful level; Live3D is never a fallback (>=100 runs)", () => {
  fc.assert(
    fc.property(
      fc.boolean(), // Live2D level succeeds?
      fc.boolean(), // CanvasAvatar level succeeds?
      fc.constantFrom("Live2D", "Live3D"), // degrade source
      (live2dOk, canvasOk, fromType) => {
        const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
        const notices = [];

        const live2d = makeFakeRenderer({ type: "Live2D" });
        const canvas = makeFakeRenderer({ type: "CanvasAvatar" });
        const orb = makeFakeRenderer({ type: "Live3D", withAgentState: true });

        const mgr = new window.RendererManager({
          canvas: window.document.getElementById("avatar"),
          notify: (msg, meta) => notices.push({ msg, meta }),
          canvasFactory: () => {
            if (!canvasOk) throw new Error("CanvasAvatar unavailable");
            return canvas;
          },
        });
        mgr.register("Live2D", () => {
          if (!live2dOk) throw new Error("Live2D unavailable");
          return live2d;
        });
        // Live3D is registered but must NEVER be chosen as a fallback target.
        mgr.register("Live3D", () => orb);

        const result = mgr.degrade(fromType, new Error("trigger"));

        // Conversation is never interrupted: degrade always reports ok.
        assert.equal(result.ok, true, "degrade never interrupts the conversation");
        assert.equal(result.degraded, true);

        // Final mode equals the first successful level.
        if (live2dOk) {
          assert.equal(result.mode, "Live2D", "Live2D is first successful level");
          assert.equal(mgr.active, live2d);
          assert.equal(mgr.type, "Live2D");
          assert.equal(mgr.textVoiceMode, false);
        } else if (canvasOk) {
          assert.equal(result.mode, "CanvasAvatar", "CanvasAvatar is first successful level");
          assert.equal(mgr.active, canvas);
          assert.equal(mgr.textVoiceMode, false);
        } else {
          // Both failed → text/voice terminus with a visible notice (R9.6).
          assert.equal(result.mode, "text/voice");
          assert.equal(result.textVoice, true);
          assert.equal(mgr.active, null, "no active renderer in text/voice mode");
          assert.equal(mgr.textVoiceMode, true);
          const tv = notices.find((n) => n.meta && n.meta.kind === "degrade-text-voice");
          assert.ok(tv, "a text/voice degrade notice was surfaced");
          assert.match(tv.msg, /形象渲染不可用/, "notice states avatar rendering is unavailable");
        }

        // Live3D never appears as a chain level / fallback result, ever.
        assert.notEqual(result.mode, "Live3D");
        assert.notEqual(mgr.type, "Live3D");
        assert.notEqual(mgr.active, orb);
        assert.equal(orb.destroyed, false, "Live3D instance is never constructed/destroyed by the chain");
        assert.ok(result.attempts.every((a) => a.mode !== "Live3D"), "no chain level is Live3D");

        mgr.destroy();
      }
    ),
    { numRuns: 100 }
  );
});
