"""omnilimb_face.tts — TTS synthesis, lip-sync volumes, and ordered playback.

This module implements the ``TTS_Player`` component from the design
(Requirements 6 and 2.3). The host does not expose a streaming TTS callback or
amplitude data: synthesis goes through the host-registered ``text_to_speech``
tool, which renders a *whole sentence* to an audio *file* and returns its path
— neither streaming PCM nor any amplitude / volume series. The plugin therefore
synthesises sentence-by-sentence, decodes the produced audio, and computes the
lip-sync ``volumes`` itself with the pure :meth:`TTSPlayer.compute_volumes`
(aligned with Open-LLM-VTuber's ``prepare_audio_payload`` chunked-RMS logic).

Task split (design.md -> "Components and Interfaces" -> "TTS_Player"):

* **Task 9.1 (this file's current scope)** — the *pure* pieces that other tasks
  build on, with no I/O and no hard dependency on the optional ``[voice]``
  stack (notably **no numpy** at import time, so the function works in the core
  install): the frozen :class:`AudioSegmentOut` value object and the static,
  side-effect-free :meth:`TTSPlayer.compute_volumes`. This is the target of
  Property 10 (lip-sync volume normalization), whose Hypothesis test is added
  by Task 9.2.
* **Task 9.3 (this file's current scope)** — the ordered playback queue:
  :meth:`TTSPlayer.enqueue`, :meth:`TTSPlayer.stop`, :meth:`TTSPlayer.is_playing`
  and a real :class:`AudioSink`-driven playback worker thread, preserving
  non-decreasing text order even when segments finish synthesising out of order
  (Requirements 6.2, 5.2). The ordering contract is documented on
  :meth:`TTSPlayer.enqueue`: each segment carries an explicit, zero-based
  ``seq`` (its position in text/sentence order); the worker plays segments in
  strictly increasing ``seq`` from an internal expected-sequence cursor,
  holding back any out-of-order arrival until its predecessors have played.
  This is the target of Property 9 (playback order preservation), whose
  Hypothesis test is added by Task 9.4.
* **Task 12.1 (this file's current scope)** — :meth:`TTSPlayer.synthesize`, which
  calls ``ctx.dispatch_tool("text_to_speech", {...})``, parses the returned JSON
  for the audio file path, decodes PCM, and feeds :meth:`compute_volumes`, with
  the 3-attempt / 10 s-per-attempt retry and final-degrade behaviour: it returns
  a :class:`SynthResult` (never raising) so the caller can show the reply as
  plain text on failure (Requirements 6.1, 6.4, 6.5).

The remaining not-yet-implemented behaviours live in other tasks; this module
imports cleanly and pytest collection stays clean, while :meth:`compute_volumes`
and :meth:`synthesize` are fully functional.

``compute_volumes`` decodes int16 mono little-endian PCM using only the
standard library (:mod:`array` + :mod:`math`); it normalises to little-endian
explicitly via :func:`array.array.byteswap` on big-endian hosts, so it is
correct regardless of platform byte order and forward-compatible with Python
3.13+ (which removes the ``audioop`` module).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import heapq
import io
import json
import logging
import math
import sys
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Protocol, Tuple, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from omnilimb_face.config import TTSSettings

logger = logging.getLogger(__name__)

# Optional keyless TTS backend (Microsoft Edge online voices). Used as a
# FALLBACK when the host exposes no usable ``text_to_speech`` tool (e.g. a
# hermes build without that tool registered). Absent / offline -> the fallback
# simply isn't available and synthesize() reports the host failure as before.
try:  # pragma: no cover - availability is environment-dependent
    import edge_tts as _edge_tts  # type: ignore

    _EDGE_TTS_AVAILABLE = True
except Exception:  # pragma: no cover
    _edge_tts = None  # type: ignore
    _EDGE_TTS_AVAILABLE = False

#: Default Edge voice for the fallback when the configured ``tts`` voice is not
#: an Edge ``*Neural`` voice. Chinese, matching the project's primary audience.
_EDGE_TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


@dataclass(frozen=True)
class AudioSegmentOut:
    """One synthesised, ready-to-send audio segment (design "TTS_Player").

    Mirrors the ``/client-ws`` ``audio`` payload the front-end consumes for
    playback, lip-sync and expressions:

    Attributes:
        wav_bytes: The synthesised audio for this segment (e.g. WAV container
            bytes) to be base64-encoded by the protocol gateway.
        volumes: The chunked, normalized RMS volume series driving lip-sync
            (each element in ``[0.0, 1.0]``), as produced by
            :meth:`TTSPlayer.compute_volumes` (Requirement 7.3).
        slice_length_ms: The duration in milliseconds each ``volumes`` element
            covers (the front-end ``slice_length``).
        display_text: The displayable sentence text for this segment (emotion
            tags already stripped by the ``Expression_Mapper``).
        expressions: The Live2D expression indices for this segment, in order
            of appearance (Requirement 8).
    """

    wav_bytes: bytes
    volumes: List[float]
    slice_length_ms: int
    display_text: str
    expressions: List[int]


@dataclass(frozen=True)
class SynthResult:
    """Outcome of one :meth:`TTSPlayer.synthesize` call (Task 12.1).

    A fully-defaulted envelope mirroring :class:`omnilimb_face.stt.TranscribeResult`
    so callers branch on ``success`` without raising. ``synthesize`` **never
    raises** (Requirement 6.5): an exhausted retry budget or any decode/IO error
    is reported here so the caller can fall back to showing the reply as plain
    text while preserving already-displayed content.

    Attributes:
        success: ``True`` when the host TTS tool produced an audio file that was
            decoded into a playable :class:`AudioSegmentOut`.
        segment: The synthesised :class:`AudioSegmentOut` on success; ``None`` on
            failure. Its ``expressions`` list is left empty here — the
            ``Expression_Mapper`` (Task 22.1) fills it in for the caller.
        error: A human-readable failure message on failure; ``None`` on success
            (Requirement 6.5 — descriptive error naming the failure cause).
        reason: A short failure category on failure; ``None`` on success:

            * ``"empty_text"`` — nothing to synthesise (blank input);
            * ``"no_dispatch"`` — no host ``dispatch_tool`` was wired;
            * ``"timeout"`` — an attempt exceeded ``cfg.synth_timeout_s``;
            * ``"decode_failed"`` — the audio file could not be decoded as
              16-bit PCM WAV (e.g. an mp3/ogg produced without the optional
              ``[voice]`` extra / ffmpeg);
            * ``"tts_failed"`` — host tool reported failure, returned no usable
              path, the file was missing/empty, or any other failure.
        provider: The TTS provider the host reported, when available;
            informational only.
    """

    success: bool
    segment: Optional[AudioSegmentOut] = None
    error: Optional[str] = None
    reason: Optional[str] = None
    provider: Optional[str] = None


class _UnsupportedAudioError(Exception):
    """Raised internally when an audio file cannot be decoded as 16-bit PCM WAV.

    Kept private to this module: :meth:`TTSPlayer.synthesize` catches it and
    converts it into a ``reason="decode_failed"`` :class:`SynthResult` rather
    than ever letting it escape (Requirement 6.5).
    """


@runtime_checkable
class AudioSink(Protocol):
    """Audio output consumer used by the ordered playback queue (Task 9.3).

    Kept as a structural :class:`typing.Protocol` so the playback queue and its
    tests can supply any object exposing ``play`` / ``stop`` (including an
    in-memory recorder for the Property 9 playback-order test) without a hard
    dependency on a concrete audio backend.
    """

    def play(self, wav_bytes: bytes) -> None:
        """Play (or enqueue for playback) the given audio bytes."""
        ...

    def stop(self) -> None:
        """Immediately stop any in-progress playback (barge-in, Req 5.2)."""
        ...


class TTSPlayer:
    """Synthesise text via the host TTS tool and play segments in order.

    Construction stores the collaborators used by the later I/O tasks; every
    collaborator is optional (default ``None``) so the pure
    :meth:`compute_volumes` static method — and Task 9.1's importable shell —
    can be exercised without a configured TTS backend, an audio sink, or the
    host's ``dispatch_tool`` (mirroring the optional-collaborator pattern used
    by :class:`omnilimb_face.interruption.InterruptionController`).

    Args:
        cfg: TTS settings reused from the host ``tts`` section (Req 2.3); only
            consulted by the I/O methods landed in Tasks 12.1 / 9.3.
        dispatch_tool: The host ``ctx.dispatch_tool`` callable used to invoke
            the registered ``text_to_speech`` tool (wired in Task 12.1).
        sink: The :class:`AudioSink` that performs actual playback (wired in
            Task 9.3).
    """

    #: Default lip-sync slice length (ms) when none is supplied. Mirrors the
    #: VAD frame cadence and Open-LLM-VTuber's default chunk length.
    DEFAULT_SLICE_LENGTH_MS = 20

    #: Default timeout (seconds) :meth:`stop` waits for the playback worker to
    #: exit before returning. Bounded so a wedged audio backend can never make
    #: barge-in (Requirement 5.2) hang the caller.
    _WORKER_JOIN_TIMEOUT_S = 5.0

    def __init__(
        self,
        cfg: "Optional[TTSSettings]" = None,
        dispatch_tool: Optional[Any] = None,
        sink: Optional[AudioSink] = None,
        enable_fallback_tts: bool = False,
    ) -> None:
        self._cfg = cfg
        self._dispatch_tool = dispatch_tool
        self._sink = sink
        # When True, synthesize() falls back to the keyless Edge-TTS backend if
        # the host has no usable text_to_speech tool. Off by default so unit
        # tests (which assert host-failure stays a failure) and offline runs are
        # unaffected; the plugin runtime enables it for the live avatar.
        self._enable_fallback_tts = bool(enable_fallback_tts)

        # ----- Ordered playback queue state (Task 9.3) -----------------
        # A single lock guards all queue state; the bound Condition lets the
        # worker block until a playable segment arrives (or a stop is
        # requested) without busy-waiting, and lets waiters observe drain.
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # Min-heap of (seq, tiebreak_counter, segment) ordered by text/sentence
        # sequence so the smallest outstanding seq is always at the top.
        self._heap: "List[Tuple[int, int, AudioSegmentOut]]" = []
        # Monotonic tiebreaker so two equal seqs never fall back to comparing
        # the (un-orderable) AudioSegmentOut payloads.
        self._counter = 0
        # Expected-sequence cursor: the seq of the next segment to play. A
        # segment is only emitted to the sink when its seq equals this cursor,
        # which is what guarantees non-decreasing playback order (Property 9).
        self._next_seq = 0
        # Number of segments enqueued in the current playback session; used to
        # auto-assign a submission-order seq when the caller omits one.
        self._submitted = 0
        # True only while a segment is actively being handed to the sink.
        self._active_seg = False
        # Set to request the worker to stop and to mark the player stopped.
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Pure logic (Task 9.1) — fully implemented, no I/O, no numpy.
    # ------------------------------------------------------------------
    @staticmethod
    def compute_volumes(
        pcm: bytes,
        sample_rate: int,
        slice_length_ms: int,
    ) -> Tuple[List[float], int]:
        """Compute the normalized, chunked RMS lip-sync volume series.

        Pure function (Property 10). The decoded PCM — assumed **int16 mono
        little-endian** — is split into consecutive chunks each covering
        ``slice_length_ms`` milliseconds (``chunk_size`` samples), the per-chunk
        RMS is computed, and the series is normalized by its peak so the loudest
        chunk maps to ``1.0`` (aligned with Open-LLM-VTuber's
        ``prepare_audio_payload`` lip-sync ``volumes``).

        Invariants for a buffer that is **not** all-silent:

        * every element lies in the closed interval ``[0.0, 1.0]``;
        * ``len(volumes)`` equals the number of chunks produced by splitting on
          ``slice_length_ms`` (``ceil(num_samples / chunk_size)``); and
        * ``max(volumes) == 1.0`` (the series is peak-normalized).

        Defined behaviour for the degenerate inputs:

        * **all-silent** buffer (every sample zero) -> a list of zeros of the
          correct chunk count (no divide-by-zero);
        * buffer **shorter than one chunk** -> a single (possibly final, short)
          chunk is still produced;
        * **empty** buffer (no whole samples) -> an empty list.

        Implementation uses only :mod:`array` + :mod:`math` (no numpy), so it
        runs in the core install where the optional ``[voice]`` extra (and its
        numpy) is absent. Byte order is normalized to little-endian explicitly,
        so results are platform-independent.

        Args:
            pcm: Decoded PCM bytes, interpreted as int16 mono little-endian. A
                trailing odd byte (incomplete sample) is ignored.
            sample_rate: Sample rate in Hz used to size each chunk.
            slice_length_ms: Milliseconds covered by each chunk / volume sample.

        Returns:
            A ``(volumes, slice_length_ms)`` tuple, where ``slice_length_ms`` is
            echoed back unchanged for the front-end ``slice_length`` field.
        """
        # Decode int16 LE mono using the stdlib; drop a trailing odd byte so the
        # buffer is a whole number of 2-byte samples.
        usable = len(pcm) - (len(pcm) % 2)
        samples = array("h", pcm[:usable])
        # ``array('h')`` uses native byte order; normalize to little-endian so
        # the result is identical on big-endian hosts too.
        if sys.byteorder == "big":
            samples.byteswap()

        num_samples = len(samples)
        if num_samples == 0:
            # No whole samples -> no chunks.
            return [], slice_length_ms

        # Samples per chunk. Clamp to >= 1 so degenerate sample_rate /
        # slice_length_ms values can never yield a zero-size (infinite) chunk.
        chunk_size = int(sample_rate * slice_length_ms / 1000)
        if chunk_size < 1:
            chunk_size = 1

        # ceil(num_samples / chunk_size); the final chunk may be short.
        num_chunks = (num_samples + chunk_size - 1) // chunk_size

        rms_values: List[float] = []
        for start in range(0, num_samples, chunk_size):
            chunk = samples[start : start + chunk_size]
            sum_squares = 0
            for value in chunk:
                sum_squares += value * value
            rms_values.append(math.sqrt(sum_squares / len(chunk)))

        peak = max(rms_values)
        if peak <= 0.0:
            # All-silent buffer: avoid divide-by-zero, return zeros.
            return [0.0] * num_chunks, slice_length_ms

        volumes = [rms / peak for rms in rms_values]
        return volumes, slice_length_ms

    # ------------------------------------------------------------------
    # Ordered playback queue (Task 9.3) — real worker-thread consumer.
    # ------------------------------------------------------------------
    def enqueue(self, seg: AudioSegmentOut, seq: Optional[int] = None) -> None:
        """Queue a synthesised segment for in-order playback (Requirement 6.2).

        Ordering contract
        -----------------
        ``seq`` is the **zero-based position of this segment in text/sentence
        order**. The playback worker emits segments to the :class:`AudioSink`
        in strictly increasing ``seq`` starting from an internal
        expected-sequence cursor (``0`` at the start of a playback session), so
        a segment that finishes synthesising early is *held back* until all of
        its lower-``seq`` predecessors have played. This makes the order in
        which the sink receives segments **non-decreasing in ``seq``**
        regardless of the order in which synthesis completes and ``enqueue`` is
        called (Property 9).

        When ``seq`` is omitted it defaults to the number of segments already
        submitted in the current session (i.e. *submission order*). That
        fallback is only correct when ``enqueue`` is invoked in text order; any
        caller that synthesises sentences concurrently MUST pass the explicit
        text-order ``seq``.

        Calling :meth:`enqueue` after a :meth:`stop` transparently starts a
        fresh playback session: the cursor resets to ``0`` and a new worker
        thread is spawned, so ``stop``/``enqueue`` cycles (barge-in then a new
        reply) work without reconstructing the player.

        Args:
            seg: The synthesised :class:`AudioSegmentOut` to play.
            seq: Optional explicit text-order sequence index (``>= 0``).

        Raises:
            TypeError: If ``seg`` is not an :class:`AudioSegmentOut`.
            ValueError: If an explicit ``seq`` is negative.
        """
        if not isinstance(seg, AudioSegmentOut):
            raise TypeError(
                f"enqueue expects an AudioSegmentOut, got {type(seg).__name__!r}"
            )
        if seq is not None and seq < 0:
            raise ValueError(f"seq must be non-negative, got {seq!r}")

        with self._cond:
            # A prior stop() leaves the player in the "stopped" state; the next
            # enqueue begins a brand-new session from a clean cursor.
            if self._stop_event.is_set():
                self._reset_locked()
            resolved_seq = self._submitted if seq is None else int(seq)
            self._submitted += 1
            heapq.heappush(self._heap, (resolved_seq, self._counter, seg))
            self._counter += 1
            self._ensure_worker_locked()
            # Wake the worker so it can re-check whether the head is playable.
            self._cond.notify_all()

    def stop(self) -> None:
        """Immediately stop playback and clear the queue (Requirement 5.2).

        Used for barge-in: signals the worker to exit, drains every pending
        segment, and asks the :class:`AudioSink` to halt any in-progress
        playback. The method is **idempotent** — calling it when already
        stopped is a harmless no-op — and never blocks indefinitely: the worker
        join is bounded by :data:`_WORKER_JOIN_TIMEOUT_S` so a wedged audio
        backend cannot hang the caller.

        After ``stop`` the player is idle (:meth:`is_playing` returns ``False``
        and the queue is empty); a subsequent :meth:`enqueue` starts a fresh
        playback session.
        """
        with self._cond:
            self._stop_event.set()
            self._heap.clear()
            worker = self._worker
            # Wake the worker (and any drain waiters) so it observes the stop.
            self._cond.notify_all()

        # Halt in-progress playback outside the lock so a re-entrant sink can
        # never deadlock against us; best-effort and idempotent by contract.
        sink = self._sink
        if sink is not None:
            try:
                sink.stop()
            except Exception:  # noqa: BLE001 - barge-in stop must not raise
                pass

        # Join the worker outside the lock; guard against joining ourselves if
        # stop() were ever invoked from within sink.play() on the worker.
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self._WORKER_JOIN_TIMEOUT_S)

        with self._cond:
            self._active_seg = False
            if self._worker is worker:
                self._worker = None
            self._cond.notify_all()

    def is_playing(self) -> bool:
        """Report whether a segment is playing or pending playback (Task 9.3).

        Returns ``True`` while a segment is actively being handed to the sink
        or while segments remain queued (including ones held back waiting for
        an earlier ``seq``); ``False`` once the queue has drained or after
        :meth:`stop`.
        """
        with self._lock:
            if self._stop_event.is_set():
                return False
            return self._active_seg or bool(self._heap)

    def wait_until_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until the queue has fully drained (no pending/active segment).

        Provided to make playback deterministically testable: after enqueuing a
        batch of segments a caller can wait for them all to reach the sink
        before asserting on the recorded order. Returns ``True`` once idle,
        ``False`` if ``timeout`` (seconds) elapses first or the player was
        stopped. A player with a held-back gap (a missing predecessor ``seq``)
        never becomes idle and will time out, since there is still pending work
        that cannot proceed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._stop_event.is_set() and (self._active_seg or self._heap):
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            return not self._stop_event.is_set()

    # ----- internal queue helpers (all called under ``self._cond``) ----
    def _reset_locked(self) -> None:
        """Reset session state for a fresh playback run (lock held)."""
        self._heap.clear()
        self._counter = 0
        self._next_seq = 0
        self._submitted = 0
        self._active_seg = False
        self._stop_event.clear()

    def _ensure_worker_locked(self) -> None:
        """Start the playback worker thread if one is not already running."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run,
                name="tts-playback",
                daemon=True,
            )
            self._worker.start()

    def _head_ready_locked(self) -> bool:
        """Whether the smallest queued seq is the one due next (lock held).

        Defensively discards any stale entries whose slot has already been
        played (``seq < cursor``); these can only appear from a duplicate/late
        seq and would otherwise wedge the head of the heap forever.
        """
        while self._heap and self._heap[0][0] < self._next_seq:
            heapq.heappop(self._heap)
        return bool(self._heap) and self._heap[0][0] == self._next_seq

    def _run(self) -> None:
        """Playback worker loop: emit segments to the sink in seq order."""
        while True:
            with self._cond:
                while not self._stop_event.is_set() and not self._head_ready_locked():
                    self._cond.wait()
                if self._stop_event.is_set():
                    return
                seq, _, seg = heapq.heappop(self._heap)
                self._next_seq = seq + 1
                self._active_seg = True

            # Play outside the lock so enqueue()/stop() stay responsive while
            # the (potentially blocking) sink runs. Re-check the stop flag to
            # narrow the window where a just-popped segment would play after a
            # barge-in stop.
            try:
                sink = self._sink
                if sink is not None and not self._stop_event.is_set():
                    sink.play(seg.wav_bytes)
            finally:
                with self._cond:
                    self._active_seg = False
                    self._cond.notify_all()

    # ------------------------------------------------------------------
    # Host-tool synthesis (Task 12.1) — invoke text_to_speech, decode, volumes.
    # ------------------------------------------------------------------
    def synthesize(self, text: str) -> SynthResult:
        """Synthesise one sentence via the host ``text_to_speech`` tool (Task 12.1).

        Calls ``dispatch_tool("text_to_speech", {"text": text})`` (the host tool
        injected as ``self._dispatch_tool`` in :meth:`__init__`), parses the
        returned JSON envelope for the audio **file** path (the host renders a
        whole sentence to a file and returns its path — usually a ``MEDIA:`` tag
        under ``~/voice-memos/`` — never streamed PCM nor amplitude data), reads
        and decodes that file to int16 mono PCM, and feeds
        :meth:`compute_volumes` to derive the lip-sync ``volumes`` /
        ``slice_length`` (Requirement 6.1). The decoded audio is packaged into an
        :class:`AudioSegmentOut` whose ``wav_bytes`` are the raw file bytes and
        whose ``expressions`` are left empty for the ``Expression_Mapper`` (Task
        22.1) to fill.

        Retry / timeout policy (Requirement 6.4): up to ``cfg.max_attempts``
        attempts (default 3 = first try + 2 retries), each bounded by
        ``cfg.synth_timeout_s`` seconds (default 10 s). The per-attempt timeout
        is enforced at the wrapper via :meth:`concurrent.futures.Future.result`
        because the host ``dispatch_tool`` exposes no timeout argument.

        This method **never raises** (Requirement 6.5): when every attempt fails
        — host error envelope, missing/empty/undecodable file, timeout, or no
        ``dispatch_tool`` — it returns a failed :class:`SynthResult` whose
        ``reason`` names the failure category and whose ``error`` is descriptive,
        so the caller can show the reply as plain text and keep already-displayed
        content.

        Non-WAV audio (e.g. an mp3/ogg produced when the optional ``[voice]``
        extra / ffmpeg is configured on the host) cannot be decoded with the
        standard-library :mod:`wave` reader; this degrades gracefully to a
        ``reason="decode_failed"`` result rather than taking a hard dependency on
        an optional audio decoder.

        Args:
            text: The sentence to synthesise (emotion tags already stripped by
                the caller). Blank text short-circuits to a failed result.

        Returns:
            A :class:`SynthResult`; ``success`` is ``True`` with a populated
            ``segment`` on success, otherwise ``False`` with ``error`` / ``reason``.
        """
        if not isinstance(text, str) or not text.strip():
            return SynthResult(
                success=False,
                error="Cannot synthesise empty text.",
                reason="empty_text",
            )

        if self._dispatch_tool is None:
            # No host TTS tool wired — try the keyless Edge fallback directly.
            fallback = self._edge_tts_synthesize(text)
            if fallback is not None:
                return fallback
            return SynthResult(
                success=False,
                error="No host dispatch_tool wired; cannot invoke text_to_speech.",
                reason="no_dispatch",
            )

        max_attempts = self._resolve_max_attempts()
        timeout = self._resolve_timeout()

        last_error = "TTS synthesis failed."
        last_reason = "tts_failed"
        last_provider: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            outcome = self._attempt_once(text, timeout, attempt, max_attempts)
            if outcome.success:
                return outcome
            last_error = outcome.error or last_error
            last_reason = outcome.reason or last_reason
            last_provider = outcome.provider or last_provider
            logger.warning(
                "omnilimb-face TTS: attempt %d/%d failed (%s): %s",
                attempt,
                max_attempts,
                outcome.reason,
                outcome.error,
            )
            # If the host clearly has no such tool, retrying is pointless — go
            # straight to the Edge fallback.
            if self._looks_like_missing_tool(outcome.error):
                break

        # Every host attempt failed (tool missing, error, or undecodable) — try
        # the keyless Edge fallback before giving up (lets the avatar speak even
        # when the host has no text_to_speech tool registered).
        fallback = self._edge_tts_synthesize(text)
        if fallback is not None:
            return fallback

        return SynthResult(
            success=False,
            error=(
                f"TTS synthesis failed after {max_attempts} attempt(s): "
                f"{last_error}"
            ),
            reason=last_reason,
            provider=last_provider,
        )

    # ----- Edge-TTS fallback (host has no text_to_speech tool) ---------
    @staticmethod
    def _looks_like_missing_tool(error: Optional[str]) -> bool:
        """Heuristic: does ``error`` indicate the host lacks the TTS tool?"""
        if not isinstance(error, str):
            return False
        low = error.lower()
        return (
            "unknown tool" in low
            or "no tool" in low
            or "not found" in low
            or "no such tool" in low
            or "unregistered" in low
        )

    def _resolve_edge_voice(self) -> str:
        """Pick an Edge voice: the configured one if it's an Edge ``*Neural``
        voice, else the Chinese default. Keeps host ``tts.voice`` values meant
        for other providers (e.g. ``alloy``) from being sent to Edge."""
        configured = getattr(self._cfg, "voice", None)
        if isinstance(configured, str) and "Neural" in configured:
            return configured
        return _EDGE_TTS_DEFAULT_VOICE

    def _edge_tts_synthesize(self, text: str) -> Optional[SynthResult]:
        """Synthesise ``text`` via edge-tts (keyless) as a fallback backend.

        Returns a successful :class:`SynthResult` carrying the MP3 bytes with an
        EMPTY ``volumes`` list — the front-end derives lip-sync from the decoded
        waveform (see frontend/app.js ``volumesFromBuffer``) — or ``None`` when
        the fallback is disabled, edge-tts is unavailable, or it fails, so the
        caller reports the original host failure. Never raises.
        """
        if not self._enable_fallback_tts:
            return None
        if not _EDGE_TTS_AVAILABLE or _edge_tts is None:
            return None
        voice = self._resolve_edge_voice()
        try:
            mp3 = self._run_edge_tts(text, voice)
        except Exception as exc:  # noqa: BLE001 - fallback must never raise
            logger.warning("omnilimb-face Edge-TTS fallback failed: %s", exc)
            return None
        if not mp3:
            return None
        logger.info(
            "omnilimb-face TTS: used Edge fallback (voice=%s, %d bytes).",
            voice,
            len(mp3),
        )
        segment = AudioSegmentOut(
            wav_bytes=mp3,
            volumes=[],  # front-end computes lip-sync from the audio waveform
            slice_length_ms=self.DEFAULT_SLICE_LENGTH_MS,
            display_text=text,
            expressions=[],
        )
        return SynthResult(success=True, segment=segment, provider="edge-tts")

    @staticmethod
    def _run_edge_tts(text: str, voice: str) -> bytes:
        """Run edge-tts synthesis to MP3 bytes, isolating its event loop.

        edge-tts is async; this runs it on a private event loop in a dedicated
        thread so it works whether or not the caller is itself on an event loop.
        """
        async def _gen() -> bytes:
            communicate = _edge_tts.Communicate(text, voice)
            buf = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    buf.extend(chunk.get("data") or b"")
            return bytes(buf)

        result: dict = {}

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                result["data"] = loop.run_until_complete(_gen())
            finally:
                loop.close()

        thread = threading.Thread(target=_worker, name="omnilimb-face-edgetts", daemon=True)
        thread.start()
        thread.join(timeout=30.0)
        return result.get("data", b"")

    # ----- synthesis internals (Task 12.1) ----------------------------
    def _attempt_once(
        self,
        text: str,
        timeout: float,
        attempt: int,
        max_attempts: int,
    ) -> SynthResult:
        """Run a single synthesis attempt on a worker thread under a timeout.

        The whole attempt (dispatch + parse + file read + decode + volumes) runs
        in the worker so a wedged host TTS backend cannot exceed the per-attempt
        budget. A synchronous host call cannot be force-cancelled, so on timeout
        the worker is detached (``shutdown(wait=False)``).
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._synthesize_once, text)
        try:
            return future.result(timeout=timeout) if timeout else future.result()
        except concurrent.futures.TimeoutError:
            return SynthResult(
                success=False,
                error=(
                    f"TTS attempt {attempt}/{max_attempts} timed out after "
                    f"{timeout:g}s."
                ),
                reason="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - never propagate out of synthesize
            return SynthResult(
                success=False,
                error=f"TTS attempt {attempt}/{max_attempts} raised: {exc}",
                reason="tts_failed",
            )
        finally:
            executor.shutdown(wait=False)

    def _synthesize_once(self, text: str) -> SynthResult:
        """Perform one dispatch + decode cycle, returning a :class:`SynthResult`.

        Runs on the worker thread spawned by :meth:`_attempt_once`. Any failure
        is returned as a non-success result (which triggers a retry) rather than
        raised, except for unexpected exceptions which :meth:`_attempt_once`
        converts into a ``tts_failed`` result.
        """
        raw = self._dispatch_tool("text_to_speech", {"text": text})
        envelope = self._coerce_envelope(raw)
        if envelope is None:
            return SynthResult(
                success=False,
                error=(
                    "Host text_to_speech returned an unparseable response "
                    f"({type(raw).__name__})."
                ),
                reason="tts_failed",
            )

        provider = envelope.get("provider")
        provider = provider if isinstance(provider, str) else None

        if not envelope.get("success"):
            error = envelope.get("error")
            if not isinstance(error, str) or not error:
                error = "Host text_to_speech reported failure."
            return SynthResult(
                success=False, error=error, reason="tts_failed", provider=provider
            )

        path = self._extract_audio_path(envelope)
        if not path:
            return SynthResult(
                success=False,
                error="Host text_to_speech envelope contained no audio file path.",
                reason="tts_failed",
                provider=provider,
            )

        try:
            with open(path, "rb") as audio_file:
                file_bytes = audio_file.read()
        except OSError as exc:
            return SynthResult(
                success=False,
                error=f"Could not read synthesised audio file {path!r}: {exc}",
                reason="tts_failed",
                provider=provider,
            )

        if not file_bytes:
            return SynthResult(
                success=False,
                error=f"Synthesised audio file {path!r} was empty.",
                reason="tts_failed",
                provider=provider,
            )

        try:
            pcm, sample_rate = self._decode_wav_to_mono_int16(file_bytes)
        except _UnsupportedAudioError as exc:
            return SynthResult(
                success=False,
                error=(
                    f"Could not decode synthesised audio {path!r}: {exc}. "
                    "Non-WAV output requires the optional [voice] extra / ffmpeg."
                ),
                reason="decode_failed",
                provider=provider,
            )

        slice_length_ms = self._resolve_slice_length_ms()
        volumes, slice_length = self.compute_volumes(pcm, sample_rate, slice_length_ms)
        segment = AudioSegmentOut(
            wav_bytes=file_bytes,
            volumes=volumes,
            slice_length_ms=slice_length,
            display_text=text,
            expressions=[],
        )
        return SynthResult(success=True, segment=segment, provider=provider)

    @staticmethod
    def _coerce_envelope(raw: Any) -> Optional[dict]:
        """Return a dict envelope, parsing a JSON ``str`` / ``bytes`` if needed.

        The host tool returns a JSON string; defensively accept an already-parsed
        dict too. Anything else (or invalid JSON) yields ``None``.
        """
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                loaded = json.loads(raw)
            except (ValueError, TypeError):
                return None
            return loaded if isinstance(loaded, dict) else None
        return None

    @staticmethod
    def _extract_audio_path(envelope: dict) -> Optional[str]:
        """Extract the audio file path from the host envelope, MEDIA-aware.

        Tolerant of the host's real shape and likely variants: prefers the
        explicit ``file_path`` key (the real ``text_to_speech`` envelope), then
        falls back to common alternates (``path`` / ``output_path`` / ``file`` /
        ``audio_path``), and finally parses a ``MEDIA:`` line out of the
        ``media_tag`` field (which may be prefixed by an ``[[audio_as_voice]]``
        marker line). The ``MEDIA:`` prefix is stripped without splitting on the
        path's own drive-letter colon (e.g. ``C:\\...`` on Windows).
        """
        for key in ("file_path", "path", "output_path", "file", "audio_path"):
            value = envelope.get(key)
            if isinstance(value, str) and value.strip():
                return TTSPlayer._strip_media_prefix(value.strip())

        media_tag = envelope.get("media_tag")
        if isinstance(media_tag, str):
            for line in media_tag.splitlines():
                stripped = line.strip()
                if stripped.startswith("MEDIA:"):
                    candidate = stripped[len("MEDIA:") :].strip()
                    if candidate:
                        return candidate
        return None

    @staticmethod
    def _strip_media_prefix(value: str) -> str:
        """Strip a leading ``MEDIA:`` tag from a path-bearing string, if present."""
        if value.startswith("MEDIA:"):
            return value[len("MEDIA:") :].strip()
        return value

    @staticmethod
    def _decode_wav_to_mono_int16(data: bytes) -> Tuple[bytes, int]:
        """Decode WAV ``data`` to little-endian int16 **mono** PCM + sample rate.

        Uses only the standard-library :mod:`wave` reader (no optional decoder
        dependency). PCM WAV stores samples little-endian, so 16-bit mono frames
        are returned as-is. Multi-channel audio is down-mixed to mono by
        averaging channels so :meth:`compute_volumes` chunk sizing stays correct.

        Raises:
            _UnsupportedAudioError: If the bytes are not a readable WAV container
                (e.g. mp3/ogg) or use a non-16-bit sample width.
        """
        try:
            with wave.open(io.BytesIO(data), "rb") as wav_file:
                n_channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
                frames = wav_file.readframes(wav_file.getnframes())
        except (wave.Error, EOFError, OSError, ValueError) as exc:
            raise _UnsupportedAudioError(
                f"not a readable WAV container ({exc})"
            ) from exc

        if sample_width != 2:
            raise _UnsupportedAudioError(
                f"unsupported sample width {sample_width * 8}-bit; "
                "only 16-bit PCM WAV is supported"
            )
        if sample_rate <= 0:
            raise _UnsupportedAudioError(f"invalid sample rate {sample_rate}")

        if n_channels <= 1:
            return frames, sample_rate

        # Down-mix interleaved multi-channel int16 to mono by averaging.
        usable = len(frames) - (len(frames) % (2 * n_channels))
        samples = array("h", frames[:usable])
        if sys.byteorder == "big":
            # Interpret stored little-endian samples correctly on big-endian hosts.
            samples.byteswap()

        mono_values = [
            sum(samples[i : i + n_channels]) // n_channels
            for i in range(0, len(samples), n_channels)
        ]
        mono = array("h", mono_values)
        if sys.byteorder == "big":
            # Emit little-endian bytes so compute_volumes decodes consistently.
            mono.byteswap()
        return mono.tobytes(), sample_rate

    def _resolve_max_attempts(self) -> int:
        """Resolve the total attempt count (>= 1) from ``cfg.max_attempts``."""
        raw = getattr(self._cfg, "max_attempts", None)
        try:
            attempts = int(raw)
        except (TypeError, ValueError):
            return 3
        return attempts if attempts >= 1 else 1

    def _resolve_timeout(self) -> float:
        """Resolve a non-negative per-attempt timeout (0 -> wait indefinitely)."""
        raw = getattr(self._cfg, "synth_timeout_s", None)
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            return 10.0
        return timeout if timeout > 0 else 0.0

    def _resolve_slice_length_ms(self) -> int:
        """Resolve the lip-sync slice length (ms), defaulting when unset/invalid."""
        raw = getattr(self._cfg, "slice_length_ms", None)
        try:
            slice_length = int(raw)
        except (TypeError, ValueError):
            return self.DEFAULT_SLICE_LENGTH_MS
        return slice_length if slice_length > 0 else self.DEFAULT_SLICE_LENGTH_MS
