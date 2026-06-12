"""omnilimb_face.live2d — Live2D model info and the avatar driver (front-end contract).

This module implements the **Python side** of the ``Live2D_Renderer`` front-end
contract from the design (design.md -> "Components and Interfaces" ->
"Live2D_Renderer（前端契约）" and Data Models -> ``Live2DModelInfo``). The actual
Cubism rendering, lip-sync and expression transitions are the front-end's job;
Python only *describes* the model and *constructs/dispatches* the driving data
over the ``/client-ws`` protocol.

Two pieces live here (Task 17.2):

* :class:`Live2DModelInfo` — a frozen value object describing the model the
  front-end should load (``name`` / ``url`` / ``emotion_map``). Its
  :meth:`Live2DModelInfo.load_model_info` classmethod reads a model name out of
  an Open-LLM-VTuber-style ``model_dict.json`` and, when the configured model
  file is **missing or cannot be parsed**, *degrades* to a placeholder
  (``is_placeholder=True``) and logs a descriptive error instead of raising
  (Requirement 7.5).
* :class:`Live2DDirector` — builds and (defensively) dispatches the driving
  server events: ``set-model-and-conf`` on startup (Requirement 7.1), an
  ``audio`` payload per synthesized segment, and an idle/neutral drive that
  returns the avatar to a closed-mouth, neutral-expression resting state
  (Requirements 7.4 / 8.5).

Coupling note: the real ``ProtocolGateway`` (Task 20.1) is not wired yet, so the
director treats its ``gateway`` collaborator loosely — it only calls a gateway
method when one is present and is a plain (non-coroutine) callable. This keeps
the module import-safe and unit-testable before the WebSocket gateway lands; the
full async ``send`` fan-out is wired later (Tasks 20.1 / 22.1). Event dataclasses
are imported from :mod:`omnilimb_face.protocol.events`, which already exists.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from omnilimb_face.protocol.events import AudioEvent, ControlEvent, SetModelEvent

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from omnilimb_face.config import Live2DSettings
    from omnilimb_face.protocol.events import ServerEvent

logger = logging.getLogger(__name__)

# Conventional emotion-map key for the neutral / resting expression. Mirrors the
# documented default ``Live2DSettings.default_expression`` (需求 8.3 / 8.5).
DEFAULT_NEUTRAL_EXPRESSION = "neutral"


# ---------------------------------------------------------------------------
# Live2DModelInfo
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Live2DModelInfo:
    """Front-end load contract for a single Live2D model.

    Attributes:
        name: The model's identifier (matches a ``name`` entry in
            ``model_dict.json``).
        url: The model resource URL the front-end loads (the model3.json path).
        emotion_map: Emotion keyword -> Live2D expression index mapping, aligned
            with Open-LLM-VTuber's ``emotionMap`` (Requirement 8).
        is_placeholder: ``True`` when the configured model file was missing or
            could not be parsed and this info describes a fallback placeholder
            avatar (Requirement 7.5).
    """

    name: str
    url: str
    emotion_map: dict[str, int] = field(default_factory=dict)
    is_placeholder: bool = False

    def to_model_info_dict(self) -> dict:
        """Render this model info as the ``model_info`` payload for the front-end.

        The key names mirror the Open-LLM-VTuber model dictionary shape the
        compatible front-end expects (notably ``emotionMap``), so a stock
        front-end can consume the ``set-model-and-conf`` event unchanged.
        """
        return {
            "name": self.name,
            "url": self.url,
            "emotionMap": dict(self.emotion_map),
            "is_placeholder": self.is_placeholder,
        }

    def neutral_expression_index(
        self, default_expression: str = DEFAULT_NEUTRAL_EXPRESSION
    ) -> Optional[int]:
        """Resolve the neutral/default expression index from the emotion map.

        Returns the index mapped to ``default_expression`` (falling back to the
        conventional ``"neutral"`` key), or ``None`` when the model exposes no
        such expression (e.g. a placeholder with an empty map).
        """
        if default_expression in self.emotion_map:
            return self.emotion_map[default_expression]
        if DEFAULT_NEUTRAL_EXPRESSION in self.emotion_map:
            return self.emotion_map[DEFAULT_NEUTRAL_EXPRESSION]
        return None

    # -- Construction helpers ------------------------------------------------
    @classmethod
    def placeholder(cls, model_name: str = "default") -> "Live2DModelInfo":
        """Build the fallback placeholder model info (Requirement 7.5)."""
        return cls(name=model_name, url="", emotion_map={}, is_placeholder=True)

    @classmethod
    def from_settings(cls, settings: "Live2DSettings") -> "Live2DModelInfo":
        """Load model info from a :class:`~omnilimb_face.config.Live2DSettings`.

        Convenience wrapper around :meth:`load_model_info` using
        ``settings.model_name`` and ``settings.model_dict_path``. Duck-typed so
        it works with any object exposing those attributes.
        """
        model_name = getattr(settings, "model_name", "default")
        model_dict_path = getattr(settings, "model_dict_path", "")
        return cls.load_model_info(model_name, model_dict_path)

    @classmethod
    def load_model_info(
        cls,
        model_name: str,
        model_dict_path: Any,
    ) -> "Live2DModelInfo":
        """Load model info for ``model_name`` from an Open-LLM-VTuber model dict.

        ``model_dict_path`` points at a JSON file shaped like Open-LLM-VTuber's
        ``model_dict.json``: typically a list of model entries, each with a
        ``name``, ``url`` and ``emotionMap``. (A mapping of ``name -> entry`` and
        a single-entry object are also accepted defensively.)

        Degradation contract (Requirement 7.5): if the file is **missing**,
        **cannot be parsed** as JSON, has an **unexpected shape**, or contains
        **no entry** for ``model_name``, this method does **not** raise — it logs
        a descriptive error and returns a placeholder
        :class:`Live2DModelInfo` with ``is_placeholder=True``.

        Args:
            model_name: The model name to look up within the model dict.
            model_dict_path: Filesystem path (``str`` / :class:`pathlib.Path`) to
                the model dictionary JSON file.

        Returns:
            A populated :class:`Live2DModelInfo` on success, otherwise a
            placeholder instance.
        """
        if not model_dict_path:
            logger.error(
                "omnilimb-face Live2D: no model_dict_path configured for model "
                "'%s'; showing placeholder avatar.",
                model_name,
            )
            return cls.placeholder(model_name)

        path = Path(model_dict_path)
        if not path.is_file():
            logger.error(
                "omnilimb-face Live2D: model dictionary '%s' is missing for "
                "model '%s'; showing placeholder avatar.",
                path,
                model_name,
            )
            return cls.placeholder(model_name)

        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            logger.error(
                "omnilimb-face Live2D: model dictionary '%s' could not be read/"
                "parsed for model '%s' (%s); showing placeholder avatar.",
                path,
                model_name,
                exc,
            )
            return cls.placeholder(model_name)

        entry = cls._find_model_entry(data, model_name)
        if entry is None:
            logger.error(
                "omnilimb-face Live2D: model '%s' not found in model dictionary "
                "'%s'; showing placeholder avatar.",
                model_name,
                path,
            )
            return cls.placeholder(model_name)

        url = entry.get("url", "")
        if not isinstance(url, str):
            url = ""
        emotion_map = cls._coerce_emotion_map(
            entry.get("emotionMap", entry.get("emotion_map", {}))
        )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            name = model_name

        return cls(
            name=name,
            url=url,
            emotion_map=emotion_map,
            is_placeholder=False,
        )

    # -- Parsing internals ---------------------------------------------------
    @staticmethod
    def _find_model_entry(data: Any, model_name: str) -> Optional[dict]:
        """Locate the entry for ``model_name`` across the accepted shapes."""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name") == model_name:
                    return item
            return None
        if isinstance(data, dict):
            # Mapping of name -> entry.
            candidate = data.get(model_name)
            if isinstance(candidate, dict):
                # Ensure the entry carries its own name for downstream use.
                if "name" not in candidate:
                    merged = dict(candidate)
                    merged["name"] = model_name
                    return merged
                return candidate
            # Single-entry object that *is* the requested model.
            if data.get("name") == model_name:
                return data
            return None
        return None

    @staticmethod
    def _coerce_emotion_map(raw: Any) -> dict[str, int]:
        """Coerce a raw ``emotionMap`` into a clean ``dict[str, int]``.

        Non-dict inputs yield an empty map; entries whose value cannot be
        interpreted as an integer expression index are dropped (defensive
        against malformed dictionaries).
        """
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for key, value in raw.items():
            if isinstance(value, bool):
                # bool is an int subclass but never a valid expression index.
                continue
            if isinstance(value, int):
                result[str(key)] = value
            elif isinstance(value, float) and value.is_integer():
                result[str(key)] = int(value)
            elif isinstance(value, str):
                stripped = value.strip()
                if stripped.lstrip("-").isdigit():
                    result[str(key)] = int(stripped)
        return result


# ---------------------------------------------------------------------------
# Live2DDirector
# ---------------------------------------------------------------------------
class Live2DDirector:
    """Builds and dispatches the Live2D driving data over the protocol gateway.

    The director never renders anything itself; it constructs the
    :mod:`omnilimb_face.protocol.events` server events and hands them to the
    ``gateway`` collaborator. Because the real ``ProtocolGateway`` (Task 20.1)
    is not wired yet, dispatch is **defensive**: a gateway method is only called
    when it exists and is a plain (non-coroutine) callable, so this class imports
    and unit-tests cleanly today and slots onto the real gateway later.

    Args:
        model: The :class:`Live2DModelInfo` describing the avatar to drive.
        gateway: Any object exposing a synchronous ``send_event(event)`` or
            ``serialize(event)`` method (or ``None``). The full async ``send``
            fan-out is wired by Tasks 20.1 / 22.1.
        default_expression: Emotion-map key used to resolve the neutral
            expression for :meth:`push_idle` (需求 8.5).
    """

    # Gateway methods tried, in priority order, for a synchronous dispatch.
    _DISPATCH_METHODS: tuple[str, ...] = ("send_event", "serialize")

    def __init__(
        self,
        model: Live2DModelInfo,
        gateway: Any = None,
        *,
        default_expression: str = DEFAULT_NEUTRAL_EXPRESSION,
    ) -> None:
        self._model = model
        self._gateway = gateway
        self._default_expression = default_expression

    @property
    def model(self) -> Live2DModelInfo:
        """The model info this director is driving."""
        return self._model

    def announce_model(self) -> SetModelEvent:
        """Build and dispatch the ``set-model-and-conf`` event (Requirement 7.1).

        Carries the model info (``name`` / ``url`` / ``emotionMap`` / placeholder
        flag) so the front-end can load and display the model. Returns the built
        :class:`~omnilimb_face.protocol.events.SetModelEvent` for testability and
        later reuse.
        """
        event = SetModelEvent(
            model_info=self._model.to_model_info_dict(),
            conf_name=self._model.name,
        )
        self._dispatch(event)
        return event

    def push_audio_segment(self, seg: Any) -> AudioEvent:
        """Build and dispatch an ``audio`` event from a synthesized segment.

        ``seg`` is an ``AudioSegmentOut``-like object (duck-typed so this method
        works before ``omnilimb_face.tts`` lands). The resulting
        :class:`~omnilimb_face.protocol.events.AudioEvent` carries:

        * ``audio``: base64-encoded WAV (optional — ``None`` drives lip-sync /
          expressions only). Read from ``seg.audio`` when already a base64
          string, otherwise base64-encoded from ``seg.wav_bytes`` when present.
        * ``volumes`` / ``slice_length``: the chunked normalized RMS sequence and
          its per-sample millisecond span for front-end lip-sync (Requirement
          7.3).
        * ``display_text``: wrapped into the protocol ``{"text": ...}`` dict shape
          when ``seg.display_text`` is a plain string.
        * ``actions``: ``{"expressions": seg.expressions}`` driving the
          expression sequence (Requirement 8).

        Returns the built event.
        """
        event = AudioEvent(
            audio=self._resolve_audio(seg),
            volumes=list(_get(seg, "volumes", []) or []),
            slice_length=int(
                _get(seg, "slice_length_ms", _get(seg, "slice_length", 0)) or 0
            ),
            display_text=self._resolve_display_text(seg),
            actions={"expressions": list(_get(seg, "expressions", []) or [])},
        )
        self._dispatch(event)
        return event

    def push_idle(self) -> list["ServerEvent"]:
        """Return the avatar to an idle / neutral resting state (需求 7.4 / 8.5).

        Dispatches a ``control: mouth-reset`` event so the front-end closes the
        mouth to its resting state after playback (Requirement 7.4) and, when the
        model exposes a neutral expression index, a no-audio ``audio`` event that
        drives the neutral expression (Requirement 8.5). Returns the list of
        events dispatched (the neutral-expression event is omitted when the model
        has no neutral mapping, e.g. a placeholder).
        """
        events: list["ServerEvent"] = []

        mouth_reset = ControlEvent(text="mouth-reset")
        self._dispatch(mouth_reset)
        events.append(mouth_reset)

        neutral_index = self._model.neutral_expression_index(self._default_expression)
        if neutral_index is not None:
            neutral = AudioEvent(
                audio=None,
                actions={"expressions": [neutral_index]},
            )
            self._dispatch(neutral)
            events.append(neutral)

        return events

    # -- Internals -----------------------------------------------------------
    @staticmethod
    def _resolve_audio(seg: Any) -> Optional[str]:
        """Resolve the optional base64 audio payload from a segment."""
        audio = _get(seg, "audio", None)
        if isinstance(audio, str):
            return audio
        wav_bytes = _get(seg, "wav_bytes", None)
        if isinstance(wav_bytes, (bytes, bytearray)) and len(wav_bytes) > 0:
            return base64.b64encode(bytes(wav_bytes)).decode("ascii")
        return None

    @staticmethod
    def _resolve_display_text(seg: Any) -> dict:
        """Coerce ``seg.display_text`` into the protocol ``display_text`` dict."""
        display_text = _get(seg, "display_text", None)
        if isinstance(display_text, dict):
            return dict(display_text)
        if isinstance(display_text, str) and display_text:
            return {"text": display_text}
        return {}

    def _dispatch(self, event: "ServerEvent") -> Any:
        """Defensively hand ``event`` to the gateway collaborator.

        Calls the first available synchronous gateway method
        (:data:`_DISPATCH_METHODS`) and returns its result; coroutine functions
        are skipped (the async ``send`` path is wired by Task 20.1). Any gateway
        error is logged and swallowed so building/sending one event never breaks
        the caller. Returns ``None`` when there is no usable gateway method.
        """
        gateway = self._gateway
        if gateway is None:
            return None
        for name in self._DISPATCH_METHODS:
            fn = getattr(gateway, name, None)
            if callable(fn) and not inspect.iscoroutinefunction(fn):
                try:
                    return fn(event)
                except Exception:  # pragma: no cover - defensive gateway guard
                    logger.exception(
                        "omnilimb-face Live2DDirector: gateway.%s(%s) failed.",
                        name,
                        type(event).__name__,
                    )
                    return None
        return None


def _get(obj: Any, name: str, default: Any) -> Any:
    """Return attribute ``name`` of ``obj`` (dict key or attribute), else default.

    Duck-typed accessor so :class:`Live2DDirector` can consume either a real
    ``AudioSegmentOut`` dataclass or a plain mapping / stub object in tests.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
