"""omnilimb_face.runtime — the plugin runtime that owns every subsystem.

This module implements the ``VTuberRuntime`` referenced by the plugin entry
point (design.md -> "Components and Interfaces" -> "Plugin Entry Point:
register(ctx)"). The runtime is the single object handed to ``register(ctx)``
that holds the plugin's configuration and (in later tasks) all of its
subsystems: voice capture, STT, the LLM bridge, sentence chunking, TTS, the
interruption controller, expression mapping, the Live2D director and the
``/client-ws`` protocol gateway.

Task split
----------
* **Task 18.1 (this file's current scope)** — the runtime *skeleton*: the
  ``__init__`` that records ``ctx`` + ``config`` and probes the optional voice /
  Live2D dependencies **without raising** (so missing extras yield a *degraded*
  registration per Requirement 12.1), plus working-but-minimal implementations
  of every method the entry point wires:

  - availability probes :meth:`VTuberRuntime.deps_available` /
    :meth:`VTuberRuntime.tts_available` (never raise, Requirement 12.1);
  - tool handlers :meth:`VTuberRuntime.tool_status` /
    :meth:`VTuberRuntime.tool_say` (always return a **JSON string**, even when
    degraded, Requirements 12.2 / 12.6);
  - lifecycle hooks :meth:`VTuberRuntime.on_session_start` /
    :meth:`VTuberRuntime.on_session_end` and the LLM-output observers
    :meth:`VTuberRuntime.on_llm_output` (returns ``None`` — observer) /
    :meth:`VTuberRuntime.on_post_llm_call`;
  - CLI + slash stubs :meth:`VTuberRuntime.build_cli_parser` /
    :meth:`VTuberRuntime.handle_cli` / :meth:`VTuberRuntime.slash_vtuber` /
    :meth:`VTuberRuntime.slash_handsfree`.

* **Task 18.2 (this file's current scope)** — full :meth:`tool_status` /
  :meth:`tool_say` behaviour (text -> TTS + lip-sync volumes + expression
  indices) and the final degraded ``check_fn`` semantics
  (Requirements 10.5, 12.2; supports 12.6 visibility).
* **Task 19.1 (later)** — real :meth:`on_session_start` / :meth:`on_session_end`
  resource management (init within 5 s; ordered release within 3 s).
* **Task 19.2 (later)** — full CLI subcommand + slash command behaviour
  (``start|stop|status|doctor``; ``/vtuber``, ``/handsfree``).
* **Task 22.1 (later)** — wiring of the :class:`omnilimb_face.llm_bridge.LLMBridge`
  so the LLM-output observers actually drive TTS + the avatar.

Import safety: this module never hard-imports the optional ``[voice]`` /
``[live2d]`` extras nor hermes core at module load. Optional dependencies are
detected with :func:`importlib.util.find_spec`, which reports availability
without importing the package and without raising on absence.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import logging
import threading
import time
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from omnilimb_face.chunker import SentenceChunker
from omnilimb_face.config import VTuberConfig
from omnilimb_face.expression import ExpressionMapper
from omnilimb_face.interruption import InterruptionController
from omnilimb_face.live2d import Live2DDirector, Live2DModelInfo
from omnilimb_face.llm_bridge import LLMBridge, ReplyChunk
from omnilimb_face.stt import STTEngine
from omnilimb_face.tts import AudioSegmentOut, TTSPlayer

logger = logging.getLogger(__name__)

__all__ = ["VTuberRuntime"]


# ---------------------------------------------------------------------------
# Optional-dependency groups (mirrors pyproject's optional extras).
#
# These names are probed with importlib.util.find_spec so a missing extra is a
# benign "not available" rather than an ImportError — the plugin can then
# register in a degraded state (Requirement 12.1) while still exposing its
# tools (Requirement 12.6).
# ---------------------------------------------------------------------------

#: Hands-free microphone capture + VAD stack (``pip install omnilimb-face[voice]``).
VOICE_MODULES: tuple[str, ...] = ("sounddevice", "webrtcvad", "numpy")
#: Optional wake-word activation (``[wakeword]``).
WAKEWORD_MODULES: tuple[str, ...] = ("openwakeword",)
#: Front-end static serving for the Live2D renderer (``[live2d]``).
LIVE2D_MODULES: tuple[str, ...] = ("starlette", "uvicorn")
#: Core ``/client-ws`` transport (shipped in core deps, probed for completeness).
PROTOCOL_MODULES: tuple[str, ...] = ("websockets",)


def _module_available(name: str) -> bool:
    """Return ``True`` when ``name`` is importable, without importing it.

    Uses :func:`importlib.util.find_spec`, so a missing module is reported as
    ``False`` instead of raising. Any unexpected probing error (e.g. a broken
    parent package) is also treated as "not available" so dependency probing
    can never raise into ``register(ctx)`` (Requirement 12.1).
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):  # pragma: no cover - defensive
        return False
    except Exception:  # pragma: no cover - ultra-defensive: never raise
        logger.debug("dependency probe for %r raised; treating as missing", name, exc_info=True)
        return False


def _missing(modules: tuple[str, ...]) -> List[str]:
    """Return the subset of ``modules`` that are not importable (never raises)."""
    return [name for name in modules if not _module_available(name)]


# ---------------------------------------------------------------------------
# Session lifecycle support (Task 19.1).
#
# ``on_session_start`` must initialise the voice-capture and avatar/front-end
# subsystems within a 5 s budget and mark the plugin "running" only when BOTH
# succeed (Requirement 10.1); on any failure / timeout it must release exactly
# the resources allocated this session and stay not-running (Requirement 10.2).
# ``on_session_end`` must release the microphone, audio-playback device and
# front-end window/server within 3 s, wrapping EACH release independently so one
# failure never blocks the others, and summarise which (if any) failed
# (Requirement 10.4).
#
# To release "exactly what was allocated", each successfully-acquired subsystem
# is recorded as an :class:`_AllocatedResource` (a name + an idempotent release
# callable). Both the partial-init cleanup path and ``on_session_end`` then walk
# the same registry, so the two can never drift out of sync.
# ---------------------------------------------------------------------------

#: Hard budget for ``on_session_start`` initialisation (Requirement 10.1).
SESSION_START_BUDGET_S: float = 5.0
#: Hard budget for ``on_session_end`` resource release (Requirement 10.3/10.4).
SESSION_END_BUDGET_S: float = 3.0


@dataclass
class _AllocatedResource:
    """A session-scoped resource paired with its idempotent release callable.

    ``name`` is the human-readable resource label surfaced in the failure
    summary (e.g. ``"microphone"`` / ``"frontend_server"``); ``release`` is a
    zero-arg callable that frees it. The release callable is expected to be
    idempotent and may raise — :meth:`VTuberRuntime._release_all` isolates each
    call in its own ``try``/``except`` so a single failure never blocks the rest
    (Requirement 10.4).
    """

    name: str
    release: Callable[[], None]


class _SessionInitError(RuntimeError):
    """Internal marker for a session-start failure (subsystem error / timeout).

    Raised inside :meth:`VTuberRuntime.on_session_start` to unwind to the single
    cleanup path that releases the resources allocated so far and records the
    error without ever propagating into the host session start (Requirement
    10.2). Never escapes the runtime.
    """


class _GatewayBroadcaster:
    """Adapter exposing a synchronous ``send_event`` over the live gateway.

    :class:`~omnilimb_face.live2d.Live2DDirector` dispatches synchronously
    through the first available ``send_event`` / ``serialize`` gateway method.
    The real :class:`~omnilimb_face.protocol.gateway.ProtocolGateway` instead
    exposes a thread-safe ``broadcast_threadsafe`` (its ``serialize`` only
    *encodes* an event without sending it), so this thin adapter bridges the
    two: its :meth:`send_event` reads whatever gateway the runtime currently has
    started — so a gateway started (or replaced) after the director was built is
    still used — and broadcasts the event to every connected front-end. Returns
    the number of clients reached, or ``None`` when no gateway is started. Never
    raises into the director (which also guards dispatch defensively).
    """

    def __init__(self, runtime: "VTuberRuntime") -> None:
        self._runtime = runtime

    def send_event(self, event: Any) -> Any:
        """Broadcast ``event`` over the runtime's currently-started gateway."""
        gateway = getattr(self._runtime, "_protocol_gateway", None)
        if gateway is None:
            return None
        broadcaster = getattr(gateway, "broadcast_threadsafe", None)
        if callable(broadcaster):
            return broadcaster(event)
        sender = getattr(gateway, "send_event", None)
        if callable(sender):
            return sender(event)
        return None


class VTuberRuntime:
    """Owns the plugin's config and (in later tasks) all of its subsystems.

    The runtime is constructed once by :func:`omnilimb_face.plugin.register` and
    its methods are registered as the plugin's hooks, tools, CLI subcommand and
    slash commands. Construction is cheap and side-effect free apart from
    non-raising dependency probing, so a failure to import an optional voice /
    Live2D extra never prevents registration (Requirement 12.1); the missing
    extras are recorded and surfaced by the availability probes and tool
    handlers instead.

    Args:
        ctx: The host :class:`PluginContext`. Only the generic extension surface
            (``inject_message`` / ``dispatch_tool`` / ``register_*``) is ever
            used; the runtime never writes to or imports any hermes core file
            (Requirement 1.3).
        config: The merged, fully-typed :class:`VTuberConfig` produced by
            :meth:`omnilimb_face.config.ConfigManager.from_host`.
    """

    def __init__(self, ctx: Any, config: VTuberConfig) -> None:
        self._ctx = ctx
        self._config = config

        # Probe optional dependencies once at construction (non-raising). The
        # snapshot drives the degraded-state reporting; the live probes below
        # re-check on demand so a dependency installed mid-session is picked up.
        self._missing_voice: List[str] = _missing(VOICE_MODULES)
        self._missing_wakeword: List[str] = _missing(WAKEWORD_MODULES)
        self._missing_live2d: List[str] = _missing(LIVE2D_MODULES)
        self._missing_protocol: List[str] = _missing(PROTOCOL_MODULES)

        # Whether the plugin is registering in a degraded state (any optional
        # voice dependency missing). Recorded for status reporting (Req 12.1).
        self._degraded: bool = bool(self._missing_voice)

        # Running state placeholder; real lifecycle wiring lands in Task 19.1.
        self._running: bool = False

        # --- Session lifecycle resource management (Task 19.1) ---------------
        # Subsystem handles acquired at session start and released at session
        # end. Populated by :meth:`on_session_start`, cleared by
        # :meth:`on_session_end`.
        self._frontend_server: Any = None
        self._protocol_gateway: Any = None
        self._voice_capture: Any = None
        self._audio_sink: Any = None
        #: Whether the voice subsystem came up in degraded/text-only mode this
        #: session (optional ``[voice]`` deps / microphone stack absent). This is
        #: a *successful* (degraded) init, NOT a failure (Requirement 12).
        self._voice_degraded: bool = False
        #: Resources allocated this session, in acquisition order, so a partial
        #: init or session end releases exactly what was allocated (Req 10.2/10.4).
        self._allocated: List[_AllocatedResource] = []
        #: Last session-start error / session-end summary, surfaced to the CLI
        #: ``status`` / ``doctor`` actions (Task 19.2) and inspectable by tests.
        self._last_lifecycle_error: Optional[str] = None
        self._last_session_end_summary: Optional[str] = None

        # Overridable hooks for testability (Task 19.1). Each factory defaults to
        # ``None`` -> the real default factory is used at session start; a unit
        # test can assign a fake factory (or a fake ``_clock``) to simulate an
        # init failure, a budget overrun, or a partial-release failure without a
        # real microphone / socket / window. The clock is a monotonic source so
        # the 5 s / 3 s budgets are enforced without hard-sleeping.
        self._frontend_server_factory: Optional[Callable[[], Any]] = None
        self._protocol_gateway_factory: Optional[Callable[[], Any]] = None
        self._voice_capture_factory: Optional[Callable[[], Any]] = None
        self._clock: Callable[[], float] = time.monotonic

        # --- End-to-end pipeline collaborators (Task 22.1) -------------------
        # Constructed lazily by :meth:`_ensure_pipeline` (on the first host-reply
        # observation, the first captured voice segment, or at session start) and
        # owned by the runtime. Each is left ``None`` here so importing the
        # runtime and registering never depends on building them, and so a unit
        # test can inject a fake before first use (``_ensure_pipeline`` only
        # builds collaborators that are still ``None``). The whole pipeline is
        # import-safe (no optional voice/Live2D extras), so text-driven turns
        # (host reply -> TTS -> Live2D) work even when the microphone stack is
        # absent, as long as ``ctx.dispatch_tool`` and the gateway are available.
        self._model: Any = None
        self._expression_mapper: Any = None
        self._chunker: Any = None
        self._tts_player: Any = None
        self._live2d_director: Any = None
        self._llm_bridge: Any = None
        self._stt_engine: Any = None
        self._interruption: Any = None
        #: Gateway adapter whose ``send_event`` broadcasts over whatever
        #: ProtocolGateway the runtime currently has started (Task 19.1 / 22.1).
        self._gateway_broadcaster: Any = None
        #: Runtime-managed turn boundary. The LLMBridge leaves its turn flagged
        #: active after ``on_post_llm_call`` (it marks completion, not
        #: inactivity), so the runtime owns the boundary and calls ``begin_turn``
        #: exactly once per turn (so each turn re-fires playback-start / ordered
        #: playback).
        self._turn_in_progress: bool = False
        #: Contiguous 0-based playback sequence for enqueued segments this turn.
        self._playback_seq: int = 0
        #: Guards lazy pipeline construction against concurrent observers.
        self._pipeline_lock = threading.Lock()

        if self._degraded:
            logger.info(
                "omnilimb-face registering in a degraded state; missing voice "
                "dependencies: %s",
                ", ".join(self._missing_voice),
            )

    # ------------------------------------------------------------------
    # Availability probes (used as tool ``check_fn``; MUST NOT raise).
    # ------------------------------------------------------------------
    def deps_available(self, *args: Any, **kwargs: Any) -> bool:
        """Whether the hands-free voice stack is present (Requirements 10.5 / 12.1).

        Used as the ``check_fn`` for the ``vtuber_status`` tool. The status tool
        is gated on the voice stack (microphone capture + VAD) that hands-free
        operation needs, so it returns ``True`` only when every module in
        :data:`VOICE_MODULES` (``sounddevice`` / ``webrtcvad`` / ``numpy``) is
        importable. The probe is re-run live (via :func:`_missing`, which uses
        :func:`importlib.util.find_spec`) so a dependency installed after load is
        picked up, and it reflects *real* optional-dependency availability rather
        than a cached snapshot.

        Never raises — a ``check_fn`` that raised could break ``hermes tools``
        enumeration, so any unexpected probing error degrades to "unavailable".
        """
        try:
            return not _missing(VOICE_MODULES)
        except Exception:  # pragma: no cover - check_fn must never raise
            return False

    def tts_available(self, *args: Any, **kwargs: Any) -> bool:
        """Whether avatar speech (TTS) can currently be driven (Requirements 10.5 / 12.1).

        Used as the ``check_fn`` for the ``vtuber_say`` tool. Avatar speech does
        **not** need a microphone — it reuses the host's registered
        ``text_to_speech`` tool through ``ctx.dispatch_tool`` (design
        "TTS_Player"). Availability therefore reflects the real optional capability
        the tool depends on: a host that exposes a callable ``dispatch_tool``
        through which the ``text_to_speech`` tool can be invoked. Never raises.
        """
        try:
            return callable(getattr(self._ctx, "dispatch_tool", None))
        except Exception:  # pragma: no cover - check_fn must never raise
            return False

    # ------------------------------------------------------------------
    # Tool handlers (MUST return JSON strings — Requirements 12.2 / 12.6).
    # ------------------------------------------------------------------
    def tool_status(self, args: Any = None, **kwargs: Any) -> str:
        """Report the status of each subsystem as a JSON string (Requirements 12.5 / 12.6).

        Always returns a valid JSON object string and never raises into the host.
        The payload reports:

        * the runtime's ``running`` and ``degraded`` state;
        * per-subsystem availability (``voice`` / ``wakeword`` / ``live2d`` /
          ``protocol`` / ``tts``), each a boolean reflecting the *live*
          optional-dependency probe (TTS additionally reflects host
          ``dispatch_tool`` availability);
        * the missing optional dependencies grouped by subsystem; and
        * a summary of the configured model/voice (the reused ``tts`` / ``stt``
          provider/voice/model and the Live2D model name) for operator
          visibility (Requirement 12.6).
        """
        try:
            subsystems = self._subsystem_availability()
            payload: Dict[str, Any] = {
                "ok": True,
                "tool": "vtuber_status",
                "plugin": "omnilimb-face",
                "running": self._running,
                "degraded": self._degraded,
                "subsystems": subsystems,
                # Back-compat flat fields some callers may already read.
                "tts_available": subsystems["tts"],
                "voice_available": subsystems["voice"],
                "missing_dependencies": {
                    "voice": list(self._missing_voice),
                    "wakeword": list(self._missing_wakeword),
                    "live2d": list(self._missing_live2d),
                    "protocol": list(self._missing_protocol),
                },
                "config": self._config_summary(),
            }
            return _json(payload)
        except Exception:  # pragma: no cover - status must never raise
            logger.debug("tool_status assembly failed; returning minimal status", exc_info=True)
            return _json(
                {
                    "ok": True,
                    "tool": "vtuber_status",
                    "plugin": "omnilimb-face",
                    "running": getattr(self, "_running", False),
                    "degraded": getattr(self, "_degraded", True),
                    "detail": "status detail unavailable",
                }
            )

    def tool_say(self, args: Any = None, **kwargs: Any) -> str:
        """Speak text through the avatar (TTS + lip-sync + expression).

        This is the "speak text through the avatar" tool — it needs **no
        microphone**, only TTS. It always returns a valid JSON string and never
        raises into the host.

        * If avatar speech is unavailable (the host cannot dispatch the
          ``text_to_speech`` tool, or required voice deps are missing), it
          returns a descriptive JSON **error** naming the missing capability,
          without affecting any other tool (Requirement 12.2).
        * Otherwise it drives the TTS path: a :class:`~omnilimb_face.tts.TTSPlayer`
          is constructed over ``ctx.dispatch_tool`` and used to synthesize the
          text into an :class:`~omnilimb_face.tts.AudioSegmentOut` (lip-sync
          ``volumes`` + ``slice_length``). The segment is enriched with display
          text + expression indices via :class:`~omnilimb_face.expression.ExpressionMapper`
          when a Live2D emotion map is available (optional), and pushed to a wired
          Live2D director when one exists (the live front-end wiring is owned by
          Tasks 20.1 / 22.1, so this is a defensive optional step). It then
          returns a JSON success summarising what was produced (spoken text,
          number of volume samples, slice length, expression indices, and whether
          it was pushed to a front-end).
        """
        if not self.tts_available():
            missing: List[str] = []
            if not callable(getattr(self._ctx, "dispatch_tool", None)):
                # The host TTS tool is reached via ctx.dispatch_tool.
                missing.append("ctx.dispatch_tool (host text_to_speech tool)")
            missing.extend(self._missing_voice)
            return _json(
                {
                    "ok": False,
                    "tool": "vtuber_say",
                    "error": "tts_unavailable",
                    "missing": missing,
                    "message": (
                        "Avatar speech is unavailable: "
                        + (", ".join(missing) if missing else "no TTS backend")
                        + "."
                    ),
                }
            )

        text = _extract_text(args, kwargs)
        if not text:
            return _json(
                {
                    "ok": False,
                    "tool": "vtuber_say",
                    "error": "missing_text",
                    "message": "No 'text' argument was provided to speak.",
                }
            )

        segment, error = self._produce_audio(text)
        if error is not None:
            # Synthesis failed at the host TTS boundary; report it without
            # raising. Other tools are unaffected (Requirement 12.2).
            return _json(
                {
                    "ok": False,
                    "tool": "vtuber_say",
                    "error": "tts_failed",
                    "text": text,
                    "message": f"Avatar speech failed: {error}",
                }
            )

        pushed = self._maybe_push_segment(segment)
        return _json(
            {
                "ok": True,
                "tool": "vtuber_say",
                "spoken": True,
                "text": segment.display_text or text,
                "volumes": len(segment.volumes),
                "slice_length_ms": segment.slice_length_ms,
                "expressions": list(segment.expressions),
                "pushed_to_frontend": pushed,
                "message": (
                    "Synthesized avatar speech"
                    + (
                        "; pushed to the Live2D front-end."
                        if pushed
                        else " (no live Live2D front-end wired yet; wiring lands "
                        "in Tasks 20.1 / 22.1)."
                    )
                ),
            }
        )

    # ------------------------------------------------------------------
    # Status / TTS helpers (private; all defensive, never raise into host).
    # ------------------------------------------------------------------
    def _subsystem_availability(self) -> Dict[str, bool]:
        """Per-subsystem availability snapshot for :meth:`tool_status`.

        Each flag reflects the live optional-dependency probe (re-run so a
        dependency installed after load is detected). ``tts`` additionally
        depends on the host exposing a callable ``dispatch_tool`` rather than on
        an importable extra. Never raises.
        """
        return {
            "voice": not _missing(VOICE_MODULES),
            "wakeword": not _missing(WAKEWORD_MODULES),
            "live2d": not _missing(LIVE2D_MODULES),
            "protocol": not _missing(PROTOCOL_MODULES),
            "tts": self.tts_available(),
        }

    def _config_summary(self) -> Dict[str, Any]:
        """Compact summary of the configured model/voice for status visibility.

        Reads the reused ``tts`` / ``stt`` settings and the Live2D model name
        defensively (via ``getattr``) so a partial/odd config can never make the
        status tool raise.
        """
        tts = getattr(self._config, "tts", None)
        stt = getattr(self._config, "stt", None)
        live2d = getattr(self._config, "live2d", None)
        return {
            "tts": {
                "provider": getattr(tts, "provider", None),
                "voice": getattr(tts, "voice", None),
                "model": getattr(tts, "model", None),
            },
            "stt": {
                "provider": getattr(stt, "provider", None),
                "model": getattr(stt, "model", None),
                "language": getattr(stt, "language", None),
            },
            "live2d": {
                "model_name": getattr(live2d, "model_name", None),
                "default_expression": getattr(live2d, "default_expression", None),
            },
        }

    def _produce_audio(
        self, text: str
    ) -> Tuple[Optional[AudioSegmentOut], Optional[str]]:
        """Drive the TTS path for ``text`` (Requirement 6 / supports 12.2).

        Returns ``(segment, None)`` on success or ``(None, error_message)`` on
        failure; never raises. Constructs a :class:`~omnilimb_face.tts.TTSPlayer`
        over ``ctx.dispatch_tool`` and prefers its :meth:`TTSPlayer.synthesize`
        (owned by Task 12.1). When ``synthesize`` is not yet implemented in the
        running build, it transparently falls back to an inline synthesis that
        uses the same host ``text_to_speech`` tool plus the pure
        :meth:`TTSPlayer.compute_volumes` lip-sync helper. The produced segment
        carries the marker-stripped spoken text and the mapped expression indices
        (when a Live2D emotion map is available).
        """
        dispatch = getattr(self._ctx, "dispatch_tool", None)
        player = TTSPlayer(
            cfg=self._config.tts, dispatch_tool=dispatch, enable_fallback_tts=True
        )

        # Marker-stripped spoken text + expression indices (optional mapping).
        display_text, expressions = self._map_expressions(text)
        spoken = display_text or text

        # Preferred path: the player's own synthesize() (Task 12.1).
        try:
            result = player.synthesize(spoken)
        except NotImplementedError:
            result = None
        except Exception as exc:  # noqa: BLE001 - genuine synthesis failure (e.g. TTSFailedError)
            logger.debug("TTSPlayer.synthesize failed", exc_info=True)
            return None, f"synthesis error: {exc}"

        if result is not None:
            seg = _coerce_segment(result)
            if seg is not None:
                return seg, None
            # Unrecognised return shape -> fall through to inline synthesis.

        # Inline fallback: dispatch the host text_to_speech tool, decode the
        # produced file, and compute lip-sync volumes locally.
        base, error = self._inline_synthesize(spoken, player)
        if error is not None:
            return None, error
        wav_bytes, volumes, slice_length_ms = base
        segment = AudioSegmentOut(
            wav_bytes=wav_bytes,
            volumes=volumes,
            slice_length_ms=slice_length_ms,
            display_text=spoken,
            expressions=expressions,
        )
        return segment, None

    def _inline_synthesize(
        self, text: str, player: TTSPlayer
    ) -> Tuple[Optional[Tuple[bytes, List[float], int]], Optional[str]]:
        """Synthesize one sentence inline via the host ``text_to_speech`` tool.

        Mirrors what :meth:`TTSPlayer.synthesize` (Task 12.1) will do, kept here
        so :meth:`tool_say` can drive the TTS path before that task lands.
        Returns ``((wav_bytes, volumes, slice_length_ms), None)`` on success or
        ``(None, error_message)`` on failure. Never raises.
        """
        dispatch = getattr(self._ctx, "dispatch_tool", None)
        if not callable(dispatch):
            return None, "ctx.dispatch_tool (host text_to_speech tool) is unavailable"
        try:
            raw = dispatch("text_to_speech", {"text": text})
        except Exception as exc:  # noqa: BLE001 - host tool dispatch must not crash us
            logger.debug("text_to_speech dispatch raised", exc_info=True)
            return None, f"text_to_speech dispatch failed: {exc}"

        envelope = _parse_envelope(raw)
        if envelope is None:
            return None, "text_to_speech returned an unparseable response"
        if not envelope.get("success", False):
            return None, str(envelope.get("error") or "text_to_speech reported failure")

        path = _resolve_audio_path(envelope)
        if not path:
            return None, "text_to_speech response did not include an audio file path"

        decoded = _decode_audio_file(path)
        if decoded is None:
            return None, f"could not read synthesized audio file: {path}"
        pcm, sample_rate, container = decoded
        volumes, slice_len = player.compute_volumes(
            pcm, sample_rate, TTSPlayer.DEFAULT_SLICE_LENGTH_MS
        )
        return (container, volumes, slice_len), None

    def _map_expressions(self, text: str) -> Tuple[str, List[int]]:
        """Best-effort emotion mapping for ``text`` (optional; Requirement 8).

        Returns ``(display_text, expressions)``. Uses an
        :class:`~omnilimb_face.expression.ExpressionMapper` built from the wired
        Live2D model's emotion map when one is available; otherwise the mapper is
        empty (no expression indices) and ``display_text`` is ``text`` with any
        ``[key]`` markers stripped. Defensive: never raises, so it works whether
        or not the Live2D director (Task 17.2 / 22.1) has been wired.
        """
        try:
            emotion_map, default_expr = self._resolve_emotion_map()
            mapper = ExpressionMapper(emotion_map, default_expr)
            result = mapper.map_reply(text)
            return result.display_text, list(result.expressions)
        except Exception:  # noqa: BLE001 - mapping is optional, never fatal
            logger.debug("expression mapping failed; using raw text", exc_info=True)
            return text, []

    def _resolve_emotion_map(self) -> Tuple[Dict[str, int], str]:
        """Resolve ``(emotion_map, default_expression)`` from a wired model.

        Defensively looks for a Live2D model on an optional ``_model`` attribute
        or via an optional ``_live2d_director`` collaborator (both owned by later
        tasks). Returns an empty map and the configured default expression when
        nothing is wired yet, so expression mapping degrades to "no expressions".
        """
        default_expr = getattr(
            getattr(self._config, "live2d", None), "default_expression", "neutral"
        )
        model = getattr(self, "_model", None)
        if model is None:
            director = getattr(self, "_live2d_director", None)
            model = getattr(director, "model", None) if director is not None else None
        emotion_map: Dict[str, int] = {}
        if model is not None:
            raw_map = getattr(model, "emotion_map", None)
            if isinstance(raw_map, dict):
                emotion_map = raw_map
        return emotion_map, default_expr

    def _maybe_push_segment(self, segment: AudioSegmentOut) -> bool:
        """Push ``segment`` to a wired Live2D director when one exists (optional).

        The live front-end/director wiring is owned by Tasks 20.1 / 22.1; until
        then this is a defensive no-op returning ``False``. Returns ``True`` only
        when an optional ``_live2d_director`` collaborator with a callable
        ``push_audio_segment`` accepts the segment. Never raises.
        """
        director = getattr(self, "_live2d_director", None)
        push = getattr(director, "push_audio_segment", None) if director is not None else None
        if callable(push):
            try:
                push(segment)
                return True
            except Exception:  # noqa: BLE001 - pushing must never break the tool
                logger.debug("Live2D director push_audio_segment failed", exc_info=True)
        return False

    # ------------------------------------------------------------------
    # End-to-end pipeline construction + wiring (Task 22.1).
    # ------------------------------------------------------------------
    def _ensure_pipeline(self) -> None:
        """Construct and wire the end-to-end pipeline collaborators (Task 22.1).

        Idempotent and defensive. Builds, in dependency order: the
        :class:`~omnilimb_face.chunker.SentenceChunker`, the
        :class:`~omnilimb_face.expression.ExpressionMapper` (from the active
        Live2D model's emotion map — empty for a placeholder model), the
        :class:`~omnilimb_face.tts.TTSPlayer` (over ``ctx.dispatch_tool``), the
        :class:`~omnilimb_face.live2d.Live2DDirector` (broadcasting over the
        started :class:`ProtocolGateway` via :class:`_GatewayBroadcaster`), the
        :class:`~omnilimb_face.stt.STTEngine`, the
        :class:`~omnilimb_face.llm_bridge.LLMBridge` (with the per-sentence
        :meth:`_drive_sentence_output` sink), and the
        :class:`~omnilimb_face.interruption.InterruptionController`.

        Only collaborators that are still ``None`` are built, so a second call is
        a no-op and a test may inject a fake beforehand. Never raises — a
        construction failure is logged and leaves the path degraded rather than
        breaking a host hook or the capture thread.
        """
        if self._llm_bridge is not None:
            return
        with self._pipeline_lock:
            if self._llm_bridge is not None:
                return
            try:
                self._build_pipeline_locked()
            except Exception:  # never break a host hook / capture thread
                logger.exception(
                    "omnilimb-face: failed to build the end-to-end pipeline; the "
                    "avatar voice/text path is degraded for this session."
                )

    def _build_pipeline_locked(self) -> None:
        """Build the pipeline collaborators (called once under ``_pipeline_lock``).

        Splitting the body out of :meth:`_ensure_pipeline` keeps the locking /
        idempotency guard tiny and this construction logic linear and readable.
        """
        cfg = self._config

        # Active Live2D model + its emotion map (placeholder => empty map, so the
        # ExpressionMapper resolves no expression indices, per the task contract).
        if self._model is None:
            self._model = self._resolve_model_info()
        emotion_map: Dict[str, int] = {}
        raw_map = getattr(self._model, "emotion_map", None)
        if isinstance(raw_map, dict):
            emotion_map = raw_map
        default_expr = getattr(
            getattr(cfg, "live2d", None), "default_expression", "neutral"
        )

        if self._expression_mapper is None:
            self._expression_mapper = ExpressionMapper(emotion_map, default_expr)

        if self._chunker is None:
            self._chunker = SentenceChunker()

        if self._tts_player is None:
            dispatch = getattr(self._ctx, "dispatch_tool", None)
            self._tts_player = TTSPlayer(
                cfg=cfg.tts, dispatch_tool=dispatch, enable_fallback_tts=True
            )

        if self._gateway_broadcaster is None:
            self._gateway_broadcaster = _GatewayBroadcaster(self)

        if self._live2d_director is None:
            self._live2d_director = Live2DDirector(
                self._model,
                self._gateway_broadcaster,
                default_expression=default_expr,
            )

        if self._stt_engine is None:
            self._stt_engine = STTEngine(cfg.stt)

        # Bridge first: its on_playback_start callback reads ``self._interruption``
        # at call time, so the controller can be built either side of it.
        if self._llm_bridge is None:
            self._llm_bridge = LLMBridge(
                self._ctx,
                cfg,
                self._chunker,
                expression_mapper=self._expression_mapper,
                tts_player=self._tts_player,
                live2d_director=self._live2d_director,
                sentence_sink=self._drive_sentence_output,
                on_playback_start=self._on_playback_start,
                on_turn_error=self._on_turn_error,
            )

        if self._interruption is None:
            self._interruption = InterruptionController(
                cfg.interruption,
                tts=self._tts_player,
                bridge=self._llm_bridge,
                vad_settings=cfg.vad,
            )

    def _resolve_model_info(self) -> Any:
        """Load the active :class:`Live2DModelInfo` (placeholder on failure).

        Defers to :meth:`Live2DModelInfo.from_settings`, which already degrades
        to a placeholder (empty emotion map) when the model dict is missing or
        unparseable (Requirement 7.5). Never raises.
        """
        try:
            return Live2DModelInfo.from_settings(getattr(self._config, "live2d", None))
        except Exception:  # noqa: BLE001 - model load is best-effort
            logger.debug(
                "omnilimb-face: Live2D model info load failed; using placeholder",
                exc_info=True,
            )
            return Live2DModelInfo.placeholder()

    def _drive_sentence_output(self, chunk: "ReplyChunk", expressions: List[int]) -> None:
        """Per-sentence sink (Task 22.1): synthesize, enqueue, drive the avatar.

        Wired as the :class:`LLMBridge` ``sentence_sink``. For each completed
        sentence — already marker-stripped to ``chunk.text`` with ``expressions``
        resolved by the :class:`ExpressionMapper` — it:

        * synthesizes the display text into an :class:`AudioSegmentOut` via the
          :class:`TTSPlayer` (``synthesize`` never raises; it returns a failed
          result on error);
        * attaches the expression indices and display text to the segment;
        * enqueues it for ordered playback with a contiguous ``seq`` in sentence
          order (so the player's cursor never stalls on a synthesis gap,
          Requirement 6.2); and
        * pushes it to the :class:`Live2DDirector` so the front-end receives
          audio + lip-sync ``volumes`` + ``expressions`` (broadcast over the
          gateway).

        Each step is defensive: a synthesis failure degrades to a text/
        expression-only frame (the reply is still shown, Requirement 6.5) and no
        exception ever propagates back into the host hook.
        """
        display_text = getattr(chunk, "text", "") or ""
        expr = list(expressions or [])
        if not display_text:
            return

        player = self._tts_player
        result = None
        if player is not None:
            try:
                result = player.synthesize(display_text)
            except Exception:  # noqa: BLE001 - synthesize should not raise; guard anyway
                logger.debug("TTS synthesize raised in sentence sink", exc_info=True)
                result = None

        segment: Optional[AudioSegmentOut] = None
        if result is not None and getattr(result, "success", False):
            base = getattr(result, "segment", None)
            if base is not None:
                try:
                    segment = replace(base, display_text=display_text, expressions=expr)
                except Exception:  # noqa: BLE001 - fall back to the raw segment
                    segment = base

        if segment is not None:
            # Ordered playback: contiguous seq across successfully-enqueued
            # segments so the player's expected-sequence cursor never stalls.
            if player is not None:
                seq = self._playback_seq
                self._playback_seq += 1
                try:
                    player.enqueue(segment, seq=seq)
                except Exception:  # noqa: BLE001 - enqueue must not break observing
                    logger.debug("TTS enqueue failed in sentence sink", exc_info=True)
            self._maybe_push_segment(segment)
            return

        # Synthesis failed -> show the text (Requirement 6.5): drive a no-audio
        # frame so the front-end still displays the sentence and its expressions.
        fallback = AudioSegmentOut(
            wav_bytes=b"",
            volumes=[],
            slice_length_ms=0,
            display_text=display_text,
            expressions=expr,
        )
        self._maybe_push_segment(fallback)

    def _on_playback_start(self) -> None:
        """Begin playback for a new turn: reset ordering, arm barge-in (Task 22.1).

        Fired once per turn by the :class:`LLMBridge` when the first reply text
        flows back (Requirement 3.3). Resets the contiguous playback sequence and
        the :class:`TTSPlayer`'s ordered-playback session (so the new turn's
        segments start at ``seq`` 0 rather than stalling behind the previous
        turn's cursor), then arms the :class:`InterruptionController` so barge-in
        can stop playback and abort the turn while it is being driven
        (Requirement 5). Never raises.
        """
        self._playback_seq = 0
        player = self._tts_player
        if player is not None:
            try:
                player.stop()  # reset the ordered-playback session for this turn
            except Exception:  # noqa: BLE001 - best-effort reset
                logger.debug("TTSPlayer.stop at playback start failed", exc_info=True)
        self._arm_interruption()

    def _on_turn_error(self, exc: Exception) -> None:
        """Record an LLMBridge turn error surfaced from the observer hooks.

        The bridge never raises into the host; it forwards internal errors here.
        We only log — the host turn proceeds untouched and the avatar simply
        stops being driven for the failed sentence (supports Requirement 6.5).
        """
        logger.debug("omnilimb-face LLM bridge turn error (observed): %s", exc)

    def _begin_turn_if_needed(self) -> None:
        """Start a fresh :class:`LLMBridge` turn at a runtime turn boundary.

        The bridge leaves its turn flagged active after ``on_post_llm_call`` (it
        marks completion but not inactivity), so the runtime owns the turn
        boundary: it calls ``begin_turn`` exactly once per turn (clearing the
        chunker and per-turn state) so each new turn re-fires playback-start and
        restarts ordered playback. Never raises.
        """
        if self._turn_in_progress:
            return
        bridge = self._llm_bridge
        if bridge is not None:
            try:
                bridge.begin_turn()
            except Exception:  # noqa: BLE001 - never break the host hook
                logger.debug("LLMBridge.begin_turn failed", exc_info=True)
        self._turn_in_progress = True

    def _arm_interruption(self) -> None:
        """Arm barge-in detection for the current playback (best-effort)."""
        controller = self._interruption
        arm = getattr(controller, "arm", None) if controller is not None else None
        if callable(arm):
            try:
                arm()
            except Exception:  # noqa: BLE001 - arming must never break a turn
                logger.debug("InterruptionController.arm failed", exc_info=True)

    def _disarm_interruption(self) -> None:
        """Disarm barge-in detection when playback is idle (best-effort)."""
        controller = self._interruption
        disarm = getattr(controller, "disarm", None) if controller is not None else None
        if callable(disarm):
            try:
                disarm()
            except Exception:  # noqa: BLE001 - disarming must never break a turn
                logger.debug("InterruptionController.disarm failed", exc_info=True)

    def _on_voice_segment(self, segment: Any) -> None:
        """Handle a completed captured voice segment (Task 22.1; Req 4.4/11.6/11.7).

        Wired as the :class:`VoiceCapture` ``on_segment`` callback. Transcribes
        the segment via the :class:`STTEngine`, drops blank/failed transcripts
        (Requirement 4.5), and injects a non-blank transcript into the active CLI
        session to trigger the host turn (Requirement 4.4). Honours the CLI-only
        scope: when ``inject_message`` returns ``False`` (gateway/messaging mode)
        no host turn is triggered and no voice turn is driven (Requirements 11.6 /
        11.7). Runs on the capture thread, so it never raises — a failure just
        drops the utterance.
        """
        try:
            self._ensure_pipeline()
            stt = self._stt_engine
            bridge = self._llm_bridge
            if stt is None or bridge is None:
                return
            result = stt.transcribe(segment)
            if not getattr(result, "success", False):
                logger.info(
                    "omnilimb-face: dropping voice segment (STT failed: %s)",
                    getattr(result, "error", "unknown"),
                )
                return
            transcript = getattr(result, "transcript", None)
            if transcript is None or getattr(transcript, "is_empty", True):
                logger.debug("omnilimb-face: dropping blank transcript (Req 4.5)")
                return
            text = getattr(transcript, "text", "") or ""
            if not text.strip():
                return
            # CLI-only scope: inject to trigger the host turn (Req 4.4). A False
            # result means gateway mode / no CLI session -> do not drive a voice
            # turn (Req 11.6 / 11.7); the bridge has already logged the reason.
            bridge.inject_user_utterance(text)
        except Exception:  # noqa: BLE001 - capture-thread callback must never raise
            logger.debug("voice segment handling failed", exc_info=True)

    def _wire_voice_pipeline(self, capture: Any) -> None:
        """Wire a started :class:`VoiceCapture` into the pipeline (Task 22.1).

        Ensures the pipeline collaborators exist, registers
        :meth:`_on_voice_segment` as the capture's segment callback (capture ->
        STT -> inject), and rebuilds the :class:`InterruptionController` bound to
        this capture so its ``arm``/``disarm`` can subscribe to the VAD-event
        stream for barge-in (Requirement 5). All steps are best-effort and never
        raise into session start.
        """
        try:
            self._ensure_pipeline()
        except Exception:  # noqa: BLE001 - defensive; _ensure_pipeline is itself guarded
            logger.debug("pipeline ensure during voice wiring failed", exc_info=True)

        on_segment = getattr(capture, "on_segment", None)
        if callable(on_segment):
            try:
                on_segment(self._on_voice_segment)
            except Exception:  # noqa: BLE001 - wiring is best-effort
                logger.debug("VoiceCapture.on_segment wiring failed", exc_info=True)

        # Rebuild the interruption controller bound to this capture so arm() can
        # subscribe to its VAD-event stream and barge-in stops playback + aborts
        # the turn (reuses Task 21.1 arm/disarm).
        try:
            self._interruption = InterruptionController(
                self._config.interruption,
                tts=self._tts_player,
                bridge=self._llm_bridge,
                capture=capture,
                vad_settings=self._config.vad,
            )
        except Exception:  # noqa: BLE001 - keep the prior controller on failure
            logger.debug(
                "rebuilding interruption controller with capture failed",
                exc_info=True,
            )

    def _teardown_pipeline(self) -> None:
        """Quiesce the pipeline at session end (Task 22.1; best-effort).

        Disarms barge-in and stops the ordered-playback worker so no audio is
        driven after the session ends, and clears the runtime turn boundary. The
        collaborators themselves are kept (cheap, reusable, and the director
        reads the live gateway dynamically); only their activity is stopped.
        Never raises.
        """
        self._turn_in_progress = False
        self._disarm_interruption()
        player = self._tts_player
        if player is not None:
            try:
                player.stop()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                logger.debug("TTSPlayer.stop during teardown failed", exc_info=True)

    # ------------------------------------------------------------------
    # Lifecycle hooks (Task 19.1 — real resource management).
    # ------------------------------------------------------------------
    def on_session_start(self, *args: Any, **kwargs: Any) -> None:
        """``on_session_start`` hook: bring up the session's subsystems (Req 10.1/10.2).

        Initialises the avatar/front-end subsystem (a
        :class:`~omnilimb_face.frontend_server.FrontendStaticServer` plus the
        ``/client-ws`` :class:`~omnilimb_face.protocol.gateway.ProtocolGateway`)
        and the voice-capture subsystem within a **5 second** budget, and marks
        the plugin ``running`` **only when both succeed** (Requirement 10.1).

        "Both succeed" means each subsystem *either started or cleanly degraded*:
        because the hands-free voice dependencies are optional and frequently
        absent, a missing microphone stack is treated as a **successful**
        degraded/text-only init (the Requirement 12 degradation contract), not a
        session failure. Only a genuine exception — or exceeding the 5 s budget —
        is a failure.

        On failure / timeout (Requirement 10.2) every resource allocated *this
        session* is released (via the same registry :meth:`on_session_end` uses,
        so partial inits release exactly what was acquired), a descriptive error
        is recorded in :attr:`_last_lifecycle_error`, and the plugin is left
        not-running. This handler never propagates an exception into the host
        session start.

        Idempotent: a call while already running is a no-op.
        """
        if self._running:
            logger.debug("omnilimb-face on_session_start called while already running; no-op")
            return None

        # Fresh session: reset the registry and degraded flag.
        self._allocated = []
        self._voice_degraded = False
        self._last_lifecycle_error = None
        started_at = self._clock()

        try:
            # 1) Avatar / front-end subsystem (front-end window/server + gateway).
            self._init_avatar_subsystem()
            self._check_budget(started_at, "avatar subsystem")

            # 2) Voice-capture subsystem (degrades cleanly to text-only).
            self._init_voice_subsystem()
            self._check_budget(started_at, "voice subsystem")
        except Exception as exc:  # noqa: BLE001 - never crash host session start
            # Either subsystem failed or the 5 s budget was exceeded -> release
            # exactly what was allocated and stay not-running (Requirement 10.2).
            failed = self._release_all(SESSION_END_BUDGET_S)
            self._running = False
            detail = (
                f"omnilimb-face session start failed: {exc}"
                + (
                    f"; additionally failed to release: {', '.join(failed)}"
                    if failed
                    else ""
                )
            )
            self._last_lifecycle_error = detail
            logger.error(detail, exc_info=True)
            return None

        # Both subsystems initialised (each started or cleanly degraded) -> running.
        # Build the end-to-end pipeline so text-driven turns (host reply -> TTS ->
        # Live2D) work this session even in voice-degraded mode; the director
        # broadcasts over the gateway started above (Task 22.1). Defensive: never
        # raises, so a pipeline build failure can't fail an otherwise-good start.
        self._ensure_pipeline()
        self._running = True
        logger.info(
            "omnilimb-face session started (running=True, voice=%s, resources=%s)",
            "degraded" if self._voice_degraded else "ready",
            ", ".join(r.name for r in self._allocated) or "none",
        )
        return None

    def on_session_end(self, *args: Any, **kwargs: Any) -> None:
        """``on_session_end`` hook: release the session's resources (Req 10.3/10.4).

        Releases the microphone, audio-playback device and front-end
        window/server within a **3 second** budget. Each release is wrapped in
        its **own** ``try``/``except`` so a single failure never blocks the
        others (Requirement 10.4); the names of any resources that failed to
        release are summarised in :attr:`_last_session_end_summary` and logged.
        The plugin state is set to **stopped** (``running = False``) regardless
        of release outcomes, and this handler never propagates an exception into
        the host session end.
        """
        # Quiesce the end-to-end pipeline first (stop ordered playback, disarm
        # barge-in) so no audio is driven while resources are torn down (Task 22.1).
        self._teardown_pipeline()
        failed = self._release_all(SESSION_END_BUDGET_S)

        # State is "stopped" regardless of per-resource release outcomes.
        self._running = False
        self._frontend_server = None
        self._protocol_gateway = None
        self._voice_capture = None
        self._audio_sink = None
        self._voice_degraded = False

        if failed:
            summary = (
                "omnilimb-face stopped; the following resources failed to "
                f"release: {', '.join(failed)}."
            )
            logger.error(summary)
        else:
            summary = "omnilimb-face stopped; all resources released."
            logger.debug(summary)
        self._last_session_end_summary = summary
        return None

    # ------------------------------------------------------------------
    # Lifecycle helpers (Task 19.1; all defensive, never crash the host).
    # ------------------------------------------------------------------
    def _init_avatar_subsystem(self) -> None:
        """Construct + start the avatar/front-end subsystem (supports Req 10.1).

        Brings up the front-end window/server (static asset hosting for the
        Live2D renderer) and the ``/client-ws`` WebSocket gateway that drives it.
        Each handle is recorded in the resource registry **immediately after** it
        starts, so if the gateway fails to start the already-started front-end
        server is still released by the cleanup path (Requirement 10.2). A
        genuine exception from either ``start`` propagates and is treated as an
        avatar-subsystem init failure by :meth:`on_session_start`.
        """
        # Front-end window/server.
        server = self._build(self._frontend_server_factory, self._default_frontend_server)
        starter = getattr(server, "start", None)
        if callable(starter):
            starter()  # genuine exception here => avatar init failure (Req 10.2)
        self._frontend_server = server
        self._track("frontend_server", lambda: self._release_frontend_server(server))

        # /client-ws WebSocket gateway.
        gateway = self._build(self._protocol_gateway_factory, self._default_protocol_gateway)
        starter = getattr(gateway, "start_in_thread", None)
        if callable(starter):
            starter()  # genuine exception here => avatar init failure (Req 10.2)
        self._protocol_gateway = gateway
        self._track("protocol_gateway", lambda: self._release_protocol_gateway(gateway))

    def _init_voice_subsystem(self) -> None:
        """Prepare the voice-capture subsystem, degrading cleanly (Req 10.1/12).

        When the optional ``[voice]`` dependencies are absent (or the microphone
        stack cannot be opened, e.g. PortAudio missing), the subsystem comes up
        in **degraded/text-only** mode: this is a *successful* init per the
        Requirement 12 degradation contract, not a session failure, so no
        microphone resource is allocated and the session still starts. Any other,
        genuinely unexpected exception propagates and fails session start
        (Requirement 10.2).
        """
        factory = self._voice_capture_factory

        # No override + optional voice deps missing -> text-only degraded mode.
        if factory is None and self._missing_voice:
            self._voice_degraded = True
            logger.info(
                "omnilimb-face voice subsystem degraded (text-only); missing "
                "voice dependencies: %s",
                ", ".join(self._missing_voice),
            )
            return

        try:
            capture = self._build(factory, self._default_voice_capture)
        except Exception as exc:  # noqa: BLE001 - classify mic-absent vs genuine
            if self._is_microphone_unavailable(exc):
                # Microphone stack absent -> degrade, do NOT fail the session.
                self._voice_degraded = True
                logger.info(
                    "omnilimb-face voice subsystem degraded (text-only); "
                    "microphone unavailable: %s",
                    exc,
                )
                return
            raise  # genuine, unexpected error -> session start failure (Req 10.2)

        self._voice_capture = capture
        self._voice_degraded = False
        # Microphone resource (released via VoiceCapture.stop_hands_free).
        self._track("microphone", lambda: self._release_voice_capture(capture))
        # Audio-playback device, when the capture/player exposes a sink to free.
        sink = getattr(capture, "sink", None) or getattr(self, "_audio_sink", None)
        if sink is not None:
            self._audio_sink = sink
            self._track("audio_playback", lambda: self._release_audio_sink(sink))
        # Wire the captured-segment -> STT -> inject path and bind the capture
        # into the interruption controller for barge-in (Task 22.1).
        self._wire_voice_pipeline(capture)

    def _build(self, factory: Optional[Callable[[], Any]], default: Callable[[], Any]) -> Any:
        """Return ``factory()`` when an override is set, else ``default()``.

        Lets a unit test inject a fake subsystem (to simulate an init failure or
        a partial-release failure) while production uses the real default
        factory. Exceptions propagate to the caller's init-failure handling.
        """
        return factory() if factory is not None else default()

    def _track(self, name: str, release: Callable[[], None]) -> None:
        """Record an allocated resource and its release callable (Req 10.2/10.4)."""
        self._allocated.append(_AllocatedResource(name=name, release=release))

    def _check_budget(self, started_at: float, phase: str) -> None:
        """Raise :class:`_SessionInitError` when the 5 s start budget is exceeded.

        Enforces Requirement 10.1's timing budget with the injectable monotonic
        :attr:`_clock` (so tests can simulate a timeout without hard-sleeping).
        """
        if self._budget_exceeded(started_at, SESSION_START_BUDGET_S):
            raise _SessionInitError(
                f"initialization exceeded the {SESSION_START_BUDGET_S:.0f}s budget "
                f"after {phase}"
            )

    def _budget_exceeded(self, started_at: float, budget_s: float) -> bool:
        """Whether ``budget_s`` seconds have elapsed since ``started_at`` (never raises)."""
        try:
            return (self._clock() - started_at) > budget_s
        except Exception:  # pragma: no cover - defensive: a bad clock never crashes us
            return False

    def _release_all(self, budget_s: float) -> List[str]:
        """Release every tracked resource, isolating failures (Req 10.4).

        Walks the registry in reverse acquisition order (LIFO) and calls each
        release callable inside its own ``try``/``except`` so one failure never
        blocks the others. Returns the names of the resources whose release
        raised. The ``budget_s`` soft-bound is measured with the injectable
        monotonic clock and logged if exceeded, but releasing every resource
        still takes priority over the soft budget. The registry is cleared on
        return so a resource is never released twice.
        """
        started_at = self._clock()
        failed: List[str] = []
        for resource in reversed(self._allocated):
            try:
                resource.release()
            except Exception:  # noqa: BLE001 - per-resource isolation (Req 10.4)
                failed.append(resource.name)
                logger.warning(
                    "omnilimb-face failed to release resource %r",
                    resource.name,
                    exc_info=True,
                )
        if self._budget_exceeded(started_at, budget_s):
            logger.warning(
                "omnilimb-face resource release exceeded the %.0fs budget.",
                budget_s,
            )
        self._allocated = []
        return failed

    @staticmethod
    def _is_microphone_unavailable(exc: BaseException) -> bool:
        """Whether ``exc`` is the catchable "microphone stack absent" error (Req 12).

        Imports :class:`~omnilimb_face.voice.capture.MicrophoneUnavailableError`
        lazily (so the runtime never hard-depends on the optional ``[voice]``
        extra) and reports whether ``exc`` is an instance. Returns ``False`` when
        the symbol cannot be imported, so an unexpected error is never silently
        downgraded to a degradation.
        """
        try:
            from omnilimb_face.voice.capture import MicrophoneUnavailableError
        except Exception:  # pragma: no cover - voice module should import cleanly
            return False
        return isinstance(exc, MicrophoneUnavailableError)

    # -- Default subsystem factories (used when no override is set) ----------
    def _default_frontend_server(self) -> Any:
        """Build the real front-end static server from the protocol settings."""
        from omnilimb_face.frontend_server import FrontendStaticServer

        return FrontendStaticServer.from_protocol_settings(
            getattr(self._config, "protocol", None)
        )

    def _default_protocol_gateway(self) -> Any:
        """Build the real ``/client-ws`` gateway wired to a :class:`MessageRouter`."""
        from omnilimb_face.protocol.gateway import ProtocolGateway
        from omnilimb_face.protocol.router import MessageRouter

        return ProtocolGateway(
            cfg=getattr(self._config, "protocol", None), router=MessageRouter()
        )

    def _default_voice_capture(self) -> Any:
        """Build the real :class:`VoiceCapture` over a sounddevice source + VAD.

        Raises :class:`~omnilimb_face.voice.capture.MicrophoneUnavailableError`
        when the optional ``[voice]`` dependency / microphone stack is absent;
        :meth:`_init_voice_subsystem` catches that and degrades to text-only
        rather than failing the session (Requirement 12).
        """
        from omnilimb_face.voice.capture import SoundDeviceAudioSource, VoiceCapture
        from omnilimb_face.voice.vad import VadSegmenter

        source = SoundDeviceAudioSource(self._config.vad)
        vad = VadSegmenter(self._config.vad)
        return VoiceCapture(self._config, source, vad)

    # -- Per-resource release helpers (best-effort; may raise to the registry) -
    def _release_frontend_server(self, server: Any) -> None:
        """Stop the front-end static server (front-end window/server resource)."""
        stop = getattr(server, "stop", None)
        if callable(stop):
            stop()

    def _release_protocol_gateway(self, gateway: Any) -> None:
        """Stop the ``/client-ws`` gateway server (front-end window/server resource)."""
        stop = getattr(gateway, "stop", None)
        if callable(stop):
            stop()

    def _release_voice_capture(self, capture: Any) -> None:
        """Release the microphone by turning hands-free capture off."""
        stop = getattr(capture, "stop_hands_free", None)
        if callable(stop):
            stop()

    def _release_audio_sink(self, sink: Any) -> None:
        """Release the audio-playback device (stop the TTS audio sink)."""
        stop = getattr(sink, "stop", None)
        if callable(stop):
            stop()

    def on_llm_output(self, text: Any = None, **kwargs: Any) -> None:
        """``transform_llm_output`` observer — forwards to the LLMBridge (Task 22.1).

        Forwards the streamed reply fragment to
        :meth:`omnilimb_face.llm_bridge.LLMBridge.on_llm_output`, which chunks it
        into complete sentences and drives the per-sentence
        :meth:`_drive_sentence_output` sink (TTS + lip-sync volumes + expression
        indices -> Live2D). As an **observer** it never rewrites the host output,
        so it **always returns ``None``** (the host treats ``None``/empty as
        "leave unchanged"); any failure in the pipeline is caught and logged and
        never raised into the host turn.
        """
        try:
            self._ensure_pipeline()
            self._begin_turn_if_needed()
            bridge = self._llm_bridge
            if bridge is not None:
                bridge.on_llm_output(text, **kwargs)
        except Exception:  # noqa: BLE001 - observer must never break the host turn
            logger.debug("on_llm_output observer failed", exc_info=True)
        # ALWAYS None: observer, never replaces the host's reply text.
        return None

    def on_post_llm_call(self, reply_text: Any = None, **kwargs: Any) -> None:
        """``post_llm_call`` fallback observer — flush the final sentence (Task 22.1).

        Forwards to :meth:`omnilimb_face.llm_bridge.LLMBridge.on_post_llm_call`
        so the chunker's trailing residual is synthesised as the final sentence
        (and, for a host that only fires ``post_llm_call`` with the whole reply,
        the full text is chunked and driven). Then closes the runtime turn
        boundary and disarms barge-in so the next fragment starts a fresh turn.
        Never raises into the host.
        """
        try:
            self._ensure_pipeline()
            self._begin_turn_if_needed()
            bridge = self._llm_bridge
            if bridge is not None:
                bridge.on_post_llm_call(reply_text, **kwargs)
        except Exception:  # noqa: BLE001 - observer must never break the host turn
            logger.debug("on_post_llm_call observer failed", exc_info=True)
        finally:
            # End the runtime turn boundary so the next fragment starts a fresh
            # turn, and stop listening for barge-in until the next playback.
            self._turn_in_progress = False
            self._disarm_interruption()
        return None

    # ------------------------------------------------------------------
    # CLI / slash command helpers (Task 19.2; all defensive, never raise).
    # ------------------------------------------------------------------
    def _all_missing_dependencies(self) -> Dict[str, List[str]]:
        """Live per-subsystem missing-dependency map (re-probed; never raises).

        Re-runs :func:`_missing` for each optional-dependency group so a
        dependency installed after load is reflected, mirroring what
        :meth:`tool_status` reports but grouped for the CLI ``status`` / ``doctor``
        actions.
        """
        return {
            "voice": list(_missing(VOICE_MODULES)),
            "wakeword": list(_missing(WAKEWORD_MODULES)),
            "live2d": list(_missing(LIVE2D_MODULES)),
            "protocol": list(_missing(PROTOCOL_MODULES)),
        }

    def _list_input_devices(self) -> List[str]:
        """Enumerate microphone input devices for diagnostics (never raises).

        Defers to :meth:`SoundDeviceAudioSource.list_input_devices`, which
        returns ``[]`` (rather than raising) when the optional ``[voice]`` stack
        is absent or the query fails, so the ``doctor`` action degrades cleanly.
        """
        try:
            from omnilimb_face.voice.capture import SoundDeviceAudioSource

            return list(SoundDeviceAudioSource.list_input_devices())
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("microphone enumeration failed", exc_info=True)
            return []

    def _cli_status_text(self) -> str:
        """Concise human-readable status line (Requirement 10.5 status action).

        Derived from the same subsystem probes the ``vtuber_status`` tool uses
        (:meth:`_subsystem_availability`), summarising the running/degraded state,
        which subsystems are ready vs unavailable, the missing optional
        dependencies, and the last lifecycle error (when any).
        """
        subs = self._subsystem_availability()
        if self._running and not self._degraded:
            headline = "running"
        elif self._running and self._degraded:
            headline = "running (voice degraded)"
        elif self._degraded:
            headline = "stopped (degraded)"
        else:
            headline = "stopped"

        ready = sorted(name for name, ok in subs.items() if ok)
        unavailable = sorted(name for name, ok in subs.items() if not ok)
        flat_missing = sorted(
            {dep for deps in self._all_missing_dependencies().values() for dep in deps}
        )

        parts = [f"omnilimb-face: {headline}."]
        parts.append("ready: " + (", ".join(ready) if ready else "none") + ".")
        if unavailable:
            parts.append("unavailable: " + ", ".join(unavailable) + ".")
        if flat_missing:
            parts.append("missing dependencies: " + ", ".join(flat_missing) + ".")
        if self._last_lifecycle_error:
            parts.append(f"last error: {self._last_lifecycle_error}")
        return " ".join(parts)

    def _cli_start(self) -> str:
        """Start the avatar UI / voice loop (Requirements 10.5 / 10.6).

        Reuses the :meth:`on_session_start` lifecycle path (front-end window /
        server + ``/client-ws`` gateway, with the voice loop degrading to
        text-only when its optional deps are absent). ``on_session_start`` never
        raises, backgrounds the heavy server work on daemon threads and enforces
        its own start budget, so this returns quick success/failure feedback
        within the 2 s budget. Reports ``already running`` when invoked twice.
        """
        was_running = self._running
        self.on_session_start()
        if self._running:
            if was_running:
                return "omnilimb-face: already running. " + self._cli_status_text()
            return "omnilimb-face: started. " + self._cli_status_text()
        error = self._last_lifecycle_error or "unknown error"
        return f"omnilimb-face: failed to start: {error}"

    def _cli_stop(self) -> str:
        """Stop the avatar UI / voice loop (Requirements 10.5 / 10.6 / 10.4).

        Reuses the :meth:`on_session_end` release path, which frees the
        microphone, audio-playback device and front-end window/server with each
        release isolated so one failure never blocks the others, and summarises
        which (if any) failed. ``on_session_end`` never raises and sets the state
        to stopped regardless, so this returns quickly.
        """
        was_running = self._running
        self.on_session_end()
        summary = self._last_session_end_summary or "omnilimb-face stopped."
        if not was_running:
            return "omnilimb-face: already stopped. " + summary
        return summary

    def _cli_doctor(self) -> str:
        """Quick diagnostics for the ``doctor`` action (Requirement 10.5).

        Reports, as a short findings list: which optional-dependency groups are
        present vs missing, whether any microphone input device is enumerated,
        whether the host ``dispatch_tool`` (the TTS path) is reachable, the
        configured gateway / asset ports, and the current running/degraded state
        plus the last lifecycle error. Never raises and returns promptly.
        """
        findings: List[str] = []

        # 1) Optional dependencies present?
        for group, missing in self._all_missing_dependencies().items():
            if missing:
                findings.append(
                    f"[warn] {group} dependencies missing: {', '.join(missing)}"
                )
            else:
                findings.append(f"[ok] {group} dependencies present")

        # 2) Microphone devices.
        devices = self._list_input_devices()
        if devices:
            preview = ", ".join(devices[:3]) + ("…" if len(devices) > 3 else "")
            findings.append(
                f"[ok] {len(devices)} microphone input device(s) enumerated ({preview})"
            )
        else:
            findings.append(
                "[warn] no microphone input devices enumerated; hands-free is "
                "unavailable (text interaction and avatar rendering remain available)"
            )

        # 3) Host dispatch_tool (TTS path).
        if self.tts_available():
            findings.append("[ok] host dispatch_tool available (avatar speech reachable)")
        else:
            findings.append(
                "[warn] host dispatch_tool unavailable (avatar speech disabled)"
            )

        # 4) Gateway / asset ports.
        proto = getattr(self._config, "protocol", None)
        host = getattr(proto, "host", "127.0.0.1")
        port = getattr(proto, "port", 12393)
        try:
            asset_port = int(port) + 1
        except (TypeError, ValueError):
            asset_port = port
        findings.append(
            f"[info] /client-ws gateway target {host}:{port}; front-end asset "
            f"port {asset_port}"
        )

        # 5) Running / degraded state.
        findings.append(
            f"[info] running={self._running}, degraded={self._degraded}, "
            f"voice_degraded={self._voice_degraded}"
        )
        if self._last_lifecycle_error:
            findings.append(f"[info] last lifecycle error: {self._last_lifecycle_error}")

        return "omnilimb-face doctor:\n" + "\n".join(findings)

    def _handsfree_toggle(self, action: str) -> str:
        """Toggle / report hands-free voice mode (Requirements 4.6 / 4.9 / 12).

        ``action`` is one of ``on`` / ``off`` / ``status``. Hands-free depends on
        the optional voice stack: when its dependencies are missing this returns
        a clear unavailable message naming them; otherwise it drives the
        :class:`~omnilimb_face.voice.capture.VoiceCapture` subsystem. The capture
        subsystem is built on demand when not already constructed (e.g. the
        command is used outside a session). Never raises (callers wrap it).
        """
        missing = _missing(VOICE_MODULES)
        capture = getattr(self, "_voice_capture", None)

        if action == "status":
            if capture is None:
                if missing:
                    return (
                        "omnilimb-face: hands-free is unavailable; missing voice "
                        f"dependencies: {', '.join(missing)} (text interaction and "
                        "avatar rendering remain available)."
                    )
                return (
                    "omnilimb-face: hands-free is off (voice capture is not active; "
                    "run /handsfree on to start listening)."
                )
            running = False
            is_running = getattr(capture, "is_running", None)
            if callable(is_running):
                running = bool(is_running())
            return f"omnilimb-face: hands-free is {'on' if running else 'off'}."

        if action == "off":
            if capture is None:
                return "omnilimb-face: hands-free is already off."
            stop = getattr(capture, "stop_hands_free", None)
            if callable(stop):
                stop()
            return "omnilimb-face: hands-free turned off."

        # action == "on"
        if missing:
            return (
                "omnilimb-face: cannot enable hands-free; the voice stack is "
                f"unavailable (missing dependencies: {', '.join(missing)}). Text "
                "interaction and avatar rendering remain available."
            )
        if capture is None:
            # Build the capture subsystem on demand (e.g. outside a session).
            try:
                capture = self._default_voice_capture()
            except Exception as exc:  # noqa: BLE001 - classify mic-absent gracefully
                logger.debug("on-demand voice capture construction failed", exc_info=True)
                return (
                    "omnilimb-face: cannot enable hands-free; the microphone is "
                    f"unavailable ({exc}). Text interaction and avatar rendering "
                    "remain available."
                )
            self._voice_capture = capture

        start = getattr(capture, "start_hands_free", None)
        if not callable(start):
            return (
                "omnilimb-face: hands-free is unavailable (voice capture does not "
                "support activation)."
            )
        result = start()
        activated = getattr(result, "activated", None)
        if activated is None:
            activated = bool(result)
        if activated:
            return "omnilimb-face: hands-free turned on; listening for speech."
        reason = (
            getattr(result, "reason", "")
            or getattr(result, "error", "")
            or "microphone unavailable"
        )
        return f"omnilimb-face: hands-free could not be enabled: {reason}"

    # ------------------------------------------------------------------
    # CLI subcommand parser (Requirement 10.5).
    # ------------------------------------------------------------------
    def build_cli_parser(self, subparser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """Populate the ``hermes vtuber`` argparse subparser.

        Adds the ``start|stop|status|doctor`` action positional (defaulting to
        ``status``) that :meth:`handle_cli` dispatches on. Returns the subparser
        for convenience. Guards against a host that hands a non-argparse object
        by returning it unchanged.
        """
        add_argument = getattr(subparser, "add_argument", None)
        if not callable(add_argument):
            return subparser
        add_argument(
            "action",
            nargs="?",
            default="status",
            choices=["start", "stop", "status", "doctor"],
            help="Avatar/voice control action (default: status).",
        )
        return subparser

    def handle_cli(self, args: Any = None, **kwargs: Any) -> str:
        """Handle a ``hermes vtuber <action>`` invocation (Requirements 10.5 / 10.6).

        Dispatches the ``start|stop|status|doctor`` action positional populated
        by :meth:`build_cli_parser` and always returns a concise, human-readable
        status string **within a ~2 s feedback budget** (Requirement 10.6):

        * ``start`` — bring up the avatar UI / voice loop via the same lifecycle
          start path as :meth:`on_session_start` (front-end window/server +
          ``/client-ws`` gateway; the voice loop degrades to text-only when its
          optional deps are absent). The heavy server work is backgrounded on
          daemon threads, so this reports success/failure quickly rather than
          blocking on I/O (Requirement 10.5/10.6).
        * ``stop`` — tear the avatar UI / voice loop down via the same ordered,
          failure-isolating release path as :meth:`on_session_end` and report
          which resources (if any) failed to release.
        * ``status`` — a concise running/degraded/missing-deps summary derived
          from the same subsystem probes the ``vtuber_status`` tool uses.
        * ``doctor`` — quick diagnostics (optional deps present? microphone
          devices enumerated? host ``dispatch_tool`` reachable? gateway/asset
          ports?) reported as a short findings list.

        Never raises into the host: any unexpected error is caught and returned
        as a descriptive string so the CLI dispatch can never crash the session.
        """
        action = getattr(args, "action", None)
        if not action:
            action = "status"
        action = str(action).strip().lower()
        try:
            if action == "start":
                return self._cli_start()
            if action == "stop":
                return self._cli_stop()
            if action == "doctor":
                return self._cli_doctor()
            if action == "status":
                return self._cli_status_text()
            # Unknown action: report it and fall back to the status summary so
            # the caller still gets useful, non-empty feedback.
            return (
                f"omnilimb-face: unknown action {action!r}; valid actions are "
                "start, stop, status, doctor. " + self._cli_status_text()
            )
        except Exception as exc:  # noqa: BLE001 - CLI dispatch must never raise
            logger.debug("handle_cli(%r) failed", action, exc_info=True)
            return f"omnilimb-face: '{action}' failed: {exc}"

    # ------------------------------------------------------------------
    # Slash commands (Requirements 4.6 / 10.5).
    # ------------------------------------------------------------------
    def slash_vtuber(self, raw: str = "", **kwargs: Any) -> str:
        """``/vtuber [start|stop|status]`` slash command (Requirement 10.5).

        Parses the first whitespace-delimited token of ``raw`` as the action and
        delegates to the same handlers as the ``hermes vtuber`` CLI subcommand
        (:meth:`handle_cli`), so the slash command and CLI behave identically.
        An empty argument defaults to ``status``. ``doctor`` is also accepted for
        parity with the CLI. Returns a human-readable string and never raises.
        """
        tokens = (raw or "").strip().split()
        action = tokens[0].lower() if tokens else "status"
        if action not in ("start", "stop", "status", "doctor"):
            return (
                f"omnilimb-face: /vtuber {action!r} is not recognised; use "
                "/vtuber [start|stop|status]. " + self._cli_status_text()
            )
        return self.handle_cli(argparse.Namespace(action=action))

    def slash_handsfree(self, raw: str = "", **kwargs: Any) -> str:
        """``/handsfree [on|off]`` slash command (Requirements 4.6 / 4.9 / 12).

        Parses the first token of ``raw`` as ``on`` / ``off`` (an empty argument
        reports the current state) and toggles hands-free voice mode through the
        :class:`~omnilimb_face.voice.capture.VoiceCapture` subsystem
        (``start_hands_free`` / ``stop_hands_free``) when the voice stack is
        available.

        When the voice stack is unavailable — the optional ``[voice]``
        dependencies are missing, or no microphone is enumerated — it returns a
        clear message explaining hands-free is unavailable (naming the missing
        dependencies) while making plain that text interaction and avatar
        rendering remain available (Requirements 4.9 / 11.5 / 12). Defensive: if
        the capture subsystem is not constructed yet (e.g. the command is used
        outside a session) it is built on demand, and any failure degrades to a
        descriptive message rather than raising.
        """
        tokens = (raw or "").strip().split()
        action = tokens[0].lower() if tokens else "status"
        if action not in ("on", "off", "status"):
            return (
                f"omnilimb-face: /handsfree {action!r} is not recognised; use "
                "/handsfree [on|off]."
            )
        try:
            return self._handsfree_toggle(action)
        except Exception as exc:  # noqa: BLE001 - slash command must never raise
            logger.debug("slash_handsfree(%r) failed", action, exc_info=True)
            return f"omnilimb-face: hands-free '{action}' failed: {exc}"


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


def _json(payload: Dict[str, Any]) -> str:
    """Serialise a tool payload to a compact JSON string.

    ``ensure_ascii=False`` preserves non-ASCII text; ``default=str`` keeps the
    serialisation total so a tool handler can never raise while reporting.
    """
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - last-resort, must return JSON
        return json.dumps({"ok": False, "error": "serialization_failed"})


def _extract_text(args: Any, kwargs: Dict[str, Any]) -> str:
    """Best-effort extraction of the ``text`` argument for ``vtuber_say``.

    Accepts the tool arguments as a dict (``{"text": ...}``), a bare string, or
    a ``text=`` keyword, returning ``""`` when nothing usable is present.
    """
    if isinstance(args, dict):
        value = args.get("text")
        if isinstance(value, str):
            return value
        if value is not None:
            return str(value)
    elif isinstance(args, str):
        return args
    kw_value = kwargs.get("text")
    if isinstance(kw_value, str):
        return kw_value
    if kw_value is not None:
        return str(kw_value)
    return ""


def _parse_envelope(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse the host ``text_to_speech`` return value into a dict (or ``None``).

    ``ctx.dispatch_tool`` may hand back the tool's JSON string envelope or, in
    some hosts/tests, the already-decoded dict. Both are accepted; anything that
    is neither a dict nor a JSON object string yields ``None`` (treated as an
    unparseable response by the caller). Never raises.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _resolve_audio_path(envelope: Dict[str, Any]) -> str:
    """Extract the synthesized audio file path from a ``text_to_speech`` envelope.

    Prefers the explicit ``file_path`` field; falls back to stripping the
    ``MEDIA:`` prefix from the ``media_tag`` field (which may also carry a
    leading ``[[audio_as_voice]]`` marker). Returns ``""`` when neither yields a
    usable path.
    """
    path = envelope.get("file_path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    media = envelope.get("media_tag")
    if isinstance(media, str) and "MEDIA:" in media:
        return media.split("MEDIA:", 1)[1].strip()
    return ""


def _decode_audio_file(path: str) -> Optional[Tuple[bytes, int, bytes]]:
    """Read an audio file -> ``(pcm_bytes, sample_rate, container_bytes)``.

    Prefers a proper WAV decode (correct header handling, accurate sample rate).
    On any decode failure it falls back to treating the raw bytes as int16 mono
    little-endian at 16 kHz so a non-WAV / odd file still yields a (degraded but
    valid) lip-sync volume series. Returns ``None`` only when the file cannot be
    read at all. Never raises.
    """
    try:
        container = Path(path).read_bytes()
    except OSError:
        logger.debug("could not read synthesized audio file %r", path, exc_info=True)
        return None
    try:
        with contextlib.closing(wave.open(io.BytesIO(container), "rb")) as wav:
            sample_rate = wav.getframerate() or 16000
            pcm = wav.readframes(wav.getnframes())
            return pcm, sample_rate, container
    except Exception:  # noqa: BLE001 - not a WAV / unreadable header -> raw fallback
        return container, 16000, container


def _coerce_segment(result: Any) -> Optional[AudioSegmentOut]:
    """Normalise a :meth:`TTSPlayer.synthesize` return value into a segment.

    Accepts an :class:`~omnilimb_face.tts.AudioSegmentOut`, a duck-typed object
    exposing ``wav_bytes`` + ``volumes``, or a wrapper carrying such an object on
    a ``.segment`` attribute (so this keeps working regardless of the concrete
    shape Task 12.1 settles on). Returns ``None`` for anything unrecognised so the
    caller can fall back to inline synthesis.
    """
    if isinstance(result, AudioSegmentOut):
        return result
    if hasattr(result, "wav_bytes") and hasattr(result, "volumes"):
        return result  # type: ignore[return-value]
    seg = getattr(result, "segment", None)
    if isinstance(seg, AudioSegmentOut):
        return seg
    if seg is not None and hasattr(seg, "wav_bytes") and hasattr(seg, "volumes"):
        return seg
    return None
