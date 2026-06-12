/*
 * renderer-manager-switch.test.js — unit tests for task 2.1.
 *
 * Covers RendererManager.switchTo / awaitReady / destroy-on-switch / signal
 * routing using the shared recording fakes:
 *   - switchTo idempotence (same type → no destroy, no rebuild, same instance);
 *   - destroy-on-switch (old destroyed, new adopted, type updated);
 *   - no-active no-op routing (every forward is a safe no-op, never throws);
 *   - rollback-on-failure (construct throw AND readiness timeout both restore
 *     the SAME pre-switch instance, preserve type, emit one switch-failed
 *     notice, and never destroy the pre-switch renderer);
 *   - event continuity (driving routes to the new renderer after switch).
 *
 * These complement the dedicated property tests P1/P2/P9/P17 (optional tasks
 * 2.4–2.8). They use the build-free jsdom loader + conformance fakes so no real
 * WebGL/GPU is required.
 *
 * Requirements: 1.2, 1.3, 1.4, 1.5, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 2.6, 2.7
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

test("switchTo: same type is idempotent — no destroy, no rebuild, same instance (R1.3)", async () => {
  const { mgr } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.adopt(live2d, "Live2D");

  let factoryCalls = 0;
  mgr.register("Live2D", () => {
    factoryCalls += 1;
    return makeFakeRenderer({ type: "Live2D" });
  });

  const result = await mgr.switchTo("Live2D");

  assert.equal(result.switched, false, "same-type switch must not rebuild");
  assert.equal(result.ok, true);
  assert.equal(mgr.active, live2d, "active is the SAME instance");
  assert.equal(mgr.type, "Live2D");
  assert.equal(live2d.destroyed, false, "the active renderer must not be destroyed");
  assert.equal(factoryCalls, 0, "the factory must not be invoked for a same-type switch");
});

test("switchTo: different type destroys old, adopts new, updates type (R1.2, R1.4, R11.1)", async () => {
  const { mgr } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.adopt(live2d, "Live2D");

  const orb = makeFakeRenderer({ type: "Live3D", withAgentState: true });
  mgr.register("Live3D", () => orb);

  const result = await mgr.switchTo("Live3D");

  assert.equal(result.ok, true);
  assert.equal(result.switched, true);
  assert.equal(mgr.active, orb, "new instance is now active");
  assert.equal(mgr.type, "Live3D");
  assert.equal(live2d.destroyed, true, "old instance destroy() was called exactly via switch (R1.4)");
  // destroy recorded exactly once on the old renderer.
  assert.equal(
    live2d.calls.filter((c) => c.method === "destroy").length,
    1,
    "old renderer destroyed exactly once"
  );
});

test("switchTo: old renderer's mouth driving is stopped before swap (R11.2)", async () => {
  const { mgr } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  live2d.setMouthOpen(0.8); // simulate an in-flight lip-sync target
  mgr.adopt(live2d, "Live2D");

  const orb = makeFakeRenderer({ type: "Live3D" });
  mgr.register("Live3D", () => orb);

  await mgr.switchTo("Live3D");

  // resetMouth was invoked on the OLD renderer to stop its mouth driving.
  assert.ok(
    live2d.calls.some((c) => c.method === "resetMouth"),
    "old renderer resetMouth() called to stop driving"
  );
  assert.equal(live2d.mouthOpen, 0, "old renderer mouth target reset to 0");
});

test("routing: no active renderer makes every forward a safe no-op (R2.7)", () => {
  const { mgr } = freshManager();
  assert.equal(mgr.active, null);

  // None of these may throw, and setModel returns undefined with no active.
  assert.doesNotThrow(() => mgr.setMouthOpen(0.5));
  assert.doesNotThrow(() => mgr.setMouthOpen(NaN));
  assert.doesNotThrow(() => mgr.setExpression(3, "joy"));
  assert.doesNotThrow(() => mgr.resetMouth());
  assert.doesNotThrow(() => mgr.playMotion("Tap"));
  assert.doesNotThrow(() => mgr.setAgentState("speaking"));
  assert.equal(mgr.setModel("https://x/m.json"), undefined);
});

test("routing: setAgentState only forwards when the active renderer implements it (R2.12)", () => {
  const { mgr } = freshManager();

  const withState = makeFakeRenderer({ type: "Live3D", withAgentState: true });
  mgr.adopt(withState, "Live3D");
  mgr.setAgentState("thinking");
  assert.equal(withState.agentState, "thinking");

  const withoutState = makeFakeRenderer({ type: "Live2D", withAgentState: false });
  mgr.adopt(withoutState, "Live2D");
  assert.equal(typeof withoutState.setAgentState, "undefined");
  // Must be a no-op guarded by typeof — never throws.
  assert.doesNotThrow(() => mgr.setAgentState("idle"));
});

test("switchTo: construction failure rolls back to the SAME pre-switch instance (R1.5, R11.6)", async () => {
  const { mgr, notices } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.adopt(live2d, "Live2D");

  // Live3D factory throws → simulates a renderer whose global isn't loaded yet.
  mgr.register("Live3D", () => {
    throw new Error("Live3DRenderer unavailable");
  });

  const result = await mgr.switchTo("Live3D");

  assert.equal(result.ok, false, "switch reports failure");
  assert.equal(result.switched, false);
  assert.equal(result.phase, "construct");
  assert.equal(mgr.active, live2d, "rolled back to the SAME pre-switch instance");
  assert.equal(mgr.type, "Live2D", "pre-switch type preserved");
  assert.equal(live2d.destroyed, false, "pre-switch renderer is NOT destroyed on failure");
  assert.equal(notices.length, 1, "exactly one switch-failed notice surfaced");
  assert.equal(notices[0].meta.kind, "switch-failed");
});

test("switchTo: readiness timeout rolls back and destroys the half-built instance (R11.4, R11.6)", async () => {
  const { mgr, notices } = freshManager({ readyTimeoutMs: 40 });
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.adopt(live2d, "Live2D");

  // A renderer whose readiness never resolves within the timeout window.
  const stuck = makeFakeRenderer({ type: "Live3D" });
  stuck.whenReady = () => new Promise(() => {}); // never settles
  mgr.register("Live3D", () => stuck);

  const result = await mgr.switchTo("Live3D");

  assert.equal(result.ok, false);
  assert.equal(result.phase, "ready");
  assert.equal(mgr.active, live2d, "rolled back to the pre-switch instance after ready timeout");
  assert.equal(mgr.type, "Live2D");
  assert.equal(live2d.destroyed, false, "pre-switch renderer preserved");
  assert.equal(stuck.destroyed, true, "the half-built, never-ready instance is destroyed");
  assert.equal(notices.length, 1);
  assert.equal(notices[0].meta.kind, "switch-failed");
});

test("switchTo: a renderer that throws from whenReady rolls back (R1.5)", async () => {
  const { mgr } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.adopt(live2d, "Live2D");

  const bad = makeFakeRenderer({ type: "Live3D" });
  bad.whenReady = () => Promise.reject(new Error("init blew up"));
  mgr.register("Live3D", () => bad);

  const result = await mgr.switchTo("Live3D");

  assert.equal(result.ok, false);
  assert.equal(mgr.active, live2d);
  assert.equal(mgr.type, "Live2D");
  assert.equal(bad.destroyed, true, "failed instance cleaned up");
});

test("awaitReady: resolves immediately when the renderer exposes no readiness signal", async () => {
  const { mgr } = freshManager();
  const r = makeFakeRenderer({ type: "Live2D" });
  const res = await mgr.awaitReady(r, 1000);
  assert.equal(res.ok, true);
});

test("awaitReady: honors a renderer's resolving whenReady() within the window", async () => {
  const { mgr } = freshManager();
  const r = makeFakeRenderer({ type: "Live3D" });
  r.whenReady = () => Promise.resolve();
  const res = await mgr.awaitReady(r, 1000);
  assert.equal(res.ok, true);
});

test("event continuity: after switch, driving routes to the NEW renderer; queue/context untouched (R11.3, R11.5)", async () => {
  const { mgr } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.adopt(live2d, "Live2D");
  const orb = makeFakeRenderer({ type: "Live3D", withAgentState: true });
  mgr.register("Live3D", () => orb);

  // Drive the OLD renderer first.
  mgr.setMouthOpen(0.3);
  mgr.setExpression(1, "happy");
  assert.equal(live2d.mouthOpen, 0.3);

  await mgr.switchTo("Live3D");

  // Subsequent signals must reach the NEW renderer exactly once, in order.
  mgr.setMouthOpen(0.7);
  mgr.setExpression(2, "sad");
  mgr.setAgentState("speaking");

  const orbMouth = orb.calls.filter((c) => c.method === "setMouthOpen");
  assert.equal(orbMouth.length, 1, "new renderer received exactly one post-switch mouth update");
  assert.equal(orb.mouthOpen, 0.7);
  assert.equal(orb.agentState, "speaking");

  // The old renderer never receives post-switch driving signals.
  const oldMouthAfter = live2d.calls
    .filter((c) => c.method === "setMouthOpen")
    .map((c) => c.args[0]);
  assert.ok(!oldMouthAfter.includes(0.7), "old renderer must not receive post-switch signals");
});

test("switchTo: unknown renderer type is ignored without throwing or changing state", async () => {
  const { mgr, notices } = freshManager();
  const live2d = makeFakeRenderer({ type: "Live2D" });
  mgr.adopt(live2d, "Live2D");

  const result = await mgr.switchTo("Hologram");

  assert.equal(result.ok, false);
  assert.equal(mgr.active, live2d, "state unchanged on invalid type");
  assert.equal(mgr.type, "Live2D");
  assert.equal(live2d.destroyed, false);
  assert.equal(notices[0].meta.kind, "switch-invalid");
});
