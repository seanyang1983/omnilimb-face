/*
 * omnilimb-face reference front-end — avatar renderer.
 *
 * `CanvasAvatar` is a dependency-free PLACEHOLDER avatar drawn on a <canvas>.
 * It exposes the small surface the protocol layer drives:
 *
 *     avatar.setMouthOpen(v)      // v in [0,1] — lip-sync from `volumes`
 *     avatar.setExpression(i, n)  // expression index + optional name
 *     avatar.resetMouth()         // close mouth to rest (control: mouth-reset)
 *     avatar.setModel(url)        // (re)load a model; no-op for the placeholder
 *
 * This proves the full /client-ws -> render loop offline. `Live2DAvatar` (at the
 * bottom of this file) renders a REAL Cubism model with the SAME surface using
 * pixi-live2d-display + Cubism Core; `app.js` picks it when those runtimes are
 * present and falls back to `CanvasAvatar` otherwise. The protocol layer
 * (protocol.js) is unchanged either way.
 */

(function (global) {
  "use strict";

  // A small palette of expression "moods". A model's emotionMap maps emotion
  // keywords -> integer indices; this placeholder maps those indices onto a
  // handful of visibly-distinct moods (cycled if the index exceeds the table).
  const MOODS = [
    { name: "neutral", face: "#ffe0bd", brow: 0, eye: 1.0, mouthCurve: 0.0 },
    { name: "joy", face: "#ffe7c2", brow: -6, eye: 0.7, mouthCurve: 0.6 },
    { name: "anger", face: "#ffd2b0", brow: 10, eye: 0.85, mouthCurve: -0.5 },
    { name: "sadness", face: "#e9e2d6", brow: -10, eye: 0.9, mouthCurve: -0.6 },
    { name: "surprise", face: "#ffe0bd", brow: -12, eye: 1.25, mouthCurve: 0.2 },
    { name: "smirk", face: "#ffe7c2", brow: -3, eye: 0.8, mouthCurve: 0.35 },
  ];

  class CanvasAvatar {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.mouthOpen = 0; // target openness [0,1]
      this._mouthShown = 0; // smoothed openness actually drawn
      this.moodIndex = 0;
      this.expressionName = "neutral";
      this._blink = 1; // 1 = open, 0 = closed
      this._blinkT = 0;
      this._raf = null;
      this._start();
    }

    /** Drive lip-sync openness; clamped to [0,1]. */
    setMouthOpen(v) {
      if (typeof v !== "number" || Number.isNaN(v)) v = 0;
      this.mouthOpen = Math.max(0, Math.min(1, v));
    }

    /** Close the mouth to its resting state (control: mouth-reset). */
    resetMouth() {
      this.mouthOpen = 0;
    }

    /**
     * Placeholder ignores the model URL (it draws its own avatar). Present so
     * the renderer surface matches Live2DAvatar and app.js can call it
     * uniformly.
     */
    setModel(_url) {
      /* no-op for the canvas placeholder */
    }

    /** Placeholder has no motions; present for a uniform renderer surface. */
    playMotion(_group) {
      /* no-op for the canvas placeholder */
    }

    /**
     * Apply an expression. `index` is the model emotionMap value; `name` is the
     * optional human-readable emotion keyword for the on-screen label.
     */
    setExpression(index, name) {
      if (typeof index === "number" && index >= 0) {
        this.moodIndex = index % MOODS.length;
      }
      this.expressionName =
        name || MOODS[this.moodIndex].name || `expr#${index}`;
    }

    _start() {
      let last = performance.now();
      const loop = (now) => {
        const dt = Math.min(0.05, (now - last) / 1000);
        last = now;
        this._update(dt);
        this._draw();
        this._raf = requestAnimationFrame(loop);
      };
      this._raf = requestAnimationFrame(loop);
    }

    _update(dt) {
      // Smooth the mouth toward its target so lip-sync looks natural rather
      // than stepping between volume samples.
      const k = 1 - Math.exp(-dt * 18);
      this._mouthShown += (this.mouthOpen - this._mouthShown) * k;

      // Idle blink every ~3.5s.
      this._blinkT += dt;
      if (this._blinkT > 3.5) {
        this._blink = Math.max(0, this._blink - dt * 12);
        if (this._blink <= 0) this._blinkT = 0;
      } else if (this._blink < 1) {
        this._blink = Math.min(1, this._blink + dt * 12);
      }
    }

    _draw() {
      const { ctx, canvas } = this;
      const w = canvas.width;
      const h = canvas.height;
      const cx = w / 2;
      const cy = h / 2;
      const mood = MOODS[this.moodIndex] || MOODS[0];

      ctx.clearRect(0, 0, w, h);

      // Head.
      ctx.save();
      ctx.fillStyle = mood.face;
      ctx.strokeStyle = "rgba(0,0,0,0.15)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.ellipse(cx, cy, w * 0.28, h * 0.33, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();

      const eyeY = cy - h * 0.06;
      const eyeDX = w * 0.12;
      const eyeOpen = this._blink * mood.eye;

      // Eyes.
      ctx.fillStyle = "#2a2a33";
      for (const sx of [-1, 1]) {
        const ex = cx + sx * eyeDX;
        ctx.save();
        ctx.beginPath();
        ctx.ellipse(ex, eyeY, w * 0.035, h * 0.045 * eyeOpen + 1, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }

      // Brows (vertical offset encodes the mood).
      ctx.strokeStyle = "#3a2f2a";
      ctx.lineWidth = 4;
      for (const sx of [-1, 1]) {
        const ex = cx + sx * eyeDX;
        const by = eyeY - h * 0.07 + mood.brow;
        ctx.beginPath();
        ctx.moveTo(ex - w * 0.045, by + (sx < 0 ? -mood.brow * 0.4 : mood.brow * 0.4));
        ctx.lineTo(ex + w * 0.045, by - (sx < 0 ? -mood.brow * 0.4 : mood.brow * 0.4));
        ctx.stroke();
      }

      // Mouth: width fixed, height driven by lip-sync openness; the resting
      // curve encodes the mood (smile/frown).
      const mouthY = cy + h * 0.14;
      const mouthW = w * 0.16;
      const open = this._mouthShown;
      const mouthH = h * (0.015 + 0.11 * open);
      const curve = mood.mouthCurve * (1 - open) * h * 0.04;

      ctx.save();
      ctx.fillStyle = "#7a2e2e";
      ctx.strokeStyle = "#52201f";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(cx - mouthW, mouthY);
      ctx.quadraticCurveTo(cx, mouthY - curve - mouthH, cx + mouthW, mouthY);
      ctx.quadraticCurveTo(cx, mouthY - curve + mouthH, cx - mouthW, mouthY);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }

    destroy() {
      if (this._raf) cancelAnimationFrame(this._raf);
      this._raf = null;
    }
  }

  /*
   * Live2DAvatar — renders a REAL Cubism model with pixi-live2d-display.
   *
   * Same surface as CanvasAvatar (setMouthOpen / setExpression / resetMouth /
   * setModel), so app.js drives either interchangeably. Requires the runtimes
   * loaded in index.html: PIXI (v6), PIXI.live2d (pixi-live2d-display) and a
   * Cubism Core (Live2DCubismCore for Cubism 4/5, or the Cubism 2.1 core).
   *
   * Design notes:
   *   * The model is loaded with `autoUpdate: false` and advanced manually in a
   *     ticker callback, so we can write the lip-sync parameter AFTER the motion
   *     update each frame. Otherwise the model's idle motion animates
   *     ParamMouthOpenY and fights the lip-sync (a well-known gotcha).
   *   * `setExpression(i)` maps the emotionMap index to the model's expression
   *     list via pixi-live2d-display's `model.expression(index)`.
   *   * Mouth openness is smoothed toward its target for natural lip movement.
   */
  class Live2DAvatar {
    constructor(canvas) {
      const PIXI = global.PIXI;
      if (!PIXI || !PIXI.live2d || !PIXI.live2d.Live2DModel) {
        throw new Error(
          "Live2DAvatar requires PIXI v6 + pixi-live2d-display (see index.html)."
        );
      }
      this.canvas = canvas;
      this.model = null;
      this._mouthTarget = 0; // lip-sync target [0,1]
      this._mouthShown = 0; // smoothed value actually written
      this._pendingExpression = null; // expression requested before model load
      this._ticking = false;
      this._lastNow = performance.now();
      // Cubism 4/5 parameter id(s) that drive the mouth for lip-sync. Different
      // models use different params: most use "ParamMouthOpenY", some (e.g. the
      // Mao sample) use the vowel param "ParamA". Discovered per-model from the
      // model's declared LipSync group in setModel(); these are the defaults
      // used before a model loads. Writing an absent id is a harmless no-op in
      // Cubism core, so it is safe to set several.
      this._mouthParamIds = ["ParamMouthOpenY", "ParamA"];

      // Disable the model's BUILT-IN motion sound clips. Some sample models
      // (e.g. Haru) ship voice clips on their Tap motions — we never want those
      // to play: all speech audio is driven by app.js's AudioPlayer (the host
      // TTS), never by the model's motion sounds.
      try {
        if (PIXI.live2d.config) PIXI.live2d.config.sound = false;
      } catch (_e) {
        /* older builds may not expose config; ignore */
      }

      // Transparent canvas so the page background shows through.
      this.app = new PIXI.Application({
        view: canvas,
        width: canvas.width,
        height: canvas.height,
        backgroundAlpha: 0,
        antialias: true,
        autoStart: true,
      });
    }

    /**
     * Load (or replace) the Cubism model from `url`. Returns a promise that
     * resolves with the loaded model or rejects on failure (app.js logs and
     * surfaces an on-canvas message).
     */
    async setModel(url) {
      if (!url) return null;
      const { Live2DModel } = global.PIXI.live2d;

      let model;
      try {
        // autoUpdate:false — we drive update() ourselves (see _tick) so the
        // lip-sync parameter can be written after the motion update.
        model = await Live2DModel.from(url, { autoUpdate: false });
      } catch (err) {
        this._showError("Live2D model load failed:\n" + err);
        throw err;
      }

      if (this.model) {
        this.app.stage.removeChild(this.model);
        try {
          this.model.destroy();
        } catch (_e) {
          /* ignore */
        }
        this.model = null;
      }

      this.model = model;
      this.app.stage.addChild(model);
      this._fit();

      // Discover which parameter(s) this model uses for lip-sync so the mouth
      // moves regardless of the model's rig (ParamMouthOpenY, ParamA, ...).
      this._mouthParamIds = this._discoverLipSyncIds(model);

      if (!this._ticking) {
        this._ticking = true;
        this._lastNow = performance.now();
        this.app.ticker.add(this._tick, this);
      }

      // Apply any expression requested before the model finished loading.
      if (this._pendingExpression !== null) {
        const p = this._pendingExpression;
        this._pendingExpression = null;
        this.setExpression(p.index, p.name);
      }
      return model;
    }

    /** Center and scale the model to fit the canvas with a little padding. */
    _fit() {
      const model = this.model;
      if (!model) return;
      model.anchor.set(0.5, 0.5);
      model.position.set(this.app.renderer.width / 2, this.app.renderer.height / 2);
      const pad = 0.9;
      const s =
        Math.min(
          this.app.renderer.width / model.width,
          this.app.renderer.height / model.height
        ) * pad;
      if (Number.isFinite(s) && s > 0) model.scale.set(s);
    }

    _tick() {
      const model = this.model;
      if (!model) return;
      const now = performance.now();
      const dt = Math.min(0.05, (now - this._lastNow) / 1000);
      this._lastNow = now;

      // Advance motion / physics / idle ourselves (autoUpdate:false).
      model.update(this.app.ticker.deltaMS);

      // Smooth the mouth toward its target so lip-sync looks natural.
      const k = 1 - Math.exp(-dt * 18);
      this._mouthShown += (this._mouthTarget - this._mouthShown) * k;

      // Write the lip-sync parameter AFTER the motion update so idle motion
      // does not override it.
      this._setMouthParam(this._mouthShown);
    }

    /** Write the mouth-open parameter, handling Cubism 4/5 and 2.1 models. */
    _setMouthParam(v) {
      const im = this.model && this.model.internalModel;
      const core = im && im.coreModel;
      if (!core) return;
      if (typeof core.setParameterValueById === "function") {
        // Cubism 4 / 5: drive every lip-sync param the model declares. An id
        // the model does not define is a harmless no-op in Cubism core.
        for (const id of this._mouthParamIds) {
          core.setParameterValueById(id, v);
        }
      } else if (typeof core.setParamFloat === "function") {
        core.setParamFloat("PARAM_MOUTH_OPEN_Y", v); // Cubism 2.1
      }
    }

    /**
     * Read the model's declared LipSync parameter id(s) from its settings so
     * lip-sync targets the right param. "ParamMouthOpenY" is always appended as
     * a safety fallback. Returns sensible defaults if the settings cannot be
     * read.
     */
    _discoverLipSyncIds(model) {
      const fallback = ["ParamMouthOpenY", "ParamA"];
      try {
        const settings = model && model.internalModel && model.internalModel.settings;
        // pixi-live2d-display keeps the parsed model3.json on the settings
        // object; the exact property name varies across builds, so probe a few.
        const json =
          (settings && (settings.json || settings._json)) || settings || null;
        const groups = json && (json.Groups || json.groups);
        if (Array.isArray(groups)) {
          const lip = groups.find(
            (g) => g && (g.Name === "LipSync" || g.name === "LipSync")
          );
          const ids = lip && (lip.Ids || lip.ids);
          if (Array.isArray(ids) && ids.length) {
            return Array.from(new Set(ids.concat("ParamMouthOpenY")));
          }
        }
      } catch (_e) {
        /* fall through to defaults */
      }
      return fallback;
    }

    /** Drive lip-sync openness; clamped to [0,1]. */
    setMouthOpen(v) {
      if (typeof v !== "number" || Number.isNaN(v)) v = 0;
      this._mouthTarget = Math.max(0, Math.min(1, v));
    }

    /** Close the mouth to its resting state (control: mouth-reset). */
    resetMouth() {
      this._mouthTarget = 0;
    }

    /**
     * Play a body motion from the model's motion group (e.g. "Tap" / "Idle")
     * so the avatar visibly moves. No-op until the model has loaded or when the
     * group is absent.
     */
    playMotion(group) {
      if (!this.model || !group) return;
      try {
        const resolved = this._resolveMotionGroup(group);
        if (!resolved) return;
        // pixi-live2d-display: model.motion(group, index?, priority?) — default
        // priority overrides the idle motion.
        this.model.motion(resolved);
      } catch (_e) {
        /* group may not exist on this model; ignore */
      }
    }

    /**
     * Map a desired motion group onto a group the loaded model actually
     * defines. Sample models name their "tap" group differently ("Tap" on
     * Haru, "TapBody" on Mao/Hiyori), so a request for "Tap" resolves to the
     * first tap-like group present. Returns null if no usable group exists.
     */
    _resolveMotionGroup(group) {
      const mm = this.model && this.model.internalModel && this.model.internalModel.motionManager;
      const defs = (mm && mm.definitions) || {};
      const available = Object.keys(defs);
      if (!available.length) return group; // unknown; let model.motion try
      if (defs[group]) return group;
      const aliases =
        group === "Tap"
          ? ["TapBody", "Tap@Body", "Tap", "Flick", "FlickUp", "Body"]
          : [group];
      for (const a of aliases) {
        if (defs[a]) return a;
      }
      // Last resort: any group whose name looks tap-like.
      return available.find((g) => /tap|flick|body/i.test(g)) || null;
    }

    /**
     * Apply an expression by emotionMap `index` (the value delivered in
     * set-model-and-conf). `name` is informational. If the model has not
     * finished loading, the request is stored and applied on load.
     */
    setExpression(index, name) {
      if (typeof index !== "number" || index < 0) return;
      if (!this.model) {
        this._pendingExpression = { index, name };
        return;
      }
      try {
        // pixi-live2d-display accepts an index into the model's expression list.
        this.model.expression(index);
      } catch (_e) {
        /* model may define fewer expressions; ignore out-of-range requests */
      }
    }

    /** Render a short error message onto the stage (load failures, etc.). */
    _showError(msg) {
      try {
        const PIXI = global.PIXI;
        const t = new PIXI.Text(String(msg), {
          fill: 0xff6b6b,
          fontSize: 14,
          wordWrap: true,
          wordWrapWidth: this.app.renderer.width - 20,
        });
        t.position.set(10, 10);
        this.app.stage.addChild(t);
      } catch (_e) {
        /* nothing else we can do */
      }
    }

    destroy() {
      if (this._ticking) {
        this.app.ticker.remove(this._tick, this);
        this._ticking = false;
      }
      if (this.model) {
        try {
          this.model.destroy();
        } catch (_e) {
          /* ignore */
        }
        this.model = null;
      }
      try {
        this.app.destroy(false);
      } catch (_e) {
        /* ignore */
      }
    }
  }

  global.CanvasAvatar = CanvasAvatar;
  global.Live2DAvatar = Live2DAvatar;
})(window);
