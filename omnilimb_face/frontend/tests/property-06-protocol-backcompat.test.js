/*
 * property-06-protocol-backcompat.test.js — property test for task 3.2.
 *
 * Feature: switchable-avatar-renderers, Property 6: 协议向后兼容（忽略未知字段，既有语义不变）
 * Validates: Requirements 3.1, 3.3, 3.4, 3.5, 3.6, 7.8
 *
 * Property 6 (协议向后兼容): For ANY valid set-model-and-conf / audio message and
 * ANY set of additional unknown/additive fields (including additive fields that
 * are present-but-invalid or missing): the processing of the EXISTING fields
 * (url, emotionMap, volumes, slice_length, actions.expressions) is bit-for-bit
 * identical to processing the message WITHOUT the injected fields; a present-
 * but-invalid additive field produces EXACTLY ONE diagnostic while existing-
 * field processing is unaffected; and nothing throws.
 *
 * This is a PROPERTY test (fast-check, >=100 runs): the generator builds a valid
 * model_info + audio pair and injects random unknown fields plus the additive
 * vrm_url field in valid / invalid / missing states. We feed a
 * real VTuberProtocol both the baseline and the extended message (no socket
 * needed — _onMessage is driven directly) and compare the existing-field views.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");

const { loadFrontend } = require("./helpers/load-frontend");

/** Build a VTuberProtocol with recording handlers (no real WebSocket needed). */
function makeProtocol(window) {
  const captured = { setModel: [], audio: [], logs: [], unknown: 0 };
  const proto = new window.VTuberProtocol("ws://test/client-ws", {
    onSetModel: (modelInfo, msg, extras) => captured.setModel.push({ modelInfo, msg, extras }),
    onAudio: (msg) => captured.audio.push(msg),
    onLog: (line) => captured.logs.push(line),
  });
  return { proto, captured };
}

/** The existing model_info fields a back-compat renderer reads. */
function existingModelView(modelInfo) {
  return { name: modelInfo.name, url: modelInfo.url, emotionMap: modelInfo.emotionMap };
}

/** The existing audio fields driving lip-sync / expressions (R3.5/7.8). */
function existingAudioView(msg) {
  return {
    audio: msg.audio,
    volumes: msg.volumes,
    slice_length: msg.slice_length,
    display_text: msg.display_text,
    actions: msg.actions,
  };
}

test("Property 6: injected unknown/additive fields never change existing-field processing (>=100 runs)", () => {
  fc.assert(
    fc.property(
      // A valid base model_info.
      fc.record({
        name: fc.string(),
        url: fc.webUrl(),
        emotionMap: fc.dictionary(fc.string(), fc.nat()),
      }),
      // A valid base audio message body.
      fc.record({
        volumes: fc.array(fc.double({ min: 0, max: 1, noNaN: true }), { maxLength: 8 }),
        slice_length: fc.integer({ min: 1, max: 1000 }),
        expressions: fc.array(fc.nat({ max: 8 }), { maxLength: 4 }),
        text: fc.string(),
      }),
      // Additive field state: vrm_url valid/invalid/missing.
      fc.constantFrom("valid", "invalid", "missing"),
      // Arbitrary unknown extra keys to sprinkle onto the messages.
      fc.dictionary(fc.string({ minLength: 1 }), fc.jsonValue(), { maxKeys: 4 }),
      (baseModel, baseAudio, vrmState, unknownExtras) => {
        const window = loadFrontend(["protocol.js"]);

        // ---- baseline: existing fields only ----
        const { proto: p0, captured: c0 } = makeProtocol(window);
        const baselineModelMsg = { type: "set-model-and-conf", model_info: Object.assign({}, baseModel) };
        const baselineAudioMsg = {
          type: "audio",
          audio: "AAAA",
          volumes: baseAudio.volumes.slice(),
          slice_length: baseAudio.slice_length,
          display_text: { text: baseAudio.text },
          actions: { expressions: baseAudio.expressions.slice() },
          forwarded: false,
        };
        assert.doesNotThrow(() => p0._onMessage(JSON.stringify(baselineModelMsg)));
        assert.doesNotThrow(() => p0._onMessage(JSON.stringify(baselineAudioMsg)));

        // ---- extended: same existing fields + injected unknown/additive ----
        const { proto: p1, captured: c1 } = makeProtocol(window);
        const extModelInfo = Object.assign({}, baseModel, unknownExtras);
        // Keep existing keys authoritative (unknownExtras must not clobber them).
        Object.assign(extModelInfo, baseModel);

        let expectInvalidAdditive = false;
        if (vrmState === "valid") extModelInfo.vrm_url = "https://example.test/a.vrm";
        else if (vrmState === "invalid") {
          extModelInfo.vrm_url = 12345; // wrong type
          expectInvalidAdditive = true;
        }

        const extModelMsg = { type: "set-model-and-conf", model_info: extModelInfo, _unknown_top: 1 };
        const extAudioMsg = Object.assign({}, baselineAudioMsg, unknownExtras, {
          // Re-assert existing fields after the unknown sprinkle.
          type: "audio",
          audio: "AAAA",
          volumes: baseAudio.volumes.slice(),
          slice_length: baseAudio.slice_length,
          display_text: { text: baseAudio.text },
          actions: { expressions: baseAudio.expressions.slice() },
          forwarded: false,
        });
        assert.doesNotThrow(() => p1._onMessage(JSON.stringify(extModelMsg)));
        assert.doesNotThrow(() => p1._onMessage(JSON.stringify(extAudioMsg)));

        // Existing-field processing is bit-for-bit identical (R3.1/3.3/3.5/7.8).
        assert.deepEqual(
          existingModelView(c1.setModel[0].modelInfo),
          existingModelView(c0.setModel[0].modelInfo),
          "existing model_info fields unchanged by injected fields"
        );
        assert.deepEqual(
          existingAudioView(c1.audio[0]),
          existingAudioView(c0.audio[0]),
          "existing audio fields unchanged by injected fields"
        );

        // Additive diagnostics (R3.4): AT MOST ONE; exactly one iff a present
        // additive field was invalid; none when additive fields are valid/missing.
        const diagnostics = c1.logs.filter((l) => /additive field ignored/.test(l));
        if (expectInvalidAdditive) {
          assert.equal(diagnostics.length, 1, "exactly one diagnostic for an invalid additive field");
        } else {
          assert.equal(diagnostics.length, 0, "no diagnostic when additive fields are valid/missing");
        }

        // Valid additive fields are surfaced via the third `extras` arg only —
        // never by mutating the existing fields.
        const extras = c1.setModel[0].extras;
        if (vrmState === "valid") assert.equal(extras.vrmUrl, "https://example.test/a.vrm");
        else assert.equal(extras.vrmUrl, null);

        // Unknown well-formed event types are ignored without throwing (R3.3/3.6).
        assert.doesNotThrow(() => p1._onMessage(JSON.stringify({ type: "totally-unknown", foo: 1 })));
      }
    ),
    { numRuns: 100 }
  );
});
