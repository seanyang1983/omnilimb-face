/*
 * property-21-design-tokens.test.js — property test for task 10.2.
 *
 * Feature: switchable-avatar-renderers, Property 21: Design_System 对比度与过渡时长不变量
 * Validates: Requirements 14.5, 14.7
 *
 * Property 21 (Design_System 对比度与过渡时长不变量): For ANY "body-text color /
 * its background color" token pair, the WCAG contrast ratio is at least the AA
 * threshold (normal text >= 4.5:1, large text >= 3:1); and for ANY --transition-*
 * duration token, the value is <= 400ms.
 *
 * This is a PROPERTY test (fast-check). Both quantified spaces — the set of
 * body-text/background token pairs and the set of transition-duration tokens —
 * are FINITE, so the property quantifies over the full set by sampling it with
 * fast-check (numRuns >= the set size and >= 100). It parses the :root token
 * block out of frontend/styles.css and reuses the hex / WCAG-contrast / duration
 * helpers from the design-system unit sanity test, lifted here as pure functions.
 *
 * Full WCAG AA compliance also requires assistive-technology and expert review;
 * this property verifies the computable token-pair contrast + transition-duration
 * subset only (see the design's Testing Strategy notes).
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fc = require("fast-check");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND_DIR = path.resolve(__dirname, "..");
const CSS = fs.readFileSync(path.join(FRONTEND_DIR, "styles.css"), "utf8");

// ----- pure helpers (reused from design-system.test.js) -------------------

/** Extract the first `:root { ... }` declaration block from the CSS. */
function rootBlock(css) {
  const m = css.match(/:root\s*\{([\s\S]*?)\}/);
  assert.ok(m, ":root token block must exist in styles.css");
  return m[1];
}

/** Parse `--name: value;` pairs out of a CSS block into a map. */
function parseTokens(block) {
  const tokens = {};
  const re = /(--[a-z0-9-]+)\s*:\s*([^;]+);/gi;
  let m;
  while ((m = re.exec(block)) !== null) {
    tokens[m[1].trim()] = m[2].trim();
  }
  return tokens;
}

/** Parse a #rrggbb (or #rgb) hex string into [r,g,b] 0-255, else null. */
function hexToRgb(hex) {
  if (typeof hex !== "string") return null;
  let h = hex.trim().replace(/^#/, "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  if (!/^[0-9a-f]{6}$/i.test(h)) return null;
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

/** WCAG relative luminance for an [r,g,b] (0-255) color. */
function relLuminance([r, g, b]) {
  const lin = [r, g, b]
    .map((v) => v / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)));
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

/** WCAG contrast ratio between two [r,g,b] colors. */
function contrast(a, b) {
  const la = relLuminance(a);
  const lb = relLuminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

/** Parse a duration token like "360ms" / "0.4s" into milliseconds, else null. */
function durationMs(value) {
  if (typeof value !== "string") return null;
  const m = value.trim().match(/^([\d.]+)\s*(ms|s)$/i);
  if (!m) return null;
  const n = parseFloat(m[1]);
  return m[2].toLowerCase() === "s" ? n * 1000 : n;
}

// ----- build the finite quantified sets -----------------------------------

const TOKENS = parseTokens(rootBlock(CSS));

// WCAG AA thresholds.
const AA_NORMAL = 4.5;
const AA_LARGE = 3.0;

// Body-text color tokens and their candidate background tokens. These are the
// "body-text color / its background" combinations the Design_System renders
// body copy on (main surface + base background). All are treated as NORMAL text
// (the strict 4.5:1 case, which subsumes the large-text 3:1 case).
const TEXT_TOKENS = ["--color-text", "--color-text-muted"];
const BG_TOKENS = ["--color-bg", "--color-surface"];

// The full finite set of text/background token pairs to quantify over.
const TEXT_BG_PAIRS = [];
for (const textTok of TEXT_TOKENS) {
  for (const bgTok of BG_TOKENS) {
    TEXT_BG_PAIRS.push({ textTok, bgTok, size: "normal" });
  }
}

// The full finite set of transition-duration tokens to quantify over.
const TRANSITION_TOKENS = Object.keys(TOKENS).filter((k) => /^--transition-/.test(k));

// numRuns: at least 100, and at least as large as each finite set so the full
// space is sampled under fast-check.
const RUNS_PAIRS = Math.max(100, TEXT_BG_PAIRS.length);
const RUNS_TRANS = Math.max(100, TRANSITION_TOKENS.length);

// ----- properties ----------------------------------------------------------

test("Property 21a: every body-text/background token pair meets WCAG AA contrast (>=100 runs)", () => {
  assert.ok(TEXT_BG_PAIRS.length > 0, "there must be at least one text/background pair");

  fc.assert(
    fc.property(fc.constantFrom(...TEXT_BG_PAIRS), (pair) => {
      const text = hexToRgb(TOKENS[pair.textTok]);
      const bg = hexToRgb(TOKENS[pair.bgTok]);
      assert.ok(text, pair.textTok + " must be a hex color");
      assert.ok(bg, pair.bgTok + " must be a hex color");

      const ratio = contrast(text, bg);
      const threshold = pair.size === "large" ? AA_LARGE : AA_NORMAL;
      assert.ok(
        ratio >= threshold,
        `${pair.textTok} on ${pair.bgTok}: contrast ${ratio.toFixed(2)} must be >= ${threshold} (${pair.size} text, R14.5)`
      );
    }),
    { numRuns: RUNS_PAIRS }
  );
});

test("Property 21b: every --transition-* duration token is <= 400ms (>=100 runs)", () => {
  assert.ok(TRANSITION_TOKENS.length > 0, "at least one --transition-* token must exist");

  fc.assert(
    fc.property(fc.constantFrom(...TRANSITION_TOKENS), (name) => {
      const ms = durationMs(TOKENS[name]);
      assert.ok(ms !== null, `${name} (${TOKENS[name]}) must be a parseable duration`);
      assert.ok(ms <= 400, `${name}=${ms}ms must be <= 400ms (R14.7)`);
    }),
    { numRuns: RUNS_TRANS }
  );
});
