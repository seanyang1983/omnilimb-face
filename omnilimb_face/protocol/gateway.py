"""``/client-ws`` protocol gateway: pure serialize/parse + error classification.

This module implements :class:`ProtocolGateway`, the component that bridges the
internal, strongly-typed protocol event dataclasses (see
:mod:`omnilimb_face.protocol.events`) and the wire format used by the
Open-LLM-VTuber compatible ``/client-ws`` WebSocket protocol.

Task 2.2 implements **only** the two pure functions and their supporting error
classification:

* :meth:`ProtocolGateway.serialize` — turn any ``ServerEvent``/``ClientEvent``
  dataclass into a single valid JSON string (Requirement 9.1). The ``type``
  discriminant field is always present in the emitted JSON.
* :meth:`ProtocolGateway.parse` — turn an inbound ``str``/``bytes`` message into
  a :class:`~omnilimb_face.protocol.events.ParseOutcome`, classifying every
  failure into exactly one of the four protocol error codes
  (Requirements 9.2/9.3/9.6/9.7) and **never raising** so the connection stays
  usable (Requirement 9.5).

The two functions together satisfy the round-trip contract
(Property 1, Requirement 9.4)::

    parse(serialize(e)) == ParseOutcome(ok=True, event=e)

for every valid protocol event, in both directions.

Task 20.1 builds the WebSocket transport on top of those pure functions: an
asyncio ``websockets`` server bound to ``cfg.host:cfg.port`` at the configured
``ws_path`` (default ``/client-ws``), runnable on its own event loop in a
background daemon thread so the CLI main thread is never blocked (design "进程
与线程模型"). The server enforces the 1 MiB inbound size cap itself (so an
oversize frame yields a ``too_large`` :class:`~omnilimb_face.protocol.events.ErrorEvent`
and the connection stays open, Requirement 9.7) and answers every
non-conforming message with an ``ErrorEvent`` while keeping the connection
usable (Requirement 9.5). It binds loopback (``127.0.0.1``) by default; a
non-loopback bind would need an access-control story before exposure.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import threading
import types
import uuid
from dataclasses import MISSING
from typing import Any, Optional, Union, get_args, get_origin, get_type_hints

import websockets

logger = logging.getLogger(__name__)

from omnilimb_face.protocol.events import (
    MAX_MESSAGE_BYTES,
    AudioEvent,
    ControlEvent,
    ErrorEvent,
    FetchConfigsEvent,
    FullTextEvent,
    InterruptSignalEvent,
    MicAudioDataEvent,
    MicAudioEndEvent,
    ParseOutcome,
    PlaybackCompleteEvent,
    PingEvent,
    PongEvent,
    ProtocolError,
    SetModelEvent,
    TextInputEvent,
)

try:  # ``Literal`` origin sentinel differs slightly across versions.
    from typing import Literal as _Literal
except ImportError:  # pragma: no cover - Literal always present on supported versions.
    _Literal = None  # type: ignore[assignment]

__all__ = ["ProtocolGateway"]


# Every protocol event dataclass, server -> client and client -> server. Used
# to build the ``type`` -> dataclass registry so ``parse`` can reconstruct the
# correct event for both directions (the round-trip property holds for all of
# them, see Property 1 / Task 2.3).
_ALL_EVENT_TYPES: tuple[type, ...] = (
    # Server -> Client (downlink).
    FullTextEvent,
    SetModelEvent,
    AudioEvent,
    ControlEvent,
    ErrorEvent,
    # Client -> Server (uplink).
    TextInputEvent,
    MicAudioDataEvent,
    MicAudioEndEvent,
    InterruptSignalEvent,
    FetchConfigsEvent,
    PlaybackCompleteEvent,
    # Additive (switchable-avatar-renderers R13.5): RTT probe + echo.
    PingEvent,
    PongEvent,
)


def _build_registry() -> dict[str, type]:
    """Map each event's ``type`` discriminant literal to its dataclass."""

    registry: dict[str, type] = {}
    for cls in _ALL_EVENT_TYPES:
        for f in dataclasses.fields(cls):
            if f.name == "type":
                # The ``type`` field always carries a literal string default.
                registry[f.default] = cls
                break
    return registry


# ``type`` discriminant -> dataclass, plus the precomputed field metadata and
# resolved type hints. Precomputing keeps ``parse`` deterministic and well
# within the 500 ms parse budget (Requirement 9.2).
_TYPE_REGISTRY: dict[str, type] = _build_registry()
_FIELD_INFO: dict[type, dict[str, dataclasses.Field]] = {
    cls: {f.name: f for f in dataclasses.fields(cls)} for cls in _ALL_EVENT_TYPES
}
_FIELD_HINTS: dict[type, dict[str, Any]] = {
    cls: get_type_hints(cls) for cls in _ALL_EVENT_TYPES
}

# Union origins that may appear in the resolved type hints (``Optional[X]`` and
# ``Union[...]`` resolve to ``typing.Union``; PEP 604 ``X | Y`` resolves to
# ``types.UnionType``).
_UNION_ORIGINS: tuple[Any, ...] = (Union,)
if hasattr(types, "UnionType"):  # Python 3.10+
    _UNION_ORIGINS = (Union, types.UnionType)


def _check_type(value: Any, hint: Any) -> bool:
    """Return ``True`` when ``value`` conforms to the annotation ``hint``.

    Handles the annotation forms used by the protocol events: plain ``str`` /
    ``int`` / ``float`` / ``bool`` / ``dict`` / ``list``, ``Optional[X]`` /
    ``Union[...]``, ``Literal[...]`` (value membership), and parametrized
    ``list[X]`` (recursively checks elements). ``bool`` is treated as distinct
    from ``int`` even though it is a subclass, so a JSON boolean never passes
    where an integer is required (and vice versa).
    """

    if hint is Any:
        return True

    origin = get_origin(hint)

    if origin is None:
        # Plain (non-parametrized) type.
        if hint is bool:
            return isinstance(value, bool)
        if hint is int:
            return isinstance(value, int) and not isinstance(value, bool)
        if hint is float:
            # JSON does not distinguish int from float; accept integral numbers
            # for float fields, but never accept booleans.
            return (isinstance(value, float) or isinstance(value, int)) and not isinstance(
                value, bool
            )
        if hint is str:
            return isinstance(value, str)
        if hint is dict:
            return isinstance(value, dict)
        if hint is list:
            return isinstance(value, list)
        if hint is type(None):
            return value is None
        try:
            return isinstance(value, hint)
        except TypeError:
            return False

    if origin in _UNION_ORIGINS:
        return any(_check_type(value, arg) for arg in get_args(hint))

    if _Literal is not None and origin is _Literal:
        return value in get_args(hint)

    if origin is list:
        if not isinstance(value, list):
            return False
        args = get_args(hint)
        if not args:
            return True
        return all(_check_type(item, args[0]) for item in value)

    if origin is dict:
        return isinstance(value, dict)

    # Fallback for any other parametrized generic: validate the origin only.
    try:
        return isinstance(value, origin)
    except TypeError:
        return False


def _reconstruct(cls: type, data: dict) -> Optional[object]:
    """Validate ``data`` against ``cls``'s schema and rebuild the dataclass.

    Returns the reconstructed (frozen) dataclass instance on success, or
    ``None`` when the payload does not conform to the event schema (missing
    required fields, wrong types, or unknown extra keys) — the caller maps that
    to the ``schema_invalid`` error code.

    Only fields present in ``data`` are passed to the constructor; absent
    optional fields fall back to their dataclass defaults. Because
    :meth:`ProtocolGateway.serialize` always emits every field, the
    reconstructed instance compares equal to the original event, including
    defaults such as an empty ``volumes`` list or ``actions=None``.
    """

    field_info = _FIELD_INFO[cls]
    hints = _FIELD_HINTS[cls]

    # Reject unknown extra keys: a conforming payload only carries the event's
    # own fields.
    for key in data:
        if key not in field_info:
            return None

    kwargs: dict[str, Any] = {}
    for name, field_obj in field_info.items():
        if name in data:
            if not _check_type(data[name], hints[name]):
                return None
            kwargs[name] = data[name]
        else:
            required = (
                field_obj.default is MISSING and field_obj.default_factory is MISSING
            )
            if required:
                return None
            # Leave the field to its dataclass default / default_factory.

    try:
        return cls(**kwargs)
    except Exception:
        # Construction must never propagate an exception out of ``parse``.
        return None


class ProtocolGateway:
    """Serialize/parse ``/client-ws`` protocol events with error classification.

    Both constructor arguments are optional so the pure serialize/parse logic is
    usable (and unit-testable) without standing up the WebSocket server:

    * ``cfg`` — a ``ProtocolSettings``-like object; the pure functions read only
      its ``max_message_bytes`` attribute, while the WebSocket server (Task 20.1)
      additionally reads ``host`` / ``port`` / ``ws_path``. When ``cfg`` is
      ``None`` the inbound size limit defaults to
      :data:`omnilimb_face.protocol.events.MAX_MESSAGE_BYTES` (1 MiB) and the
      transport defaults to ``127.0.0.1:12393`` at ``/client-ws``.
    * ``router`` — the :class:`~omnilimb_face.protocol.router.MessageRouter` used
      by the WebSocket server; when set, every parsed inbound event is dispatched
      through ``router.route(event)``.
    * ``on_event`` — optional callback invoked for each successfully parsed
      inbound event as ``on_event(event, action, client_uid)`` (``action`` is the
      router's :class:`~omnilimb_face.protocol.router.RouteAction` or ``None``
      when no router is set). May be a plain function or a coroutine function;
      coroutines are awaited. Exceptions raised by the callback are logged and
      swallowed so one bad handler never tears down the connection.
    """

    #: Transport defaults used when ``cfg`` does not provide them.
    _DEFAULT_HOST = "127.0.0.1"
    _DEFAULT_PORT = 12393
    _DEFAULT_WS_PATH = "/client-ws"

    def __init__(
        self,
        cfg: Any = None,
        router: Any = None,
        on_event: Any = None,
        ssl_context: Any = None,
        static_dir: Any = None,
    ) -> None:
        self._cfg = cfg
        self._router = router
        self._on_event = on_event
        # Optional ssl.SSLContext -> serve `wss://` instead of `ws://`. Must pair
        # with the front-end being served over HTTPS (same cert), so a LAN phone
        # has a secure context for both the page and the WebSocket.
        self._ssl_context = ssl_context
        # Optional single-port mode: when set to a directory, non-WS GET requests
        # are answered by serving static files from it (via websockets'
        # process_request hook), so the page AND /client-ws share ONE origin/port.
        # This means a single TLS cert / single firewall port / single tunnel,
        # and the page connects to its OWN origin for the WebSocket (no second
        # cert to accept). The served index.html is tagged so the front-end uses
        # a same-origin ws URL. None -> classic WS-only gateway (two-port setup).
        from pathlib import Path as _Path
        self._static_dir = _Path(static_dir) if static_dir is not None else None
        if cfg is None:
            self._max_message_bytes = MAX_MESSAGE_BYTES
            self._host = self._DEFAULT_HOST
            self._port = self._DEFAULT_PORT
            self._ws_path = self._DEFAULT_WS_PATH
        else:
            self._max_message_bytes = getattr(
                cfg, "max_message_bytes", MAX_MESSAGE_BYTES
            )
            self._host = getattr(cfg, "host", self._DEFAULT_HOST)
            self._port = getattr(cfg, "port", self._DEFAULT_PORT)
            self._ws_path = getattr(cfg, "ws_path", self._DEFAULT_WS_PATH)

        # --- WebSocket server runtime state (Task 20.1) -------------------
        # All of these are populated when the server starts and cleared on stop.
        self._server: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._bound_host: Optional[str] = None
        self._bound_port: Optional[int] = None
        # client_uid -> connected WebSocket protocol. Mutated only on the
        # server's event loop thread (connect/disconnect), so no extra lock is
        # needed for the server's own use; snapshots are taken for broadcast.
        self._clients: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Pure serialize / parse (Task 2.2).
    # ------------------------------------------------------------------
    def serialize(self, event: Any) -> str:
        """Serialize a protocol event dataclass to a single JSON string.

        Works for any ``ServerEvent``/``ClientEvent`` variant. The discriminant
        ``type`` field is always present in the emitted JSON (Requirement 9.1).
        """

        payload = dataclasses.asdict(event)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def parse(self, raw: Union[str, bytes]) -> ParseOutcome:
        """Parse an inbound message into a :class:`ParseOutcome` (never raises).

        Error classification, in order (Requirements 9.2/9.3/9.6/9.7):

        1. Size in **bytes** exceeds the configured limit (1 MiB) -> ``too_large``.
           The size is checked first, before any decoding/parsing.
        2. Not valid JSON (or not valid UTF-8) -> ``invalid_json``.
        3. Valid JSON but missing / non-``str`` ``type``, or a ``type`` that is
           not a known protocol message type -> ``unsupported_type``.
        4. Known ``type`` but the fields do not conform to that event's schema
           (missing required fields, wrong types, unknown extra keys) ->
           ``schema_invalid``.

        On success returns ``ParseOutcome(ok=True, event=<dataclass>)`` with the
        reconstructed event; on every failure returns
        ``ParseOutcome(ok=False, event=None, error=ProtocolError(...))``. The
        connection therefore stays usable for subsequent messages
        (Requirement 9.5).
        """

        try:
            # 1) Size check FIRST, measured in UTF-8 bytes.
            if isinstance(raw, (bytes, bytearray)):
                size = len(raw)
            elif isinstance(raw, str):
                size = len(raw.encode("utf-8"))
            else:
                return ParseOutcome(
                    ok=False,
                    error=ProtocolError(
                        "invalid_json",
                        f"unsupported raw message type: {type(raw).__name__}",
                    ),
                )

            if size > self._max_message_bytes:
                return ParseOutcome(
                    ok=False,
                    error=ProtocolError(
                        "too_large",
                        f"message size {size} bytes exceeds limit "
                        f"{self._max_message_bytes} bytes",
                    ),
                )

            # 2) Decode + JSON parse.
            try:
                if isinstance(raw, (bytes, bytearray)):
                    text = bytes(raw).decode("utf-8")
                else:
                    text = raw
                parsed = json.loads(text)
            except (ValueError, UnicodeDecodeError) as exc:
                return ParseOutcome(
                    ok=False,
                    error=ProtocolError("invalid_json", f"invalid JSON: {exc}"),
                )

            # 3) Discriminant ``type`` must be present, a string, and known.
            if not isinstance(parsed, dict):
                return ParseOutcome(
                    ok=False,
                    error=ProtocolError(
                        "unsupported_type",
                        "message is not a JSON object with a 'type' field",
                    ),
                )

            msg_type = parsed.get("type")
            if not isinstance(msg_type, str) or msg_type not in _TYPE_REGISTRY:
                return ParseOutcome(
                    ok=False,
                    error=ProtocolError(
                        "unsupported_type",
                        f"unknown or missing message type: {msg_type!r}",
                    ),
                )

            # 4) Validate against the event schema and reconstruct.
            cls = _TYPE_REGISTRY[msg_type]
            event = _reconstruct(cls, parsed)
            if event is None:
                return ParseOutcome(
                    ok=False,
                    error=ProtocolError(
                        "schema_invalid",
                        f"payload does not conform to '{msg_type}' schema",
                    ),
                )

            return ParseOutcome(ok=True, event=event)
        except Exception as exc:  # pragma: no cover - defensive: never raise.
            # parse() must never raise; classify any unexpected failure rather
            # than letting it escape and tear down the connection.
            return ParseOutcome(
                ok=False,
                error=ProtocolError("invalid_json", f"unexpected parse error: {exc}"),
            )

    # ------------------------------------------------------------------
    # WebSocket server (Task 20.1).
    # ------------------------------------------------------------------
    #
    # Lifecycle overview
    # ------------------
    # ``serve()`` is the long-running coroutine: it binds the ``websockets``
    # server, records the (possibly ephemeral) bound address, signals readiness
    # and then awaits an internal stop event before tearing the server down
    # cleanly (closing all client connections). It can be awaited directly on an
    # existing loop, or driven by the sync :meth:`start_in_thread` /
    # :meth:`stop` helpers which own a private event loop on a daemon thread so
    # the CLI main thread is never blocked (design "进程与线程模型").

    def set_on_event(self, callback: Any) -> None:
        """Register/replace the per-inbound-event callback (see class docs)."""
        self._on_event = callback

    # ------------------------------------------------------------------
    # Single-port static serving (optional; see __init__ static_dir).
    # ------------------------------------------------------------------
    async def _process_request_static(self, path: str, request_headers: Any):
        """websockets ``process_request`` hook: serve static files for non-WS GETs.

        Returning ``None`` lets the request proceed to the WebSocket handshake
        (used for ``ws_path``); returning an ``(status, headers, body)`` tuple
        answers the request as plain HTTP. This is how the page and the
        ``/client-ws`` endpoint share a single port/origin (single-port mode):
        any non-``ws_path`` path is served from ``self._static_dir``. The served
        ``index.html`` is tagged with ``window.__VTUBER_SINGLE_PORT__`` so the
        front-end connects to its OWN origin for the WebSocket.
        """
        import http
        import mimetypes
        import posixpath
        import urllib.parse

        raw = (path or "/").split("?", 1)[0].split("#", 1)[0]
        # Proceed to the WS upgrade for the configured ws path.
        if raw == self._ws_path:
            return None

        base = self._static_dir
        if base is None:
            return (http.HTTPStatus.NOT_FOUND, [], b"404 Not Found")

        rel = urllib.parse.unquote(raw)
        if rel in ("", "/"):
            rel = "/index.html"
        elif rel.endswith("/"):
            rel = rel + "index.html"
        # Normalise + prevent path traversal outside the static dir.
        rel = posixpath.normpath(rel).lstrip("/")
        try:
            base_resolved = base.resolve()
            target = (base_resolved / rel).resolve()
            target.relative_to(base_resolved)
        except Exception:
            return (http.HTTPStatus.FORBIDDEN, [("Content-Type", "text/plain")], b"403")
        if not target.is_file():
            return (
                http.HTTPStatus.NOT_FOUND,
                [("Content-Type", "text/plain; charset=utf-8")],
                b"404 Not Found",
            )

        try:
            body = target.read_bytes()
        except Exception:
            return (http.HTTPStatus.INTERNAL_SERVER_ERROR, [], b"500")

        suffix = target.suffix.lower()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif suffix == ".html":
            ctype = "text/html; charset=utf-8"
            # Tag the page so the front-end uses a SAME-ORIGIN WebSocket URL
            # (single port -> one cert, one tunnel, no second cert to accept).
            text = body.decode("utf-8", "replace")
            inject = "<script>window.__VTUBER_SINGLE_PORT__=true;</script>"
            if "</head>" in text:
                text = text.replace("</head>", inject + "</head>", 1)
            else:
                text = inject + text
            body = text.encode("utf-8")
        elif suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif suffix == ".json":
            ctype = "application/json; charset=utf-8"

        headers = [
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store, must-revalidate"),
        ]
        import http as _http
        return (_http.HTTPStatus.OK, headers, body)

    @property
    def bound_host(self) -> Optional[str]:
        """The host the server actually bound to (``None`` until started)."""
        return self._bound_host

    @property
    def bound_port(self) -> Optional[int]:
        """The port the server actually bound to (resolves ``port=0`` to the
        ephemeral port the OS assigned). ``None`` until the server is started.
        """
        return self._bound_port

    def is_running(self) -> bool:
        """Return ``True`` while the WebSocket server is bound and serving."""
        return self._server is not None and self._ready.is_set()

    @property
    def client_count(self) -> int:
        """Number of currently connected clients."""
        return len(self._clients)

    async def serve(self) -> None:
        """Start the ``/client-ws`` server and run until :meth:`shutdown`.

        Binds ``websockets`` to ``host:port`` and serves the configured
        ``ws_path``. The built-in per-message size cap is disabled
        (``max_size=None``) so an oversize frame is delivered to the handler and
        classified as ``too_large`` by :meth:`parse` — the gateway then answers
        with an ``ErrorEvent`` and keeps the connection open (Requirement 9.7)
        instead of letting the transport close it. Loopback-only by default, so
        accepting an oversize frame before rejecting it is a bounded, local risk.

        The coroutine returns only after :meth:`shutdown` (or :meth:`stop`) is
        invoked, at which point the server and all client connections are closed
        cleanly via the ``websockets`` async context manager.
        """
        loop = asyncio.get_event_loop()
        self._loop = loop
        self._stop_event = asyncio.Event()

        async with websockets.serve(
            self._connection_handler,
            self._host,
            self._port,
            # Disable the transport-level size cap; we enforce the 1 MiB limit
            # ourselves so oversize messages keep the connection open (需求 9.7).
            max_size=None,
            # ``ping_interval=None`` keeps idle test connections from being
            # closed by keepalive timeouts during slow assertions.
            ping_interval=None,
            # Optional TLS -> serve `wss://` (paired with HTTPS front-end).
            ssl=self._ssl_context,
            # Optional single-port static serving (None disables -> WS-only).
            process_request=(
                self._process_request_static if self._static_dir is not None else None
            ),
        ) as server:
            self._server = server
            sock = server.sockets[0] if server.sockets else None
            if sock is not None:
                addr = sock.getsockname()
                self._bound_host, self._bound_port = addr[0], addr[1]
            logger.info(
                "omnilimb-face Protocol_Gateway serving ws://%s:%s%s",
                self._bound_host,
                self._bound_port,
                self._ws_path,
            )
            # Signal sync waiters (start_in_thread) that the bind is complete.
            self._ready.set()
            try:
                await self._stop_event.wait()
            finally:
                self._ready.clear()
        # Exiting the context manager closes the server and all connections.
        self._server = None
        self._clients.clear()
        logger.info("omnilimb-face Protocol_Gateway stopped.")

    async def shutdown(self) -> None:
        """Signal the running :meth:`serve` loop to stop and close connections.

        Safe to call from within the server's event loop. Setting the stop event
        causes :meth:`serve` to exit its ``websockets.serve`` context manager,
        which closes the listening socket and every client connection cleanly.
        From another thread use :meth:`stop` (which schedules this safely).
        """
        if self._stop_event is not None:
            self._stop_event.set()

    async def send(self, client_uid: str, event: Any) -> bool:
        """Serialize ``event`` and send it to the client identified by
        ``client_uid``.

        Returns ``True`` when the client was connected and the send was
        attempted, ``False`` when no such client is connected. A closed
        connection is treated as a no-op (logged, returns ``False``).
        """
        ws = self._clients.get(client_uid)
        if ws is None:
            return False
        data = self.serialize(event)
        try:
            await ws.send(data)
            return True
        except websockets.ConnectionClosed:
            self._clients.pop(client_uid, None)
            return False

    async def broadcast(self, event: Any) -> int:
        """Serialize ``event`` once and send it to every connected client.

        Used to push audio / expression driving events to all connected
        frontends. Returns the number of clients the event was delivered to;
        connections that error out are dropped and not counted.
        """
        if not self._clients:
            return 0
        data = self.serialize(event)
        targets = list(self._clients.items())
        results = await asyncio.gather(
            *(ws.send(data) for _uid, ws in targets),
            return_exceptions=True,
        )
        delivered = 0
        for (uid, _ws), result in zip(targets, results):
            if isinstance(result, Exception):
                # Drop clients whose send failed (e.g. already closed).
                self._clients.pop(uid, None)
            else:
                delivered += 1
        return delivered

    # --- Synchronous, thread-managed lifecycle ------------------------------
    def start_in_thread(self, ready_timeout: float = 5.0) -> threading.Thread:
        """Start the server on a private event loop in a daemon thread.

        Returns the daemon :class:`threading.Thread` running the server. Blocks
        until the server has bound (or ``ready_timeout`` seconds elapse, in which
        case a :class:`RuntimeError` is raised). After this returns,
        :attr:`bound_host` / :attr:`bound_port` are populated (resolving an
        ephemeral ``port=0``) and the server is ready to accept connections.
        Calling it again while already running returns the existing thread.
        """
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        self._ready.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="omnilimb-face-ws",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(ready_timeout):
            # Best-effort cleanup before surfacing the failure.
            self.stop(timeout=ready_timeout)
            raise RuntimeError(
                "omnilimb-face Protocol_Gateway failed to start within "
                f"{ready_timeout}s."
            )
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        """Stop a server started with :meth:`start_in_thread` and join its thread.

        Schedules :meth:`shutdown` on the server's loop (thread-safe), waits for
        the serve loop to finish, then closes the loop. Idempotent: a no-op when
        the server is not running.
        """
        loop = self._loop
        thread = self._thread
        if loop is not None and self._stop_event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                # Loop already stopped/closed — nothing to signal.
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._ready.clear()

    def send_threadsafe(
        self, client_uid: str, event: Any, timeout: Optional[float] = 5.0
    ) -> bool:
        """Thread-safe :meth:`send` for callers off the server's event loop.

        Schedules the coroutine on the server's loop and blocks for its result
        (up to ``timeout`` seconds). Returns ``False`` when the server is not
        running.
        """
        return bool(self._run_coroutine_threadsafe(self.send(client_uid, event), timeout))

    def broadcast_threadsafe(
        self, event: Any, timeout: Optional[float] = 5.0
    ) -> int:
        """Thread-safe :meth:`broadcast` for callers off the server's event loop.

        Used by the playback / expression threads to push driving events to all
        connected frontends. Returns the number of clients reached, or ``0`` when
        the server is not running.
        """
        result = self._run_coroutine_threadsafe(self.broadcast(event), timeout)
        return int(result) if result is not None else 0

    # --- Internals ----------------------------------------------------------
    def _thread_main(self) -> None:
        """Daemon-thread entry point: own the loop, run :meth:`serve`, clean up."""
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.serve())
        except Exception:  # pragma: no cover - defensive: log and unblock waiters
            logger.exception("omnilimb-face Protocol_Gateway serve loop crashed.")
            # Ensure a failed start does not leave start_in_thread blocked.
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()

    def _run_coroutine_threadsafe(self, coro, timeout: Optional[float]) -> Any:
        """Run ``coro`` on the server's loop from another thread; return result.

        Returns ``None`` (after closing the coroutine) when the server loop is
        not available/running.
        """
        loop = self._loop
        if loop is None or loop.is_closed() or not self.is_running():
            coro.close()
            return None
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout)

    @staticmethod
    def _extract_request_path(websocket: Any) -> Optional[str]:
        """Read the request target path across websockets versions.

        websockets >= 13 (the new asyncio implementation, and the default in
        14/15) exposes the handshake path at ``websocket.request.path``; the
        legacy implementation exposed ``websocket.path`` directly. Try the new
        location first, then fall back, so the gateway works whether the host
        ships websockets 12.x or 15.x.
        """
        request = getattr(websocket, "request", None)
        if request is not None:
            path = getattr(request, "path", None)
            if isinstance(path, str):
                return path
        legacy = getattr(websocket, "path", None)
        return legacy if isinstance(legacy, str) else None

    def _path_matches(self, raw_path: Optional[str]) -> bool:
        """Return ``True`` when ``raw_path`` targets the configured ``ws_path``.

        Compares only the path component (query string stripped) and ignores a
        trailing slash, so ``/client-ws`` and ``/client-ws/`` both match.
        """
        if not self._ws_path:
            return True
        if raw_path is None:
            return False
        path = raw_path.split("?", 1)[0]
        return path.rstrip("/") == self._ws_path.rstrip("/")

    async def _connection_handler(self, websocket: Any) -> None:
        """Per-connection coroutine: validate path, track client, pump messages.

        Connections to any path other than the configured ``ws_path`` are closed
        with policy-violation code 1008. Each accepted client is tracked under a
        generated ``client_uid`` for the duration of the connection; inbound
        messages are handled one at a time and any non-conforming message is
        answered with an ``ErrorEvent`` without closing the connection
        (Requirements 9.5 / 9.7).
        """
        raw_path = self._extract_request_path(websocket)
        if not self._path_matches(raw_path):
            await websocket.close(code=1008, reason="unknown path")
            return

        client_uid = uuid.uuid4().hex
        self._clients[client_uid] = websocket
        logger.debug("omnilimb-face client connected: %s", client_uid)
        try:
            async for raw in websocket:
                await self._handle_inbound(client_uid, websocket, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.pop(client_uid, None)
            logger.debug("omnilimb-face client disconnected: %s", client_uid)

    async def _handle_inbound(
        self, client_uid: str, websocket: Any, raw: Any
    ) -> None:
        """Parse one inbound message and route it / answer with an error.

        On a parse failure (oversize, invalid JSON, unknown type, schema invalid)
        an :class:`~omnilimb_face.protocol.events.ErrorEvent` carrying the
        classified code/reason is sent back to *this* client and the connection
        is left open (Requirements 9.5 / 9.7). On success the event is routed
        through the configured router (if any) and handed to the ``on_event``
        callback (if any).
        """
        outcome = self.parse(raw)
        if not outcome.ok:
            error = outcome.error
            assert error is not None  # parse() always sets error when not ok.
            try:
                await websocket.send(
                    self.serialize(ErrorEvent(code=error.code, reason=error.reason))
                )
            except websockets.ConnectionClosed:
                return
            return

        event = outcome.event

        action = None
        if self._router is not None:
            try:
                action = self._router.route(event)
            except Exception:  # pragma: no cover - router is pure/total today
                logger.exception(
                    "omnilimb-face router.route failed for %s",
                    type(event).__name__,
                )

        callback = self._on_event
        if callback is not None:
            try:
                result = callback(event, action, client_uid)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "omnilimb-face on_event callback failed for %s",
                    type(event).__name__,
                )
