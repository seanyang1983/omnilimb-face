# omnilimb-face

![omnilimb-face](docs/assets/banner.svg)

Open-LLM-VTuber capabilities as a standalone, installable
[hermes-agent](https://github.com/NousResearch) plugin: hands-free voice
interaction (VAD + STT), real-time barge-in, and a Live2D avatar with
lip-sync and expression driving — all without modifying any hermes core file.

将 [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) 的核心能力
（语音免提交互、实时打断、Live2D 形象互动）作为独立可安装的 hermes-agent 插件交付，
仅通过 `register(ctx)` 扩展面集成，**不修改** hermes 核心文件。

> 📖 **详细文档见 [`docs/GUIDE.md`](docs/GUIDE.md)** —— 介绍 / 架构 / 安装 / 配置 /
> 命令(CLI·斜杠·工具)/ Live2D·Live3D 切换 / 实时打断(barge-in)/ 故障排查 / 开发测试。
>
> 🎭 **形象深度整合见 [`docs/AVATAR_INTEGRATION.md`](docs/AVATAR_INTEGRATION.md)** —— Live2D/Live3D
> 表情·动作·口型**怎么绑定**、**怎么导入新模型**（Cubism / VRM）、以及后续 2D+3D **深度融合开发**方向。

## How it works

The plugin reuses hermes' existing systems instead of carrying its own model
config: transcripts are injected via `ctx.inject_message`, the host agent's
normal turn produces the reply (using the user's active model, tools and
memory), and the reply text is intercepted through the `transform_llm_output` /
`post_llm_call` hooks to drive TTS and the Live2D avatar. Speech transcription
reuses the `stt` config section; speech synthesis reuses the `tts` section.

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

The **core** install deliberately excludes the voice/Live2D packages. When
those optional extras are missing the plugin still registers in a *degraded*
state and its tools stay visible in `hermes tools` (Requirement 12.1).

### Enabling

Discovered via the `hermes_agent.plugins` pip entry point, or by placing the
directory at `~/AppData/Local/hermes/plugins/omnilimb-face/`. Enable it by
adding `omnilimb-face` to `plugins.enabled` in `config.yaml`.

### Full product: chat in hermes, avatar speaks the agent's replies

To run the real integrated form — you chat in the `hermes` CLI and the browser
avatar speaks the agent's actual LLM replies (lip-sync + expressions):

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

The plugin reads the reply text from the `transform_llm_output` / `post_llm_call`
hooks (it never calls a model itself), so the avatar always speaks your
configured agent's real answer. Speech uses the host `text_to_speech` tool when
present, else the Edge-TTS fallback (Chinese voice by default; configurable via
`tts.<provider>.voice` when it is an Edge `*Neural` voice). The `/client-ws`
gateway is compatible with both websockets 12.x and 13–15.x, so it runs against
the host's websockets build.

### Preview the avatar (no hermes-agent needed)

To see the Live2D avatar render, lip-sync and switch expressions without a
running hermes session, launch the standalone preview from the plugin root:

```bash
python preview.py            # serves the front-end + /client-ws gateway, opens a browser
```

It opens http://127.0.0.1:12394/ and loads the reference model; typing drives a
synthetic lip-sync + expression demo (no real TTS audio). For the full voice +
avatar experience, enable the plugin and run `hermes vtuber start`. See
`frontend/README.md` for details and how to swap in your own model.

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

### Credits & third-party licensing / 致谢与第三方授权

This plugin **does not bundle or redistribute** any avatar models, the Live2D
Cubism Core, or any third-party front-end runtime. Everything below is loaded
from CDN at runtime only (with a dependency-free canvas fallback when offline).
Full details in [`NOTICE.md`](NOTICE.md).

- **Open-LLM-VTuber** — the `/client-ws` protocol here is an independent
  re-implementation *compatible* with
  [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)
  (MIT through v1.2.0 as of 2026-06; see `NOTICE.md` for the license-transition
  note). No upstream source is copied. Thanks to the project for the protocol
  design.
- **Live2D Cubism sample model** — the default avatar (`models/model_dict.json`)
  references Live2D Inc.'s official Cubism sample **"Mao"** (a commit-pinned CDN
  URL only). The Cubism Core runtime is proprietary to Live2D Inc. and is loaded
  from Live2D's CDN, never bundled.

  > This content uses sample data owned and copyrighted by Live2D Inc.
  > (the "Terms of Use for Live2D Cubism Sample Data" / Live2D Cubism SDK
  > license — https://www.live2d.com/en/).

  Live2D's official samples are free for general users and small-scale
  enterprises (latest annual sales under 10,000,000 JPY); larger entities are
  subject to additional Live2D terms. **For commercial / large-scale use, swap in
  a model you own or are licensed to use** by editing `models/model_dict.json`.
- **pixi.js / pixi-live2d-display / three.js / @pixiv/three-vrm** — all MIT,
  loaded from CDN.
- **Optional Python deps** — `edge-tts` (`[preview]`, **GPL-3.0**, never
  bundled) and `openWakeWord` (`[wakeword]`, Apache-2.0 library but its
  pre-trained models are **CC-BY-NC-SA-4.0 / NonCommercial**) need attention for
  commercial use. Full per-dependency breakdown in
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

本插件**不打包、不再分发**任何形象模型、Live2D Cubism Core 或第三方前端运行时,
全部仅在运行时经 CDN 加载(离线时回退到无依赖的 canvas 占位形象)。默认形象
仅通过 CDN URL 引用 Live2D 官方示例模型;使用 Live2D 原创角色须保留上述版权声明,
正式或规模化商用请替换为你自有或已获授权的模型。详见 [`NOTICE.md`](NOTICE.md)。
