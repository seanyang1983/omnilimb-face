"""Warm, streaming chat worker for the omnilimb-face preview (P1 + P2).

Run with the hermes venv python, cwd = hermes-agent. It builds an AIAgent ONCE
(reusing hermes' one-shot construction) so there is no per-message cold start,
then answers prompts over a stdin/stdout JSON-line protocol, STREAMING reply
token deltas so the caller can synthesize speech sentence-by-sentence. Supports
cooperative cancellation (barge-in).

Protocol (one compact JSON object per line):
  worker -> caller:
    {"t":"ready"}                         once the agent is built
    {"t":"delta","id":N,"d":"<tokens>"}   streamed reply text
    {"t":"done","id":N,"text":"<full>"}   reply finished
    {"t":"cancelled","id":N}              reply was cancelled (barge-in)
    {"t":"error","id":N,"e":"<msg>"}      failure
  caller -> worker:
    {"t":"chat","id":N,"text":"<prompt>"}
    {"t":"cancel"}                        cancel the in-flight reply

stdout carries ONLY this protocol: the real stdout fd is captured at import and
the agent's own stdout/stderr are redirected to devnull during generation so
they can never corrupt the stream.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading

# Force UTF-8 on the protocol pipes BEFORE capturing the real stdout — on
# Windows the console/pipe default is often GBK (cp936), which would corrupt
# the JSON protocol for any non-ASCII (Chinese) reply text.
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

# Captured BEFORE anything can reassign sys.stdout. All protocol writes go here.
_REAL_OUT = sys.stdout

os.environ.setdefault("HERMES_QUIET", "1")
os.environ["HERMES_YOLO_MODE"] = "1"      # non-interactive: auto-approve
os.environ["HERMES_ACCEPT_HOOKS"] = "1"


def _emit(obj: dict) -> None:
    try:
        _REAL_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
        _REAL_OUT.flush()
    except Exception:
        pass


class _Cancelled(Exception):
    """Raised inside the stream callback to abort generation on barge-in."""


def _build_agent(state: dict):
    """Build the warm AIAgent once (pure chat, no tools), wired for streaming."""
    import logging

    logging.disable(logging.CRITICAL)
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.oneshot import (
        _create_session_db_for_oneshot,
        _oneshot_clarify_callback,
    )
    from run_agent import AIAgent

    cfg = load_config()
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        cfg_model = model_cfg
    else:
        cfg_model = model_cfg.get("default") or model_cfg.get("model") or ""
    effective_model = os.getenv("HERMES_INFERENCE_MODEL", "").strip() or cfg_model

    runtime = resolve_runtime_provider(
        requested=None, target_model=effective_model or None
    )
    fb = get_fallback_chain(cfg)

    # VTuber persona — injected as the agent's system prompt so the avatar
    # role-plays this character WITHOUT touching hermes' own config. Empty ->
    # the host default system prompt is used.
    persona = os.environ.get("OMNILIMB_FACE_PERSONA", "").strip()

    def on_delta(*args, **kwargs):
        # Abort generation immediately when a cancel arrived (barge-in).
        if state.get("cancel"):
            raise _Cancelled()
        delta = ""
        if args:
            delta = args[0]
        elif "delta" in kwargs:
            delta = kwargs["delta"]
        if isinstance(delta, str) and delta:
            _emit({"t": "delta", "id": state.get("id"), "d": delta})

    agent_kwargs = dict(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        model=effective_model,
        enabled_toolsets=[],          # pure conversation -> fast, no tool detours
        quiet_mode=True,
        platform="cli",
        session_db=_create_session_db_for_oneshot(),
        credential_pool=runtime.get("credential_pool"),
        fallback_model=fb or None,
        clarify_callback=_oneshot_clarify_callback,
        stream_delta_callback=on_delta,
    )
    if persona:
        agent_kwargs["ephemeral_system_prompt"] = persona
    agent = AIAgent(**agent_kwargs)
    agent.suppress_status_output = True
    agent.tool_gen_callback = None
    return agent


def _stdin_reader(req_q: "queue.Queue", state: dict) -> None:
    """Read protocol lines on a thread so cancel is seen DURING generation."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        t = msg.get("t")
        if t == "cancel":
            state["cancel"] = True          # picked up by on_delta mid-stream
        elif t == "chat":
            req_q.put(msg)
        elif t == "shutdown":
            req_q.put(None)
            return


def main() -> int:
    state: dict = {"cancel": False, "id": None}
    try:
        agent = _build_agent(state)
    except Exception as exc:  # noqa: BLE001
        _emit({"t": "error", "id": 0, "e": f"agent build failed: {exc!r}"})
        return 1

    _emit({"t": "ready"})

    req_q: "queue.Queue" = queue.Queue()
    reader = threading.Thread(target=_stdin_reader, args=(req_q, state), daemon=True)
    reader.start()

    devnull = open(os.devnull, "w", encoding="utf-8")
    while True:
        req = req_q.get()
        if req is None:
            break
        cid = req.get("id")
        text = req.get("text", "")
        state["cancel"] = False
        state["id"] = cid
        try:
            with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                reply = agent.chat(text) or ""
            _emit({"t": "done", "id": cid, "text": reply})
        except _Cancelled:
            _emit({"t": "cancelled", "id": cid})
        except Exception as exc:  # noqa: BLE001
            _emit({"t": "error", "id": cid, "e": f"{exc!r}"})
    try:
        devnull.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
