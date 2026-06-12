/*
 * omnilimb-face reference front-end — Live3DRenderer (three.js + three-vrm).
 *
 * `Live3DRenderer` 是「Live3D」渲染器：用 three.js 在提供的 <canvas> 上实时渲染一个
 * VRM 人形形象，背景透明（alpha=0）使下层页面背景完全透出。它实现统一的
 * `Renderer_Interface`（见 renderer-manager.js），因此协议层（app.js / protocol.js）
 * 可用与其它渲染器相同的信号驱动它，而无需判断其具体类型。
 *
 * ============================================================================
 * 本文件实现范围（任务 5.1 + 5.2）
 * ============================================================================
 *   - `setModel(url)`：用 three.js `GLTFLoader` + three-vrm `VRMLoaderPlugin` 加载
 *     VRM 模型；10 秒超时 / 网络失败 / 格式不可解析 → 在画布上显示可见的失败原因
 *     文本提示、保留页面其余功能、**不渲染部分或损坏的模型**；返回指示失败的结果，
 *     绝不抛出未捕获的错误（Requirement 5.1 加载 / 5.2 失败态 / setModel 契约 2.10）。
 *   - 透明画布：`WebGLRenderer({ alpha:true, premultipliedAlpha:false })` +
 *     `setClearAlpha(0)`，使下层页面背景完全透出（Requirement 5.3）。
 *   - 无语音播放时循环执行待机动画（idle，轻微呼吸/摆动）（Requirement 5.4）。
 *   - 无语音播放时以 2–6 秒随机间隔驱动眨眼（blink）（Requirement 5.5）；眨眼间隔
 *     采样器 `_nextBlinkInterval()` 返回 [2000,6000] 毫秒内的值，供属性测试探测。
 *   - 依赖缺失时「干净失败」：构造函数检测到 `three.js`（window.THREE）缺失或浏览器
 *     WebGL 上下文无法创建时**抛出错误**（而非静默 no-op），使 RendererManager 按
 *     Requirement 9 降级（回退到 CanvasAvatar / Live2D）。镜像 OrbRenderer 的语义。
 *   - 口型 lip-sync（任务 5.2）：`setMouthOpen(v)` clamp 后线性驱动 VRM 口型 blendshape
 *     （按可用项探测 `aa`/元音口型 preset），`resetMouth()` 置 0；非数值保持当前值、不抛错
 *     （Requirement 6.1–6.5）。**关键**：口型权重在每帧 `vrm.update(dt)` **之后**写入
 *     （`_applyMouth`），避免被 idle/expression 动画覆盖（three-vrm 的已知 gotcha，
 *     镜像 Live2D 在 motion 更新后写口型参数的策略）。
 *   - 情绪映射（任务 5.2）：`setExpression(index, name)` 经 `name` 映射到 VRM 情绪 preset
 *     （happy/angry/sad/relaxed/surprised/neutral），应用前清零上一表情权重以维持「单活
 *     表情」不变量——任意时刻至多一个情绪 preset 权重大于 0（Requirement 6.6）；无对应
 *     表情则保持当前表情不变、不抛错（Requirement 6.7）。
 */

(function (global) {
  "use strict";

  // VRM 加载超时（毫秒）。超过此时限未完成加载即判定失败（Requirement 5.1/5.2）。
  const VRM_LOAD_TIMEOUT_MS = 10000;
  // 眨眼随机间隔区间（毫秒）（Requirement 5.5）。
  const BLINK_MIN_MS = 2000;
  const BLINK_MAX_MS = 6000;
  // 一次眨眼的闭合时长（毫秒）：blink 权重从 1 衰减到 0 所需的时间。
  const BLINK_DURATION_MS = 140;
  // 「正在说话」的余辉窗口（毫秒）：setMouthOpen(v>0) 后多久内视为仍在说话，
  // 用以在语音播放期间抑制眨眼调度（Requirement 5.5 的 WHILE 无语音条件）。
  const SPEAKING_HOLD_MS = 300;

  // 候选「口型张开」VRM 表情/morph 名（按优先级）。three-vrm 的口型 preset 标准名为
  // "aa"（最大张口）；不同模型/分发也可能用 "a"/"ou"/"oh"/"ih"/"ee" 等元音口型。
  // _resolveMouthExpression() 在加载模型时按可用项探测，命中第一个存在的；探测不能时
  // 退回 "aa"（写入经 try/catch 包裹，模型无此项也安全）（Requirement 6.1）。
  const MOUTH_EXPR_CANDIDATES = ["aa", "a", "ou", "oh", "ih", "ee"];

  // 情绪名（emotionMap 反查得到的可读名，由 setExpression 的 name 参数传入）→ VRM
  // 表情 preset 的映射。three-vrm 的标准情绪 preset 为：happy/angry/sad/relaxed/
  // surprised/neutral。常见情绪关键词归一到这些 preset；未列出的名字无对应 VRM 表情，
  // 按 Requirement 6.7 保持当前表情不变。键统一用小写匹配。
  const EMOTION_TO_VRM_EXPRESSION = {
    neutral: "neutral",
    normal: "neutral",
    calm: "relaxed",
    relaxed: "relaxed",
    relax: "relaxed",
    happy: "happy",
    happiness: "happy",
    joy: "happy",
    joyful: "happy",
    smile: "happy",
    fun: "happy",
    laugh: "happy",
    angry: "angry",
    anger: "angry",
    mad: "angry",
    rage: "angry",
    disgust: "angry",
    sad: "sad",
    sadness: "sad",
    sorrow: "sad",
    cry: "sad",
    crying: "sad",
    surprised: "surprised",
    surprise: "surprised",
    shock: "surprised",
    shocked: "surprised",
    astonished: "surprised",
    fear: "surprised",
    fearful: "surprised",
  };

  /**
   * 探测并创建一个 WebGL 渲染上下文（Requirement 5.3 透明画布 + WebGL 不可用检测）。
   * 按 three.js 的上下文优先级（webgl2 → webgl → experimental-webgl）尝试，使后续
   * three.js 的 `WebGLRenderer` 可经 `context` 选项复用同一上下文（避免在同一画布上
   * 重复创建上下文导致的冲突）。透明所需的上下文属性（`alpha:true`、
   * `premultipliedAlpha:false`）在首次创建上下文时即指定。任一类型成功即返回该
   * 上下文；全部失败返回 null。
   *
   * @param {HTMLCanvasElement} canvas 目标画布。
   * @returns {WebGLRenderingContext|null} WebGL 上下文，或不可用时 null。
   */
  function acquireWebGLContext(canvas) {
    if (!canvas || typeof canvas.getContext !== "function") return null;
    const attrs = { alpha: true, premultipliedAlpha: false, antialias: true };
    const types = ["webgl2", "webgl", "experimental-webgl"];
    for (const type of types) {
      try {
        const gl = canvas.getContext(type, attrs);
        if (gl) return gl;
      } catch (_e) {
        /* 尝试下一种上下文类型 */
      }
    }
    return null;
  }

  /**
   * 解析 three.js 的 `GLTFLoader` 构造器。three.js 经典（非模块）CDN 构建把核心挂在
   * `window.THREE` 上；GLTFLoader 在不同分发中可能位于 `THREE.GLTFLoader` 或全局
   * `window.GLTFLoader`。两者皆探测，找到即返回，否则返回 null（由 setModel 干净失败）。
   * @param {object} THREE window.THREE。
   * @returns {Function|null}
   */
  function resolveGLTFLoader(THREE) {
    if (THREE && typeof THREE.GLTFLoader === "function") return THREE.GLTFLoader;
    if (typeof global.GLTFLoader === "function") return global.GLTFLoader;
    return null;
  }

  /**
   * 解析 three-vrm 的 `VRMLoaderPlugin` 构造器。@pixiv/three-vrm 的 UMD 构建把其导出
   * 挂在 `window.THREE_VRM` 上；某些分发也可能直接暴露 `window.VRMLoaderPlugin`。
   * 两者皆探测，找到即返回，否则返回 null（由 setModel 干净失败）。
   * @returns {Function|null}
   */
  function resolveVRMLoaderPlugin() {
    const ns = global.THREE_VRM;
    if (ns && typeof ns.VRMLoaderPlugin === "function") return ns.VRMLoaderPlugin;
    if (typeof global.VRMLoaderPlugin === "function") return global.VRMLoaderPlugin;
    return null;
  }

  class Live3DRenderer {
    /**
     * @param {HTMLCanvasElement} canvas 渲染目标画布（RendererManager 提供）。
     * @param {object} [ctx] RendererManager 传入的上下文 { canvas, manager, type }。
     * @throws {Error} 当 `three.js`（window.THREE）缺失或 WebGL 上下文无法创建时——
     *   干净失败而非静默 no-op，使 RendererManager 按 R9 降级（镜像 OrbRenderer）。
     */
    constructor(canvas, ctx) {
      const THREE = global.THREE;
      if (!THREE || typeof THREE.WebGLRenderer !== "function") {
        // three.js 缺失：干净失败，触发 RendererManager 降级（R9）。
        throw new Error(
          "Live3DRenderer requires three.js (window.THREE); dependency missing."
        );
      }
      if (!canvas) {
        throw new Error("Live3DRenderer requires a canvas to render into.");
      }

      // WebGL 可用性探测（R5.3 透明画布需要 WebGL）：无法创建上下文即干净失败。
      const gl = acquireWebGLContext(canvas);
      if (!gl) {
        throw new Error(
          "Live3DRenderer requires a WebGL context; WebGL is unavailable in this browser."
        );
      }

      this._THREE = THREE;
      this.canvas = canvas;
      this.type = "Live3D";

      // --- 接口状态（口型/表情驱动，任务 5.2）-----------------------------
      this._mouthTarget = 0; // setMouthOpen 的 clamp 目标 [0,1]（每帧 update 后写入）。
      this._mouthExpr = "aa"; // 实际驱动的 VRM 口型表情名（加载模型时按可用项探测）。
      this._expressionIndex = null; // 当前已应用的表情索引。
      this._expressionName = "neutral";
      this._activeExprPreset = null; // 当前已置 1 的 VRM 情绪 preset（单活：至多一个）。
      this._pendingExpression = null; // 模型加载完成前请求的表情，加载后补用。

      // --- VRM / 加载态 --------------------------------------------------
      this._vrm = null; // 已加载的 VRM（含 .scene / .update / .expressionManager）。
      this._modelUrl = null; // 当前已成功加载的模型 URL。
      this._loadSeq = 0; // 加载序号：丢弃过期（被后续 setModel 取代）的加载结果。
      this._errorEl = null; // 失败原因文本的 DOM 覆盖层（R5.2）。

      // --- 待机 / 眨眼调度 ------------------------------------------------
      this._speakingUntil = 0; // 「正在说话」的余辉截止时间戳（ms）。
      this._blinkTimer = 0; // 距下次眨眼的累计时间（ms）。
      this._blinkInterval = this._nextBlinkInterval(); // 下次眨眼间隔（ms，[2000,6000]）。
      this._blinkValue = 0; // 当前眨眼权重 [0,1]（1=完全闭眼）。
      this._blinking = false; // 是否处于一次眨眼的闭合衰减中。

      // --- 渲染循环状态 --------------------------------------------------
      this._raf = null;
      this._disposed = false;
      this._lastNow =
        typeof performance !== "undefined" && performance.now
          ? performance.now()
          : Date.now();

      // --- three.js 场景搭建 --------------------------------------------
      try {
        // 透明画布（alpha=0）：复用上面探测得到的 WebGL 上下文（R5.3）。
        this._renderer = new THREE.WebGLRenderer({
          canvas: canvas,
          context: gl,
          alpha: true,
          premultipliedAlpha: false,
          antialias: true,
        });
        const dpr =
          (typeof global !== "undefined" && global.devicePixelRatio) || 1;
        if (typeof this._renderer.setPixelRatio === "function") {
          this._renderer.setPixelRatio(dpr);
        }
        if (typeof this._renderer.setSize === "function") {
          // 第三个参数 false：不改写 canvas 的 CSS 尺寸（由页面布局控制）。
          this._renderer.setSize(canvas.width, canvas.height, false);
        }
        // alpha=0 清屏，使下层页面背景完全透出（R5.3）。
        if (typeof this._renderer.setClearAlpha === "function") {
          this._renderer.setClearAlpha(0);
        }

        this._scene = new THREE.Scene();

        const aspect = canvas.height > 0 ? canvas.width / canvas.height : 1;
        // VRM 通常面向 +Z、身高约 1.0–1.6m；相机略高于半身、稍远以铺满画布。
        this._camera = new THREE.PerspectiveCamera(30, aspect, 0.1, 100);
        if (this._camera.position && typeof this._camera.position.set === "function") {
          this._camera.position.set(0, 1.3, 2.6);
        }
        if (typeof this._camera.lookAt === "function") {
          this._camera.lookAt(0, 1.2, 0);
        }

        // 基础布光（可选——仅当 three.js 构建提供对应构造器时添加；最小 stub 可缺省）。
        if (typeof THREE.DirectionalLight === "function") {
          const dir = new THREE.DirectionalLight(0xffffff, 1.0);
          if (dir.position && typeof dir.position.set === "function") {
            dir.position.set(1, 1.5, 1.5);
          }
          if (this._scene && typeof this._scene.add === "function") {
            this._scene.add(dir);
          }
        }
        if (typeof THREE.AmbientLight === "function") {
          const amb = new THREE.AmbientLight(0xffffff, 0.6);
          if (this._scene && typeof this._scene.add === "function") {
            this._scene.add(amb);
          }
        }
      } catch (err) {
        // three.js 在创建渲染器/上下文阶段抛错：释放已分配资源后干净地重新抛出，
        // 使 RendererManager 降级（R9）。
        this._disposeThree();
        throw err instanceof Error
          ? err
          : new Error("Live3DRenderer initialisation failed: " + String(err));
      }

      // 启动渲染循环（R5.1 ≥30 FPS；真实帧率需真机/可视验证）。
      this._frame = this._frame.bind(this);
      this._start();
    }

    /**
     * 眨眼间隔采样器：返回 [2000,6000] 毫秒区间内的一个值（Requirement 5.5）。
     * 暴露为方法以便属性测试（任务 5.4）多次取样断言区间。
     * @returns {number} 下次眨眼间隔（毫秒）。
     */
    _nextBlinkInterval() {
      return BLINK_MIN_MS + Math.random() * (BLINK_MAX_MS - BLINK_MIN_MS);
    }

    /** 是否处于「正在说话」的余辉窗口内（用于抑制眨眼调度，R5.5）。 */
    _isSpeaking() {
      const now =
        typeof performance !== "undefined" && performance.now
          ? performance.now()
          : Date.now();
      return now < this._speakingUntil;
    }

    /** 启动 requestAnimationFrame 渲染循环。 */
    _start() {
      if (this._disposed) return;
      this._lastNow =
        typeof performance !== "undefined" && performance.now
          ? performance.now()
          : Date.now();
      this._raf = requestAnimationFrame(this._frame);
    }

    /**
     * 每帧渲染回调：推进 VRM 动画、待机摆动与眨眼调度，并渲染场景。
     * @param {number} now requestAnimationFrame 提供的时间戳（ms）。
     */
    _frame(now) {
      if (this._disposed) return;
      const t =
        typeof now === "number"
          ? now
          : typeof performance !== "undefined" && performance.now
          ? performance.now()
          : Date.now();
      const dtMs = Math.min(50, Math.max(0, t - this._lastNow));
      const dt = dtMs / 1000;
      this._lastNow = t;

      // 待机动画（R5.4）：无语音时给模型一个轻微的呼吸/摆动；语音播放期间收敛到中性。
      this._updateIdle(dt, t);
      // 眨眼调度（R5.5）：无语音时按 2–6s 随机间隔驱动 blink。
      this._updateBlink(dtMs);

      // 推进 VRM 内部动画（表情/弹簧骨骼等）。任务 5.2 将在此之后写入 lip-sync 权重。
      if (this._vrm && typeof this._vrm.update === "function") {
        try {
          this._vrm.update(dt);
        } catch (_e) {
          /* safe：单帧更新异常不应中断渲染循环 */
        }
      }
      // 眨眼权重写入放在 vrm.update 之后，避免被 idle/expression 动画覆盖。
      this._applyBlink();
      // lip-sync 口型权重同样在 vrm.update 之后写入：这是 three-vrm 的已知 gotcha——
      // vrm.update(dt) 会推进 idle/expression 动画并把口型权重重新应用一遍，若在 update
      // 之前写入会被其覆盖。镜像 Live2D「在 motion 更新后写口型参数」的策略（R6.1）。
      this._applyMouth();

      if (this._renderer && typeof this._renderer.render === "function") {
        this._renderer.render(this._scene, this._camera);
      }

      this._raf = requestAnimationFrame(this._frame);
    }

    /**
     * 待机摆动（R5.4）：无语音播放时让模型根节点做轻微的左右摆动/呼吸。
     * @param {number} dt 帧间隔（秒）。
     * @param {number} t  当前时间戳（ms）。
     */
    _updateIdle(dt, t) {
      const root = this._vrm && this._vrm.scene;
      if (!root || !root.rotation || !root.position) return;
      if (this._isSpeaking()) return; // 语音播放期间不做额外待机摆动。
      const sec = t / 1000;
      // 轻微的左右摆头与上下呼吸；幅度很小以显自然。
      if (typeof root.rotation.y === "number") {
        root.rotation.y = Math.sin(sec * 0.6) * 0.05;
      }
      if (typeof root.position.y === "number") {
        this._baseY = typeof this._baseY === "number" ? this._baseY : root.position.y;
        root.position.y = this._baseY + Math.sin(sec * 1.2) * 0.01;
      }
    }

    /**
     * 眨眼调度（R5.5）：无语音播放时按 [2000,6000]ms 随机间隔触发一次眨眼，
     * 每次眨眼后重新采样下一间隔。眨眼的视觉衰减在 _applyBlink 中写入。
     * @param {number} dtMs 帧间隔（毫秒）。
     */
    _updateBlink(dtMs) {
      // 推进当前正在进行的眨眼闭合衰减。
      if (this._blinking) {
        const step = dtMs / BLINK_DURATION_MS;
        this._blinkValue = Math.max(0, this._blinkValue - step);
        if (this._blinkValue <= 0) {
          this._blinking = false;
          this._blinkValue = 0;
        }
        return;
      }

      // 语音播放期间不调度眨眼（R5.5 的 WHILE 无语音条件）；累计计时器冻结。
      if (this._isSpeaking()) return;

      this._blinkTimer += dtMs;
      if (this._blinkTimer >= this._blinkInterval) {
        this._blinkTimer = 0;
        this._blinkInterval = this._nextBlinkInterval();
        this._blinking = true;
        this._blinkValue = 1; // 立即闭眼，随后逐帧衰减。
      }
    }

    /** 把当前眨眼权重写入 VRM 表情管理器（若可用）。幂等、不抛错。 */
    _applyBlink() {
      const em = this._vrm && this._vrm.expressionManager;
      if (!em || typeof em.setValue !== "function") return;
      try {
        em.setValue("blink", this._blinkValue);
      } catch (_e) {
        /* 模型可能无 blink 表情：安全忽略 */
      }
    }

    /**
     * 把当前口型目标 `_mouthTarget`（[0,1]，0=完全闭合，1=最大张开）线性写入 VRM 的
     * 口型 blendshape（Requirement 6.1）。**必须在每帧 `vrm.update(dt)` 之后调用**：
     * three-vrm 的 update 会推进 idle/expression 动画并重写表情权重，在其之前写入会被
     * 覆盖（与 Live2D 的 lip-sync-after-motion gotcha 同理）。幂等、不抛错。
     */
    _applyMouth() {
      const em = this._vrm && this._vrm.expressionManager;
      if (!em || typeof em.setValue !== "function") return;
      try {
        em.setValue(this._mouthExpr || "aa", this._mouthTarget);
      } catch (_e) {
        /* 模型可能无该口型表情：安全忽略 */
      }
    }

    /**
     * 在加载完模型时按可用项探测一个「口型张开」表情名（Requirement 6.1）。three-vrm
     * 1.0 的 `expressionManager` 暴露 `getExpression(name)`，据此命中第一个存在的候选；
     * 无法探测（如旧版/最小实现缺少 getExpression）时退回 "aa"（写入经 try/catch 包裹，
     * 模型无此项也安全）。
     * @private
     * @param {object} em VRM expressionManager。
     * @returns {string} 口型表情名。
     */
    _resolveMouthExpression(em) {
      if (em && typeof em.getExpression === "function") {
        for (const c of MOUTH_EXPR_CANDIDATES) {
          try {
            if (em.getExpression(c)) return c;
          } catch (_e) {
            /* 尝试下一个候选 */
          }
        }
      }
      return "aa";
    }

    /**
     * 把情绪名（emotionMap 反查得到的可读名）映射到 VRM 情绪 preset
     * （happy/angry/sad/relaxed/surprised/neutral）。无对应名时返回 null，由调用方按
     * Requirement 6.7 保持当前表情不变。
     * @private
     * @param {number} _index Emotion_Index（保留参数，映射主要依据 name）。
     * @param {string} name 可读情绪名。
     * @returns {string|null} VRM 表情 preset，或无映射时 null。
     */
    _resolveExpressionPreset(_index, name) {
      if (typeof name !== "string" || !name) return null;
      const key = name.trim().toLowerCase();
      const preset = EMOTION_TO_VRM_EXPRESSION[key];
      return preset || null;
    }

    /**
     * 检测 VRM 是否存在某个表情 preset（Requirement 6.7 判定「无对应表情」）。可探测时
     * （`getExpression` 可用）以其结果为准；无法探测时保守地视为存在（写入经 try/catch
     * 包裹，仍然安全）。
     * @private
     */
    _hasExpression(em, preset) {
      if (!em) return false;
      if (typeof em.getExpression === "function") {
        try {
          return !!em.getExpression(preset);
        } catch (_e) {
          return false;
        }
      }
      return true;
    }

    /**
     * 应用一个 VRM 情绪 preset，并维持「单活表情」不变量（Requirement 6.6）：应用新
     * 表情前先把上一表情权重清零，使任意时刻至多一个情绪 preset 的权重大于 0。
     * 若该 preset 在模型中不存在（R6.7）或写入失败，则保持当前表情不变并返回 false。
     * @private
     * @param {string} preset 目标 VRM 表情 preset。
     * @returns {boolean} 是否成功应用。
     */
    _applyExpressionPreset(preset) {
      const em = this._vrm && this._vrm.expressionManager;
      if (!em || typeof em.setValue !== "function") return false;
      // 无对应表情：保持当前不变（R6.7）。
      if (!this._hasExpression(em, preset)) return false;
      // 单活不变量：先清零上一表情权重，再置新表情（R6.6）。
      if (this._activeExprPreset && this._activeExprPreset !== preset) {
        try {
          em.setValue(this._activeExprPreset, 0);
        } catch (_e) {
          /* safe */
        }
      }
      try {
        em.setValue(preset, 1);
      } catch (_e) {
        return false;
      }
      this._activeExprPreset = preset;
      return true;
    }

    // --- Renderer_Interface 方法 -----------------------------------------

    /**
     * 加载/替换 VRM 模型（Requirement 5.1 加载、5.2 失败态、setModel 契约 2.10）。
     *
     * 成功：在 10 秒内用 GLTFLoader + VRMLoaderPlugin 解析 VRM，替换场景中的旧模型并
     * 渲染（返回 `{ ok:true, url }`）。失败（url 无效 / 加载器不可用 / 10s 超时 /
     * 网络失败 / 格式不可解析）：在画布上显示可见的失败原因文本、**保持调用前的渲染
     * 状态不变**（不渲染部分或损坏的模型）、返回 `{ ok:false, error }`，且绝不抛出
     * 未捕获的错误。
     *
     * @param {string} url VRM 模型的 HTTP(S) URL。
     * @returns {Promise<{ok:boolean, url?:string, error?:string}>}
     */
    setModel(url) {
      const seq = ++this._loadSeq;

      if (!url || typeof url !== "string") {
        const error = "invalid VRM url";
        this._showError("Live3D: " + error);
        return Promise.resolve({ ok: false, error: error });
      }

      const GLTFLoaderCtor = resolveGLTFLoader(this._THREE);
      const VRMLoaderPlugin = resolveVRMLoaderPlugin();
      if (typeof GLTFLoaderCtor !== "function" || typeof VRMLoaderPlugin !== "function") {
        const error =
          "VRM loader unavailable (three-vrm / GLTFLoader not loaded)";
        this._showError("Live3D: " + error);
        return Promise.resolve({ ok: false, error: error });
      }

      let loader;
      try {
        loader = new GLTFLoaderCtor();
        if (typeof loader.register === "function") {
          loader.register((parser) => new VRMLoaderPlugin(parser));
        }
      } catch (err) {
        const error = "failed to initialise VRM loader: " + (err && err.message ? err.message : String(err));
        this._showError("Live3D: " + error);
        return Promise.resolve({ ok: false, error: error });
      }

      return this._loadWithTimeout(loader, url, VRM_LOAD_TIMEOUT_MS).then(
        (gltf) => {
          // 丢弃过期加载（期间又发起了新的 setModel）：不改变现状。
          if (this._disposed || seq !== this._loadSeq) {
            return { ok: false, error: "superseded by a newer setModel" };
          }
          const vrm = gltf && gltf.userData && gltf.userData.vrm;
          if (!vrm || !vrm.scene) {
            const error = "parsed asset is not a valid VRM";
            this._showError("Live3D: " + error);
            return { ok: false, error: error };
          }
          // 成功：替换旧模型并接管渲染。仅在此刻清除任何先前的失败文本。
          this._installVrm(vrm);
          this._modelUrl = url;
          this._clearError();
          return { ok: true, url: url };
        },
        (err) => {
          // 超时 / 网络失败 / 解析失败：保持调用前渲染状态不变，显示失败原因文本，
          // 不渲染部分/损坏模型（R5.2），返回失败结果（不抛出未捕获错误）。
          if (this._disposed || seq !== this._loadSeq) {
            return { ok: false, error: "superseded by a newer setModel" };
          }
          const error = err && err.message ? err.message : String(err);
          this._showError("Live3D VRM load failed: " + error);
          return { ok: false, error: error };
        }
      );
    }

    /**
     * 以 10 秒超时包裹 GLTFLoader.load（Requirement 5.1/5.2）。超时即 reject，
     * 不等待底层请求继续（过期结果由 setModel 的 seq 校验丢弃）。
     * @private
     * @param {object} loader GLTFLoader 实例（已注册 VRMLoaderPlugin）。
     * @param {string} url VRM URL。
     * @param {number} timeoutMs 超时毫秒。
     * @returns {Promise<object>} 解析后的 gltf（含 userData.vrm）。
     */
    _loadWithTimeout(loader, url, timeoutMs) {
      return new Promise((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          reject(new Error("VRM load timed out after " + timeoutMs + "ms"));
        }, timeoutMs);

        const onLoad = (gltf) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(gltf);
        };
        const onError = (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          reject(err instanceof Error ? err : new Error(String(err || "VRM load failed")));
        };

        try {
          // GLTFLoader.load(url, onLoad, onProgress, onError)
          loader.load(url, onLoad, undefined, onError);
        } catch (err) {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      });
    }

    /**
     * 安装一个新加载的 VRM：移除并释放旧模型，将新模型加入场景。
     * @private
     * @param {object} vrm three-vrm 的 VRM 实例。
     */
    _installVrm(vrm) {
      // 移除并释放旧 VRM（若有）。
      this._removeVrm();
      this._vrm = vrm;
      this._baseY = undefined;
      // 新模型：重置单活表情追踪，并按可用项探测口型表情名（R6.1）。
      this._activeExprPreset = null;
      this._mouthExpr = this._resolveMouthExpression(vrm.expressionManager);
      if (this._scene && vrm.scene && typeof this._scene.add === "function") {
        try {
          this._scene.add(vrm.scene);
        } catch (_e) {
          /* safe */
        }
      }
      // 补用在模型加载完成前请求的表情（如启动时的 neutral）（R6.6）。
      if (this._pendingExpression) {
        const p = this._pendingExpression;
        this._pendingExpression = null;
        this.setExpression(p.index, p.name);
      }
    }

    /** 从场景移除并释放当前 VRM（若有）。幂等、不抛错。 */
    _removeVrm() {
      const vrm = this._vrm;
      if (!vrm) return;
      if (this._scene && vrm.scene && typeof this._scene.remove === "function") {
        try {
          this._scene.remove(vrm.scene);
        } catch (_e) {
          /* safe */
        }
      }
      // three-vrm 提供 VRMUtils.deepDispose 用于彻底释放；若可用则调用。
      try {
        const ns = global.THREE_VRM;
        if (ns && ns.VRMUtils && typeof ns.VRMUtils.deepDispose === "function") {
          ns.VRMUtils.deepDispose(vrm.scene);
        }
      } catch (_e) {
        /* safe */
      }
      this._vrm = null;
    }

    /**
     * 驱动 VRM 口型 lip-sync 目标（Requirement 6.1–6.4）。做 clamp + 存储目标值
     * （[0,1]，`v<0→0`、`v>1→1`，非数值/NaN/Infinity 时**保持当前值不变**，且不抛错），
     * 并标记「正在说话」以抑制待机/眨眼。实际写入 VRM 口型 blendshape 发生在每帧
     * `vrm.update(dt)` **之后**（`_applyMouth`），以免被 idle/expression 动画覆盖；下一帧
     * （≪100ms）即生效，达成「≤100ms 内线性驱动」（R6.1）。
     * @param {number} v 开合度 [0,1]。
     */
    setMouthOpen(v) {
      if (typeof v === "number" && Number.isFinite(v)) {
        this._mouthTarget = Math.max(0, Math.min(1, v));
        if (this._mouthTarget > 0) {
          const now =
            typeof performance !== "undefined" && performance.now
              ? performance.now()
              : Date.now();
          this._speakingUntil = now + SPEAKING_HOLD_MS;
        }
      }
      // 非数值 / NaN / Infinity：保持当前值，不抛错。
    }

    /**
     * 应用情绪表情（Requirement 6.6/6.7）。经 `name`（emotionMap 反查得到的可读情绪名）
     * 映射到 VRM 情绪 preset（happy/angry/sad/relaxed/surprised/neutral），应用前先把上一
     * 表情权重清零，维持「单活表情」不变量——任意时刻至多一个情绪 preset 权重大于 0
     * （R6.6）。若 `name` 无对应 preset、或该 preset 在模型中不存在，则**保持当前表情不变**
     * 且不抛错（R6.7）。模型尚未加载时记为待应用，加载完成后补用。
     * @param {number} index emotionMap 值（>=0 整数）。
     * @param {string} [name] 可读情绪名（happy/angry/sad/... 用于映射与界面标签）。
     */
    setExpression(index, name) {
      // 无效 Emotion_Index（非 >=0 整数）：保持当前表情不变，不抛错（与 Live2D 一致，
      // Requirement 2.1 约定 index 为 >=0 整数）。
      if (typeof index !== "number" || !Number.isInteger(index) || index < 0) {
        return;
      }
      const preset = this._resolveExpressionPreset(index, name);
      // 无对应 VRM 表情：保持当前表情不变，不抛错（R6.7）。
      if (!preset) return;

      // 模型尚未加载：记下请求，待 _installVrm 后补用（R6.6 启动 neutral 等场景）。
      if (!this._vrm || !this._vrm.expressionManager) {
        this._pendingExpression = { index: index, name: name };
        return;
      }

      const applied = this._applyExpressionPreset(preset);
      if (applied) {
        if (typeof index === "number" && Number.isInteger(index) && index >= 0) {
          this._expressionIndex = index;
        }
        if (typeof name === "string" && name) this._expressionName = name;
      }
      // applied===false（模型无此 preset）：保持当前表情不变，不抛错（R6.7）。
    }

    /** 口型复位：将 lip-sync 目标回到静止态 0（Requirement 6.5；下一帧 ≪100ms 写入 VRM）。 */
    resetMouth() {
      this._mouthTarget = 0;
    }

    /**
     * 播放动作组。本文件为安全占位（VRM 动作/动画的播放可在后续任务按需扩展）：
     * 立即返回、不抛错。
     * @param {string} _group 动作组名。
     */
    playMotion(_group) {
      /* 占位：Live3D 动作播放可选，保持接口安全无副作用 */
    }

    /** 停止渲染循环并释放 VRM / WebGL / 失败文本覆盖层（Requirement 1.4）。 */
    destroy() {
      this._disposed = true;
      if (this._raf != null) {
        try {
          cancelAnimationFrame(this._raf);
        } catch (_e) {
          /* safe */
        }
        this._raf = null;
      }
      this._removeVrm();
      this._clearError();
      this._disposeThree();
    }

    // --- 失败文本覆盖层（R5.2：在画布上显示可见的失败原因文本）-------------

    /**
     * 在画布上方显示一条可见的失败原因文本（Requirement 5.2）。WebGL 画布本身不便
     * 绘制文本，故用一个绝对定位、覆盖在画布之上的 DOM 元素承载，确保失败原因「在
     * 画布上」可见，同时保留页面其余功能可正常交互。幂等：重复调用更新文本。
     * @param {string} msg 失败原因文本。
     */
    _showError(msg) {
      const doc = this.canvas && this.canvas.ownerDocument
        ? this.canvas.ownerDocument
        : (typeof document !== "undefined" ? document : null);
      if (!doc || typeof doc.createElement !== "function") return;
      try {
        if (!this._errorEl) {
          const el = doc.createElement("div");
          el.className = "live3d-error";
          el.setAttribute("role", "alert");
          const s = el.style;
          if (s) {
            s.position = "absolute";
            s.left = "0";
            s.top = "0";
            s.right = "0";
            s.padding = "8px 10px";
            s.font = "13px sans-serif";
            s.color = "#ff6b6b";
            s.background = "rgba(0,0,0,0.55)";
            s.whiteSpace = "pre-wrap";
            s.pointerEvents = "none";
            s.zIndex = "5";
          }
          const parent = this.canvas && this.canvas.parentNode
            ? this.canvas.parentNode
            : (doc.body || null);
          if (parent && typeof parent.appendChild === "function") {
            parent.appendChild(el);
          }
          this._errorEl = el;
        }
        this._errorEl.textContent = String(msg);
      } catch (_e) {
        /* 显示失败提示本身不应抛错 */
      }
    }

    /** 移除失败原因文本覆盖层（若有）。幂等、不抛错。 */
    _clearError() {
      const el = this._errorEl;
      if (!el) return;
      try {
        if (el.parentNode && typeof el.parentNode.removeChild === "function") {
          el.parentNode.removeChild(el);
        }
      } catch (_e) {
        /* safe */
      }
      this._errorEl = null;
    }

    /** 释放 three.js 资源（renderer / scene / camera）。幂等、不抛错。 */
    _disposeThree() {
      if (this._renderer && typeof this._renderer.dispose === "function") {
        try {
          this._renderer.dispose();
        } catch (_e) {
          /* safe */
        }
      }
      this._renderer = null;
      this._scene = null;
      this._camera = null;
    }
  }

  // 与现有 window.CanvasAvatar / window.Live2DAvatar / window.OrbRenderer /
  // window.RendererManager 一致，挂为全局，供 RendererManager 的默认工厂
  // `new window.Live3DRenderer(canvas, ctx)` 构造。
  global.Live3DRenderer = Live3DRenderer;
})(window);
