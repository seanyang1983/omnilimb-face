/*
 * interface-conformance.test.js — property test for task 1.3.
 *
 * Feature: switchable-avatar-renderers, Property 7: 渲染器接口一致性
 * Validates: Requirements 2.2, 2.3, 2.4, 2.5
 *
 * Property 7 (渲染器接口一致性): For ANY renderer implementation
 * (Live2DAvatar, CanvasAvatar, and the fake standing in for Live3DRenderer),
 * the six interface methods — setMouthOpen, setExpression, resetMouth,
 * setModel, playMotion, destroy — all exist and are functions; and a renderer
 * MAY additionally expose the optional setAgentState as a function.
 *
 * This is a PROPERTY test (fast-check, >=100 numRuns): a generator samples,
 * across the whole space of renderer implementations, both the renderer to
 * probe and HOW it is materialised (class prototype vs constructed instance,
 * plus randomised constructor options for the fakes). For every sample the
 * interface surface must conform. It reuses the shared conformance scaffold
 * (REQUIRED_METHODS / checkRendererInterface / assertRendererInterface and the
 * recording fakes) and the build-free jsdom loader, so no real WebGL/GPU is
 * needed.
 *
 * Live3D's not-yet-loaded renderer is represented by the scaffold's conforming
 * fake per task 1.3's instructions; its real implementation's conformance is
 * retested in the Live3D renderer test.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");
const {
  REQUIRED_METHODS,
  OPTIONAL_METHODS,
  checkRendererInterface,
  assertRendererInterface,
  makeFakeRenderer,
} = require("./helpers/conformance");

// Load the real front-end renderer classes once; each descriptor below decides
// whether to probe a class prototype or a freshly constructed instance.
const window = loadFrontend(["renderer-manager.js", "avatar.js"]);

/**
 * Descriptors for EVERY renderer implementation in the feature. Each yields a
 * fresh interface "surface" (class or instance) to probe, a friendly label, and
 * whether the optional setAgentState is also required.
 *
 * - Live2DAvatar / CanvasAvatar: real implemented classes (Requirements 2.2/2.3).
 * - Live3D: not yet loaded → conforming six-method fake (Requirement 2.3); its
 *   real conformance is retested in the Live3D renderer test.
 * - A fake WITH setAgentState exercises the optional-method clause (Req 2.5).
 */
const RENDERER_DESCRIPTORS = [
  {
    label: "CanvasAvatar (class)",
    requireSetAgentState: false,
    make: () => window.CanvasAvatar,
  },
  {
    label: "CanvasAvatar (instance)",
    requireSetAgentState: false,
    make: () => {
      const canvas = window.document.getElementById("avatar");
      return new window.CanvasAvatar(canvas);
    },
    cleanup: (inst) => {
      if (inst && typeof inst.destroy === "function") inst.destroy();
    },
  },
  {
    label: "Live2DAvatar (class)",
    requireSetAgentState: false,
    make: () => window.Live2DAvatar,
  },
  {
    label: "Live3DRenderer (fake stand-in)",
    requireSetAgentState: false,
    make: (opts) => makeFakeRenderer(Object.assign({ type: "Live3D" }, opts)),
  },
  {
    label: "agent-state renderer (fake stand-in)",
    requireSetAgentState: true,
    // A renderer that also exposes setAgentState; force it on regardless of opts.
    make: (opts) =>
      makeFakeRenderer(Object.assign({}, opts, { type: "Live3D", withAgentState: true })),
  },
];

test("Property 7: every renderer implementation conforms to Renderer_Interface (>=100 runs)", () => {
  fc.assert(
    fc.property(
      // Pick any renderer implementation from the whole space...
      fc.integer({ min: 0, max: RENDERER_DESCRIPTORS.length - 1 }),
      // ...and randomise the fakes' construction options to broaden the input
      // space (these only affect the fake stand-ins; real classes ignore them).
      fc.record({
        failSetModel: fc.boolean(),
        type: fc.constantFrom("Live2D", "Live3D"),
      }),
      (idx, opts) => {
        const desc = RENDERER_DESCRIPTORS[idx];
        const target = desc.make(opts);
        try {
          // The six required methods must all be present as functions; the
          // agent-state stand-in must additionally expose setAgentState.
          assertRendererInterface(assert, target, {
            label: desc.label,
            requireSetAgentState: desc.requireSetAgentState,
          });

          const { ok, missing } = checkRendererInterface(target, {
            requireSetAgentState: desc.requireSetAgentState,
          });
          assert.equal(ok, true, `${desc.label} missing: [${missing.join(", ")}]`);
        } finally {
          if (desc.cleanup) desc.cleanup(target);
        }
      }
    ),
    { numRuns: 100 }
  );
});

test("Property 7 (explicit): the six required methods are named exactly and are functions on each renderer", () => {
  // A non-property restatement to make the contract surface obvious and to fail
  // loudly per-method if any renderer regresses.
  assert.deepEqual(
    Array.from(REQUIRED_METHODS),
    ["setMouthOpen", "setExpression", "resetMouth", "setModel", "playMotion", "destroy"],
    "REQUIRED_METHODS describes exactly the six Renderer_Interface methods"
  );
  assert.deepEqual(Array.from(OPTIONAL_METHODS), ["setAgentState"]);

  for (const desc of RENDERER_DESCRIPTORS) {
    const target = desc.make({});
    try {
      assertRendererInterface(assert, target, {
        label: desc.label,
        requireSetAgentState: desc.requireSetAgentState,
      });
    } finally {
      if (desc.cleanup) desc.cleanup(target);
    }
  }
});

test("Property 7 (negative control): the setAgentState requirement bites for a six-method-only renderer", () => {
  // Confirms the property's optional-method clause is meaningful: a
  // six-method-only renderer (no setAgentState) conforms to the required set but
  // would FAIL the optional requirement — i.e. the requireSetAgentState flag bites.
  const sixOnly = makeFakeRenderer({ withAgentState: false });
  assert.equal(checkRendererInterface(sixOnly).ok, true, "six required methods present");
  const withOptional = checkRendererInterface(sixOnly, { requireSetAgentState: true });
  assert.equal(withOptional.ok, false);
  assert.deepEqual(withOptional.missing, ["setAgentState"]);

  // And a renderer that exposes setAgentState satisfies it.
  const agentStateRenderer = makeFakeRenderer({ type: "Live3D", withAgentState: true });
  assert.equal(checkRendererInterface(agentStateRenderer, { requireSetAgentState: true }).ok, true);
});
