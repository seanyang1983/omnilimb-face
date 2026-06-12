"""omnilimb_face.voice.capture — microphone audio source and device enumeration.

This module implements the I/O side of the ``Voice_Capture`` component
(design.md → Components and Interfaces → ``Voice_Capture（含 VAD 与可选唤醒词）``
and "跨平台音频策略"). It is split across two tasks that edit this same file:

* **Task 14.1 (this file's current scope)** — the concrete, cross-platform
  audio *source*: the :class:`AudioSource` structural Protocol and its
  ``sounddevice``/PortAudio implementation :class:`SoundDeviceAudioSource`
  (``start`` / ``stop`` / ``frames`` and the static
  :meth:`SoundDeviceAudioSource.list_input_devices` enumerator), plus the
  availability probe (:data:`SOUNDDEVICE_AVAILABLE` /
  :meth:`SoundDeviceAudioSource.is_available`) and the catchable
  :class:`MicrophoneUnavailableError` (需求 4.1, 11.4; supports 4.9 / 11.5 / 12).
* **Task 14.2 (later, edits this same file)** — the ``VoiceCapture``
  orchestrator that gates hands-free mode on microphone availability, runs the
  VAD over the :class:`AudioFrame` stream produced here, and assembles
  :class:`~omnilimb_face.voice.vad.VoiceSegment` payloads. It is intentionally
  **not** defined in this task to avoid overwriting; see the marked placeholder
  at the bottom of this file.

Optional-dependency guarding (需求 12, 12.1)
-------------------------------------------
``sounddevice`` and ``numpy`` live in the optional ``[voice]`` extra and are
**not** part of the core install. The import below is therefore guarded so this
module *always imports cleanly*, even on a core-only install (or a host where
the PortAudio shared library is missing, which makes ``import sounddevice``
raise ``OSError`` rather than ``ImportError``). When the backend is absent:

* :meth:`SoundDeviceAudioSource.list_input_devices` returns ``[]`` (no devices /
  unavailable) instead of raising, so callers can degrade gracefully (需求
  4.9 / 11.5 / 12.4); and
* constructing **or** starting a :class:`SoundDeviceAudioSource` raises the
  clear, catchable :class:`MicrophoneUnavailableError` (rather than failing at
  import), naming the missing dependency.

Threading / callback model
---------------------------
``sounddevice`` drives capture from a **PortAudio callback thread**: PortAudio
invokes :meth:`SoundDeviceAudioSource._callback` on its own thread once per
audio block. The callback copies the block into a thread-safe
:class:`queue.Queue` as ``(pcm_bytes, ts_ms)`` and returns promptly (it never
blocks on the consumer). :meth:`SoundDeviceAudioSource.frames` runs on the
*consumer* thread (the VAD / capture loop), draining that queue and yielding
:class:`AudioFrame` values. This decouples the realtime audio thread from
downstream processing; back-pressure is bounded by the queue's ``maxsize`` (a
full queue drops the newest frame and counts the drop rather than blocking the
audio thread). Timestamps are derived from a running sample counter
(``ts_ms = captured_samples * 1000 / sample_rate``), so they are monotonic and
independent of wall-clock jitter.
"""

from __future__ import annotations

import array
import logging
import math
import queue
import sys
import threading
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Callable,
    Iterator,
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
    runtime_checkable,
)

from omnilimb_face.voice.vad import AudioFrame, VadEvent, VadSegmenter, VoiceSegment

if TYPE_CHECKING:  # pragma: no cover - typing-only import, avoids a runtime dep
    from omnilimb_face.config import VADSettings, VTuberConfig
    from omnilimb_face.voice.wake_word import WakeWord

logger = logging.getLogger(__name__)

__all__ = [
    "SOUNDDEVICE_AVAILABLE",
    "MicrophoneUnavailableError",
    "AudioSource",
    "SoundDeviceAudioSource",
    "StartResult",
    "VoiceCapture",
]


# ---------------------------------------------------------------------------
# Optional-dependency guard (需求 12.1): the module MUST import cleanly without
# the ``[voice]`` extra. ``import sounddevice`` can raise ``ImportError`` (not
# installed) or ``OSError`` (PortAudio shared library missing), so both are
# tolerated and collapse to the "unavailable" state.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised by environment, not unit tests
    import numpy as _np  # noqa: F401  (used inside the PortAudio callback)
    import sounddevice as _sd

    SOUNDDEVICE_AVAILABLE = True
except (ImportError, OSError):  # pragma: no cover - depends on the environment
    _np = None  # type: ignore[assignment]
    _sd = None  # type: ignore[assignment]
    SOUNDDEVICE_AVAILABLE = False


class MicrophoneUnavailableError(RuntimeError):
    """The microphone audio source cannot be created or started.

    Defined **locally** in this module (rather than imported across files) so
    Task 14.1 and Task 14.2 do not contend over a shared symbol. It is the
    single, catchable error the capture backend raises for either cause:

    * the optional ``[voice]`` dependency (``sounddevice``/``numpy``) is absent
      — the message names the missing dependency so the degraded-tool path can
      surface it (需求 12.1 / 12.2); or
    * the OS cannot open an input device (device absent, in use, or permission
      denied) — the hands-free gate must then refuse to activate (需求 4.9 /
      11.5).

    Being a :class:`RuntimeError` subclass keeps it easy to catch without
    importing this module's symbol everywhere (``except RuntimeError``), while
    callers that *do* import it get a precise type to match on.
    """


@runtime_checkable
class AudioSource(Protocol):
    """Structural contract for a microphone audio source (design "Voice_Capture").

    Mirrors the design's ``AudioSource`` Protocol shape. ``VoiceCapture``
    (Task 14.2) depends only on this structural interface, so tests can supply a
    lightweight fake source (e.g. the Property 13 microphone-gating test) and
    production wires the concrete :class:`SoundDeviceAudioSource`.
    """

    def start(self) -> None:
        """Begin capturing audio (acquire and start the input device)."""
        ...

    def stop(self) -> None:
        """Stop capturing and release the input device."""
        ...

    def frames(self) -> Iterator[AudioFrame]:
        """Yield captured :class:`AudioFrame` values until stopped."""
        ...

    @staticmethod
    def list_input_devices() -> List[str]:
        """Enumerate input-capable device names (empty when unavailable)."""
        ...


class SoundDeviceAudioSource:
    """Cross-platform microphone source backed by ``sounddevice``/PortAudio.

    Captures **mono int16 little-endian PCM** at the configured sample rate,
    splitting the stream into fixed ``frame_ms`` frames and yielding them as
    :class:`AudioFrame` values with monotonically increasing timestamps. The
    same PortAudio backend enumerates input devices on Windows, macOS and Linux
    (需求 4.1, 11.4), giving the hands-free gate a single cross-platform way to
    decide whether a microphone is present (需求 4.9 / 11.5).

    Lifecycle / threading (see the module docstring): :meth:`start` opens a
    PortAudio :class:`sounddevice.InputStream` whose callback runs on PortAudio's
    own thread and enqueues frames; :meth:`frames` drains that queue on the
    consumer thread; :meth:`stop` halts and closes the stream and unblocks any
    waiting consumer. Instances are single-shot-friendly but reusable: a stopped
    source can be :meth:`start`-ed again.

    Degradation: if the optional ``[voice]`` dependency is missing, construction
    raises :class:`MicrophoneUnavailableError` immediately (you cannot build a
    ``sounddevice``-backed source without ``sounddevice``); the static
    :meth:`list_input_devices` and class :meth:`is_available` remain callable and
    report the unavailable state without raising (需求 12.1).
    """

    #: Sentinel pushed onto the frame queue by :meth:`stop` to promptly wake a
    #: consumer blocked in :meth:`frames`.
    _SENTINEL = object()

    def __init__(
        self,
        cfg: "VADSettings",
        *,
        device: Optional[Union[int, str]] = None,
        max_queued_frames: Optional[int] = None,
    ) -> None:
        """Build a microphone source from VAD settings.

        Args:
            cfg: A :class:`omnilimb_face.config.VADSettings` (or any object
                exposing ``sample_rate`` and ``frame_ms``). ``sample_rate`` is
                the capture rate in Hz and ``frame_ms`` the duration of each
                emitted :class:`AudioFrame`; together they fix the PortAudio
                block size (``samples_per_frame = sample_rate * frame_ms /
                1000``).
            device: Optional PortAudio device selector (index or name). ``None``
                uses the system default input device.
            max_queued_frames: Optional bound on the internal frame queue. When
                ``None`` it defaults to roughly 30 seconds of frames, sized from
                ``frame_ms``. A full queue drops the newest frame (and counts the
                drop) rather than blocking the realtime audio thread.

        Raises:
            MicrophoneUnavailableError: If the optional ``[voice]`` dependency
                (``sounddevice``/``numpy``) is not installed.
        """
        if not SOUNDDEVICE_AVAILABLE:
            raise MicrophoneUnavailableError(
                "Microphone capture requires the optional 'sounddevice' "
                "dependency (install the omnilimb-face [voice] extra); it is "
                "not available, so a SoundDeviceAudioSource cannot be created."
            )

        self._sample_rate: int = int(getattr(cfg, "sample_rate", 16000))
        self._frame_ms: int = int(getattr(cfg, "frame_ms", 20))
        if self._sample_rate < 1:
            self._sample_rate = 16000
        if self._frame_ms < 1:
            self._frame_ms = 20

        # Samples per emitted frame (PortAudio block size). Clamp to >= 1 so a
        # degenerate sample_rate/frame_ms can never request a zero-size block.
        self._samples_per_frame: int = max(
            1, int(self._sample_rate * self._frame_ms / 1000)
        )
        self._device = device

        if max_queued_frames is not None and max_queued_frames > 0:
            maxsize = int(max_queued_frames)
        else:
            # ~30s of frames at the configured cadence, with a sane floor.
            maxsize = max(100, int(30_000 / self._frame_ms))
        self._queue: "queue.Queue[Union[Tuple[bytes, int], object]]" = queue.Queue(
            maxsize=maxsize
        )

        self._stream = None  # sounddevice.InputStream once started
        self._stop_event = threading.Event()
        self._samples_captured: int = 0
        self._dropped_frames: int = 0

    # ------------------------------------------------------------------ #
    # Availability / enumeration (callable without an open device)
    # ------------------------------------------------------------------ #
    @classmethod
    def is_available(cls) -> bool:
        """Whether the ``sounddevice``/PortAudio backend can be used (需求 12.1)."""
        return SOUNDDEVICE_AVAILABLE

    @staticmethod
    def list_input_devices() -> List[str]:
        """Enumerate input-capable device names across the current platform.

        Uses PortAudio's device query (so the same call works on Windows, macOS
        and Linux, 需求 11.4) and returns the names of every device exposing at
        least one input channel — the set used to decide whether a microphone is
        present for hands-free mode (需求 4.9 / 11.5).

        Returns:
            The list of input-capable device names, in PortAudio enumeration
            order. Returns ``[]`` when the optional ``[voice]`` dependency is
            absent (需求 12.1) or the underlying query fails, so callers can
            treat "no devices" and "backend unavailable" uniformly without
            handling an exception.
        """
        if not SOUNDDEVICE_AVAILABLE:
            return []
        try:
            devices = _sd.query_devices()
        except Exception:  # pragma: no cover - depends on host audio stack
            logger.warning(
                "Failed to enumerate audio input devices via sounddevice; "
                "treating as no microphone available.",
                exc_info=True,
            )
            return []

        names: List[str] = []
        for device in devices:
            try:
                if int(device.get("max_input_channels", 0)) > 0:
                    names.append(str(device.get("name", "")))
            except Exception:  # pragma: no cover - defensive per-entry guard
                continue
        return names

    # ------------------------------------------------------------------ #
    # AudioSource Protocol: start / stop / frames
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Open and start the PortAudio input stream.

        Idempotent: a second call while already running is a no-op. Resets the
        timestamp/frame-drop counters and clears the stop signal so a previously
        stopped source can be restarted.

        Raises:
            MicrophoneUnavailableError: If the input device cannot be opened
                (device absent, already in use, or permission denied), wrapping
                the underlying PortAudio error (需求 4.9 / 11.5). Also raised if
                the backend dependency is unavailable (defensive; construction
                already guards this).
        """
        if not SOUNDDEVICE_AVAILABLE:  # pragma: no cover - guarded at construction
            raise MicrophoneUnavailableError(
                "Microphone capture requires the optional 'sounddevice' "
                "dependency; it is not available."
            )
        if self._stream is not None:
            return  # already started

        self._stop_event.clear()
        self._samples_captured = 0
        self._dropped_frames = 0
        # Drain any stale items from a previous run.
        self._drain_queue()

        try:
            stream = _sd.InputStream(
                samplerate=self._sample_rate,
                blocksize=self._samples_per_frame,
                channels=1,
                dtype="int16",
                callback=self._callback,
                device=self._device,
            )
            stream.start()
        except Exception as exc:  # PortAudioError, OSError, ValueError, ...
            self._stream = None
            raise MicrophoneUnavailableError(
                f"Could not open the microphone input device "
                f"(device={self._device!r}): {exc}"
            ) from exc
        self._stream = stream

    def stop(self) -> None:
        """Stop capture and release the input device.

        Safe to call when not started and safe to call more than once. Stops and
        closes the PortAudio stream and pushes a sentinel so a consumer blocked
        in :meth:`frames` returns promptly. Stream-close errors are logged and
        swallowed so cleanup never raises (resource release is best-effort).
        """
        self._stop_event.set()
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # pragma: no cover - best-effort device release
                logger.warning(
                    "Error while stopping/closing the audio input stream.",
                    exc_info=True,
                )
        # Wake any consumer parked in frames(); ignore a full queue (the
        # timeout-based loop in frames() will still observe the stop event).
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:  # pragma: no cover - rare race with a full queue
            pass

    def frames(self) -> Iterator[AudioFrame]:
        """Yield captured :class:`AudioFrame` values until the source is stopped.

        Runs on the consumer thread, draining the queue the PortAudio callback
        fills. Each yielded frame carries ``frame_ms`` worth of mono int16 PCM
        and a monotonically increasing ``ts_ms`` derived from the running sample
        counter. The generator returns once the source is stopped and the queue
        is drained (or when it observes the stop sentinel), so a ``for`` loop
        over it terminates cleanly on :meth:`stop`.
        """
        while True:
            if self._stop_event.is_set() and self._queue.empty():
                return
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is self._SENTINEL:
                return
            pcm, ts_ms = item  # type: ignore[misc]
            yield AudioFrame(pcm=pcm, ts_ms=ts_ms)

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #
    @property
    def dropped_frames(self) -> int:
        """Number of frames dropped because the consumer fell behind."""
        return self._dropped_frames

    @property
    def is_running(self) -> bool:
        """Whether the input stream is currently open and capturing."""
        return self._stream is not None

    # ------------------------------------------------------------------ #
    # Context-manager sugar (start on enter, guaranteed stop on exit)
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "SoundDeviceAudioSource":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # Internal: PortAudio callback (runs on PortAudio's own thread)
    # ------------------------------------------------------------------ #
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        """PortAudio input callback: copy one block onto the frame queue.

        Invoked on PortAudio's realtime thread once per ``blocksize`` block.
        Must not block: it copies ``indata`` (a numpy int16 array of shape
        ``(samples, 1)``) to little-endian PCM bytes, tags it with a
        sample-counter-derived ``ts_ms``, and enqueues it without waiting. A
        full queue means the consumer fell behind, so the newest frame is
        dropped and counted (bounded latency / memory) instead of blocking the
        audio thread.
        """
        if status:  # XRuns / overflow etc. — informational, keep capturing.
            logger.warning("Audio input stream status: %s", status)
        if self._stop_event.is_set():
            return

        # Copy out of the (reused) PortAudio buffer as mono int16 little-endian.
        try:
            frame_bytes = indata[:, 0].astype("<i2", copy=False).tobytes()
            num_samples = int(indata.shape[0])
        except Exception:  # pragma: no cover - defensive fallback
            frame_bytes = bytes(indata)
            num_samples = self._samples_per_frame

        ts_ms = int(self._samples_captured * 1000 / self._sample_rate)
        self._samples_captured += num_samples

        try:
            self._queue.put_nowait((frame_bytes, ts_ms))
        except queue.Full:
            self._dropped_frames += 1
            if self._dropped_frames == 1 or self._dropped_frames % 100 == 0:
                logger.warning(
                    "Audio frame queue full; dropping frames "
                    "(consumer is falling behind). dropped=%d",
                    self._dropped_frames,
                )

    def _drain_queue(self) -> None:
        """Discard any queued items (used on (re)start to clear stale frames)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


# ---------------------------------------------------------------------------
# Task 14.2 — ``VoiceCapture`` orchestrator and microphone gating.
#
# ``VoiceCapture`` is the hands-free orchestrator that sits above the concrete
# :class:`AudioSource` (Task 14.1). It gates activation on microphone
# availability, runs a :class:`~omnilimb_face.voice.vad.VadSegmenter` over the
# :class:`AudioFrame` stream, assembles :class:`VoiceSegment` PCM payloads from
# the buffered frames, and delivers completed segments to a registered
# callback (需求 4.1, 4.9, 11.5, 12.3, 12.4).
# ---------------------------------------------------------------------------

# Callback shape: receives one completed :class:`VoiceSegment` (PCM-populated).
SegmentCallback = Callable[[VoiceSegment], None]

# Default speech/silence decision threshold for the built-in RMS gate, as a
# fraction of int16 full scale. Frames whose normalized RMS is at or above this
# value count as voice activity; below it, as silence. Chosen low enough to
# treat any real speech as voiced while rejecting digital silence / faint noise.
_DEFAULT_RMS_SPEECH_THRESHOLD = 0.02


@dataclass(frozen=True)
class StartResult:
    """Outcome of :meth:`VoiceCapture.start_hands_free`.

    Attributes:
        activated: Whether hands-free mode is now active (the capture loop is
            running). ``False`` means the microphone gate refused to activate
            (no input device, or the source could not be opened) — text and
            rendering remain available regardless (需求 11.5).
        success: Whether the start request completed without an error path.
            Mirrors ``activated`` for this component (activation is the success
            condition); kept as a separate, explicit flag for callers that
            prefer success/error phrasing.
        reason: Human-readable explanation. Empty when activated; a descriptive
            microphone-unavailable message when refused (需求 4.9 / 11.5).
        error: The same descriptive message as :attr:`reason` when the start
            failed, else ``None`` — so callers can branch on either an
            ``error`` value or the boolean flags.
    """

    activated: bool
    success: bool
    reason: str = ""
    error: Optional[str] = None

    @classmethod
    def activated_ok(cls, reason: str = "") -> "StartResult":
        """Build a successful, activated result."""
        return cls(activated=True, success=True, reason=reason, error=None)

    @classmethod
    def unavailable(cls, reason: str) -> "StartResult":
        """Build a refused result carrying a descriptive ``reason``/``error``."""
        return cls(activated=False, success=False, reason=reason, error=reason)


class VoiceCapture:
    """Hands-free capture orchestrator with microphone gating (design "Voice_Capture").

    ``VoiceCapture`` wires together an injected :class:`AudioSource`, a
    :class:`~omnilimb_face.voice.vad.VadSegmenter`, and an optional wake-word
    gate. Collaborators are injected (not constructed internally) so the
    orchestrator is testable with a lightweight fake source that yields a
    scripted :class:`AudioFrame` list and a *real* :class:`VadSegmenter` — no
    live microphone or optional ``[voice]`` dependency required.

    Microphone gating (需求 4.9 / 11.5 / 12.4)
    ------------------------------------------
    :meth:`start_hands_free` makes its activation decision **solely** from
    ``source.list_input_devices()``: an empty list means no microphone is
    enumerated, so hands-free does **not** activate and a descriptive
    microphone-unavailable :class:`StartResult` is returned while text and
    rendering remain available. A non-empty list means a device exists, so the
    source is started and a consumer loop spun up. If starting the source raises
    :class:`MicrophoneUnavailableError` (device absent / in use / permission
    denied) the same refused result is returned rather than crashing.

    Runtime mic-loss (需求 12.3 / 12.4)
    -----------------------------------
    If the source raises mid-run (the device disappears while capturing), the
    consumer loop logs a descriptive error and turns hands-free **off**
    (``is_running()`` becomes ``False``) gracefully, without propagating the
    exception to the caller's thread.

    Segmentation / delivery
    -----------------------
    The consumer loop pulls :class:`AudioFrame` values from the source, derives
    a :class:`VadEvent` per frame from a simple normalized-RMS speech/silence
    gate, feeds the :class:`VadSegmenter`, and buffers the active segment's PCM.
    When the segmenter closes a segment, the buffered frames are concatenated
    into the :class:`VoiceSegment.pcm` payload and delivered to the callback
    registered via :meth:`on_segment`.
    """

    def __init__(
        self,
        cfg: "VTuberConfig",
        source: "AudioSource",
        vad: VadSegmenter,
        wake: "Optional[WakeWord]" = None,
        *,
        rms_speech_threshold: float = _DEFAULT_RMS_SPEECH_THRESHOLD,
        join_timeout_s: float = 5.0,
    ) -> None:
        """Build a hands-free orchestrator from injected collaborators.

        Args:
            cfg: The composed :class:`omnilimb_face.config.VTuberConfig`. Only
                read for informational/limit purposes; the segmentation
                thresholds live in the injected ``vad``.
            source: The :class:`AudioSource` to capture from (the concrete
                :class:`SoundDeviceAudioSource` in production, a fake in tests).
            vad: The :class:`VadSegmenter` state machine that converts the VAD
                event stream into :class:`VoiceSegment` boundaries.
            wake: Optional wake-word gate. When provided, completed segments are
                delivered only while its gate is open; ``None`` (the default)
                delivers every segment. Full wake-word detection wiring is done
                by later tasks; this orchestrator only consults the gate state.
            rms_speech_threshold: Normalized (0..1) RMS threshold for the
                built-in speech/silence classifier used to derive VAD events.
            join_timeout_s: Bound, in seconds, on how long :meth:`stop_hands_free`
                waits for the consumer thread to finish.
        """
        self._cfg = cfg
        self._source = source
        self._segmenter = vad
        self._wake = wake
        self._rms_threshold = float(rms_speech_threshold)
        self._join_timeout_s = float(join_timeout_s)

        self._on_segment: Optional[SegmentCallback] = None

        # Lifecycle state guarded by ``_lock``.
        self._lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()
        self._consumer_thread: Optional[threading.Thread] = None

        # Segment-assembly state (touched only on the consumer thread between
        # start and stop, so it needs no extra locking).
        self._buffer: List[bytes] = []
        self._event_in_speech = False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def on_segment(self, callback: SegmentCallback) -> None:
        """Register the callback invoked with each completed :class:`VoiceSegment`."""
        self._on_segment = callback

    def is_running(self) -> bool:
        """Whether hands-free mode is currently active."""
        return self._running

    def start_hands_free(self) -> StartResult:
        """Activate hands-free capture, gating on microphone availability.

        The activation decision is made **solely** from
        ``source.list_input_devices()`` (需求 4.9 / 11.5 / 12.4):

        * empty list → do **not** activate; return a microphone-unavailable
          result (text and rendering stay available);
        * non-empty list → start the source and spin the consumer loop. If the
          source raises :class:`MicrophoneUnavailableError` while starting,
          return the same unavailable result instead of crashing.

        Idempotent: calling while already running returns an activated result
        without starting a second loop.
        """
        with self._lock:
            if self._running:
                return StartResult.activated_ok("hands-free mode already running")

            # --- Microphone availability gate (pure: based on device list) ---
            try:
                devices = self._source.list_input_devices()
            except Exception as exc:  # defensive: enumeration must not crash us
                reason = (
                    "microphone unavailable: failed to enumerate input devices "
                    f"({exc}); hands-free mode not activated (text and rendering "
                    "remain available)"
                )
                logger.warning(reason, exc_info=True)
                return StartResult.unavailable(reason)

            if not devices:
                reason = (
                    "microphone unavailable: no input devices enumerated; "
                    "hands-free mode not activated (text and rendering remain "
                    "available)"
                )
                logger.warning(reason)
                return StartResult.unavailable(reason)

            # --- A device exists: prepare fresh run state and start capture ---
            self._stop_event.clear()
            self._segmenter.reset()
            self._buffer = []
            self._event_in_speech = False

            try:
                self._source.start()
            except MicrophoneUnavailableError as exc:
                reason = (
                    f"microphone unavailable: could not start audio source ({exc}); "
                    "hands-free mode not activated (text and rendering remain "
                    "available)"
                )
                logger.warning(reason)
                self._stop_event.set()
                return StartResult.unavailable(reason)

            self._running = True
            thread = threading.Thread(
                target=self._consume_loop,
                name="omnilimb-face-voice-capture",
                daemon=True,
            )
            self._consumer_thread = thread
            thread.start()
            return StartResult.activated_ok()

    def stop_hands_free(self) -> None:
        """Stop hands-free capture and the consumer loop cleanly (idempotent).

        Signals the loop to stop, stops the source (best-effort; errors are
        logged and swallowed) and joins the consumer thread. Safe to call when
        not running and safe to call repeatedly.
        """
        with self._lock:
            self._running = False
            self._stop_event.set()
            thread = self._consumer_thread
            self._consumer_thread = None

        # Stop the source outside the lock so a blocking stop() can't deadlock
        # against the consumer thread.
        try:
            self._source.stop()
        except Exception:
            logger.warning(
                "Error stopping audio source during stop_hands_free.",
                exc_info=True,
            )

        # Join the consumer thread (never join ourselves, e.g. if a callback
        # were to call stop_hands_free from within the loop).
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=self._join_timeout_s)

    # ------------------------------------------------------------------ #
    # Internal: consumer loop and per-frame processing
    # ------------------------------------------------------------------ #
    def _consume_loop(self) -> None:
        """Pull frames, derive VAD events, segment, and deliver completed segments.

        Runs on the dedicated consumer thread. A mid-run source error (the
        device disappearing) is caught and handled by turning hands-free off
        gracefully (需求 12.3 / 12.4) rather than propagating.
        """
        try:
            for frame in self._source.frames():
                if self._stop_event.is_set():
                    break
                self._process_frame(frame)
        except Exception as exc:
            self._handle_runtime_failure(exc)

    def _process_frame(self, frame: AudioFrame) -> None:
        """Derive a VAD event for one frame, buffer PCM, and emit any segment."""
        event = self._derive_event(frame)

        # Buffer the active segment's PCM. A ``speech_start`` opens a new buffer
        # with this frame; while a segment is already open every frame is
        # appended (including the trailing silence frame that ends it).
        if event.kind == "speech_start":
            self._buffer = [frame.pcm]
        elif self._segmenter.is_active:
            self._buffer.append(frame.pcm)

        segment = self._segmenter.feed(event)
        if segment is None:
            return

        # Segment closed: assemble the buffered PCM into the payload.
        pcm = b"".join(self._buffer)
        self._buffer = []
        completed = VoiceSegment(
            pcm=pcm,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            end_reason=segment.end_reason,
        )
        self._deliver(completed)

    def _derive_event(self, frame: AudioFrame) -> VadEvent:
        """Classify a frame as speech/silence (RMS gate) and build a VadEvent.

        Tracks a tiny speech/silence state so transitions map to the segmenter's
        event vocabulary: voiced-after-silence → ``speech_start``; continued
        voice → ``speech``; voiced→unvoiced → ``speech_end``; continued silence
        → ``silence``.
        """
        rms = _frame_rms(frame.pcm)
        voiced = rms >= self._rms_threshold

        if voiced:
            kind = "speech" if self._event_in_speech else "speech_start"
            self._event_in_speech = True
        else:
            kind = "speech_end" if self._event_in_speech else "silence"
            self._event_in_speech = False

        return VadEvent(kind=kind, ts_ms=frame.ts_ms, rms=rms)

    def _deliver(self, segment: VoiceSegment) -> None:
        """Hand a completed segment to the callback, honoring the wake gate."""
        callback = self._on_segment
        if callback is None:
            return
        # Optional wake-word gate: deliver only while the gate is open. Full
        # detection feeding is wired by later tasks; here we only consult state.
        if self._wake is not None and not self._wake.is_gate_open():
            return
        try:
            callback(segment)
        except Exception:
            logger.warning(
                "on_segment callback raised; continuing capture.",
                exc_info=True,
            )

    def _handle_runtime_failure(self, exc: BaseException) -> None:
        """Handle a mid-run source failure: log and turn hands-free off (需求 12.3/12.4)."""
        logger.error(
            "Microphone became unavailable during hands-free capture; turning "
            "off hands-free mode. error=%s",
            exc,
            exc_info=True,
        )
        # 需求 12.4: turn off hands-free. Run on the consumer thread, so just
        # flip the state and signal/stop the source — never join ourselves.
        self._running = False
        self._stop_event.set()
        try:
            self._source.stop()
        except Exception:
            logger.warning(
                "Error stopping audio source after a runtime microphone failure.",
                exc_info=True,
            )


def _frame_rms(pcm: bytes) -> float:
    """Return the normalized (0..1) RMS energy of a mono int16 PCM frame.

    Pure helper with no optional dependency: decodes little-endian int16 samples
    with the :mod:`array` module (byte-swapping on big-endian hosts) and returns
    ``sqrt(mean(sample**2)) / 32768``. Returns ``0.0`` for an empty/odd-length
    frame so a degenerate frame reads as silence rather than raising.
    """
    if not pcm:
        return 0.0
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":  # pragma: no cover - host-endianness dependent
        samples.byteswap()
    if not samples:
        return 0.0
    acc = 0
    for sample in samples:
        acc += sample * sample
    mean_square = acc / len(samples)
    return math.sqrt(mean_square) / 32768.0
