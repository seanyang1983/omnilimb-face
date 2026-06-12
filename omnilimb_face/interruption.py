"""omnilimb_face.interruption — real-time barge-in interruption control.

This module implements the ``Interruption_Controller`` component from the
design (Requirement 5). It is split across two tasks:

* **Task 7.1 (this file's current scope)** implements the *pure decision*
  portion: the :class:`InterruptDecision` value object and
  :meth:`InterruptionController.on_vad_event`, a deterministic, side-effect
  free function that decides whether the agent's speech playback should be
  interrupted based on the incoming VAD event stream. This is the target of
  Property 7 (barge-in decision), whose Hypothesis test is added by Task 7.2.
* **Task 21.1 (this file's current scope)** wires the I/O behaviour of
  :meth:`arm` / :meth:`disarm` to the real collaborators (``VadSegmenter``/
  ``TTSPlayer``/``LLMBridge``/``VoiceCapture``). While *armed*, every VAD event
  delivered to :meth:`feed_vad_event` is run through the pure
  :meth:`on_vad_event`; on a confirmed barge-in it stops TTS playback
  immediately (``TTSPlayer.stop`` — within the 300 ms confirmation→stop budget,
  Requirement 5.2), aborts the host reply turn the ``LLMBridge`` is observing
  (Requirement 5.3), and resets the segmenter/accumulator so capture begins a
  fresh user segment right away (Requirement 5.4). When interruption is disabled
  in config, :meth:`feed_vad_event` never stops playback (Requirement 5.5). If
  the mic/VAD fails while armed (an event feed raises, or the capture loop calls
  :meth:`signal_detection_failure`), detection is torn down, the current
  playback is left **uninterrupted**, and a descriptive "barge-in unavailable"
  error is surfaced (Requirement 5.6).

The collaborator constructor arguments (``vad``/``tts``/``bridge``/``capture``)
remain **optional** (default ``None``) so the controller is unit-testable with
fakes and the pure :meth:`on_vad_event` decision (Property 7) is never touched —
it only reads the configured ``enabled`` flag and the barge-in speech threshold.

The type references to ``VadEvent`` (``omnilimb_face.voice.vad``) and
``InterruptionSettings`` / ``VADSettings`` (``omnilimb_face.config``) are
imported under ``TYPE_CHECKING`` so this module imports cleanly even before
those sibling modules land; ``on_vad_event`` only relies on the documented
attributes (``event.kind`` / ``event.ts_ms`` and ``settings.enabled``).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from omnilimb_face.config import InterruptionSettings, VADSettings
    from omnilimb_face.voice.vad import VadEvent

logger = logging.getLogger(__name__)

# Default barge-in threshold (Requirement 5.2): a continuous speech run must
# reach at least 200 ms before an interruption is requested. Mirrors
# ``VADSettings.barge_in_min_speech_ms`` so this module has a sane fallback
# when no settings object is supplied.
DEFAULT_BARGE_IN_MIN_SPEECH_MS = 200


@dataclass(frozen=True)
class InterruptDecision:
    """Outcome of a single :meth:`InterruptionController.on_vad_event` call.

    Attributes:
        should_interrupt: ``True`` iff interruption is enabled in config *and*
            the accumulated *continuous* speech duration has reached the
            barge-in threshold (Requirement 5.2). Always ``False`` when
            interruption is disabled (Requirement 5.5).
        accumulated_speech_ms: The current continuous speech duration (in
            milliseconds) tracked from the VAD event stream. Reset to ``0`` by
            any non-speech / silence event.
    """

    should_interrupt: bool
    accumulated_speech_ms: int


class InterruptionController:
    """Listens for user speech during playback and decides on barge-in.

    The constructor mirrors the design signature
    ``(cfg, vad, tts, bridge, capture)`` but keeps every collaborator optional
    so that:

    * Task 7.1 can construct the controller with only an
      :class:`~omnilimb_face.config.InterruptionSettings` (plus a VAD settings
      object or an explicit threshold) to exercise the pure decision, and
    * Task 21.1 can later supply the real ``vad``/``tts``/``bridge``/``capture``
      collaborators and flesh out :meth:`arm`/:meth:`disarm` without changing
      the pure :meth:`on_vad_event` contract.

    Args:
        cfg: Interruption settings; only ``cfg.enabled`` is consulted by the
            decision (Requirement 5.5).
        vad: VAD segmenter collaborator (wired in Task 21.1). Unused here.
        tts: TTS player collaborator (wired in Task 21.1). Unused here.
        bridge: LLM bridge collaborator (wired in Task 21.1). Unused here.
        capture: Voice capture collaborator (wired in Task 21.1). Unused here.
        vad_settings: Optional VAD settings; ``barge_in_min_speech_ms`` is read
            from it when ``barge_in_min_speech_ms`` is not given explicitly.
        barge_in_min_speech_ms: Optional explicit threshold override (ms). Takes
            precedence over ``vad_settings``; defaults to
            :data:`DEFAULT_BARGE_IN_MIN_SPEECH_MS` (200 ms).
    """

    # VAD event kinds that represent active user speech. Any other kind
    # (``"silence"``, ``"speech_end"``, ``"max_timeout"``, ...) breaks the
    # continuous run and resets the accumulator.
    _SPEECH_KINDS: frozenset[str] = frozenset({"speech_start", "speech"})

    # Bridge methods tried, in order, to abort the host reply turn currently
    # being observed (Requirement 5.3). The first callable one wins. The real
    # :class:`omnilimb_face.llm_bridge.LLMBridge` is a Plan A *observer* and
    # exposes no explicit cancel, so the documented best-effort fallback is
    # ``begin_turn`` (see :meth:`_abort_bridge_turn`).
    _BRIDGE_ABORT_METHODS: tuple[str, ...] = (
        "abort_turn",
        "cancel_turn",
        "cancel",
        "abort",
        "interrupt_turn",
        "interrupt",
        "begin_turn",
    )

    # Capture methods tried, in order, to (un)subscribe this controller to the
    # VAD event stream so the capture loop drives :meth:`feed_vad_event`. All
    # optional: when the capture exposes none, the runtime feeds events into
    # :meth:`feed_vad_event` directly instead.
    _CAPTURE_SUBSCRIBE_METHODS: tuple[str, ...] = (
        "subscribe_vad_events",
        "add_vad_listener",
        "on_vad_event",
    )
    _CAPTURE_UNSUBSCRIBE_METHODS: tuple[str, ...] = (
        "unsubscribe_vad_events",
        "remove_vad_listener",
    )

    # Capture methods tried, in order, to ask the capture pipeline to begin a
    # fresh user segment immediately after a barge-in (Requirement 5.4). All
    # optional; the segmenter/accumulator reset below is always performed.
    _CAPTURE_NEW_SEGMENT_METHODS: tuple[str, ...] = (
        "begin_new_segment",
        "restart_segment",
    )

    def __init__(
        self,
        cfg: "InterruptionSettings",
        vad: Optional[Any] = None,
        tts: Optional[Any] = None,
        bridge: Optional[Any] = None,
        capture: Optional[Any] = None,
        *,
        vad_settings: "Optional[VADSettings]" = None,
        barge_in_min_speech_ms: Optional[int] = None,
        on_interrupt: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._cfg = cfg
        # Collaborators wired by Task 21.1. The pure decision (Task 7.1) never
        # touches them; the arm/disarm I/O wiring below does.
        self._vad = vad
        self._tts = tts
        self._bridge = bridge
        self._capture = capture

        # Optional runtime hooks: ``on_interrupt`` fires once per confirmed
        # barge-in (after playback is stopped and the turn aborted) so the
        # runtime can react; ``on_error`` surfaces a barge-in-unavailable error
        # to the user (Requirement 5.6).
        self._on_interrupt = on_interrupt
        self._on_error = on_error

        self._threshold_ms = self._resolve_threshold(
            vad_settings, barge_in_min_speech_ms
        )

        # Continuous-speech accumulator state.
        # ``_speech_anchor_ms`` marks the timestamp of the first speech event in
        # the current continuous run; ``None`` means we are not currently in a
        # speech run. ``_accumulated_speech_ms`` is the elapsed continuous
        # speech duration derived from event timestamps.
        self._speech_anchor_ms: Optional[int] = None
        self._accumulated_speech_ms: int = 0

        # --- Task 21.1 I/O wiring state (guarded by ``_lock``) -------------
        # ``feed_vad_event`` runs on the capture/VAD thread while ``arm``/
        # ``disarm`` are typically called from the session thread, so the
        # listening flag and bookkeeping are lock-protected.
        self._lock = threading.RLock()
        self._armed: bool = False
        self._subscribed: bool = False
        self._barge_in_available: bool = True
        self._last_error: Optional[Exception] = None
        self._interruption_count: int = 0

    @staticmethod
    def _resolve_threshold(
        vad_settings: "Optional[VADSettings]",
        barge_in_min_speech_ms: Optional[int],
    ) -> int:
        """Resolve the barge-in threshold (ms) from the supplied inputs.

        Precedence: explicit ``barge_in_min_speech_ms`` > ``vad_settings`` >
        :data:`DEFAULT_BARGE_IN_MIN_SPEECH_MS`.
        """
        if barge_in_min_speech_ms is not None:
            return int(barge_in_min_speech_ms)
        if vad_settings is not None:
            return int(getattr(vad_settings, "barge_in_min_speech_ms",
                               DEFAULT_BARGE_IN_MIN_SPEECH_MS))
        return DEFAULT_BARGE_IN_MIN_SPEECH_MS

    @property
    def barge_in_min_speech_ms(self) -> int:
        """The resolved barge-in speech threshold in milliseconds."""
        return self._threshold_ms

    @property
    def enabled(self) -> bool:
        """Whether interruption (barge-in) is enabled in config (Req 5.5)."""
        return bool(getattr(self._cfg, "enabled", False))

    @property
    def accumulated_speech_ms(self) -> int:
        """Current continuous speech duration tracked from the event stream."""
        return self._accumulated_speech_ms

    @property
    def armed(self) -> bool:
        """Whether the controller is currently listening for barge-in.

        ``True`` between a successful :meth:`arm` and a :meth:`disarm` (or until
        a detection failure tears listening down, Requirement 5.6).
        """
        return self._armed

    @property
    def barge_in_available(self) -> bool:
        """Whether barge-in detection is currently believed to be available.

        Set to ``False`` when the mic/VAD fails while armed
        (:meth:`signal_detection_failure` or an event-feed exception), and reset
        to ``True`` on the next successful :meth:`arm` (Requirement 5.6).
        """
        return self._barge_in_available

    @property
    def last_error(self) -> Optional[Exception]:
        """The most recent detection failure recorded while armed, if any."""
        return self._last_error

    @property
    def interruption_count(self) -> int:
        """Number of confirmed barge-in interruptions performed so far."""
        return self._interruption_count

    def reset(self) -> None:
        """Clear the continuous-speech accumulator.

        Pure state reset (no I/O). Used when (re)starting a listening window so
        a fresh barge-in measurement begins from zero.
        """
        self._speech_anchor_ms = None
        self._accumulated_speech_ms = 0

    def on_vad_event(self, event: "VadEvent") -> InterruptDecision:
        """Decide whether to interrupt playback given one VAD event.

        Pure, deterministic decision (Property 7). The controller maintains the
        accumulated *continuous* speech duration across calls: a speech event
        extends the current run (duration measured from the run's first speech
        event timestamp), while any non-speech / silence event resets the
        accumulation to zero.

        The decision is::

            should_interrupt == enabled and accumulated_speech_ms >= threshold

        so an interruption is requested **iff** interruption is enabled in
        config (Requirement 5.5) *and* the accumulated continuous speech has
        reached ``barge_in_min_speech_ms`` (Requirement 5.2). When interruption
        is disabled, ``should_interrupt`` is ``False`` for any event stream.

        Args:
            event: A VAD event exposing ``kind`` (one of ``"speech_start"``,
                ``"speech"``, ``"silence"``, ``"speech_end"``,
                ``"max_timeout"``) and a millisecond timestamp ``ts_ms``.

        Returns:
            An :class:`InterruptDecision` carrying the boolean outcome and the
            current accumulated continuous speech duration.
        """
        if event.kind in self._SPEECH_KINDS:
            if self._speech_anchor_ms is None:
                # First speech frame of a new continuous run anchors the clock.
                self._speech_anchor_ms = event.ts_ms
            # Continuous speech duration = elapsed time since the run's anchor.
            # ``max(0, ...)`` guards against out-of-order / non-monotonic
            # timestamps so the accumulator never goes negative.
            self._accumulated_speech_ms = max(0, event.ts_ms - self._speech_anchor_ms)
        else:
            # Any non-speech / silence event breaks the continuous run.
            self._speech_anchor_ms = None
            self._accumulated_speech_ms = 0

        should_interrupt = (
            self.enabled and self._accumulated_speech_ms >= self._threshold_ms
        )
        return InterruptDecision(
            should_interrupt=should_interrupt,
            accumulated_speech_ms=self._accumulated_speech_ms,
        )

    def arm(self) -> None:
        """Begin listening for barge-in during playback (Requirements 5.1–5.4).

        Resets the continuous-speech accumulator so a fresh measurement starts
        for this playback, marks the controller *armed*, and — when the injected
        :class:`VoiceCapture` exposes a VAD-event subscription hook — registers
        :meth:`feed_vad_event` so the capture loop drives it. When the capture
        exposes no such hook (or is ``None``), the runtime feeds events into
        :meth:`feed_vad_event` directly; arming still gates that processing.

        Arming is safe to call repeatedly: a second ``arm`` while already armed
        re-resets the accumulator and re-clears any prior detection error.
        Interruption being disabled in config is honoured downstream by
        :meth:`on_vad_event` (it never returns ``should_interrupt``), so an armed
        controller with interruption disabled never stops playback
        (Requirement 5.5).
        """
        with self._lock:
            self.reset()
            self._armed = True
            self._barge_in_available = True
            self._last_error = None
            self._subscribe_locked()

    def disarm(self) -> None:
        """Stop listening for barge-in; idempotent (Requirement 5.1).

        Unsubscribes from the capture VAD-event stream (when a subscription was
        made), clears the *armed* flag and resets the accumulator. Calling
        :meth:`disarm` when already disarmed is a harmless no-op.
        """
        with self._lock:
            self._unsubscribe_locked()
            self._armed = False
            self.reset()

    # ------------------------------------------------------------------
    # I/O entry points (Task 21.1).
    # ------------------------------------------------------------------
    def feed_vad_event(self, event: "VadEvent") -> Optional[InterruptDecision]:
        """Process one VAD event from the capture loop while armed.

        This is the I/O counterpart to the pure :meth:`on_vad_event`: it runs
        the (unchanged) decision and, on a confirmed barge-in, performs the
        interruption side effects — stop TTS playback, abort the host reply
        turn, and ready capture for a new segment.

        Behaviour:

        * **Not armed** → returns ``None`` and does nothing (events outside a
          playback window are ignored).
        * **Armed, decision is "interrupt"** → stops the :class:`TTSPlayer`
          within the confirmation→stop budget (Requirement 5.2), aborts the
          :class:`LLMBridge` turn (Requirement 5.3), resets the segmenter /
          accumulator so the next user speech is captured immediately
          (Requirement 5.4), and fires the optional ``on_interrupt`` hook.
        * **Armed, decision is "no interrupt"** (including when interruption is
          disabled in config, Requirement 5.5) → returns the decision with no
          side effects.
        * **The decision raises** (a malformed event / VAD failure) → detection
          is torn down, the current playback is left **uninterrupted**, and a
          barge-in-unavailable error is surfaced (Requirement 5.6); returns
          ``None``.

        Returns:
            The :class:`InterruptDecision` computed for ``event`` while armed, or
            ``None`` when not armed or when detection failed on this event.
        """
        with self._lock:
            if not self._armed:
                return None
            try:
                decision = self.on_vad_event(event)
            except Exception as exc:  # VAD/detection failure during playback
                self._fail_detection_locked(exc)
                return None
            if decision.should_interrupt:
                self._interrupt_now_locked()
            return decision

    def signal_detection_failure(self, error: Optional[Exception] = None) -> None:
        """Signal that the mic/VAD failed during playback (Requirement 5.6).

        Called by the capture loop when it can no longer produce VAD events
        (device lost, VAD backend error). Tears down barge-in detection
        (unsubscribes + disarms), leaves the current :class:`TTSPlayer` playback
        **untouched**, records the error and surfaces it via the optional
        ``on_error`` hook so the user can be told barge-in is unavailable. Safe
        to call when not armed (it still records the unavailable state).
        """
        with self._lock:
            exc = error or RuntimeError(
                "Microphone/VAD failed during playback; barge-in is unavailable."
            )
            self._fail_detection_locked(exc)

    # ------------------------------------------------------------------
    # Internal I/O helpers (all called with ``self._lock`` held).
    # ------------------------------------------------------------------
    def _interrupt_now_locked(self) -> None:
        """Perform a confirmed barge-in: stop TTS, abort turn, ready capture."""
        # 1) Stop playback immediately (Requirement 5.2). ``TTSPlayer.stop`` is
        #    synchronous and idempotent, so this adds no detectable latency on
        #    top of the detection→confirmation that already happened.
        self._stop_playback()
        # 2) Interrupt the host reply the bridge is observing (Requirement 5.3).
        self._abort_bridge_turn()
        # 3) Ready capture to record the user's new utterance (Requirement 5.4).
        self._begin_new_segment()
        # Reset the accumulator so a second interruption is not re-triggered by
        # the same continuous speech run (playback is already stopped).
        self.reset()
        self._interruption_count += 1
        if self._on_interrupt is not None:
            try:
                self._on_interrupt()
            except Exception:  # pragma: no cover - hook must not break barge-in
                logger.warning(
                    "on_interrupt hook raised during barge-in; ignoring.",
                    exc_info=True,
                )

    def _stop_playback(self) -> None:
        """Stop the TTS player's playback (best-effort, never raises)."""
        tts = self._tts
        if tts is None:
            return
        stop = getattr(tts, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception:  # pragma: no cover - stop is best-effort for barge-in
            logger.warning(
                "TTSPlayer.stop() raised during barge-in; continuing.",
                exc_info=True,
            )

    def _abort_bridge_turn(self) -> None:
        """Interrupt the host reply turn the bridge is observing (Req 5.3).

        Tries the methods in :data:`_BRIDGE_ABORT_METHODS` in order and invokes
        the first callable one. The real :class:`LLMBridge` is a Plan A observer
        that cannot cancel the host's generation directly and exposes no explicit
        cancel, so the documented best-effort is its ``begin_turn`` reset — which
        flushes the :class:`SentenceChunker` residual and clears per-turn state,
        halting any further synthesis / avatar driving of the interrupted reply
        (the host turn is left to conclude on its own while the plugin stops
        driving voice/avatar output).
        """
        bridge = self._bridge
        if bridge is None:
            return
        for name in self._BRIDGE_ABORT_METHODS:
            method = getattr(bridge, name, None)
            if callable(method):
                try:
                    method()
                except Exception:  # pragma: no cover - abort is best-effort
                    logger.warning(
                        "LLMBridge.%s() raised during barge-in; continuing.",
                        name,
                        exc_info=True,
                    )
                return
        logger.debug(
            "No abort method found on the bridge; the interrupted reply will "
            "stop being driven once playback halts."
        )

    def _begin_new_segment(self) -> None:
        """Ready the capture pipeline to record a new user segment (Req 5.4).

        Always resets the injected :class:`VadSegmenter` (and this controller's
        own accumulator via the caller) so the user's ongoing speech is treated
        as a fresh segment immediately. When the capture exposes an explicit
        "begin new segment" hook it is invoked too (best-effort).
        """
        vad = self._vad
        if vad is not None:
            reset = getattr(vad, "reset", None)
            if callable(reset):
                try:
                    reset()
                except Exception:  # pragma: no cover - best-effort
                    logger.warning(
                        "VadSegmenter.reset() raised during barge-in; continuing.",
                        exc_info=True,
                    )
        capture = self._capture
        if capture is not None:
            for name in self._CAPTURE_NEW_SEGMENT_METHODS:
                method = getattr(capture, name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:  # pragma: no cover - best-effort
                        logger.warning(
                            "VoiceCapture.%s() raised during barge-in; continuing.",
                            name,
                            exc_info=True,
                        )
                    return

    def _fail_detection_locked(self, exc: Exception) -> None:
        """Tear down detection, keep playback, surface a barge-in error (Req 5.6)."""
        self._unsubscribe_locked()
        self._armed = False
        self._barge_in_available = False
        self._last_error = exc
        # NB: playback is intentionally left running — a detection failure must
        # not interrupt the agent's current speech (Requirement 5.6).
        logger.error(
            "Barge-in detection failed during playback; stopping detection and "
            "leaving playback uninterrupted. error=%s",
            exc,
            exc_info=True,
        )
        if self._on_error is not None:
            try:
                self._on_error(exc)
            except Exception:  # pragma: no cover - error hook must not cascade
                logger.warning(
                    "on_error hook raised while surfacing a barge-in failure; "
                    "ignoring.",
                    exc_info=True,
                )

    def _subscribe_locked(self) -> None:
        """Register :meth:`feed_vad_event` with the capture VAD stream, if able.

        Optional and best-effort: when the capture exposes no subscription hook
        the runtime drives :meth:`feed_vad_event` directly, so a missing hook is
        not an error. A subscription that *raises* is treated as a detection
        failure (Requirement 5.6).
        """
        capture = self._capture
        if capture is None or self._subscribed:
            return
        for name in self._CAPTURE_SUBSCRIBE_METHODS:
            subscribe = getattr(capture, name, None)
            if callable(subscribe):
                try:
                    subscribe(self.feed_vad_event)
                    self._subscribed = True
                except Exception as exc:
                    self._fail_detection_locked(exc)
                return

    def _unsubscribe_locked(self) -> None:
        """Unregister :meth:`feed_vad_event` from the capture VAD stream, if any."""
        capture = self._capture
        if capture is None or not self._subscribed:
            self._subscribed = False
            return
        for name in self._CAPTURE_UNSUBSCRIBE_METHODS:
            unsubscribe = getattr(capture, name, None)
            if callable(unsubscribe):
                try:
                    unsubscribe(self.feed_vad_event)
                except Exception:  # pragma: no cover - teardown is best-effort
                    logger.warning(
                        "VoiceCapture.%s() raised during disarm; continuing.",
                        name,
                        exc_info=True,
                    )
                break
        self._subscribed = False
