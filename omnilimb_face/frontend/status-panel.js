/*
 * omnilimb-face reference front-end — StatusPanel (Telemetry HUD).
 *
 * 本文件实现「状态信息面板」与遥测采集（任务 9.1，Requirement 13）。StatusPanel
 * 是一个横切组件：它把运行时各项 Telemetry 指标集中渲染到设置弹窗「状态」标签页
 * （复用现有 `#status-body`），并以一个**独立的、与 LLM/TTS 工作负载解耦的
 * `setInterval`** 严格驱动网络延迟（Network_Latency）的 ping 发送与数值刷新。
 *
 * ============================================================================
 * 设计要点（Design）
 * ============================================================================
 *
 *  - 渲染是一个**纯函数**：`StatusPanel.buildRows(telemetry)` 接收任意 telemetry
 *    对象（含缺失字段、disconnected/reconnecting 连接状态、甚至非对象/非法值），
 *    产出一组已归一化的 { key, value, available } 行；`StatusPanel.toHtml(rows)`
 *    把它们转成转义安全的 HTML。任一指标缺失/不可用 → 显示占位符（「未知」），
 *    其余指标仍正常展示，且渲染**绝不抛出错误**（Requirement 13.9/13.11、Property 12）。
 *    因此属性测试 9.2 可用任意 telemetry 对象驱动 `buildRows`/`render`。
 *
 *  - 数据来源仅复用现有 `/client-ws` 字段或按 Requirement 3 增量新增字段
 *    （`set-model-and-conf` 的 `name`/`url`/`stt_model`/`tts_voice` 既有字段，
 *    `agent-state`/`ping`/`pong` 增量事件），不修改、不移除任何既有协议字段
 *    （Requirement 13.10）。
 *
 *  - Network_Latency 的**严格刷新**（Requirement 13.5）：`start()` 用一个固定周期
 *    （默认 3000ms，硬上限 5000ms）的 `setInterval` 周期性 (a) 通过 `sendPing`
 *    回调发送 ping、(b) 重绘面板以刷新「上次成功 pong」延迟。该节奏独立于
 *    AudioPlayer / LLM delta 流等工作负载，确保刷新间隔被严格执行。延迟数值在收到
 *    pong 时经 `recordPong(t)` 由 `now() - t` 算得。
 *
 *  - Connection_Status（Requirement 13.4/13.8）：`setConnection(value)` 接收枚举
 *    （connected/connecting/reconnecting/disconnected）或底层 WebSocket 状态字串
 *    （open/connecting/closed/error），经 `normalizeConnection` 归一并立即重绘，
 *    使连接状态变化在下一次同步事件即反映（远小于 1000ms）。
 *
 * 该模块为纯 JS IIFE（无构建步骤），把 `StatusPanel` 挂到 window 全局，与
 * avatar.js / renderer-manager.js 等保持一致；渲染逻辑
 * 不依赖具体 DOM 实现细节，可在 jsdom 测试中单独加载并以注入的 scheduler 做 fake
 * timers 断言。
 */

(function (global) {
  "use strict";

  // 两种合法渲染器类型（Requirement 13.1）。与 RendererManager.RENDERER_TYPES 一致。
  const RENDERER_TYPES = ["Live2D", "Live3D"];

  // 合法连接状态枚举（Requirement 13.4）。
  const CONNECTION_STATES = [
    "connected",
    "connecting",
    "reconnecting",
    "disconnected",
  ];

  // 合法 Agent_State 枚举（Requirement 13.6）。
  const AGENT_STATES = ["idle", "listening", "thinking", "speaking"];

  // 任一指标缺失/不可用时的占位符（Requirement 13.11）。
  const PLACEHOLDER = "未知";

  // Network_Latency 刷新间隔硬上限（Requirement 13.5：刷新间隔严格 ≤ 5000ms）。
  const MAX_LATENCY_INTERVAL_MS = 5000;
  const DEFAULT_LATENCY_INTERVAL_MS = 3000;

  // 连接状态 → 可读中文标签。
  const CONNECTION_LABELS = {
    connected: "已连接",
    connecting: "连接中",
    reconnecting: "重连中",
    disconnected: "已断开",
  };

  // 底层 WebSocket / protocol.onStatus 字串 → Connection_Status 枚举。
  const WIRE_CONNECTION = {
    open: "connected",
    connected: "connected",
    connecting: "connecting",
    reconnecting: "reconnecting",
    closed: "disconnected",
    disconnected: "disconnected",
    error: "disconnected",
  };

  // Agent_State 枚举 → 可读中文标签（Requirement 13.6）。
  const AGENT_LABELS = {
    idle: "空闲",
    listening: "聆听",
    thinking: "思考",
    speaking: "说话",
  };

  /** HTML 文本转义，确保任意字符串（含尖括号/引号）安全渲染、不破坏结构。 */
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** 取「非空字符串」否则 null。 */
  function str(x) {
    return typeof x === "string" && x ? x : null;
  }

  /** 取「有限数值」否则 null。 */
  function num(x) {
    return typeof x === "number" && isFinite(x) ? x : null;
  }

  // ----- 纯归一化助手（静态，可单测） -------------------------------------

  /**
   * 归一化渲染器类型（Requirement 13.1）。合法四种之一 → 原值，否则 null（占位符）。
   */
  function normalizeRenderer(value) {
    return RENDERER_TYPES.indexOf(value) !== -1 ? value : null;
  }

  /**
   * 归一化连接状态（Requirement 13.4/13.8）。接收枚举或底层 WebSocket 状态字串，
   * 返回 connected/connecting/reconnecting/disconnected 之一，否则 null（占位符）。
   */
  function normalizeConnection(value) {
    if (CONNECTION_STATES.indexOf(value) !== -1) return value;
    if (typeof value === "string" && WIRE_CONNECTION[value]) {
      return WIRE_CONNECTION[value];
    }
    return null;
  }

  /**
   * 归一化 Agent_State（Requirement 13.6）。合法四种之一 → 原值，否则 null（占位符）。
   */
  function normalizeAgentState(value) {
    return AGENT_STATES.indexOf(value) !== -1 ? value : null;
  }

  /**
   * 由 telemetry 对象构建一组已归一化的展示行（纯函数，Requirement 13，Property 12）。
   *
   * 每一行形如 `{ key, value, available }`：`available=false` 表示该指标缺失/不可用，
   * `value` 取占位符「未知」；`available=true` 表示有合法取值。任意输入（含 telemetry
   * 非对象、字段类型错误、连接处于 disconnected/reconnecting）都安全处理、不抛错。
   *
   * @param {*} telemetry 任意 telemetry 状态对象。形状（均为可选）：
   *   { renderer, model:{name,url}, stt, tts, connection, latencyMs, agentState }
   * @returns {Array<{key:string, value:string, available:boolean}>}
   */
  function buildRows(telemetry) {
    const t = telemetry && typeof telemetry === "object" ? telemetry : {};
    const rows = [];
    const add = (key, value) => {
      const ok = typeof value === "string" && value.length > 0;
      rows.push({ key: key, value: ok ? value : PLACEHOLDER, available: ok });
    };

    // 1) 渲染器类型（Requirement 13.1）。
    const renderer = normalizeRenderer(t.renderer);
    add("渲染器", renderer);

    // 2) 模型名 + 来源（Requirement 13.2）。
    const model = t.model && typeof t.model === "object" ? t.model : null;
    const name = model ? str(model.name) : null;
    const url = model ? str(model.url) : null;
    let modelText = null;
    if (name && url) modelText = name + " · " + url;
    else if (name) modelText = name;
    else if (url) modelText = url;
    add("模型 / 来源", modelText);

    // 3) STT 模型 与 TTS 语音（复用既有 stt_model / tts_voice 字段，Requirement 13.3）。
    add("STT 模型", str(t.stt));
    add("TTS 语音", str(t.tts));

    // 4) Connection_Status（Requirement 13.4/13.8/13.9）。
    const conn = normalizeConnection(t.connection);
    add("连接状态", conn ? CONNECTION_LABELS[conn] : null);

    // 5) Network_Latency（Requirement 13.5）。
    const latency = num(t.latencyMs);
    add("网络延迟", latency === null ? null : Math.round(latency) + " ms");

    // 6) Agent_State（Requirement 13.6）。
    const agent = normalizeAgentState(t.agentState);
    add("智能体状态", agent ? AGENT_LABELS[agent] : null);

    return rows;
  }

  /** 把 buildRows 的行渲染为转义安全的 HTML（状态网格 .status-grid 的内容）。 */
  function toHtml(rows) {
    if (!Array.isArray(rows)) return "";
    return rows
      .map((r) => {
        const cls = r && r.available ? "v" : "v no";
        const key = escapeHtml(r ? r.key : "");
        const value = escapeHtml(r ? r.value : PLACEHOLDER);
        return '<div class="k">' + key + '</div><div class="' + cls + '">' + value + "</div>";
      })
      .join("");
  }

  /**
   * StatusPanel —— 状态面板实例：持有 telemetry 状态、把渲染绑定到一个容器元素，
   * 并以独立 setInterval 严格驱动延迟刷新（Requirement 13.5）。
   */
  class StatusPanel {
    /**
     * @param {object} [options]
     * @param {object}   [options.container] 渲染目标 DOM 元素（如 #status-body）。
     * @param {function} [options.sendPing] 每个刷新周期调用一次以发送 ping（可选）。
     *   通常为 () => { if (socket open) proto.sendPing(now()); }。
     * @param {function} [options.now] 时间源，默认 Date.now（便于测试注入）。
     * @param {number}   [options.intervalMs] 刷新间隔，默认 3000ms，硬上限 5000ms
     *   （超过则钳制到 5000，Requirement 13.5）。
     * @param {object}   [options.scheduler] { setInterval, clearInterval } 注入点
     *   （便于 fake timers 测试），默认 window 的定时器。
     * @param {object}   [options.telemetry] 初始 telemetry 状态。
     */
    constructor(options = {}) {
      this.container = options.container || null;
      this._sendPing = typeof options.sendPing === "function" ? options.sendPing : null;
      this._now = typeof options.now === "function" ? options.now : Date.now;

      let iv = options.intervalMs;
      if (typeof iv !== "number" || !isFinite(iv) || iv <= 0) {
        iv = DEFAULT_LATENCY_INTERVAL_MS;
      }
      // 严格上限：刷新间隔不超过 5000ms（Requirement 13.5）。
      this.intervalMs = Math.min(iv, MAX_LATENCY_INTERVAL_MS);

      const sched = options.scheduler || {};
      // NOTE: native window.setInterval / clearInterval MUST be invoked with
      // `this === window`. Storing a bare reference and later calling it as
      // `this._setInterval(...)` makes `this` the StatusPanel instance, which
      // throws "Illegal invocation" in real browsers (jsdom does not, which is
      // why unit tests passed). So wrap the global timers to call them on the
      // global object; an injected scheduler (tests) is used as-is.
      this._setInterval =
        typeof sched.setInterval === "function"
          ? sched.setInterval
          : (typeof setInterval === "function"
              ? (fn, ms) => setInterval(fn, ms)
              : null);
      this._clearInterval =
        typeof sched.clearInterval === "function"
          ? sched.clearInterval
          : (typeof clearInterval === "function"
              ? (id) => clearInterval(id)
              : null);

      this._timer = null;
      this._pingSeq = 0; // 已发送 ping 计数（诊断用）。

      // 内部 telemetry 状态（增量 update 合并）。
      this.telemetry = {
        renderer: null,
        model: null,
        stt: null,
        tts: null,
        connection: null,
        latencyMs: null,
        agentState: null,
      };
      if (options.telemetry && typeof options.telemetry === "object") {
        this.update(options.telemetry);
      }
    }

    /**
     * 增量合并 telemetry 字段并重绘（缺省字段保持不变）。
     * @param {object} partial 部分 telemetry 字段。
     * @returns {StatusPanel} this。
     */
    update(partial) {
      if (partial && typeof partial === "object") {
        for (const k of Object.keys(partial)) {
          this.telemetry[k] = partial[k];
        }
      }
      this.render();
      return this;
    }

    /** 设置 Connection_Status（接收枚举或底层状态字串），归一化后重绘（R13.4/13.8）。 */
    setConnection(value) {
      const conn = normalizeConnection(value);
      this.telemetry.connection = conn;
      // 非 connected 状态下延迟不可知 → 清空为占位符（Requirement 13.11/13.9）。
      if (conn !== "connected") this.telemetry.latencyMs = null;
      this.render();
      return this;
    }

    /** 设置 Agent_State（归一化后重绘，R13.6）。 */
    setAgentState(state) {
      this.telemetry.agentState = normalizeAgentState(state);
      this.render();
      return this;
    }

    /** 设置激活渲染器类型并重绘（R13.1）。 */
    setRenderer(type) {
      this.telemetry.renderer = normalizeRenderer(type);
      this.render();
      return this;
    }

    /**
     * 记录一次 pong 回送：以 `now() - t` 算得 RTT 延迟（毫秒）并重绘（R13.5）。
     * `t` 非数值时忽略（保持上次延迟）。
     * @param {number} t pong 回送的发送时间戳（ms）。
     */
    recordPong(t) {
      if (typeof t === "number" && isFinite(t)) {
        const rtt = this._now() - t;
        this.telemetry.latencyMs = rtt >= 0 ? rtt : 0;
        this.render();
      }
      return this;
    }

    /**
     * 启动独立的延迟刷新定时器（Requirement 13.5）。固定周期 `intervalMs`（≤5000ms）
     * 周期性发送 ping 并重绘以刷新延迟数值；该节奏与 LLM/TTS 负载解耦。
     * 幂等：重复调用不会创建多个定时器。
     * @returns {StatusPanel} this。
     */
    start() {
      if (this._timer || !this._setInterval) {
        // 首次渲染一次，确保面板即便未启动定时器也有内容。
        this.render();
        return this;
      }
      this._timer = this._setInterval(() => this._tick(), this.intervalMs);
      this.render();
      return this;
    }

    /** 停止延迟刷新定时器并释放资源。 */
    stop() {
      if (this._timer && this._clearInterval) {
        this._clearInterval(this._timer);
      }
      this._timer = null;
      return this;
    }

    /** 单个刷新周期：发送 ping（若配置）并重绘以刷新延迟展示。绝不抛错。 */
    _tick() {
      try {
        if (this._sendPing) {
          this._pingSeq++;
          this._sendPing(this._now());
        }
      } catch (_e) {
        /* ping 发送失败不应中断刷新节奏 */
      }
      this.render();
    }

    /**
     * 把当前 telemetry 渲染到容器（Requirement 13.9/13.11：永不抛错）。无容器时安全跳过。
     * @returns {string} 渲染出的 HTML（便于测试断言）。
     */
    render() {
      let html;
      try {
        html = toHtml(buildRows(this.telemetry));
      } catch (_e) {
        // 兜底：即便构建失败也不抛错，渲染一个最小安全占位。
        html = '<div class="k">状态</div><div class="v no">' + PLACEHOLDER + "</div>";
      }
      if (this.container && "innerHTML" in this.container) {
        try {
          this.container.innerHTML = html;
        } catch (_e) {
          /* 容器不可写时静默跳过 */
        }
      }
      return html;
    }
  }

  // 暴露纯助手与常量，供属性测试（9.2）与单元测试复用。
  StatusPanel.RENDERER_TYPES = RENDERER_TYPES;
  StatusPanel.CONNECTION_STATES = CONNECTION_STATES;
  StatusPanel.AGENT_STATES = AGENT_STATES;
  StatusPanel.PLACEHOLDER = PLACEHOLDER;
  StatusPanel.MAX_LATENCY_INTERVAL_MS = MAX_LATENCY_INTERVAL_MS;
  StatusPanel.CONNECTION_LABELS = CONNECTION_LABELS;
  StatusPanel.AGENT_LABELS = AGENT_LABELS;
  StatusPanel.buildRows = buildRows;
  StatusPanel.toHtml = toHtml;
  StatusPanel.escapeHtml = escapeHtml;
  StatusPanel.normalizeRenderer = normalizeRenderer;
  StatusPanel.normalizeConnection = normalizeConnection;
  StatusPanel.normalizeAgentState = normalizeAgentState;

  global.StatusPanel = StatusPanel;
})(window);
