"""omnilimb_face.voice.wake_word — pure wake-word gating state machine.

This module implements the *pure-logic* core of the optional wake-word feature
(design.md → Components and Interfaces → ``Voice_Capture（含 VAD 与可选唤醒词）``,
requirement group 13). It is split across tasks:

* **Task 15.1 (this file's scope)** implements the deterministic, side-effect
  free :class:`WakeWord` gate: a state machine driven by two kinds of inputs
  over time — wake-word *detection confidence* and *silence duration* — that
  decides whether the hands-free pipeline's "gate" is open (segments are
  submitted for transcription / a transcript may be injected) or closed (the
  capture loop keeps listening for the wake word and injects nothing). This is
  the target of Property 14 (wake-word gating state machine), whose Hypothesis
  test is added by Task 15.2.
* **Task 14 / Task 15.3 (later)** wire the real ``openwakeword`` engine and the
  "engine unavailable / failed to initialize" error path (Requirement 13.5):
  stop capture, surface an error to the user, inject no transcript. This module
  exposes :class:`WakeWordUnavailableError` and an ``engine_available`` flag as
  a clean hook for that I/O wiring without performing any audio I/O itself.

Gate semantics (Requirements 13.1, 13.2, 13.3, 13.4, 13.6)
----------------------------------------------------------
* **Disabled in config** → the gate is **always open** (Requirement 13.6): when
  wake-word activation is off, every detected segment is submitted for
  transcription, exactly as plain hands-free mode would do.
* **Enabled** → the gate is open **iff** a detection whose confidence is at
  least ``confidence_threshold`` has occurred (Requirement 13.1) *and* the
  continuous silence accumulated since that detection has **not** yet reached
  ``listen_timeout_s`` (Requirement 13.4). While enabled and not yet triggered,
  the gate is **closed** and no transcript is injected (Requirements 13.2,
  13.3). Once the silence run reaches ``listen_timeout_s``, the machine returns
  to the wake-word listening state (the gate closes again, Requirement 13.4).

The ``WakeWordSettings`` type lives in :mod:`omnilimb_face.config`; it is
imported under ``TYPE_CHECKING`` only so this module stays import-safe and has
no hard dependency cycle. At runtime :class:`WakeWord` only relies on the
documented attributes (``enabled``, ``phrase``, ``confidence_threshold``,
``listen_timeout_s``), so any duck-typed settings object works.

This module performs **no audio I/O** and embeds **no wake-word engine**; it is
exhaustively replayable and deterministic, which is what makes it property
testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only import, avoids a runtime dep
    from omnilimb_face.config import WakeWordSettings

__all__ = ["WakeWord", "WakeWordUnavailableError"]


class WakeWordUnavailableError(RuntimeError):
    """The wake-word detection engine is unavailable or failed to initialize.

    Raised by the I/O wiring layer (Task 14 / Task 15.3) when the optional
    ``openwakeword`` engine cannot be loaded while wake-word activation is
    enabled. Per Requirement 13.5 the capture loop must then stop, surface a
    descriptive error to the user, and inject no transcript. The pure
    :class:`WakeWord` gate never raises this itself; it is defined here so the
    voice subpackage has a single, importable error type for that path and so
    :meth:`WakeWord.is_gate_open` can keep the gate closed while the engine is
    marked unavailable.
    """

    def __init__(self, message: str = "wake-word detection engine is unavailable") -> None:
        super().__init__(message)


class WakeWord:
    """Pure wake-word gate: a deterministic ``(confidence, silence)`` machine.

    The gate is driven by two kinds of observations fed over time:

    * :meth:`observe_detection` — a wake-word detection score. A score at or
      above ``confidence_threshold`` is a *qualifying* detection that opens the
      gate and resets the continuous-silence clock (a detection is itself voice
      activity; Requirement 13.1).
    * :meth:`observe_silence` — a continuous-silence duration since the last
      voice activity. Successive silence observations accumulate; once the
      accumulated continuous silence reaches ``listen_timeout_s`` the gate
      closes and the machine returns to listening for the wake word
      (Requirement 13.4).

    :meth:`observe_voice_activity` resets only the silence clock (without
    changing the trigger state), modelling non-wake-word speech that arrives
    after the gate has opened — the silence timeout is measured *since the last
    voice activity* (Requirement 13.4), not since the wake word alone.

    When wake-word activation is **disabled**, the gate is always open and the
    ``observe_*`` methods are no-ops that report an open gate (Requirement
    13.6). When enabled but the detection engine has been marked unavailable
    (see :meth:`set_engine_available`), the gate stays closed so no transcript
    is injected (Requirement 13.5 hook).

    The machine is pure logic — no audio I/O, no embedded engine — and fully
    replayable, which makes it the target of Property 14 (Task 15.2).
    """

    def __init__(
        self,
        cfg: "WakeWordSettings",
        *,
        engine_available: bool = True,
    ) -> None:
        """Build a gate from a wake-word settings object.

        Args:
            cfg: A :class:`omnilimb_face.config.WakeWordSettings` (or any object
                exposing ``enabled``, ``phrase``, ``confidence_threshold`` and
                ``listen_timeout_s``). ``confidence_threshold`` is clamped to the
                documented ``0.0``–``1.0`` range (design / Requirement 13.1).
            engine_available: Whether the underlying detection engine is usable.
                Defaults to ``True``. The I/O wiring (Task 14 / 15.3) sets this
                to ``False`` when ``openwakeword`` fails to load so the gate
                stays closed (Requirement 13.5).
        """
        self._cfg = cfg
        self._enabled: bool = bool(getattr(cfg, "enabled", False))
        self._phrase: str = str(getattr(cfg, "phrase", ""))
        # Clamp the threshold into the documented 0.0–1.0 range so an
        # out-of-range config value degrades predictably (Requirement 13.1).
        raw_threshold = float(getattr(cfg, "confidence_threshold", 0.7))
        self._confidence_threshold: float = min(1.0, max(0.0, raw_threshold))
        self._listen_timeout_s: float = float(getattr(cfg, "listen_timeout_s", 3.0))
        self._engine_available: bool = bool(engine_available)

        # Mutable run state (initialized by reset()).
        # ``_triggered`` is True once a qualifying detection has opened the gate
        # and before the silence timeout closes it again.
        self._triggered: bool = False
        # ``_silence_accum_s`` is the continuous silence accumulated since the
        # last qualifying detection / voice activity.
        self._silence_accum_s: float = 0.0
        self.reset()

    # ------------------------------------------------------------------ #
    # Read-only configuration / state accessors
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        """Whether wake-word activation is enabled in config (Requirement 13.6)."""
        return self._enabled

    @property
    def phrase(self) -> str:
        """The configured wake-word phrase (informational)."""
        return self._phrase

    @property
    def confidence_threshold(self) -> float:
        """Minimum detection confidence that opens the gate (Requirement 13.1)."""
        return self._confidence_threshold

    @property
    def listen_timeout_s(self) -> float:
        """Continuous silence (s) that returns the gate to listening (Req 13.4)."""
        return self._listen_timeout_s

    @property
    def engine_available(self) -> bool:
        """Whether the wake-word detection engine is currently usable (Req 13.5)."""
        return self._engine_available

    @property
    def triggered(self) -> bool:
        """Whether a qualifying detection has opened the gate and not timed out."""
        return self._triggered

    @property
    def silence_elapsed_s(self) -> float:
        """Continuous silence accumulated since the last detection / activity."""
        return self._silence_accum_s

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """Clear the runtime gate state, returning to wake-word listening.

        Pure state reset (no I/O). The configured ``enabled`` /
        ``confidence_threshold`` / ``listen_timeout_s`` and the
        ``engine_available`` flag are preserved.
        """
        self._triggered = False
        self._silence_accum_s = 0.0

    def set_engine_available(self, available: bool) -> None:
        """Mark the detection engine available or unavailable (Req 13.5 hook).

        When the engine becomes unavailable the gate is forced shut so no
        transcript is injected; the run state is reset so a later recovery
        starts cleanly from the listening state.
        """
        self._engine_available = bool(available)
        if not self._engine_available:
            self.reset()

    def is_gate_open(self) -> bool:
        """Return whether the gate is currently open.

        The gate decision:

        * disabled in config → always ``True`` (Requirement 13.6);
        * enabled but engine unavailable → ``False`` (Requirement 13.5 hook);
        * enabled and engine available → ``True`` iff a qualifying detection has
          occurred and the continuous silence since it has not yet reached
          ``listen_timeout_s`` (Requirements 13.1, 13.2, 13.3, 13.4).
        """
        if not self._enabled:
            return True
        if not self._engine_available:
            return False
        return self._triggered

    def observe_detection(self, confidence: float, ts: Optional[int] = None) -> bool:
        """Feed one wake-word detection score; return the resulting gate state.

        A detection whose ``confidence`` is at least ``confidence_threshold`` is
        a *qualifying* detection: it opens the gate and resets the
        continuous-silence clock, because a detection is itself voice activity
        (Requirement 13.1). A sub-threshold score does not open the gate and,
        while the gate is still closed, leaves the machine listening for the
        wake word (Requirements 13.2, 13.3).

        Args:
            confidence: The detection confidence in ``[0.0, 1.0]``.
            ts: Optional event timestamp (milliseconds). Accepted for interface
                symmetry with the rest of the voice pipeline and future I/O
                wiring; the pure gating logic is duration-driven via
                :meth:`observe_silence` and does not use it.

        Returns:
            ``True`` if the gate is open after this observation, else ``False``.
        """
        del ts  # informational only; see docstring.
        if not self._enabled:
            return True  # disabled → gate is always open (Requirement 13.6).
        if not self._engine_available:
            return False  # Requirement 13.5 hook: cannot detect → stay closed.

        if float(confidence) >= self._confidence_threshold:
            # Qualifying wake word: open the gate and restart the silence clock
            # (the detection is voice activity, Requirements 13.1 / 13.4).
            self._triggered = True
            self._silence_accum_s = 0.0
        # else: not a wake word; keep current state and keep listening.
        return self._triggered

    def observe_silence(self, silence_s: float) -> bool:
        """Accumulate continuous silence; return the resulting gate state.

        Successive calls add up the continuous silence since the last
        qualifying detection / voice activity. Once the accumulated silence
        reaches ``listen_timeout_s`` while the gate is open, the gate closes and
        the machine returns to wake-word listening (Requirement 13.4). Negative
        durations are ignored (treated as ``0``) so the accumulator is monotone.

        When wake-word activation is disabled the gate stays open regardless of
        silence (Requirement 13.6); when the engine is unavailable the gate
        stays closed (Requirement 13.5 hook).

        Args:
            silence_s: The duration of continuous silence observed in this step,
                in seconds.

        Returns:
            ``True`` if the gate is open after this observation, else ``False``.
        """
        if not self._enabled:
            return True  # disabled → gate is always open (Requirement 13.6).
        if not self._engine_available:
            return False  # Requirement 13.5 hook: stay closed.

        increment = max(0.0, float(silence_s))
        self._silence_accum_s += increment

        if self._triggered and self._silence_accum_s >= self._listen_timeout_s:
            # Silence has reached the listen timeout: return to listening for
            # the wake word; the gate closes again (Requirement 13.4).
            self._triggered = False
        return self._triggered

    def observe_voice_activity(self) -> bool:
        """Reset the continuous-silence clock on observed voice activity.

        Models non-wake-word speech arriving after the gate has opened: the
        silence timeout is measured *since the last voice activity*
        (Requirement 13.4), so any voice activity restarts the countdown
        without changing whether the gate is currently open. A no-op while
        disabled (gate always open) or while the engine is unavailable.

        Returns:
            ``True`` if the gate is open after this observation, else ``False``.
        """
        if not self._enabled:
            return True
        if not self._engine_available:
            return False
        self._silence_accum_s = 0.0
        return self._triggered
