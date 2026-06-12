/*
 * renderer-manager-degrade.test.js — unit tests for task 2.2.
 *
 * Covers RendererManager.degrade() — the ordered fallback chain
 *   Live2D → CanvasAvatar → 纯文本/语音 (text/voice)
 * using the shared recording fakes + failure injection (no real WebGL/GPU):
 *   - Live2D ok            → active becomes the Live2D-level renderer (mode "Live2D");
 *   - Live2D fails         → drops to the explicit CanvasAvatar level (mode "CanvasAvatar");
 *   - Live2D + Canvas fail → enters text/voice mode (no active renderer, visible notice,
 *                            conversation routing stays a safe no-op);
 *   - Live3D is NEVER a fallback target (depends on WebGL);
 *   - Live3D unavailable/crash entry point falls back to Live2D, destroying the
 *     dead DH instance while preserving conversation context (manager never touches it);
 *   - degrade is reachable from switchTo() when there is no pre-switch renderer to roll back to.
 *
 * These complement the dedicated property test P10 (optional task 2.7). They use the
 * build-free jsdom loader + conformance fakes.
 *
 * Requirements: 9.1, 9.3, 9.4, 9.5, 9.6
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { loadFrontend } = require("./helpers/load-frontend");
const { makeFakeRenderer } = require("./helpers/conformance");

/** Build a fresh RendererManager (and the window) for each test. */
function freshManager(options = {}) {
  const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
  const notices = [];
  const mgr = new window.RendererManager(
    Object.assign(
      {
        canvas: window.document.getElementById("avatar"),
        notify: (msg, meta) => notices.push({ msg, meta }),
      },
      options
    )
  );
  return { window, mgr, notices };
}

test("degrade: Live2D level succeeds first — active is the Live2D renderer (R9.5)", () => {
  const { mgr } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.register("Live2D", () => live2d);

  const result = mgr.degrade("Live3D", new Error("dh unavailable"));

  assert.equal(result.ok, true);
  assert.equal(result.degraded, true);
  assert.equal(result.mode, "Live2D", "first successful level is Live2D");
  assert.equal(result.textVoice, false);
  assert.equal(mgr.active, live2d, "active is the Live2D-level renderer");
  assert.equal(mgr.type, "Live2D");
  assert.equal(mgr.textVoiceMode, false);
  // The CanvasAvatar level must not have been attempted.
  assert.equal(result.attempts.length, 1);
  // Field-by-field (not deepEqual): the manager builds these objects inside the
  // jsdom vm realm, whose Object.prototype differs from this test realm's, so a
  // strict deep-equal against a plain literal would fail on prototype identity.
  assert.equal(result.attempts[0].mode, "Live2D");
  assert.equal(result.attempts[0].ok, true);
  assert.equal(result.attempts[0].error, null);
});

test("degrade: Live2D fails → drops to explicit CanvasAvatar level (R9.5)", () => {
  const canvasFake = makeFakeRenderer({ type: "CanvasAvatar" });
  const { mgr } = freshManager({ canvasFactory: () => canvasFake });
  // Live2D factory throws (simulates createAvatar/Live2D path unavailable).
  mgr.register("Live2D", () => {
    throw new Error("Live2D factory boom");
  });

  const result = mgr.degrade("Live3D", new Error("init failed"));

  assert.equal(result.ok, true);
  assert.equal(result.mode, "CanvasAvatar", "fell back to CanvasAvatar after Live2D failed");
  assert.equal(result.textVoice, false);
  assert.equal(mgr.active, canvasFake, "active is the CanvasAvatar-level renderer");
  assert.equal(mgr.type, "Live2D", "CanvasAvatar fallback reports the Live2D visual path type");
  assert.equal(mgr.textVoiceMode, false);
  // Two attempts recorded: Live2D failed, CanvasAvatar ok.
  assert.equal(result.attempts.length, 2);
  assert.equal(result.attempts[0].mode, "Live2D");
  assert.equal(result.attempts[0].ok, false);
  assert.ok(result.attempts[0].error, "Live2D failure reason captured");
  // Field-by-field (not deepEqual) to avoid cross-realm prototype mismatch.
  assert.equal(result.attempts[1].mode, "CanvasAvatar");
  assert.equal(result.attempts[1].ok, true);
  assert.equal(result.attempts[1].error, null);
});

test("degrade: Live2D fails → uses real window.CanvasAvatar when no canvasFactory injected", () => {
  const { window, mgr } = freshManager();
  mgr.register("Live2D", () => {
    throw new Error("Live2D unavailable");
  });

  const result = mgr.degrade("Live3D", new Error("webgl missing"));

  assert.equal(result.mode, "CanvasAvatar");
  assert.ok(mgr.active instanceof window.CanvasAvatar, "active is a real CanvasAvatar instance");
  assert.equal(mgr.textVoiceMode, false);

  // Clean up the CanvasAvatar's requestAnimationFrame loop so no timer leaks.
  mgr.destroy();
});

test("degrade: Live2D AND CanvasAvatar both fail → text/voice mode, conversation continues (R9.6)", () => {
  const { mgr, notices } = freshManager({
    canvasFactory: () => {
      throw new Error("CanvasAvatar boom");
    },
  });
  mgr.register("Live2D", () => {
    throw new Error("Live2D boom");
  });

  const result = mgr.degrade("Live3D", new Error("all gone"));

  assert.equal(result.ok, true, "degrade still reports ok — conversation continues");
  assert.equal(result.degraded, true);
  assert.equal(result.mode, "text/voice");
  assert.equal(result.textVoice, true);
  assert.equal(mgr.active, null, "no active renderer in text/voice mode");
  assert.equal(mgr.type, null);
  assert.equal(mgr.textVoiceMode, true, "text/voice flag is set");

  // A visible "形象渲染不可用" notice was surfaced.
  const textVoiceNotice = notices.find((n) => n.meta && n.meta.kind === "degrade-text-voice");
  assert.ok(textVoiceNotice, "a text/voice degrade notice was surfaced");
  assert.match(textVoiceNotice.msg, /形象渲染不可用/, "notice states avatar rendering is unavailable");

  // Both levels were attempted and failed.
  assert.equal(result.attempts.length, 2);
  assert.equal(result.attempts[0].ok, false);
  assert.equal(result.attempts[1].ok, false);

  // Conversation routing keeps working: every forward is a safe no-op, never throws (R9.4/R9.6).
  assert.doesNotThrow(() => mgr.setMouthOpen(0.5));
  assert.doesNotThrow(() => mgr.setExpression(2, "happy"));
  assert.doesNotThrow(() => mgr.resetMouth());
  assert.doesNotThrow(() => mgr.playMotion("Tap"));
  assert.doesNotThrow(() => mgr.setAgentState("speaking"));
  assert.equal(mgr.setModel("https://x/m.json"), undefined);
});

test("degrade: Live3D is NEVER a fallback target even when registered (R9 / Live3D depends on WebGL)", () => {
  const orb = makeFakeRenderer({ type: "Live3D", withAgentState: true });
  const live2d = makeFakeRenderer({ type: "Live2D" });
  const { mgr } = freshManager();
  mgr.register("Live3D", () => orb);
  mgr.register("Live2D", () => live2d);

  const result = mgr.degrade("Live3D", new Error("dh down"));

  assert.notEqual(result.mode, "Live3D", "Live3D is never the resulting fallback mode");
  assert.notEqual(mgr.type, "Live3D");
  assert.notEqual(mgr.active, orb, "the Live3D instance is never adopted as a fallback");
  assert.equal(orb.destroyed, false, "Live3D is never even constructed/destroyed by the chain");
  // No attempt entry ever names Live3D.
  assert.ok(result.attempts.every((a) => a.mode !== "Live3D"), "no chain level is Live3D");
});

test("degrade: Live3D never targeted even at the text/voice terminus", () => {
  const { mgr } = freshManager({
    canvasFactory: () => {
      throw new Error("no canvas");
    },
  });
  const orb = makeFakeRenderer({ type: "Live3D", withAgentState: true });
  mgr.register("Live3D", () => orb);
  mgr.register("Live2D", () => {
    throw new Error("no live2d");
  });

  const result = mgr.degrade("Live3D", new Error("webgl unavailable"));

  assert.equal(result.mode, "text/voice", "terminus is text/voice, never Live3D");
  assert.equal(orb.destroyed, false, "Live3D instance never constructed as a fallback");
  assert.ok(result.attempts.every((a) => a.mode !== "Live3D"));
});

test("degrade: Live3D crash entry destroys the dead DH instance and falls back to Live2D (R9.1, R9.3)", () => {
  const { mgr } = freshManager();
  const dh = makeFakeRenderer({ type: "Live3D" });
  mgr.adopt(dh, "Live3D"); // a live (now crashed) digital-human renderer

  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.register("Live2D", () => live2d);

  const result = mgr.degrade("Live3D", new Error("worker heartbeat lost"));

  assert.equal(result.mode, "Live2D", "fell back to Live2D");
  assert.equal(mgr.active, live2d, "Live2D renderer is now active");
  assert.equal(mgr.type, "Live2D");
  assert.equal(dh.destroyed, true, "the dead Live3D instance was destroyed (resource release)");
  assert.equal(result.fromType, "Live3D", "the degrade source is recorded");
});

test("degrade: invoked from switchTo() when there is no pre-switch renderer to roll back to", async () => {
  const { mgr } = freshManager();
  // No adopt(): manager starts with no active renderer.
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.register("Live2D", () => live2d);
  // Live3D construction fails → switchTo rolls back, but there is nothing to roll back to,
  // so it falls through to the degrade chain.
  mgr.register("Live3D", () => {
    throw new Error("Live3DRenderer unavailable");
  });

  const result = await mgr.switchTo("Live3D");

  assert.equal(result.ok, false, "the switch itself failed");
  // The degrade chain ran and brought up the Live2D fallback.
  assert.equal(mgr.active, live2d, "degrade chain activated the Live2D fallback");
  assert.equal(mgr.type, "Live2D");
  assert.equal(mgr.textVoiceMode, false);
});

test("degrade: does not tear down subsystems — only swaps the renderer instance (R9.4)", () => {
  const { mgr } = freshManager();
  const dh = makeFakeRenderer({ type: "Live3D" });
  mgr.adopt(dh, "Live3D");
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.register("Live2D", () => live2d);

  // Driving the manager before and after degrade must never throw — the manager owns no
  // STT/TTS/persona/barge-in state and never clears any conversation queue.
  assert.doesNotThrow(() => mgr.setMouthOpen(0.4));
  mgr.degrade("Live3D", new Error("crash"));
  assert.doesNotThrow(() => mgr.setMouthOpen(0.6));
  assert.doesNotThrow(() => mgr.setExpression(1, "neutral"));

  // Post-degrade signals reach the NEW active (Live2D) renderer.
  assert.equal(live2d.mouthOpen, 0.6, "post-degrade lip-sync routes to the Live2D fallback");
});
