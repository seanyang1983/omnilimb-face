/*
 * omnilimb-face reference front-end — application wiring.
 *
 * Glues the protocol client (protocol.js) to the avatar renderer (avatar.js)
 * and the page UI. Owns the AudioPlayer that:
 *   * decodes each `audio` message's base64 WAV via the Web Audio API,
 *   * plays segments strictly IN ORDER (a queue; the backend already sends
 *     them in sentence order),
 *   * drives LIP-SYNC by sampling the `volumes` array at playback time
 *     (each element spans `slice_length` ms) into avatar.setMouthOpen(),
 *   * applies the segment's `actions.expressions` (mapped to emotion names via
 *     the model emotionMap from set-model-and-conf),
 *   * shows `display_text.text` as a subtitle,
 *   * on barge-in (control:interrupt) STOPS playback immediately and clears the
 *     queue, and on normal end notifies the backend (frontend-playback-complete).
 */

(function () {
  "use strict";

  // --- DOM ---------------------------------------------------------------
  const els = {
    canvas: document.getElementById("avatar"),
    status: document.getElementById("conn-status"),
    subtitle: document.getElementById("subtitle"),
    expressionLabel: document.getElementById("expression-label"),
    form: document.getElementById("text-form"),
    input: document.getElementById("text-input"),
    interruptBtn: document.getElementById("interrupt-btn"),
    handsfreeBtn: document.getElementById("handsfree-btn"),
    pttBtn: document.getElementById("ptt-btn"),
    sttModel: document.getElementById("stt-model"),
    log: document.getElementById("log"),
    // settings modal
    settingsBtn: document.getElementById("settings-btn"),
    modal: document.getElementById("settings-modal"),
    ttsVoice: document.getElementById("tts-voice"),
    ttsRate: document.getElementById("tts-rate"),
    ttsRateVal: document.getElementById("tts-rate-val"),
    vadThreshold: document.getElementById("vad-threshold"),
    vadVal: document.getElementById("vad-val"),
    autoMic: document.getElementById("auto-mic"),
    avatarScale: document.getElementById("avatar-scale"),
    scaleVal: document.getElementById("scale-val"),
    bgMode: document.getElementById("bg-mode"),
    rendererSelect: document.getElementById("renderer-select"),
    showSubtitle: document.getElementById("show-subtitle"),
    showLog: document.getElementById("show-log"),
    bargeinToggle: document.getElementById("bargein-toggle"),
    statusBody: document.getElementById("status-body"),
    personaText: document.getElementById("persona-text"),
    personaSave: document.getElementById("persona-save"),
  };

  function log(line) {
    const ts = new Date().toLocaleTimeString();
    els.log.textContent = `[${ts}] ${line}\n` + els.log.textContent;
  }

  // --- persisted client settings (localStorage) -------------------------
  const SETTINGS_KEY = "omnilimb-face-settings";
  const SETTINGS_DEFAULTS = {
    vadThreshold: 0.015,
    autoMic: false,
    avatarScale: 1.0,
    bgMode: "default",
    showSubtitle: true,
    showLog: true,
    bargeIn: true,
    // --- switchable-avatar-renderers (additive; existing fields unchanged) ---
    // Selected renderer type: "Live2D" | "Live3D"
    // (default Live2D — Requirement 1.6). Normalized on load via
    // RendererManager.resolveRenderer (Requirement 1.9/1.10).
    renderer: "Live2D",
    vrmUrl: "", // Live3D VRM model URL (task 5)
    designTheme: "dark", // Design_System theme: "dark" | "light" | "auto" (task 10)
    layout: "cinema", // stage 版式：cinema(剧场·默认) | solo(纯形象)
  };
  function loadSettings() {
    let s = {};
    try {
      s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    } catch (_e) {
      s = {};
    }
    return Object.assign({}, SETTINGS_DEFAULTS, s);
  }
  const SETTINGS = loadSettings();
  function saveSettings() {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(SETTINGS));
    } catch (_e) {
      /* storage may be unavailable */
    }
  }

  // --- gateway URL resolution -------------------------------------------
  // The static-asset server runs on (protocol.port + 1); the /client-ws gateway
  // on protocol.port. So when this page is served from :PORT, the gateway is at
  // :(PORT-1). Both are overridable via query params: ?ws=ws://host:port/path,
  // or ?host=&port=&path=.
  function resolveGatewayUrl() {
    const q = new URLSearchParams(location.search);
    if (q.get("ws")) return q.get("ws");
    // Single-port mode: the page and /client-ws share ONE origin (the server
    // tagged the page with window.__VTUBER_SINGLE_PORT__). Connect to our OWN
    // host:port so there is only one cert/origin — works over HTTPS and through
    // tunnels (the gateway is reached at the same host:port as the page).
    if (window.__VTUBER_SINGLE_PORT__) {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const path = q.get("path") || "/client-ws";
      return `${scheme}://${location.host}${path}`;
    }
    const host = q.get("host") || location.hostname || "127.0.0.1";
    let port = q.get("port");
    if (!port) {
      const served = Number(location.port);
      port = served ? String(served - 1) : "12393";
    }
    const path = q.get("path") || "/client-ws";
    // Under HTTPS the page must use a secure WebSocket (wss://), otherwise the
    // browser blocks the mixed-content ws:// connection. The preview's --https
    // serves the gateway with the same cert so wss works end to end.
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${host}:${port}${path}`;
  }

  // --- avatar ------------------------------------------------------------
  // Prefer the REAL Live2D renderer when the runtimes loaded (index.html loads
  // them from CDN); otherwise fall back to the dependency-free canvas
  // placeholder so the protocol->render loop still runs (e.g. offline).
  function createAvatar(canvas) {
    const haveCore = !!(window.Live2DCubismCore || window.Live2D);
    const havePixi = !!(window.PIXI && window.PIXI.live2d && window.PIXI.live2d.Live2DModel);
    if (havePixi && haveCore) {
      try {
        const a = new Live2DAvatar(canvas);
        log("renderer: Live2D (pixi-live2d-display)");
        return a;
      } catch (e) {
        log("Live2D init failed; using canvas placeholder: " + e);
      }
    } else {
      log("Live2D runtime not present; using canvas placeholder.");
    }
    return new CanvasAvatar(canvas);
  }

  // Wrap the concrete renderer in a RendererManager. The manager holds the
  // "currently active renderer" and forwards every Renderer_Interface call to
  // it WITHOUT changing semantics — Live2DAvatar / CanvasAvatar behavior is
  // byte-for-byte identical (Requirement 4 regression protection). The manager
  // owns runtime switching (switchTo/awaitReady/destroy) and signal routing
  // (task 2.1); the fallback chain (degrade) and persistence land in tasks
  // 2.2/2.3. Here we wire the shared canvas, a user-visible notice channel, and
  // register the Live2D construction factory so switching is pluggable.
  //
  // The Live2D selection logic lives in createAvatar() (Live2DAvatar when the
  // PIXI + pixi-live2d-display + Cubism Core runtimes are present, else the
  // CanvasAvatar fallback with a notice). The active type for that path is
  // "Live2D" regardless of which concrete implementation createAvatar picked.
  // Live3D constructs from its window global (window.Live3DRenderer); until the
  // three.js stack is loaded, switching to it is a construction failure that
  // rolls back to the pre-switch renderer.
  const rendererManager = new RendererManager({
    canvas: els.canvas,
    // Surface switch-failed / rollback notices through the existing event log.
    notify: (message) => log(message),
  });
  // Register a construction factory for EACH renderer type so runtime switching
  // (switchTo) is pluggable end-to-end with no orphaned code (Requirement 4.1):
  //   * Live2D       — reuses createAvatar() (Live2DAvatar when the PIXI +
  //                    pixi-live2d-display + Cubism Core runtimes are present,
  //                    else the CanvasAvatar fallback). Behavior UNCHANGED (R4).
  //   * Live3D       — three.js + three-vrm from window.Live3DRenderer; its VRM
  //                    source (vrm_url) arrives via the additive set-model-and-conf
  //                    extras and is applied in loadModelForActiveRenderer().
  rendererManager.register("Live2D", (ctx) => createAvatar(ctx.canvas));
  rendererManager.register("Live3D", (ctx) => new Live3DRenderer(ctx.canvas, ctx));
  rendererManager.adopt(createAvatar(els.canvas), "Live2D");
  let avatar = rendererManager;

  // --- 伴随「声波粒子球」(Orb) — 常驻在主形象旁边的独立可视化 -----------------
  // Orb 不参与渲染器切换（绝不覆盖 Live2D/Live3D），而是作为一个独立的伴随可视化常驻
  // 在主形象旁边的 #orb-canvas 上：用与主形象相同的 lip-sync 口型值与 Agent_State 驱动
  // ——说话时粒子沿径向律动、状态变化时变色换角标。依赖 three.js（其全局在 ES 模块加载
  // 后才就绪，故经 whenThreeReady 延迟创建，见文件末尾 initVoiceOrb）；three.js / WebGL
  // 不可用时静默跳过，主形象不受影响。`driveMouth`/`driveResetMouth` 把口型信号同时
  // 扇出到主渲染器与伴随 Orb（声明在此以便 AudioPlayer 在运行时引用）。
  let voiceOrb = null;
  function driveMouth(v) {
    avatar.setMouthOpen(v);
    if (voiceOrb) voiceOrb.setMouthOpen(v);
  }
  function driveResetMouth() {
    avatar.resetMouth();
    if (voiceOrb) voiceOrb.resetMouth();
  }
  // Renderer-specific model sources captured from the additive set-model-and-conf
  // extras (protocol.js): the Live3D VRM URL (R5) and the latest emotion map used
  // when (re)loading a model after a renderer switch.
  let _lastVrmUrl = "";
  let _lastEmotionMap = {};

  // Startup renderer recovery (Requirement 1.9/1.10). Read the persisted
  // renderer from SETTINGS, normalized to one of the valid types — a
  // missing/invalid value resolves to "Live2D". Live2D is already adopted
  // above; if a *different* valid renderer was persisted, switch to it now.
  // Live3D needs three.js; until its global is loaded, switchTo() construction
  // fails and rolls back to the adopted Live2D — still a safe, valid active
  // renderer (consistent with R1.10).
  // three.js is now loaded as ES modules (see index.html), which execute AFTER
  // these classic scripts. So window.THREE may not exist yet when app.js runs.
  // whenThreeReady() runs `cb` immediately if the three stack is already present,
  // else once the module shim dispatches its `three-ready` event. Used to defer
  // activating a persisted Live3D renderer until three.js is available
  // (Live2D does not need three.js).
  function whenThreeReady(cb) {
    if (window.THREE || window.__threeReady) {
      cb();
      return;
    }
    window.addEventListener("three-ready", function once() {
      window.removeEventListener("three-ready", once);
      cb();
    });
  }

  const _startupRenderer = RendererManager.resolveRenderer(SETTINGS.renderer);
  function _doStartupSwitch() {
    Promise.resolve(rendererManager.switchTo(_startupRenderer)).then((res) => {
      if (res && res.ok && res.switched) {
        log("startup renderer restored: " + _startupRenderer);
        loadModelForActiveRenderer(
          _lastEmotionMap,
          (_lastModelInfo && _lastModelInfo.name) || "",
          _lastModelInfo
        );
      }
      syncRendererSelect();
    });
  }
  if (_startupRenderer !== "Live2D") {
    // Live3D needs three.js — wait for the module shim before switching.
    if (_startupRenderer === "Live3D") {
      whenThreeReady(_doStartupSwitch);
    } else {
      _doStartupSwitch();
    }
  }

  // Reverse map: expression INDEX -> emotion NAME, from the model emotionMap
  // delivered in set-model-and-conf, so expression labels are human-readable.
  let indexToEmotion = {};

  function applyExpressions(actions) {
    if (!actions || !Array.isArray(actions.expressions)) return;
    if (actions.expressions.length === 0) return;
    // The placeholder shows the PRIMARY (first) expression of the segment.
    const idx = actions.expressions[0];
    avatar.setExpression(idx, indexToEmotion[idx]);
    els.expressionLabel.textContent =
      "expression: " + (indexToEmotion[idx] || `#${idx}`);
  }

  // Load the correct model SOURCE into whatever renderer is currently active
  // (Requirement 4.1 end-to-end wiring; R5 Live3D):
  //   * Live2D / CanvasAvatar — UNCHANGED baseline: fetch the Cubism model from
  //     model_info.url (Requirement 4 regression — same logs, same flow).
  //   * Live3D — load the VRM from the additive vrm_url extra (set-model-and-conf),
  //     not the Cubism url; on success apply the neutral expression.
  // This is invoked from onSetModel and right after a runtime renderer switch so
  // the newly-activated renderer always shows the right model.
  function loadModelForActiveRenderer(emap, name, modelInfo) {
    emap = emap || {};
    const type = rendererManager.type;

    if (type === "Live3D") {
      const url = _lastVrmUrl || SETTINGS.vrmUrl || "";
      if (url && avatar.setModel) {
        log("loading VRM model: " + url);
        Promise.resolve(avatar.setModel(url))
          .then((res) => {
            if (res && res.ok === false) {
              log("VRM model load failed: " + (res.error || "unknown"));
              return;
            }
            log("VRM model loaded");
            if ("neutral" in emap) avatar.setExpression(emap.neutral, "neutral");
          })
          .catch((err) => log("VRM model load failed: " + err));
      } else if ("neutral" in emap) {
        avatar.setExpression(emap.neutral, "neutral");
      }
      return;
    }

    // Live2D / CanvasAvatar — preserved baseline behavior (Requirement 4).
    if (modelInfo && !modelInfo.is_placeholder && modelInfo.url && avatar.setModel) {
      log("loading model: " + modelInfo.url);
      Promise.resolve(avatar.setModel(modelInfo.url))
        .then(() => {
          log("model loaded: " + (name || modelInfo.name || "(unnamed)"));
          // Start at the neutral/default expression once the model is ready.
          if ("neutral" in emap) avatar.setExpression(emap.neutral, "neutral");
        })
        .catch((err) => log("model load failed: " + err));
    } else if ("neutral" in emap) {
      // Placeholder renderer: apply neutral immediately.
      avatar.setExpression(emap.neutral, "neutral");
    }
  }

  // --- audio player + lip-sync ------------------------------------------
  class AudioPlayer {
    constructor() {
      this.ctx = null;
      this.queue = [];
      this.current = null; // { source, raf, stopLipSync }
      this.playing = false;
    }

    _ensureCtx() {
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        this.ctx = new AC();
      }
      if (this.ctx.state === "suspended") this.ctx.resume();
      return this.ctx;
    }

    /** Queue an `audio` protocol message for in-order playback. */
    enqueue(msg) {
      this.queue.push(msg);
      if (!this.playing) this._next();
    }

    async _next() {
      const msg = this.queue.shift();
      if (!msg) {
        this.playing = false;
        // Playback queue drained: the agent has finished speaking (Req 13.6).
        applyAgentState("idle");
        return;
      }
      this.playing = true;
      // A segment is starting to play: the agent is speaking (Req 13.6/12.5).
      applyAgentState("speaking");

      // Subtitle + expression apply as the segment begins.
      const dt = msg.display_text || {};
      if (typeof dt.text === "string") {
        els.subtitle.textContent = dt.text;
        log("形象: " + dt.text); // show the agent's spoken reply in the log
      }
      applyExpressions(msg.actions);
      // Play a short body motion so the avatar visibly reacts to each reply
      // (no-op on the canvas placeholder; uses the model's "Tap" motion group).
      if (avatar.playMotion) avatar.playMotion("Tap");

      const volumes = Array.isArray(msg.volumes) ? msg.volumes : [];
      const sliceMs = msg.slice_length > 0 ? msg.slice_length : 20;

      if (!msg.audio) {
        // Expression / lip-sync-only frame (no audio): run a short timed
        // lip-sync from `volumes`, then advance.
        await this._lipSyncWithoutAudio(volumes, sliceMs);
        this._finishSegment();
        return;
      }

      let buffer;
      try {
        const ctx = this._ensureCtx();
        const bytes = base64ToBytes(msg.audio);
        buffer = await ctx.decodeAudioData(bytes.buffer);
      } catch (err) {
        log("audio decode failed; skipping segment: " + err);
        this._finishSegment();
        return;
      }

      // When the backend ships real audio but no precomputed `volumes` (e.g. a
      // TTS that returns only an audio file), derive the lip-sync envelope from
      // the decoded waveform itself (RMS per slice), so the mouth tracks the
      // actual speech.
      const lipVolumes =
        volumes.length > 0 ? volumes : volumesFromBuffer(buffer, sliceMs);

      const ctx = this.ctx;
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);

      const startTime = ctx.currentTime;
      let raf = null;
      const tick = () => {
        const elapsedMs = (ctx.currentTime - startTime) * 1000;
        const idx = Math.floor(elapsedMs / sliceMs);
        const v = idx >= 0 && idx < lipVolumes.length ? lipVolumes[idx] : 0;
        driveMouth(v);
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);

      this.current = {
        source,
        stopLipSync: () => {
          if (raf) cancelAnimationFrame(raf);
          driveResetMouth();
        },
      };

      source.onended = () => this._finishSegment(/*notify=*/ true);
      source.start();
    }

    _lipSyncWithoutAudio(volumes, sliceMs) {
      return new Promise((resolve) => {
        if (volumes.length === 0) {
          resolve();
          return;
        }
        const start = performance.now();
        const totalMs = volumes.length * sliceMs;
        let raf = null;
        const tick = (now) => {
          const elapsed = now - start;
          if (elapsed >= totalMs) {
            driveResetMouth();
            resolve();
            return;
          }
          const idx = Math.floor(elapsed / sliceMs);
          driveMouth(volumes[idx] || 0);
          raf = requestAnimationFrame(tick);
        };
        this.current = {
          source: null,
          stopLipSync: () => {
            if (raf) cancelAnimationFrame(raf);
            driveResetMouth();
            resolve();
          },
        };
        raf = requestAnimationFrame(tick);
      });
    }

    _finishSegment(notify) {
      if (this.current && this.current.stopLipSync) this.current.stopLipSync();
      this.current = null;
      driveResetMouth();
      if (notify && app.proto) app.proto.sendPlaybackComplete();
      this._next();
    }

    /** Barge-in: stop the current segment, drop the queue, close the mouth. */
    stop() {
      this.queue = [];
      if (this.current) {
        try {
          if (this.current.source) this.current.source.onended = null;
          if (this.current.source) this.current.source.stop();
        } catch (_e) {
          /* already stopped */
        }
        if (this.current.stopLipSync) this.current.stopLipSync();
        this.current = null;
      }
      driveResetMouth();
      this.playing = false;
    }
  }

  function base64ToBytes(b64) {
    const bin = atob(b64);
    const len = bin.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  // Derive a lip-sync envelope from a decoded AudioBuffer: RMS amplitude per
  // `sliceMs` window, normalized to [0,1]. Used when the backend sends real
  // audio without a precomputed `volumes` series.
  function volumesFromBuffer(buffer, sliceMs) {
    try {
      const data = buffer.getChannelData(0);
      const sr = buffer.sampleRate || 48000;
      const per = Math.max(1, Math.floor((sr * sliceMs) / 1000));
      const out = [];
      let peak = 1e-4;
      for (let i = 0; i < data.length; i += per) {
        let sum = 0;
        let n = 0;
        for (let j = i; j < i + per && j < data.length; j++) {
          sum += data[j] * data[j];
          n++;
        }
        const rms = Math.sqrt(sum / Math.max(1, n));
        if (rms > peak) peak = rms;
        out.push(rms);
      }
      // Normalize with a little headroom so the mouth opens convincingly.
      return out.map((v) => Math.max(0, Math.min(1, (v / peak) * 1.1)));
    } catch (_e) {
      return [];
    }
  }

  // --- application ------------------------------------------------------
  const app = {
    proto: null,
    audio: new AudioPlayer(),
  };

  // --- status panel (Telemetry HUD) — Requirement 13 -------------------
  // The StatusPanel renders the runtime Telemetry grid into the 状态 tab's
  // existing #status-body, sourcing values ONLY from existing /client-ws fields
  // or the additive fields per Requirement 3 (renderer type, model name+url,
  // stt_model/tts_voice, connection status, ping/pong latency, agent-state).
  // Network_Latency refresh is
  // driven by the panel's OWN setInterval (≤5000ms, strictly enforced) decoupled
  // from the AudioPlayer / LLM work — the tick sends a ping (only when the socket
  // is open, so it never spams the log while down) and re-renders so the value
  // refreshes on a fixed cadence (Requirement 13.5).
  const statusPanel = new StatusPanel({
    container: els.statusBody,
    sendPing: () => {
      if (app.proto && app.proto.ws && app.proto.ws.readyState === WebSocket.OPEN) {
        app.proto.sendPing(Date.now());
      }
    },
  });
  // Whether the socket has ever been open — distinguishes the initial
  // "connecting" from a post-drop "reconnecting" for Connection_Status.
  let _everConnected = false;

  // --- Agent_State derivation (Requirement 13.6 / 12.5) -----------------
  // Single sink for the agent's lifecycle phase. It is driven BOTH by local app
  // state (idle / listening while the mic captures speech / thinking while the
  // LLM+TTS turn is pending / speaking while audio plays) AND by an optional
  // server-sent `agent-state` event — both call this so the active renderer
  // (Live2D/Live3D no-op via the manager's typeof guard)
  // and the Telemetry panel always agree. Invalid values are ignored.
  let _agentState = "idle";
  function applyAgentState(state) {
    if (
      state !== "idle" &&
      state !== "listening" &&
      state !== "thinking" &&
      state !== "speaking"
    ) {
      return; // ignore unknown states; keep current (never throws)
    }
    _agentState = state;
    if (avatar && typeof avatar.setAgentState === "function") {
      avatar.setAgentState(state);
    }
    if (voiceOrb && typeof voiceOrb.setAgentState === "function") {
      voiceOrb.setAgentState(state);
    }
    statusPanel.setAgentState(state);
  }

  // A centered on-stage overlay that surfaces the /client-ws connection state
  // prominently (instead of only a blank canvas + a buried log line). The most
  // common failure on localhost is a proxy/VPN stripping the WebSocket Upgrade
  // header (the gateway then rejects the handshake with HTTP 426), so the
  // failure copy points the user straight at that fix.
  let _connOverlay = null;
  function connOverlay() {
    if (_connOverlay) return _connOverlay;
    const host = document.querySelector(".stage") || document.body;
    const el = document.createElement("div");
    el.className = "conn-overlay";
    const s = el.style;
    s.position = "absolute";
    s.left = "50%";
    s.top = "50%";
    s.transform = "translate(-50%, -50%)";
    s.maxWidth = "32rem";
    s.padding = "16px 20px";
    s.borderRadius = "12px";
    s.background = "rgba(0,0,0,0.7)";
    s.border = "1px solid rgba(255,255,255,0.15)";
    s.color = "#e6e8ef";
    s.fontSize = "14px";
    s.lineHeight = "1.6";
    s.textAlign = "center";
    s.zIndex = "50";
    s.pointerEvents = "none";
    if (host && getComputedStyle(host).position === "static") {
      host.style.position = "relative";
    }
    (host || document.body).appendChild(el);
    _connOverlay = el;
    return el;
  }
  function showConnNotice(kind) {
    const el = connOverlay();
    if (kind === "connected") {
      el.style.display = "none";
      return;
    }
    el.style.display = "block";
    if (kind === "connecting") {
      el.innerHTML = "正在连接网关…<br><span style='opacity:.7'>ws://127.0.0.1:12393/client-ws</span>";
    } else {
      // disconnected / reconnecting — the actionable proxy-bypass hint.
      el.innerHTML =
        "⚠ 连不上对话网关<br>" +
        "<span style='opacity:.75;font-size:13px'>ws://127.0.0.1:12393/client-ws</span><br><br>" +
        "多半是<strong>代理 / VPN / 加速器</strong>拦截了本地 WebSocket（服务器回 426）。<br>" +
        "请把 <strong>127.0.0.1</strong> 和 <strong>localhost</strong> 加入代理的<strong>「绕过 / 直连」</strong>，" +
        "或临时关闭代理后按 <strong>Ctrl+Shift+R</strong> 刷新。";
    }
  }

  function setStatus(s) {
    els.status.textContent = s;
    els.status.className = "status status--" + s;
    // Map the wire status to a Connection_Status enum and reflect it in the
    // panel immediately (Requirement 13.4/13.8). After the first successful
    // open, a subsequent "connecting"/"closed" is a reconnect attempt.
    let conn;
    if (s === "open") {
      _everConnected = true;
      conn = "connected";
      showConnNotice("connected");
    } else if (s === "connecting") {
      conn = _everConnected ? "reconnecting" : "connecting";
      showConnNotice(_everConnected ? "disconnected" : "connecting");
    } else if (s === "closed" || s === "error") {
      conn = _everConnected ? "reconnecting" : "disconnected";
      showConnNotice("disconnected");
    } else {
      conn = s;
    }
    statusPanel.setConnection(conn);
  }

  const proto = new VTuberProtocol(resolveGatewayUrl(), {
    onStatus: setStatus,
    onLog: log,
    onFullText: (text) => {
      if (!text) return;
      els.subtitle.textContent = text;
      log("full-text: " + text);
    },
    onSetModel: (modelInfo, _msg, extras) => {
      const name = modelInfo.name || "(unnamed)";
      const emap = modelInfo.emotionMap || {};
      indexToEmotion = {};
      for (const [emotion, index] of Object.entries(emap)) {
        indexToEmotion[index] = emotion;
      }
      const note = modelInfo.is_placeholder ? " (placeholder)" : "";
      log(
        `set-model: ${name}${note}; emotions=[${Object.keys(emap).join(", ")}]`
      );
      // Populate the STT model selector from the server's advertised options.
      populateSttSelect(modelInfo);
      populateTtsSelect(modelInfo);
      _lastModelInfo = modelInfo;
      _lastEmotionMap = emap;
      // Capture the additive vrm_url extra (Live3D VRM source, Requirement 5) so
      // loadModelForActiveRenderer loads the VRM (not the Cubism url) when Live3D
      // is the active renderer.
      if (extras && typeof extras.vrmUrl === "string" && extras.vrmUrl) {
        _lastVrmUrl = extras.vrmUrl;
      }
      // Feed the Telemetry panel from existing fields + additive extras (R13).
      renderStatus();
      if (els.ttsRate) {
        const m = (modelInfo.tts_rate || "+0%").match(/-?\d+/);
        const pct = m ? parseInt(m[0], 10) : 0;
        els.ttsRate.value = pct;
        els.ttsRateVal.textContent = (pct >= 0 ? "+" : "") + pct + "%";
      }
      if (!els.modal.hidden) renderStatus();
      // Fill the persona editor with the avatar's current persona (once).
      if (els.personaText && typeof modelInfo.persona === "string" && !els.personaText.value) {
        els.personaText.value = modelInfo.persona;
      }
      // Load the real model into whatever renderer is active. The active
      // renderer determines the model SOURCE: Live2D/Canvas use model_info.url
      // (UNCHANGED baseline, Requirement 4); Live3D uses the additive vrm_url.
      loadModelForActiveRenderer(emap, name, modelInfo);
    },
    onAudio: (msg) => {
      app.audio.enqueue(msg);
    },
    onControl: (text) => {
      log("control: " + text);
      if (text === "interrupt") {
        app.audio.stop(); // barge-in: stop playback immediately.
      } else if (text === "mouth-reset") {
        driveResetMouth();
      } else if (text === "start-mic") {
        // The placeholder UI does not capture the mic (the plugin captures
        // server-side); this is informational. A real front-end mic client
        // would begin streaming mic-audio-data here.
      }
    },
    onError: (code, reason) => log(`error [${code}]: ${reason}`),
    // Additive Agent_State event (Requirement 3.6/13.6): a SERVER-sent agent
    // state routes through the same sink as the locally-derived state, driving
    // the active renderer (Live2D/Live3D no-op via the manager guard)
    // and the Telemetry panel consistently.
    onAgentState: (state) => applyAgentState(state),
    // Additive pong (Requirement 13.5): compute round-trip latency from the
    // echoed timestamp and refresh the panel's Network_Latency value.
    onPong: (t) => statusPanel.recordPong(t),
  });
  app.proto = proto;

  // --- UI events --------------------------------------------------------
  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = els.input.value.trim();
    if (!text) return;
    if (proto.sendTextInput(text)) {
      log("你: " + text);
      els.input.value = "";
      // A turn is pending the LLM + TTS response: the agent is thinking (R13.6).
      applyAgentState("thinking");
    }
  });

  els.interruptBtn.addEventListener("click", () => {
    app.audio.stop(); // stop locally for snappy feedback…
    proto.sendInterrupt(0); // …and tell the backend to abort the turn.
    log("> interrupt");
  });

  // --- hands-free voice: capture mic PCM -> server STT (faster-whisper) ---
  // An energy VAD segments utterances. Talking over the avatar barges in
  // immediately (stop playback + interrupt-signal). The captured speech is
  // downsampled to 16 kHz int16 PCM and sent (mic-audio-data + mic-audio-end)
  // for server-side transcription — the natural-conversation loop Open-LLM-
  // VTuber implements with VAD. Works in any browser with getUserMedia (the
  // page is on localhost, a secure context). Tip: use HEADPHONES — otherwise
  // the mic hears the avatar and can interrupt/transcribe itself.
  function downsamplePCM16(frames, srcRate, dstRate) {
    let total = 0;
    for (const f of frames) total += f.length;
    const merged = new Float32Array(total);
    let o = 0;
    for (const f of frames) {
      merged.set(f, o);
      o += f.length;
    }
    const ratio = srcRate / dstRate;
    const outLen = Math.max(0, Math.floor(merged.length / ratio));
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const v = merged[Math.floor(i * ratio)] || 0;
      out[i] = Math.max(-1, Math.min(1, v)) * 32767;
    }
    return out;
  }

  function int16ToBase64(int16) {
    const bytes = new Uint8Array(int16.buffer);
    let bin = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(bin);
  }

  class MicCapture {
    constructor(handlers) {
      this.h = handlers || {};
      this.supported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
      this.on = false;            // VAD continuous (免提) mode active
      this.streaming = false;     // mic stream + processing node wired up
      this._mode = "vad";         // "vad" | "manual"(push-to-talk)
      this._recording = false;    // manual: currently capturing a held press
      this._manualFrames = [];
      this._speaking = false;
      this._speechMs = 0;
      this._silenceMs = 0;
      this._frames = [];
      this._threshold = 0.015; // RMS speech gate
    }
    /** Open the mic stream + processing node once (shared by VAD and PTT). */
    async _ensureStream() {
      if (this.streaming) return;
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
      this.src = this.ctx.createMediaStreamSource(this.stream);
      this.node = this.ctx.createScriptProcessor(2048, 1, 1);
      this.node.onaudioprocess = (e) =>
        this._onAudio(e.inputBuffer.getChannelData(0));
      this.src.connect(this.node);
      this.node.connect(this.ctx.destination);
      this.streaming = true;
    }
    /** Start continuous VAD (免提) capture. */
    async start() {
      if (!this.supported || this.on) return;
      this._mode = "vad";
      await this._ensureStream();
      this.on = true;
    }
    /** Push-to-talk: begin capturing while the button is held. */
    async startManual() {
      if (!this.supported) return;
      this._mode = "manual";
      await this._ensureStream();
      this._manualFrames = [];
      this._recording = true;
    }
    /** Push-to-talk: stop on release and flush the held audio as one utterance. */
    stopManual() {
      if (!this._recording) return;
      this._recording = false;
      const frames = this._manualFrames;
      this._manualFrames = [];
      if (!this.ctx) return;
      const pcm16 = downsamplePCM16(frames, this.ctx.sampleRate, 16000);
      if (pcm16.length < 1600) return; // < 100 ms @16k -> ignore stray taps
      this.h.onUtterance && this.h.onUtterance(pcm16);
    }
    _onAudio(frame) {
      // Push-to-talk: accumulate raw frames only while the button is held.
      if (this._mode === "manual") {
        if (this._recording) this._manualFrames.push(new Float32Array(frame));
        return;
      }
      let s = 0;
      for (let i = 0; i < frame.length; i++) s += frame[i] * frame[i];
      const rms = Math.sqrt(s / frame.length);
      const ms = (frame.length / this.ctx.sampleRate) * 1000;
      if (rms > this._threshold) {
        this._speechMs += ms;
        this._silenceMs = 0;
        if (!this._speaking && this._speechMs > 120) {
          this._speaking = true;
          this._frames = [];
          this.h.onSpeechStart && this.h.onSpeechStart();
        }
      } else if (this._speaking) {
        this._silenceMs += ms;
      } else {
        this._speechMs = Math.max(0, this._speechMs - ms);
      }
      if (this._speaking) {
        this._frames.push(new Float32Array(frame));
        if (this._silenceMs > 700) this._end();
      }
    }
    _end() {
      this._speaking = false;
      this._silenceMs = 0;
      this._speechMs = 0;
      const frames = this._frames;
      this._frames = [];
      const pcm16 = downsamplePCM16(frames, this.ctx.sampleRate, 16000);
      if (pcm16.length < 1600) return; // < 100 ms @16k -> ignore stray noise
      this.h.onUtterance && this.h.onUtterance(pcm16);
    }
    stop() {
      this.on = false;
      this.streaming = false;
      this._mode = "vad";
      this._recording = false;
      this._manualFrames = [];
      this._speaking = false;
      this._frames = [];
      try { this.node && this.node.disconnect(); } catch (_e) {}
      try { this.src && this.src.disconnect(); } catch (_e) {}
      try { this.stream && this.stream.getTracks().forEach((t) => t.stop()); } catch (_e) {}
      try { this.ctx && this.ctx.close(); } catch (_e) {}
    }
  }

  const mic = new MicCapture({
    onSpeechStart: () => {
      // Barge-in: stop the avatar the instant the user starts talking.
      if (SETTINGS.bargeIn && app.audio.playing) {
        app.audio.stop();
        proto.sendInterrupt(0);
        log("⏹ barge-in (you started talking)");
      }
      // The user is speaking: the agent is listening (Requirement 13.6/12.5).
      applyAgentState("listening");
    },
    onUtterance: (pcm16) => {
      proto.sendMicAudioData(int16ToBase64(pcm16), 16000);
      proto.sendMicAudioEnd();
      log("🎤 sent " + pcm16.length + " samples → server STT");
      // Utterance captured; awaiting STT + LLM + TTS: the agent is thinking.
      applyAgentState("thinking");
    },
  });

  async function setHandsfree(on) {
    if (!mic.supported) {
      log("hands-free unavailable: getUserMedia not supported in this browser.");
      return;
    }
    if (on) {
      app.audio._ensureCtx();
      try {
        await mic.start();
      } catch (e) {
        log("mic error: " + e);
        return;
      }
      els.handsfreeBtn.classList.add("btn--active");
      els.handsfreeBtn.textContent = "🎤 免提·开";
      log("hands-free ON — speak to chat; talk over the avatar to interrupt. (Needs the preview's --stt; tip: use headphones.)");
    } else {
      mic.stop();
      els.handsfreeBtn.classList.remove("btn--active");
      els.handsfreeBtn.textContent = "🎤 免提";
      log("hands-free OFF");
    }
  }

  els.handsfreeBtn.addEventListener("click", () => setHandsfree(!mic.on));
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    els.handsfreeBtn.disabled = true;
    els.handsfreeBtn.title = "getUserMedia not supported in this browser";
  }

  // --- 按住说话(push-to-talk) — 移动端用它替换持续「免提」(CSS 控制显隐) -------
  // 手机上持续 VAD 免提交互体验差,改用对讲机式「按住说话」:按住录音、松开把这一段
  // 作为一句话发去 STT。按下即触发打断(barge-in)。麦克风仍需安全上下文——页面在
  // 127.0.0.1 或 HTTPS 下才放行;局域网 http 会被浏览器拦截,此时给出明确提示。
  if (els.pttBtn) {
    let _pttActive = false;
    if (!mic.supported) {
      els.pttBtn.disabled = true;
      els.pttBtn.title =
        "麦克风不可用:此浏览器不支持,或页面不在 127.0.0.1 / HTTPS 下" +
        "(手机经局域网 http 访问会被浏览器拦截麦克风,请用 HTTPS)。";
    }

    async function pttDown() {
      if (!mic.supported || _pttActive) return;
      _pttActive = true;
      app.audio._ensureCtx();
      // 按下即打断:停掉形象正在播放的语音并通知后端中止本轮(barge-in)。
      if (SETTINGS.bargeIn && app.audio.playing) {
        app.audio.stop();
        proto.sendInterrupt(0);
      }
      try {
        await mic.startManual();
      } catch (e) {
        _pttActive = false;
        log(
          "麦克风打开失败:" + e +
          "(手机请改用 HTTPS 或在 127.0.0.1 打开;局域网 http 浏览器会拦截麦克风)"
        );
        return;
      }
      els.pttBtn.classList.add("btn--recording");
      els.pttBtn.textContent = "🔴"; // 录音中(纯图标,避免长按选字/右键菜单)
      applyAgentState("listening");
    }

    function pttUp() {
      if (!_pttActive) return;
      _pttActive = false;
      try {
        mic.stopManual(); // 把按住期间的音频作为一句话发去 STT(onUtterance)
      } catch (e) {
        log("发送语音失败:" + e);
      }
      els.pttBtn.classList.remove("btn--recording");
      els.pttBtn.textContent = "🎤";
    }

    // 用 pointer 事件统一鼠标/触摸;按住时阻止页面滚动/长按菜单/选区。
    els.pttBtn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      pttDown();
    });
    els.pttBtn.addEventListener("pointerup", (e) => {
      e.preventDefault();
      pttUp();
    });
    els.pttBtn.addEventListener("pointercancel", pttUp);
    els.pttBtn.addEventListener("pointerleave", pttUp); // 手指滑出按钮即发送
    els.pttBtn.addEventListener("contextmenu", (e) => e.preventDefault());
    els.pttBtn.addEventListener("touchstart", (e) => e.preventDefault(), {
      passive: false,
    });
  }

  // --- STT model selector (accuracy vs speed/size) ----------------------
  let _sttPopulated = false;
  function populateSttSelect(modelInfo) {
    const sel = els.sttModel;
    if (!sel) return;
    const models = modelInfo.stt_models || {};
    const current = modelInfo.stt_model || "";
    if (!modelInfo.stt_enabled) {
      sel.innerHTML = '<option value="">（STT 未启用，用 --stt 启动）</option>';
      sel.disabled = true;
      return;
    }
    // Rebuild options once (and refresh the selected value on each set-model).
    if (!_sttPopulated || sel.options.length <= 1) {
      sel.innerHTML = "";
      for (const [value, label] of Object.entries(models)) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        sel.appendChild(opt);
      }
      _sttPopulated = true;
    }
    if (current) sel.value = current;
    sel.disabled = false;
  }

  els.sttModel.addEventListener("change", () => {
    const v = els.sttModel.value;
    if (!v) return;
    log("切换 STT 模型 → " + v + "（首次会下载，请稍候）");
    proto.sendTextInput("::cmd::stt=" + v);
  });

  // --- TTS voice selector ----------------------------------------------
  let _ttsPopulated = false;
  function populateTtsSelect(modelInfo) {
    const sel = els.ttsVoice;
    if (!sel) return;
    const voices = modelInfo.tts_voices || {};
    const current = modelInfo.tts_voice || "";
    if (!modelInfo.tts_enabled) {
      sel.innerHTML = '<option value="">（TTS 未启用）</option>';
      sel.disabled = true;
      return;
    }
    if (!_ttsPopulated || sel.options.length <= 1) {
      sel.innerHTML = "";
      for (const [value, label] of Object.entries(voices)) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        sel.appendChild(opt);
      }
      _ttsPopulated = true;
    }
    if (current) sel.value = current;
    sel.disabled = false;
  }
  els.ttsVoice.addEventListener("change", () => {
    const v = els.ttsVoice.value;
    if (!v) return;
    log("切换音色 → " + v);
    proto.sendTextInput("::cmd::tts=" + v);
  });

  // --- status pane ------------------------------------------------------
  let _lastModelInfo = {};
  function renderStatus() {
    const mi = _lastModelInfo;
    // Drive the StatusPanel from existing /client-ws fields + additive extras
    // (Requirement 13). The active renderer type comes from RendererManager;
    // STT/TTS reuse the existing stt_model/tts_voice fields (R13.3); the model
    // name+url comes from set-model-and-conf. Connection status and latency are
    // owned by the panel (set from setStatus / onPong) and preserved across this
    // incremental update.
    statusPanel.update({
      renderer: rendererManager.type,
      model: { name: mi.name || null, url: mi.url || null },
      stt: mi.stt_enabled ? (mi.stt_model || null) : null,
      tts: mi.tts_enabled ? (mi.tts_voice || null) : null,
    });
  }

  // --- settings modal wiring -------------------------------------------
  function applyClientSettings() {
    // avatar scale (CSS transform on the canvas). Target the manager's LIVE
    // canvas (the manager swaps in a fresh canvas on each renderer switch), so
    // the scale keeps applying after switching renderers.
    const liveCanvas =
      (rendererManager && typeof rendererManager.getCanvas === "function"
        ? rendererManager.getCanvas()
        : null) || els.canvas;
    liveCanvas.style.transform = "scale(" + SETTINGS.avatarScale + ")";
    liveCanvas.style.transformOrigin = "center center";
    // background
    const stage = document.querySelector(".stage") || document.body;
    const bg = {
      default: "",
      transparent: "transparent",
      green: "#00b140",
      blue: "#0047bb",
    }[SETTINGS.bgMode];
    document.body.style.background = bg === "" ? "" : bg;
    if (stage) stage.style.background = bg === "" ? "" : bg;
    // subtitle / log visibility
    els.subtitle.style.display = SETTINGS.showSubtitle ? "" : "none";
    els.log.style.display = SETTINGS.showLog ? "" : "none";
    // mic VAD threshold
    mic._threshold = SETTINGS.vadThreshold;
  }

  function syncSettingsControls() {
    els.vadThreshold.value = SETTINGS.vadThreshold;
    els.vadVal.textContent = SETTINGS.vadThreshold.toFixed(3);
    els.autoMic.checked = !!SETTINGS.autoMic;
    els.avatarScale.value = SETTINGS.avatarScale;
    els.scaleVal.textContent = Math.round(SETTINGS.avatarScale * 100) + "%";
    els.bgMode.value = SETTINGS.bgMode;
    if (els.rendererSelect) {
      els.rendererSelect.value = RendererManager.resolveRenderer(
        rendererManager.type || SETTINGS.renderer
      );
    }
    els.showSubtitle.checked = !!SETTINGS.showSubtitle;
    els.showLog.checked = !!SETTINGS.showLog;
    els.bargeinToggle.checked = !!SETTINGS.bargeIn;
  }

  function openModal() {
    renderStatus();
    els.modal.hidden = false;
  }
  function closeModal() {
    els.modal.hidden = true;
  }
  els.settingsBtn.addEventListener("click", openModal);
  els.modal.addEventListener("click", (e) => {
    if (e.target.hasAttribute("data-close")) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !els.modal.hidden) closeModal();
  });
  // tabs
  els.modal.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.getAttribute("data-tab");
      els.modal.querySelectorAll(".tab").forEach((t) =>
        t.classList.toggle("tab--active", t === tab)
      );
      els.modal.querySelectorAll(".pane").forEach((p) =>
        p.classList.toggle("pane--active", p.getAttribute("data-pane") === name)
      );
      if (name === "status") renderStatus();
    });
  });

  // control listeners
  els.vadThreshold.addEventListener("input", () => {
    SETTINGS.vadThreshold = parseFloat(els.vadThreshold.value);
    els.vadVal.textContent = SETTINGS.vadThreshold.toFixed(3);
    mic._threshold = SETTINGS.vadThreshold;
    saveSettings();
  });
  els.autoMic.addEventListener("change", () => {
    SETTINGS.autoMic = els.autoMic.checked;
    saveSettings();
  });
  els.avatarScale.addEventListener("input", () => {
    SETTINGS.avatarScale = parseFloat(els.avatarScale.value);
    els.scaleVal.textContent = Math.round(SETTINGS.avatarScale * 100) + "%";
    applyClientSettings();
    saveSettings();
  });
  els.bgMode.addEventListener("change", () => {
    SETTINGS.bgMode = els.bgMode.value;
    applyClientSettings();
    saveSettings();
  });
  // --- renderer selector (外观 tab) — Requirement 1.1/1.2/1.7/1.8 ---------
  // Reflect the currently active renderer type in the selector control.
  function syncRendererSelect() {
    if (!els.rendererSelect) return;
    els.rendererSelect.value = RendererManager.resolveRenderer(
      rendererManager.type || SETTINGS.renderer
    );
    // Reflect the active renderer type in the Telemetry panel (Requirement 13.1).
    statusPanel.setRenderer(rendererManager.type);
  }
  if (els.rendererSelect) {
    els.rendererSelect.addEventListener("change", () => {
      // Normalize the selection to one of the valid types (R1.1/1.9).
      const choice = RendererManager.resolveRenderer(els.rendererSelect.value);
      // Runtime switch (R1.2): construct/destroy is handled by the manager,
      // which rolls back to the previous renderer on failure (R1.5).
      Promise.resolve(rendererManager.switchTo(choice)).then(() => {
        // Persist the selected renderer to SETTINGS + localStorage (R1.7).
        // On setItem failure, persistRenderer keeps the selection in memory
        // (so the chosen renderer stays active) and surfaces a "设置保存失败"
        // notice through the event log (R1.8) — it never throws.
        RendererManager.persistRenderer(choice, {
          settings: SETTINGS,
          storageKey: SETTINGS_KEY,
          notify: (message) => log(message),
        });
        // Load the active renderer's model SOURCE (Live2D url / Live3D vrm_url)
        // so the freshly-switched renderer shows content without waiting for the
        // next set-model-and-conf (Requirement 4.1/5).
        loadModelForActiveRenderer(
          _lastEmotionMap,
          (_lastModelInfo && _lastModelInfo.name) || "",
          _lastModelInfo
        );
        syncRendererSelect();
      });
    });
  }
  els.showSubtitle.addEventListener("change", () => {
    SETTINGS.showSubtitle = els.showSubtitle.checked;
    applyClientSettings();
    saveSettings();
  });
  els.showLog.addEventListener("change", () => {
    SETTINGS.showLog = els.showLog.checked;
    applyClientSettings();
    saveSettings();
  });
  els.bargeinToggle.addEventListener("change", () => {
    SETTINGS.bargeIn = els.bargeinToggle.checked;
    saveSettings();
  });
  let _rateTimer = null;
  els.ttsRate.addEventListener("input", () => {
    const pct = parseInt(els.ttsRate.value, 10) || 0;
    els.ttsRateVal.textContent = (pct >= 0 ? "+" : "") + pct + "%";
    clearTimeout(_rateTimer);
    _rateTimer = setTimeout(() => {
      proto.sendTextInput("::cmd::ttsrate=" + (pct >= 0 ? "+" : "") + pct + "%");
    }, 350);
  });

  syncSettingsControls();
  applyClientSettings();

  // --- 版式切换（topbar 分段控件，data-layout 应用在 .stage 上）-------------
  // 提供两种舞台排版供选择：cinema(剧场·默认) / solo(纯形象)。选择写入
  // SETTINGS.layout 并持久化，启动时恢复；默认 cinema。纯 CSS 切换（见 styles.css
  // 的 [data-layout]），不重建任何渲染器，故 Live2D/Live3D 与伴随声波球均不受影响。
  (function setupLayoutSwitch() {
    const stage = document.querySelector(".stage");
    const buttons = Array.prototype.slice.call(
      document.querySelectorAll(".layout-btn")
    );
    if (!stage || buttons.length === 0) return;
    const VALID = ["cinema", "solo"];
    function apply(layout) {
      const choice = VALID.indexOf(layout) !== -1 ? layout : "cinema";
      stage.dataset.layout = choice;
      buttons.forEach((b) => {
        const on = b.getAttribute("data-layout") === choice;
        b.classList.toggle("is-active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      SETTINGS.layout = choice;
    }
    buttons.forEach((b) => {
      b.addEventListener("click", () => {
        apply(b.getAttribute("data-layout"));
        saveSettings();
        log("版式 → " + stage.dataset.layout);
      });
    });
    // Restore the persisted layout on startup (default home).
    apply(SETTINGS.layout);
  })();

  // --- persona ("灵魂人格") save ---------------------------------------
  function b64utf8(s) {
    return btoa(unescape(encodeURIComponent(s)));
  }
  els.personaSave.addEventListener("click", () => {
    const text = (els.personaText.value || "").trim();
    if (!text) {
      log("人格不能为空");
      return;
    }
    proto.sendTextInput("::cmd::persona=" + b64utf8(text));
    log("已保存灵魂人格，正在用新人设重建对话…");
  });

  // Browsers require a user gesture before audio can start; resume the audio
  // context on the first interaction so the first reply is audible. Also honor
  // the "auto-mic" setting here (getUserMedia is happiest after a gesture).
  const resumeAudio = () => {
    app.audio._ensureCtx();
    if (SETTINGS.autoMic && !mic.on) {
      setHandsfree(true);
    }
    window.removeEventListener("pointerdown", resumeAudio);
    window.removeEventListener("keydown", resumeAudio);
  };
  window.addEventListener("pointerdown", resumeAudio);
  window.addEventListener("keydown", resumeAudio);

  // --- go ---------------------------------------------------------------
  log("front-end ready; gateway = " + resolveGatewayUrl());
  // Seed the Telemetry panel with the current renderer type and start its
  // independent latency-refresh interval (≤5000ms, decoupled from LLM/TTS).
  statusPanel.setRenderer(rendererManager.type);
  statusPanel.start();
  // Seed the initial Agent_State (idle) so the panel starts coherent.
  applyAgentState("idle");

  // Bring up the companion voice-orb beside the main avatar once three.js is
  // ready (it renders a particle sphere that pulses with lip-sync and recolors
  // by Agent_State). It NEVER replaces the active Live2D/Live3D renderer — it is
  // an independent always-on visualizer on its own #orb-canvas. If three.js or
  // WebGL is unavailable the orb is silently skipped and the avatar is unaffected.
  function initVoiceOrb() {
    if (voiceOrb) return;
    const orbCanvas = document.getElementById("orb-canvas");
    if (!orbCanvas || typeof OrbRenderer !== "function") return;
    try {
      voiceOrb = new OrbRenderer(orbCanvas, { canvas: orbCanvas, type: "Orb" });
      // Sync the orb to the current Agent_State immediately.
      voiceOrb.setAgentState(_agentState);
    } catch (e) {
      log("声波粒子球初始化失败（已跳过，不影响主形象）：" + e);
      voiceOrb = null;
    }
  }
  whenThreeReady(initVoiceOrb);

  proto.connect();
})();
