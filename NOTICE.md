# NOTICE — Third-Party Credits & Licensing / 致谢与第三方授权

`omnilimb-face` is released under the MIT License (see `LICENSE`). The plugin's
own source code (Python package and the self-authored front-end renderers) is
original work. This file documents third-party components the plugin
**references at runtime** — none of them are bundled or redistributed in this
repository. For a per-dependency license breakdown see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

本插件以 MIT 协议发布(见 `LICENSE`)。插件自身的源代码(Python 包与自写的前端
渲染器)均为原创。下列第三方组件**仅在运行时从 CDN 加载或由用户自行安装**,本仓库
**不打包、不再分发**它们的任何文件。逐项依赖授权见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

## 1. Open-LLM-VTuber (protocol design only)

The plugin implements a WebSocket protocol that is **compatible** with the
[Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) `/client-ws`
interface. The protocol layer here is an **independent, clean-room
re-implementation**; **no Open-LLM-VTuber source code is copied, derived from, or
redistributed**. With thanks to the Open-LLM-VTuber project for the protocol
design that inspired this work.

> Licensing note (as of 2026-06): Open-LLM-VTuber's backend is published under
> the MIT License through v1.2.0; the project has announced it may move to a
> custom "Open-LLM-VTuber License 1.0" around v1.3–v1.4. Because this plugin only
> re-implements the wire protocol (an interface) and copies no upstream source,
> that upstream license change does not apply to this repository. Always review
> the upstream license that matches the version you interoperate with.

本插件实现了与 Open-LLM-VTuber `/client-ws` 接口**兼容**的 WebSocket 协议,为
**独立 clean-room 重写**,**未拷贝、未衍生、未再分发**其任何源码。截至 2026-06,
其后端在 v1.2.0 及之前为 MIT,官方公告可能在 v1.3–v1.4 改用自定义协议;因本插件
仅重写线协议(接口),不复制上游源码,该变更不适用于本仓库。

## 2. Live2D Cubism — sample model & Core (runtime / CDN only)

The reference avatar in `models/model_dict.json` points (via a commit-pinned CDN
URL) to Live2D Inc.'s official **Cubism sample model "Mao"** from
[CubismWebSamples](https://github.com/Live2D/CubismWebSamples). The Cubism Core
runtime (`live2dcubismcore.min.js`) is proprietary to Live2D Inc. and is loaded
from Live2D's own CDN — both are **referenced at runtime, never bundled or
redistributed** by this repository.

"Mao" is a **Live2D Original Character**. Per the *Terms of Use for Live2D Cubism
Sample Data* and the *Free Material License Agreement*
(https://www.live2d.com/en/), the required copyright notice is:

> This content uses sample data owned and copyrighted by Live2D Inc. The sample
> data are utilized in accordance with terms and conditions set by Live2D Inc.
> This content itself is created at the author's sole discretion.

**Usage notes / 使用须知:**
- Live2D Original Characters may be used **free of charge for commercial and
  non-commercial purposes by General Users and Small-Scale Enterprises** (latest
  annual sales under 10,000,000 JPY). Entities at or above that threshold are
  limited to internal / supervision use and need Live2D's prior written approval
  for promotional use (see §2.1.3 of the Free Material License Agreement).
- The Live2D sample data are **referenced via CDN, never redistributed** by this
  repo. Do **not** commit the model files (`*.moc3`, `*.model3.json`, textures)
  or the Cubism Core into the repository — that would constitute redistribution,
  which the Live2D terms prohibit (§4.1.1).
- Live2D's terms (§4.1.7) also prohibit using the sample data to produce sexual,
  violent, discriminatory, or political/religious derivative works, and prohibit
  combining the output with middleware that competes with Live2D's.
- For commercial or large-scale deployments, **replace the default with a model
  you own or are licensed to use** by editing `models/model_dict.json`, and
  review the current Live2D Cubism SDK / sample-data terms yourself.

Live2D 官方原创角色对**一般用户与小规模事业者**(最近年销售额低于 1000 万日元)
**可商用可非商用**且免费;达到/超过门槛者仅限内部/监修用途,宣传须 Live2D 书面
授权。示例数据**仅经 CDN 引用,不再分发**;请勿将模型文件或 Cubism Core 提交进
仓库(构成再分发,Live2D 条款禁止)。正式或规模化商用请替换为你自有或已获授权
的模型。

## 3. Front-end runtime libraries (CDN only, all MIT)

Loaded at runtime from CDN (versions pinned in `frontend/index.html`), not
bundled:

- [pixi.js](https://github.com/pixijs/pixijs) — MIT
- [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) — MIT
- [three.js](https://github.com/mrdoob/three.js) — MIT
- [@pixiv/three-vrm](https://github.com/pixiv/three-vrm) — MIT
- Cubism 2.1 Core (`live2d.min.js`, via the `dylanNew/live2d` mirror) — Live2D
  proprietary runtime; loaded from CDN for legacy Cubism 2.1 models only.

If any CDN asset fails to load (e.g. offline), the front-end falls back to a
dependency-free canvas placeholder, so no third-party file is required for the
plugin to run.

### Sample VRM (website demo only)

The hosted website demo (`app.html?demo=1`) loads the official three-vrm sample
model **`VRM1_Constraint_Twist_Sample.vrm`** from the
[pixiv/three-vrm](https://github.com/pixiv/three-vrm) examples (via CDN) purely
to demonstrate the Live3D renderer. It is **not bundled or redistributed** by
this repository and is **not** the plugin's default model — the plugin's Live3D
VRM source is supplied at runtime by the host. Review the model's embedded VRM
license metadata before using it elsewhere.

## 4. Optional Python dependencies (user-installed, NOT bundled)

These are declared as **optional extras** in `pyproject.toml` and are installed
by the user via `pip`; the plugin does **not** redistribute them. Two carry
licensing terms worth highlighting:

- **edge-tts** (`[preview]` extra) — **GPL-3.0**. Used as a keyless TTS fallback
  in the standalone preview / when the host has no `text_to_speech` tool. It is
  invoked as a separately-installed dependency and is **never bundled**, so the
  plugin's own MIT-licensed code is unaffected. If you need a fully permissive
  default audio path, install a non-copyleft TTS backend and treat edge-tts as
  an explicit opt-in.
- **openWakeWord** (`[wakeword]` extra) — the library is **Apache-2.0**, but its
  **pre-trained wake-word models are CC-BY-NC-SA-4.0 (NonCommercial)** due to
  restrictive licensing in their training data. The models are downloaded by
  openWakeWord at runtime (not bundled here). **For commercial use, supply your
  own commercially-licensed wake-word models** or disable the wake-word feature.

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for the full dependency
list and their licenses.

---

*This NOTICE is provided for attribution and transparency and does not
constitute legal advice. Review the upstream licenses for authoritative terms.*
