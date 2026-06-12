"""``/client-ws`` protocol event data models (Requirement 9.1).

This module defines the internal, strongly-typed representation of every
message exchanged over the Open-LLM-VTuber compatible ``/client-ws`` WebSocket
protocol. Each protocol message is a single JSON object whose ``type`` field
acts as the discriminant (matching Open-LLM-VTuber's
``WebSocketHandler._route_message``). Events are modelled as frozen
dataclasses so they are immutable, value-comparable and safe to share across
the gateway's threads.

The shapes here are the single source of truth for serialization/parsing in
``omnilimb_face.protocol.gateway`` (Task 2.2). Because every dataclass carries
its discriminant ``type`` as a literal default and uses the exact field
names/types/defaults from the design, the round-trip contract

    parse(serialize(e)) == ParseOutcome(ok=True, event=e)

(Property 1, Requirement 9.4) and the parse error model (Property 2,
Requirements 9.3/9.6/9.7) can be implemented purely on top of these models.

Protocol facts are derived from the Open-LLM-VTuber sources
(``websocket_handler.py``, ``utils/stream_audio.py``, ``agent/output_types.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Union

__all__ = [
    # Server -> Client
    "FullTextEvent",
    "SetModelEvent",
    "AudioEvent",
    "ControlEvent",
    "ErrorEvent",
    "ServerEvent",
    # Client -> Server
    "TextInputEvent",
    "MicAudioDataEvent",
    "MicAudioEndEvent",
    "InterruptSignalEvent",
    "FetchConfigsEvent",
    "PlaybackCompleteEvent",
    "PingEvent",
    "PongEvent",
    "ClientEvent",
    # Parse result / error model
    "ProtocolError",
    "ParseOutcome",
    "ErrorCode",
    "MAX_MESSAGE_BYTES",
]

# Shared discriminant for the four protocol error classifications used by both
# the inbound ``ErrorEvent`` sent to the frontend and the internal
# ``ProtocolError`` returned by the gateway parser (Requirements 9.3/9.6/9.7).
ErrorCode = Literal["invalid_json", "schema_invalid", "unsupported_type", "too_large"]


# ---------------------------------------------------------------------------
# Server -> Client (downlink) events.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FullTextEvent:
    """Connection-confirmation / status text pushed to the frontend."""

    text: str
    type: Literal["full-text"] = "full-text"


@dataclass(frozen=True)
class SetModelEvent:
    """Live2D model information and configuration identity (Requirement 7.1)."""

    model_info: dict  # Live2D model info (includes emotionMap, url, ...).
    conf_name: str = ""
    conf_uid: str = ""
    type: Literal["set-model-and-conf"] = "set-model-and-conf"


@dataclass(frozen=True)
class AudioEvent:
    """Audio payload that drives playback, lip-sync and expressions.

    Mirrors Open-LLM-VTuber's ``prepare_audio_payload``. ``volumes`` is the
    chunked, normalized RMS sequence the frontend uses for lip-sync
    (Requirement 7.3); ``actions.expressions`` carries the expression index
    sequence (Requirement 8). ``audio`` may be ``None`` to drive
    expressions/mouth only.
    """

    audio: Optional[str]  # base64 WAV; may be null (expression/lip-sync only).
    volumes: list[float] = field(default_factory=list)  # chunked normalized RMS.
    slice_length: int = 0  # milliseconds covered by each volume sample.
    display_text: dict = field(default_factory=dict)  # {"text", "name", ...}.
    actions: Optional[dict] = None  # {"expressions": [int, ...]}.
    forwarded: bool = False
    type: Literal["audio"] = "audio"


@dataclass(frozen=True)
class ControlEvent:
    """Control signal that toggles frontend behaviour (mic, chain, mouth)."""

    text: Literal[
        "start-mic",
        "stop-mic",
        "mic-audio-end",
        "conversation-chain-start",
        "conversation-chain-end",
        "interrupt",
        "mouth-reset",
    ]
    type: Literal["control"] = "control"


@dataclass(frozen=True)
class ErrorEvent:
    """Error response returned to the sender of a non-conforming message.

    ``code`` is one of the four protocol error classifications; ``reason``
    carries the human-readable validation detail (Requirements 9.3/9.6/9.7).
    """

    code: ErrorCode
    reason: str
    type: Literal["error"] = "error"


# Discriminated union of all server -> client events.
ServerEvent = Union[
    FullTextEvent,
    SetModelEvent,
    AudioEvent,
    ControlEvent,
    ErrorEvent,
]


# ---------------------------------------------------------------------------
# Client -> Server (uplink) events.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TextInputEvent:
    """User text typed in the frontend input box."""

    text: str
    type: Literal["text-input"] = "text-input"


@dataclass(frozen=True)
class MicAudioDataEvent:
    """A base64-encoded PCM chunk forwarded from the frontend microphone."""

    audio: str  # base64 PCM chunk.
    sample_rate: int = 16000
    type: Literal["mic-audio-data"] = "mic-audio-data"


@dataclass(frozen=True)
class MicAudioEndEvent:
    """Marks the end of a frontend microphone audio stream."""

    type: Literal["mic-audio-end"] = "mic-audio-end"


@dataclass(frozen=True)
class InterruptSignalEvent:
    """Frontend-initiated barge-in signal (Requirement 5)."""

    at_text_index: int = 0  # text position already played (context truncation).
    type: Literal["interrupt-signal"] = "interrupt-signal"


@dataclass(frozen=True)
class FetchConfigsEvent:
    """Request for the available model/configuration list."""

    type: Literal["fetch-configs"] = "fetch-configs"


@dataclass(frozen=True)
class PlaybackCompleteEvent:
    """Frontend playback-finished notification (silently ignored upstream)."""

    type: Literal["frontend-playback-complete"] = "frontend-playback-complete"


@dataclass(frozen=True)
class PingEvent:
    """Lightweight RTT probe (additive — switchable-avatar-renderers R13.5).

    Carries the sender's timestamp ``t`` (epoch ms) so the peer can echo it back
    in a :class:`PongEvent` for round-trip latency measurement. Additive and
    forward-compatible: peers that do not understand it ignore it.
    """

    type: Literal["ping"] = "ping"
    t: Optional[int] = None


@dataclass(frozen=True)
class PongEvent:
    """RTT probe echo (additive). Echoes the originating ping's ``t``."""

    type: Literal["pong"] = "pong"
    t: Optional[int] = None


# Discriminated union of all client -> server events.
ClientEvent = Union[
    TextInputEvent,
    MicAudioDataEvent,
    MicAudioEndEvent,
    InterruptSignalEvent,
    FetchConfigsEvent,
    PlaybackCompleteEvent,
    PingEvent,
    PongEvent,
]


# ---------------------------------------------------------------------------
# Parse result and error model (Requirements 9.2/9.3/9.5/9.6/9.7).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProtocolError:
    """Internal parse error describing why an inbound message was rejected."""

    code: ErrorCode
    reason: str


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing an inbound ``/client-ws`` message.

    On success ``ok`` is ``True`` and ``event`` holds the internal
    :data:`ClientEvent`; on failure ``ok`` is ``False`` and ``error`` describes
    the classification. Either way the gateway never raises, so the WebSocket
    connection stays usable for subsequent messages (Requirement 9.5).
    """

    ok: bool
    event: Optional[ClientEvent] = None
    error: Optional[ProtocolError] = None


# Maximum accepted inbound message size: 1 MiB (Requirements 9.2 / 9.7).
MAX_MESSAGE_BYTES: int = 1_048_576
