# omnilimb-face

![omnilimb-face](docs/assets/banner.svg)

**English** · [中文](README.zh-CN.md)

A standalone, installable [hermes-agent](https://github.com/NousResearch) plugin
that gives your agent a **face and a voice**: hands-free voice interaction
(VAD + STT), real-time barge-in, and a Live2D / Live3D avatar with lip-sync and
expression driving — all **without modifying any hermes core file**. Part of the
[omnilimb](https://github.com/seanyang1983/omnilimb) family.

## Demo

The avatar renders on top and speaks the agent's reply with lip-sync and
expressions; you type (or talk) in the dialog box below.

![omnilimb-face demo](docs/assets/demo.gif)

> The animation above is a synthetic preview (`python preview.py`); a still frame
> is in [`docs/assets/screenshot.png`](docs/assets/screenshot.png).
> Avatar: Live2D Cubism sample **"Mao" © Live2D Inc.** (loaded from CDN, not
> bundled — see [Credits](#credits--third-party-licensing)).

> 📖 **Full docs: [`docs/GUIDE.md`](docs/GUIDE.md)** — overview / architecture /
> install / config / commands (CLI · slash · tools) / Live2D ↔ Live3D switching /
> real-time barge-in / troubleshooting / development.
>
> 🎭 **Avatar deep-dive: [`docs/AVATAR_INTEGRATION.md`](docs/AVATAR_INTEGRATION.md)** —
> how Live2D/Live3D expressions, motions and lip-sync are bound, how to import
> your own model (Cubism / VRM), and the 2D + 3D fusion roadmap.

## How it works

The plugin reuses hermes' existing systems instead of carrying its own model
config: transcripts are injected via `ctx.inject_message`, the host agent's
normal turn produces the reply (using the user's active model, tools and
memory), and the reply text is intercepted through the `transform_llm_output` /
`post_llm_call` hooks to drive TTS and the avatar. Speech transcription reuses
the `stt` config section; speech synthesis reuses the `tts` section. The plugin
**never calls an LLM itself** — the avatar always speaks your configured agent's
real answer.

![architecture](docs/assets/architecture.svg)

## Getting started

Requires **Python 3.11+**. Pick one of the two paths below — each is step by step.

### Option A — 1-minute preview (no hermes-agent needed)

```bash
# 1) install EVERYTHING in one command (incl. the Edge-TTS voice, so you HEAR it)
pip install -e ".[all]"
# 2) start the preview (it serves the web page AND the gateway)
python preview.py
# 3) open the PAGE in your browser:
#       http://127.0.0.1:12394/      <-- the web page
```

Type in the page and the avatar replies in Chinese voice with lip-sync + expressions.
(Click or type once first — browsers only allow audio after a user gesture.)

> ⚠️ **The page is on port 12394, not 12393.** Port **12393** is the WebSocket
> *gateway* (`ws://…/client-ws`); opening `http://127.0.0.1:12393/` in a browser
> will NOT work. The page lives on `gateway port + 1` = **12394**. (In single-port
> mode — `python preview.py --single-port` / `--https` — the page and gateway
> share 12393.)

### Option B — full product (chat in hermes, the avatar speaks the real replies)

```bash
# 1) install into the hermes venv, everything included, in ONE command
<hermes-venv>/python -m pip install -e "path/to/omnilimb-face[all]"
# 2) enable it: add `omnilimb-face` to plugins.enabled in ~/.hermes/config.yaml
# 3) run hermes — it starts the gateway (12393) + front-end (12394)
hermes
hermes vtuber status                 # check the avatar subsystem came up
# 4) open http://127.0.0.1:12394/ , then chat in the hermes terminal —
#    the avatar speaks each reply with lip-sync + expressions.
```

Speech uses the host `text_to_speech` tool when present, else the keyless Edge-TTS
fallback (Chinese voice by default; set `tts.<provider>.voice` to an Edge
`*Neural` voice to change it). The `/client-ws` gateway works with websockets
12.x and 13–15.x.

### Smaller installs (optional)

`[all]` pulls everything for a working setup. If you want a smaller footprint,
install only the pieces you need — the **core** install runs in a *degraded* state
without them (it still registers and its tools stay visible in `hermes tools`):

```bash
pip install -e ".[voice]"      # microphone capture + VAD (hands-free)
pip install -e ".[wakeword]"   # optional wake-word activation
pip install -e ".[live2d]"     # front-end static serving
pip install -e ".[preview]"    # Edge-TTS voice + local STT for `preview.py`
pip install -e ".[dev]"        # test tooling
```

### Enabling in hermes

Discovered via the `hermes_agent.plugins` pip entry point, or by placing the
directory at `~/AppData/Local/hermes/plugins/omnilimb-face/`. Enable it by adding
`omnilimb-face` to `plugins.enabled` in `config.yaml`.

### Mobile (phone) support

The avatar and hands-free voice also run on a phone. Because browsers only allow
microphone access from a secure context, the preview can serve over **HTTPS with
a self-signed certificate** on your LAN:

```bash
python preview.py --lan --https          # HTTPS on your LAN IP (single port 12393)
# or use start.bat options 3 / 4 (LAN HTTPS, optionally with --llm --stt)
```

Then open `https://<your-LAN-IP>:12393/` on the phone (same Wi-Fi) and accept the
self-signed certificate warning once — after that the mobile mic works. Cert
generation needs the `[preview]` extra (`cryptography`).

## Troubleshooting

- **`http://127.0.0.1:12393/` won't open.** That's the WebSocket *gateway*, not a
  web page. Open **`http://127.0.0.1:12394/`** (gateway port **+ 1**).
- **No sound.** Install the voice engine: `pip install edge-tts` (or
  `pip install -e ".[all]"`). The preview prints `voice: on (edge-tts …)` when it
  is active. You also need internet (Edge online voices) and to click/type once
  in the page first (browser autoplay policy).
- **I installed it but nothing happens / no avatar.** Installing only adds the
  code — you still have to START it: `python preview.py` (Option A) or
  `hermes vtuber start` (Option B), then open `http://127.0.0.1:12394/`.
- **Avatar doesn't appear.** The Live2D model loads from CDN — check your
  internet. Offline, it falls back to a dependency-free canvas placeholder.

## Layout

```
omnilimb-face/
├── pyproject.toml          # packaging, pinned deps, optional extras, entry point
├── plugin.yaml             # PluginManifest (name/version/hooks/tools)
├── __init__.py             # directory-discovery shim -> re-exports register
├── omnilimb_face/          # plugin package
│   ├── plugin.py           # register(ctx) entry point
│   ├── voice/              # capture, VAD, wake-word
│   └── protocol/           # /client-ws event models + gateway
└── tests/                  # pytest + Hypothesis (unit / property / integration)
```

## Development

```bash
pytest                                   # run the test suite
HYPOTHESIS_PROFILE=ci pytest             # heavier property-test run
```

### Releasing (maintainers)

Bump `version` in `pyproject.toml`, then publish with the helper script:

```powershell
py -3.11 -m pip install --upgrade build twine   # one-time
./release.ps1                                   # build + upload to PyPI
./release.ps1 -TestPyPI                         # dry-run against TestPyPI first
./release.ps1 -SkipBuild                        # upload an existing dist/ only
```

`release.ps1` reads the project-scoped PyPI token from a local `.env`
(`PYPI_API_TOKEN_FACE`) that lives **outside** the repo and is never committed.
The token is used transiently and masked in all output. PyPI rejects re-uploading
an existing version, so always bump `version` first.

## License

Licensed under the **GNU Affero General Public License v3.0 or later
(AGPL-3.0-or-later)** — see [`LICENSE`](LICENSE).

In short: you are free to use, study, modify and share this software, **including
for commercial purposes**, but if you distribute it **or run a modified version
as a network service**, you must release your complete corresponding source
under the AGPL as well. This keeps every downstream version open.

**Commercial / proprietary license available.** If you want to use omnilimb-face
in a closed-source or proprietary product without the AGPL's source-disclosure
obligations, a separate commercial license can be purchased — see
[`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md) or contact
**yase19636404@163.com**.

Copyright © 2025 seanyang1983.

### Credits & third-party licensing

This plugin **does not bundle or redistribute** any avatar models, the Live2D
Cubism Core, or any third-party front-end runtime. Everything below is loaded
from CDN at runtime only (with a dependency-free canvas fallback when offline).
Full details in [`NOTICE.md`](NOTICE.md) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

- **Open-LLM-VTuber** — the `/client-ws` protocol here is an independent
  re-implementation *compatible* with
  [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) (MIT
  through v1.2.0 as of 2026-06; see `NOTICE.md` for the license-transition note).
  No upstream source is copied. Thanks to the project for the protocol design.
- **Live2D Cubism sample model** — the default avatar (`models/model_dict.json`)
  references Live2D Inc.'s official Cubism sample **"Mao"** (a commit-pinned CDN
  URL only). The Cubism Core runtime is proprietary to Live2D Inc. and is loaded
  from Live2D's CDN, never bundled.

  > This content uses sample data owned and copyrighted by Live2D Inc. (the
  > "Terms of Use for Live2D Cubism Sample Data" / Live2D Cubism SDK license —
  > https://www.live2d.com/en/).

  Live2D's official samples are free for general users and small-scale
  enterprises (latest annual sales under 10,000,000 JPY); larger entities are
  subject to additional Live2D terms. **For commercial / large-scale use, swap in
  a model you own or are licensed to use** by editing `models/model_dict.json`.
- **pixi.js / pixi-live2d-display / three.js / @pixiv/three-vrm** — all MIT,
  loaded from CDN.
- **Optional Python deps** — `edge-tts` (`[preview]`, **GPL-3.0**, never bundled)
  and `openWakeWord` (`[wakeword]`, Apache-2.0 library but its pre-trained models
  are **CC-BY-NC-SA-4.0 / NonCommercial**) need attention for commercial use.
