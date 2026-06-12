# omnilimb-face front-end (reference example)

A self-contained client for the plugin's Open-LLM-VTuber compatible
`/client-ws` WebSocket protocol. It renders an avatar and drives it end-to-end
from the agent's replies:

- connects to the `/client-ws` gateway (with auto-reconnect);
- handles every server→client message (`full-text`, `set-model-and-conf`,
  `audio`, `control`, `error`);
- plays the synthesized reply audio (Web Audio API) and drives **lip-sync** from
  the `audio` message's `volumes` series (each element spans `slice_length` ms),
  synced to playback time;
- applies **expressions** from `actions.expressions` using the model `emotionMap`
  delivered in `set-model-and-conf`;
- shows `display_text.text` as a subtitle;
- lets you **type** to the agent (`text-input`) and **interrupt** it
  (`interrupt-signal`); on `control: interrupt` it stops playback immediately
  (barge-in).

By default the page renders a **real Live2D model** — the
[Mao](https://cdn.jsdelivr.net/gh/Live2D/CubismWebSamples/Samples/Resources/Mao/Mao.model3.json)
Cubism 4 sample from
[Live2D's official CubismWebSamples](https://github.com/Live2D/CubismWebSamples) —
loaded from CDN (pinned to a commit), with working lip-sync (`ParamA`) and
expression switching (the 8 `exp_01`–`exp_08` expressions, mapped via
`emotionMap`). The actual model is read from `models/model_dict.json`. The
proprietary Cubism Core runtime is **not** bundled here; it is loaded from
Live2D's CDN.

If the runtimes or the model can't be loaded (e.g. offline), the page falls back
to a dependency-free **canvas-drawn placeholder** so the whole protocol→render
loop still runs. The protocol layer is fully real in both cases — only the
renderer differs.

## Files

| File | Role |
|------|------|
| `index.html` | page + canvas + controls; loads the Live2D runtimes (CDN) + scripts |
| `styles.css` | styling |
| `avatar.js` | `Live2DAvatar` (real pixi-live2d-display renderer) + `CanvasAvatar` placeholder fallback |
| `protocol.js` | `VTuberProtocol` — `/client-ws` wire layer (connect/parse/send) |
| `app.js` | wiring + renderer selection + `AudioPlayer` (decode + ordered playback + lip-sync scheduling) |

## Quick preview (no hermes-agent needed)

To just *see* the avatar render, lip-sync and switch expressions without
standing up a full hermes-agent session, run the bundled preview launcher from
the plugin root:

```bash
python preview.py            # serves the page + gateway, opens the browser
python preview.py --no-browser
python preview.py --port 12393   # gateway port (page is served on port+1)
```

It serves this front-end (default http://127.0.0.1:12394/) and runs the
`/client-ws` gateway (default ws://127.0.0.1:12393/client-ws). On connect it
sends `set-model-and-conf` so the Mao reference model loads; typing in the page
drives a **synthetic** lip-sync + expression demo (no real TTS audio — the mouth
animates from a generated `volumes` series). Press Ctrl+C to stop.

### Real Chinese voice + real AI replies (optional)

The preview can also **speak** and **answer for real**:

```bash
python preview.py                 # echo replies, silent synthetic lip-sync
python preview.py                 # (edge-tts auto-used if installed -> real voice)
python preview.py --llm           # real answers from your hermes LLM + voice
python preview.py --llm --voice zh-CN-YunxiNeural
```

- **Voice**: if `edge-tts` is installed (`pip install -e ".[preview]"`), replies
  are synthesized to real Chinese speech (keyless Microsoft Edge voices, needs
  internet); the front-end computes lip-sync from the actual waveform. Without
  it, the avatar falls back to silent synthetic lip-sync.
- **`--llm`**: each typed message is answered by your **real hermes LLM**,
  reusing your configured model + credentials — no hermes config changes. The
  avatar speaks the agent's real reply.
  - **Warm worker (P1)**: a persistent chat worker (`hermes_chat_worker.py`) is
    spawned once (one-time ~3s cold start, pre-warmed on connect); subsequent
    replies have **no cold start** (~1-2s to start generating).
  - **Streaming (P2)**: the reply streams token-by-token and is spoken
    **sentence-by-sentence** (first audio in ~2-4s instead of waiting ~13s for
    the whole reply); sentences are synthesized while earlier ones play.
  - Falls back to a per-reply one-shot if the worker can't start. Override the
    hermes location with `--hermes-python` / `--hermes-dir`.

### Real-time voice interruption (barge-in)

The preview implements the same natural-conversation loop Open-LLM-VTuber uses:

- **🎤 免提 (hands-free)** button: captures the mic, runs an energy VAD, and
  **streams your utterance as 16 kHz PCM to the server** (`mic-audio-data` /
  `mic-audio-end`) for transcription. **Talking while the avatar is speaking
  interrupts it immediately** (stops playback + cancels the in-flight reply,
  mirroring OLV's `current_conversation_tasks[uid].cancel()`).
- **Interrupt** button: manual barge-in.
- Server-side, each reply is a **cancellable task**; an `interrupt-signal`
  cancels it (killing the in-flight LLM generation) so a barged-in reply never
  finishes or plays.

For the mic loop to produce replies the preview must run with **`--stt`** (local
faster-whisper). Voice flow: mic → VAD → PCM → server STT → warm LLM (stream) →
edge-tts → avatar, all interruptible.

> Tip: use **headphones**. Browser echo cancellation is imperfect on open
> speakers, so the mic can hear the avatar and interrupt/transcribe itself.

### Server-side STT (`--stt`)

```bash
python preview.py --llm --stt                 # full voice loop
python preview.py --llm --stt --stt-model small   # better accuracy, slower
```

Uses local **faster-whisper** (keyless, offline after first use). The model
downloads on first run via `HF_ENDPOINT` (defaults to the **hf-mirror.com**
mirror so it works where huggingface.co is blocked; override with
`--hf-endpoint`). Install it with `pip install faster-whisper`.

> This is a dev/preview tool only — it does not use the host LLM, TTS or
> microphone. For the real voice + avatar experience, install the plugin into
> hermes-agent and run `hermes vtuber start` (see below).

## How it's served / how it connects

When the plugin session starts, `FrontendStaticServer` serves this directory
over loopback HTTP on **`protocol.port + 1`** (default `12394`), and the
`/client-ws` gateway listens on **`protocol.port`** (default `12393`). So the
page served at `http://127.0.0.1:12394/` connects to
`ws://127.0.0.1:12393/client-ws` automatically.

Open the avatar window with the CLI:

```bash
hermes vtuber start      # starts the gateway + serves this front-end
# then open http://127.0.0.1:12394/ in a browser/webview
hermes vtuber status     # check state
hermes vtuber stop
```

### Gateway URL overrides (query params)

The page derives the gateway URL as "served port − 1" by default. Override it
when serving differently:

```
http://127.0.0.1:12394/?ws=ws://127.0.0.1:12393/client-ws
http://127.0.0.1:12394/?host=127.0.0.1&port=12393&path=/client-ws
```

> Browsers require a user gesture before audio can play — click or press a key
> once in the page so the first reply is audible.

## Using a different / your own Live2D model

The reference model loads from CDN out of the box. To use a different model,
edit the shipped model dictionary (`models/model_dict.json`, read by the
plugin's `Live2DModelInfo.load_model_info`, configurable via
`live2d.model_dict_path`):

1. Point the `default` entry's `url` at your Cubism `*.model3.json` (a remote
   URL, or a path served by the front-end — drop assets under `frontend/` and
   use a relative URL like `models/<your-model>/<your-model>.model3.json`).
2. Align `emotionMap` to your model's expression list. Each value is an **index**
   into the model's `Expressions` array; the agent's `actions.expressions`
   carry those indices, and `Live2DAvatar.setExpression(i)` applies them via
   pixi-live2d-display's `model.expression(i)`.

No front-end code changes are needed — `Live2DAvatar` loads whatever
`set-model-and-conf` delivers. It handles both Cubism 4/5 (`ParamMouthOpenY`)
and Cubism 2.1 (`PARAM_MOUTH_OPEN_Y`) lip-sync parameters.

### How rendering is selected

`app.js` picks the renderer at startup:

- if `PIXI` + `pixi-live2d-display` + a Cubism Core are present (loaded in
  `index.html`), it uses **`Live2DAvatar`** (real model);
- otherwise it falls back to the **`CanvasAvatar`** placeholder.

`Live2DAvatar` loads the model with `autoUpdate: false` and advances it in a
ticker callback so the lip-sync parameter is written **after** the motion
update each frame — otherwise the model's idle motion animates the mouth and
fights lip-sync. The protocol layer (`protocol.js`) and the audio/lip-sync
scheduling (`app.js`) are renderer-agnostic.

### Offline / production: self-host the runtimes

`index.html` loads the runtimes from CDN for convenience:

- `live2dcubismcore.min.js` (proprietary Cubism 4/5 Core, from Live2D's CDN),
- `live2d.min.js` (Cubism 2.1 Core, for older models),
- `pixi.js@6` (pixi-live2d-display targets PixiJS **v6**, not v7),
- `pixi-live2d-display`.

For an offline or production deployment, download these next to the page and
change the `<script src>` to the local copies. The Cubism Core is licensed by
Live2D Inc. — review their SDK license before redistributing it.

### Model dictionary

The plugin's `Live2DModelInfo.load_model_info` reads `models/model_dict.json`
(configurable via `live2d.model_dict_path`). The shipped example defines a
`default` model (Mao, from CDN) with an `emotionMap`. If the entry is missing
or unreadable it degrades to a placeholder (`is_placeholder=True`) and the
front-end shows the canvas avatar.
