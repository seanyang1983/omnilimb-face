"""omnilimb_face.frontend_server — front-end static-asset hosting.

This module implements the **Frontend Static Server** component from the design
(design.md "Architecture" → ``FS["Frontend Static Server"]``, decision 2
"复用兼容前端做渲染"). It hosts the Open-LLM-VTuber–compatible front-end so the
desktop window / browser can load the Live2D rendering assets and connect back
to the plugin's ``/client-ws`` gateway (Requirement 7.1).

Dependency choice (Requirement 12 degradation)
----------------------------------------------
HTTP-serving frameworks (``starlette`` / ``uvicorn``) live in the OPTIONAL
``[live2d]`` extra and are **not** part of the core install. To let the core
install serve assets *without* any extra dependency, this server is built on the
**standard library** ``http.server`` — specifically
:class:`http.server.ThreadingHTTPServer` driving
:class:`http.server.SimpleHTTPRequestHandler`, run on a background daemon
thread so it never blocks the CLI main thread.

Even though the stdlib modules are effectively always importable on CPython, the
import is still **guarded**: if the serving capability were ever unavailable the
module must still import cleanly and the server reports a degraded / unavailable
state (consistent with Requirement 12) rather than raising at import time.

Networking / security
---------------------
The server binds to ``127.0.0.1`` (loopback) by default, reusing
:class:`~omnilimb_face.config.ProtocolSettings`'s ``host``. Because it is a
**localhost-bound** asset server, no authentication is required (only processes
on the same machine can reach it). If it were ever bound to a non-loopback host
that would expose the assets to the network and would require adding access
control (auth / origin checks) before doing so.

Port selection
--------------
The ``/client-ws`` :class:`ProtocolGateway` already uses
``ProtocolSettings.port`` (default ``12393``). The static-asset server is a
*separate* listener, so by default it serves on the **same host** but a
**distinct port** (``ProtocolSettings.port + 1``) to avoid colliding with the
WebSocket gateway. A port of ``0`` requests an ephemeral port from the OS (used
by tests); the actually-bound port is then discoverable via :attr:`base_url`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# --- Guarded import of the stdlib serving capability (Requirement 12) -------
#
# A stdlib ``http.server`` implementation is preferred precisely because it
# needs no optional ``[live2d]`` dependency. The import is wrapped defensively
# so that, should the serving capability ever be missing, this module still
# imports cleanly and the server degrades instead of crashing at import time.
try:  # pragma: no branch - exercised by the import either succeeding or not
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    _SERVE_IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover - stdlib http.server should import
    SimpleHTTPRequestHandler = None  # type: ignore[assignment,misc]
    ThreadingHTTPServer = None  # type: ignore[assignment,misc]
    _SERVE_IMPORT_ERROR = exc


# Default loopback host and base port mirror ``ProtocolSettings`` so this module
# has sane fallbacks without importing the config module at runtime.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PROTOCOL_PORT = 12393


def default_frontend_dir() -> Path:
    """Return the bundled ``frontend/`` asset directory.

    The front-end assets are shipped as **package data inside the
    ``omnilimb_face`` package** (``omnilimb_face/frontend/``) so a pip-installed
    wheel carries the avatar UI. This resolver therefore prefers the in-package
    location (``<this file's dir>/frontend``).

    For backward compatibility with an older repo-root layout (and any external
    caller that kept assets one level up), it falls back to
    ``<project root>/frontend`` when the in-package directory is absent.
    """
    in_package = Path(__file__).resolve().parent / "frontend"
    if in_package.is_dir():
        return in_package
    return Path(__file__).resolve().parent.parent / "frontend"


@dataclass(frozen=True)
class FrontendServerStatus:
    """Snapshot of the front-end static server's state.

    Attributes:
        available: Whether the underlying serving capability could be loaded.
            ``False`` means the (guarded) stdlib import failed and the server is
            degraded/unavailable (Requirement 12).
        running: Whether the background HTTP server is currently serving.
        base_url: The base URL assets are served from (e.g.
            ``http://127.0.0.1:12394``) when running, else ``None``.
        message: Human-readable status / reason, useful for ``vtuber doctor``.
    """

    available: bool
    running: bool
    base_url: Optional[str]
    message: str


class _QuietHTTPRequestHandler(SimpleHTTPRequestHandler if SimpleHTTPRequestHandler else object):  # type: ignore[misc]
    """``SimpleHTTPRequestHandler`` that routes access logs to ``logging``.

    The stock handler writes one line per request to ``stderr``; that would
    clutter the CLI. Here those messages are demoted to ``logger.debug`` so the
    asset server stays quiet during normal operation.
    """

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("frontend-asset %s - %s", self.address_string(), format % args)

    def end_headers(self) -> None:  # noqa: D401
        """Send no-store cache headers so a browser refresh always reloads.

        This is a localhost dev/asset server, so disabling caching costs nothing
        and avoids the classic "edited the JS but the browser served the old
        file" confusion during development.
        """
        try:
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        except Exception:  # pragma: no cover - never block serving on a header
            pass
        super().end_headers()


class FrontendStaticServer:
    """Serves the bundled front-end assets over loopback HTTP.

    The server runs :class:`http.server.ThreadingHTTPServer` on a background
    daemon thread, serving files from :attr:`frontend_dir`. It is safe to call
    :meth:`start` / :meth:`stop` repeatedly; both are idempotent and guarded by
    an internal lock.

    Args:
        host: Interface to bind. Defaults to loopback ``127.0.0.1`` (reused from
            :class:`ProtocolSettings`). Binding to a non-loopback host would
            require adding access control first (see module docstring).
        port: TCP port for the asset listener. ``0`` requests an ephemeral port
            from the OS (handy for tests); the bound port is then exposed via
            :attr:`base_url`. Defaults to ``ProtocolSettings.port + 1`` so it
            does not collide with the ``/client-ws`` gateway.
        frontend_dir: Directory whose contents are served. Defaults to the
            bundled :func:`default_frontend_dir`.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PROTOCOL_PORT + 1,
        frontend_dir: Optional[Path] = None,
        ssl_context: Optional[Any] = None,
    ) -> None:
        self._host = host
        self._configured_port = int(port)
        self._frontend_dir = (
            Path(frontend_dir) if frontend_dir is not None else default_frontend_dir()
        )
        # Optional ssl.SSLContext: when provided, assets are served over HTTPS
        # (so a phone reaching the page over the LAN gets a secure context and
        # the browser allows microphone access). None -> plain HTTP.
        self._ssl_context = ssl_context

        self._lock = threading.Lock()
        self._server: Optional["ThreadingHTTPServer"] = None  # type: ignore[type-arg]
        self._thread: Optional[threading.Thread] = None
        self._bound_port: Optional[int] = None

    # -- Construction helpers ------------------------------------------------

    @classmethod
    def from_protocol_settings(
        cls,
        settings: Any,
        frontend_dir: Optional[Path] = None,
        ssl_context: Optional[Any] = None,
    ) -> "FrontendStaticServer":
        """Build a server from a :class:`ProtocolSettings`-like object.

        Reuses ``settings.host`` (loopback by default) and derives the asset
        port as ``settings.port + 1`` so the static listener sits beside the
        ``/client-ws`` gateway without clashing.
        """
        host = getattr(settings, "host", DEFAULT_HOST)
        base_port = int(getattr(settings, "port", DEFAULT_PROTOCOL_PORT))
        return cls(host=host, port=base_port + 1, frontend_dir=frontend_dir, ssl_context=ssl_context)

    # -- Properties ----------------------------------------------------------

    @property
    def frontend_dir(self) -> Path:
        """The directory whose contents are served over HTTP."""
        return self._frontend_dir

    @property
    def host(self) -> str:
        """The bound interface (loopback by default)."""
        return self._host

    @property
    def port(self) -> Optional[int]:
        """The actually-bound port when running, else the configured port.

        While stopped this returns the configured port (which may be ``0`` to
        request an ephemeral port). While running it returns the real port the
        OS assigned.
        """
        return self._bound_port if self._bound_port is not None else self._configured_port

    @property
    def base_url(self) -> Optional[str]:
        """The base URL assets are served from, or ``None`` when not running."""
        if self._bound_port is None:
            return None
        scheme = "https" if self._ssl_context is not None else "http"
        return f"{scheme}://{self._host}:{self._bound_port}"

    def is_available(self) -> bool:
        """Whether the serving capability loaded (Requirement 12 degradation)."""
        return _SERVE_IMPORT_ERROR is None

    def is_running(self) -> bool:
        """Whether the background HTTP server is currently serving."""
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> FrontendServerStatus:
        """Start serving assets on a background daemon thread (idempotent).

        Returns a :class:`FrontendServerStatus`. If the guarded serving import
        failed, the server reports an *unavailable* (degraded) status instead of
        raising, so the rest of the plugin keeps working (Requirement 12).
        """
        with self._lock:
            if _SERVE_IMPORT_ERROR is not None:
                msg = (
                    "Front-end static server unavailable: the standard-library "
                    f"http.server could not be loaded ({_SERVE_IMPORT_ERROR!r}); "
                    "asset hosting is degraded."
                )
                logger.error(msg)
                return FrontendServerStatus(
                    available=False, running=False, base_url=None, message=msg
                )

            if self.is_running():
                return self._status_locked("Front-end static server already running.")

            # Ensure there is something to serve; a missing directory is not
            # fatal (the handler would 404), but we surface it for diagnostics.
            if not self._frontend_dir.is_dir():
                logger.warning(
                    "Front-end asset directory %s does not exist; serving will "
                    "return 404 until assets are present.",
                    self._frontend_dir,
                )

            handler = partial(
                _QuietHTTPRequestHandler, directory=str(self._frontend_dir)
            )
            try:
                self._server = ThreadingHTTPServer((self._host, self._configured_port), handler)
            except OSError as exc:
                msg = (
                    f"Front-end static server failed to bind {self._host}:"
                    f"{self._configured_port}: {exc}"
                )
                logger.error(msg)
                self._server = None
                return FrontendServerStatus(
                    available=True, running=False, base_url=None, message=msg
                )

            # ``server_address[1]`` reflects the real port (resolves port 0).
            self._bound_port = self._server.server_address[1]

            # Wrap the listening socket with TLS when an ssl context is provided
            # (HTTPS), so a phone on the LAN gets a secure context and the
            # browser allows microphone access.
            if self._ssl_context is not None:
                try:
                    self._server.socket = self._ssl_context.wrap_socket(
                        self._server.socket, server_side=True
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("failed to enable TLS on front-end server: %s", exc)
                    try:
                        self._server.server_close()
                    finally:
                        self._server = None
                        self._bound_port = None
                    return FrontendServerStatus(
                        available=True, running=False, base_url=None,
                        message=f"Front-end HTTPS setup failed: {exc}",
                    )

            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="omnilimb-face-frontend",
                daemon=True,
            )
            self._thread.start()

            return self._status_locked(
                f"Front-end static server serving {self._frontend_dir} at "
                f"{self.base_url}."
            )

    def stop(self) -> FrontendServerStatus:
        """Stop the background HTTP server and release its socket (idempotent)."""
        with self._lock:
            server = self._server
            thread = self._thread
            if server is None:
                return self._status_locked("Front-end static server already stopped.")

            try:
                server.shutdown()  # unblocks serve_forever()
            except Exception:  # pragma: no cover - defensive
                logger.debug("Error during front-end server shutdown", exc_info=True)
            finally:
                server.server_close()

            if thread is not None:
                thread.join(timeout=3.0)

            self._server = None
            self._thread = None
            self._bound_port = None
            return self._status_locked("Front-end static server stopped.")

    def status(self) -> FrontendServerStatus:
        """Return the current status snapshot (thread-safe)."""
        with self._lock:
            if _SERVE_IMPORT_ERROR is not None:
                return FrontendServerStatus(
                    available=False,
                    running=False,
                    base_url=None,
                    message="Front-end static server unavailable (serving capability not loaded).",
                )
            return self._status_locked(
                "Front-end static server running." if self.is_running()
                else "Front-end static server stopped."
            )

    # -- Context manager sugar ----------------------------------------------

    def __enter__(self) -> "FrontendStaticServer":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -- Internals -----------------------------------------------------------

    def _status_locked(self, message: str) -> FrontendServerStatus:
        """Build a status snapshot; caller must hold ``self._lock``."""
        return FrontendServerStatus(
            available=self.is_available(),
            running=self.is_running(),
            base_url=self.base_url,
            message=message,
        )
