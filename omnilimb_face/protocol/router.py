"""omnilimb_face.protocol.router — pure dispatch of parsed ``/client-ws`` events.

This module implements the design's ``MessageRouter`` (design.md -> Data Models
-> "``MessageRouter`` 与 ``Live2DModelInfo``", Requirement 9.1). Once an inbound
``/client-ws`` message has been parsed into a strongly-typed
:data:`~omnilimb_face.protocol.events.ClientEvent`, the router decides *what the
plugin should do* with it — and nothing more.

Design contract:

* **Pure / no I/O.** :meth:`MessageRouter.route` performs only an in-memory
  mapping from a :data:`~omnilimb_face.protocol.events.ClientEvent` variant to a
  :class:`RouteAction`. It never touches the network, filesystem, audio devices
  or any subsystem; the actual work is performed by whoever consumes the
  returned action (wired by later tasks). This keeps routing trivially
  unit-testable and deterministic.
* **Total over the client union.** Every variant of
  :data:`~omnilimb_face.protocol.events.ClientEvent`
  (:class:`~omnilimb_face.protocol.events.TextInputEvent`,
  :class:`~omnilimb_face.protocol.events.MicAudioDataEvent`,
  :class:`~omnilimb_face.protocol.events.MicAudioEndEvent`,
  :class:`~omnilimb_face.protocol.events.InterruptSignalEvent`,
  :class:`~omnilimb_face.protocol.events.FetchConfigsEvent`,
  :class:`~omnilimb_face.protocol.events.PlaybackCompleteEvent`) maps to exactly
  one :class:`RouteAction`. Routing never raises.
* **Silent-ignore parity.** ``frontend-playback-complete`` is a notification the
  Open-LLM-VTuber backend silently ignores; the router models this with a benign
  action carrying ``ignored=True`` so the dispatch stays *total* (no special
  ``None`` case) while still signalling "do nothing".
* **Resilient over unexpected input.** Rather than raising on an object that is
  not a known :data:`~omnilimb_face.protocol.events.ClientEvent` variant, the
  router returns a benign :data:`RouteKind.NOOP` action with a human-readable
  ``reason``. The gateway only ever hands :meth:`route` a parsed client event,
  but keeping routing *total and non-raising* means a future miswiring can never
  take down the WebSocket connection (mirrors the parser's "connection stays
  usable" guarantee, Requirement 9.5).

The router intentionally carries the originating event on the returned action so
downstream consumers get the typed payload (e.g. the base64 PCM chunk on a
:class:`~omnilimb_face.protocol.events.MicAudioDataEvent`, or the
``at_text_index`` on an
:class:`~omnilimb_face.protocol.events.InterruptSignalEvent`) without re-parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple, Type

from omnilimb_face.protocol.events import (
    ClientEvent,
    FetchConfigsEvent,
    InterruptSignalEvent,
    MicAudioDataEvent,
    MicAudioEndEvent,
    PlaybackCompleteEvent,
    TextInputEvent,
)

__all__ = [
    "RouteKind",
    "RouteAction",
    "MessageRouter",
]


class RouteKind(str, Enum):
    """The set of plugin actions an inbound client event can map to.

    Subclasses :class:`str` so a :class:`RouteKind` *is* its wire-friendly string
    value (e.g. ``RouteKind.TEXT_INPUT == "text_input"`` is ``True``), which keeps
    comparisons and logging ergonomic without giving up enum exhaustiveness.
    """

    #: User typed text in the frontend -> inject as a user utterance / turn.
    INJECT_TEXT = "inject_text"
    #: A base64 PCM chunk from the frontend mic -> feed the capture/VAD pipeline.
    MIC_AUDIO = "mic_audio"
    #: Frontend mic stream finished -> finalize the current audio segment.
    MIC_AUDIO_END = "mic_audio_end"
    #: Barge-in signal -> stop playback/generation (Requirement 5).
    INTERRUPT = "interrupt"
    #: Request for available model/configuration list -> reply with configs.
    FETCH_CONFIGS = "fetch_configs"
    #: Frontend playback finished -> benign, silently ignored (upstream parity).
    PLAYBACK_COMPLETE = "playback_complete"
    #: Nothing to do — an unexpected/unknown object was handed to the router.
    NOOP = "noop"


@dataclass(frozen=True)
class RouteAction:
    """The decision produced by :meth:`MessageRouter.route` for one event.

    Attributes:
        kind: The :class:`RouteKind` describing what the plugin should do.
        event: The originating object handed to the router — for any valid
            :data:`~omnilimb_face.protocol.events.ClientEvent` this is the typed
            event itself, carried verbatim so downstream consumers get the
            payload (text, audio chunk, interrupt index, ...) without re-parsing.
            For the defensive :data:`RouteKind.NOOP` path it is whatever
            unexpected object was supplied (possibly ``None``).
        ignored: ``True`` for benign actions that intentionally do no work:
            ``frontend-playback-complete`` (upstream silent-ignore parity) and
            the :data:`RouteKind.NOOP` fallback.
        reason: Human-readable explanation, populated for the
            :data:`RouteKind.NOOP` fallback to say *why* nothing is done. Empty
            for ordinary actions.
    """

    kind: RouteKind
    event: Any
    ignored: bool = False
    reason: str = ""


class MessageRouter:
    """Maps a parsed :data:`~omnilimb_face.protocol.events.ClientEvent` to a :class:`RouteAction`.

    The mapping is a fixed, in-memory table — :meth:`route` is a pure function of
    its input with no side effects, making it deterministic and trivially
    testable (Requirement 9.1).
    """

    # Static event-class -> (kind, ignored) dispatch table. ``frontend-playback-
    # complete`` is the only benign/ignored mapped action, matching Open-LLM-
    # VTuber's silent-ignore of that notification.
    _DISPATCH: Dict[Type[ClientEvent], Tuple[RouteKind, bool]] = {
        TextInputEvent: (RouteKind.INJECT_TEXT, False),
        MicAudioDataEvent: (RouteKind.MIC_AUDIO, False),
        MicAudioEndEvent: (RouteKind.MIC_AUDIO_END, False),
        InterruptSignalEvent: (RouteKind.INTERRUPT, False),
        FetchConfigsEvent: (RouteKind.FETCH_CONFIGS, False),
        PlaybackCompleteEvent: (RouteKind.PLAYBACK_COMPLETE, True),
    }

    def route(self, event: ClientEvent) -> RouteAction:
        """Dispatch ``event`` to its :class:`RouteAction` (pure, no I/O).

        Total over the :data:`~omnilimb_face.protocol.events.ClientEvent` union:
        every valid client event yields exactly one action.
        ``frontend-playback-complete`` yields a benign ``ignored=True`` action,
        mirroring the upstream silent-ignore behaviour.

        Routing never raises. If handed an object that is *not* a known client
        event (a programming error, since the gateway only ever passes parsed
        client events), :meth:`route` returns a benign :data:`RouteKind.NOOP`
        action whose ``reason`` names the offending type — keeping the gateway
        resilient instead of letting an exception escape.

        Args:
            event: A parsed client event (as produced by the gateway parser).

        Returns:
            The :class:`RouteAction` describing what the plugin should do.
        """
        mapping: Optional[Tuple[RouteKind, bool]] = self._DISPATCH.get(type(event))
        if mapping is None:
            return RouteAction(
                kind=RouteKind.NOOP,
                event=event,
                ignored=True,
                reason=(
                    "unrecognized inbound object of type "
                    f"{type(event).__name__!r}; expected a ClientEvent variant"
                ),
            )
        kind, ignored = mapping
        return RouteAction(kind=kind, event=event, ignored=ignored)
