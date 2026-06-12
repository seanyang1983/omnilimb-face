/*
 * omnilimb-face reference front-end — OrbRenderer (three.js 粒子球 + 声波律动).
 *
 * `OrbRenderer` 是「Orb」渲染器：用 three.js 在提供的 <canvas> 上渲染一个由粒子组成
 * 的发光球体，背景透明（alpha=0）使下层页面背景完全透出。它不是人形形象，而是一个
 * 抽象的「声音可视化」形象——会随 lip-sync 口型值做**声波律动**（粒子沿径向脉动、整体
 * 缩放呼吸），并随 Agent_State（空闲/聆听/思考/说话）切换**配色与角标**。它实现统一的
 * `Renderer_Interface`（见 renderer-manager.js），因此协议层（app.js / protocol.js）可用
 * 与其它渲染器相同的信号驱动它，而无需判断其具体类型。
 *
 * ============================================================================
 * 接口语义
 * ============================================================================
 *   - `setMouthOpen(v)`：clamp 到 [0,1] 后驱动声波脉动幅度（v 越大粒子外扩越明显、
 *     整体微微放大）；非数值/NaN/Infinity 时保持当前值不变、不抛错（Requirement 6.1–6.4）。
 *   - `resetMouth()`：声波幅度立即回到静止基线（_deformScale=1, _mouthTarget=0）（R6.5）。
 *   - `setExpression(index, name)`：Orb 不支持表情 → 安全 no-op（R12.9）。
 *   - `setModel(url)`：Orb 无模型文件 → 安全 no-op（R12.9）。
 *   - `playMotion(group)`：Orb 无动作组 → 安全 no-op（R12.9）。
 *   - `setAgentState(state)`：可选方法。state ∈ {idle,listening,thinking,speaking} 时切换
 *     球体配色 / 旋转速度 / 角标文字与颜色（Requirement 12.2/12.5）；非法值保持当前
 *     视觉与角标不变、不抛错（Requirement 12.6）。
 *   - `destroy()`：停止渲染循环并释放 three.js 资源与角标覆盖层（幂等、不抛错）。
 *
 * 依赖缺失时「干净失败」：构造函数检测到 `three.js`（window.THREE）缺失或浏览器 WebGL
 * 上下文无法创建时**抛出错误**（而非静默 no-op），使 RendererManager 按 Requirement 9
 * 降级（回退到 CanvasAvatar / Live2D）。镜像 Live3DRenderer 的语义。
 */

(function (global) {
  "use strict";

  // 球面粒子数量（足够密集以显出球体轮廓，又不过载低端 GPU）。
  const PARTICLE_COUNT = 1400;
  // 球体基础半径（three.js 世界单位）。
  const BASE_RADIUS = 1.0;
  // 声波脉动的最大径向位移（按 mouth 值线性，叠加在 BASE_RADIUS 上）。
  const WAVE_MAX_AMPLITUDE = 0.35;
  // 整体缩放的「呼吸」上限：说话（mouth=1）时球体最多放大到此倍数。
  const DEFORM_MAX_SCALE = 1.18;
  // _deformScale 朝目标缓动的每帧系数（指数平滑，越大越跟手）。
  const DEFORM_EASE = 0.18;

  // 合法 Agent_State 枚举（Requirement 12.2）。
  const AGENT_STATES = ["idle", "listening", "thinking", "speaking"];

  // 每个 Agent_State 的视觉配置：粒子颜色（0xRRGGBB）、基础旋转速度（弧度/秒）、
  // 自发脉动（无语音时的轻微律动幅度）、角标文字与角标颜色（Requirement 12.5）。
  const STATE_VISUALS = {
    idle: { color: 0x6ea8ff, spin: 0.18, idlePulse: 0.04, label: "空闲", labelColor: "#6ea8ff" },
    listening: { color: 0x4fd6a0, spin: 0.35, idlePulse: 0.10, label: "聆听", labelColor: "#4fd6a0" },
    thinking: { color: 0xffb454, spin: 0.55, idlePulse: 0.14, label: "思考", labelColor: "#ffb454" },
    speaking: { color: 0xff6b9d, spin: 0.30, idlePulse: 0.06, label: "说话", labelColor: "#ff6b9d" },
  };

  /**
   * 探测并创建一个透明 WebGL 渲染上下文（镜像 Live3DRenderer.acquireWebGLContext）。
   * 按 webgl2 → webgl → experimental-webgl 优先级尝试；全部失败返回 null。
   * @param {HTMLCanvasElement} canvas 目标画布。
   * @returns {WebGLRenderingContext|null}
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

  class OrbRenderer {
    /**
     * @param {HTMLCanvasElement} canvas 渲染目标画布（RendererManager 提供）。
     * @param {object} [ctx] RendererManager 传入的上下文 { canvas, manager, type }。
     * @throws {Error} 当 `three.js`（window.THREE）缺失或 WebGL 上下文无法创建时——
     *   干净失败而非静默 no-op，使 RendererManager 按 R9 降级（镜像 Live3DRenderer）。
     */
    constructor(canvas, ctx) {
      const THREE = global.THREE;
      if (!THREE || typeof THREE.WebGLRenderer !== "function") {
        throw new Error(
          "OrbRenderer requires three.js (window.THREE); dependency missing."
        );
      }
      if (!canvas) {
        throw new Error("OrbRenderer requires a canvas to render into.");
      }
      const gl = acquireWebGLContext(canvas);
      if (!gl) {
        throw new Error(
          "OrbRenderer requires a WebGL context; WebGL is unavailable in this browser."
        );
      }

      this._THREE = THREE;
      this.canvas = canvas;
      this.type = "Orb";

      // --- 接口状态 ------------------------------------------------------
      this._mouthTarget = 0; // setMouthOpen 的 clamp 目标 [0,1]（声波幅度的驱动源）。
      this._deformScale = 1; // 当前整体缩放（朝 1 + mouth*(DEFORM_MAX_SCALE-1) 缓动）。
      this._agentState = "idle"; // 当前 Agent_State（默认空闲）。
      this._stateVisual = STATE_VISUALS.idle; // 当前状态视觉配置。
      this._label = null; // 角标 DOM 覆盖层（空闲/聆听/思考/说话）。

      // --- 渲染循环状态 --------------------------------------------------
      this._raf = null;
      this._disposed = false;
      this._spin = 0; // 累计自转角（弧度）。
      this._lastNow = this._now();

      // --- three.js 场景搭建 --------------------------------------------
      try {
        this._renderer = new THREE.WebGLRenderer({
          canvas: canvas,
          context: gl,
          alpha: true,
          premultipliedAlpha: false,
          antialias: true,
        });
        const dpr = (typeof global !== "undefined" && global.devicePixelRatio) || 1;
        if (typeof this._renderer.setPixelRatio === "function") {
          this._renderer.setPixelRatio(dpr);
        }
        if (typeof this._renderer.setSize === "function") {
          this._renderer.setSize(canvas.width, canvas.height, false);
        }
        if (typeof this._renderer.setClearAlpha === "function") {
          this._renderer.setClearAlpha(0); // 透明清屏，露出页面背景（R5.3 同款）。
        }

        this._scene = new THREE.Scene();

        const aspect = canvas.height > 0 ? canvas.width / canvas.height : 1;
        this._camera = new THREE.PerspectiveCamera(45, aspect, 0.1, 100);
        if (this._camera.position && typeof this._camera.position.set === "function") {
          this._camera.position.set(0, 0, 3.2);
        }
        if (typeof this._camera.lookAt === "function") {
          this._camera.lookAt(0, 0, 0);
        }

        // 生成球面粒子（fibonacci 球面分布，均匀且无极点聚集）。基础方向向量保存在
        // _dirs（单位向量数组），每帧据此 + 声波幅度重算粒子位置写入 _positions。
        this._buildParticles();
      } catch (err) {
        this._disposeThree();
        throw err instanceof Error
          ? err
          : new Error("OrbRenderer initialisation failed: " + String(err));
      }

      // 初始角标。
      this._renderLabel();

      // 启动渲染循环。
      this._frame = this._frame.bind(this);
      this._start();
    }

    /** 高精度时间源（ms），回退到 Date.now。 */
    _now() {
      return typeof performance !== "undefined" && performance.now
        ? performance.now()
        : Date.now();
    }

    /**
     * 生成球面粒子几何（fibonacci 球面）。保存单位方向向量 `_dirs` 与可写位置缓冲
     * `_positions`（Float32Array），把 position attribute 接到几何上并构造 THREE.Points。
     * 与最小 THREE stub 兼容：所有 three.js 对象方法调用均经 typeof 守卫。
     * @private
     */
    _buildParticles() {
      const THREE = this._THREE;
      const n = PARTICLE_COUNT;
      const dirs = new Float32Array(n * 3);
      const positions = new Float32Array(n * 3);
      const golden = Math.PI * (3 - Math.sqrt(5)); // 黄金角
      for (let i = 0; i < n; i++) {
        const y = 1 - (i / (n - 1)) * 2; // y ∈ [1,-1]
        const r = Math.sqrt(Math.max(0, 1 - y * y));
        const theta = golden * i;
        const x = Math.cos(theta) * r;
        const z = Math.sin(theta) * r;
        dirs[i * 3] = x;
        dirs[i * 3 + 1] = y;
        dirs[i * 3 + 2] = z;
        positions[i * 3] = x * BASE_RADIUS;
        positions[i * 3 + 1] = y * BASE_RADIUS;
        positions[i * 3 + 2] = z * BASE_RADIUS;
      }
      this._dirs = dirs;
      this._positions = positions;

      const geometry = new THREE.BufferGeometry();
      // Float32BufferAttribute(array, itemSize)：真实 three 下其构造器会 new Float32Array(array)
      // **复制**一份数组，故必须改写 attribute 自身的 buffer（`_posAttr.array`），否则
      // 每帧的声波位移写到我们这份副本上、不会上传，粒子涟漪在真实浏览器里不会动（jsdom
      // 的最小 stub 不会暴露此问题）。stub 下 _posAttr 无 .array，则继续用我们这份。
      const PosAttr = THREE.Float32BufferAttribute || THREE.BufferAttribute;
      this._posAttr = new PosAttr(positions, 3);
      if (this._posAttr && this._posAttr.array) {
        this._positions = this._posAttr.array; // 指向 attribute 真正上传的 buffer
      }
      if (geometry && typeof geometry.setAttribute === "function") {
        geometry.setAttribute("position", this._posAttr);
      }
      this._geometry = geometry;

      const material = new THREE.PointsMaterial({
        color: this._stateVisual.color,
        size: 0.045,
        sizeAttenuation: true,
        transparent: true,
        opacity: 0.9,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      this._material = material;

      this._points = new THREE.Points(geometry, material);
      if (this._scene && this._points && typeof this._scene.add === "function") {
        this._scene.add(this._points);
      }
    }

    /** 启动 requestAnimationFrame 渲染循环。 */
    _start() {
      if (this._disposed) return;
      this._lastNow = this._now();
      this._raf = requestAnimationFrame(this._frame);
    }

    /**
     * 每帧：缓动整体缩放、按声波幅度重算粒子径向位移、自转，并渲染。
     * @param {number} now requestAnimationFrame 时间戳（ms）。
     */
    _frame(now) {
      if (this._disposed) return;
      const t = typeof now === "number" ? now : this._now();
      const dtMs = Math.min(50, Math.max(0, t - this._lastNow));
      const dt = dtMs / 1000;
      this._lastNow = t;

      // 整体缩放朝目标（1 + mouth 驱动）指数缓动，形成「呼吸/律动」。
      const targetScale = 1 + this._mouthTarget * (DEFORM_MAX_SCALE - 1);
      this._deformScale += (targetScale - this._deformScale) * DEFORM_EASE;

      // 自转：速度由当前 Agent_State 决定（思考最快）。
      this._spin += this._stateVisual.spin * dt;

      this._updateWave(t);
      this._applyTransform();

      if (this._renderer && typeof this._renderer.render === "function") {
        this._renderer.render(this._scene, this._camera);
      }
      this._raf = requestAnimationFrame(this._frame);
    }

    /**
     * 声波律动：每帧沿每个粒子的单位方向把半径调制为
     *   r = BASE_RADIUS * (1 + wave)
     * 其中 wave = (idlePulse + mouth*WAVE_MAX_AMPLITUDE) * sin(相位)，相位随时间与粒子
     * 的纵向位置（y）流动，形成沿球面扩散的「声波」。写入可写位置缓冲并标记需更新。
     * 与最小 THREE stub 兼容（stub 下 _posAttr 无 needsUpdate 字段，赋值无害）。
     * @private
     * @param {number} t 当前时间戳（ms）。
     */
    _updateWave(t) {
      const dirs = this._dirs;
      const pos = this._positions;
      if (!dirs || !pos) return;
      const sec = t / 1000;
      const amp = this._stateVisual.idlePulse + this._mouthTarget * WAVE_MAX_AMPLITUDE;
      const n = pos.length / 3;
      for (let i = 0; i < n; i++) {
        const dx = dirs[i * 3];
        const dy = dirs[i * 3 + 1];
        const dz = dirs[i * 3 + 2];
        // 相位：时间流动 + 纵向波纹，使声波沿球面上下扩散。
        const phase = sec * 3.2 + dy * 6.0;
        const wave = amp * Math.sin(phase);
        const r = BASE_RADIUS * (1 + wave);
        pos[i * 3] = dx * r;
        pos[i * 3 + 1] = dy * r;
        pos[i * 3 + 2] = dz * r;
      }
      if (this._posAttr) this._posAttr.needsUpdate = true;
    }

    /** 应用自转与整体缩放到粒子对象（经 typeof 守卫，兼容 stub）。 */
    _applyTransform() {
      const p = this._points;
      if (!p) return;
      if (p.rotation && typeof p.rotation.y === "number") {
        p.rotation.y = this._spin;
      }
      if (p.scale && typeof p.scale.set === "function") {
        p.scale.set(this._deformScale, this._deformScale, this._deformScale);
      }
    }

    // --- Renderer_Interface 方法 -----------------------------------------

    /**
     * 驱动声波律动幅度（Requirement 6.1–6.4）。clamp 到 [0,1] 后存为脉动驱动源；
     * 非数值/NaN/Infinity 时保持当前值不变、不抛错。实际视觉在每帧 `_updateWave` /
     * `_applyTransform` 中应用（下一帧 ≪100ms 即生效）。
     * @param {number} v 开合度 [0,1]。
     */
    setMouthOpen(v) {
      if (typeof v === "number" && Number.isFinite(v)) {
        this._mouthTarget = Math.max(0, Math.min(1, v));
      }
      // 非数值 / NaN / Infinity：保持当前值，不抛错。
    }

    /** Orb 不支持表情：安全 no-op（Requirement 12.9）。 */
    setExpression(_index, _name) {
      /* no-op：立即返回、不改变任何可观察状态、不抛错 */
    }

    /** 声波幅度复位到静止基线（Requirement 6.5）。 */
    resetMouth() {
      this._mouthTarget = 0;
      this._deformScale = 1;
    }

    /** Orb 无模型文件：安全 no-op（Requirement 12.9）。 */
    setModel(_url) {
      /* no-op：Orb 无可加载的模型来源 */
    }

    /** Orb 无动作组：安全 no-op（Requirement 12.9）。 */
    playMotion(_group) {
      /* no-op */
    }

    /**
     * 切换 Agent_State 的视觉与角标（Requirement 12.2/12.5）。state 合法（idle/
     * listening/thinking/speaking）时更新配色、旋转速度与角标文字/颜色；非法值保持
     * 当前视觉与角标不变、不抛错（Requirement 12.6）。
     * @param {string} state Agent_State。
     */
    setAgentState(state) {
      if (AGENT_STATES.indexOf(state) === -1) return; // 非法：保持不变，不抛错。
      if (state === this._agentState) return;
      this._agentState = state;
      this._stateVisual = STATE_VISUALS[state];
      // 更新粒子颜色（经 typeof 守卫，兼容 stub）。
      const mat = this._material;
      if (mat && mat.color && typeof mat.color.set === "function") {
        try {
          mat.color.set(this._stateVisual.color);
        } catch (_e) {
          /* safe */
        }
      } else if (mat && typeof mat.color === "object" && mat.color) {
        // 某些构建下 color 是带 setHex 的对象。
        if (typeof mat.color.setHex === "function") {
          try { mat.color.setHex(this._stateVisual.color); } catch (_e) { /* safe */ }
        }
      }
      this._renderLabel();
    }

    /** 停止渲染循环并释放 three.js 资源与角标覆盖层（Requirement 1.4）。幂等、不抛错。 */
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
      this._clearLabel();
      this._disposeThree();
    }

    // --- 状态角标覆盖层（Requirement 12.5）-------------------------------

    /**
     * 在画布上方渲染当前 Agent_State 的可读中文角标（空闲/聆听/思考/说话）。WebGL 画布
     * 不便绘制文本，故用一个绝对定位、覆盖在画布之上的 DOM 元素承载。幂等：更新文字与
     * 颜色。样式类 `orb-state-label` 由 styles.css 提供 token 化基线（含内联兜底）。
     * @private
     */
    _renderLabel() {
      const doc =
        this.canvas && this.canvas.ownerDocument
          ? this.canvas.ownerDocument
          : typeof document !== "undefined"
          ? document
          : null;
      if (!doc || typeof doc.createElement !== "function") return;
      try {
        if (!this._label) {
          const el = doc.createElement("div");
          el.className = "orb-state-label badge";
          const s = el.style;
          if (s) {
            s.position = "absolute";
            s.left = "50%";
            s.top = "8px";
            s.transform = "translateX(-50%)";
            s.padding = "4px 12px";
            s.borderRadius = "999px";
            s.font = "13px sans-serif";
            s.background = "rgba(0,0,0,0.45)";
            s.pointerEvents = "none";
            s.zIndex = "5";
          }
          const parent =
            this.canvas && this.canvas.parentNode
              ? this.canvas.parentNode
              : doc.body || null;
          if (parent && typeof parent.appendChild === "function") {
            parent.appendChild(el);
          }
          this._label = el;
        }
        this._label.textContent = this._stateVisual.label;
        if (this._label.style) this._label.style.color = this._stateVisual.labelColor;
      } catch (_e) {
        /* 角标渲染本身不应抛错 */
      }
    }

    /** 移除状态角标覆盖层（若有）。幂等、不抛错。 */
    _clearLabel() {
      const el = this._label;
      if (!el) return;
      try {
        if (el.parentNode && typeof el.parentNode.removeChild === "function") {
          el.parentNode.removeChild(el);
        }
      } catch (_e) {
        /* safe */
      }
      this._label = null;
    }

    /** 释放 three.js 资源（renderer / geometry / material）。幂等、不抛错。 */
    _disposeThree() {
      if (this._geometry && typeof this._geometry.dispose === "function") {
        try { this._geometry.dispose(); } catch (_e) { /* safe */ }
      }
      if (this._material && typeof this._material.dispose === "function") {
        try { this._material.dispose(); } catch (_e) { /* safe */ }
      }
      if (this._renderer && typeof this._renderer.dispose === "function") {
        try { this._renderer.dispose(); } catch (_e) { /* safe */ }
      }
      this._geometry = null;
      this._material = null;
      this._points = null;
      this._renderer = null;
      this._scene = null;
      this._camera = null;
    }
  }

  // 与现有 window.CanvasAvatar / window.Live2DAvatar / window.Live3DRenderer /
  // window.RendererManager 一致，挂为全局，供 RendererManager 的默认工厂
  // `new window.OrbRenderer(canvas, ctx)` 构造。
  global.OrbRenderer = OrbRenderer;
})(window);
