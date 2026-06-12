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

## Install

Requires Python 3.11+.

```bash
# From the plugin directory, in a virtual environment:
pip install -e ".[dev]"        # core + test tooling
pip install -e ".[voice]"      # add microphone capture + VAD
pip install -e ".[wakeword]"   # add optional wake-word activation
pip install -e ".[live2d]"     # add front-end static serving
```

The **core** install deliberately excludes the voice/Live2D packages. When those
optional extras are missing the plugin still registers in a *degraded* state and
its tools stay visible in `hermes tools`.

### Enabling

Discovered via the `hermes_agent.plugins` pip entry point, or by placing the
directory at `~/AppData/Local/hermes/plugins/omnilimb-face/`. Enable it by adding
`omnilimb-face` to `plugins.enabled` in `config.yaml`.

### Full product: chat in hermes, the avatar speaks the agent's replies

```bash
# 1. install the plugin into the hermes venv (editable; no dependency changes)
<hermes-venv>/python -m pip install -e path/to/omnilimb-face --no-deps
# 2. voice backend: hermes has no text_to_speech tool, so the plugin uses a
#    keyless Edge-TTS fallback. Install it into the hermes venv:
<hermes-venv>/python -m pip install edge-tts
# 3. enable the plugin: add `omnilimb-face` to plugins.enabled in
#    ~/.hermes/config.yaml
# 4. run hermes; it starts the /client-ws gateway (12393) + front-end (12394)
hermes
hermes vtuber status      # check the avatar subsystem came up
# 5. open http://127.0.0.1:12394/ in a browser, then chat in the hermes
#    terminal — the avatar speaks each reply.
```

Speech uses the host `text_to_speech` tool when present, else the Edge-TTS
fallback (Chinese voice by default; configurable via `tts.<provider>.voice` when
it is an Edge `*Neural` voice). The `/client-ws` gateway is compatible with both
websockets 12.x and 13–15.x.

### Preview the avatar (no hermes-agent needed)

```bash
python preview.py            # serves the front-end + /client-ws gateway, opens a browser
```

It opens http://127.0.0.1:12394/ and loads the reference model; typing drives a
synthetic lip-sync + expression demo (no real TTS audio). For the full voice +
avatar experience, enable the plugin and run `hermes vtuber start`. See
`omnilimb_face/frontend/README.md` for details and how to swap in your own model.

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

## License

Released under the **MIT License** — see [`LICENSE`](LICENSE).

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
