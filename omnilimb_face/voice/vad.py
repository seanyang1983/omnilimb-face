"""omnilimb_face.voice.vad — pure VAD segmentation logic and data models.

This module implements the *pure-logic* core of the hands-free voice pipeline
(design.md → Components and Interfaces → ``Voice_Capture（含 VAD 与可选唤醒词）``):

* :class:`AudioFrame` — a single int16 mono PCM frame tagged with a timestamp.
* :class:`VadEvent` — a voice-activity-detection event emitted by an upstream
  VAD implementation (``webrtcvad`` or an RMS energy gate; see Task 14).
* :class:`VoiceSegment` — the boundaries (and end reason) of one captured
  utterance.
* :class:`VadSegmenter` — a deterministic, replayable state machine that
  consumes a :class:`VadEvent` stream and produces :class:`VoiceSegment`
  boundaries. It performs **no I/O** and holds no audio buffers, which makes it
  exhaustively testable by the Property 5 property test (Task 5.2).

State-machine semantics (Requirements 4.2, 4.3, 4.8)
----------------------------------------------------
A segment opens on a ``speech_start`` event. Once open, the segmenter ends the
segment on whichever of these two conditions is reached **first**, using the
event timestamps (``ts_ms``) to measure durations:

* **silence** — a *continuous* run of silence reaches ``silence_threshold_s``.
  The silence counter starts at the first ``silence``/``speech_end`` event of a
  run and is **reset** whenever a ``speech`` event resumes voice activity before
  the threshold is reached.
* **max_timeout** — the recording duration since ``speech_start`` reaches
  ``max_record_s``.

The produced :class:`VoiceSegment` carries ``end_reason`` matching the
triggering condition (``"silence"`` vs ``"max_timeout"``). When both thresholds
are crossed at the same event, the one whose threshold timestamp is *earlier*
wins; an exact tie resolves to ``"silence"`` (the natural end), consistent with
Requirement 4.8's "max record reached *while still no silence detected*".

The PCM payload of the returned segment is intentionally empty (``b""``): the
pure segmenter only computes boundaries. The actual audio bytes are assembled
by :class:`omnilimb_face.voice.capture.VoiceCapture` (Task 14), which owns the
:class:`AudioFrame` stream and is out of scope for this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only import, avoids a runtime dep
    # ``VADSettings`` is defined in ``omnilimb_face.config`` (Task 3.1). It is
    # imported here for type checking only so this module stays import-safe even
    # before ``config.py`` lands. At runtime :class:`VadSegmenter` only requires
    # an object exposing the ``silence_threshold_s`` and ``max_record_s``
    # attributes (duck typing), which :class:`VADSettings` provides.
    from omnilimb_face.config import VADSettings

__all__ = [
    "AudioFrame",
    "VadEvent",
    "VoiceSegment",
    "VAD_EVENT_KINDS",
    "SEGMENT_END_REASONS",
    "VadSegmenter",
]

# Allowed discriminant values, mirrored from design.md. Kept as module-level
# constants so tests (and upstream VAD adapters) can validate against them.
VAD_EVENT_KINDS = frozenset(
    {"speech_start", "speech", "silence", "speech_end", "max_timeout"}
)
SEGMENT_END_REASONS = frozenset({"silence", "max_timeout"})


@dataclass(frozen=True)
class AudioFrame:
    """A single mono int16 PCM frame tagged with its capture timestamp.

    Attributes:
        pcm: Raw little-endian int16 mono PCM bytes for this frame.
        ts_ms: Frame timestamp in milliseconds, monotonic within a capture run.
    """

    pcm: bytes
    ts_ms: int


@dataclass(frozen=True)
class VadEvent:
    """A voice-activity-detection event from an upstream VAD implementation.

    Attributes:
        kind: One of :data:`VAD_EVENT_KINDS` — ``"speech_start"``, ``"speech"``,
            ``"silence"``, ``"speech_end"`` or ``"max_timeout"``.
        ts_ms: Event timestamp in milliseconds, used to measure durations.
        rms: Normalized root-mean-square energy of the frame the event was
            derived from (informational; not used by the segmentation logic).
    """

    kind: str
    ts_ms: int
    rms: float


@dataclass(frozen=True)
class VoiceSegment:
    """The boundaries and end reason of one captured utterance.

    Attributes:
        pcm: Captured audio bytes. Empty (``b""``) when produced by the pure
            :class:`VadSegmenter`; populated by ``VoiceCapture`` (Task 14).
        start_ms: Timestamp of the opening ``speech_start`` event.
        end_ms: Timestamp of the event that triggered the segment's end.
        end_reason: One of :data:`SEGMENT_END_REASONS` — ``"silence"`` when the
            continuous-silence threshold was reached first, ``"max_timeout"``
            when the maximum recording duration was reached first.
    """

    pcm: bytes
    start_ms: int
    end_ms: int
    end_reason: str


class VadSegmenter:
    """Deterministic segmentation state machine over a :class:`VadEvent` stream.

    The segmenter is pure logic: :meth:`feed` is a total function of the current
    state and the incoming event, performs no I/O, and is fully replayable. This
    is the target of the Property 5 property test (Task 5.2), which asserts that
    a segment ends *if and only if* the earliest of the silence / max-record
    conditions has been met, with a matching ``end_reason``.
    """

    def __init__(self, cfg: "VADSettings") -> None:
        """Build a segmenter from a VAD settings object.

        Args:
            cfg: A :class:`omnilimb_face.config.VADSettings` (or any object
                exposing ``silence_threshold_s`` and ``max_record_s``). Both are
                interpreted in seconds and converted to milliseconds internally
                to match :class:`VadEvent` timestamps.
        """
        self._cfg = cfg
        # Convert the second-valued thresholds to milliseconds once, up front.
        self._silence_threshold_ms: float = float(cfg.silence_threshold_s) * 1000.0
        self._max_record_ms: float = float(cfg.max_record_s) * 1000.0

        # Mutable run state (initialized by reset()).
        self._active: bool = False
        self._start_ms: int = 0
        # Start timestamp of the current *continuous* silence run, or None while
        # voice activity is present.
        self._silence_start_ms: Optional[int] = None
        self._last_ts_ms: int = 0
        self.reset()

    def reset(self) -> None:
        """Clear all run state, abandoning any in-progress segment."""
        self._active = False
        self._start_ms = 0
        self._silence_start_ms = None
        self._last_ts_ms = 0

    def feed(self, event: "VadEvent") -> Optional["VoiceSegment"]:
        """Consume one VAD event, returning a :class:`VoiceSegment` if it ends.

        Returns ``None`` while the current segment is still open (or when no
        segment is open). Returns a completed :class:`VoiceSegment` on the event
        that satisfies the earliest of the silence / max-record conditions; the
        segmenter then resets, ready for the next ``speech_start``.
        """
        kind = event.kind
        ts = event.ts_ms

        if kind == "speech_start":
            # Open (or restart) a segment. We begin in speech, so there is no
            # active silence run yet.
            self._active = True
            self._start_ms = ts
            self._silence_start_ms = None
            self._last_ts_ms = ts
            return self._evaluate(ts, force_max=False)

        if not self._active:
            # No open segment: non-start events carry no segment to close.
            return None

        # An event arrived while a segment is open: advance the clock.
        self._last_ts_ms = ts

        if kind == "speech":
            # Voice resumed before the silence threshold was reached: reset the
            # continuous-silence counter (Requirement 4.3 "reset on resume").
            self._silence_start_ms = None
            return self._evaluate(ts, force_max=False)

        if kind in ("silence", "speech_end"):
            # Voice activity stopped (or remains stopped). ``speech_end`` marks
            # the transition point; both begin/continue a continuous silence run
            # measured from the first such event.
            if self._silence_start_ms is None:
                self._silence_start_ms = ts
            return self._evaluate(ts, force_max=False)

        if kind == "max_timeout":
            # Explicit max-record signal from the upstream source/timer.
            return self._evaluate(ts, force_max=True)

        # Any other (unknown) kind only advances the clock and re-checks the
        # computed thresholds; it never changes the silence/speech state.
        return self._evaluate(ts, force_max=False)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _evaluate(self, ts: int, force_max: bool) -> Optional["VoiceSegment"]:
        """Decide whether the open segment ends at timestamp ``ts``.

        ``force_max`` is set when an explicit ``max_timeout`` event arrives,
        forcing the max-record condition regardless of the computed threshold.
        """
        if not self._active:
            return None

        # Max-record condition: recording duration since speech start.
        max_reached_ts = self._start_ms + self._max_record_ms
        max_met = force_max or (ts >= max_reached_ts)

        # Silence condition: continuous-silence duration, only while in a run.
        if self._silence_start_ms is None:
            silence_reached_ts: Optional[float] = None
            silence_met = False
        else:
            silence_reached_ts = self._silence_start_ms + self._silence_threshold_ms
            silence_met = ts >= silence_reached_ts

        if not max_met and not silence_met:
            return None

        end_reason = self._classify_end(
            silence_met=silence_met,
            max_met=max_met,
            silence_reached_ts=silence_reached_ts,
            max_reached_ts=max_reached_ts,
        )

        segment = VoiceSegment(
            pcm=b"",
            start_ms=self._start_ms,
            end_ms=ts,
            end_reason=end_reason,
        )
        self.reset()
        return segment

    @staticmethod
    def _classify_end(
        *,
        silence_met: bool,
        max_met: bool,
        silence_reached_ts: Optional[float],
        max_reached_ts: float,
    ) -> str:
        """Pick the ``end_reason`` for the triggering condition(s).

        When both conditions are met at the same event, the one whose threshold
        timestamp is earlier wins; an exact tie resolves to ``"silence"`` (the
        natural end), matching Requirement 4.8's wording that ``max_timeout`` is
        the forced end reached *while still no silence detected*.
        """
        if silence_met and max_met:
            if silence_reached_ts is not None and silence_reached_ts <= max_reached_ts:
                return "silence"
            return "max_timeout"
        if silence_met:
            return "silence"
        return "max_timeout"

    @property
    def is_active(self) -> bool:
        """Whether a segment is currently open (no segment emitted yet)."""
        return self._active
