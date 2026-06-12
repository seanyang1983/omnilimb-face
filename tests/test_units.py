"""Unit tests for the omnilimb-face plugin.

This module hosts example-based and edge-case unit tests for individual
components (config required-secret handling, STT failure/timeout paths, TTS
retry/degrade, lifecycle resource release, CLI command status, etc.).

Populated by later tasks. At the scaffolding stage it carries smoke tests that
pin the package's public entry point so the import surface stays intact and the
suite collects/passes cleanly under pytest.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def test_package_imports_and_exposes_register():
    """The package imports and the plugin entry point is exposed (Req 1.1).

    The canonical pip-install import path is ``omnilimb_face.plugin.register``
    —the same module the ``hermes_agent.plugins`` entry point resolves to.
    """
    import omnilimb_face
    from omnilimb_face.plugin import PLUGIN_NAME, register

    assert omnilimb_face.__version__
    assert PLUGIN_NAME == "omnilimb-face"
    assert callable(register)


def test_root_init_reexports_register():
    """The directory-discovery root ``__init__.py`` re-exports ``register``.

    hermes' PluginManager directory discovery loads the plugin directory's
    root ``__init__.py`` and reads its ``register`` attribute (Req 1.1). That
    shim must resolve to the very same callable as ``omnilimb_face.plugin``.
    """
    repo_root = Path(__file__).resolve().parent.parent
    root_init = repo_root / "__init__.py"
    assert root_init.is_file(), "directory-discovery root __init__.py is missing"

    spec = importlib.util.spec_from_file_location("omnilimb_face_root_shim", root_init)
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    from omnilimb_face.plugin import register

    assert shim.register is register


def test_register_is_a_clean_noop_stub():
    """The scaffold ``register(ctx)`` runs without touching anything (Req 1.3).

    Later tasks replace the body; for now it must import and execute cleanly
    when handed a context object, returning None (no registrations yet).
    """
    from omnilimb_face.plugin import register

    class _DummyCtx:
        pass

    assert register(_DummyCtx()) is None


# ---------------------------------------------------------------------------
# MessageRouter (Task 17.1, Requirement 9.1): pure dispatch of parsed
# /client-ws client events to a RouteAction describing intended handling.
# ---------------------------------------------------------------------------
def test_router_maps_each_client_event_to_expected_kind():
    """Every ClientEvent variant routes to exactly the expected RouteKind."""
    from omnilimb_face.protocol.events import (
        FetchConfigsEvent,
        InterruptSignalEvent,
        MicAudioDataEvent,
        MicAudioEndEvent,
        PlaybackCompleteEvent,
        TextInputEvent,
    )
    from omnilimb_face.protocol.router import MessageRouter, RouteKind

    router = MessageRouter()

    cases = [
        (TextInputEvent(text="hello"), RouteKind.INJECT_TEXT, False),
        (MicAudioDataEvent(audio="QUJD"), RouteKind.MIC_AUDIO, False),
        (MicAudioEndEvent(), RouteKind.MIC_AUDIO_END, False),
        (InterruptSignalEvent(at_text_index=7), RouteKind.INTERRUPT, False),
        (FetchConfigsEvent(), RouteKind.FETCH_CONFIGS, False),
        (PlaybackCompleteEvent(), RouteKind.PLAYBACK_COMPLETE, True),
    ]

    for event, expected_kind, expected_ignored in cases:
        action = router.route(event)
        assert action.kind is expected_kind
        # The originating event is carried verbatim for downstream consumers.
        assert action.event is event
        assert action.ignored is expected_ignored
        assert action.reason == ""


def test_router_interrupt_action_carries_at_text_index():
    """InterruptSignalEvent routing preserves the at_text_index payload (Req 5)."""
    from omnilimb_face.protocol.events import InterruptSignalEvent
    from omnilimb_face.protocol.router import MessageRouter, RouteKind

    action = MessageRouter().route(InterruptSignalEvent(at_text_index=42))

    assert action.kind is RouteKind.INTERRUPT
    assert action.event.at_text_index == 42


def test_router_returns_benign_noop_for_unknown_object():
    """Unexpected/unknown objects yield a benign NOOP action (never raises)."""
    from omnilimb_face.protocol.router import MessageRouter, RouteAction, RouteKind

    router = MessageRouter()

    for bogus in (object(), None, "not-an-event", 123):
        action = router.route(bogus)  # type: ignore[arg-type]
        assert isinstance(action, RouteAction)
        assert action.kind is RouteKind.NOOP
        assert action.ignored is True
        assert action.reason  # non-empty explanation


def test_router_route_is_pure_and_deterministic():
    """Routing the same event twice yields equal, side-effect-free actions."""
    from omnilimb_face.protocol.events import TextInputEvent
    from omnilimb_face.protocol.router import MessageRouter

    router = MessageRouter()
    event = TextInputEvent(text="同样的输入")

    first = router.route(event)
    second = router.route(event)

    assert first == second


# ---------------------------------------------------------------------------
# TTSPlayer ordered playback queue (Task 9.3, Requirements 6.2 / 5.2).
#
# A recording AudioSink captures the order it receives segments. These are
# example-based unit tests; the exhaustive Property 9 (playback order
# preservation) Hypothesis test is owned by Task 9.4.
# ---------------------------------------------------------------------------
import threading


class _RecordingSink:
    """Mock AudioSink that records, in order, the segments handed to it."""

    def __init__(self):
        self.played = []
        self.stopped = 0
        self._lock = threading.Lock()

    def play(self, wav_bytes: bytes) -> None:
        with self._lock:
            self.played.append(wav_bytes)

    def stop(self) -> None:
        with self._lock:
            self.stopped += 1


def _seg(seq: int):
    """Build an AudioSegmentOut whose wav_bytes encodes its text-order seq."""
    from omnilimb_face.tts import AudioSegmentOut

    return AudioSegmentOut(
        wav_bytes=seq.to_bytes(4, "big"),
        volumes=[0.0],
        slice_length_ms=20,
        display_text=f"s{seq}",
        expressions=[],
    )


def _seq_of(wav_bytes: bytes) -> int:
    return int.from_bytes(wav_bytes, "big")


def test_recording_sink_satisfies_audiosink_protocol():
    """A plain recorder structurally satisfies the AudioSink Protocol."""
    from omnilimb_face.tts import AudioSink

    assert isinstance(_RecordingSink(), AudioSink)


def test_enqueue_plays_in_seq_order_despite_shuffled_readiness():
    """Segments play in non-decreasing seq order even when enqueued out of order.

    Readiness (enqueue call) order is the reverse of text order; the sink must
    still receive segments as 0,1,2,3,4 (Requirement 6.2, Property 9 essence).
    """
    from omnilimb_face.tts import TTSPlayer

    sink = _RecordingSink()
    player = TTSPlayer(sink=sink)

    for seq in reversed(range(5)):  # enqueue 4,3,2,1,0
        player.enqueue(_seg(seq), seq=seq)

    assert player.wait_until_idle(timeout=5.0)
    assert [_seq_of(b) for b in sink.played] == [0, 1, 2, 3, 4]
    assert not player.is_playing()


def test_held_back_segment_waits_for_predecessors():
    """An early-arriving later seq is held back until its predecessor arrives."""
    from omnilimb_face.tts import TTSPlayer

    sink = _RecordingSink()
    player = TTSPlayer(sink=sink)

    # seq=1 arrives first and must NOT play before seq=0.
    player.enqueue(_seg(1), seq=1)
    assert not player.wait_until_idle(timeout=0.2)  # gap -> never idle
    assert sink.played == []  # nothing played while seq=0 is missing
    assert player.is_playing()  # pending (held-back) work

    player.enqueue(_seg(0), seq=0)  # predecessor arrives -> both flush in order
    assert player.wait_until_idle(timeout=5.0)
    assert [_seq_of(b) for b in sink.played] == [0, 1]


def test_enqueue_without_seq_uses_submission_order():
    """Omitting seq falls back to submission order within the session."""
    from omnilimb_face.tts import TTSPlayer

    sink = _RecordingSink()
    player = TTSPlayer(sink=sink)

    for seq in range(4):
        player.enqueue(_seg(seq))  # no explicit seq

    assert player.wait_until_idle(timeout=5.0)
    assert [_seq_of(b) for b in sink.played] == [0, 1, 2, 3]


def test_stop_halts_clears_queue_and_is_idempotent():
    """stop() leaves the player idle, drains the queue, and asks the sink to stop.

    Calling stop() repeatedly is a harmless no-op (Requirement 5.2 barge-in).
    """
    from omnilimb_face.tts import TTSPlayer

    sink = _RecordingSink()
    player = TTSPlayer(sink=sink)

    # Hold everything back behind a missing seq=0 so nothing can play.
    for seq in range(1, 6):
        player.enqueue(_seg(seq), seq=seq)
    assert player.is_playing()

    player.stop()
    assert not player.is_playing()
    assert sink.stopped >= 1
    # Idempotent: subsequent stops do not raise and keep the player idle.
    player.stop()
    player.stop()
    assert not player.is_playing()


def test_stop_then_enqueue_starts_fresh_session():
    """A stop()/enqueue cycle (barge-in then a new reply) restarts cleanly."""
    from omnilimb_face.tts import TTSPlayer

    sink = _RecordingSink()
    player = TTSPlayer(sink=sink)

    player.enqueue(_seg(0), seq=0)
    player.enqueue(_seg(1), seq=1)
    assert player.wait_until_idle(timeout=5.0)

    player.stop()

    # New session: cursor resets to 0 and a fresh worker is spawned.
    player.enqueue(_seg(0), seq=0)
    player.enqueue(_seg(1), seq=1)
    assert player.wait_until_idle(timeout=5.0)
    assert [_seq_of(b) for b in sink.played] == [0, 1, 0, 1]


def test_enqueue_rejects_bad_arguments():
    """enqueue validates its inputs (type + non-negative seq)."""
    import pytest

    from omnilimb_face.tts import TTSPlayer

    player = TTSPlayer(sink=_RecordingSink())
    with pytest.raises(TypeError):
        player.enqueue("not-a-segment")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        player.enqueue(_seg(0), seq=-1)


# ---------------------------------------------------------------------------
# ConfigManager.required_secret (Task 3.4, Requirement 2.8): a missing required
# secret must block startup and name the missing key; a present-but-blank value
# is treated as missing; a present non-blank value is available and usable.
#
# These are example-based unit tests. Assertions on the message stay loose: we
# only require that the missing key name appears in it, not its exact wording.
# ---------------------------------------------------------------------------


def test_required_secret_missing_blocks_startup_and_names_key():
    """An absent required secret blocks startup and names the key (Req 2.8)."""
    from omnilimb_face.config import ConfigManager, SecretResolution

    key = "OMNILIMB_FACE_API_KEY"
    result = ConfigManager.required_secret({}, key)

    assert isinstance(result, SecretResolution)
    assert result.key == key
    assert result.available is False
    assert result.blocks_startup is True
    assert result.value is None
    # The message must name the missing key so the operator knows what to set.
    assert key in result.message


def test_required_secret_missing_with_unrelated_env_still_blocks():
    """A populated env that lacks the required key still blocks and names it."""
    from omnilimb_face.config import ConfigManager

    env = {"SOME_OTHER_KEY": "value", "PATH": "/usr/bin"}
    key = "OPENAI_API_KEY"
    result = ConfigManager.required_secret(env, key)

    assert result.available is False
    assert result.blocks_startup is True
    assert result.value is None
    assert key in result.message


def test_required_secret_blank_value_treated_as_missing():
    """An empty-string secret is treated as missing and blocks startup (Req 2.8)."""
    from omnilimb_face.config import ConfigManager

    key = "ELEVENLABS_API_KEY"
    result = ConfigManager.required_secret({key: ""}, key)

    assert result.available is False
    assert result.blocks_startup is True
    assert result.value is None
    assert key in result.message


def test_required_secret_whitespace_value_treated_as_missing():
    """A whitespace-only secret is treated as missing and blocks startup."""
    from omnilimb_face.config import ConfigManager

    key = "AZURE_SPEECH_KEY"
    for blank in ("   ", "\t", "\n", " \t\n "):
        result = ConfigManager.required_secret({key: blank}, key)
        assert result.available is False, f"{blank!r} should count as missing"
        assert result.blocks_startup is True
        assert result.value is None
        assert key in result.message


def test_required_secret_present_value_is_available_and_returned():
    """A present, non-blank secret is available, does not block, and is returned."""
    from omnilimb_face.config import ConfigManager

    key = "OPENAI_API_KEY"
    secret = "sk-live-1234567890"
    result = ConfigManager.required_secret({key: secret}, key)

    assert result.key == key
    assert result.available is True
    assert result.blocks_startup is False
    assert result.value == secret


def test_required_secret_value_with_surrounding_whitespace_is_available():
    """A secret with leading/trailing whitespace around real content is usable.

    The non-blank check uses ``strip()`` to decide presence; the original value
    is preserved verbatim (not trimmed) so callers receive exactly what .env had.
    """
    from omnilimb_face.config import ConfigManager

    key = "DEEPGRAM_API_KEY"
    secret = "  token-with-spaces  "
    result = ConfigManager.required_secret({key: secret}, key)

    assert result.available is True
    assert result.blocks_startup is False
    assert result.value == secret


def test_required_secret_only_resolves_the_requested_key():
    """Resolution targets the requested key alone, ignoring other env entries."""
    from omnilimb_face.config import ConfigManager

    env = {"PRESENT_KEY": "abc", "ANOTHER": "def"}

    present = ConfigManager.required_secret(env, "PRESENT_KEY")
    assert present.available is True
    assert present.value == "abc"

    missing = ConfigManager.required_secret(env, "MISSING_KEY")
    assert missing.available is False
    assert missing.blocks_startup is True
    assert "MISSING_KEY" in missing.message


# ---------------------------------------------------------------------------
# Live2DModelInfo.load_model_info degradation (Task 17.3, Requirement 7.5):
# when the model file is missing or cannot be parsed (bad JSON, or the
# requested model is absent), the loader must NOT raise —it degrades to a
# placeholder (is_placeholder=True) and logs a descriptive error. A valid
# dictionary that contains the requested model loads it fully (positive case).
#
# These are example-based unit tests using pytest's tmp_path for temp files
# and caplog captured at ERROR level on the live2d logger.
# ---------------------------------------------------------------------------
import json
import logging

LIVE2D_LOGGER = "omnilimb_face.live2d"


def test_load_model_info_missing_path_returns_placeholder_and_logs(caplog):
    """A non-existent model_dict_path degrades to a placeholder (Req 7.5).

    The loader must not raise: it returns a placeholder Live2DModelInfo and
    logs a descriptive error naming the missing dictionary and the model.
    """
    from omnilimb_face.live2d import Live2DModelInfo

    missing_path = Path("this") / "path" / "does" / "not" / "exist.json"

    with caplog.at_level(logging.ERROR, logger=LIVE2D_LOGGER):
        info = Live2DModelInfo.load_model_info("default", str(missing_path))

    assert info.is_placeholder is True
    assert info.url == ""
    assert info.emotion_map == {}

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "a descriptive error must be logged for a missing file"
    message = error_records[-1].getMessage()
    assert "missing" in message.lower()
    assert "default" in message  # names the requested model
    assert "placeholder" in message.lower()


def test_load_model_info_invalid_json_returns_placeholder_and_logs(tmp_path, caplog):
    """A model dictionary containing invalid JSON degrades to a placeholder.

    Writing garbage that cannot be parsed as JSON must not raise; the loader
    logs a descriptive parse error and returns a placeholder (Req 7.5).
    """
    from omnilimb_face.live2d import Live2DModelInfo

    bad_file = tmp_path / "model_dict.json"
    bad_file.write_text("{ this is not: valid json ]]] <garbage>", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger=LIVE2D_LOGGER):
        info = Live2DModelInfo.load_model_info("default", str(bad_file))

    assert info.is_placeholder is True
    assert info.url == ""
    assert info.emotion_map == {}

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "a descriptive error must be logged for unparseable JSON"
    message = error_records[-1].getMessage().lower()
    assert "parse" in message or "read" in message
    assert "placeholder" in message


def test_load_model_info_model_not_in_dict_returns_placeholder_and_logs(
    tmp_path, caplog
):
    """A valid dict that lacks the requested model degrades to a placeholder.

    The file parses fine, but the requested model_name has no entry, so the
    loader logs a descriptive "not found" error and returns a placeholder
    (Req 7.5).
    """
    from omnilimb_face.live2d import Live2DModelInfo

    model_dict = tmp_path / "model_dict.json"
    model_dict.write_text(
        json.dumps(
            [
                {
                    "name": "some_other_model",
                    "url": "/live2d-models/other/other.model3.json",
                    "emotionMap": {"neutral": 0, "joy": 3},
                }
            ]
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger=LIVE2D_LOGGER):
        info = Live2DModelInfo.load_model_info("missing_model", str(model_dict))

    assert info.is_placeholder is True
    assert info.url == ""
    assert info.emotion_map == {}

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "a descriptive error must be logged when the model is absent"
    message = error_records[-1].getMessage()
    assert "not found" in message.lower()
    assert "missing_model" in message  # names the requested model


def test_load_model_info_present_model_loads_url_and_emotion_map(tmp_path, caplog):
    """A valid dict containing the requested model loads it fully (positive case).

    The happy path: is_placeholder is False, and url + emotion_map are populated
    from the matching entry. No error is logged.
    """
    from omnilimb_face.live2d import Live2DModelInfo

    expected_url = "/live2d-models/shizuku/shizuku.model3.json"
    expected_emotion_map = {"neutral": 0, "anger": 2, "joy": 3, "sadness": 1}

    model_dict = tmp_path / "model_dict.json"
    model_dict.write_text(
        json.dumps(
            [
                {"name": "decoy", "url": "/x.json", "emotionMap": {"neutral": 0}},
                {
                    "name": "shizuku",
                    "url": expected_url,
                    "emotionMap": expected_emotion_map,
                },
            ]
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger=LIVE2D_LOGGER):
        info = Live2DModelInfo.load_model_info("shizuku", str(model_dict))

    assert info.is_placeholder is False
    assert info.name == "shizuku"
    assert info.url == expected_url
    assert info.emotion_map == expected_emotion_map

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records, "the happy path must not log any error"


# ---------------------------------------------------------------------------
# WakeWord engine-unavailable path (Task 15.3, Requirement 13.5):
# WHERE wake-word activation is ENABLED, IF the detection engine is unavailable
# or failed to initialize, THE Voice_Capture SHALL stop capture, surface an
# error indicating wake-word detection is unavailable, and inject no transcript.
#
# The pure WakeWord gate models that I/O contract as: while enabled and the
# engine is marked unavailable, the gate stays CLOSED (is_gate_open() is False)
# so no transcript can be submitted/injected —even a high-confidence
# "detection" cannot open it, because with no engine there is nothing capable
# of producing a real detection. WakeWordUnavailableError is the importable
# error type the I/O wiring raises on that path; the pure gate itself never
# raises. A contrast case (engine available + a >=threshold detection opens the
# gate) confirms the closed state is specifically due to engine unavailability.
#
# These are example-based unit tests. The exhaustive Property 14 (wake-word
# gating state machine) Hypothesis test is owned by Task 15.2.
# ---------------------------------------------------------------------------


def _enabled_wake_word_settings(**overrides):
    """Build an ENABLED WakeWordSettings (Req 13) via omnilimb_face.config.

    Defaults mirror the design (confidence_threshold=0.7, listen_timeout_s=3.0)
    but enabled is flipped on so the gate is governed by detections / engine
    availability rather than being unconditionally open (Requirement 13.6).
    """
    from omnilimb_face.config import WakeWordSettings

    params = {
        "enabled": True,
        "phrase": "hey hermes",
        "confidence_threshold": 0.7,
        "listen_timeout_s": 3.0,
    }
    params.update(overrides)
    return WakeWordSettings(**params)


def test_wake_word_engine_unavailable_keeps_gate_closed():
    """Enabled + engine unavailable at construction —gate stays closed (Req 13.5).

    With no usable detection engine the gate must report closed, so the
    capture loop submits/injects nothing. A high-confidence "detection" cannot
    open the gate because, without an engine, no real detection can occur.
    """
    from omnilimb_face.voice.wake_word import WakeWord

    cfg = _enabled_wake_word_settings()
    gate = WakeWord(cfg, engine_available=False)

    # Sanity: wake-word activation IS enabled (so the gate is NOT unconditionally
    # open per Req 13.6) and the engine is reported unavailable.
    assert gate.enabled is True
    assert gate.engine_available is False

    # Req 13.5: the gate is closed because the engine is unavailable.
    assert gate.is_gate_open() is False

    # Even a >=threshold "detection" cannot open the gate with no engine.
    opened = gate.observe_detection(0.99)
    assert opened is False
    assert gate.is_gate_open() is False
    assert gate.triggered is False

    # Repeated high-confidence detections still cannot open it.
    assert gate.observe_detection(1.0) is False
    assert gate.is_gate_open() is False


def test_set_engine_unavailable_forces_gate_closed_and_resets_state():
    """Losing the engine after a prior open forces the gate shut and resets (Req 13.5).

    Start enabled with a working engine, open the gate with a qualifying
    detection, then mark the engine unavailable. The gate must immediately
    close (no transcript injected) and the run state must be reset so a later
    recovery starts cleanly from the listening state.
    """
    from omnilimb_face.voice.wake_word import WakeWord

    cfg = _enabled_wake_word_settings()
    gate = WakeWord(cfg, engine_available=True)

    # Open the gate with a qualifying detection (engine available).
    assert gate.observe_detection(0.85) is True
    assert gate.is_gate_open() is True
    assert gate.triggered is True

    # Engine becomes unavailable —gate forced closed and state reset (Req 13.5).
    gate.set_engine_available(False)
    assert gate.engine_available is False
    assert gate.is_gate_open() is False
    assert gate.triggered is False
    assert gate.silence_elapsed_s == 0.0

    # While unavailable, detections remain inert (no transcript can be injected).
    assert gate.observe_detection(0.99) is False
    assert gate.is_gate_open() is False


def test_wake_word_unavailable_error_is_importable_runtime_error():
    """WakeWordUnavailableError is the importable I/O-layer error type (Req 13.5).

    The pure gate never raises it, but the voice subpackage must expose a single
    importable error type the capture/I-O wiring raises when the engine is
    unavailable. Verify it is a subclass of RuntimeError, instantiable, and
    carries a descriptive message indicating wake-word detection is unavailable.
    """
    from omnilimb_face.voice.wake_word import WakeWordUnavailableError

    # Type relationship: a RuntimeError subclass (importable, catchable broadly).
    assert issubclass(WakeWordUnavailableError, RuntimeError)

    # Instances are RuntimeErrors with a descriptive default message.
    err = WakeWordUnavailableError()
    assert isinstance(err, WakeWordUnavailableError)
    assert isinstance(err, RuntimeError)
    message = str(err)
    assert message  # non-empty default message
    assert "wake-word" in message.lower()
    assert "unavailable" in message.lower()

    # A custom message is preserved verbatim for the user-facing error.
    custom = WakeWordUnavailableError("openwakeword failed to initialize")
    assert str(custom) == "openwakeword failed to initialize"


def test_wake_word_pure_gate_never_raises_on_engine_unavailable():
    """The pure gate degrades silently (closed) rather than raising (Req 13.5).

    Contrast with the I/O layer: the deterministic gate must not raise
    WakeWordUnavailableError itself —it just keeps the gate closed across all
    of its observe_* methods so the property test can replay it freely.
    """
    from omnilimb_face.voice.wake_word import WakeWord

    cfg = _enabled_wake_word_settings()
    gate = WakeWord(cfg, engine_available=False)

    # None of the observation methods raise; all report a closed gate.
    assert gate.observe_detection(1.0) is False
    assert gate.observe_silence(0.5) is False
    assert gate.observe_voice_activity() is False
    assert gate.is_gate_open() is False


def test_wake_word_engine_available_detection_opens_gate_contrast():
    """Contrast: enabled + engine available + >=threshold detection opens it (Req 13.1).

    This confirms the closed-state in the unavailable cases above is specifically
    due to engine unavailability (Req 13.5), not because the gate can never open.
    """
    from omnilimb_face.voice.wake_word import WakeWord

    cfg = _enabled_wake_word_settings(confidence_threshold=0.7)
    gate = WakeWord(cfg, engine_available=True)

    # Closed until a qualifying detection arrives (Req 13.2 / 13.3).
    assert gate.is_gate_open() is False

    # A detection at exactly the threshold opens the gate (Req 13.1).
    assert gate.observe_detection(0.7) is True
    assert gate.is_gate_open() is True
    assert gate.triggered is True


def test_wake_word_engine_recovery_reopens_via_new_detection():
    """After recovery, the gate is reopenable by a fresh qualifying detection.

    set_engine_available(True) restores availability but leaves the gate in the
    (reset) listening state; only a new >=threshold detection reopens it. This
    pins the "starts cleanly from listening" behavior implied by Req 13.5.
    """
    from omnilimb_face.voice.wake_word import WakeWord

    cfg = _enabled_wake_word_settings()
    gate = WakeWord(cfg, engine_available=False)

    # Unavailable —closed regardless of detections.
    assert gate.observe_detection(0.95) is False
    assert gate.is_gate_open() is False

    # Recover the engine: still closed (listening) until a new detection.
    gate.set_engine_available(True)
    assert gate.engine_available is True
    assert gate.is_gate_open() is False
    assert gate.triggered is False

    # A fresh qualifying detection reopens the gate.
    assert gate.observe_detection(0.8) is True
    assert gate.is_gate_open() is True


# ---------------------------------------------------------------------------
# ProtocolGateway.parse resilience / "connection stays usable" (Task 2.5,
# Requirement 9.5): WHEN the gateway finishes handling a non-conforming
# message, THE gateway SHALL keep the connection usable for subsequent
# messages.
#
# parse() is a pure function (no socket): it classifies every failure into one
# of the four error codes and NEVER raises, returning ParseOutcome(ok=False,
# event=None, error=...). The transport (Task 20.1) relies on exactly that
# never-raise contract to keep the WebSocket open after a bad frame. We
# therefore model "connection stays usable" as:
#
#   * parse(non-conforming) never raises and yields ok=False with the expected
#     error code and event is None, AND
#   * a subsequent VALID message still parses correctly on the SAME gateway
#     instance (the parser is unaffected by the prior failure), even after a
#     whole sequence of malformed messages.
#
# These are example-based / boundary unit tests. The exhaustive Property 1
# (round-trip) and Property 2 (error classification) Hypothesis tests are owned
# by Tasks 2.3 / 2.4.
# ---------------------------------------------------------------------------

import types as _types


def _gateway_with_limit(max_message_bytes: int):
    """Build a ProtocolGateway whose inbound size cap is ``max_message_bytes``.

    The pure functions read only ``cfg.max_message_bytes``, so a tiny namespace
    is enough to exercise the ``too_large`` path without allocating a real
    1 MiB buffer.
    """
    from omnilimb_face.protocol.gateway import ProtocolGateway

    cfg = _types.SimpleNamespace(max_message_bytes=max_message_bytes)
    return ProtocolGateway(cfg=cfg)


def test_parse_non_json_is_invalid_json_and_does_not_raise():
    """parse(non-JSON) —ok=False, code 'invalid_json', event None, no raise (Req 9.5/9.3)."""
    from omnilimb_face.protocol.gateway import ProtocolGateway

    gateway = ProtocolGateway()

    # A bare word is not valid JSON; parse must classify, not raise.
    outcome = gateway.parse("this is not json")

    assert outcome.ok is False
    assert outcome.event is None
    assert outcome.error is not None
    assert outcome.error.code == "invalid_json"
    assert outcome.error.reason  # carries a descriptive validation reason


def test_parser_still_usable_after_non_conforming_message():
    """A valid message parses correctly right after a malformed one (Req 9.5).

    This is the core "connection still usable" check: the very same gateway
    instance that just rejected garbage must immediately parse a valid
    serialized event back into the original dataclass.
    """
    from omnilimb_face.protocol.events import ParseOutcome, TextInputEvent
    from omnilimb_face.protocol.gateway import ProtocolGateway

    gateway = ProtocolGateway()

    # 1) Handle a non-conforming message (rejected, does not raise).
    bad = gateway.parse("}{ not json")
    assert bad.ok is False
    assert bad.error is not None
    assert bad.error.code == "invalid_json"

    # 2) Immediately parse a valid serialized event on the SAME instance.
    event = TextInputEvent(text="hello after garbage")
    good = gateway.parse(gateway.serialize(event))

    assert good == ParseOutcome(ok=True, event=event)
    assert good.ok is True
    assert good.event == event
    assert good.error is None


def test_mixed_malformed_sequence_then_valid_keeps_gateway_usable():
    """A whole sequence of malformed messages never disables the gateway (Req 9.5).

    On ONE ProtocolGateway instance, feed (in order) an oversize message, an
    invalid-JSON message, an unknown-type message, and a schema-invalid
    message —each must be rejected with the expected error code —then a final
    valid message must parse successfully. No call may raise across the whole
    sequence, mirroring a WebSocket connection that stays open after each bad
    frame (Requirements 9.5 / 9.2 / 9.3 / 9.6 / 9.7).
    """
    from omnilimb_face.protocol.events import InterruptSignalEvent
    from omnilimb_face.protocol.gateway import ProtocolGateway

    limit = 256
    gateway = _gateway_with_limit(limit)

    oversize = '{"type":"text-input","text":"' + ("x" * (limit * 2)) + '"}'
    invalid_json = "}{ definitely not json"
    unknown_type = '{"type":"totally-unknown-type"}'
    schema_invalid = '{"type":"text-input"}'  # missing required "text"

    # Each malformed message —ok=False with the expected classification.
    malformed = [
        (oversize, "too_large"),
        (invalid_json, "invalid_json"),
        (unknown_type, "unsupported_type"),
        (schema_invalid, "schema_invalid"),
    ]
    for raw, expected_code in malformed:
        outcome = gateway.parse(raw)  # must not raise
        assert outcome.ok is False, f"{raw!r} should be rejected"
        assert outcome.event is None
        assert outcome.error is not None
        assert outcome.error.code == expected_code, (
            f"{raw!r} expected {expected_code}, got {outcome.error.code}"
        )

    # After the entire malformed run, a valid message still parses correctly.
    event = InterruptSignalEvent(at_text_index=5)
    final = gateway.parse(gateway.serialize(event))
    assert final.ok is True
    assert final.event == event
    assert final.error is None


def test_repeated_malformed_messages_each_independently_rejected():
    """Repeatedly rejecting bad messages does not degrade later parses (Req 9.5).

    Interleave malformed and valid messages many times on one instance; every
    valid message must still round-trip and every malformed one must still be
    rejected, proving the parser holds no failure state between messages.
    """
    from omnilimb_face.protocol.events import TextInputEvent
    from omnilimb_face.protocol.gateway import ProtocolGateway

    gateway = ProtocolGateway()

    for i in range(10):
        rejected = gateway.parse("not json #%d" % i)
        assert rejected.ok is False
        assert rejected.error is not None
        assert rejected.error.code == "invalid_json"

        event = TextInputEvent(text="message %d" % i)
        accepted = gateway.parse(gateway.serialize(event))
        assert accepted.ok is True
        assert accepted.event == event


def test_parse_edge_cases_never_raise_and_classify_expected_codes():
    """Empty/whitespace/array/typeless/non-string-type inputs classify cleanly (Req 9.5).

    None of these boundary inputs may raise; each must yield ok=False,
    event=None, and the expected error code:

      * empty string / empty bytes / whitespace-only —invalid_json
        (nothing decodes to a JSON value)
      * a JSON array (not an object) —unsupported_type
      * a JSON object without "type" —unsupported_type
      * a "type" that is not a string —unsupported_type
    """
    from omnilimb_face.protocol.gateway import ProtocolGateway

    gateway = ProtocolGateway()

    cases = [
        ("", "invalid_json"),
        (b"", "invalid_json"),
        ("   ", "invalid_json"),
        ("\t\n  \r", "invalid_json"),
        ("[1, 2, 3]", "unsupported_type"),  # valid JSON, but not an object
        ('{"text": "no type field"}', "unsupported_type"),  # object without "type"
        ('{"type": 123}', "unsupported_type"),  # "type" is not a string
        ('{"type": null}', "unsupported_type"),  # "type" is null
        ('{"type": ["text-input"]}', "unsupported_type"),  # "type" is a list
    ]

    for raw, expected_code in cases:
        outcome = gateway.parse(raw)  # must not raise for any of these
        assert outcome.ok is False, f"{raw!r} should be rejected"
        assert outcome.event is None, f"{raw!r} must not produce an event"
        assert outcome.error is not None
        assert outcome.error.code == expected_code, (
            f"{raw!r} expected {expected_code}, got {outcome.error.code}"
        )

    # The gateway is still usable after every edge case above.
    from omnilimb_face.protocol.events import FetchConfigsEvent

    event = FetchConfigsEvent()
    final = gateway.parse(gateway.serialize(event))
    assert final.ok is True
    assert final.event == event


def test_parse_edge_cases_as_bytes_also_classify_and_stay_usable():
    """The same boundary inputs delivered as bytes behave identically (Req 9.5).

    WebSocket frames may arrive as bytes; the byte path must classify the same
    way and never raise, and the gateway must remain usable afterwards.
    """
    from omnilimb_face.protocol.events import TextInputEvent
    from omnilimb_face.protocol.gateway import ProtocolGateway

    gateway = ProtocolGateway()

    byte_cases = [
        (b"   ", "invalid_json"),
        (b"not json", "invalid_json"),
        (b"[1, 2, 3]", "unsupported_type"),
        (b'{"text": "no type"}', "unsupported_type"),
        (b'{"type": 123}', "unsupported_type"),
        (b'{"type": "ghost-type"}', "unsupported_type"),
        (b'{"type": "text-input"}', "schema_invalid"),  # missing "text"
    ]

    for raw, expected_code in byte_cases:
        outcome = gateway.parse(raw)  # must not raise
        assert outcome.ok is False, f"{raw!r} should be rejected"
        assert outcome.event is None
        assert outcome.error is not None
        assert outcome.error.code == expected_code, (
            f"{raw!r} expected {expected_code}, got {outcome.error.code}"
        )

    # Still usable after the byte-path edge cases.
    event = TextInputEvent(text="bytes ok")
    final = gateway.parse(gateway.serialize(event.__class__(text="bytes ok")))
    assert final.ok is True
    assert final.event == event


def test_schema_invalid_variants_are_rejected_then_gateway_recovers():
    """Several schema-invalid shapes are rejected, then a valid one parses (Req 9.5).

    Known message types whose payloads violate the schema (missing required
    field, wrong field type, unknown extra key) must each be classified as
    schema_invalid without raising, and the gateway must keep working.
    """
    from omnilimb_face.protocol.events import MicAudioDataEvent
    from omnilimb_face.protocol.gateway import ProtocolGateway

    gateway = ProtocolGateway()

    schema_invalid_cases = [
        '{"type": "text-input"}',  # missing required "text"
        '{"type": "text-input", "text": 123}',  # wrong type for "text"
        '{"type": "text-input", "text": "hi", "extra": 1}',  # unknown extra key
        '{"type": "mic-audio-data"}',  # missing required "audio"
        '{"type": "mic-audio-data", "audio": "QUJD", "sample_rate": "fast"}',  # bad type
        '{"type": "interrupt-signal", "at_text_index": "five"}',  # bad int field
    ]
    for raw in schema_invalid_cases:
        outcome = gateway.parse(raw)  # must not raise
        assert outcome.ok is False, f"{raw!r} should be rejected"
        assert outcome.event is None
        assert outcome.error is not None
        assert outcome.error.code == "schema_invalid", (
            f"{raw!r} expected schema_invalid, got {outcome.error.code}"
        )

    # Recovery: a well-formed message of a previously-failed type parses fine.
    event = MicAudioDataEvent(audio="QUJD", sample_rate=16000)
    final = gateway.parse(gateway.serialize(event))
    assert final.ok is True
    assert final.event == event


# ---------------------------------------------------------------------------
# STTEngine.transcribe failure / timeout drop path (Task 11.3, Requirement 4.7):
# IF the STT back-end fails while processing a segment, OR does not return a
# result within the configured transcribe timeout (default 10s), THEN the
# VTuber plugin SHALL drop the segment, keep listening, inject NOTHING, and log
# a descriptive transcription-failure error.
#
# The STT_Engine models that contract by NEVER raising out of transcribe():
# every failure (host exception, host {"success": False} envelope, an
# unparseable envelope, or a timeout) is folded into an error TranscribeResult
# (success=False) carrying a descriptive ``error`` and a ``reason`` category
# ("timeout" vs "stt_failed"). The caller (LLM_Bridge, Task 13.1) branches on
# ``success`` to drop the segment without injecting. A positive-contrast case
# (a {"success": True, "transcript": "hello"} envelope) confirms the failure
# results are specifically due to the failure/timeout, not a broken happy path.
#
# These are example-based unit tests. They build a small VoiceSegment with real
# int16 PCM bytes and inject a mock host_transcribe_audio so no real STT
# back-end (or hermes checkout) is required. The timeout test uses a tiny
# configured transcribe_timeout_s (0.05s) so it runs fast.
# ---------------------------------------------------------------------------

import struct
import time


def _pcm_int16(samples=(0, 1000, -1000, 2000, -2000, 0, 512, -512)) -> bytes:
    """Pack a short run of int16 mono samples into little-endian PCM bytes."""
    return struct.pack("<%dh" % len(samples), *samples)


def _voice_segment(pcm: bytes | None = None):
    """Build a small VoiceSegment carrying real int16 PCM for transcribe()."""
    from omnilimb_face.voice.vad import VoiceSegment

    return VoiceSegment(
        pcm=_pcm_int16() if pcm is None else pcm,
        start_ms=0,
        end_ms=200,
        end_reason="silence",
    )


def _stt_settings(**overrides):
    """Build STTSettings (host-reused stt section) via omnilimb_face.config."""
    from omnilimb_face.config import STTSettings

    return STTSettings(**overrides)


def test_transcribe_host_exception_returns_error_result_without_raising():
    """A host back-end that RAISES yields an error result, not an exception (Req 4.7).

    The injected host_transcribe_audio blows up mid-call; transcribe() must
    catch it and return success=False with reason 'stt_failed' and a
    descriptive error, so the caller drops the segment and injects nothing.
    """
    from omnilimb_face.stt import STTEngine, TranscribeResult

    calls = []

    def boom(file_path, model=None):
        calls.append((file_path, model))
        raise RuntimeError("whisper backend exploded")

    engine = STTEngine(_stt_settings(), host_transcribe_audio=boom)

    result = engine.transcribe(_voice_segment())  # must NOT raise

    assert isinstance(result, TranscribeResult)
    assert result.success is False
    assert result.transcript is None  # nothing to inject
    assert result.reason == "stt_failed"
    assert result.error  # descriptive failure message (Req 4.7 logging)
    assert calls, "the host back-end should have been invoked"


def test_transcribe_failure_envelope_returns_error_result():
    """A host {'success': False, 'error': ...} envelope —error result (Req 4.7).

    The back-end returns cleanly but reports failure; transcribe() must surface
    success=False with reason 'stt_failed', no transcript, and carry the host's
    error message verbatim so it can be logged.
    """
    from omnilimb_face.stt import STTEngine

    def failed_envelope(file_path, model=None):
        return {"success": False, "error": "no speech recognized", "provider": "local"}

    engine = STTEngine(_stt_settings(), host_transcribe_audio=failed_envelope)

    result = engine.transcribe(_voice_segment())

    assert result.success is False
    assert result.transcript is None
    assert result.reason == "stt_failed"
    assert result.error == "no speech recognized"  # host message preserved
    assert result.provider == "local"  # informational passthrough


def test_transcribe_timeout_returns_error_result_quickly_without_raising():
    """A back-end slower than the configured timeout —'timeout' error result (Req 4.7).

    With a tiny transcribe_timeout_s (0.05s) and a host that sleeps far longer,
    transcribe() must return an error result with reason 'timeout' WITHOUT
    raising, and must do so promptly (it must not block for the full sleep).
    """
    from omnilimb_face.stt import STTEngine

    slow_sleep_s = 3.0  # far longer than the 0.05s timeout below

    def slowpoke(file_path, model=None):
        time.sleep(slow_sleep_s)
        return {"success": True, "transcript": "too late"}

    engine = STTEngine(
        _stt_settings(transcribe_timeout_s=0.05),
        host_transcribe_audio=slowpoke,
    )

    start = time.perf_counter()
    result = engine.transcribe(_voice_segment())  # must NOT raise
    elapsed = time.perf_counter() - start

    assert result.success is False
    assert result.transcript is None  # nothing to inject
    assert result.reason == "timeout"
    assert result.error  # descriptive timeout message (Req 4.7 logging)
    # It returned promptly: nowhere near the full host sleep.
    assert elapsed < slow_sleep_s / 2.0, (
        f"transcribe() blocked for {elapsed:.3f}s; the timeout guard should "
        f"return well before the {slow_sleep_s:.1f}s back-end sleep"
    )


def test_transcribe_success_envelope_returns_transcript_contrast():
    """Contrast: a {'success': True, 'transcript': 'hello'} envelope —success (Req 4.7).

    Confirms the failure/timeout results above are specifically due to the
    failure path, not a broken happy path: a healthy back-end yields a success
    result whose transcript carries the text and is_empty=False.
    """
    from omnilimb_face.stt import STTEngine

    def ok_envelope(file_path, model=None):
        return {"success": True, "transcript": "hello", "provider": "local"}

    engine = STTEngine(_stt_settings(), host_transcribe_audio=ok_envelope)

    result = engine.transcribe(_voice_segment())

    assert result.success is True
    assert result.error is None
    assert result.reason is None
    assert result.transcript is not None
    assert result.transcript.text == "hello"
    assert result.transcript.is_empty is False
    assert result.provider == "local"


# ---------------------------------------------------------------------------
# LLMBridge no-active-model + reply-timeout paths (Task 13.2, Requirements
# 3.4 / 3.5).
#
# Req 3.4: IF hermes-agent has no active model / valid credentials so the host
#   conversation turn cannot produce a reply, THE plugin SHALL present the
#   host's "no active model" state, SHALL NOT fabricate any reply, and keep the
#   host-owned session context unchanged.
# Req 3.5: IF the host turn fails or produces no text within 30s, THE plugin
#   SHALL terminate this turn's voice/avatar output and log a descriptive
#   reply-generation-failure error.
#
# LLMBridge models these as the importable NoActiveModelError (carrying
# ``context_preserved``) and ReplyTimeoutError (carrying ``elapsed_s`` /
# ``timeout_s``), raised off the host path from ``signal_no_active_model()`` /
# ``conclude_turn()`` and ``check_timeout()`` respectively. ``inject_user_utterance``
# returns ``ctx.inject_message``'s boolean and ``host_turn_available()`` mirrors
# the last inject result (False in gateway / no-CLI mode, Req 11.6 / 11.7).
# ``ctx.llm`` is deliberately never touched on the primary reply path.
#
# These are example-based unit tests using a recording fake ctx (no real host)
# and an injectable fake clock, so the 30s window is exercised deterministically
# and instantly. The exhaustive property tests for the bridge live elsewhere;
# this task pins the documented no-model / timeout semantics by example.
# ---------------------------------------------------------------------------


class _RecordingBridgeCtx:
    """Fake PluginContext recording inject_message calls (Plan A primary path).

    ``inject_message`` records each ``(text, role)`` and returns the configured
    boolean. ``False`` models gateway / non-interactive mode where there is no
    CLI session to inject into, so no host turn is triggered (Req 11.6 / 11.7).
    Accessing ``llm`` flips ``llm_accessed`` so a test can assert the bridge
    never uses ``ctx.llm`` on the primary reply path.
    """

    def __init__(self, inject_result=False):
        self._inject_result = bool(inject_result)
        self.injected = []  # list of (text, role) tuples, in call order
        self.llm_accessed = False

    def inject_message(self, text, role="user"):
        self.injected.append((text, role))
        return self._inject_result

    @property
    def llm(self):  # pragma: no cover - touching this would fail an assertion
        self.llm_accessed = True
        return None


class _FakeClock:
    """Deterministic monotonic clock; tests set/advance ``t`` (seconds)."""

    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


def _make_bridge(ctx, *, clock=None, reply_timeout_s=30.0):
    """Build an LLMBridge with a real SentenceChunker and a dummy config.

    ``cfg`` is stored but unused on the no-model / timeout paths, so ``None`` is
    sufficient; an injectable clock + ``reply_timeout_s`` drive the 30s window.
    """
    from omnilimb_face.chunker import SentenceChunker
    from omnilimb_face.llm_bridge import LLMBridge

    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return LLMBridge(
        ctx,
        None,  # cfg unused on these paths
        SentenceChunker(),
        reply_timeout_s=reply_timeout_s,
        **kwargs,
    )


# --- Requirement 3.4: no active model / gateway no-turn ---------------------


def test_gateway_mode_inject_false_makes_host_turn_unavailable():
    """inject_message False (gateway/no-CLI) —host_turn_available False (Req 3.4).

    In gateway / non-interactive mode ``ctx.inject_message`` returns False (no
    CLI session to inject into), so no host turn is triggered and no voice turn
    is driven. ``host_turn_available()`` must reflect that, the utterance must be
    injected verbatim exactly once (role="user"), nothing must be fabricated,
    and the primary path must never touch ``ctx.llm``.
    """
    ctx = _RecordingBridgeCtx(inject_result=False)
    bridge = _make_bridge(ctx)

    # Before any inject the best-effort availability flag is False.
    assert bridge.host_turn_available() is False

    # Gateway / no-CLI mode: inject_message returns False -> no host turn.
    assert bridge.inject_user_utterance("hello there") is False
    assert bridge.host_turn_available() is False

    # Exactly one inject, role="user", content verbatim; nothing spurious.
    assert ctx.injected == [("hello there", "user")]
    # No reply was fabricated and no text was observed.
    assert bridge.driven_chunks == []
    assert bridge.any_text_seen is False
    # The primary reply path never reads ctx.llm (Plan A keeps it off-path).
    assert ctx.llm_accessed is False


def test_signal_no_active_model_raises_and_preserves_context():
    """signal_no_active_model raises NoActiveModelError, context unchanged (Req 3.4).

    The runtime flags a turn that cannot produce a reply; the bridge raises
    NoActiveModelError carrying ``context_preserved=True``, concludes the turn,
    fabricates no reply, and injects nothing into the session (the host-owned
    context is left untouched).
    """
    import pytest

    from omnilimb_face.llm_bridge import NoActiveModelError

    ctx = _RecordingBridgeCtx(inject_result=False)
    bridge = _make_bridge(ctx)
    bridge.begin_turn()

    with pytest.raises(NoActiveModelError) as excinfo:
        bridge.signal_no_active_model("no model configured")

    err = excinfo.value
    # Req 3.4: the session context owned by the host is left unchanged.
    assert err.context_preserved is True
    # The descriptive detail is preserved for surfacing the host's state.
    assert "no model configured" in str(err)
    # Turn is concluded; no reply fabricated and no message injected.
    assert bridge.turn_active is False
    assert bridge.driven_chunks == []
    assert ctx.injected == []  # the no-active-model path injects nothing
    assert ctx.llm_accessed is False


def test_conclude_turn_without_reply_surfaces_no_active_model():
    """A silent, non-triggerable turn concludes as NoActiveModelError (Req 3.4).

    The utterance is injected in gateway mode (inject returns False, so no host
    turn is triggerable), no text ever flows back, and concluding the turn
    surfaces NoActiveModelError with the context preserved —without fabricating
    or injecting any reply beyond the single user utterance.
    """
    import pytest

    from omnilimb_face.llm_bridge import NoActiveModelError

    ctx = _RecordingBridgeCtx(inject_result=False)
    bridge = _make_bridge(ctx)

    # Attempt to trigger a host turn in gateway mode (returns False).
    assert bridge.inject_user_utterance("どうも") is False
    bridge.begin_turn()

    # No text ever flowed back and no host turn was triggerable.
    with pytest.raises(NoActiveModelError) as excinfo:
        bridge.conclude_turn()

    assert excinfo.value.context_preserved is True
    assert bridge.turn_active is False
    # Only the single user utterance inject; no fabricated reply injected.
    assert ctx.injected == [("どうも", "user")]
    assert bridge.driven_chunks == []
    assert ctx.llm_accessed is False


def test_no_active_model_error_carries_context_preserved_flag():
    """NoActiveModelError is importable and carries context_preserved (Req 3.4)."""
    from omnilimb_face.llm_bridge import LLMBridgeError, NoActiveModelError

    # Subclass of the bridge base error (and RuntimeError) —broadly catchable.
    assert issubclass(NoActiveModelError, LLMBridgeError)
    assert issubclass(NoActiveModelError, RuntimeError)

    # Default carries context_preserved=True and a descriptive default message.
    err = NoActiveModelError()
    assert err.context_preserved is True
    assert str(err)  # non-empty default message

    # An explicit message is preserved verbatim; the flag stays set.
    custom = NoActiveModelError("offline", context_preserved=True)
    assert custom.context_preserved is True
    assert str(custom) == "offline"


# --- Requirement 3.5: 30s no-text reply timeout -----------------------------


def test_check_timeout_raises_reply_timeout_after_window_with_no_text():
    """30s elapse with no text —ReplyTimeoutError terminates the turn (Req 3.5).

    Just before the window elapses no timeout fires; once the configured 30s
    pass with NO text observed, ``check_timeout`` raises ReplyTimeoutError
    carrying the observed ``elapsed_s`` and configured ``timeout_s``, marks the
    turn inactive (voice/avatar output terminated), and fabricates nothing.
    """
    import pytest

    from omnilimb_face.llm_bridge import ReplyTimeoutError

    ctx = _RecordingBridgeCtx(inject_result=True)
    clock = _FakeClock(0.0)
    bridge = _make_bridge(ctx, clock=clock, reply_timeout_s=30.0)

    bridge.begin_turn()  # the no-text window starts at t=0

    # Just before the window elapses: no timeout fires, turn stays active.
    clock.t = 29.9
    bridge.check_timeout()  # must NOT raise
    assert bridge.turn_active is True

    # Advance beyond the 30s window with NO text observed.
    clock.t = 31.0
    with pytest.raises(ReplyTimeoutError) as excinfo:
        bridge.check_timeout()

    err = excinfo.value
    assert err.timeout_s == 30.0
    assert err.elapsed_s == pytest.approx(31.0)
    # The turn's voice/avatar output is terminated; nothing was fabricated.
    assert bridge.turn_active is False
    assert bridge.driven_chunks == []
    assert bridge.any_text_seen is False


def test_check_timeout_fires_at_exact_window_boundary():
    """At exactly the timeout the window has elapsed and fires (>= boundary)."""
    import pytest

    from omnilimb_face.llm_bridge import ReplyTimeoutError

    ctx = _RecordingBridgeCtx(inject_result=True)
    clock = _FakeClock(0.0)
    bridge = _make_bridge(ctx, clock=clock, reply_timeout_s=30.0)
    bridge.begin_turn()

    clock.t = 30.0  # exactly the timeout: the >= boundary fires
    with pytest.raises(ReplyTimeoutError) as excinfo:
        bridge.check_timeout()

    assert excinfo.value.elapsed_s == pytest.approx(30.0)
    assert excinfo.value.timeout_s == 30.0
    assert bridge.turn_active is False


def test_text_arriving_within_window_suppresses_timeout():
    """Text observed within the window —no timeout ever fires (Req 3.5 / 3.3).

    A reply fragment flows back well within the window: playback begins, the
    first-text deadline (5s) is met, and the host's sentence is driven
    downstream. Even long after the 30s window, ``check_timeout`` is a no-op
    because text was observed.
    """
    ctx = _RecordingBridgeCtx(inject_result=True)
    clock = _FakeClock(0.0)
    bridge = _make_bridge(ctx, clock=clock, reply_timeout_s=30.0)
    bridge.begin_turn()

    # A text fragment flows back well within the window.
    clock.t = 2.0
    assert bridge.on_llm_output("Hello there.") is None  # observer returns None
    assert bridge.any_text_seen is True
    assert bridge.playback_started is True
    assert bridge.first_text_within_deadline() is True  # within 5s (Req 3.3)
    # The host's own sentence is driven downstream (observed, not fabricated).
    assert [chunk.text for chunk in bridge.driven_chunks] == ["Hello there."]

    # Even far beyond the 30s window, no timeout fires once text has arrived.
    clock.t = 100.0
    bridge.check_timeout()  # must NOT raise
    assert bridge.turn_active is True


def test_check_timeout_is_noop_without_an_active_turn():
    """With no active turn, check_timeout is a harmless no-op (never raises)."""
    ctx = _RecordingBridgeCtx(inject_result=True)
    clock = _FakeClock(1000.0)
    bridge = _make_bridge(ctx, clock=clock, reply_timeout_s=30.0)

    # No begin_turn() -> no active turn; the check must do nothing.
    bridge.check_timeout()  # must NOT raise
    assert bridge.turn_active is False


def test_conclude_turn_raises_timeout_when_host_available_but_silent():
    """Host reachable but no text within window —conclude_turn times out (Req 3.5).

    A host turn is triggerable (inject returns True), but no text flows back
    before the window elapses, so concluding the turn raises ReplyTimeoutError
    carrying ``elapsed_s`` / ``timeout_s`` and terminates the turn.
    """
    import pytest

    from omnilimb_face.llm_bridge import ReplyTimeoutError

    ctx = _RecordingBridgeCtx(inject_result=True)
    clock = _FakeClock(0.0)
    bridge = _make_bridge(ctx, clock=clock, reply_timeout_s=30.0)

    assert bridge.inject_user_utterance("hi") is True  # host turn triggerable
    assert bridge.host_turn_available() is True
    bridge.begin_turn()

    clock.t = 45.0  # host reachable but produced no text within the window
    with pytest.raises(ReplyTimeoutError) as excinfo:
        bridge.conclude_turn()

    assert excinfo.value.timeout_s == 30.0
    assert excinfo.value.elapsed_s == pytest.approx(45.0)
    assert bridge.turn_active is False
    assert bridge.driven_chunks == []


def test_reply_timeout_error_carries_elapsed_and_timeout():
    """ReplyTimeoutError is importable and carries elapsed_s / timeout_s (Req 3.5)."""
    from omnilimb_face.llm_bridge import LLMBridgeError, ReplyTimeoutError

    assert issubclass(ReplyTimeoutError, LLMBridgeError)
    assert issubclass(ReplyTimeoutError, RuntimeError)

    err = ReplyTimeoutError(elapsed_s=42.0, timeout_s=30.0)
    assert err.elapsed_s == 42.0
    assert err.timeout_s == 30.0
    assert str(err)  # non-empty default message


def test_default_reply_timeout_window_is_30_seconds():
    """The documented default no-text window is 30 seconds (Req 3.5).

    Built WITHOUT an explicit ``reply_timeout_s``, the bridge must not time out
    just under 30s but must fire at 30s, pinning the default window.
    """
    import pytest

    from omnilimb_face.chunker import SentenceChunker
    from omnilimb_face.llm_bridge import LLMBridge, ReplyTimeoutError

    ctx = _RecordingBridgeCtx(inject_result=True)
    clock = _FakeClock(0.0)
    # Construct WITHOUT reply_timeout_s -> documented default of 30s.
    bridge = LLMBridge(ctx, None, SentenceChunker(), clock=clock)
    bridge.begin_turn()

    clock.t = 29.5
    bridge.check_timeout()  # within the default window: no raise
    assert bridge.turn_active is True

    clock.t = 30.0
    with pytest.raises(ReplyTimeoutError) as excinfo:
        bridge.check_timeout()
    assert excinfo.value.timeout_s == 30.0


# ---------------------------------------------------------------------------
# Task 18.4 —register(ctx) capability set + core-file invariance + plugin
# structure-validity contract (Requirements 1.2, 1.3, 1.8).
#
# Req 1.2: WHEN the plugin loader scans the plugin directory, register(ctx)
#   SHALL register the plugin's full surface —its hooks, tools, CLI subcommand
#   and slash commands.
# Req 1.3: the plugin SHALL register ONLY through ctx's extension points and
#   SHALL NOT write to or modify any core file, so every core file is byte-for
#   -byte identical before and after the plugin is installed/registered.
# Req 1.8: IF the plugin directory lacks plugin.yaml or __init__.py, OR the
#   __init__.py does not define register(ctx), THEN the loader SHALL NOT load
#   the plugin and SHALL report a structure-invalid error.
#
# These are example-based unit tests:
#   * Registration set —driven through a *local* recording fake ctx defined
#     right here (the integration suite has its own recorder, but tests must
#     not import across test modules), asserting register(ctx) registers
#     EXACTLY the documented surface (design.md -> "Plugin Entry Point").
#   * Core-file invariance —snapshot a representative hermes-agent core file
#     (and the other named core files when present) by sha256 + mtime + size
#     BEFORE and AFTER calling register(ctx), and assert nothing changed; the
#     representative file is hermes_cli/plugins.py. When no core file is present
#     in this environment the byte-level assertion is skipped gracefully.
#   * Structure-validity —a small local helper mirroring the loader's contract
#     (plugin.yaml + __init__.py defining register) validates the project's own
#     root as the positive case and rejects synthesized, hermetic tmp_path dirs
#     that violate the contract as the negative cases. It deliberately does NOT
#     depend on the real hermes PluginManager.
# ---------------------------------------------------------------------------

import ast
import hashlib

# Repo root: this file lives at <root>/tests/test_units.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# The complete capability surface register(ctx) is expected to register
# (design.md -> "Plugin Entry Point: register(ctx)"): four lifecycle/LLM-output
# hooks, two tools, one CLI subcommand and two slash commands.
_REG_EXPECTED_HOOKS = {
    "on_session_start",
    "on_session_end",
    "transform_llm_output",
    "post_llm_call",
}
_REG_EXPECTED_TOOLS = {"vtuber_status", "vtuber_say"}
_REG_EXPECTED_CLI_COMMANDS = {"vtuber"}
_REG_EXPECTED_SLASH_COMMANDS = {"vtuber", "handsfree"}

# The hermes-agent core files the plugin must never touch (Req 1.3 / glossary
# "Core_Files"). hermes_cli/plugins.py is the representative file named by the
# task; the rest are snapshotted too when present for a stronger invariant.
_NAMED_CORE_FILES = (
    "hermes_cli/plugins.py",  # representative (the loader / PluginManager)
    "run_agent.py",
    "cli.py",
    "gateway/run.py",
    "hermes_cli/main.py",
)


class _RegRecordingCtx:
    """A local recording stand-in for the host PluginContext (Req 1.2).

    Records every registration call register(ctx) makes through the generic
    extension surface, implementing exactly the four registration methods the
    plugin uses (register_hook / register_tool / register_cli_command /
    register_command) plus an empty ``config`` dict so ConfigManager.from_host
    resolves to documented defaults without touching a real hermes host. It is
    intentionally minimal and defined here (NOT imported from another test
    module) so this unit test is self-contained.
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

    def hook_names(self) -> set:
        return {name for name, _ in self.hooks}

    def tool_names(self) -> set:
        return {t["name"] for t in self.tools}

    def cli_command_names(self) -> set:
        return {c["name"] for c in self.cli_commands}

    def slash_command_names(self) -> set:
        return {c["name"] for c in self.slash_commands}


# --- Requirement 1.2: register(ctx) registers exactly the documented surface --


def test_register_records_exactly_the_four_lifecycle_and_llm_hooks():
    """register(ctx) registers exactly the 4 hooks, once each (Req 1.2).

    on_session_start / on_session_end are the lifecycle hooks; the LLM-output
    observers transform_llm_output / post_llm_call drive TTS/Live2D. Each must
    be registered exactly once with a callable handler.
    """
    from omnilimb_face.plugin import register

    ctx = _RegRecordingCtx()
    register(ctx)

    assert len(ctx.hooks) == 4
    assert ctx.hook_names() == _REG_EXPECTED_HOOKS
    # Every hook handler is callable, and no hook is registered twice.
    assert all(callable(handler) for _name, handler in ctx.hooks)
    assert len(ctx.hook_names()) == 4


def test_register_records_two_vtuber_tools_with_handlers_and_check_fn():
    """register(ctx) registers the 2 tools under toolset "vtuber" (Req 1.2 / 12).

    Both vtuber_status and vtuber_say are registered with a callable ``handler``
    AND a callable ``check_fn`` (the availability gate that keeps the tool
    visible while reflecting degraded state), under the "vtuber" toolset.
    """
    from omnilimb_face.plugin import register

    ctx = _RegRecordingCtx()
    register(ctx)

    assert len(ctx.tools) == 2
    assert ctx.tool_names() == _REG_EXPECTED_TOOLS

    by_name = {t["name"]: t for t in ctx.tools}
    for name in _REG_EXPECTED_TOOLS:
        tool = by_name[name]
        assert tool["toolset"] == "vtuber"
        assert callable(tool["handler"]), f"{name} must have a callable handler"
        assert callable(tool["check_fn"]), f"{name} must have a callable check_fn"
        # The schema names the tool consistently with its registration name.
        assert tool["schema"]["name"] == name


def test_register_records_one_cli_command_and_two_slash_commands():
    """register(ctx) registers the 1 CLI subcommand + 2 slash commands (Req 1.2).

    The CLI subcommand is ``hermes vtuber ...`` (with callable setup/handler
    functions); the slash commands are /vtuber and /handsfree (each with a
    callable handler).
    """
    from omnilimb_face.plugin import register

    ctx = _RegRecordingCtx()
    register(ctx)

    # Exactly one CLI subcommand: vtuber, with callable setup_fn + handler_fn.
    assert len(ctx.cli_commands) == 1
    assert ctx.cli_command_names() == _REG_EXPECTED_CLI_COMMANDS
    cli = ctx.cli_commands[0]
    assert callable(cli["setup_fn"])
    assert callable(cli["handler_fn"])

    # Exactly two slash commands: /vtuber and /handsfree, callable handlers.
    assert len(ctx.slash_commands) == 2
    assert ctx.slash_command_names() == _REG_EXPECTED_SLASH_COMMANDS
    assert all(callable(c["handler"]) for c in ctx.slash_commands)


def test_register_registers_the_complete_surface_in_one_call():
    """One register(ctx) call registers the entire documented surface (Req 1.2).

    A single call must yield exactly 4 hooks + 2 tools + 1 CLI command + 2 slash
    commands and nothing more —the full contract in aggregate.
    """
    from omnilimb_face.plugin import register

    ctx = _RegRecordingCtx()
    assert register(ctx) is None  # entry point returns None

    assert ctx.hook_names() == _REG_EXPECTED_HOOKS
    assert ctx.tool_names() == _REG_EXPECTED_TOOLS
    assert ctx.cli_command_names() == _REG_EXPECTED_CLI_COMMANDS
    assert ctx.slash_command_names() == _REG_EXPECTED_SLASH_COMMANDS
    # No extra registrations beyond the documented surface.
    assert (len(ctx.hooks), len(ctx.tools), len(ctx.cli_commands), len(ctx.slash_commands)) == (
        4,
        2,
        1,
        2,
    )


# --- Requirement 1.3: core files are byte-for-byte unchanged by register -----


def _find_hermes_agent_root():
    """Locate the hermes-agent checkout robustly, or return None.

    Prefers the sibling layout (omnilimb-face and hermes-agent live side by side
    under the same parent) so the test is portable, then falls back to the
    conventional ~/AppData/Local/hermes/hermes-agent location and an optional
    HERMES_AGENT_ROOT override. Returns None when none exists, so the byte-level
    invariance assertion can be skipped gracefully rather than failing in an
    environment without the core checkout.
    """
    candidates = [
        _REPO_ROOT.parent / "hermes-agent",
        Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent",
    ]
    env_root = os.environ.get("HERMES_AGENT_ROOT")
    if env_root:
        candidates.insert(0, Path(env_root))
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _snapshot_core_files(agent_root: Path) -> dict:
    """Snapshot each present named core file by (sha256, mtime_ns, size).

    Reads the file as raw BYTES and hashes with sha256, capturing the
    modification time and size too, so any in-place rewrite (even one that
    preserves length) is detected. Absent files are skipped; the returned dict
    is keyed by the relative core-file path.
    """
    snapshot: dict = {}
    for rel in _NAMED_CORE_FILES:
        path = agent_root / rel
        if not path.is_file():
            continue
        raw = path.read_bytes()
        stat = path.stat()
        snapshot[rel] = (
            hashlib.sha256(raw).hexdigest(),
            stat.st_mtime_ns,
            stat.st_size,
        )
    return snapshot


def test_register_does_not_modify_hermes_agent_core_files():
    """register(ctx) leaves hermes-agent core files byte-for-byte unchanged (Req 1.3).

    Snapshot the representative core file (hermes_cli/plugins.py) and the other
    named core files by sha256 + mtime + size BEFORE calling register(ctx), run
    register against a recording ctx, then re-snapshot and assert nothing under
    the hermes-agent tree changed —proving register only touches ctx, never any
    core file. If no core file is present in this environment, the byte-level
    assertion is skipped gracefully rather than failing.
    """
    import pytest

    from omnilimb_face.plugin import register

    agent_root = _find_hermes_agent_root()
    if agent_root is None:
        pytest.skip(
            "hermes-agent core checkout not present in this environment; "
            "cannot verify core-file byte-invariance (Req 1.3)"
        )

    before = _snapshot_core_files(agent_root)
    if not before:
        pytest.skip(
            f"no named hermes-agent core file present under {agent_root}; "
            "cannot verify core-file byte-invariance (Req 1.3)"
        )

    # The representative file named by the task must be among those snapshotted.
    representative = "hermes_cli/plugins.py"
    representative_present = representative in before

    # Run the full registration against a recording ctx (touches ctx only).
    ctx = _RegRecordingCtx()
    register(ctx)
    # Sanity: register actually did its work (so the invariance below is meaningful).
    assert ctx.tool_names() == _REG_EXPECTED_TOOLS

    after = _snapshot_core_files(agent_root)

    # Byte-for-byte (sha256), mtime and size are all unchanged for every core
    # file: register made no filesystem writes under the hermes-agent tree.
    assert after == before, "register(ctx) must not modify any hermes-agent core file"

    if representative_present:
        # Explicit per-file check on the representative loader file (read bytes,
        # sha256, mtime) to pin the task's named target precisely.
        assert after[representative][0] == before[representative][0], (
            "hermes_cli/plugins.py content (sha256) changed"
        )
        assert after[representative][1] == before[representative][1], (
            "hermes_cli/plugins.py mtime changed (an in-place write occurred)"
        )


# --- Requirement 1.8: plugin structure-validity contract ---------------------


def _init_defines_register(source: str) -> bool:
    """Whether an __init__.py source binds a top-level ``register`` symbol.

    Mirrors the loader contract ("__init__.py defines register(ctx)") WITHOUT
    executing the module: parses the source with ast and treats ``register`` as
    defined when it is a top-level function definition, an imported name (e.g.
    ``from omnilimb_face.plugin import register``), or a top-level assignment —
    any of which makes ``module.register`` resolvable the way the host does.
    Malformed Python is treated as "does not define register".
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "register":
                return True
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == "register":
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == "register":
                    return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "register":
                    return True
    return False


def _plugin_structure_is_valid(plugin_dir: Path) -> tuple[bool, str]:
    """Validate a plugin directory against the loader's structure contract (Req 1.8).

    Returns ``(True, "")`` when the directory contains BOTH a plugin.yaml
    manifest AND an __init__.py that defines ``register``; otherwise returns
    ``(False, reason)`` with a descriptive structure-invalid reason naming the
    missing/!defective piece —the information the loader surfaces to the user
    when it refuses to load a structurally-invalid plugin. This local helper
    deliberately mirrors the contract instead of importing the real hermes
    PluginManager, keeping the test hermetic.
    """
    if not plugin_dir.is_dir():
        return False, f"plugin path is not a directory: {plugin_dir}"

    manifest = plugin_dir / "plugin.yaml"
    init = plugin_dir / "__init__.py"

    if not manifest.is_file():
        return False, "structure invalid: missing plugin.yaml manifest"
    if not init.is_file():
        return False, "structure invalid: missing __init__.py"
    if not _init_defines_register(init.read_text(encoding="utf-8")):
        return False, "structure invalid: __init__.py does not define register"
    return True, ""


def test_project_root_satisfies_plugin_structure_contract():
    """Positive case: the project's own root is a valid plugin dir (Req 1.1 / 1.8).

    The shipped plugin directory must contain plugin.yaml AND a root __init__.py
    that defines register (it re-exports omnilimb_face.plugin.register), so the
    structure-validity contract reports it valid with no error reason.
    """
    valid, reason = _plugin_structure_is_valid(_REPO_ROOT)

    assert valid is True, f"the shipped plugin dir must be valid, got: {reason}"
    assert reason == ""
    # Spell out the two required artifacts for clarity.
    assert (_REPO_ROOT / "plugin.yaml").is_file()
    assert (_REPO_ROOT / "__init__.py").is_file()
    assert _init_defines_register(
        (_REPO_ROOT / "__init__.py").read_text(encoding="utf-8")
    )


def test_missing_plugin_yaml_is_structure_invalid(tmp_path):
    """A dir with a valid __init__.py but NO plugin.yaml is invalid (Req 1.8)."""
    (tmp_path / "__init__.py").write_text(
        "def register(ctx):\n    return None\n", encoding="utf-8"
    )
    # No plugin.yaml written.

    valid, reason = _plugin_structure_is_valid(tmp_path)

    assert valid is False
    assert "plugin.yaml" in reason
    assert "invalid" in reason.lower()


def test_missing_init_is_structure_invalid(tmp_path):
    """A dir with plugin.yaml but NO __init__.py is invalid (Req 1.8)."""
    (tmp_path / "plugin.yaml").write_text("name: omnilimb-face\n", encoding="utf-8")
    # No __init__.py written.

    valid, reason = _plugin_structure_is_valid(tmp_path)

    assert valid is False
    assert "__init__.py" in reason
    assert "invalid" in reason.lower()


def test_init_without_register_is_structure_invalid(tmp_path):
    """A dir whose __init__.py does NOT define register is invalid (Req 1.8).

    Both required files are present, but the __init__.py never binds a
    ``register`` symbol, so the loader would refuse it with a descriptive
    structure-invalid error.
    """
    (tmp_path / "plugin.yaml").write_text("name: omnilimb-face\n", encoding="utf-8")
    (tmp_path / "__init__.py").write_text(
        "# this module forgot to define register\n"
        "def setup(ctx):\n    return None\n",
        encoding="utf-8",
    )

    valid, reason = _plugin_structure_is_valid(tmp_path)

    assert valid is False
    assert "register" in reason
    assert "invalid" in reason.lower()


def test_synthesized_valid_plugin_dir_passes_contract(tmp_path):
    """Contrast: a synthesized dir with both files + register is valid (Req 1.8).

    Confirms the negative cases above fail specifically because of the missing/
    defective piece, not because the helper can never validate a dir: a hermetic
    tmp_path dir carrying plugin.yaml AND an __init__.py that defines register
    (via either a function def or a re-export import) is accepted.
    """
    # Variant A: __init__.py defines register as a function.
    dir_a = tmp_path / "plugin_a"
    dir_a.mkdir()
    (dir_a / "plugin.yaml").write_text("name: omnilimb-face\n", encoding="utf-8")
    (dir_a / "__init__.py").write_text(
        "def register(ctx):\n    return None\n", encoding="utf-8"
    )
    valid_a, reason_a = _plugin_structure_is_valid(dir_a)
    assert valid_a is True, reason_a
    assert reason_a == ""

    # Variant B: __init__.py binds register via a re-export import (the shape the
    # real root __init__.py uses).
    dir_b = tmp_path / "plugin_b"
    dir_b.mkdir()
    (dir_b / "plugin.yaml").write_text("name: omnilimb-face\n", encoding="utf-8")
    (dir_b / "__init__.py").write_text(
        "from omnilimb_face.plugin import register  # noqa: F401\n", encoding="utf-8"
    )
    valid_b, reason_b = _plugin_structure_is_valid(dir_b)
    assert valid_b is True, reason_b
    assert reason_b == ""

# ---------------------------------------------------------------------------
# TTSPlayer.synthesize retry count + final-degrade fallback (Task 12.2,
# Requirements 6.4 / 6.5).
#
# Req 6.4: WHEN host TTS synthesis fails, THE TTS_Player SHALL retry up to
#   cfg.max_attempts times (default 3 = first attempt + 2 retries), each bounded
#   by cfg.synth_timeout_s.
# Req 6.5: IF every attempt fails, THE TTS_Player SHALL NOT raise —it returns a
#   failed SynthResult (success=False, segment=None) carrying a descriptive
#   ``error`` and a ``reason`` category, so the caller can fall back to showing
#   the reply as plain text and keep already-displayed content. (The caller's
#   text fallback lives in runtime.tool_say / Task 22.1; here we only assert
#   synthesize signals failure cleanly.)
#
# These are example-based unit tests using a MOCK dispatch_tool that counts its
# invocations (thread-safe, because synthesize runs each attempt on a worker
# thread). A persistent failure envelope drives the retry-count + degrade path;
# a flip-to-success mock proves the player is not wedged after a failed call;
# and a success envelope pointing at a tiny real temp WAV (written with the
# stdlib ``wave`` module) is the positive contrast (exactly one attempt, a
# populated AudioSegmentOut with peak-normalized lip-sync volumes). A short
# synth_timeout_s keeps the failing paths fast and hang-proof.
#
# The exhaustive Property 9 (playback order) / Property 10 (volume
# normalization) Hypothesis tests are owned by Tasks 9.4 / 9.2; this task pins
# the retry/degrade contract by example.
# ---------------------------------------------------------------------------

import wave as _wave


class _CountingDispatch:
    """Mock host ``dispatch_tool`` that counts invocations (thread-safe).

    ``synthesize`` runs each attempt on a worker thread, so the call counter is
    guarded by a lock. ``responder(call_index, tool_name, payload)`` decides the
    envelope returned for each call, letting a test make the tool always fail,
    always succeed, or flip between ``synthesize`` calls.
    """

    def __init__(self, responder):
        self._responder = responder
        self._lock = threading.Lock()
        self.calls = []  # (tool_name, payload) per invocation, in call order

    def __call__(self, tool_name, payload):
        with self._lock:
            index = len(self.calls)
            recorded = dict(payload) if isinstance(payload, dict) else payload
            self.calls.append((tool_name, recorded))
        return self._responder(index, tool_name, payload)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)


def _tts_settings(**overrides):
    """Build TTSSettings (host-reused tts section) via omnilimb_face.config."""
    from omnilimb_face.config import TTSSettings

    return TTSSettings(**overrides)


def _write_temp_wav(path, samples, sample_rate=16000):
    """Write a tiny REAL int16 mono WAV with the stdlib ``wave`` module.

    Returns the path so the caller can read the raw bytes back for assertions.
    """
    with _wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # 16-bit PCM
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return path


# --- Requirement 6.4: retry count is exactly cfg.max_attempts ---------------


def test_synthesize_attempts_exactly_max_attempts_on_persistent_failure():
    """A persistently-failing tool is attempted EXACTLY max_attempts times (Req 6.4).

    With max_attempts=3 and a dispatch_tool that ALWAYS returns a failure
    envelope, synthesize must invoke the tool exactly 3 times (first attempt +
    2 retries) and return a failed SynthResult WITHOUT raising.
    """
    from omnilimb_face.tts import SynthResult, TTSPlayer

    def always_fail(index, tool_name, payload):
        assert tool_name == "text_to_speech"  # the host TTS tool name
        assert payload == {"text": "Hello world."}  # sentence passed through
        return {"success": False, "error": "provider unavailable"}

    dispatch = _CountingDispatch(always_fail)
    player = TTSPlayer(
        cfg=_tts_settings(max_attempts=3, synth_timeout_s=2.0),
        dispatch_tool=dispatch,
    )

    result = player.synthesize("Hello world.")  # must NOT raise

    # Exactly first attempt + 2 retries == 3 dispatch invocations (Req 6.4).
    assert dispatch.count == 3
    assert isinstance(result, SynthResult)
    assert result.success is False
    assert result.segment is None
    assert result.reason == "tts_failed"
    assert result.error  # descriptive (asserted in detail by the degrade test)


def test_synthesize_honors_a_custom_max_attempts_value():
    """The retry budget is driven by cfg.max_attempts, not hard-coded (Req 6.4).

    A single-attempt configuration must invoke the tool exactly once before
    degrading, confirming the count above is genuinely config-driven.
    """
    from omnilimb_face.tts import TTSPlayer

    def always_fail(index, tool_name, payload):
        return {"success": False, "error": "still unavailable"}

    dispatch = _CountingDispatch(always_fail)
    player = TTSPlayer(
        cfg=_tts_settings(max_attempts=1, synth_timeout_s=2.0),
        dispatch_tool=dispatch,
    )

    result = player.synthesize("Only one shot.")

    assert dispatch.count == 1  # no retries when max_attempts == 1
    assert result.success is False
    assert result.reason == "tts_failed"


# --- Requirement 6.5: final degrade signals failure cleanly -----------------


def test_synthesize_failure_signals_clean_degrade_for_text_fallback():
    """After the budget is exhausted, synthesize degrades cleanly (Req 6.5).

    The failed SynthResult must carry success=False, segment=None, a set
    ``reason`` and a descriptive ``error`` so the caller (runtime.tool_say /
    Task 22.1) can show the reply as plain text and keep already-displayed
    content. synthesize itself must never raise.
    """
    from omnilimb_face.tts import TTSPlayer

    def always_fail(index, tool_name, payload):
        return {"success": False, "error": "no audio device"}

    dispatch = _CountingDispatch(always_fail)
    player = TTSPlayer(
        cfg=_tts_settings(max_attempts=3, synth_timeout_s=2.0),
        dispatch_tool=dispatch,
    )

    result = player.synthesize("The reply the caller will show as plain text.")

    # Clean failure signal: the caller branches on success and falls back.
    assert result.success is False
    assert result.segment is None
    assert result.reason == "tts_failed"
    assert isinstance(result.error, str) and result.error
    # Descriptive: names that synthesis failed and reflects the attempt budget,
    # and preserves the last underlying failure for diagnosis.
    assert "3 attempt" in result.error
    assert "no audio device" in result.error


def test_synthesize_recovers_on_next_call_after_failed_call(tmp_path):
    """A failed call does not wedge the player; a later call still succeeds (Req 6.5).

    The mock fails for the first synthesize call (exhausting all 3 attempts),
    then flips to success returning a real temp WAV. The next synthesize call
    must succeed on its first attempt —proving the player is not left in a
    broken state after a final degrade.
    """
    from omnilimb_face.tts import AudioSegmentOut, TTSPlayer

    wav_path = _write_temp_wav(
        tmp_path / "recovered.wav",
        [0, 8000, -8000, 16000, -16000, 4000],
    )
    state = {"fail": True}

    def flip(index, tool_name, payload):
        if state["fail"]:
            return {"success": False, "error": "warming up"}
        return {"success": True, "file_path": str(wav_path), "provider": "edge"}

    dispatch = _CountingDispatch(flip)
    player = TTSPlayer(
        cfg=_tts_settings(max_attempts=3, synth_timeout_s=2.0),
        dispatch_tool=dispatch,
    )

    first = player.synthesize("first try")
    assert first.success is False
    assert first.segment is None
    assert dispatch.count == 3  # the failing call exhausted the budget

    # Not wedged: once the tool recovers, the very next call succeeds.
    state["fail"] = False
    second = player.synthesize("second try")
    assert second.success is True
    assert isinstance(second.segment, AudioSegmentOut)
    assert second.segment.display_text == "second try"
    assert dispatch.count == 4  # exactly one more attempt (succeeded immediately)


# --- Success contrast: a valid envelope + real WAV —one attempt, a segment --


def test_synthesize_success_single_attempt_with_real_wav(tmp_path):
    """A valid envelope pointing at a real WAV —exactly 1 attempt, success (Req 6.4/6.1).

    Confirms the failure/degrade cases above are specifically due to the failure
    path, not a broken happy path: a healthy tool yields success in a single
    attempt with a populated AudioSegmentOut whose lip-sync volumes are computed
    (peak-normalized to [0, 1]).
    """
    import pytest

    from omnilimb_face.tts import AudioSegmentOut, TTSPlayer

    samples = [0, 8000, -8000, 16000, -16000, 12000, -12000, 0]
    wav_path = _write_temp_wav(tmp_path / "voice.wav", samples, sample_rate=16000)
    file_bytes = wav_path.read_bytes()

    def succeed(index, tool_name, payload):
        assert tool_name == "text_to_speech"
        return {"success": True, "file_path": str(wav_path), "provider": "edge"}

    dispatch = _CountingDispatch(succeed)
    player = TTSPlayer(cfg=_tts_settings(max_attempts=3), dispatch_tool=dispatch)

    result = player.synthesize("Speak this sentence.")

    assert dispatch.count == 1  # no retries on the happy path
    assert result.success is True
    assert result.error is None
    assert result.reason is None
    assert result.provider == "edge"

    seg = result.segment
    assert isinstance(seg, AudioSegmentOut)
    # The raw synthesised file bytes are carried verbatim for the gateway.
    assert seg.wav_bytes == file_bytes
    assert seg.display_text == "Speak this sentence."
    assert seg.expressions == []  # Expression_Mapper (Task 22.1) fills these in
    # compute_volumes ran: a non-empty, peak-normalized series in [0.0, 1.0].
    assert seg.volumes
    assert all(0.0 <= v <= 1.0 for v in seg.volumes)
    assert max(seg.volumes) == pytest.approx(1.0)
    assert seg.slice_length_ms == TTSPlayer.DEFAULT_SLICE_LENGTH_MS


# ---------------------------------------------------------------------------
# VoiceCapture microphone-unavailable handling (Task 14.4, Requirements 4.9 /
# 12.3).
#
# Req 4.9: WHEN hands-free mode is requested and the microphone is unavailable
#   (absent, in use, or permission denied), THE Voice_Capture SHALL NOT activate
#   hands-free, SHALL surface a prompt indicating the microphone is unavailable,
#   and SHALL keep text and rendering available (the failure is contained —the
#   call returns cleanly, it does not raise).
# Req 12.3: IF the microphone becomes unavailable at runtime (the device
#   disappears mid-capture), THE Voice_Capture SHALL record a descriptive error
#   and turn hands-free OFF without crashing.
#
# These are example-based unit tests built on a FAKE AudioSource (no real mic and
# no optional [voice] dependency) wired into a real VoiceCapture together with a
# real VadSegmenter (constructed from the config VADSettings). The fake lets each
# scenario script device enumeration, the start() outcome, and the frames()
# stream independently:
#
#   * mic absent              -> list_input_devices() == []  (never starts)
#   * device present but start raises MicrophoneUnavailableError (in use / denied)
#   * runtime mic-loss        -> frames() raises mid-run after activation
#
# The exhaustive Property 13 (microphone availability gating) Hypothesis test is
# owned by Task 14.3; this task pins the unavailable-prompt + runtime-loss
# contract by example.
# ---------------------------------------------------------------------------

import time as _time


class _FakeAudioSource:
    """A scriptable, mic-less AudioSource for VoiceCapture gating tests.

    Structurally satisfies the ``AudioSource`` contract VoiceCapture depends on
    (``list_input_devices`` / ``start`` / ``stop`` / ``frames``) without any real
    microphone or the optional ``[voice]`` dependency. Each scenario configures:

    * ``devices`` —what ``list_input_devices()`` reports (``[]`` == no mic);
    * ``start_error`` —an exception ``start()`` raises (device in use / denied);
    * ``frames_factory`` —a zero-arg callable returning an iterable of
      :class:`AudioFrame` for ``frames()`` to yield from (it may itself raise
      mid-iteration to simulate the device disappearing at runtime).

    It also records ``start_calls`` and ``stop_calls`` so tests can assert the
    source was never started when the gate refuses, and was released on the
    runtime-loss path.
    """

    def __init__(self, devices, *, start_error=None, frames_factory=None):
        self._devices = list(devices)
        self._start_error = start_error
        self._frames_factory = frames_factory
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0

    def list_input_devices(self):
        # Return a fresh copy so callers can never mutate our scripted list.
        return list(self._devices)

    def start(self) -> None:
        self.start_calls += 1
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.started = False

    def frames(self):
        # A generator (yield below): when no factory is given it simply yields
        # nothing and returns, ending the consumer loop cleanly.
        if self._frames_factory is not None:
            yield from self._frames_factory()


def _voice_capture_config():
    """Build a default VTuberConfig (its .vad drives the real VadSegmenter)."""
    from omnilimb_face.config import VTuberConfig

    return VTuberConfig()


def _build_voice_capture(source):
    """Wire a real VoiceCapture around a fake source + a real VadSegmenter."""
    from omnilimb_face.voice.capture import VoiceCapture
    from omnilimb_face.voice.vad import VadSegmenter

    cfg = _voice_capture_config()
    vad = VadSegmenter(cfg.vad)
    return VoiceCapture(cfg, source, vad)


def _wait_until(predicate, timeout=5.0, interval=0.01) -> bool:
    """Poll ``predicate`` until true or ``timeout`` (seconds) elapses."""
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(interval)
    return predicate()


CAPTURE_LOGGER = "omnilimb_face.voice.capture"


# --- Requirement 4.9: microphone absent (no input devices enumerated) --------


def test_start_hands_free_no_devices_does_not_activate_and_prompts():
    """No enumerated input device —hands-free does NOT activate (Req 4.9).

    The fake reports an empty device list, so the gate must refuse: the returned
    StartResult is not activated / not success, carries a descriptive
    microphone-unavailable message (in both ``reason`` and ``error``), and
    is_running() stays False. The source is never started, and the call returns
    cleanly (no raise) so text and rendering remain available.
    """
    from omnilimb_face.voice.capture import StartResult

    source = _FakeAudioSource(devices=[])  # no microphone present
    capture = _build_voice_capture(source)

    result = capture.start_hands_free()  # must NOT raise

    assert isinstance(result, StartResult)
    assert result.activated is False
    assert result.success is False
    assert capture.is_running() is False
    # The source must never be started when no device is enumerated.
    assert source.start_calls == 0
    assert source.started is False

    # A descriptive prompt indicating the microphone is unavailable, carried on
    # both the human-readable reason and the error field.
    assert result.reason
    assert result.error == result.reason
    reason = result.reason.lower()
    assert "microphone" in reason
    assert "unavailable" in reason
    # The failure is contained: text and rendering remain available.
    assert "text and rendering remain available" in reason


# --- Requirement 4.9: device present but start() raises (in use / denied) ----


def test_start_hands_free_device_present_but_start_raises_is_contained():
    """A present device whose start() raises —contained, unavailable result (Req 4.9).

    Models a device that is enumerated (in use / permission denied) but cannot be
    opened: list_input_devices() reports it, yet start() raises
    MicrophoneUnavailableError. start_hands_free must catch it, return a
    not-activated microphone-unavailable StartResult WITHOUT raising, and leave
    is_running() False.
    """
    from omnilimb_face.voice.capture import (
        MicrophoneUnavailableError,
        StartResult,
    )

    source = _FakeAudioSource(
        devices=["Built-in Microphone"],
        start_error=MicrophoneUnavailableError(
            "device is already in use by another application"
        ),
    )
    capture = _build_voice_capture(source)

    result = capture.start_hands_free()  # must NOT raise despite start() failing

    assert isinstance(result, StartResult)
    assert result.activated is False
    assert result.success is False
    assert capture.is_running() is False
    # start() was attempted exactly once (the device was enumerated) and failed.
    assert source.start_calls == 1
    assert source.started is False

    assert result.reason
    assert result.error == result.reason
    reason = result.reason.lower()
    assert "microphone" in reason
    assert "unavailable" in reason
    # The underlying cause is surfaced for diagnosis.
    assert "already in use" in reason
    # Contained failure: text and rendering remain available.
    assert "text and rendering remain available" in reason


# --- Requirement 12.3: microphone lost at runtime (frames() raises mid-run) --


def test_runtime_microphone_loss_turns_handsfree_off_and_logs(caplog):
    """A mid-run source failure turns hands-free OFF and records an error (Req 12.3).

    The fake activates normally (a device is present and start() succeeds) but
    its frames() yields one benign frame and then raises
    MicrophoneUnavailableError, simulating the device disappearing mid-capture.
    The consumer loop must catch it, log a descriptive error, and turn hands-free
    OFF (is_running() becomes False) WITHOUT crashing the caller. The source is
    released (stop() called) on the way down.
    """
    import logging

    from omnilimb_face.voice.capture import MicrophoneUnavailableError
    from omnilimb_face.voice.vad import AudioFrame

    silent_frame = AudioFrame(pcm=b"\x00\x00" * 160, ts_ms=0)

    def _frames_then_lose_device():
        # One benign (silent) frame proves we got mid-run, then the device dies.
        yield silent_frame
        raise MicrophoneUnavailableError("input device disconnected mid-capture")

    source = _FakeAudioSource(
        devices=["USB Microphone"],
        frames_factory=_frames_then_lose_device,
    )
    capture = _build_voice_capture(source)

    with caplog.at_level(logging.ERROR, logger=CAPTURE_LOGGER):
        result = capture.start_hands_free()
        # Activation itself succeeds: a device was present and start() worked.
        assert result.activated is True
        assert result.success is True

        # The background consumer hits the mid-run failure and turns off
        # hands-free; wait for is_running() to flip to False (Req 12.3).
        assert _wait_until(lambda: capture.is_running() is False), (
            "hands-free must turn OFF after a runtime microphone loss"
        )

    assert capture.is_running() is False
    # The source was released as part of the graceful shutdown.
    assert _wait_until(lambda: source.stop_calls >= 1)

    # A descriptive error was recorded (Req 12.3): names the microphone loss and
    # preserves the underlying cause for diagnosis.
    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name == CAPTURE_LOGGER
    ]
    assert error_records, "a descriptive error must be logged on runtime mic loss"
    message = error_records[-1].getMessage().lower()
    assert "microphone" in message
    assert "unavailable" in message
    assert "input device disconnected mid-capture" in message

    # The caller never saw an exception: stop_hands_free remains safe/idempotent.
    capture.stop_hands_free()
    assert capture.is_running() is False


# --- Positive contrast: a present, openable device activates hands-free ------


def test_start_hands_free_activates_when_device_present_and_openable():
    """Contrast: an enumerated, openable device activates hands-free (Req 4.9).

    Confirms the refusals above are specifically due to microphone
    unavailability, not because the gate can never activate. With a device
    present and a start() that succeeds (and a frames() stream that simply ends),
    start_hands_free returns an activated/success result with an empty
    reason/error and is_running() is True until stopped.
    """
    from omnilimb_face.voice.capture import StartResult

    source = _FakeAudioSource(devices=["Built-in Microphone"])  # frames() empty
    capture = _build_voice_capture(source)

    result = capture.start_hands_free()

    assert isinstance(result, StartResult)
    assert result.activated is True
    assert result.success is True
    assert result.error is None
    assert result.reason == ""
    assert capture.is_running() is True
    assert source.start_calls == 1

    # Clean shutdown turns it off and releases the source.
    capture.stop_hands_free()
    assert capture.is_running() is False
    assert source.stop_calls >= 1


# ---------------------------------------------------------------------------
# InterruptionController disabled + device-failure paths (Task 21.2,
# Requirements 5.4 / 5.6).
#
# These are example-based unit tests over the I/O wiring of
# ``InterruptionController.arm`` / ``feed_vad_event`` / ``signal_detection_failure``
# (Task 21.1), exercised with fake collaborators (a TTS player that records
# stop() calls, an LLM bridge that records turn-abort calls, and a VoiceCapture
# that records VAD subscription + new-segment requests). They pin two refusal
# paths plus a positive contrast:
#
#   * Interruption DISABLED (Req 5.5/5.4): while armed and fed continuous speech
#     that crosses the barge-in threshold, playback is NEVER stopped, no turn is
#     aborted, and the on_interrupt hook never fires —so the "after interrupt,
#     capture resumes" path is only taken when interruption is enabled.
#   * Mic/VAD FAILURE during playback (Req 5.6): a malformed event (feed raises)
#     or an explicit signal_detection_failure() stops detection (controller
#     disarmed, barge-in unavailable), leaves the current playback UNINTERRUPTED
#     (tts.stop() not called as a result of the failure), and surfaces an error
#     (on_error hook fired + last_error set).
#   * Positive contrast (Req 5.2/5.3/5.4): ENABLED + continuous speech crossing
#     the threshold DOES stop playback, abort the bridge turn, ready a fresh
#     capture segment, and fire on_interrupt exactly once —confirming the
#     refusals above are specifically due to those conditions.
#
# The exhaustive Property 7 (barge-in decision) Hypothesis test is owned by
# Task 7.2; these tests target the side-effecting I/O wiring instead.
# ---------------------------------------------------------------------------


class _FakeTTSPlayerForInterrupt:
    """Fake TTSPlayer recording stop() calls (barge-in must stop playback)."""

    def __init__(self):
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeBridgeForInterrupt:
    """Fake LLMBridge recording host-turn aborts (Requirement 5.3).

    Exposes only ``abort_turn`` —the first method the controller tries in
    ``_BRIDGE_ABORT_METHODS`` —so the abort path is unambiguous and easy to
    assert against.
    """

    def __init__(self):
        self.abort_calls = 0

    def abort_turn(self) -> None:
        self.abort_calls += 1


class _FakeInterruptCapture:
    """Fake VoiceCapture recording VAD subscription + new-segment requests.

    Implements the optional ``subscribe_vad_events`` / ``unsubscribe_vad_events``
    hooks (so ``arm`` / ``disarm`` exercise the real subscription wiring) and the
    ``begin_new_segment`` hook the controller invokes after a confirmed barge-in
    (Requirement 5.4).
    """

    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self.new_segments = 0

    def subscribe_vad_events(self, callback) -> None:
        self.subscribed.append(callback)

    def unsubscribe_vad_events(self, callback) -> None:
        self.unsubscribed.append(callback)

    def begin_new_segment(self) -> None:
        self.new_segments += 1


def _build_interruption_controller(*, enabled, threshold_ms=200, on_interrupt=None,
                                   on_error=None):
    """Construct an InterruptionController wired with the interrupt fakes.

    Returns ``(controller, tts, bridge, capture)``. The barge-in threshold is
    supplied via a real ``VADSettings`` so the wiring matches the design
    signature (``vad_settings=VADSettings(...)``).
    """
    from omnilimb_face.config import InterruptionSettings, VADSettings
    from omnilimb_face.interruption import InterruptionController

    tts = _FakeTTSPlayerForInterrupt()
    bridge = _FakeBridgeForInterrupt()
    capture = _FakeInterruptCapture()
    controller = InterruptionController(
        InterruptionSettings(enabled=enabled),
        tts=tts,
        bridge=bridge,
        capture=capture,
        vad_settings=VADSettings(barge_in_min_speech_ms=threshold_ms),
        on_interrupt=on_interrupt,
        on_error=on_error,
    )
    return controller, tts, bridge, capture


def _speech_event(ts_ms: int):
    """A continuous-speech VAD event at ``ts_ms`` (extends the speech run)."""
    from omnilimb_face.voice.vad import VadEvent

    return VadEvent(kind="speech", ts_ms=ts_ms, rms=0.9)


def test_interruption_disabled_never_stops_playback_or_fires_interrupt():
    """Disabled barge-in never stops playback even past threshold (Req 5.4/5.5).

    With ``InterruptionSettings(enabled=False)``, arming and then feeding a
    continuous run of speech that clearly crosses the 200 ms threshold must
    leave the agent's playback running: ``tts.stop()`` is never called, the host
    turn is never aborted, the on_interrupt hook never fires, and capture is
    never readied for a new segment (the "after interrupt, capture resumes" path
    is gated behind interruption being enabled).
    """
    interrupts = []
    controller, tts, bridge, capture = _build_interruption_controller(
        enabled=False,
        threshold_ms=200,
        on_interrupt=lambda: interrupts.append(True),
    )

    controller.arm()
    assert controller.armed is True
    assert controller.enabled is False

    # Feed a continuous speech run whose accumulated duration crosses 200 ms.
    last_decision = None
    for i in range(8):
        last_decision = controller.feed_vad_event(_speech_event(i * 100))

    # The accumulator DID cross the threshold —so the only reason no
    # interruption happened is that barge-in is disabled (Req 5.5).
    assert last_decision is not None
    assert last_decision.accumulated_speech_ms >= 200
    assert last_decision.should_interrupt is False

    # Playback continues (Req 5.4): nothing was stopped/aborted, no hook fired,
    # and capture was never readied for a fresh user segment.
    assert tts.stop_calls == 0
    assert bridge.abort_calls == 0
    assert interrupts == []
    assert controller.interruption_count == 0
    assert capture.new_segments == 0

    # Detection is still healthy and listening (it was never torn down).
    assert controller.armed is True
    assert controller.barge_in_available is True
    assert controller.last_error is None


def test_signal_detection_failure_stops_detection_keeps_playback_surfaces_error():
    """Mic/VAD failure stops detection, keeps playback, surfaces error (Req 5.6).

    Arming with interruption ENABLED and then signalling a detection failure
    must tear down barge-in detection (disarmed + barge-in unavailable) while
    leaving the current playback UNINTERRUPTED (``tts.stop()`` is not called as a
    result of the failure) and surfacing a descriptive error via the on_error
    hook + ``last_error``.
    """
    errors = []
    controller, tts, bridge, capture = _build_interruption_controller(
        enabled=True,
        on_error=lambda exc: errors.append(exc),
    )

    controller.arm()
    assert controller.armed is True
    assert controller.barge_in_available is True

    controller.signal_detection_failure()

    # Detection STOPS (Req 5.6): the controller is disarmed and barge-in is
    # reported unavailable.
    assert controller.armed is False
    assert controller.barge_in_available is False

    # The current playback is NOT stopped by the failure (Req 5.6) and the host
    # turn is not aborted.
    assert tts.stop_calls == 0
    assert bridge.abort_calls == 0

    # An error is surfaced: the on_error hook fired exactly once and last_error
    # is recorded for the user-facing "barge-in unavailable" message.
    assert len(errors) == 1
    assert isinstance(errors[0], Exception)
    assert controller.last_error is errors[0]

    # The capture VAD subscription made by arm() was torn down on failure.
    assert len(capture.unsubscribed) == 1


def test_signal_detection_failure_preserves_custom_error_instance():
    """An explicit failure cause is preserved verbatim on last_error (Req 5.6)."""
    errors = []
    controller, tts, bridge, _capture = _build_interruption_controller(
        enabled=True,
        on_error=lambda exc: errors.append(exc),
    )
    controller.arm()

    boom = RuntimeError("input device disconnected during playback")
    controller.signal_detection_failure(boom)

    assert controller.last_error is boom
    assert errors == [boom]
    # Playback still untouched by the failure path.
    assert tts.stop_calls == 0
    assert bridge.abort_calls == 0
    assert controller.barge_in_available is False
    assert controller.armed is False


def test_feed_malformed_event_tears_down_detection_and_keeps_playback():
    """A malformed event during playback fails safe (Req 5.6).

    When ``feed_vad_event`` is handed a malformed event whose decision raises,
    the controller treats it as a VAD/detection failure: it tears down detection
    (disarmed + barge-in unavailable), leaves playback running (``tts.stop()``
    not called), surfaces the error, and returns ``None`` rather than raising.
    """
    errors = []
    controller, tts, bridge, _capture = _build_interruption_controller(
        enabled=True,
        on_error=lambda exc: errors.append(exc),
    )

    controller.arm()
    assert controller.armed is True

    # A bare object() has no ``.kind`` —on_vad_event raises, which the I/O
    # wrapper catches and converts into a detection failure.
    result = controller.feed_vad_event(object())  # type: ignore[arg-type]

    assert result is None
    assert controller.armed is False
    assert controller.barge_in_available is False
    assert controller.last_error is not None

    # Playback is left uninterrupted by the failure (Req 5.6).
    assert tts.stop_calls == 0
    assert bridge.abort_calls == 0
    assert len(errors) == 1


def test_enabled_continuous_speech_stops_playback_and_aborts_turn():
    """Contrast: enabled + threshold crossing DOES interrupt (Req 5.2/5.3/5.4).

    Confirms the disabled/failure refusals above are specifically due to those
    conditions. With interruption ENABLED, feeding a continuous speech run that
    crosses the 200 ms threshold stops playback, aborts the host turn, readies a
    fresh capture segment, and fires on_interrupt exactly once.
    """
    interrupts = []
    controller, tts, bridge, capture = _build_interruption_controller(
        enabled=True,
        threshold_ms=200,
        on_interrupt=lambda: interrupts.append(True),
    )

    controller.arm()

    decisions = []
    for i in range(4):  # ts = 0, 100, 200, 300 -> crosses 200 ms at ts=200
        decisions.append(controller.feed_vad_event(_speech_event(i * 100)))

    # An interruption was confirmed once continuous speech reached the threshold.
    assert any(d is not None and d.should_interrupt for d in decisions)

    # Playback stopped (Req 5.2), the host turn aborted (Req 5.3), a fresh
    # capture segment readied (Req 5.4), and the hook fired exactly once.
    assert tts.stop_calls >= 1
    assert bridge.abort_calls >= 1
    assert capture.new_segments >= 1
    assert interrupts == [True]
    assert controller.interruption_count == 1

    # Detection remains healthy/armed after a normal barge-in (no failure).
    assert controller.armed is True
    assert controller.barge_in_available is True
    assert controller.last_error is None


# ---------------------------------------------------------------------------
# VTuberRuntime lifecycle + CLI command unit tests (Task 19.3, Requirements
# 10.2, 10.4, 10.6).
#
# These exercise the real VTuberRuntime through a fake ctx plus the runtime's
# own overridable subsystem factories (``_frontend_server_factory`` /
# ``_protocol_gateway_factory`` / ``_voice_capture_factory``) and injectable
# ``_clock`` —so NO real sockets, microphone or front-end window are ever
# touched and no background threads leak. Each lifecycle test that starts a
# session also ends it (or starts/ends through fakes that never spawn threads).
# ---------------------------------------------------------------------------

import argparse as _argparse


class _LifecycleCtx:
    """Minimal fake PluginContext for the lifecycle / CLI tests.

    Exposes a callable ``dispatch_tool`` (so the TTS path probes as *available*
    via :meth:`VTuberRuntime.tts_available`) and an ``inject_message`` that
    returns ``False`` (gateway-style: never triggers a real host turn). The
    runtime only ever reads this generic extension surface.
    """

    def __init__(self):
        self.dispatched = []  # (name, args) tuples, in call order

    def dispatch_tool(self, name, args=None, **kwargs):
        self.dispatched.append((name, args))
        return "{}"

    def inject_message(self, text, role="user"):
        return False


class _FakeFrontendServer:
    """Front-end static server fake recording ``start()`` / ``stop()``.

    ``stop_raises=True`` makes release fail, to prove the runtime's per-resource
    isolation (Requirement 10.4) and partial-init cleanup (Requirement 10.2).
    """

    def __init__(self, stop_raises=False):
        self.start_calls = 0
        self.stop_calls = 0
        self._stop_raises = stop_raises

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1
        if self._stop_raises:
            raise RuntimeError("frontend stop boom")


class _FakeProtocolGateway:
    """``/client-ws`` gateway fake recording ``start_in_thread()`` / ``stop()``.

    The runtime starts the gateway via ``start_in_thread`` and releases it via
    ``stop`` (see ``_init_avatar_subsystem`` / ``_release_protocol_gateway``).
    No real thread is ever started.
    """

    def __init__(self, stop_raises=False):
        self.start_calls = 0
        self.stop_calls = 0
        self._stop_raises = stop_raises

    def start_in_thread(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1
        if self._stop_raises:
            raise RuntimeError("gateway stop boom")


class _ScriptedClock:
    """Monotonic clock returning a scripted sequence, then holding the last value.

    Lets a test drive ``VTuberRuntime._check_budget`` deterministically (so the
    5 s start budget can be tripped without hard-sleeping).
    """

    def __init__(self, values):
        self._values = list(values)
        self._last = self._values[-1] if self._values else 0.0

    def __call__(self):
        if self._values:
            self._last = self._values.pop(0)
        return self._last


def _make_runtime(ctx=None):
    """Build a real VTuberRuntime over a fake ctx + documented-default config."""
    from omnilimb_face.config import VTuberConfig
    from omnilimb_face.runtime import VTuberRuntime

    return VTuberRuntime(
        ctx=ctx if ctx is not None else _LifecycleCtx(),
        config=VTuberConfig(),
    )


# --- Requirement 10.2: partial init releases exactly what was allocated ------


def test_on_session_start_partial_init_releases_allocated_frontend():
    """A failed subsystem init releases the already-allocated one (Req 10.2).

    The front-end server is allocated/started first, then the protocol gateway
    factory raises. The runtime must unwind to its single cleanup path, leave
    the plugin not-running, record a descriptive lifecycle error, and RELEASE
    exactly the resource it had allocated so far —the front-end server's
    ``stop()`` is called (no leak), and nothing more.
    """
    runtime = _make_runtime()
    server = _FakeFrontendServer()

    def _gateway_boom():
        raise RuntimeError("gateway init boom")

    runtime._frontend_server_factory = lambda: server
    runtime._protocol_gateway_factory = _gateway_boom
    # Force text-only voice degrade so no real microphone stack is probed.
    runtime._missing_voice = ["sounddevice"]

    runtime.on_session_start()

    # Not running, and the error is descriptive (names the failure cause).
    assert runtime._running is False
    assert runtime._last_lifecycle_error
    assert "gateway init boom" in runtime._last_lifecycle_error

    # The front-end server was allocated/started and then RELEASED (Req 10.2):
    # partial init releases exactly what was allocated, nothing more, nothing
    # double-released.
    assert server.start_calls == 1
    assert server.stop_calls == 1
    assert runtime._allocated == []


def test_on_session_start_budget_overrun_releases_allocated_resources():
    """Exceeding the 5 s start budget releases everything allocated (Req 10.2).

    Budget-overrun variant: both avatar resources start successfully, but the
    injectable ``_clock`` reports >5 s elapsed at the post-init budget check, so
    ``_check_budget`` trips. The runtime must release every resource it had
    allocated (front-end server + gateway both get ``stop()``), stay
    not-running, and record a descriptive (budget) error.
    """
    runtime = _make_runtime()
    server = _FakeFrontendServer()
    gateway = _FakeProtocolGateway()
    runtime._frontend_server_factory = lambda: server
    runtime._protocol_gateway_factory = lambda: gateway
    runtime._missing_voice = ["sounddevice"]  # text-only voice degrade
    # started_at -> 0.0; the avatar-subsystem budget check sees 6.0 -> overrun.
    runtime._clock = _ScriptedClock([0.0, 6.0])

    runtime.on_session_start()

    assert runtime._running is False
    assert runtime._last_lifecycle_error
    assert "budget" in runtime._last_lifecycle_error.lower()

    # Both allocated avatar resources were released exactly once.
    assert server.stop_calls == 1
    assert gateway.stop_calls == 1
    assert runtime._allocated == []


# --- Requirement 10.4: session end attempts all releases + names failures ----


def test_on_session_end_partial_failure_attempts_all_and_names_failure():
    """One failing release does not block the others; the failure is named (Req 10.4).

    A session is started with two controllable fake resources (front-end server
    + gateway) via the overridable factories. The gateway's ``stop()`` raises on
    release. ``on_session_end`` must still attempt BOTH releases (the healthy
    front-end server is released anyway), set the plugin stopped, and record a
    summary that names the resource that failed to release.
    """
    runtime = _make_runtime()
    server = _FakeFrontendServer()
    gateway = _FakeProtocolGateway(stop_raises=True)  # this one fails to release
    runtime._frontend_server_factory = lambda: server
    runtime._protocol_gateway_factory = lambda: gateway
    runtime._missing_voice = ["sounddevice"]  # text-only voice degrade

    runtime.on_session_start()
    # Both avatar subsystems came up -> running, with exactly the two fakes
    # recorded in the allocation registry.
    assert runtime._running is True
    assert {r.name for r in runtime._allocated} == {
        "frontend_server",
        "protocol_gateway",
    }

    runtime.on_session_end()

    # Stopped regardless of release outcomes (Req 10.4).
    assert runtime._running is False

    # BOTH releases were attempted even though the gateway raised.
    assert gateway.stop_calls == 1
    assert server.stop_calls == 1

    # The summary names the resource that failed to release.
    assert runtime._last_session_end_summary is not None
    assert "protocol_gateway" in runtime._last_session_end_summary
    assert "fail" in runtime._last_session_end_summary.lower()

    # Handles cleared and registry emptied (no double release).
    assert runtime._allocated == []
    assert runtime._protocol_gateway is None
    assert runtime._frontend_server is None


# --- Requirement 10.6: CLI start/stop/status/doctor return promptly ----------


def test_handle_cli_status_and_doctor_return_non_empty_strings_quickly():
    """``status`` / ``doctor`` return non-empty strings well under 2 s (Req 10.6)."""
    import time as _t

    runtime = _make_runtime()

    for action in ("status", "doctor"):
        start = _t.monotonic()
        result = runtime.handle_cli(_argparse.Namespace(action=action))
        elapsed = _t.monotonic() - start

        assert isinstance(result, str)
        assert result.strip()  # non-empty feedback
        assert elapsed < 2.0, f"{action} took {elapsed:.3f}s (must be < 2s)"


def test_handle_cli_start_then_stop_reflects_state_promptly():
    """``start`` then ``stop`` return status strings reflecting state (Req 10.6).

    Both calls must return promptly (well under the 2 s feedback budget) and
    never raise; ``start`` must flip the plugin to running, ``stop`` must flip it
    back to stopped, releasing the started subsystem fakes (no leaked threads —
    the fakes never spawn any).
    """
    import time as _t

    runtime = _make_runtime()
    server = _FakeFrontendServer()
    gateway = _FakeProtocolGateway()
    runtime._frontend_server_factory = lambda: server
    runtime._protocol_gateway_factory = lambda: gateway
    runtime._missing_voice = ["sounddevice"]  # text-only voice degrade

    t0 = _t.monotonic()
    start_msg = runtime.handle_cli(_argparse.Namespace(action="start"))
    start_elapsed = _t.monotonic() - t0

    assert isinstance(start_msg, str) and start_msg.strip()
    assert start_elapsed < 2.0, f"start took {start_elapsed:.3f}s (must be < 2s)"
    assert runtime._running is True
    assert "start" in start_msg.lower()  # 'started'

    t1 = _t.monotonic()
    stop_msg = runtime.handle_cli(_argparse.Namespace(action="stop"))
    stop_elapsed = _t.monotonic() - t1

    assert isinstance(stop_msg, str) and stop_msg.strip()
    assert stop_elapsed < 2.0, f"stop took {stop_elapsed:.3f}s (must be < 2s)"
    assert runtime._running is False
    assert "stop" in stop_msg.lower()  # 'stopped'

    # The started subsystem fakes were released exactly once (no leak).
    assert server.stop_calls == 1
    assert gateway.stop_calls == 1


def test_handle_cli_unknown_action_returns_non_empty_string_quickly():
    """An unknown CLI action still returns useful, non-empty feedback (Req 10.6)."""
    import time as _t

    runtime = _make_runtime()

    start = _t.monotonic()
    result = runtime.handle_cli(_argparse.Namespace(action="frobnicate"))
    elapsed = _t.monotonic() - start

    assert isinstance(result, str) and result.strip()
    assert elapsed < 2.0


def test_slash_vtuber_status_returns_string():
    """``/vtuber status`` returns a human-readable status string (Req 10.6)."""
    runtime = _make_runtime()

    result = runtime.slash_vtuber("status")

    assert isinstance(result, str)
    assert result.strip()
    assert "omnilimb-face" in result.lower()
