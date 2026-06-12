# omnilimb-face front-end tests

A **self-contained** JS test area for the plain-JS front-end (`frontend/*.js`).
It lives entirely under `frontend/tests/` and has **no effect** on how the
plugin serves the front-end at runtime — the runtime front-end still ships
without a build step. Everything here is dev-time only.

## Stack

- **Test runner**: Node's built-in [`node --test`](https://nodejs.org/api/test.html)
  runner (no extra runner dependency, no hand-rolled framework).
- **Property-based testing**: [`fast-check`](https://github.com/dubzzz/fast-check)
  (≥100 random iterations per property) — we do **not** hand-roll a property
  framework.
- **DOM environment**: [`jsdom`](https://github.com/jsdom/jsdom), so the
  front-end IIFE scripts that attach to `window` can be loaded and exercised.

## Install & run

From this directory (`frontend/tests/`):

```bash
npm install        # installs fast-check + jsdom (dev only)
npm test           # runs all *.test.js via `node --test`
```

`npm test` runs `node --test`, which discovers every `*.test.js` file under
this directory. To watch: `npm run test:watch`.

## How the build-free front-end is loaded

`helpers/load-frontend.js` loads the **unmodified** front-end sources. Each
front-end file is an IIFE of the form `(function (global) { ... })(window)` that
attaches its exports onto `window`. The loader:

1. creates a jsdom window (`pretendToBeVisual: true` for `requestAnimationFrame`);
2. makes that window its own `window` self-reference and turns it into a `vm`
   context, so bare globals (`window`, `performance`, `requestAnimationFrame`,
   `document`) resolve;
3. evaluates the requested files in that context with `node:vm`.

It returns the populated `window`, from which tests read the attached classes
(e.g. `window.RendererManager`, `window.CanvasAvatar`, `window.Live2DAvatar`).
No front-end source is modified, and no bundler is involved.

```js
const { loadFrontend } = require("./helpers/load-frontend");
const window = loadFrontend(["renderer-manager.js", "avatar.js"]);
// window.RendererManager, window.CanvasAvatar, window.Live2DAvatar are ready.
```

To stub WebGL/three for renderers that need them, pass `extraGlobals`:

```js
const { makeStubThree } = require("./helpers/conformance");
const window = loadFrontend(["orb-renderer.js"], { extraGlobals: { THREE: makeStubThree() } });
```

## Shared renderer-conformance scaffold

`helpers/conformance.js` is the single source of truth for the
`Renderer_Interface` surface and provides reusable fixtures every per-renderer
test can share:

- `REQUIRED_METHODS` / `OPTIONAL_METHODS` / `RENDERER_TYPES` — the contract.
- `checkRendererInterface(target, { requireSetAgentState })` — pure check
  returning `{ ok, missing, present }`; accepts a class (probes its prototype)
  or an instance.
- `assertRendererInterface(assert, target, opts)` — asserts the six interface
  methods (and optionally `setAgentState`) exist and are functions.
- `RecordingFakeRenderer` / `makeFakeRenderer(opts)` — a fully-conformant,
  call-recording fake renderer (with failure-injection options) for later
  routing/clamp/no-op property tests.
- `makeStubThree()` / `makeStubWebGLContext()` / `stubCanvasWebGL(canvas)` —
  stubs so three/WebGL-dependent renderers (Live3D, Orb) can be constructed in
  tests without a real GPU.

This scaffold is the foundation reused by the per-renderer property tests added
in later tasks (e.g. task 1.3, Property 7).

## Files

| File | Role |
|------|------|
| `package.json` | dev deps (`fast-check`, `jsdom`) + `npm test` script |
| `helpers/load-frontend.js` | build-free jsdom loader for the front-end IIFE scripts |
| `helpers/conformance.js` | shared `Renderer_Interface` scaffold + reusable fakes/stubs |
| `smoke.test.js` | minimal harness smoke test (proves the plumbing runs) |
