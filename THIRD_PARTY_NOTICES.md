# Third-Party Notices

`omnilimb-face` is MIT-licensed (see `LICENSE`). It does **not bundle or
redistribute** any third-party code, runtime, or avatar model: front-end
libraries load from CDN at runtime, and Python dependencies are installed
separately by the user via `pip`. This document lists those components and their
licenses for transparency.

Licenses below reflect the versions in the declared dependency ranges at the
time of writing (2026-06). Always verify against the exact version you install.
This file is informational and is not legal advice.

## Python dependencies

| Package | Extra | License | Bundled? | Notes |
|---------|-------|---------|----------|-------|
| `websockets` | core | BSD-3-Clause | No (pip) | `/client-ws` gateway transport. |
| `sounddevice` | `[voice]` | MIT | No (pip) | Microphone capture (PortAudio binding). |
| `webrtcvad` | `[voice]` | MIT (wrapper) / BSD-3-Clause (WebRTC) | No (pip) | Voice-activity detection. |
| `numpy` | `[voice]` | BSD-3-Clause | No (pip) | Audio buffer math. |
| `openwakeword` | `[wakeword]` | Apache-2.0 (library) | No (pip) | **Pre-trained models are CC-BY-NC-SA-4.0 (NonCommercial)** — see below. |
| `starlette` | `[live2d]` | BSD-3-Clause | No (pip) | Front-end static serving. |
| `uvicorn` | `[live2d]` | BSD-3-Clause | No (pip) | ASGI server for static serving. |
| `edge-tts` | `[preview]` | **GPL-3.0** | No (pip) | Keyless TTS fallback — see below. |
| `faster-whisper` | `[preview]` | MIT | No (pip) | Local STT for the preview tool. |
| `cryptography` | `[preview]` | Apache-2.0 OR BSD-3-Clause | No (pip) | Self-signed cert for `preview.py --https`. |
| `pytest` | `[test]`/`[dev]` | MIT | No (pip) | Test runner. |
| `hypothesis` | `[test]`/`[dev]` | MPL-2.0 | No (pip) | Property-based tests (dev only). |

### Dependencies needing attention

- **edge-tts — GPL-3.0.** Optional (`[preview]` extra), invoked as a
  separately-installed dependency and never bundled, so it does not affect the
  license of this plugin's own MIT code. For a fully permissive default audio
  path, prefer a non-copyleft TTS backend and treat edge-tts as explicit opt-in.
- **openWakeWord pre-trained models — CC-BY-NC-SA-4.0 (NonCommercial).** The
  library itself is Apache-2.0, but the default pretrained models carry a
  NonCommercial restriction. The models are downloaded at runtime, not bundled.
  For commercial use, supply your own commercially-licensed wake-word models or
  disable the wake-word feature.

## Front-end libraries (loaded from CDN at runtime, not bundled)

Versions are pinned in `omnilimb_face/frontend/index.html`.

| Component | License | Notes |
|-----------|---------|-------|
| [pixi.js](https://github.com/pixijs/pixijs) | MIT | WebGL renderer (v6, for pixi-live2d-display). |
| [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) | MIT | Live2D model renderer. |
| [three.js](https://github.com/mrdoob/three.js) | MIT | WebGL engine for the Live3D (VRM) renderer. |
| [@pixiv/three-vrm](https://github.com/pixiv/three-vrm) | MIT | VRM loading for Live3D. |
| Live2D Cubism Core (`live2dcubismcore.min.js`) | Proprietary — © Live2D Inc. | Loaded from Live2D's official CDN. Review the Cubism SDK license before self-hosting/redistributing. |
| Cubism 2.1 Core (`live2d.min.js`, `dylanNew/live2d` mirror) | Proprietary — © Live2D Inc. | Legacy Cubism 2.1 runtime, CDN only. |

## Avatar sample data

| Asset | Source | Terms |
|-------|--------|-------|
| Live2D "Mao" sample model | [Live2D/CubismWebSamples](https://github.com/Live2D/CubismWebSamples) (commit-pinned CDN) | *Terms of Use for Live2D Cubism Sample Data* + *Free Material License Agreement* — https://www.live2d.com/en/ . Referenced via CDN, never redistributed. See `NOTICE.md` §2 for the required copyright notice and usage limits. |

## Protocol

The `/client-ws` WebSocket protocol is an independent, clean-room
re-implementation compatible with
[Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber). No
upstream source is copied or redistributed. See `NOTICE.md` §1.
