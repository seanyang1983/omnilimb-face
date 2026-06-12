/*
 * omnilimb-face reference front-end — RendererManager (renderer lifecycle hub).
 *
 * 本文件引入「可切换形象渲染器」特性的中枢 `RendererManager`，并集中文档化所有
 * 渲染器共享的隐式接口契约 `Renderer_Interface`。
 *
 * ============================================================================
 * Renderer_Interface（统一渲染器接口 — 隐式 duck-typing 契约，plain JS 无 TS）
 * ============================================================================
 *
 * 现有 `CanvasAvatar` 与 `Live2DAvatar`（见 avatar.js），以及 `Live3DRenderer`
 * （见 live3d-renderer.js），都实现同一套方法，
 * 使协议层（app.js / protocol.js）能用相同的信号驱动「当前激活的渲染器」而无需
 * 判断其具体类型。所有方法均为「安全」方法：非法输入不抛出未捕获的错误。
 *
 *   setMouthOpen(v)         // v 为 number，取值 [0,1]：0=完全闭合，1=完全张开。
 *                           //   越界 clamp 到最近边界；NaN/null/undefined/非
 *                           //   number 时保持当前口型值不变。驱动 lip-sync。
 *   setExpression(index, name) // index 为 >=0 的整数（emotionMap 值），name 为
 *                           //   可读情绪名（用于界面标签）。无对应表情时保持当前
 *                           //   表情不变。不支持表情的渲染器实现为 no-op。
 *   resetMouth()            // 口型回到静止态（完全闭合 / 基线）。
 *   setModel(url)           // 加载/替换模型；url 为非空字符串。失败时保持调用前
 *                           //   状态不变并返回指示失败的结果，不抛未捕获错误。
 *                           //   可返回 Promise（Live2D/Live3D）或同步结果。
 *   playMotion(group)       // 播放动作组（group 为非空字符串）。不支持时 no-op。
 *   destroy()               // 释放资源（ticker / WebGL / 视频流 / 子进程连接）。
 *
 *   setAgentState(state)    // 可选方法。state ∈ {idle,listening,thinking,speaking}。
 *                           //   渲染器可实现为 no-op 或不实现，
 *                           //   由 RendererManager 在转发前用 typeof 守卫。
 *
 * 可选就绪契约（readiness contract，供 switchTo/awaitReady 使用）：
 *   渲染器 MAY 暴露 `whenReady()`（返回 Promise）或 `ready`（一个 Promise 属性），
 *   表示「实例已可接收 Audio_Event 调用」的就绪态。该就绪态**不含**模型加载
 *   （Live3D VRM）——它有独立的超时与
 *   降级路径。若两者都未暴露，则实例在构造完成后即视为就绪。
 *
 * no-op 的统一语义：立即返回 + 不修改任何可观察的渲染状态 + 不抛出错误。
 *
 * ----------------------------------------------------------------------------
 * RendererManager
 * ----------------------------------------------------------------------------
 *
 * `RendererManager` 是渲染器生命周期与信号路由的单一中枢，取代 app.js 原先的裸
 * `avatar` 变量。它持有「当前激活渲染器」（`this.active`）与其类型（`this.type`），
 * 并对外暴露与 `Renderer_Interface` 同形的转发方法，使 app.js 的 AudioPlayer 等
 * 调用点无需改动语义即可继续工作。
 *
 * 本文件实现（任务 1.1 + 2.1 + 2.2 范围）：
 *   - 接口同形转发 + 无激活时 no-op（任务 1.1）。
 *   - 运行时切换 `switchTo(type)`、就绪判定 `awaitReady()`、销毁旧实例与信号路由
 *     （任务 2.1）。
 *   - 一个按类型构造渲染器的可插拔工厂注册表（factory registry）。
 *   - 降级链 `degrade()`：有序兜底 Live2D → CanvasAvatar → 纯文本/语音（任务 2.2）。
 *     末级显示「形象渲染不可用」提示但对话继续。
 *
 * 尚未在本文件实现（留待后续任务）：
 *   - `resolveRenderer()` 归一化与 SETTINGS/localStorage 持久化、外观选择控件
 *     （任务 2.3）。
 */

(function (global) {
  "use strict";

  // 两种合法渲染器类型（Requirement 1.1）。归一化/选择控件复用之。
  const RENDERER_TYPES = ["Live2D", "Live3D"];

  // 非 Live2D 渲染器的默认工厂：从 window 全局构造（Live3DRenderer 挂为 window
  // 全局）。Live2D 的选择逻辑（Live2DAvatar vs CanvasAvatar 回退）留在 app.js 的
  // createAvatar() 中，由 app.js 通过 register("Live2D", ...) 注入。
  const DEFAULT_GLOBAL_RENDERERS = {
    Live3D: "Live3DRenderer",
  };

  // switchTo 的就绪时限（Requirement 11.4）：新实例须在 3000ms 内进入可接收
  // Audio_Event 调用的就绪态（不含模型/视频流加载）。
  const DEFAULT_READY_TIMEOUT_MS = 3000;

  class RendererManager {
    /**
     * @param {object} [options]
     * @param {object} [options.canvas] 所有渲染器共享接管的画布/舞台容器。
     * @param {function} [options.notify] 用户可见提示回调 (message, meta) => void，
     *   用于「切换失败」「回退」等提示（Requirement 1.5/11.6）。
     * @param {function} [options.stopDriving] 停止旧渲染器口型/表情驱动的应用层钩子
     *   (prevType, prevRenderer) => void（Requirement 11.2，如取消 lip-sync rAF）。
     * @param {function} [options.onDegrade] 可选的降级 override 钩子；若提供则
     *   `degrade()` 委托之，否则运行内置降级链（任务 2.2）。
     * @param {function} [options.canvasFactory] 降级链 CanvasAvatar 级的构造工厂
     *   (ctx) => CanvasAvatar 实例；默认从 window.CanvasAvatar 构造。
     * @param {object}   [options.factories] 可选的 type→factory 初始注册表。
     * @param {number}   [options.readyTimeoutMs] 就绪时限，默认 3000ms。
     */
    constructor(options = {}) {
      // 当前激活的渲染器实例（一个实现 Renderer_Interface 的对象）。
      this.active = null;
      // 当前激活渲染器的类型："Live2D" | "Live3D"。
      this.type = null;

      // 可插拔的「类型 → 工厂函数」注册表。工厂签名：(ctx) => rendererInstance，
      // ctx = { canvas, manager, type }。后续任务只需 register() 自己的构造器。
      this._factories = {};

      // 防重入：switchTo 进行中标记，避免并发切换互相踩踏。
      this._switching = false;

      // 纯文本/语音（text/voice）兜底模式标记（降级链末级，任务 2.2）。为 true 时
      // 无激活渲染器（active === null），但对话仍继续——所有渲染路由变为安全 no-op，
      // 且本管理器从不拆除 STT/TTS/persona/barge-in 等子系统（R9.4、R9.6）。
      this.textVoiceMode = false;

      this.configure(options);

      if (options.factories && typeof options.factories === "object") {
        for (const [type, factory] of Object.entries(options.factories)) {
          this.register(type, factory);
        }
      }
    }

    /**
     * late-binding 配置（app.js 先 new 后接线时使用）。仅覆盖显式提供的字段。
     * @param {object} [options] 见 constructor。
     * @returns {RendererManager} this（便于链式调用）。
     */
    configure(options = {}) {
      if ("canvas" in options) this._canvas = options.canvas;
      if (typeof options.notify === "function") this._notify = options.notify;
      if (typeof options.stopDriving === "function") this._stopDriving = options.stopDriving;
      if (typeof options.onDegrade === "function") this._onDegrade = options.onDegrade;
      // 降级链的 CanvasAvatar 级构造工厂（可选注入；默认从 window.CanvasAvatar 构造）。
      if (typeof options.canvasFactory === "function") this._canvasFactory = options.canvasFactory;
      if (typeof options.readyTimeoutMs === "number" && options.readyTimeoutMs > 0) {
        this._readyTimeoutMs = options.readyTimeoutMs;
      }
      if (!this._canvas) this._canvas = this._canvas || null;
      if (typeof this._notify !== "function") this._notify = function () {};
      if (typeof this._stopDriving !== "function") this._stopDriving = function () {};
      if (typeof this._readyTimeoutMs !== "number") this._readyTimeoutMs = DEFAULT_READY_TIMEOUT_MS;
      return this;
    }

    /**
     * 注册某类型的渲染器构造工厂（可插拔）。Live3D 或 app.js（Live2D 经
     * createAvatar）调用此方法注入构造器。
     * @param {string} type RENDERER_TYPES 之一。
     * @param {function} factory (ctx) => rendererInstance。
     * @returns {RendererManager} this。
     */
    register(type, factory) {
      if (RENDERER_TYPES.indexOf(type) === -1) {
        throw new Error("RendererManager.register: unknown renderer type: " + type);
      }
      if (typeof factory !== "function") {
        throw new Error("RendererManager.register: factory must be a function for type: " + type);
      }
      this._factories[type] = factory;
      return this;
    }

    /**
     * 采用一个已构造好的渲染器作为当前激活实例（初始化接线用）。
     * 完整生命周期由 switchTo() 管理；adopt 仅做初始接管，不销毁旧实例。
     * @param {object} renderer 实现 Renderer_Interface 的渲染器实例。
     * @param {string} type 渲染器类型标签（RENDERER_TYPES 之一）。
     * @returns {object|null} 被采用的渲染器实例。
     */
    adopt(renderer, type) {
      this.active = renderer || null;
      this.type = type || null;
      return this.active;
    }

    // --- 切换 / 就绪 / 降级 ----------------------------------------------

    /**
     * 按类型构造一个新渲染器实例。Live2D 走注册工厂（app.js 的 createAvatar）；
     * Live3D 若无注册工厂则回退到 window 全局构造，缺失即视为
     * 构造失败（抛错），由 switchTo 捕获并触发回退/降级。
     * @param {string} type RENDERER_TYPES 之一。
     * @returns {object} 新构造的渲染器实例。
     * @throws {Error} 当无可用工厂/全局构造器或构造抛错时。
     */
    _construct(type, canvas) {
      if (RENDERER_TYPES.indexOf(type) === -1) {
        throw new Error("RendererManager: unknown renderer type: " + type);
      }
      const ctx = { canvas: canvas || this._canvas, manager: this, type: type };
      const factory = this._factories[type];
      if (typeof factory === "function") {
        const inst = factory(ctx);
        if (!inst) throw new Error("RendererManager: factory for '" + type + "' returned no instance");
        return inst;
      }
      // 默认工厂：从 window 全局构造非 Live2D 渲染器。
      const globalName = DEFAULT_GLOBAL_RENDERERS[type];
      const Ctor = globalName && typeof global !== "undefined" ? global[globalName] : undefined;
      if (typeof Ctor !== "function") {
        throw new Error(
          type + " renderer unavailable (no registered factory and window." +
            (globalName || type) + " not loaded)"
        );
      }
      const inst = new Ctor(ctx.canvas, ctx);
      if (!inst) throw new Error("RendererManager: window." + globalName + " produced no instance");
      return inst;
    }

    // --- 画布管理（每次切换给新渲染器一块全新的 <canvas>）-----------------
    // 关键约束：一块 <canvas> 的上下文类型（2D / WebGL）一旦绑定就无法更改，也无法
    // 从 WebGL 退回 2D。因此 Live3D 绑定 WebGL 后，再切回 Live2D/Canvas 会拿不到
    // 2D 或全新 WebGL 上下文而崩溃。解决办法：每次切换都为新渲染器创建一块**全新的**
    // canvas，构造/就绪成功后再把它替换进 DOM（失败则丢弃、旧 canvas 原样保留，保证
    // 回退干净）。在无真实 DOM 的测试环境下（canvas 无 ownerDocument）退化为复用原
    // canvas，行为与改动前一致。

    /** 是否为可在 DOM 中替换的真实 canvas 元素（测试用的桩对象返回 false）。 */
    _isDomCanvas(c) {
      return !!(c && c.ownerDocument && typeof c.ownerDocument.createElement === "function");
    }

    /**
     * 创建一块与当前 canvas 同尺寸/同类名/同内联样式的全新 <canvas>（尚未插入 DOM）。
     * 无真实 DOM 时返回当前 canvas（复用，测试环境）。
     * @private
     */
    _makeFreshCanvas() {
      const old = this._canvas;
      if (!this._isDomCanvas(old)) return old;
      const c = old.ownerDocument.createElement("canvas");
      if (typeof old.width === "number") c.width = old.width;
      if (typeof old.height === "number") c.height = old.height;
      c.className = old.className;
      // 保留内联样式（如 avatar scale 的 transform），切换后视觉不跳变。
      if (old.style && typeof old.style.cssText === "string") {
        c.style.cssText = old.style.cssText;
      }
      return c;
    }

    /**
     * 提交画布替换：把全新 canvas 替换进 DOM 中旧 canvas 的位置（继承其 id），并更新
     * this._canvas。仅在新渲染器构造+就绪成功后调用。无真实 DOM 或同一对象时跳过。
     * @private
     */
    _commitCanvas(fresh) {
      const old = this._canvas;
      if (!fresh || fresh === old || !this._isDomCanvas(old)) {
        if (fresh) this._canvas = fresh;
        return;
      }
      if (old.parentNode) {
        if (old.id) {
          fresh.id = old.id;
          old.removeAttribute("id");
        }
        old.parentNode.replaceChild(fresh, old);
      }
      this._canvas = fresh;
    }

    /** 当前激活渲染器所用的 canvas（app.js 据此施加 avatar scale 等样式）。 */
    getCanvas() {
      return this._canvas;
    }

    /**
     * 就绪判定（Requirement 11.4）。等待目标渲染器进入「可接收 Audio_Event 调用」
     * 的就绪态，最多 `timeoutMs`（默认 3000ms）。该时限不含模型/视频流加载。
     *
     * 就绪信号来源（可选）：渲染器的 `whenReady()`（返回 Promise）或 `ready`
     * （Promise 属性）。两者皆无 → 视为构造完成即就绪，立即 resolve。
     *
     * @param {object} [renderer] 目标渲染器，默认 this.active。
     * @param {number} [timeoutMs] 超时毫秒，默认 this._readyTimeoutMs。
     * @returns {Promise<{ok:boolean}>} 就绪 resolve；超时/初始化抛错则 reject。
     */
    awaitReady(renderer, timeoutMs) {
      const r = renderer || this.active;
      const limit = typeof timeoutMs === "number" && timeoutMs > 0 ? timeoutMs : this._readyTimeoutMs;

      let readyPromise = null;
      if (r) {
        try {
          if (typeof r.whenReady === "function") {
            readyPromise = Promise.resolve(r.whenReady());
          } else if (r.ready && typeof r.ready.then === "function") {
            readyPromise = r.ready;
          }
        } catch (err) {
          return Promise.reject(err instanceof Error ? err : new Error(String(err)));
        }
      }

      // 无显式就绪信号：构造完成即就绪。
      if (!readyPromise) return Promise.resolve({ ok: true, timedOut: false });

      return new Promise((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          reject(new Error("renderer readiness timed out after " + limit + "ms"));
        }, limit);
        readyPromise.then(
          () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve({ ok: true, timedOut: false });
          },
          (err) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            reject(err instanceof Error ? err : new Error(String(err || "renderer init failed")));
          }
        );
      });
    }

    /**
     * 运行时切换到目标渲染器类型（Requirement 1.2/1.3/1.4/1.5、11.1/11.2/11.4/11.6）。
     *
     * 行为：
     *   - `to === this.type` 且已有激活实例 → 直接返回，不销毁、不重建（R1.3）。
     *   - 否则：先停止旧渲染器口型/表情驱动（≤200ms，R11.2），在同一画布容器构造
     *     新实例，等待其 ≤3000ms 就绪（R11.4，不含模型/视频流加载）。成功后才销毁
     *     旧实例（R1.4）并提升新实例为激活——延迟销毁是为了让失败时能原样回退到
     *     切换前的同一实例（满足 Property 2 / R1.5 / R11.6）。
     *   - 构造抛错或就绪超时 → 回退到切换前渲染器、保留上下文（管理器从不触碰会话
     *     上下文与待处理 Audio_Event 队列）、显示「切换失败」提示；若无可回退的切换
     *     前渲染器，则调用 degrade() 接缝（R9，任务 2.2 实现降级链）。
     *
     * 不重载页面：仅做实例级生命周期管理，document 不重新初始化（R11.1）。
     *
     * @param {string} type 目标渲染器类型（RENDERER_TYPES 之一）。
     * @returns {Promise<{ok:boolean, type:string, switched:boolean, error?:Error}>}
     */
    async switchTo(type) {
      if (RENDERER_TYPES.indexOf(type) === -1) {
        // 非法类型：不改变现状，提示并返回失败（不抛错）。
        this._notify("ignored switch to unknown renderer type: " + type, {
          kind: "switch-invalid",
          type: type,
        });
        return { ok: false, switched: false, type: this.type, error: new Error("unknown renderer type: " + type) };
      }

      // R1.3：同类型且已激活 → 直接返回，不销毁不重建（active 引用不变）。
      if (type === this.type && this.active) {
        return { ok: true, switched: false, type: this.type, renderer: this.active };
      }

      if (this._switching) {
        return { ok: false, switched: false, type: this.type, error: new Error("a renderer switch is already in progress") };
      }
      this._switching = true;

      const prevRenderer = this.active;
      const prevType = this.type;

      try {
        // R11.2：尽快停止旧渲染器的口型/表情驱动（≤200ms）。
        //   1) 内在：复位旧渲染器口型（resetMouth 为安全 no-op 契约）。
        //   2) 应用层钩子：取消正在驱动旧渲染器的 lip-sync rAF（由 app.js 提供）。
        // 注意：仅停止「驱动」，绝不清空会话上下文或待处理 Audio_Event 队列
        //       （R11.5），管理器不持有/不触碰该队列。
        if (prevRenderer && typeof prevRenderer.resetMouth === "function") {
          try { prevRenderer.resetMouth(); } catch (_e) { /* safe */ }
        }
        try { this._stopDriving(prevType, prevRenderer); } catch (_e) { /* safe */ }

        // 构造新实例：先创建一块全新的 canvas（离屏，未插入 DOM），新渲染器构造到
        // 其中。成功就绪后再把它替换进 DOM（_commitCanvas）；失败则丢弃这块新 canvas，
        // 旧 canvas + 旧渲染器原样保留，回退干净（解决 WebGL↔2D 画布上下文不可复用）。
        const freshCanvas = this._makeFreshCanvas();
        let next = null;
        try {
          next = this._construct(type, freshCanvas);
        } catch (err) {
          return this._failSwitch(prevRenderer, prevType, type, err, "construct");
        }

        // 等待新实例 ≤3000ms 进入就绪态（R11.4；不含模型/视频流加载）。
        // 在就绪确认前不替换 active、不销毁旧实例，以便失败时原样回退。
        try {
          await this.awaitReady(next, this._readyTimeoutMs);
        } catch (err) {
          // 就绪超时或初始化抛错：销毁半成品新实例，回退切换前渲染器。
          if (next && typeof next.destroy === "function") {
            try { next.destroy(); } catch (_e) { /* safe */ }
          }
          return this._failSwitch(prevRenderer, prevType, type, err, "ready");
        }

        // 成功：把新 canvas 替换进 DOM，提升新实例为激活，销毁被替换的旧实例（R1.4）。
        this._commitCanvas(freshCanvas);
        this.active = next;
        this.type = type;
        if (prevRenderer && prevRenderer !== next && typeof prevRenderer.destroy === "function") {
          try { prevRenderer.destroy(); } catch (_e) { /* safe */ }
        }
        return { ok: true, switched: true, type: this.type, renderer: next };
      } finally {
        this._switching = false;
      }
    }

    /**
     * 切换失败的统一处理：回退到切换前的同一渲染器实例并保留上下文（R1.5/11.6），
     * 显示「切换失败」提示。旧实例从未被销毁，故可原样恢复（满足 Property 2）。
     * 若不存在可回退的切换前渲染器，则调用 degrade() 接缝（任务 2.2 实现）。
     * @private
     */
    _failSwitch(prevRenderer, prevType, attemptedType, err, phase) {
      // 回退：恢复切换前的同一实例与类型。
      this.active = prevRenderer || null;
      this.type = prevType || null;

      const reason = (err && err.message) || String(err);
      this._notify(
        "renderer switch to " + attemptedType + " failed (" + phase + "): " + reason +
          (prevType ? "; rolled back to " + prevType : ""),
        { kind: "switch-failed", type: attemptedType, phase: phase, error: err }
      );

      // 无可回退的切换前渲染器 → 进入降级链接缝（R9）。
      if (!this.active) {
        try { this.degrade(attemptedType, err); } catch (_e) { /* seam must be safe */ }
      }

      return { ok: false, switched: false, type: this.type, error: err, phase: phase };
    }

    /**
     * 降级链 `degrade()`（Requirement 9.1/9.3/9.4/9.5/9.6）。
     *
     * 有序兜底链：**Live2D → CanvasAvatar → 纯文本/语音（text/voice）**。每一级
     * 构造失败即下降一级；首个构造成功的级成为最终激活模式（Property 10）。
     *
     *   1) Live2D：经已注册的 "Live2D" 工厂构造，沿用 app.js 的 createAvatar()
     *      选择逻辑（运行时可用 → Live2DAvatar，否则其内部回退 CanvasAvatar）。
     *   2) CanvasAvatar：显式构造无依赖的 2D 占位渲染器（不依赖 WebGL、无模型文件），
     *      作为 Live2D 工厂本身不可用时的独立、保证性视觉兜底。
     *   3) 纯文本/语音：无激活渲染器（active === null），显示「形象渲染不可用」的可见
     *      提示，但对话继续（所有渲染路由变为安全 no-op）。
     *
     * 入口与适用场景：任一渲染器切换/初始化失败且
     * 无可回退渲染器时调用 `degrade(fromType, reason)`，沿本链回退到 Live2D。
     * degrade 自身同步完成，并保留当前对话上下文（见下）。
     *
     * 上下文与子系统保全（R9.4）：本管理器从不持有/触碰会话上下文与待处理
     * Audio_Event 队列，也从不拆除 STT/TTS/persona/barge-in；degrade 仅做渲染器实例
     * 的构造/接管，以及对**被替换实例**的资源释放。因此降级期间对话不中断，Live2D
     * 与 Live3D 路径以及上述子系统均保持可正常调用。
     *
     * @param {string} [fromType] 触发降级的来源类型（如 "Live3D"）。
     * @param {*} [reason] 触发原因（错误对象/消息），用于提示与诊断。
     * @returns {{ok:boolean, degraded:boolean, mode:string, type:(string|null),
     *   textVoice:boolean, attempts:Array<{mode:string,ok:boolean,error:(string|null)}>,
     *   fromType:(string|null), renderer:(object|null)}}
     */
    degrade(fromType, reason) {
      // 可选外部 override 钩子（保留 task 2.1 的接缝语义，便于自定义接线/测试）。
      // 未注入时运行内置降级链（生产默认路径——app.js 不注入该钩子）。
      if (typeof this._onDegrade === "function") {
        return this._onDegrade(fromType, reason);
      }
      return this._runDegradeChain(fromType, reason);
    }

    /**
     * 执行有序降级链 Live2D → CanvasAvatar → 纯文本/语音（同步完成）。
     * @private
     */
    _runDegradeChain(fromType, reason) {
      const previous = this.active;
      const reasonMsg = reason == null ? "" : (reason.message || String(reason));
      const attempts = [];

      // 兜底级定义（顺序即优先级）。仅依赖无 WebGL 的 Live2D/CanvasAvatar。
      // 两级均映射到 type "Live2D"——CanvasAvatar 是 Live2D 视觉路径的占位兜底，与
      // app.js createAvatar() 的类型标注一致；细粒度模式经返回值的 `mode` 区分。
      const levels = [
        { mode: "Live2D", type: "Live2D", build: (cv) => this._construct("Live2D", cv) },
        { mode: "CanvasAvatar", type: "Live2D", build: (cv) => this._constructCanvasAvatar(cv) },
      ];

      for (const level of levels) {
        let inst = null;
        let err = null;
        // Build each fallback level into its OWN fresh canvas (a prior WebGL
        // renderer may have tainted the current one); commit it only on success.
        const freshCanvas = this._makeFreshCanvas();
        try {
          inst = level.build(freshCanvas);
        } catch (e) {
          err = e instanceof Error ? e : new Error(String(e));
        }
        attempts.push({ mode: level.mode, ok: !!inst, error: err ? err.message : null });

        if (inst) {
          this._commitCanvas(freshCanvas);
          this.active = inst;
          this.type = level.type;
          this.textVoiceMode = false;
          // 释放被替换的（失败/已死，如崩溃的数字人）实例资源；绝不触碰其它子系统（R9.4）。
          if (previous && previous !== inst && typeof previous.destroy === "function") {
            try { previous.destroy(); } catch (_e) { /* safe */ }
          }
          this._notify(
            "renderer degraded to " + level.mode +
              (fromType ? " (from " + fromType + ")" : "") +
              (reasonMsg ? ": " + reasonMsg : ""),
            { kind: "degrade", mode: level.mode, fromType: fromType || null, reason: reasonMsg }
          );
          return {
            ok: true, degraded: true, mode: level.mode, type: this.type,
            textVoice: false, attempts: attempts, fromType: fromType || null, renderer: inst,
          };
        }
      }

      // 末级：纯文本/语音（无形象）模式——对话继续，显示可见提示（R9.6）。
      if (previous && typeof previous.destroy === "function") {
        try { previous.destroy(); } catch (_e) { /* safe */ }
      }
      this.active = null;
      this.type = null;
      this.textVoiceMode = true;
      this._notify(
        "形象渲染不可用，已切换到纯文本/语音模式（对话继续）" +
          (fromType ? "（from " + fromType + "）" : ""),
        { kind: "degrade-text-voice", fromType: fromType || null, reason: reasonMsg }
      );
      return {
        ok: true, degraded: true, mode: "text/voice", type: null,
        textVoice: true, attempts: attempts, fromType: fromType || null, renderer: null,
      };
    }

    /**
     * 显式构造无依赖的 CanvasAvatar 兜底渲染器（不依赖 WebGL、无模型文件）。降级链
     * 第二级专用——独立于 Live2D 工厂，确保即便 Live2D 工厂本身抛错也有保证性兜底。
     * 优先使用注入的 canvasFactory（便于测试/自定义），否则从 window.CanvasAvatar
     * 全局构造（avatar.js 已加载）。
     * @private
     * @returns {object} CanvasAvatar 实例。
     * @throws {Error} 当 canvasFactory 与 window.CanvasAvatar 均不可用或构造失败时。
     */
    _constructCanvasAvatar(canvas) {
      const cv = canvas || this._canvas;
      const ctx = { canvas: cv, manager: this, type: "CanvasAvatar" };
      if (typeof this._canvasFactory === "function") {
        const inst = this._canvasFactory(ctx);
        if (!inst) throw new Error("RendererManager: canvasFactory returned no instance");
        return inst;
      }
      const Ctor = typeof global !== "undefined" ? global.CanvasAvatar : undefined;
      if (typeof Ctor !== "function") {
        throw new Error("CanvasAvatar unavailable (window.CanvasAvatar not loaded)");
      }
      const inst = new Ctor(cv);
      if (!inst) throw new Error("RendererManager: window.CanvasAvatar produced no instance");
      return inst;
    }

    // --- Renderer_Interface 转发方法 -------------------------------------
    // 仅路由到当前激活渲染器，不读取/不判断其具体类型（Requirement 2.6）。
    // 当不存在激活渲染器时，所有路由调用为 no-op、不抛错（Requirement 2.7）。

    /** 转发 lip-sync 口型开合度到当前激活渲染器。 */
    setMouthOpen(v) {
      if (!this.active) return;
      this.active.setMouthOpen(v);
    }

    /** 转发表情应用到当前激活渲染器。 */
    setExpression(index, name) {
      if (!this.active) return;
      this.active.setExpression(index, name);
    }

    /** 转发口型复位到当前激活渲染器。 */
    resetMouth() {
      if (!this.active) return;
      this.active.resetMouth();
    }

    /**
     * 转发模型加载到当前激活渲染器，并原样返回其结果（Live2D/Live3D 返回
     * Promise，占位渲染器返回 undefined），以保持现有调用点语义不变。
     */
    setModel(url) {
      if (!this.active) return undefined;
      return this.active.setModel(url);
    }

    /** 转发动作组播放到当前激活渲染器。 */
    playMotion(group) {
      if (!this.active) return;
      this.active.playMotion(group);
    }

    /**
     * 转发 Agent_State（可选方法）。仅当当前激活渲染器实现了 setAgentState 时
     * 才转发（typeof 守卫），否则视为 no-op（Requirement 2.12）。
     */
    setAgentState(state) {
      if (!this.active) return;
      if (typeof this.active.setAgentState === "function") {
        this.active.setAgentState(state);
      }
    }

    /** 销毁当前激活渲染器并释放其引用。 */
    destroy() {
      if (!this.active) return;
      if (typeof this.active.destroy === "function") {
        this.active.destroy();
      }
      this.active = null;
      this.type = null;
    }
  }

  // 暴露合法类型常量，供后续任务（归一化/外观选择控件）复用。
  RendererManager.RENDERER_TYPES = RENDERER_TYPES;

  // SETTINGS Object 在 localStorage 中的 key（与 app.js 的 SETTINGS_KEY 一致）。
  RendererManager.SETTINGS_KEY = "omnilimb-face-settings";

  /**
   * 渲染器选择的归一化（Requirement 1.6/1.9/1.10，Property 5）。
   *
   * 结果恒为两种合法渲染器类型之一：当 `value` 恰等于 `Live2D`/`Live3D`
   * 之一时返回该值；否则（缺失、`null`/`undefined`、空串、
   * 非字符串或任意非法字符串，包括任何已下线的历史持久化值）一律归一为
   * 默认渲染器 `Live2D`。
   *
   * @param {*} value 任意输入（持久化值 / 选择控件值 / 协议值）。
   * @returns {string} `Live2D` | `Live3D`。
   */
  RendererManager.resolveRenderer = function (value) {
    return RENDERER_TYPES.indexOf(value) !== -1 ? value : "Live2D";
  };

  /**
   * 将所选渲染器写入 SETTINGS 并持久化到 localStorage（Requirement 1.7/1.8）。
   *
   * 选择值先经 `resolveRenderer` 归一化后写入 `settings.renderer`（即便随后持久化
   * 失败，该归一化选择仍保留在内存的 SETTINGS 中，使「当前所选渲染器保持激活」——
   * R1.8）。随后尝试 `storage.setItem(key, JSON.stringify(settings))`：
   *   - 成功 → 返回 `{ ok:true, type, persisted:true }`。
   *   - `setItem` 抛错或 storage 不可用 → 经 `notify` 弹出一条「设置保存失败」提示，
   *     返回 `{ ok:false, type, persisted:false, error }`（不抛出未捕获错误，R1.8）。
   *
   * @param {*} type 待持久化的渲染器选择（会被归一化）。
   * @param {object} [opts]
   * @param {object} [opts.settings] 要写入的 SETTINGS Object（原地修改其 renderer）。
   * @param {object} [opts.storage] 存储后端，默认 `window.localStorage`。
   * @param {string} [opts.storageKey] 存储 key，默认 `RendererManager.SETTINGS_KEY`。
   * @param {function} [opts.notify] 用户可见提示回调 (message, meta) => void。
   * @returns {{ok:boolean, type:string, persisted:boolean, error?:Error}}
   */
  RendererManager.persistRenderer = function (type, opts) {
    opts = opts || {};
    const normalized = RendererManager.resolveRenderer(type);
    const settings = opts.settings || {};
    // 先写入内存 SETTINGS：归一化后的选择即时生效，保持当前所选渲染器激活（R1.8）。
    settings.renderer = normalized;

    const key = opts.storageKey || RendererManager.SETTINGS_KEY;
    const storage =
      opts.storage ||
      (typeof global !== "undefined" ? global.localStorage : undefined);
    const notify = typeof opts.notify === "function" ? opts.notify : function () {};

    try {
      if (!storage || typeof storage.setItem !== "function") {
        throw new Error("localStorage unavailable");
      }
      storage.setItem(key, JSON.stringify(settings));
      return { ok: true, type: normalized, persisted: true };
    } catch (err) {
      const e = err instanceof Error ? err : new Error(String(err));
      notify("设置保存失败：" + e.message, {
        kind: "settings-save-failed",
        type: normalized,
        error: e,
      });
      return { ok: false, type: normalized, persisted: false, error: e };
    }
  };

  /**
   * 启动时读取已持久化的渲染器类型并归一化（Requirement 1.9/1.10，Property 8 配套）。
   *
   * 来源优先级：若提供 `opts.settings`（app.js 已合并默认值+持久化值的 SETTINGS），
   * 则取其 `renderer` 字段；否则直接从 `storage` 读取并解析。任一情况下结果都经
   * `resolveRenderer` 归一化——等于四种合法值之一则激活之，缺失/非法一律归一为
   * `Live2D`。读取/解析异常时安全回退到 `Live2D`，不抛出未捕获错误。
   *
   * @param {object} [opts]
   * @param {object} [opts.settings] 已加载的 SETTINGS Object（优先使用其 renderer）。
   * @param {object} [opts.storage] 存储后端，默认 `window.localStorage`。
   * @param {string} [opts.storageKey] 存储 key，默认 `RendererManager.SETTINGS_KEY`。
   * @returns {string} 归一化后的启动渲染器类型。
   */
  RendererManager.loadRenderer = function (opts) {
    opts = opts || {};
    if (opts.settings && typeof opts.settings === "object" && "renderer" in opts.settings) {
      return RendererManager.resolveRenderer(opts.settings.renderer);
    }
    const key = opts.storageKey || RendererManager.SETTINGS_KEY;
    const storage =
      opts.storage ||
      (typeof global !== "undefined" ? global.localStorage : undefined);
    try {
      const raw =
        storage && typeof storage.getItem === "function" ? storage.getItem(key) : null;
      const parsed = raw ? JSON.parse(raw) : {};
      return RendererManager.resolveRenderer(parsed && parsed.renderer);
    } catch (_e) {
      return "Live2D";
    }
  };

  // 与现有 window.CanvasAvatar / window.Live2DAvatar 一致，挂为全局。
  global.RendererManager = RendererManager;
})(window);
