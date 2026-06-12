/*
 * design-system.test.js — sanity checks for the Design_System tokens (task 10.1).
 *
 * This is a lightweight UNIT sanity check (not the full Property 21 test, which
 * lives in task 10.2). It parses the static `frontend/styles.css` :root token
 * block and asserts the Design_System contract that task 10.1 must uphold so the
 * UI is consistent, accessible and smooth (Requirement 14):
 *
 *   - the core design tokens (colors / typography / spacing / radius / shadow /
 *     transition) are DEFINED in :root (Requirement 14.1);
 *   - body text vs its background(s) meets WCAG AA normal-text contrast (>=4.5:1)
 *     and muted text vs surface also clears 4.5:1 (Requirement 14.5);
 *   - every transition-duration token is <= 400ms (Requirement 14.7);
 *   - the renderer selector (#renderer-select) is present in the appearance tab
 *     markup with exactly the two renderer options (Requirement 1.1).
 *
 * Pixel-level layout, keyboard-focus walkthrough and the renderer frame-rate /
 * full WCAG audit are manual/visual checks (see the task's verification notes).
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND_DIR = path.resolve(__dirname, "..");
const CSS = fs.readFileSync(path.join(FRONTEND_DIR, "styles.css"), "utf8");
const HTML = fs.readFileSync(path.join(FRONTEND_DIR, "index.html"), "utf8");

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

const TOKENS = parseTokens(rootBlock(CSS));

test("Design_System: core token families are defined in :root (R14.1)", () => {
  const required = [
    // colors (incl. dark theme defaults)
    "--color-bg",
    "--color-surface",
    "--color-text",
    "--color-text-muted",
    "--color-primary",
    "--color-accent",
    "--color-ok",
    "--color-warn",
    "--color-err",
    "--color-border",
    // typography
    "--font-sans",
    "--fs-400",
    "--fs-700",
    "--lh-base",
    // spacing
    "--space-1",
    "--space-8",
    // radius
    "--radius-sm",
    "--radius-lg",
    // shadow
    "--shadow-1",
    "--shadow-2",
    // transition
    "--transition-base",
  ];
  for (const name of required) {
    assert.ok(
      Object.prototype.hasOwnProperty.call(TOKENS, name),
      `token ${name} must be defined in :root`
    );
  }
});

test("Design_System: body text vs background meets WCAG AA normal text (>=4.5:1) (R14.5)", () => {
  const text = hexToRgb(TOKENS["--color-text"]);
  const bg = hexToRgb(TOKENS["--color-bg"]);
  const surface = hexToRgb(TOKENS["--color-surface"]);
  assert.ok(text && bg && surface, "text/bg/surface tokens must be hex colors");

  const cBg = contrast(text, bg);
  const cSurface = contrast(text, surface);
  assert.ok(cBg >= 4.5, `text/bg contrast ${cBg.toFixed(2)} must be >= 4.5`);
  assert.ok(
    cSurface >= 4.5,
    `text/surface contrast ${cSurface.toFixed(2)} must be >= 4.5`
  );
});

test("Design_System: muted text vs surface meets WCAG AA normal text (>=4.5:1) (R14.5)", () => {
  const muted = hexToRgb(TOKENS["--color-text-muted"]);
  const surface = hexToRgb(TOKENS["--color-surface"]);
  const bg = hexToRgb(TOKENS["--color-bg"]);
  assert.ok(muted && surface && bg, "muted/surface/bg tokens must be hex colors");
  assert.ok(
    contrast(muted, surface) >= 4.5,
    `muted/surface contrast ${contrast(muted, surface).toFixed(2)} must be >= 4.5`
  );
  assert.ok(
    contrast(muted, bg) >= 4.5,
    `muted/bg contrast ${contrast(muted, bg).toFixed(2)} must be >= 4.5`
  );
});

test("Design_System: every transition-duration token is <= 400ms (R14.7)", () => {
  const durations = Object.keys(TOKENS).filter((k) => /^--transition-/.test(k));
  assert.ok(durations.length > 0, "at least one --transition-* token must exist");
  for (const name of durations) {
    const ms = durationMs(TOKENS[name]);
    assert.ok(ms !== null, `${name} (${TOKENS[name]}) must be a duration`);
    assert.ok(ms <= 400, `${name}=${ms}ms must be <= 400ms`);
  }
});

test("appearance tab: #renderer-select present with exactly the two renderer options (R1.1)", () => {
  assert.match(HTML, /id="renderer-select"/, "#renderer-select must exist");
  const sel = HTML.match(/<select[^>]*id="renderer-select"[\s\S]*?<\/select>/);
  assert.ok(sel, "renderer-select markup must be parseable");
  const values = [...sel[0].matchAll(/value="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(values, ["Live2D", "Live3D"]);
});
