"""omnilimb_face.llm_bridge — Plan A LLM bridge (inject + hook interception).

This module implements the ``LLM_Bridge`` component from the design
(design.md -> "Components and Interfaces" -> "LLM_Bridge", Requirements 3 and
4.4). It realises **Plan A** ("give the agent a face"): the plugin never calls a
model directly. Instead it injects the user's (transcribed) utterance into the
active CLI conversation via ``ctx.inject_message`` to trigger the host agent's
*regular* session turn — carrying the host's own tools, memory and context — and
then **observes** the streamed reply text through the host's LLM-output hooks
(``transform_llm_output`` / ``post_llm_call``) to drive TTS and the Live2D
avatar. ``ctx.llm`` is deliberately kept off the primary reply path so the
plugin never bypasses the host's tools/memory nor causes a double-reply
conflict.

Why an *observer*, not a transformer
------------------------------------
``transform_llm_output`` is in the host's ``VALID_HOOKS`` with the contract
"return a string to replace the response text, or ``None``/empty to leave it
unchanged; first non-``None`` string wins." This bridge is purely an
**observer**: :meth:`LLMBridge.on_llm_output` captures the streamed fragment and
**always returns ``None``** so the host's output is never rewritten.

Observer safety
---------------
The two host-facing hook handlers (:meth:`on_llm_output`,
:meth:`on_post_llm_call`) **never raise into the host** — an exception escaping a
``transform_llm_output`` observer could corrupt or drop the user-visible reply.
They capture text, drive the downstream sinks best-effort, record any internal
error, and return (``None`` / nothing). The error *conditions* mandated by the
design are modelled as the :class:`NoActiveModelError` (Requirement 3.4) and
:class:`ReplyTimeoutError` (Requirement 3.5) types and are **raised** from the
dedicated, runtime-facing turn-conclusion path (:meth:`check_timeout`,
:meth:`conclude_turn`, :meth:`signal_no_active_model`) — the correct place to
surface a turn outcome without endangering the host's output stream.

Collaborators
-------------
Every downstream collaborator is **optional / injected** so this unit-tests
cleanly and Task 22.1 can wire the real ones:

* ``expression_mapper`` — an :class:`omnilimb_face.expression.ExpressionMapper`
  (pure); when present, each complete sentence is mapped to ``display_text`` +
  expression indices before being handed to the sink.
* ``sentence_sink`` — the decoupled per-sentence callback
  ``(chunk: ReplyChunk, expressions: list[int]) -> None`` that actually drives
  TTS/avatar output. This is the seam Task 22.1 uses to bridge sentences to the
  ``TTS_Player`` + ``Live2D_Director``.
* ``tts_player`` / ``live2d_director`` — stored for Task 22.1's wiring; their
  synthesis/playback methods are owned by later tasks, so this bridge drives
  them indirectly through ``sentence_sink`` rather than calling those
  not-yet-complete APIs here.
* ``on_playback_start`` — fired once, when the first text fragment of a turn
  arrives, so playback can begin at the earliest sentence boundary
  (Requirement 3.3).

Imports are kept safe outside a hermes checkout: this module never hard-imports
hermes core at top level. Only sibling plugin modules
(:mod:`omnilimb_face.chunker`, :mod:`omnilimb_face.expression`) are referenced,
and only for typing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, List, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from omnilimb_face.chunker import SentenceChunker
    from omnilimb_face.config import VTuberConfig
    from omnilimb_face.expression import ExpressionMapper

logger = logging.getLogger(__name__)

__all__ = [
    "ReplyChunk",
    "LLMBridge",
    "LLMBridgeError",
    "NoActiveModelError",
    "ReplyTimeoutError",
]

# Default turn-window timings, fixed by the requirements (not configurable):
#   * the first reply text must flow back within 5 s to begin playback (Req 3.3)
#   * a turn that produces no text within 30 s is timed out (Req 3.5)
DEFAULT_REPLY_TIMEOUT_S = 30.0
DEFAULT_FIRST_TEXT_DEADLINE_S = 5.0

# Keyword names a host might use to pass the streamed fragment / full reply when
# it does not supply it positionally. Searched in order; first present wins.
_OUTPUT_KEYS = ("output", "llm_output", "response", "text", "content", "message")
_REPLY_KEYS = ("reply_text", "response", "output", "text", "content", "message", "reply")


# ---------------------------------------------------------------------------
# Error types (Requirements 3.4 / 3.5). Defined locally to avoid cross-file
# conflicts during this implementation wave (a shared errors module can adopt
# them later without changing this contract).
# ---------------------------------------------------------------------------


class LLMBridgeError(RuntimeError):
    """Base class for errors raised while bridging a host reply turn."""


class NoActiveModelError(LLMBridgeError):
    """The host turn yielded no reply because no model/credentials are active.

    Raised (best-effort) when a triggered host turn produces no text at all
    (Requirement 3.4). The session context is left **unchanged**: the bridge
    only resets its own internal chunker/turn state and never fabricates a
    reply, so the host's conversation is untouched and the user can be shown the
    host's own "no active model" state.
    """

    def __init__(self, message: str = "", *, context_preserved: bool = True) -> None:
        super().__init__(
            message or "Host turn produced no reply (no active model or credentials)."
        )
        #: Always ``True`` — signals that the conversation context was not mutated.
        self.context_preserved = context_preserved


class ReplyTimeoutError(LLMBridgeError):
    """The host turn failed or produced no text within the timeout window.

    Raised when the host turn fails or 30 s elapse with no text flowing back
    through the output hooks (Requirement 3.5), terminating this turn's
    voice/avatar output. Carries the observed ``elapsed_s`` and the configured
    ``timeout_s`` for diagnostics.
    """

    def __init__(
        self,
        message: str = "",
        *,
        elapsed_s: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        super().__init__(
            message
            or "Host turn produced no text within the reply timeout window."
        )
        self.elapsed_s = elapsed_s
        self.timeout_s = timeout_s


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplyChunk:
    """A reply fragment intercepted from a host turn via the output hooks.

    Attributes:
        text: The displayable / synthesisable sentence with emotion tags already
            stripped (when an ``ExpressionMapper`` is wired); otherwise the raw
            sentence text.
        raw: The original sentence as emitted by the host, including any
            ``[emotion]`` markers.
        is_final: ``True`` for the final sentence of a turn (the residual flushed
            by :meth:`LLMBridge.on_post_llm_call`); ``False`` for sentences
            emitted mid-stream.
    """

    text: str
    raw: str
    is_final: bool


# ---------------------------------------------------------------------------
# LLM bridge
# ---------------------------------------------------------------------------


class LLMBridge:
    """Plan A bridge: inject the utterance, observe the host reply, drive output.

    The bridge does not own a model or credentials. It triggers a host turn with
    :meth:`inject_user_utterance` (``ctx.inject_message``) and observes the
    streamed reply through :meth:`on_llm_output` (``transform_llm_output``) and
    :meth:`on_post_llm_call` (``post_llm_call``), splitting the text into
    sentences with the injected :class:`SentenceChunker` and driving the
    downstream sinks per sentence (Requirement 3).

    Args:
        ctx: The host :class:`PluginContext`. Only ``ctx.inject_message`` is used
            on the primary path (``ctx.llm`` is deliberately avoided).
        cfg: The plugin configuration (stored for Task 22.1; the turn-window
            timings are fixed by the requirements, see below).
        chunker: The :class:`SentenceChunker` used to split streamed text into
            complete sentences at terminator boundaries.
        expression_mapper: Optional pure mapper applied per sentence to strip
            emotion tags and resolve expression indices.
        tts_player: Optional ``TTS_Player`` collaborator (wired by Task 22.1).
        live2d_director: Optional ``Live2D_Director`` collaborator (wired by
            Task 22.1).
        sentence_sink: Optional per-sentence callback
            ``(chunk: ReplyChunk, expressions: list[int]) -> None`` — the
            decoupled seam that drives TTS/avatar output.
        on_playback_start: Optional callback fired once when the first text
            fragment of a turn arrives (begin playback, Requirement 3.3).
        on_turn_error: Optional callback invoked with any error recorded by the
            observer hooks (which never raise into the host).
        clock: Optional monotonic clock ``() -> float`` (seconds); defaults to
            :func:`time.monotonic`. Injectable for deterministic tests.
        reply_timeout_s: No-text timeout window in seconds (default 30, Req 3.5).
        first_text_deadline_s: First-text playback deadline in seconds
            (default 5, Requirement 3.3).
    """

    def __init__(
        self,
        ctx: Any,
        cfg: "VTuberConfig",
        chunker: "SentenceChunker",
        *,
        expression_mapper: "Optional[ExpressionMapper]" = None,
        tts_player: Optional[Any] = None,
        live2d_director: Optional[Any] = None,
        sentence_sink: Optional[Callable[["ReplyChunk", List[int]], None]] = None,
        on_playback_start: Optional[Callable[[], None]] = None,
        on_turn_error: Optional[Callable[[Exception], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        reply_timeout_s: float = DEFAULT_REPLY_TIMEOUT_S,
        first_text_deadline_s: float = DEFAULT_FIRST_TEXT_DEADLINE_S,
    ) -> None:
        self._ctx = ctx
        self._cfg = cfg
        self._chunker = chunker

        # Optional / injected collaborators (Task 22.1 wires the real ones).
        self._expression_mapper = expression_mapper
        self._tts_player = tts_player
        self._live2d_director = live2d_director
        self._sentence_sink = sentence_sink
        self._on_playback_start = on_playback_start
        self._on_turn_error = on_turn_error

        if clock is None:
            import time

            clock = time.monotonic
        self._clock = clock
        self._reply_timeout_s = float(reply_timeout_s)
        self._first_text_deadline_s = float(first_text_deadline_s)

        # Turn state.
        self._turn_active: bool = False
        self._turn_complete: bool = False
        self._turn_started_at: Optional[float] = None
        self._any_text: bool = False
        self._first_text_at: Optional[float] = None
        self._playback_started: bool = False
        self._no_active_model: bool = False
        self._last_error: Optional[Exception] = None
        self._driven_chunks: List[ReplyChunk] = []

        # Best-effort host-turn availability flag: ``None`` until the first
        # inject attempt, then mirrors the last ``inject_message`` result.
        self._last_inject_result: Optional[bool] = None

    # ------------------------------------------------------------------
    # Public read-only state (handy for the runtime and for tests).
    # ------------------------------------------------------------------
    @property
    def turn_active(self) -> bool:
        """Whether a host-reply turn is currently being observed."""
        return self._turn_active

    @property
    def any_text_seen(self) -> bool:
        """Whether any reply text has flowed back during the current turn."""
        return self._any_text

    @property
    def playback_started(self) -> bool:
        """Whether playback was started (first text observed) this turn."""
        return self._playback_started

    @property
    def driven_chunks(self) -> List[ReplyChunk]:
        """The sentence chunks driven so far this turn (in order)."""
        return list(self._driven_chunks)

    @property
    def last_error(self) -> Optional[Exception]:
        """The most recent error recorded by the observer hooks, if any."""
        return self._last_error

    def first_text_within_deadline(self) -> bool:
        """Whether the first text arrived within ``first_text_deadline_s`` (Req 3.3).

        ``False`` when no text has arrived yet or when the turn was never
        started; otherwise compares the first-text timestamp against the turn
        start and the 5 s deadline.
        """
        if self._first_text_at is None or self._turn_started_at is None:
            return False
        return (self._first_text_at - self._turn_started_at) <= self._first_text_deadline_s

    # ------------------------------------------------------------------
    # Utterance injection (Requirement 4.4, 11.6 / 11.7).
    # ------------------------------------------------------------------
    def inject_user_utterance(self, text: str) -> bool:
        """Inject the (transcribed) utterance to trigger the host turn (Req 4.4).

        Calls ``ctx.inject_message(text, role="user")`` so the utterance appears
        in the standard session transcript/history and triggers the host agent's
        regular turn, whose reply this bridge then observes via the output hooks.

        Returns the host's boolean result. This only works in interactive CLI
        sessions; in gateway/messaging mode ``ctx.inject_message`` has no CLI
        reference and returns ``False`` (Requirements 11.6 / 11.7) — in that case
        no host turn is triggered and **no voice turn is driven**. The result is
        cached for the best-effort :meth:`host_turn_available`.
        """
        try:
            result = bool(self._ctx.inject_message(text, role="user"))
        except Exception as exc:  # defensive: a broken host must not crash us
            logger.warning("inject_message failed: %s", exc)
            self._last_inject_result = False
            return False
        self._last_inject_result = result
        if not result:
            logger.info(
                "inject_message returned False (gateway mode / no CLI session); "
                "no voice turn will be driven."
            )
        return result

    def host_turn_available(self) -> bool:
        """Best-effort: whether a host turn can currently be triggered (Req 3.4/11.6).

        A host turn is only triggerable in an interactive CLI session where
        ``ctx.inject_message`` actually queues input. The plugin cannot fully
        detect that without the host, so this returns a best-effort flag derived
        from the **last** ``inject_message`` result: ``True`` only after a
        successful inject (interactive CLI), ``False`` before any inject or after
        a gateway-mode ``False``.
        """
        return self._last_inject_result is True

    # ------------------------------------------------------------------
    # Turn lifecycle.
    # ------------------------------------------------------------------
    def begin_turn(self) -> None:
        """Mark the start of a new host-reply turn (Requirement 3.5).

        Resets the sentence chunker (discarding any stale residual) and starts
        the 30 s "no-text-arrived" timeout window so :meth:`check_timeout` can
        terminate a turn that produces nothing. Clears all per-turn state.
        """
        # Drain any leftover buffer so a previous partial sentence can't leak
        # into this turn. ``flush`` clears the chunker's internal buffer.
        self._chunker.flush()

        self._turn_active = True
        self._turn_complete = False
        self._turn_started_at = self._clock()
        self._any_text = False
        self._first_text_at = None
        self._playback_started = False
        self._no_active_model = False
        self._last_error = None
        self._driven_chunks = []

    def on_llm_output(self, text: Any = None, **kwargs: Any) -> Optional[str]:
        """``transform_llm_output`` observer: capture a streamed reply fragment.

        Pushes the fragment into the :class:`SentenceChunker` and drives the
        downstream sinks for each complete sentence (Requirement 3.3); the first
        text fragment begins playback. As an **observer** it must never rewrite
        the host output, so it **always returns ``None``** and never lets an
        internal error escape into the host (errors are recorded and forwarded to
        ``on_turn_error``).

        The turn-outcome error conditions are surfaced separately, off the host
        path: a 30 s no-text window is detected by :meth:`check_timeout`
        (raising :class:`ReplyTimeoutError`, Requirement 3.5) and a turn that
        yields nothing is detected by :meth:`conclude_turn` /
        :meth:`signal_no_active_model` (raising :class:`NoActiveModelError`,
        Requirement 3.4).
        """
        try:
            if not self._turn_active:
                # Defensive lazy begin: some hosts only invoke the hook without
                # the plugin having called begin_turn() first.
                self.begin_turn()
            value = _coerce_text(text, kwargs, _OUTPUT_KEYS)
            self._observe_text(value)
        except Exception as exc:  # never break/ rewrite host output
            self._record_error(exc)
        # ALWAYS None: observer, never replaces the host's reply text.
        return None

    def on_post_llm_call(self, reply_text: Any = None, **kwargs: Any) -> None:
        """``post_llm_call`` fallback observer: flush the chunker residual.

        Flushes the chunker's trailing residual as the **final** sentence so the
        last sentence of the reply is synthesised (Requirement 3.3). When no
        fragments arrived via :meth:`on_llm_output` (a host that only fires
        ``post_llm_call`` with the whole reply), the full ``reply_text`` is
        observed here first so it is still chunked and driven.

        Like :meth:`on_llm_output`, this is an observer: it never raises into the
        host and returns ``None``.
        """
        try:
            if not self._turn_active:
                self.begin_turn()
            # Fallback: if nothing streamed, treat the full reply as the stream.
            if not self._any_text:
                value = _coerce_text(reply_text, kwargs, _REPLY_KEYS)
                if value:
                    self._observe_text(value)
            # Flush the trailing partial sentence (at most one) as the final one.
            for sentence in self._chunker.flush():
                self._drive_sentence(sentence, is_final=True)
            if self._any_text:
                self._turn_complete = True
        except Exception as exc:
            self._record_error(exc)
        return None

    # ------------------------------------------------------------------
    # Runtime-facing turn conclusion / timeout (raise the error types).
    # ------------------------------------------------------------------
    def check_timeout(self) -> None:
        """Raise :class:`ReplyTimeoutError` if the no-text window elapsed (Req 3.5).

        Intended to be polled by the runtime's turn timer. Does nothing when no
        turn is active or when text has already flowed back; otherwise, once
        ``reply_timeout_s`` (30 s) has elapsed with no text, it terminates the
        turn and raises :class:`ReplyTimeoutError`.
        """
        if not self._turn_active or self._any_text:
            return
        elapsed = self._elapsed()
        if elapsed >= self._reply_timeout_s:
            self._turn_active = False
            raise ReplyTimeoutError(
                f"No reply text within {self._reply_timeout_s:.0f}s; "
                "terminating this turn's voice/avatar output.",
                elapsed_s=elapsed,
                timeout_s=self._reply_timeout_s,
            )

    def signal_no_active_model(self, detail: str = "") -> None:
        """Flag the current turn as having no active model and raise (Req 3.4).

        Called by the runtime when it determines (from the host's own state) that
        the triggered turn cannot produce a reply because no model/credentials
        are active. Marks the turn inactive and raises
        :class:`NoActiveModelError` without touching the session context (the
        bridge fabricates no reply and mutates only its own internal state).
        """
        self._no_active_model = True
        self._turn_active = False
        raise NoActiveModelError(
            detail or "Host turn has no active model or credentials."
        )

    def conclude_turn(self, reply_text: Any = None, **kwargs: Any) -> None:
        """Finish the turn: flush residual, then raise on an empty outcome.

        Convenience for the runtime. First flushes via :meth:`on_post_llm_call`
        so the final sentence is driven, then:

        * if any text was produced, marks the turn complete and returns;
        * if the turn yielded nothing and was explicitly flagged via
          :meth:`signal_no_active_model`, or no host turn was ever triggerable,
          raises :class:`NoActiveModelError` (Requirement 3.4, context unchanged);
        * if the no-text window elapsed, raises :class:`ReplyTimeoutError`
          (Requirement 3.5);
        * otherwise (host reachable but empty reply) raises
          :class:`NoActiveModelError` as the best-effort outcome.
        """
        self.on_post_llm_call(reply_text, **kwargs)
        if self._any_text:
            self._turn_active = False
            self._turn_complete = True
            return

        elapsed = self._elapsed()
        self._turn_active = False
        if self._no_active_model or not self.host_turn_available():
            raise NoActiveModelError(
                "Host turn produced no reply (no active model or credentials)."
            )
        if elapsed >= self._reply_timeout_s:
            raise ReplyTimeoutError(
                f"Host turn produced no text within {self._reply_timeout_s:.0f}s.",
                elapsed_s=elapsed,
                timeout_s=self._reply_timeout_s,
            )
        raise NoActiveModelError(
            "Host turn completed without producing any reply text."
        )

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------
    def _elapsed(self) -> float:
        """Seconds elapsed since the current turn started (0 if not started)."""
        if self._turn_started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._turn_started_at)

    def _observe_text(self, text: str) -> None:
        """Capture a text fragment: mark first-text, chunk it, drive sentences."""
        if not text:
            return
        if not self._any_text:
            # First text of the turn -> begin playback (Requirement 3.3).
            self._any_text = True
            self._first_text_at = self._clock()
            if not self._playback_started:
                self._playback_started = True
                if self._on_playback_start is not None:
                    try:
                        self._on_playback_start()
                    except Exception as exc:  # decoupled; never break observing
                        self._record_error(exc)
        for sentence in self._chunker.push(text):
            self._drive_sentence(sentence, is_final=False)

    def _drive_sentence(self, raw_sentence: str, is_final: bool) -> None:
        """Map (optional) and hand one complete sentence to the sink in order."""
        if not raw_sentence:
            return
        display_text = raw_sentence
        expressions: List[int] = []
        if self._expression_mapper is not None:
            try:
                mapped = self._expression_mapper.map_reply(raw_sentence)
                display_text = mapped.display_text
                expressions = list(mapped.expressions)
            except Exception as exc:  # mapper is pure; guard defensively anyway
                self._record_error(exc)
        chunk = ReplyChunk(text=display_text, raw=raw_sentence, is_final=is_final)
        self._driven_chunks.append(chunk)
        # Decoupled primary sink: the seam Task 22.1 uses to drive TTS + avatar.
        if self._sentence_sink is not None:
            self._sentence_sink(chunk, expressions)

    def _record_error(self, exc: Exception) -> None:
        """Record an internal error and forward it to ``on_turn_error``.

        Observer hooks call this instead of raising so the host output is never
        endangered; the runtime can react via the ``on_turn_error`` callback.
        """
        self._last_error = exc
        logger.warning("LLMBridge turn error (observed, not raised to host): %s", exc)
        if self._on_turn_error is not None:
            try:
                self._on_turn_error(exc)
            except Exception:  # pragma: no cover - callback must not cascade
                logger.exception("on_turn_error callback raised; ignoring")


def _coerce_text(positional: Any, kwargs: dict, keys: tuple) -> str:
    """Best-effort extraction of the reply text from hook arguments.

    Prefers the positional value; otherwise searches ``kwargs`` for the first of
    ``keys`` present. Non-string values are stringified; ``None`` becomes ``""``.
    """
    value = positional
    if value is None:
        for key in keys:
            if key in kwargs and kwargs[key] is not None:
                value = kwargs[key]
                break
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
