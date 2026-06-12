"""omnilimb_face.stt — STT_Engine: transcribe captured voice segments.

This module implements the ``STT_Engine`` component from the design
(design.md → "Components and Interfaces" → "STT_Engine"). It is the transcription
facade that reuses the host's ``stt`` configuration/back-end (需求 2.2) to turn a
captured :class:`omnilimb_face.voice.vad.VoiceSegment` into text that the
``LLM_Bridge`` later injects into the active CLI session (需求 4.4 — the actual
injection lives in Task 13.1).

Grounding — how hermes STT is really invoked (CRITICAL)
-------------------------------------------------------
Unlike TTS (the ``text_to_speech`` tool, dispatched via
``ctx.dispatch_tool(...)``), the host's speech-to-text back-end is **not** a
registry tool. ``tools/transcription_tools.py`` never calls
``registry.register`` for it; the host and its own tests invoke it as a plain
module function::

    from tools.transcription_tools import transcribe_audio
    result = transcribe_audio(file_path, model=None)

Its real signature and return shape (verified against the host source) are::

    def transcribe_audio(file_path: str, model: Optional[str] = None) -> dict:
        # returns {"success": bool, "transcript": str,
        #          "error": str (optional), "provider": str (optional)}

Therefore this engine **directly calls that module function** (never
``ctx.dispatch_tool`` — the registry has no such tool name and dispatch would
fail). The callable is *injected* as the ``host_transcribe_audio`` constructor
argument so tests can supply a mock; when omitted it defaults to a **lazy
import** helper (:func:`_default_host_transcribe_audio`) that imports
``tools.transcription_tools`` only at call time. This keeps ``omnilimb_face.stt``
importable outside a hermes checkout (e.g. in the plugin's own unit/property
test environment), satisfying the degraded-import requirement (需求 12.1).

Timeout / failure handling (需求 4.7)
-------------------------------------
``transcribe_audio`` is a synchronous call with no native timeout knob, so
:meth:`STTEngine.transcribe` runs it on a worker thread and waits at most
``cfg.transcribe_timeout_s`` seconds for the result. On timeout (or any host
exception, or an unparseable envelope) the segment is dropped and a descriptive
error :class:`TranscribeResult` is returned (``success=False``); the worker
thread is detached (``shutdown(wait=False)``) because a synchronous host call
cannot be force-cancelled in Python.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import tempfile
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only imports, no runtime dependency
    from omnilimb_face.config import STTSettings
    from omnilimb_face.voice.vad import VoiceSegment

logger = logging.getLogger(__name__)

__all__ = [
    "Transcript",
    "TranscribeResult",
    "STTEngine",
]

#: Fallback sample rate (Hz) for the temporary WAV when neither the segment nor
#: the STT settings carry one. Matches the VAD default capture rate (16 kHz),
#: which is also the rate faster-whisper / cloud Whisper expect.
DEFAULT_SAMPLE_RATE = 16000


@dataclass(frozen=True)
class Transcript:
    """A transcription result's text plus a blank-ness flag.

    Attributes:
        text: The transcribed text exactly as returned by the host back-end.
        is_empty: ``True`` when ``text`` is empty or whitespace-only (需求 4.5),
            computed via :meth:`STTEngine.is_blank`. The ``LLM_Bridge``
            (Task 13.1) uses this to reject blank utterances without injecting.
    """

    text: str
    is_empty: bool


@dataclass(frozen=True)
class TranscribeResult:
    """Outcome of one :meth:`STTEngine.transcribe` call.

    A clean, fully-defaulted envelope so callers can branch on ``success``
    without worrying about which optional fields are populated:

    Attributes:
        success: ``True`` when the host back-end transcribed the segment
            (even if the resulting text is blank — blank *rejection* is the
            caller's concern, see :class:`Transcript.is_empty`).
        transcript: The :class:`Transcript` on success; ``None`` on failure.
        error: A human-readable error message on failure; ``None`` on success
            (需求 4.7 — descriptive error for dropped segments).
        reason: A short failure category — ``"timeout"`` when the back-end
            exceeded ``cfg.transcribe_timeout_s``, ``"stt_failed"`` for any
            other failure (host error envelope, exception, or unparseable
            response); ``None`` on success.
        provider: The STT provider the host reported (e.g. ``"local"``),
            when available; informational only.
    """

    success: bool
    transcript: Optional[Transcript] = None
    error: Optional[str] = None
    reason: Optional[str] = None
    provider: Optional[str] = None


def _default_host_transcribe_audio(file_path: str, model: Optional[str] = None) -> Any:
    """Lazy adapter to the host's ``tools.transcription_tools.transcribe_audio``.

    Imported **only when called** so ``omnilimb_face.stt`` stays importable
    outside a hermes checkout (需求 12.1). Mirrors the real signature
    ``transcribe_audio(file_path, model=None)`` and returns its envelope
    unchanged.
    """
    from tools.transcription_tools import transcribe_audio

    return transcribe_audio(file_path, model=model)


class STTEngine:
    """Transcribe captured voice segments via the host STT back-end (需求 2.2).

    Args:
        cfg: The host-reused :class:`omnilimb_face.config.STTSettings`
            (``model`` / ``language`` / ``transcribe_timeout_s``). Any object
            exposing those attributes works (duck typing).
        host_transcribe_audio: The injected callable pointing at the
            **directly-imported** ``tools.transcription_tools.transcribe_audio``
            (signature ``(file_path, model=None) -> dict``). Defaults to the
            lazy import helper :func:`_default_host_transcribe_audio` so the real
            host function is resolved on first use; tests pass a mock here.
    """

    def __init__(
        self,
        cfg: "STTSettings",
        host_transcribe_audio: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._cfg = cfg
        self._host_transcribe_audio: Callable[..., Any] = (
            host_transcribe_audio
            if host_transcribe_audio is not None
            else _default_host_transcribe_audio
        )

    # ------------------------------------------------------------------
    # Pure logic (需求 4.5) — the Property 6 blank-rejection target.
    # ------------------------------------------------------------------
    @staticmethod
    def is_blank(text: str) -> bool:
        """Return ``True`` for empty or whitespace-only text (需求 4.5).

        Pure predicate: ``len(text.strip()) == 0``. Part of the Property 6
        (inject vs blank rejection) target — the ``LLM_Bridge`` uses it to drop
        blank utterances instead of injecting them into the session.
        """
        return len(text.strip()) == 0

    # ------------------------------------------------------------------
    # Transcription (需求 2.2, 4.4 [provides text], 4.7)
    # ------------------------------------------------------------------
    def transcribe(self, segment: "VoiceSegment") -> TranscribeResult:
        """Transcribe one captured :class:`VoiceSegment`.

        Writes the segment's int16 mono PCM to a temporary WAV file, calls the
        injected host ``transcribe_audio(file_path, model=cfg.model or None)``
        with a ``cfg.transcribe_timeout_s`` guard, parses the returned envelope,
        and builds a :class:`TranscribeResult`:

        * host envelope ``success=True`` -> ``success`` result whose
          :class:`Transcript` carries the ``transcript`` text and an
          ``is_empty`` flag from :meth:`is_blank`;
        * host envelope ``success=False`` -> failure result (``reason``
          ``"stt_failed"``) carrying the host error message;
        * back-end exceeded the timeout -> failure result (``reason``
          ``"timeout"``), 需求 4.7;
        * host raised, or returned an unparseable / non-dict envelope ->
          failure result (``reason`` ``"stt_failed"``).

        The temporary file is always removed, even on failure.
        """
        model = self._resolve_model()
        sample_rate = self._resolve_sample_rate(segment)
        pcm = getattr(segment, "pcm", b"") or b""

        try:
            tmp_path = self._write_wav(pcm, sample_rate)
        except (OSError, wave.Error) as exc:
            logger.error("omnilimb-face STT: failed to stage audio: %s", exc)
            return TranscribeResult(
                success=False,
                error=f"Failed to write temporary audio file: {exc}",
                reason="stt_failed",
            )

        try:
            return self._invoke_and_parse(tmp_path, model)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:  # pragma: no cover - best-effort cleanup
                logger.debug(
                    "omnilimb-face STT: temp file cleanup failed for %s",
                    tmp_path,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve_model(self) -> Optional[str]:
        """Resolve the model override passed to the host (``None`` when unset).

        ``cfg.model or None`` so an empty string falls back to the host's own
        configured/default model rather than forcing a blank model id.
        """
        model = getattr(self._cfg, "model", None)
        return model or None

    def _resolve_sample_rate(self, segment: "VoiceSegment") -> int:
        """Pick a WAV sample rate: segment > cfg > :data:`DEFAULT_SAMPLE_RATE`."""
        for source in (segment, self._cfg):
            rate = getattr(source, "sample_rate", None)
            if isinstance(rate, int) and rate > 0:
                return rate
        return DEFAULT_SAMPLE_RATE

    @staticmethod
    def _write_wav(pcm: bytes, sample_rate: int) -> str:
        """Write int16 mono PCM to a fresh temp WAV file; return its path.

        The file descriptor from :func:`tempfile.mkstemp` is closed immediately
        so :mod:`wave` can reopen the path for writing on Windows (where an open
        handle would otherwise block re-opening).
        """
        fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="omnilimb-face-stt-")
        os.close(fd)
        try:
            with wave.open(tmp_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)  # int16 -> 2 bytes/sample
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm)
        except Exception:
            # Don't leak the temp file if writing fails.
            try:
                os.remove(tmp_path)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
            raise
        return tmp_path

    def _invoke_and_parse(self, file_path: str, model: Optional[str]) -> TranscribeResult:
        """Call the host back-end with a timeout guard and parse its envelope."""
        timeout = self._resolve_timeout()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._host_transcribe_audio, file_path, model=model)
        try:
            envelope = future.result(timeout=timeout) if timeout else future.result()
        except concurrent.futures.TimeoutError:
            logger.error(
                "omnilimb-face STT: transcription timed out after %.3gs", timeout
            )
            return TranscribeResult(
                success=False,
                error=(
                    f"STT transcription timed out after {timeout:g}s; "
                    f"segment dropped."
                ),
                reason="timeout",
            )
        except Exception as exc:  # host back-end raised
            logger.error(
                "omnilimb-face STT: transcription back-end raised: %s",
                exc,
                exc_info=True,
            )
            return TranscribeResult(
                success=False,
                error=f"STT transcription failed: {exc}",
                reason="stt_failed",
            )
        finally:
            # A synchronous host call cannot be force-cancelled; detach the
            # worker without blocking on it (relevant on the timeout path).
            executor.shutdown(wait=False)

        return self._build_result(envelope)

    def _resolve_timeout(self) -> float:
        """Resolve a non-negative timeout in seconds (0 -> wait indefinitely)."""
        raw = getattr(self._cfg, "transcribe_timeout_s", None)
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return timeout if timeout > 0 else 0.0

    @classmethod
    def _build_result(cls, envelope: Any) -> TranscribeResult:
        """Turn a host transcribe envelope into a :class:`TranscribeResult`.

        Accepts the real ``dict`` envelope and, defensively, a JSON ``str`` /
        ``bytes`` envelope (``json.loads`` it). Anything else is treated as a
        failure.
        """
        parsed = cls._coerce_envelope(envelope)
        if parsed is None:
            return TranscribeResult(
                success=False,
                error=(
                    "STT back-end returned an unparseable response "
                    f"({type(envelope).__name__})."
                ),
                reason="stt_failed",
            )

        provider = parsed.get("provider")
        provider = provider if isinstance(provider, str) else None

        if not parsed.get("success"):
            error = parsed.get("error")
            if not isinstance(error, str) or not error:
                error = "STT transcription failed."
            return TranscribeResult(
                success=False,
                error=error,
                reason="stt_failed",
                provider=provider,
            )

        raw_text = parsed.get("transcript")
        text = raw_text if isinstance(raw_text, str) else ""
        transcript = Transcript(text=text, is_empty=cls.is_blank(text))
        return TranscribeResult(
            success=True,
            transcript=transcript,
            provider=provider,
        )

    @staticmethod
    def _coerce_envelope(envelope: Any) -> Optional[dict]:
        """Return a dict envelope, parsing a JSON string/bytes if needed."""
        if isinstance(envelope, dict):
            return envelope
        if isinstance(envelope, (str, bytes, bytearray)):
            try:
                loaded = json.loads(envelope)
            except (ValueError, TypeError):
                return None
            return loaded if isinstance(loaded, dict) else None
        return None
