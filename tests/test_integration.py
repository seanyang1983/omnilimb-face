"""Integration / smoke tests for the omnilimb-face plugin.

This module hosts cross-component and end-to-end style tests: the ``/client-ws``
handshake and initialization sequence, the capture -> transcribe -> inject ->
hook-intercept -> chunk -> synthesize -> lip-sync pipeline (with host I/O
mocked), and plugin discovery via both the directory path and the pip entry
point.

Populated by later tasks (20.3, 22.2, 24.x). Intentionally empty of tests at
the scaffolding stage; it must import and collect cleanly under pytest.
"""

# ---------------------------------------------------------------------------
# Task 24.3 — Front-end lip-sync / expression driving integration stub.
# ---------------------------------------------------------------------------
#
# IMPORTANT — what this stub does and does NOT verify:
#
# The *rendering-side* targets of Requirements 7 and 8 are FRONT-END concerns
# that can only be validated visually / manually against the running compatible
# front-end window. This stub does NOT (and cannot) assert any of them:
#
#   * rendering frame-rate >= 30 FPS                         (Requirement 7.2)
#   * lip-sync amplitude latency <= 100 ms relative to audio (Requirement 7.3)
#   * mouth returns to its closed resting state within 200 ms (Requirement 7.4)
#   * expression transition starts within 300 ms and
#     completes in 200-500 ms                                (Requirement 8.2)
#
# Those are properties of the Live2D (Cubism / WebGL) renderer running at
# >= 30 FPS in the front-end and are listed in design.md as manual / visual
# verification items (tasks.md task 24.3 note).
#
# What this stub *does* assert is the **backend driving-data contract**: that
# the Python side (``Live2DDirector``) EMITS the correct ``/client-ws`` driving
# events so a conforming front-end has everything it needs to hit those
# rendering targets:
#
#   * ``push_audio_segment`` emits an ``audio`` event carrying the lip-sync
#     ``volumes`` / ``slice_length`` (Requirement 7.3 data) and the expression
#     sequence under ``actions == {"expressions": [...]}`` (Requirement 8).
#   * ``announce_model`` emits a ``set-model-and-conf`` event carrying the
#     model info the front-end loads (Requirement 7.1 / 7.2 setup).
#   * ``push_idle`` emits a ``control: mouth-reset`` event instructing the
#     front-end to return the mouth to its resting state (Requirement 7.4).

from omnilimb_face.live2d import Live2DDirector, Live2DModelInfo
from omnilimb_face.protocol.events import AudioEvent, ControlEvent, SetModelEvent
from omnilimb_face.tts import AudioSegmentOut


class _RecordingGateway:
    """Fake protocol gateway that records every dispatched server event.

    ``Live2DDirector`` dispatches synchronously through the first available
    ``send_event`` / ``serialize`` method (see ``Live2DDirector._DISPATCH_METHODS``).
    Exposing a plain (non-coroutine) ``send_event`` here captures the exact
    events the director hands to the gateway without needing the real async
    WebSocket transport (Task 20.1).
    """

    def __init__(self) -> None:
        self.events: list = []

    def send_event(self, event) -> None:
        self.events.append(event)


def _make_model() -> Live2DModelInfo:
    """A small, fully-populated model with a neutral expression mapping."""
    return Live2DModelInfo(
        name="shizuku",
        url="models/shizuku/shizuku.model3.json",
        emotion_map={"neutral": 0, "joy": 1, "anger": 3},
        is_placeholder=False,
    )


def test_push_audio_segment_emits_audio_event_with_lipsync_and_expressions():
    """``push_audio_segment`` emits an ``audio`` event carrying the lip-sync
    volume series and the expression action sequence (backend portion of
    Requirements 7.3 and 8 — rendering latency/transition timing are front-end,
    manual-verification targets).
    """
    gateway = _RecordingGateway()
    director = Live2DDirector(_make_model(), gateway)

    seg = AudioSegmentOut(
        wav_bytes=b"RIFF\x00\x00\x00\x00WAVEfmt ",
        volumes=[0.0, 0.5, 1.0, 0.25],
        slice_length_ms=20,
        display_text="Hello there",
        expressions=[1, 3],
    )

    returned = director.push_audio_segment(seg)

    # Exactly one event was dispatched, and it is the returned ``audio`` event.
    assert len(gateway.events) == 1
    recorded = gateway.events[0]
    assert recorded is returned
    assert isinstance(recorded, AudioEvent)
    assert recorded.type == "audio"

    # Lip-sync driving data (Requirement 7.3) is carried verbatim.
    assert recorded.volumes == [0.0, 0.5, 1.0, 0.25]
    assert recorded.slice_length == 20

    # Expression driving data (Requirement 8) is carried under ``actions``.
    assert recorded.actions == {"expressions": [1, 3]}

    # The displayable sentence is wrapped into the protocol ``display_text`` dict
    # and the WAV bytes are forwarded as a base64 ``audio`` payload.
    assert recorded.display_text == {"text": "Hello there"}
    assert isinstance(recorded.audio, str) and recorded.audio


def test_push_audio_segment_carries_empty_volumes_and_expressions():
    """A no-expression / silent-volume segment still emits a well-formed
    ``audio`` event (empty ``volumes`` and empty expression list), so the
    front-end contract holds for the degenerate case too.
    """
    gateway = _RecordingGateway()
    director = Live2DDirector(_make_model(), gateway)

    seg = AudioSegmentOut(
        wav_bytes=b"",
        volumes=[],
        slice_length_ms=0,
        display_text="",
        expressions=[],
    )

    recorded = director.push_audio_segment(seg)

    assert isinstance(recorded, AudioEvent)
    assert recorded.volumes == []
    assert recorded.slice_length == 0
    assert recorded.actions == {"expressions": []}
    # No WAV bytes -> no base64 audio payload (lip-sync/expression-only frame).
    assert recorded.audio is None


def test_announce_model_emits_set_model_event_carrying_model_info():
    """``announce_model`` emits a ``set-model-and-conf`` event carrying the
    model info the front-end loads and displays (Requirement 7.1 setup).
    """
    gateway = _RecordingGateway()
    model = _make_model()
    director = Live2DDirector(model, gateway)

    returned = director.announce_model()

    assert len(gateway.events) == 1
    recorded = gateway.events[0]
    assert recorded is returned
    assert isinstance(recorded, SetModelEvent)
    assert recorded.type == "set-model-and-conf"

    # The model info payload carries the name, url and emotionMap the front-end
    # needs to load the avatar.
    assert recorded.model_info["name"] == "shizuku"
    assert recorded.model_info["url"] == "models/shizuku/shizuku.model3.json"
    assert recorded.model_info["emotionMap"] == {"neutral": 0, "joy": 1, "anger": 3}
    assert recorded.conf_name == "shizuku"


def test_push_idle_emits_mouth_reset_control_event():
    """``push_idle`` emits a ``control: mouth-reset`` event so the front-end
    returns the mouth to its closed resting state after playback (backend
    portion of Requirement 7.4 — the 200 ms return timing is front-end/manual).
    """
    gateway = _RecordingGateway()
    director = Live2DDirector(_make_model(), gateway)

    director.push_idle()

    # A mouth-reset control event must be among the dispatched events.
    control_events = [
        e for e in gateway.events if isinstance(e, ControlEvent)
    ]
    assert any(e.text == "mouth-reset" and e.type == "control" for e in control_events)


def test_push_idle_drives_neutral_expression_when_model_has_one():
    """When the model exposes a neutral expression, ``push_idle`` also emits a
    no-audio ``audio`` event driving that neutral expression (backend portion
    of Requirement 8.5 — the visual transition is front-end/manual).
    """
    gateway = _RecordingGateway()
    director = Live2DDirector(_make_model(), gateway)

    director.push_idle()

    neutral_audio = [
        e
        for e in gateway.events
        if isinstance(e, AudioEvent) and e.audio is None
    ]
    assert len(neutral_audio) == 1
    # neutral index for this model is 0 (emotion_map["neutral"]).
    assert neutral_audio[0].actions == {"expressions": [0]}


# ---------------------------------------------------------------------------
# Task 24.1 — Discovery-path loading smoke tests (Requirement 1.6).
# ---------------------------------------------------------------------------
#
# Requirement 1.6: WHERE the user installs the plugin via the
# ``~/AppData/Local/hermes/plugins/`` directory OR via a pip entry point, the
# plugin loader SHALL recognise the plugin along the existing discovery paths.
#
# These are SMOKE tests run against a *recording fake ctx* — a stand-in for the
# host ``PluginContext`` that merely records ``register_hook`` /
# ``register_tool`` / ``register_cli_command`` / ``register_command`` calls. It
# is deliberately NOT the real hermes host: the goal here is only to prove the
# plugin is *loadable* and *registers* its full surface along BOTH discovery
# paths, without needing a live hermes runtime. The real cross-platform CI
# matrix install (actually `pip install`-ing into a fresh interpreter on
# Windows/macOS/Linux and importing through the host) is covered by task 24.2
# and the 11.x cross-platform tasks; this test runs anywhere pytest can collect
# the package.

import importlib
import importlib.util
import tomllib
from pathlib import Path

import pytest

# Repo root: this file lives at <root>/tests/test_integration.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The capabilities ``register(ctx)`` is expected to register (design.md ->
# "Plugin Entry Point: register(ctx)"): four lifecycle/LLM hooks, two tools,
# one CLI subcommand and two slash commands.
_EXPECTED_HOOKS = {
    "on_session_start",
    "on_session_end",
    "transform_llm_output",
    "post_llm_call",
}
_EXPECTED_TOOLS = {"vtuber_status", "vtuber_say"}
_EXPECTED_CLI_COMMANDS = {"vtuber"}
_EXPECTED_SLASH_COMMANDS = {"vtuber", "handsfree"}


class _RecordingCtx:
    """A recording stand-in for the host ``PluginContext``.

    It records every registration call ``register(ctx)`` makes through the
    generic extension surface. It implements exactly the four registration
    methods the plugin uses (``register_hook`` / ``register_tool`` /
    ``register_cli_command`` / ``register_command``) plus an empty ``config``
    dict so ``ConfigManager.from_host`` resolves to documented defaults without
    touching a real hermes host. It is intentionally minimal — NOT the real
    host context.
    """

    def __init__(self) -> None:
        # Empty host config -> ConfigManager.from_host uses documented defaults.
        self.config: dict = {}
        self.hooks: list[tuple[str, object]] = []
        self.tools: list[dict] = []
        self.cli_commands: list[dict] = []
        self.slash_commands: list[dict] = []

    def register_hook(self, name, handler):
        self.hooks.append((name, handler))

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)

    def register_command(self, name, handler, **kwargs):
        self.slash_commands.append({"name": name, "handler": handler, **kwargs})

    # -- assertion helpers --------------------------------------------------

    def hook_names(self) -> set[str]:
        return {name for name, _ in self.hooks}

    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    def cli_command_names(self) -> set[str]:
        return {c["name"] for c in self.cli_commands}

    def slash_command_names(self) -> set[str]:
        return {c["name"] for c in self.slash_commands}


def _assert_full_surface_registered(ctx: _RecordingCtx) -> None:
    """Assert ``register(ctx)`` registered the complete expected surface:
    4 hooks, 2 tools (vtuber_status / vtuber_say), 1 CLI command (vtuber) and
    2 slash commands (vtuber / handsfree).
    """
    # Four lifecycle / LLM-output hooks, registered exactly once each.
    assert len(ctx.hooks) == 4
    assert ctx.hook_names() == _EXPECTED_HOOKS

    # Two tools, both under the "vtuber" toolset, with callable handlers.
    assert len(ctx.tools) == 2
    assert ctx.tool_names() == _EXPECTED_TOOLS
    for tool in ctx.tools:
        assert tool["toolset"] == "vtuber"
        assert callable(tool["handler"])

    # One CLI subcommand: hermes vtuber ...
    assert ctx.cli_command_names() == _EXPECTED_CLI_COMMANDS

    # Two slash commands: /vtuber and /handsfree.
    assert len(ctx.slash_commands) == 2
    assert ctx.slash_command_names() == _EXPECTED_SLASH_COMMANDS


def _load_root_init_as_module():
    """Load the project's ROOT ``__init__.py`` from its file path.

    This mirrors how hermes' ``PluginManager`` directory discovery loads a
    plugin folder's root ``__init__.py`` (via ``importlib.util`` from the file
    path) and then reads its ``register`` attribute.
    """
    root_init = _PROJECT_ROOT / "__init__.py"
    assert root_init.is_file(), f"missing root __init__.py at {root_init}"

    spec = importlib.util.spec_from_file_location(
        "omnilimb_face_dir_discovery", root_init
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_directory_discovery_path_loads_and_registers():
    """Directory-discovery path (Requirement 1.6).

    Load the root ``__init__.py`` from its file path the way the host's
    directory discovery does, assert it exposes a callable ``register``, then
    call ``register(fake_ctx)`` and assert the full capability surface was
    registered.
    """
    module = _load_root_init_as_module()

    register = getattr(module, "register", None)
    assert callable(register), "root __init__.py must expose a callable register"

    ctx = _RecordingCtx()
    register(ctx)

    _assert_full_surface_registered(ctx)


def test_entry_point_path_declares_and_loads():
    """Entry-point path (Requirement 1.6).

    Read ``pyproject.toml`` and assert it declares the ``hermes_agent.plugins``
    entry point pointing at ``omnilimb_face.plugin``; then import that module,
    assert ``register`` is callable and that it registers the same surface when
    called with a fresh fake ctx.
    """
    pyproject = _PROJECT_ROOT / "pyproject.toml"
    assert pyproject.is_file(), f"missing pyproject.toml at {pyproject}"

    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)

    entry_points = (
        data.get("project", {})
        .get("entry-points", {})
        .get("hermes_agent.plugins", {})
    )
    assert entry_points, "pyproject must declare the hermes_agent.plugins group"
    # The host resolves the entry-point value to a module, then reads `register`.
    assert "omnilimb-face" in entry_points
    target = entry_points["omnilimb-face"]
    assert target == "omnilimb_face.plugin"

    # Import the module the entry point resolves to and exercise register().
    module = importlib.import_module(target)
    register = getattr(module, "register", None)
    assert callable(register), "entry-point module must expose a callable register"

    ctx = _RecordingCtx()
    register(ctx)

    _assert_full_surface_registered(ctx)


def test_both_discovery_paths_resolve_to_the_same_register():
    """Both discovery paths must converge on the same ``register`` callable.

    The directory root ``__init__.py`` simply re-exports
    ``omnilimb_face.plugin.register``, so the object loaded via the file-path
    (directory) discovery and the object imported via the entry-point target
    are the very same function — confirming the two paths are equivalent
    (Requirement 1.6).
    """
    dir_module = _load_root_init_as_module()
    ep_module = importlib.import_module("omnilimb_face.plugin")

    assert dir_module.register is ep_module.register


def test_plugin_yaml_present_and_parses_with_discovery_name():
    """The root ``plugin.yaml`` manifest must exist and parse, declaring the
    discovery/enablement key ``name == "omnilimb-face"`` (Requirement 1.6 — the
    name the loader keys directory discovery and ``plugins.enabled`` on).
    """
    # yaml is only needed for this one assertion; import it lazily so the rest
    # of the discovery smoke tests still run even where PyYAML is absent.
    yaml = pytest.importorskip("yaml")

    manifest_path = _PROJECT_ROOT / "plugin.yaml"
    assert manifest_path.is_file(), f"missing plugin.yaml at {manifest_path}"

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    assert manifest.get("name") == "omnilimb-face"


# ---------------------------------------------------------------------------
# Task 20.3 — /client-ws handshake + initialization sequence integration test
# (Requirements 7.1, 9.5).
# ---------------------------------------------------------------------------
#
# Unlike the backend-contract stubs above (which exercise the pure
# serialize/dispatch logic with a fake gateway), this test stands up the REAL
# ``ProtocolGateway`` WebSocket transport (Task 20.1) on an ephemeral loopback
# port and drives it with a REAL ``websockets`` client over an actual TCP
# connection. It verifies, end to end, that:
#
#   * Initialization sequence (Requirement 7.1 setup): the server serializes the
#     ``full-text`` -> ``set-model-and-conf`` -> ``control: start-mic`` init
#     messages exactly as the Open-LLM-VTuber compatible front-end expects, and
#     a real client receives and parses them back into the right event
#     types/values. The runtime wiring that *decides when* to push these on
#     connect lives elsewhere; here we drive them explicitly through the
#     gateway's broadcast path to assert the wire serialization is correct.
#
#   * Resilience (Requirement 9.5): after the gateway answers a non-conforming
#     message with an ``ErrorEvent`` it keeps the connection usable. We send a
#     malformed (invalid-JSON) frame -> ``invalid_json`` error + connection stays
#     open; then a valid ``text-input`` -> it is parsed and routed (observed via
#     an ``on_event`` recorder) and the connection still works; then a
#     known-type-but-bad-schema frame -> ``schema_invalid`` error + connection
#     still open.
#
# pytest-asyncio is intentionally NOT a dependency of this package, so the async
# client scenario is driven via ``asyncio.run`` inside a plain (sync) test
# function. The gateway runs its own event loop on a daemon thread, so the
# client loop (this thread) and the server loop never share a loop.

import asyncio
import json
import threading
import time

import websockets

from omnilimb_face.config import ProtocolSettings
from omnilimb_face.protocol.events import (
    ControlEvent,
    ErrorEvent,
    FullTextEvent,
    SetModelEvent,
    TextInputEvent,
)
from omnilimb_face.protocol.gateway import ProtocolGateway


class _EventRecorder:
    """Thread-safe ``on_event`` recorder for parsed inbound events.

    The gateway invokes ``on_event(event, action, client_uid)`` on its own
    event-loop thread for every successfully parsed inbound message, so the
    recorder guards its list with a lock and exposes a snapshot the test thread
    can poll.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[tuple] = []

    def __call__(self, event, action, client_uid) -> None:
        with self._lock:
            self._records.append((event, action, client_uid))

    def snapshot(self) -> list[tuple]:
        with self._lock:
            return list(self._records)


async def _await_true(predicate, timeout: float, interval: float = 0.02) -> None:
    """Await until ``predicate()`` returns truthy or ``timeout`` seconds pass.

    Used to bridge the two event loops without a fixed sleep: e.g. waiting for
    the server thread to register the just-connected client, or for the
    ``on_event`` recorder to observe a routed inbound event.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    if predicate():
        return
    raise AssertionError(f"condition not met within {timeout}s")


async def _client_ws_scenario(
    uri: str,
    gateway: ProtocolGateway,
    recorder: _EventRecorder,
    model_info: dict,
    full_text: str,
) -> None:
    """Drive a single real client connection through the init + resilience flow."""
    async with websockets.connect(uri, ping_interval=None, open_timeout=5) as ws:
        # The server registers the client in its handler coroutine, which runs
        # just after the handshake completes; wait for it before broadcasting so
        # the init messages are actually delivered to this connection.
        await _await_true(lambda: gateway.client_count >= 1, timeout=5.0)

        # --- Initialization sequence (Requirement 7.1 setup) ---------------
        # Drive the server -> client init messages explicitly through the
        # gateway's (thread-safe) broadcast path: full-text, set-model-and-conf,
        # control: start-mic, in that order.
        delivered = gateway.broadcast_threadsafe(FullTextEvent(text=full_text))
        assert delivered == 1, "init full-text must reach the one connected client"
        gateway.broadcast_threadsafe(
            SetModelEvent(model_info=model_info, conf_name="shizuku")
        )
        gateway.broadcast_threadsafe(ControlEvent(text="start-mic"))

        raw1 = await asyncio.wait_for(ws.recv(), timeout=5)
        raw2 = await asyncio.wait_for(ws.recv(), timeout=5)
        raw3 = await asyncio.wait_for(ws.recv(), timeout=5)

        # Each frame is a single JSON object with the expected discriminant, and
        # parses back (round-trip) into the matching event with matching values.
        assert json.loads(raw1)["type"] == "full-text"
        out1 = gateway.parse(raw1)
        assert out1.ok and isinstance(out1.event, FullTextEvent)
        assert out1.event.text == full_text

        assert json.loads(raw2)["type"] == "set-model-and-conf"
        out2 = gateway.parse(raw2)
        assert out2.ok and isinstance(out2.event, SetModelEvent)
        assert out2.event.model_info == model_info
        assert out2.event.conf_name == "shizuku"

        assert json.loads(raw3)["type"] == "control"
        out3 = gateway.parse(raw3)
        assert out3.ok and isinstance(out3.event, ControlEvent)
        assert out3.event.text == "start-mic"

        # --- Resilience (Requirement 9.5) ----------------------------------
        # 1) Malformed (invalid JSON) -> invalid_json ErrorEvent, connection
        #    stays open (proven by receiving the error back over the same ws).
        await ws.send("this is definitely not valid json {")
        err_raw = await asyncio.wait_for(ws.recv(), timeout=5)
        err_out = gateway.parse(err_raw)
        assert err_out.ok and isinstance(err_out.event, ErrorEvent)
        assert err_out.event.code == "invalid_json"
        assert ws.state.name == "OPEN"

        # 2) Valid text-input -> parsed and routed; observed via the on_event
        #    recorder. A successful parse produces NO server -> client reply, so
        #    we poll the recorder rather than recv().
        valid = TextInputEvent(text="hello from the frontend")
        await ws.send(gateway.serialize(valid))
        await _await_true(
            lambda: any(
                isinstance(ev, TextInputEvent) and ev.text == "hello from the frontend"
                for (ev, _action, _uid) in recorder.snapshot()
            ),
            timeout=5.0,
        )
        assert ws.state.name == "OPEN"

        # 3) Known type but bad schema (missing required ``text``) ->
        #    schema_invalid ErrorEvent, connection still works. Receiving this
        #    error after the valid message confirms the connection survived the
        #    earlier malformed frame and the valid routed message.
        await ws.send(json.dumps({"type": "text-input"}))
        err2_raw = await asyncio.wait_for(ws.recv(), timeout=5)
        err2_out = gateway.parse(err2_raw)
        assert err2_out.ok and isinstance(err2_out.event, ErrorEvent)
        assert err2_out.event.code == "schema_invalid"
        assert ws.state.name == "OPEN"


def test_client_ws_handshake_init_sequence_and_resilience():
    """Real ``/client-ws`` client: init message sequence + non-conforming
    message resilience, end to end against the live gateway transport.

    Covers Requirement 7.1 (the server serializes the
    full-text -> set-model-and-conf -> control:start-mic init sequence the
    front-end expects) and Requirement 9.5 (after answering a non-conforming
    message with an ``ErrorEvent`` the WebSocket connection stays usable for
    subsequent messages). Cleans up by stopping the gateway and asserting no
    server thread lingers.
    """
    model_info = {
        "name": "shizuku",
        "url": "models/shizuku/shizuku.model3.json",
        "emotionMap": {"neutral": 0, "joy": 1, "anger": 3},
    }
    recorder = _EventRecorder()
    # Bind to an ephemeral loopback port (port=0 -> OS assigns a free port).
    cfg = ProtocolSettings(host="127.0.0.1", port=0)
    gateway = ProtocolGateway(cfg=cfg, on_event=recorder)

    thread = gateway.start_in_thread(ready_timeout=5.0)
    try:
        assert gateway.is_running()
        host = gateway.bound_host
        port = gateway.bound_port
        assert host == "127.0.0.1"
        assert isinstance(port, int) and port > 0  # ephemeral port resolved.

        uri = f"ws://{host}:{port}{cfg.ws_path}"
        # Hard outer timeout so a hung connection can never wedge the suite.
        asyncio.run(
            asyncio.wait_for(
                _client_ws_scenario(uri, gateway, recorder, model_info, "Connection established."),
                timeout=30,
            )
        )

        # The valid text-input was parsed and routed exactly once.
        text_inputs = [
            ev for (ev, _a, _uid) in recorder.snapshot() if isinstance(ev, TextInputEvent)
        ]
        assert len(text_inputs) == 1
        assert text_inputs[0].text == "hello from the frontend"
    finally:
        # Clean up: stop() the gateway and join its serving thread.
        gateway.stop(timeout=5.0)

    # No lingering server thread after stop().
    assert thread is not None
    assert not thread.is_alive()
    assert not gateway.is_running()
    assert gateway.client_count == 0


# ---------------------------------------------------------------------------
# Task 22.2 — End-to-end voice-turn integration smoke test
# (Requirements 3.1, 3.2, 6.1).
# ---------------------------------------------------------------------------
#
# These smoke tests exercise the FULL plugin chain with all host I/O MOCKED, per
# design.md's Testing Strategy and decision 1 (Plan A — "give the agent a face"):
#
#     mic segment -> STT_Engine -> ctx.inject_message -> host turn ->
#     transform_llm_output / post_llm_call hooks -> Sentence_Chunker ->
#     TTS_Player (+ Expression_Mapper) -> Live2D_Director -> /client-ws gateway
#
# They are deliberately hermetic and fast: NO real network, NO real microphone,
# NO real model. The host surface is faked:
#
#   * ``ctx.dispatch_tool("text_to_speech", {...})`` returns a valid host
#     envelope JSON pointing at a small *real* temp WAV file we write, so the
#     pure ``TTSPlayer.compute_volumes`` runs on REAL int16 PCM and produces a
#     genuine lip-sync volume series (Requirement 6.1 / 7.3).
#   * ``ctx.inject_message(text, role=...)`` records the call and returns ``True``
#     (interactive CLI mode), the entry point that triggers the host turn
#     (Requirement 4.4).
#   * The protocol gateway is a fake recorder bound to ``runtime._protocol_gateway``
#     — the runtime's ``_GatewayBroadcaster`` reads it at dispatch time and calls
#     ``broadcast_threadsafe`` — so we capture exactly the events the
#     ``Live2DDirector`` broadcasts without standing up the real WebSocket
#     transport (keeps the test off real sockets, mirroring task 22.1's
#     gateway-broadcaster seam).
#
# The STT back-end is faked via the ``STTEngine``'s injectable
# ``host_transcribe_audio`` (the directly-imported, NON-registry
# ``tools.transcription_tools.transcribe_audio`` in production), so the voice
# entry point is driven without a real speech back-end.

import math
import os
import tempfile
import wave
from array import array

from omnilimb_face.config import VTuberConfig
from omnilimb_face.runtime import VTuberRuntime
from omnilimb_face.stt import STTEngine


def _write_temp_wav_with_tone(sample_rate: int = 16000, duration_ms: int = 120) -> str:
    """Write a short, NON-silent int16 mono PCM WAV to a temp file; return its path.

    A decaying 220 Hz tone (amplitude varies across slices, so distinct per-chunk
    RMS) guarantees ``TTSPlayer.compute_volumes`` yields a non-empty,
    peak-normalized (``max == 1.0``) lip-sync volume series rather than the
    all-silent degenerate case. Written with the stdlib ``wave`` module so the
    file is a real 16-bit PCM WAV the player can decode (Requirement 6.1).
    """
    n_samples = int(sample_rate * duration_ms / 1000)
    samples = array("h")
    for i in range(n_samples):
        decay = 1.0 - (i / (2 * n_samples))
        value = int(30000 * math.sin(2 * math.pi * 220 * i / sample_rate) * decay)
        samples.append(max(-32768, min(32767, value)))

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="omnilimb-face-e2e-tts-")
    os.close(fd)
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # int16 -> 2 bytes/sample
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())
    return path


class _FakeHostCtx:
    """Fake host ``PluginContext`` exercising the runtime's end-to-end path.

    Implements only the slice of the host surface the voice/text turn touches:

    * :meth:`dispatch_tool` — the host ``text_to_speech`` tool. Records every
      dispatch and, for ``text_to_speech``, returns a valid host envelope JSON
      string pointing at the real temp WAV (so synthesis decodes real PCM).
    * :meth:`inject_message` — records ``(text, role)`` and returns ``True`` to
      model an interactive CLI session (Requirement 4.4 / 11.6).

    The empty ``config`` dict lets ``ConfigManager.from_host`` resolve to
    documented defaults if it were ever consulted (it is not here — we pass an
    explicit :class:`VTuberConfig`).
    """

    def __init__(self, tts_wav_path: str) -> None:
        self._tts_wav_path = str(tts_wav_path)
        self.config: dict = {}
        self.dispatch_calls: list[tuple] = []
        self.tts_texts: list[str] = []
        self.injected: list[tuple] = []

    def dispatch_tool(self, name, args=None):
        self.dispatch_calls.append((name, args))
        if name == "text_to_speech":
            text = args.get("text", "") if isinstance(args, dict) else ""
            self.tts_texts.append(text)
            return json.dumps(
                {
                    "success": True,
                    "file_path": self._tts_wav_path,
                    "provider": "edge",
                }
            )
        return json.dumps({"success": False, "error": f"unknown tool {name!r}"})

    def inject_message(self, text, role="user"):
        self.injected.append((text, role))
        return True


class _RecordingBroadcastGateway:
    """Fake ``ProtocolGateway`` recording every broadcast server event.

    The runtime's ``_GatewayBroadcaster.send_event`` reads
    ``runtime._protocol_gateway`` at dispatch time and prefers its
    ``broadcast_threadsafe`` to fan an event out to connected front-ends,
    returning the client count. Recording that call captures exactly the
    ``/client-ws`` events the ``Live2DDirector`` pushes over the gateway,
    hermetically (no real WebSocket / TCP socket).
    """

    def __init__(self) -> None:
        self.events: list = []

    def broadcast_threadsafe(self, event):
        self.events.append(event)
        return 1


class _FakeVoiceSegment:
    """Minimal stand-in for a captured ``VoiceSegment`` (duck-typed).

    ``STTEngine.transcribe`` reads ``segment.pcm`` (int16 mono PCM) and
    ``segment.sample_rate`` to stage a temp WAV before calling the host STT
    back-end; a tiny silent buffer is enough since the back-end is faked.
    """

    def __init__(self, pcm: bytes, sample_rate: int = 16000) -> None:
        self.pcm = pcm
        self.sample_rate = sample_rate
        self.start_ms = 0
        self.end_ms = 100
        self.end_reason = "silence"


def test_end_to_end_host_turn_drives_tts_lipsync_and_avatar():
    """Full host-turn chain (Requirements 3.1, 3.2, 6.1), host I/O mocked.

    Streams a multi-sentence agent reply token-by-token through the
    ``transform_llm_output`` observer and finalizes it via ``post_llm_call``,
    then asserts the avatar pipeline ran end to end:

    * ``TTSPlayer.synthesize`` was invoked once per sentence — i.e.
      ``ctx.dispatch_tool("text_to_speech", ...)`` fired per sentence in text
      order, with emotion markers stripped (Requirement 6.1);
    * one ``AudioEvent`` per sentence was broadcast over the (fake) gateway, each
      carrying a non-empty, peak-normalized lip-sync ``volumes`` series computed
      from the REAL temp WAV PCM, plus the ``actions.expressions`` sequence
      (Requirement 6.1 / 7.3 / 8);
    * every ``on_llm_output`` call returned ``None`` (pure observer, never
      rewrites the host reply); and
    * the reply derives solely from the host hook text + the host TTS tool, with
      no plugin-owned model — so switching the host model needs no plugin change
      (Requirement 3.2).
    """
    wav_path = _write_temp_wav_with_tone()
    runtime = None
    try:
        ctx = _FakeHostCtx(wav_path)
        runtime = VTuberRuntime(ctx=ctx, config=VTuberConfig())

        # Hermetic gateway: capture every event the director broadcasts.
        gateway = _RecordingBroadcastGateway()
        runtime._protocol_gateway = gateway

        # Pre-seed the active Live2D model so the ExpressionMapper has an emotion
        # map (the default config points at a missing model dict -> placeholder
        # with an empty map). ``_ensure_pipeline`` keeps an already-set model.
        runtime._model = Live2DModelInfo(
            name="shizuku",
            url="models/shizuku/shizuku.model3.json",
            emotion_map={"neutral": 0, "joy": 1, "anger": 3},
            is_placeholder=False,
        )

        # --- Simulate a host turn (Requirement 3.1) ------------------------
        # A 3-sentence reply led by a [joy] emotion marker, streamed char by char
        # through the observer hook, then concluded via post_llm_call.
        reply = "[joy] Hello there. How are you today? I am doing great!"
        for token in reply:
            # Observer contract: NEVER rewrites the host output -> always None.
            assert runtime.on_llm_output(token) is None
        assert runtime.on_post_llm_call(reply) is None

        # TTSPlayer.synthesize ran once per sentence: dispatch_tool called with
        # text_to_speech for each sentence, in text order, markers stripped.
        tts_calls = [a for (n, a) in ctx.dispatch_calls if n == "text_to_speech"]
        assert len(tts_calls) == 3
        assert ctx.tts_texts == [
            "Hello there.",
            "How are you today?",
            "I am doing great!",
        ]

        # The director broadcast exactly one AudioEvent per sentence over the
        # (fake) gateway.
        audio_events = [e for e in gateway.events if isinstance(e, AudioEvent)]
        assert len(audio_events) == 3

        # Every synthesized segment carries a non-empty, peak-normalized lip-sync
        # volume series computed from the REAL temp WAV PCM (Requirement 6.1/7.3),
        # an expression action sequence, and a base64 WAV audio payload.
        for ev in audio_events:
            assert ev.volumes, "expected a non-empty lip-sync volume series"
            assert all(0.0 <= v <= 1.0 for v in ev.volumes)
            assert max(ev.volumes) == pytest.approx(1.0)
            assert ev.slice_length > 0
            assert isinstance(ev.actions, dict) and "expressions" in ev.actions
            assert isinstance(ev.audio, str) and ev.audio  # base64 WAV present

        # The first sentence's [joy] marker maps to expression index 1; the
        # later (marker-free) sentences carry an empty expression list.
        assert audio_events[0].actions == {"expressions": [1]}
        assert audio_events[0].display_text == {"text": "Hello there."}
        assert audio_events[1].actions == {"expressions": []}
        assert audio_events[2].actions == {"expressions": []}
        # At least one segment carried a non-empty expression action sequence.
        assert any(ev.actions["expressions"] for ev in audio_events)

        # Model switching (Requirement 3.2): the avatar reply derived entirely
        # from the host hook text + the host text_to_speech tool. The plugin
        # dispatched ONLY text_to_speech (it owns no model/credentials and never
        # called a model itself), and the reply text flowed in solely through the
        # output hooks (not via inject here) -> switching the host's active model
        # needs no plugin change.
        assert {n for (n, _a) in ctx.dispatch_calls} == {"text_to_speech"}
        assert ctx.injected == []
    finally:
        # No leaked threads: stop the ordered-playback worker and quiesce.
        if runtime is not None:
            runtime._teardown_pipeline()
            assert not runtime._tts_player.is_playing()
        if os.path.exists(wav_path):
            os.remove(wav_path)


def test_end_to_end_voice_input_injects_transcript_to_trigger_host_turn():
    """Voice-input entry point (Requirements 3.1 / 4.4), STT + host I/O mocked.

    Drives the runtime's capture -> STT -> inject path with a fake captured
    segment and a fake STT back-end returning a fixed transcript ("hello"), and
    asserts the non-blank transcript is injected exactly once into the active CLI
    session via ``ctx.inject_message(text, role="user")`` — the entry point that
    triggers the host's regular turn (whose reply the output hooks then observe,
    as covered by the host-turn test above). No real microphone or STT back-end
    is used.
    """
    wav_path = _write_temp_wav_with_tone()
    runtime = None
    try:
        ctx = _FakeHostCtx(wav_path)
        runtime = VTuberRuntime(ctx=ctx, config=VTuberConfig())

        gateway = _RecordingBroadcastGateway()
        runtime._protocol_gateway = gateway

        # Fake STT path: a REAL STTEngine wired to a fake host transcribe_audio
        # so no real STT back-end / microphone is touched. Pre-setting the engine
        # makes _ensure_pipeline keep it (it only builds collaborators still None).
        def fake_transcribe_audio(file_path, model=None):
            # The engine stages the captured segment to a real temp WAV before
            # calling us; confirm it handed us a path, then return the canned
            # transcript in the host stt back-end envelope shape.
            assert isinstance(file_path, str) and file_path
            return {"success": True, "transcript": "hello", "provider": "local"}

        runtime._stt_engine = STTEngine(
            VTuberConfig().stt, host_transcribe_audio=fake_transcribe_audio
        )

        # Drive the capture -> STT -> inject voice entry with a fake segment.
        segment = _FakeVoiceSegment(pcm=b"\x00\x00" * 800)
        runtime._on_voice_segment(segment)

        # The non-blank transcript was injected exactly once with role="user" to
        # trigger the host turn (Requirements 3.1 / 4.4).
        assert ctx.injected == [("hello", "user")]
        # And the bridge now considers a host turn available (CLI inject -> True).
        assert runtime._llm_bridge.host_turn_available() is True
    finally:
        if runtime is not None:
            runtime._teardown_pipeline()
        if os.path.exists(wav_path):
            os.remove(wav_path)


def test_end_to_end_blank_transcript_is_not_injected():
    """A blank/whitespace transcript is dropped, never injected (Requirement 4.5).

    Representative negative sample for the voice entry: when the faked STT
    back-end yields only whitespace, ``_on_voice_segment`` must reject it
    (``Transcript.is_empty``) and trigger NO host turn — keeping the smoke suite
    honest that injection is gated on a non-blank transcript.
    """
    wav_path = _write_temp_wav_with_tone()
    runtime = None
    try:
        ctx = _FakeHostCtx(wav_path)
        runtime = VTuberRuntime(ctx=ctx, config=VTuberConfig())
        runtime._protocol_gateway = _RecordingBroadcastGateway()

        def blank_transcribe_audio(file_path, model=None):
            return {"success": True, "transcript": "   ", "provider": "local"}

        runtime._stt_engine = STTEngine(
            VTuberConfig().stt, host_transcribe_audio=blank_transcribe_audio
        )

        runtime._on_voice_segment(_FakeVoiceSegment(pcm=b"\x00\x00" * 800))

        # Blank transcript -> nothing injected, no host turn triggered.
        assert ctx.injected == []
    finally:
        if runtime is not None:
            runtime._teardown_pipeline()
        if os.path.exists(wav_path):
            os.remove(wav_path)


# ---------------------------------------------------------------------------
# Task 24.2 — No-microphone degradation smoke test (Requirement 11.5).
# ---------------------------------------------------------------------------
#
# The cross-platform CI matrix (.github/workflows/ci.yml) installs ONLY the core
# package + dev/test extras on Windows/macOS/Linux — i.e. WITHOUT the optional
# ``[voice]`` / ``[live2d]`` extras — so it actually exercises the plugin in its
# degraded / no-microphone configuration on every OS (需求 11.1/11.2/11.4/11.5).
# This test asserts that SAME degradation contract locally and hermetically: NO
# real microphone, NO real sockets, NO ``[voice]`` deps required. It verifies
# that when no microphone is enumerated (either because the ``[voice]`` stack is
# absent — as it is in the core venv — or via a fake source that enumerates no
# input devices):
#
#   (a) ``SoundDeviceAudioSource.list_input_devices()`` degrades gracefully
#       (returns ``[]`` / a list, never raising) and ``is_available()`` reports a
#       plain bool, and ``VoiceCapture.start_hands_free()`` over a no-device
#       source refuses to activate with a descriptive reason instead of
#       crashing; and
#   (b) a ``VTuberRuntime`` built over a fake host ctx still registers its full
#       surface and operates with hands-free DISABLED while the text + avatar
#       paths remain available — ``handle_cli(doctor)`` reports no microphone
#       devices (and is not crashed), ``/handsfree on`` returns the "voice stack
#       unavailable" message, and the TTS/avatar path stays reachable.
#
# A local fake ctx is defined here (NOT imported across test modules) per the
# task's hermeticity requirement.

import argparse

from omnilimb_face.voice.capture import (
    SoundDeviceAudioSource,
    StartResult,
    VoiceCapture,
)
from omnilimb_face.voice.vad import VadSegmenter


class _NoMicAudioSource:
    """Fake :class:`AudioSource` that enumerates NO input devices.

    Models a host with neither the optional ``[voice]`` stack nor a microphone:
    ``list_input_devices()`` returns ``[]`` so the hands-free gate must refuse to
    activate. ``start`` / ``stop`` / ``frames`` are present to satisfy the
    structural ``AudioSource`` protocol but are never reached (the gate refuses
    before starting). No real device, socket or ``[voice]`` dependency is used.
    """

    def __init__(self) -> None:
        self.started = False
        self.start_calls = 0

    def start(self) -> None:  # pragma: no cover - gate refuses before start
        self.started = True
        self.start_calls += 1

    def stop(self) -> None:
        self.started = False

    def frames(self):  # pragma: no cover - never iterated (never started)
        return iter(())

    @staticmethod
    def list_input_devices():
        return []


class _DegradedHostCtx:
    """Local fake host ``PluginContext`` for the no-microphone degradation test.

    Records the plugin's ``register_*`` calls (so we can assert the full surface
    still registers in a degraded state) AND exposes the slice of the host I/O
    surface the text + avatar paths use (``dispatch_tool`` / ``inject_message``),
    so those paths remain reachable while hands-free is disabled. Defined locally
    — NOT imported from another test module — to keep this smoke test hermetic.
    The empty ``config`` dict lets ``ConfigManager.from_host`` resolve documented
    defaults if it were ever consulted.
    """

    def __init__(self) -> None:
        self.config: dict = {}
        self.hooks: list[tuple] = []
        self.tools: list[dict] = []
        self.cli_commands: list[dict] = []
        self.slash_commands: list[dict] = []
        self.dispatch_calls: list[tuple] = []

    def register_hook(self, name, handler):
        self.hooks.append((name, handler))

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)

    def register_command(self, name, handler, **kwargs):
        self.slash_commands.append({"name": name, "handler": handler, **kwargs})

    def dispatch_tool(self, name, args=None):
        # The avatar/TTS path reaches the host text_to_speech tool through here;
        # recording its availability is enough for the degradation contract.
        self.dispatch_calls.append((name, args))
        return json.dumps({"success": True, "file_path": "", "provider": "edge"})

    def inject_message(self, text, role="user"):
        return True


def test_no_mic_list_input_devices_degrades_gracefully():
    """``list_input_devices()`` / ``is_available()`` degrade without raising, and
    a no-device source makes ``VoiceCapture`` refuse hands-free gracefully
    (Requirement 11.5 / 4.9 / 12.4).

    Part (a) of the no-microphone contract: enumerating devices and probing
    availability must never raise even when the ``[voice]`` backend is absent
    (the core venv), and the hands-free gate — driven SOLELY by the device list
    — must refuse to activate with a descriptive reason rather than crashing.
    """
    # Enumeration + availability probe never raise and return the right shapes
    # regardless of whether the [voice] backend is present.
    devices = SoundDeviceAudioSource.list_input_devices()
    assert isinstance(devices, list)
    assert isinstance(SoundDeviceAudioSource.is_available(), bool)

    # A fake source that enumerates NO input devices: the gate must refuse to
    # activate hands-free (no real mic / [voice] deps needed). Uses a REAL
    # VadSegmenter so this exercises the production VoiceCapture gating logic.
    cfg = VTuberConfig()
    source = _NoMicAudioSource()
    capture = VoiceCapture(cfg, source=source, vad=VadSegmenter(cfg.vad))

    result = capture.start_hands_free()

    assert isinstance(result, StartResult)
    assert result.activated is False
    assert result.success is False
    assert capture.is_running() is False
    # The source was never started (the gate refused before opening a device).
    assert source.start_calls == 0
    # A descriptive microphone-unavailable reason is surfaced (not an exception).
    reason = (result.reason or "") + (result.error or "")
    assert "microphone" in reason.lower()
    assert "not activated" in reason.lower()

    # Idempotent / safe cleanup even though nothing started.
    capture.stop_hands_free()
    assert capture.is_running() is False


def test_runtime_degrades_without_microphone_text_and_avatar_remain(monkeypatch):
    """A ``VTuberRuntime`` over a fake ctx operates with hands-free DISABLED while
    text + avatar paths remain available (Requirement 11.5).

    Part (b) of the no-microphone contract:

    * ``register(ctx)`` still registers the full surface in the degraded state
      (tools stay visible — 需求 12);
    * ``handle_cli(doctor)`` reports that no microphone devices are enumerated
      and does NOT crash;
    * ``/handsfree on`` returns the "voice stack unavailable" message while
      making plain that text + avatar rendering remain available; and
    * the TTS / avatar path stays reachable (``tts_available`` True; the status
      tool reports voice unavailable but TTS available).

    Hermetic: the no-microphone condition is forced deterministically by
    monkeypatching device enumeration to ``[]`` (no real device, socket or
    ``[voice]`` dependency), so the contract holds regardless of the host OS.
    """
    # Force "no microphone enumerated" everywhere the runtime looks, so the
    # contract is asserted deterministically even on a host that has a real mic.
    monkeypatch.setattr(
        SoundDeviceAudioSource, "list_input_devices", staticmethod(lambda: [])
    )

    # Import the plugin entry point lazily (local to this test) so collection
    # never depends on it. register() must succeed in the degraded state.
    from omnilimb_face.plugin import register

    ctx = _DegradedHostCtx()
    register(ctx)

    # Full surface still registered while degraded (tools remain visible — 12).
    assert {t["name"] for t in ctx.tools} == {"vtuber_status", "vtuber_say"}
    assert {n for (n, _h) in ctx.hooks} >= {"on_session_start", "on_session_end"}
    assert {c["name"] for c in ctx.slash_commands} == {"vtuber", "handsfree"}

    runtime = VTuberRuntime(ctx=ctx, config=VTuberConfig())

    # (1) doctor reports no microphone devices and does not crash.
    doctor = runtime.handle_cli(argparse.Namespace(action="doctor"))
    assert isinstance(doctor, str)
    low = doctor.lower()
    assert "no microphone input devices" in low
    # Degradation is explicit that text + avatar rendering remain available.
    assert "avatar rendering remain available" in low

    # (2) /handsfree on -> "voice stack unavailable" message (no [voice] deps in
    # the core/CI install), while text + rendering remain available.
    handsfree = runtime.slash_handsfree("on")
    assert isinstance(handsfree, str)
    hf = handsfree.lower()
    assert "voice stack is unavailable" in hf
    assert "remain available" in hf

    # (3) text + avatar path remains available: TTS reachable via dispatch_tool;
    # status tool returns valid JSON marking voice unavailable but TTS available.
    assert runtime.tts_available() is True
    status = json.loads(runtime.tool_status())
    assert status["ok"] is True
    assert status["subsystems"]["voice"] is False
    assert status["subsystems"]["tts"] is True
