/*
 * omnilimb-face reference front-end — /client-ws protocol client (wire layer).
 *
 * Pure transport + (de)serialization for the Open-LLM-VTuber compatible
 * `/client-ws` protocol. It owns the WebSocket (with auto-reconnect), parses
 * each inbound server->client message by its `type` discriminant, and forwards
 * it to the supplied handlers. It also exposes typed senders for the
 * client->server messages. Rendering/audio live in app.js; this file never
 * touches the canvas.
 *
 * Server -> client message shapes (must match omnilimb_face/protocol/events.py):
 *   full-text          { type, text }
 *   set-model-and-conf { type, model_info:{name,url,emotionMap,is_placeholder}, conf_name, conf_uid }
 *   audio              { type, audio: base64|null, volumes:[float], slice_length:int(ms),
 *                        display_text:{text,...}, actions:{expressions:[int]}|null, forwarded:bool }
 *   control            { type, text: "start-mic"|"stop-mic"|"mic-audio-end"|
 *                        "conversation-chain-start"|"conversation-chain-end"|"interrupt"|"mouth-reset" }
 *   error              { type, code, reason }
 *
 * Additive (forward-compatible) extensions — switchable-avatar-renderers feature
 * (Requirement 3, additive-only; existing field names/hierarchy/types are NEVER
 * changed and unknown event types/fields are ignored):
 *   agent-state        { type, state: "idle"|"listening"|"thinking"|"speaking" }
 *                        -> routed to the optional onAgentState handler.
 *   ping / pong        { type, t: <ms> }  RTT measurement support. An inbound
 *                        `ping` notifies onPing and is auto-answered with `pong`
 *                        (echoing `t`); an inbound `pong` notifies onPong so a
 *                        StatusPanel can compute round-trip latency.
 *   set-model-and-conf.model_info OPTIONAL additive fields surfaced as a third
 *   `extras` argument to onSetModel(modelInfo, msg, extras) without touching the
 *   existing fields:
 *     vrm_url        : string  (Live3D VRM source)
 *   When an additive field is present but invalid it is ignored (falls back to
 *   existing-field default behavior) and ONE diagnostic is emitted; a missing
 *   field is the normal back-compat case and is silent.
 *
 * Client -> server senders:
 *   text-input { type, text }
 *   interrupt-signal { type, at_text_index }
 *   mic-audio-data { type, audio, sample_rate }  (not used by the placeholder UI)
 *   mic-audio-end { type }
 *   fetch-configs { type }
 *   frontend-playback-complete { type }
 *   ping / pong { type, t }  (additive; RTT measurement)
 */

(function (global) {
  "use strict";

  class VTuberProtocol {
    /**
     * @param {string} url  ws:// URL of the /client-ws gateway.
     * @param {object} handlers  { onStatus, onLog, onFullText, onSetModel,
     *   onAudio, onControl, onError, onAgentState, onPing, onPong } — all optional.
     */
    constructor(url, handlers) {
      this.url = url;
      this.h = handlers || {};
      this.ws = null;
      this._closedByUser = false;
      this._reconnectDelay = 500; // ms, exponential backoff up to 8s.
    }

    connect() {
      this._closedByUser = false;
      this._status("connecting");
      let ws;
      try {
        ws = new WebSocket(this.url);
      } catch (err) {
        this._log("connect error: " + err);
        this._scheduleReconnect();
        return;
      }
      this.ws = ws;

      ws.onopen = () => {
        this._reconnectDelay = 500;
        this._status("open");
        this._log("connected to " + this.url);
        // Ask the backend for the available model/config list.
        this.sendFetchConfigs();
      };

      ws.onmessage = (ev) => this._onMessage(ev.data);

      ws.onerror = () => {
        this._status("error");
        this._log("socket error");
      };

      ws.onclose = () => {
        this._status("closed");
        if (!this._closedByUser) this._scheduleReconnect();
      };
    }

    close() {
      this._closedByUser = true;
      if (this.ws) this.ws.close();
    }

    _scheduleReconnect() {
      const delay = this._reconnectDelay;
      this._reconnectDelay = Math.min(8000, delay * 2);
      this._log(`reconnecting in ${delay}ms…`);
      setTimeout(() => {
        if (!this._closedByUser) this.connect();
      }, delay);
    }

    _onMessage(raw) {
      let msg;
      try {
        msg = JSON.parse(raw);
      } catch (err) {
        this._log("dropping non-JSON inbound message");
        return;
      }
      if (!msg || typeof msg.type !== "string") {
        this._log("dropping message without a string 'type'");
        return;
      }
      switch (msg.type) {
        case "full-text":
          this.h.onFullText && this.h.onFullText(msg.text || "");
          break;
        case "set-model-and-conf": {
          // Existing fields (name/url/emotionMap/is_placeholder/...) are passed
          // through unchanged. Optional additive fields are parsed and surfaced
          // as a third `extras` argument so existing callers (which read only the
          // first argument) keep working untouched (Requirement 3.1, 3.2).
          const modelInfo = msg.model_info || {};
          const extras = this._parseModelExtras(modelInfo);
          this.h.onSetModel && this.h.onSetModel(modelInfo, msg, extras);
          break;
        }
        case "audio":
          this.h.onAudio && this.h.onAudio(msg);
          break;
        case "control":
          this.h.onControl && this.h.onControl(msg.text || "");
          break;
        case "error":
          this.h.onError && this.h.onError(msg.code || "error", msg.reason || "");
          this._log(`server error [${msg.code}]: ${msg.reason}`);
          break;
        // -- additive, forward-compatible lightweight events ---------------
        case "agent-state":
          // Route Agent_State to the optional handler (Requirement 3.6). The raw
          // string is forwarded; renderers normalize/no-op on invalid values.
          this.h.onAgentState &&
            this.h.onAgentState(typeof msg.state === "string" ? msg.state : "");
          break;
        case "ping": {
          // RTT support: notify the hook and auto-answer with a pong echoing the
          // server's timestamp so a server-initiated ping is always answered.
          const t = typeof msg.t === "number" ? msg.t : null;
          this.h.onPing && this.h.onPing(t);
          this.sendPong(t === null ? Date.now() : t);
          break;
        }
        case "pong":
          // Echoed timestamp: let a StatusPanel compute round-trip latency.
          this.h.onPong && this.h.onPong(typeof msg.t === "number" ? msg.t : null);
          break;
        default:
          // Unknown but well-formed: ignore (forward-compatible), like the backend.
          this._log("ignoring unknown message type: " + msg.type);
      }
    }

    /**
     * Parse OPTIONAL additive fields on set-model-and-conf.model_info without
     * touching any existing field (Requirement 3.2, 3.4). Returns a normalized
     * `extras` object: { vrmUrl }. A field that is present but invalid is
     * ignored (treated as absent) and AT MOST ONE diagnostic is emitted; a
     * missing field is the normal back-compat case and is silent, so
     * existing-field processing is never affected.
     * @param {object} modelInfo
     * @returns {{vrmUrl: (string|null)}}
     */
    _parseModelExtras(modelInfo) {
      const extras = { vrmUrl: null };
      if (!modelInfo || typeof modelInfo !== "object") return extras;

      let diagnosed = false;
      const warnOnce = (m) => {
        if (diagnosed) return;
        diagnosed = true;
        this._log("set-model-and-conf additive field ignored: " + m);
      };

      if ("vrm_url" in modelInfo) {
        const v = modelInfo.vrm_url;
        if (typeof v === "string" && v) {
          extras.vrmUrl = v;
        } else {
          warnOnce("vrm_url present but invalid (expected non-empty string)");
        }
      }

      return extras;
    }

    // -- client -> server senders -----------------------------------------
    _send(obj) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        this._log("cannot send (socket not open): " + obj.type);
        return false;
      }
      this.ws.send(JSON.stringify(obj));
      return true;
    }

    sendTextInput(text) {
      return this._send({ type: "text-input", text: String(text) });
    }

    sendInterrupt(atTextIndex) {
      return this._send({
        type: "interrupt-signal",
        at_text_index: atTextIndex | 0,
      });
    }

    sendMicAudioData(base64, sampleRate) {
      return this._send({
        type: "mic-audio-data",
        audio: base64,
        sample_rate: sampleRate | 0 || 16000,
      });
    }

    sendMicAudioEnd() {
      return this._send({ type: "mic-audio-end" });
    }

    sendFetchConfigs() {
      return this._send({ type: "fetch-configs" });
    }

    sendPlaybackComplete() {
      return this._send({ type: "frontend-playback-complete" });
    }

    // Additive (forward-compatible) RTT senders. A StatusPanel can periodically
    // sendPing(Date.now()) and compute latency when the matching pong arrives;
    // sendPong echoes a received timestamp. The backend ignores unknown types,
    // so these are safe no-ops against an older gateway (Requirement 3.6, 13.5).
    sendPing(t) {
      return this._send({ type: "ping", t: typeof t === "number" ? t : Date.now() });
    }

    sendPong(t) {
      return this._send({ type: "pong", t: typeof t === "number" ? t : Date.now() });
    }

    // -- internal helpers --------------------------------------------------
    _status(s) {
      this.h.onStatus && this.h.onStatus(s);
    }
    _log(line) {
      this.h.onLog && this.h.onLog(line);
    }
  }

  global.VTuberProtocol = VTuberProtocol;
})(window);
