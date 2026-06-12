"""Property-based tests for the omnilimb-face plugin (Hypothesis).

This module hosts the 14 named correctness properties defined in design.md.
Each property is implemented by exactly one Hypothesis test, annotated with a
``# Feature: open-llm-vtuber-plugin, Property {n}: ...`` comment and a
``Validates: 需求 ...`` reference, and runs at least 100 random examples via
the ``vtuber`` Hypothesis profile registered in ``tests/conftest.py``.

Populated by later tasks (e.g. Property 1/2 -> protocol gateway, Property 3/4
-> config merge, etc.). Intentionally empty of tests at the scaffolding stage;
it must import and collect cleanly under pytest.
"""
from __future__ import annotations

import collections

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from omnilimb_face.config import (
    ConfigIssue,
    ConfigManager,
    InterruptionSettings,
    Live2DSettings,
    ProtocolSettings,
    STTSettings,
    TTSSettings,
    VADSettings,
    VTuberConfig,
    WakeWordSettings,
)

# ---------------------------------------------------------------------------
# Property 3: config merge totality & defaulting
#
# Target: ConfigManager.merge (pure). The strategy below builds raw host config
# sections (plugin / stt / tts / env) that, per known setting, randomly: OMIT
# the key, inject a TYPE-VALID value, or inject a TYPE-INVALID value. For each
# choice we record the expected post-merge outcome so the test can assert the
# full Property 3 invariant in one shot.
# ---------------------------------------------------------------------------

# Realistic, collision-free provider names for the provider-scoped tts.<p>.voice
# / tts.<p>.model resolution (never overlap the flat tts.* keys below).
_PROVIDER_NAMES = ("edge", "openai", "azure", "local", "elevenlabs", "piper")

# (leaf, kind, default) for each flat section.
_STT_FIELDS = (
    ("enabled", "bool", True),
    ("provider", "str", "local"),
    ("model", "str", "base"),
    ("language", "str", ""),
    ("transcribe_timeout_s", "float", 10.0),
)
_TTS_TOP_FIELDS = (
    ("synth_timeout_s", "float", 10.0),
    ("max_attempts", "int", 3),
)
_PLUGIN_SECTIONS = {
    "vad": (
        ("silence_threshold_s", "float", 2.0),
        ("max_record_s", "float", 60.0),
        ("barge_in_min_speech_ms", "int", 200),
        ("sample_rate", "int", 16000),
        ("frame_ms", "int", 20),
    ),
    "wake_word": (
        ("enabled", "bool", False),
        ("phrase", "str", "hey hermes"),
        ("confidence_threshold", "float", 0.7),
        ("listen_timeout_s", "float", 3.0),
    ),
    "live2d": (
        ("model_name", "str", "default"),
        ("model_dict_path", "str", "models/model_dict.json"),
        ("default_expression", "str", "neutral"),
        ("target_fps", "int", 30),
    ),
    "protocol": (
        ("host", "str", "127.0.0.1"),
        ("port", "int", 12393),
        ("ws_path", "str", "/client-ws"),
        ("max_message_bytes", "int", 1_048_576),
    ),
    "interruption": (("enabled", "bool", True),),
}

# Expectation record: one per leaf field of the merged VTuberConfig.
_Rec = collections.namedtuple("_Rec", "section leaf path kind expected issue")

_FLOAT_KW = dict(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6)


def _valid_value(draw, kind):
    """Draw a value that IS of ``kind`` (preserved as-is by merge)."""
    if kind == "bool":
        return draw(st.booleans())
    if kind == "int":
        return draw(st.integers(min_value=-(10 ** 6), max_value=10 ** 6))
    if kind == "float":
        return draw(st.floats(**_FLOAT_KW))
    return draw(st.text(max_size=24))  # str


def _invalid_value(draw, kind):
    """Draw a value that is NOT a valid (nor coercible) ``kind``.

    Mirrors ``config._coerce``: ``bool`` is never a valid int/float; ``int`` IS
    a valid float (coerced), so ``int`` is excluded from the float-invalid pool.
    """
    pools = {
        "bool": (st.integers(), st.text(max_size=8), st.floats(**_FLOAT_KW),
                 st.none(), st.lists(st.integers(), max_size=3)),
        "int": (st.booleans(), st.text(max_size=8), st.floats(**_FLOAT_KW),
                st.none(), st.lists(st.integers(), max_size=3)),
        "float": (st.booleans(), st.text(max_size=8),
                  st.none(), st.lists(st.integers(), max_size=3)),
        "str": (st.integers(), st.booleans(), st.floats(**_FLOAT_KW),
                st.none(), st.lists(st.integers(), max_size=3)),
    }
    return draw(st.one_of(*pools[kind]))


def _populate(draw, fields, container, prefix, section, expectations):
    """Apply omit/valid/invalid decisions for ``fields`` into ``container``."""
    for leaf, kind, default in fields:
        decision = draw(st.sampled_from(("omit", "valid", "invalid")))
        path = f"{prefix}.{leaf}"
        if decision == "omit":
            expected, issue = default, False
        elif decision == "valid":
            value = _valid_value(draw, kind)
            container[leaf] = value
            expected, issue = value, False
        else:  # invalid
            container[leaf] = _invalid_value(draw, kind)
            expected, issue = default, True
        expectations.append(_Rec(section, leaf, path, kind, expected, issue))


@st.composite
def _merge_inputs(draw):
    """Build (plugin, stt, tts, env, expectations) with mixed omit/valid/invalid."""
    expectations: list = []

    # -- STT (flat, reuses top-level stt section) -----------------------
    stt_section: dict = {}
    _populate(draw, _STT_FIELDS, stt_section, "stt", "stt", expectations)

    # -- TTS: provider resolution drives provider-scoped voice/model -----
    tts_section: dict = {}
    pdec = draw(st.sampled_from(("omit", "valid", "invalid")))
    if pdec == "omit":
        resolved = "edge"
        expectations.append(_Rec("tts", "provider", "tts.provider", "str", "edge", False))
    elif pdec == "valid":
        pv = draw(st.sampled_from(_PROVIDER_NAMES))
        tts_section["provider"] = pv
        resolved = pv
        expectations.append(_Rec("tts", "provider", "tts.provider", "str", pv, False))
    else:  # invalid
        tts_section["provider"] = _invalid_value(draw, "str")
        resolved = "edge"
        expectations.append(_Rec("tts", "provider", "tts.provider", "str", "edge", True))

    _populate(draw, _TTS_TOP_FIELDS, tts_section, "tts", "tts", expectations)

    # provider-scoped voice/model live under tts.<resolved>.*
    provider_section: dict = {}
    for leaf, default in (("voice", "en-US-AriaNeural"), ("model", "")):
        decision = draw(st.sampled_from(("omit", "valid", "invalid")))
        path = f"tts.{resolved}.{leaf}"
        if decision == "omit":
            expected, issue = default, False
        elif decision == "valid":
            value = _valid_value(draw, "str")
            provider_section[leaf] = value
            expected, issue = value, False
        else:
            provider_section[leaf] = _invalid_value(draw, "str")
            expected, issue = default, True
        expectations.append(_Rec("tts", leaf, path, "str", expected, issue))
    if provider_section:
        tts_section[resolved] = provider_section

    # -- Plugin-scoped sections -----------------------------------------
    plugin: dict = {}
    for key, fields in _PLUGIN_SECTIONS.items():
        sub: dict = {}
        _populate(draw, fields, sub, key, key, expectations)
        if sub:  # leaving it out exercises the "whole section missing" path
            plugin[key] = sub

    # env is accepted by merge but must never affect a (non-secret) field.
    env = draw(st.dictionaries(st.text(max_size=10), st.text(max_size=10), max_size=5))

    return plugin, stt_section, tts_section, env, expectations


def _type_ok(kind, value):
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, float)
    return isinstance(value, str)


# Feature: open-llm-vtuber-plugin, Property 3: config merge totality & defaulting
# Validates: 需求 2.1, 2.5, 2.6, 2.9
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(_merge_inputs())
def test_property3_config_merge_totality_and_defaulting(case):
    """merge ALWAYS yields a fully-populated, valid VTuberConfig.

    For any raw config (arbitrary missing keys + arbitrary type-invalid values):
    every field is present and correctly typed; missing optional keys take their
    documented defaults; each type-invalid key falls back to its default AND
    produces exactly one ConfigIssue naming its setting_path + expected_type;
    type-valid keys are preserved as-is.
    """
    plugin, stt_section, tts_section, env, expectations = case

    config, issues = ConfigManager.merge(plugin, stt_section, tts_section, env)

    # Totality: a fully-typed VTuberConfig with every sub-section present.
    assert isinstance(config, VTuberConfig)
    assert isinstance(config.stt, STTSettings)
    assert isinstance(config.tts, TTSSettings)
    assert isinstance(config.vad, VADSettings)
    assert isinstance(config.wake_word, WakeWordSettings)
    assert isinstance(config.live2d, Live2DSettings)
    assert isinstance(config.protocol, ProtocolSettings)
    assert isinstance(config.interruption, InterruptionSettings)

    # Every leaf: present, correctly typed, equal to its expected value
    # (missing -> default, invalid -> default, valid -> preserved as-is).
    for rec in expectations:
        actual = getattr(getattr(config, rec.section), rec.leaf)
        assert _type_ok(rec.kind, actual), (rec, actual)
        assert actual == rec.expected, (rec, actual)

    # Issues: exactly one ConfigIssue per type-invalid key, each identifying its
    # setting_path + expected_type; valid/missing keys produce no issue.
    assert all(isinstance(i, ConfigIssue) for i in issues)
    got_paths = sorted(i.setting_path for i in issues)
    want_paths = sorted(rec.path for rec in expectations if rec.issue)
    assert got_paths == want_paths
    by_path = {i.setting_path: i for i in issues}
    for rec in expectations:
        if rec.issue:
            issue = by_path[rec.path]
            assert issue.expected_type == rec.kind
            assert rec.path in issue.message


# ---------------------------------------------------------------------------
# Property 4: config source partition
#
# Target: ConfigManager.merge + ConfigManager.required_secret (both pure).
# This property pins the source-partition invariant of 需求 2.4 / 2.7:
#   * NON-SECRET settings resolve ONLY from config.yaml. merge deliberately
#     never consults ``env``; so even an adversarial env that mirrors every
#     non-secret leaf key (e.g. "voice"/"port"/"silence_threshold_s", and their
#     dotted paths) can NEVER change a merged field -> merge(..., evil_env) must
#     equal merge(..., {}) on both the config AND the issues list.
#   * SECRET settings resolve ONLY from .env via required_secret; a same-named
#     key sitting in config.yaml can never satisfy a secret, and a secret that
#     lives only in config.yaml (absent from .env) is reported missing/blocked.
# ---------------------------------------------------------------------------

# Every non-secret leaf key name; used to build an adversarial env that mirrors
# the config-only settings. If merge ever read env, at least one would change.
_NONSECRET_LEAF_KEYS = (
    "enabled", "provider", "model", "language", "transcribe_timeout_s",
    "synth_timeout_s", "max_attempts", "voice",
    "silence_threshold_s", "max_record_s", "barge_in_min_speech_ms",
    "sample_rate", "frame_ms", "phrase", "confidence_threshold",
    "listen_timeout_s", "model_name", "model_dict_path",
    "default_expression", "target_fps", "host", "port", "ws_path",
    "max_message_bytes",
)

# Non-blank text: required_secret treats blank/whitespace-only values as absent,
# so an env-resolvable secret value must contain at least one non-space char.
_NONBLANK_TEXT = st.text(min_size=1, max_size=24).filter(lambda s: s.strip() != "")


@st.composite
def _partition_case(draw):
    """Build adversarial config/env material for the source-partition property.

    Returns everything needed to exercise BOTH partitions in one test:
      * (plugin, stt, tts)         -- non-secret config sections
      * evil_env                   -- env mirroring every non-secret leaf/path
      * secret_key / secret_val    -- a secret present (non-blank) in .env
      * config_secret_val          -- a DIFFERENT same-named value in config.yaml
      * config_only_key            -- a secret living ONLY in config.yaml
    """
    # -- Non-secret material: reuse the Property 3 generator for the sections.
    plugin, stt_section, tts_section, _env_unused, expectations = draw(_merge_inputs())

    # An env that maliciously mirrors every non-secret setting, both as a bare
    # leaf key ("voice", "port", ...) and as its dotted path ("tts.edge.voice",
    # "protocol.port", ...), each mapped to an arbitrary value.
    evil_env: dict = {}
    for key in _NONSECRET_LEAF_KEYS:
        evil_env[key] = draw(st.text(max_size=12))
    for rec in expectations:
        evil_env[rec.path] = draw(st.text(max_size=12))
    evil_env.update(
        draw(st.dictionaries(st.text(max_size=10), st.text(max_size=10), max_size=5))
    )

    # -- Secret material.
    secret_key = draw(st.text(min_size=1, max_size=20))
    secret_val = draw(_NONBLANK_TEXT)
    # The SAME key also appears in config.yaml with a DIFFERENT value; it must
    # never win over (nor be consulted instead of) the .env value.
    config_secret_val = secret_val + "::from-config-yaml"
    # A required secret that lives ONLY in config.yaml (never placed in .env).
    config_only_key = draw(st.text(min_size=1, max_size=20))

    return (
        plugin, stt_section, tts_section, evil_env,
        secret_key, secret_val, config_secret_val, config_only_key,
    )


# Feature: open-llm-vtuber-plugin, Property 4: config source partition
# Validates: 需求 2.4, 2.7
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(_partition_case())
def test_property4_config_source_partition(case):
    """Secrets resolve ONLY from .env; non-secrets resolve ONLY from config.yaml.

    需求 2.7: an adversarial env mirroring every non-secret leaf key/path can
    never change a merged field -> merge(..., evil_env) == merge(..., {}).
    需求 2.4: required_secret reads the .env value even when a same-named key
    sits in config.yaml, and a config.yaml-only secret (absent from .env) is
    reported missing and blocks startup.
    """
    (plugin, stt_section, tts_section, evil_env,
     secret_key, secret_val, config_secret_val, config_only_key) = case

    # -- Non-secret partition (需求 2.7) --------------------------------
    # The adversarial env mirrors every non-secret setting; merge must ignore it
    # entirely, so the merged config AND the issue list are byte-for-byte
    # identical to merging with an empty env.
    cfg_evil, issues_evil = ConfigManager.merge(
        plugin, stt_section, tts_section, evil_env
    )
    cfg_empty, issues_empty = ConfigManager.merge(
        plugin, stt_section, tts_section, {}
    )
    assert cfg_evil == cfg_empty, (cfg_evil, cfg_empty)
    assert issues_evil == issues_empty, (issues_evil, issues_empty)

    # -- Secret partition (需求 2.4) ------------------------------------
    # config.yaml carries the secret under the SAME name with a DIFFERENT value;
    # required_secret only ever consults .env, so the .env value wins.
    config_yaml_secrets = {secret_key: config_secret_val}
    env_with_secret = dict(evil_env)
    env_with_secret[secret_key] = secret_val

    resolved = ConfigManager.required_secret(env_with_secret, secret_key)
    assert resolved.available is True
    assert resolved.blocks_startup is False
    assert resolved.value == secret_val
    # The config.yaml value never leaks into the resolution.
    assert resolved.value != config_yaml_secrets[secret_key]

    # A secret present ONLY in config.yaml (never in .env) cannot be satisfied:
    # it must be reported missing, name the missing key, and block startup.
    env_without_config_only = {
        k: v for k, v in env_with_secret.items() if k != config_only_key
    }
    blocked = ConfigManager.required_secret(env_without_config_only, config_only_key)
    assert blocked.available is False
    assert blocked.blocks_startup is True
    assert blocked.value is None
    assert config_only_key in blocked.message


# ---------------------------------------------------------------------------
# Property 5: segmentation boundaries
#
# Target: VadSegmenter.feed (pure, replayable). The generator builds a random
# VAD event stream — a leading ``speech_start`` followed by a mix of
# speech / silence / speech_end / max_timeout / speech_start events with
# STRICTLY INCREASING timestamps — against a VADSettings whose
# silence_threshold_s is randomized within the configured 0.5–10s range and
# whose max_record_s is randomized within a bounded range so BOTH boundaries
# (silence-first and max-record-first) are reachable.
#
# An independent reference model (``_reference_end``) re-derives the expected
# boundary straight from the requirement semantics (需求 4.2/4.3/4.8): a
# segment ends on the EARLIEST event whose timestamp satisfies whichever of
#   * continuous silence >= silence_threshold_s, or
#   * recording duration since speech_start >= max_record_s
# is reached FIRST, with end_reason matching the triggering condition. The test
# then asserts the segmenter does NOT end early (every prior feed returns None)
# and DOES end exactly at that event with the matching end_reason.
# ---------------------------------------------------------------------------

from omnilimb_face.voice.vad import VadEvent, VadSegmenter, VoiceSegment

# Kind pool biased toward silence so continuous-silence runs accumulate often
# enough to exercise the silence-first branch, while still mixing in speech
# (which RESETS the silence run), explicit max_timeout signals, and mid-stream
# speech_start restarts (which reopen the segment from a new start_ms).
_VAD_KIND_POOL = (
    "speech", "silence", "silence", "silence",
    "speech_end", "max_timeout", "speech_start",
)


@st.composite
def _vad_timeline(draw):
    """Build ``(cfg, events)`` for a randomized VAD event stream.

    A leading ``speech_start`` opens the segment; the remaining events carry
    strictly increasing ``ts_ms`` so durations are well defined. The thresholds
    are randomized so neither boundary dominates: ``silence_threshold_s`` spans
    its full configured 0.5–10s range and ``max_record_s`` spans a bounded
    range that the generated timeline can actually reach.
    """
    silence_threshold_s = draw(
        st.floats(min_value=0.5, max_value=10.0,
                  allow_nan=False, allow_infinity=False)
    )
    max_record_s = draw(
        st.floats(min_value=0.5, max_value=12.0,
                  allow_nan=False, allow_infinity=False)
    )
    cfg = VADSettings(
        silence_threshold_s=silence_threshold_s,
        max_record_s=max_record_s,
    )

    rms = st.floats(min_value=0.0, max_value=1.0,
                    allow_nan=False, allow_infinity=False)

    ts = draw(st.integers(min_value=0, max_value=2000))
    events = [VadEvent(kind="speech_start", ts_ms=ts, rms=draw(rms))]
    n = draw(st.integers(min_value=1, max_value=40))
    for _ in range(n):
        ts += draw(st.integers(min_value=1, max_value=1500))  # strictly increasing
        kind = draw(st.sampled_from(_VAD_KIND_POOL))
        events.append(VadEvent(kind=kind, ts_ms=ts, rms=draw(rms)))
    return cfg, events


def _reference_end(cfg, events):
    """Independent reference model of the Property 5 boundary semantics.

    Replays the event stream from the requirement's first principles and
    returns the FIRST segment boundary as
    ``(end_index, start_ms, end_ms, end_reason)`` — or ``None`` if neither
    threshold is ever reached (the segment stays open). The end fires on the
    earliest event whose timestamp satisfies EITHER a continuous-silence run of
    ``silence_threshold_s`` (counted from the first silence/speech_end of the
    run, reset whenever speech resumes) OR a recording duration since
    ``speech_start`` of ``max_record_s`` (or an explicit ``max_timeout`` event).
    On a same-event tie the earlier threshold timestamp wins; an exact tie
    resolves to ``"silence"`` (the natural end), per 需求 4.8's wording.
    """
    silence_threshold_ms = float(cfg.silence_threshold_s) * 1000.0
    max_record_ms = float(cfg.max_record_s) * 1000.0

    active = False
    start_ms = 0
    silence_start = None  # start ts of the current continuous-silence run

    for idx, ev in enumerate(events):
        kind, ts = ev.kind, ev.ts_ms
        forced_max = False

        if kind == "speech_start":
            active = True
            start_ms = ts
            silence_start = None  # (re)open in speech: no silence run yet
        elif not active:
            continue
        elif kind == "speech":
            silence_start = None  # voice resumed -> reset the silence run
        elif kind in ("silence", "speech_end"):
            if silence_start is None:
                silence_start = ts  # first event of a continuous silence run
        elif kind == "max_timeout":
            forced_max = True
        # (the generator emits no other kinds)

        # Evaluate the end conditions at this event's timestamp.
        max_reached_ts = start_ms + max_record_ms
        max_met = forced_max or ts >= max_reached_ts
        if silence_start is None:
            silence_reached_ts = None
            silence_met = False
        else:
            silence_reached_ts = silence_start + silence_threshold_ms
            silence_met = ts >= silence_reached_ts

        if not max_met and not silence_met:
            continue  # neither threshold reached yet -> keep recording

        if silence_met and max_met:
            reason = (
                "silence"
                if silence_reached_ts is not None
                and silence_reached_ts <= max_reached_ts
                else "max_timeout"
            )
        elif silence_met:
            reason = "silence"
        else:
            reason = "max_timeout"
        return idx, start_ms, ts, reason

    return None


# Feature: open-llm-vtuber-plugin, Property 5: segmentation boundaries
# Validates: 需求 4.2, 4.3, 4.8
@settings(max_examples=100, deadline=None)
@given(_vad_timeline())
def test_property5_segmentation_boundaries(case):
    """A segment ends IFF the earliest of the silence / max-record thresholds
    is reached, with a matching end_reason.

    For any randomized event stream: the VadSegmenter never ends a segment
    before either threshold is met (需求 4.2 — recording continues), and ends
    EXACTLY at the earliest event whose timestamp satisfies the continuous
    silence threshold (需求 4.3 -> end_reason "silence") or the max-record
    duration / explicit max_timeout (需求 4.8 -> end_reason "max_timeout"),
    matched independently by a reference model.
    """
    cfg, events = case
    expected = _reference_end(cfg, events)

    segmenter = VadSegmenter(cfg)

    if expected is None:
        # Neither threshold is ever reached: the segment stays open, so every
        # feed returns None and the segmenter remains active (opened by the
        # leading speech_start and never closed).
        for ev in events:
            assert segmenter.feed(ev) is None, ev
        assert segmenter.is_active
        return

    end_index, exp_start, exp_end, exp_reason = expected

    # Does NOT end early: every event BEFORE the earliest-threshold event keeps
    # the segment open (需求 4.2).
    for i in range(end_index):
        assert segmenter.feed(events[i]) is None, (i, events[i])

    # DOES end exactly at the earliest-threshold event, and the produced
    # VoiceSegment's boundaries + end_reason match the triggering condition
    # (需求 4.3 silence-first / 需求 4.8 max-record-first).
    segment = segmenter.feed(events[end_index])
    assert isinstance(segment, VoiceSegment), segment
    assert segment.end_reason in ("silence", "max_timeout")
    assert segment.end_reason == exp_reason, (segment, expected)
    assert segment.start_ms == exp_start, (segment, expected)
    assert segment.end_ms == exp_end, (segment, expected)
    # The producing event's timestamp is the segment end, and the segment is
    # bounded by its opening speech_start.
    assert segment.end_ms == events[end_index].ts_ms
    assert segment.end_ms >= segment.start_ms
    # Having emitted a segment, the segmenter resets and is ready for the next
    # utterance (no lingering open state).
    assert not segmenter.is_active


# ---------------------------------------------------------------------------
# Property 8: sentence chunking losslessness & boundaries
#
# Target: SentenceChunker.push / flush (pure, incremental). The generator
# builds random token streams from a MIXED alphabet that deliberately includes
# every sentence terminator (。．.!?！？…\n), Unicode text (CJK / kana),
# ASCII letters, spaces and tabs, plus interspersed empty-string tokens — so a
# single token may carry zero, one, or many terminators, and the stream as a
# whole exercises 0..many sentence boundaries. Streaming the tokens through
# push() (collecting emitted sentences in order) and then flush() must satisfy
# the Property 8 invariant: (1) losslessness — concatenating every pushed
# sentence plus the single flush residual reproduces the concatenation of all
# input tokens exactly (no characters lost, duplicated, or reordered); and
# (2) boundaries — every push-returned sentence ends with a terminator, while
# the lone flush residual (the trailing partial sentence at end-of-stream) is
# the only emitted piece allowed not to.
# ---------------------------------------------------------------------------

from omnilimb_face.chunker import SentenceChunker

# A mixed alphabet that INCLUDES every terminator char so generated tokens
# contain 0..many terminators, alongside Unicode text, ASCII letters, and
# whitespace (space + tab) to mirror real streamed LLM output.
_CHUNK_ALPHABET = (
    SentenceChunker.TERMINATORS  # 。．.!?！？…\n  (terminators)
    + "abcXYZ"                   # ASCII letters
    + " \t"                      # whitespace
    + "你好世界こんにちは"          # CJK + kana (Unicode)
)

# A single stream token: a possibly-empty string over the mixed alphabet. The
# explicit empty-string branch guarantees empty tokens appear in the stream
# (push must be a no-op for them, neither losing nor inventing characters).
_chunk_token = st.one_of(
    st.just(""),
    st.text(alphabet=_CHUNK_ALPHABET, max_size=12),
)

# A full streamed token sequence (0..many tokens), so the chunker is exercised
# from empty streams up to long multi-terminator streams.
_chunk_tokens = st.lists(_chunk_token, max_size=20)


# Feature: open-llm-vtuber-plugin, Property 8: sentence chunking losslessness & boundaries
# Validates: 需求 3.3, 6.3
@settings(max_examples=100, deadline=None)
@given(_chunk_tokens)
def test_property8_sentence_chunking_losslessness_and_boundaries(tokens):
    """Streaming tokens through push()+flush() is lossless and well-bounded.

    For ANY token stream over the mixed (terminator-bearing, Unicode,
    whitespace, empty) alphabet:
      * Losslessness (需求 3.3): joining, in order, every sentence returned by
        successive push() calls plus the single flush() residual reproduces the
        concatenation of all input tokens EXACTLY — no character is lost,
        duplicated, or reordered.
      * Boundaries (需求 6.3): every push-returned sentence ends with a
        terminator char; flush() returns at most one trailing residual, which
        is the ONLY emitted piece permitted not to end with a terminator.
    """
    chunker = SentenceChunker()

    pushed: list[str] = []
    for token in tokens:
        emitted = chunker.push(token)
        # Boundary (需求 6.3): each push-returned sentence is non-empty and ends
        # exactly on a terminator character.
        for sentence in emitted:
            assert sentence, (token, emitted)
            assert sentence[-1] in SentenceChunker.TERMINATORS, (sentence, emitted)
        pushed.extend(emitted)

    residual = chunker.flush()
    # flush() yields at most the single trailing partial sentence.
    assert len(residual) <= 1, residual
    # When present, the residual is the lone piece allowed to lack a terminator;
    # it must still be non-empty (flush returns [] rather than [""]).
    if residual:
        assert residual[0] != "", residual

    # Losslessness (需求 3.3): pushed sentences + flush residual, concatenated in
    # order, equal the concatenation of the input tokens — byte-for-byte.
    assert "".join(pushed + residual) == "".join(tokens), (
        pushed, residual, tokens,
    )


# ---------------------------------------------------------------------------
# Property 7: barge-in decision
#
# Target: InterruptionController.on_vad_event (pure, stateful decision). The
# generator builds a random VAD event stream "during playback" — a mix of
# speech-extending (speech_start / speech) and run-breaking (silence /
# speech_end) events with STRICTLY INCREASING ts_ms — paired with a random
# ``enabled`` flag and a random ``barge_in_min_speech_ms`` threshold. A fresh
# controller is driven one event at a time via on_vad_event.
#
# An INDEPENDENT reference model (``_reference_barge_in``) re-derives, from the
# requirement's first principles, the accumulated CONTINUOUS speech duration at
# every event: a speech event extends the current run (duration measured from
# the run's first speech ts), while ANY non-speech / silence event resets the
# accumulation to 0. The Property 7 invariant (需求 5.2 / 5.5) is then asserted
# per event: should_interrupt is True IFF interruption is enabled AND that
# accumulated continuous-speech duration has reached the threshold; when
# disabled, should_interrupt is False for EVERY event regardless of the stream.
# ---------------------------------------------------------------------------

from omnilimb_face.interruption import InterruptDecision, InterruptionController

# Speech kinds extend a continuous run; everything else breaks it (resets the
# accumulator). Mirrors InterruptionController._SPEECH_KINDS exactly. The pool
# is biased toward speech so continuous runs accumulate far enough to cross the
# threshold often, while still interleaving run-breaking events.
_BARGE_SPEECH_KINDS = ("speech_start", "speech", "speech")
_BARGE_BREAK_KINDS = ("silence", "speech_end", "max_timeout")
_BARGE_KIND_POOL = _BARGE_SPEECH_KINDS + _BARGE_BREAK_KINDS


@st.composite
def _barge_in_case(draw):
    """Build ``(enabled, threshold_ms, use_vad_settings, events)`` for Property 7.

    ``enabled`` toggles interruption (需求 5.5). ``threshold_ms`` randomizes the
    barge-in minimum continuous-speech duration (需求 5.2) across a range the
    generated timeline can actually reach (so both the below-threshold and
    at/above-threshold branches are exercised). ``use_vad_settings`` selects how
    the threshold is supplied to the real constructor (explicit kwarg vs.
    ``vad_settings``); both must resolve identically. The event stream carries
    strictly increasing ts_ms so continuous-speech durations are well defined.
    """
    enabled = draw(st.booleans())
    threshold_ms = draw(st.integers(min_value=0, max_value=1000))
    use_vad_settings = draw(st.booleans())

    rms = st.floats(min_value=0.0, max_value=1.0,
                    allow_nan=False, allow_infinity=False)

    ts = draw(st.integers(min_value=0, max_value=2000))
    n = draw(st.integers(min_value=1, max_value=40))
    events = []
    for _ in range(n):
        kind = draw(st.sampled_from(_BARGE_KIND_POOL))
        events.append(VadEvent(kind=kind, ts_ms=ts, rms=draw(rms)))
        ts += draw(st.integers(min_value=1, max_value=400))  # strictly increasing
    return enabled, threshold_ms, use_vad_settings, events


def _reference_barge_in(enabled, threshold_ms, events):
    """Independent reference model of the Property 7 decision semantics.

    Replays the event stream from first principles and returns, per event, the
    expected ``(accumulated_speech_ms, should_interrupt)``. A speech_start /
    speech event extends the current continuous run (duration = elapsed time
    since the run's FIRST speech ts); any other event resets the accumulation to
    0. should_interrupt is True IFF ``enabled`` AND the accumulated continuous
    speech has reached ``threshold_ms`` at that event (需求 5.2); when disabled
    it is False for every event (需求 5.5).
    """
    speech_kinds = {"speech_start", "speech"}
    anchor = None  # ts of the first speech event of the current continuous run
    expected = []
    for ev in events:
        if ev.kind in speech_kinds:
            if anchor is None:
                anchor = ev.ts_ms
            accumulated = max(0, ev.ts_ms - anchor)
        else:
            anchor = None
            accumulated = 0
        should_interrupt = enabled and accumulated >= threshold_ms
        expected.append((accumulated, should_interrupt))
    return expected


# Feature: open-llm-vtuber-plugin, Property 7: barge-in decision
# Validates: 需求 5.2, 5.5
@settings(max_examples=100, deadline=None)
@given(_barge_in_case())
def test_property7_barge_in_decision(case):
    """Interrupt IFF enabled AND continuous speech has reached the threshold.

    For ANY VAD event stream during playback: at each event the controller
    decides should_interrupt == True IFF interruption is enabled (需求 5.5) AND
    the accumulated CONTINUOUS speech duration (since the last non-speech reset)
    has reached barge_in_min_speech_ms (需求 5.2); when disabled, should_interrupt
    is False for EVERY event regardless of the stream. The accumulated duration
    is re-derived by an independent reference model and compared step by step.
    """
    enabled, threshold_ms, use_vad_settings, events = case

    cfg = InterruptionSettings(enabled=enabled)
    if use_vad_settings:
        controller = InterruptionController(
            cfg, vad_settings=VADSettings(barge_in_min_speech_ms=threshold_ms)
        )
    else:
        controller = InterruptionController(
            cfg, barge_in_min_speech_ms=threshold_ms
        )

    # The resolved threshold must match whichever way it was supplied.
    assert controller.barge_in_min_speech_ms == threshold_ms
    assert controller.enabled is enabled

    expected = _reference_barge_in(enabled, threshold_ms, events)

    for ev, (exp_accumulated, exp_interrupt) in zip(events, expected):
        decision = controller.on_vad_event(ev)
        assert isinstance(decision, InterruptDecision), decision
        # Continuous-speech accumulation matches the independent reference model.
        assert decision.accumulated_speech_ms == exp_accumulated, (ev, decision)
        # Barge-in decision: enabled AND accumulated >= threshold (需求 5.2).
        assert decision.should_interrupt == exp_interrupt, (
            ev, threshold_ms, decision, exp_interrupt,
        )
        # When interruption is disabled, NO event ever requests a barge-in
        # (需求 5.5), regardless of how long speech has accumulated.
        if not enabled:
            assert decision.should_interrupt is False, (ev, decision)


# ---------------------------------------------------------------------------
# Property 11: expression mapping
#
# Target: ExpressionMapper.map_reply (pure, deterministic). The generator builds
# a random model emotion_map (keyword -> int index, keys drawn from a small
# pool, possibly empty), a default_expression that is SOMETIMES in the map and
# sometimes not, and a reply text assembled by interleaving plain text fragments
# (Unicode + ASCII + whitespace, but NEVER containing '[' or ']') with `[key]`
# bracket tags whose keys are drawn from BOTH the emotion-keyword pool (so a tag
# may be recognized or, when that key is absent from this map, unmatched) and a
# disjoint unknown-key pool (always unmatched). Because the text is built by
# joining the pieces in order, the ground-truth order of every tag is known, so
# the expected expressions / unmatched / primary are recomputed independently
# (mirroring "recognized = key present in emotion_map") and compared exactly.
# ---------------------------------------------------------------------------

from omnilimb_face.expression import ExpressionMapper, ExpressionResult

# Emotion-keyword pool (the model's emotionMap is a random SUBSET of these, so a
# tag drawn from this pool may be recognized OR — when the key is absent from a
# particular map — unmatched). Mixes ASCII and Unicode (CJK / kana) keys; none
# contain '[' , ']' or whitespace, so TAG_PATTERN matches them cleanly.
_EMOTION_KEYS = (
    "joy", "anger", "neutral", "sadness", "surprise",
    "fear", "disgust", "smirk", "喜悦", "怒り",
)

# Unknown-key pool, DISJOINT from _EMOTION_KEYS: tags using these keys are never
# in any generated emotion_map, so they are always unmatched. Bracket/whitespace
# free so the regex still matches the tag cleanly.
_UNKNOWN_KEYS = ("mystery", "unknown", "qux", "wat", "謎", "blink", "???", "zzz")

# Extra default-expression names that are NEVER members of _EMOTION_KEYS, so a
# default drawn from here is guaranteed absent from the map (exercises the
# "primary degrades to None" branch when nothing is recognized).
_DEFAULT_EXTRA = ("__no_default__", "missing", "絶対無い")

# Tags are drawn from BOTH pools; recognition is decided purely by membership in
# the generated emotion_map (exactly mirroring the implementation).
_TAG_KEY_POOL = _EMOTION_KEYS + _UNKNOWN_KEYS

# Plain-text alphabet for the fragments between tags: Unicode + ASCII letters,
# digits, punctuation and whitespace, but DELIBERATELY excluding '[' and ']' so
# the ONLY bracket characters in the reply come from the tags we insert (hence
# stripping every tag must leave no bracket behind).
_PLAIN_ALPHABET = "abcXYZ123 .,!?\t\n你好世界こんにちは"


@st.composite
def _expression_case(draw):
    """Build ``(emotion_map, default_expression, text, tag_keys)`` for Property 11.

    ``emotion_map`` is a random (possibly empty) subset of _EMOTION_KEYS mapped
    to integer indices; ``default_expression`` is drawn from the emotion pool
    plus never-mapped extras so it is sometimes present and sometimes absent.
    ``text`` interleaves bracket-free plain fragments with ``[key]`` tags drawn
    from _TAG_KEY_POOL; ``tag_keys`` records every tag key in its exact order of
    appearance so the expected outcome can be derived independently.
    """
    emotion_map = draw(
        st.dictionaries(
            st.sampled_from(_EMOTION_KEYS),
            st.integers(min_value=0, max_value=9),
            min_size=0,
            max_size=len(_EMOTION_KEYS),
        )
    )
    default_expression = draw(st.sampled_from(_EMOTION_KEYS + _DEFAULT_EXTRA))

    n = draw(st.integers(min_value=0, max_value=14))
    pieces: list[str] = []
    tag_keys: list[str] = []
    for _ in range(n):
        if draw(st.booleans()):  # emit a [key] tag
            key = draw(st.sampled_from(_TAG_KEY_POOL))
            tag_keys.append(key)
            pieces.append(f"[{key}]")
        else:  # emit a bracket-free plain fragment (may be empty)
            pieces.append(draw(st.text(alphabet=_PLAIN_ALPHABET, max_size=8)))

    return emotion_map, default_expression, "".join(pieces), tag_keys


# Feature: open-llm-vtuber-plugin, Property 11: expression mapping
# Validates: 需求 8.1, 8.3, 8.4, 8.5
@settings(max_examples=100, deadline=None)
@given(_expression_case())
def test_property11_expression_mapping(case):
    """map_reply strips every tag, maps recognized keys in order, records the
    unmatched ones, and resolves primary by first-recognized / default / None.

    For ANY emotion_map, default_expression and interleaved reply text:
      * display_text has EVERY ``[key]`` bracket tag removed — since the only
        brackets in the reply come from the inserted tags, none remain (需求 8.x
        remove_emotion_keywords).
      * expressions == the indices of the RECOGNIZED tags (key present in
        emotion_map) in order of appearance (需求 8.1).
      * unmatched == the keys of the unrecognized tags in order of appearance,
        and NONE of them contribute an entry to expressions (需求 8.3).
      * primary == the first recognized tag's index when any tag is recognized
        (earliest wins on conflicts, 需求 8.4); else the default_expression's
        index when it is in emotion_map; else None (需求 8.5 graceful fallback).
    Expected values are recomputed independently from the known tag order.
    """
    emotion_map, default_expression, text, tag_keys = case

    # Independent ground truth derived from the known tag order. "recognized"
    # mirrors the implementation exactly: a key is recognized iff it is present
    # in emotion_map (so an emotion-pool key absent from THIS map is unmatched).
    expected_expressions = [emotion_map[k] for k in tag_keys if k in emotion_map]
    expected_unmatched = [k for k in tag_keys if k not in emotion_map]
    if expected_expressions:
        expected_primary = expected_expressions[0]
    elif default_expression in emotion_map:
        expected_primary = emotion_map[default_expression]
    else:
        expected_primary = None

    mapper = ExpressionMapper(emotion_map, default_expression)
    result = mapper.map_reply(text)

    assert isinstance(result, ExpressionResult), result

    # display_text: all bracket tags removed -> no '[' or ']' survive.
    assert "[" not in result.display_text, result.display_text
    assert "]" not in result.display_text, result.display_text

    # expressions: recognized indices in appearance order (需求 8.1).
    assert result.expressions == expected_expressions, (result, expected_expressions)

    # unmatched: unknown keys in appearance order; none leak into expressions
    # (需求 8.3). Counts must partition the tags: recognized + unmatched == all.
    assert result.unmatched == expected_unmatched, (result, expected_unmatched)
    assert len(result.expressions) + len(result.unmatched) == len(tag_keys)

    # primary: first recognized -> default-in-map -> None (需求 8.4 / 8.5).
    assert result.primary == expected_primary, (result, expected_primary)


# ---------------------------------------------------------------------------
# Property 10: lip-sync volume normalization
#
# Target: TTSPlayer.compute_volumes (pure, no numpy). The generator builds a
# random NON-all-silent int16 mono buffer (a list of samples in
# [-32768, 32767] with at least one forced non-zero sample), packed to bytes as
# explicit little-endian via ``struct`` (matching the int16 LE contract the
# function decodes regardless of host byte order), together with a random
# sample_rate (8000–48000 Hz) and slice_length_ms (10–60 ms).
#
# The chunk count is re-derived INDEPENDENTLY from the documented split rule
# (``ceil(num_samples / chunk_size)`` with ``chunk_size = max(1,
# int(sample_rate * slice_length_ms / 1000))``) and compared exactly. For the
# non-silent buffer the Property 10 invariant (需求 7.3) is asserted: every
# volume lies in [0.0, 1.0], the series length equals that chunk count, and the
# peak-normalized maximum is exactly 1.0. A second, all-silent buffer of the
# SAME length pins the documented degenerate behaviour (zeros of the correct
# length) — the peak-normalization (max == 1.0) invariant applies to the
# non-silent buffer only.
# ---------------------------------------------------------------------------

import struct

from omnilimb_face.tts import TTSPlayer


@st.composite
def _pcm_case(draw):
    """Build ``(sample_rate, slice_length_ms, samples)`` for Property 10.

    ``samples`` is a non-empty list of int16 values with at least one forced
    non-zero entry, so the buffer is guaranteed NOT all-silent and the
    peak-normalization branch (max(volumes) == 1.0) is always exercised.
    """
    sample_rate = draw(st.integers(min_value=8000, max_value=48000))
    slice_length_ms = draw(st.integers(min_value=10, max_value=60))
    samples = draw(
        st.lists(
            st.integers(min_value=-32768, max_value=32767),
            min_size=1,
            max_size=2000,
        )
    )
    # Force at least one non-zero sample so the buffer is never all-silent
    # (the normalization invariant below requires a positive peak).
    pos = draw(st.integers(min_value=0, max_value=len(samples) - 1))
    mag = draw(st.integers(min_value=1, max_value=32767))
    sign = draw(st.sampled_from((1, -1)))
    samples[pos] = sign * mag
    return sample_rate, slice_length_ms, samples


def _expected_chunk_count(num_samples, sample_rate, slice_length_ms):
    """Independent re-derivation of the documented chunk count.

    Mirrors the split rule from first principles: ``chunk_size`` samples per
    chunk where ``chunk_size = max(1, int(sample_rate * slice_length_ms /
    1000))``, and the number of chunks is ``ceil(num_samples / chunk_size)``
    (the final chunk may be short).
    """
    chunk_size = int(sample_rate * slice_length_ms / 1000)
    if chunk_size < 1:
        chunk_size = 1
    return (num_samples + chunk_size - 1) // chunk_size


# Feature: open-llm-vtuber-plugin, Property 10: lip-sync volume normalization
# Validates: 需求 7.3
@settings(max_examples=100, deadline=None)
@given(_pcm_case())
def test_property10_lip_sync_volume_normalization(case):
    """compute_volumes returns a peak-normalized, correctly-chunked RMS series.

    For ANY non-all-silent int16 mono little-endian PCM buffer with a random
    sample_rate (8000–48000 Hz) and slice_length_ms (10–60 ms):
      * every element of ``volumes`` lies in the closed interval [0.0, 1.0];
      * ``len(volumes)`` equals the independently-computed chunk count
        ``ceil(num_samples / chunk_size)`` with
        ``chunk_size = max(1, int(sample_rate * slice_length_ms / 1000))``;
      * ``max(volumes) == 1.0`` — the loudest chunk is peak-normalized (需求 7.3);
      * ``slice_length_ms`` is echoed back unchanged.
    The all-silent buffer of the SAME length returns zeros of the correct chunk
    count (documented degenerate behaviour); the peak-normalization invariant
    applies to the non-silent buffer only.
    """
    sample_rate, slice_length_ms, samples = case

    num_samples = len(samples)
    # The generator guarantees a non-all-silent buffer.
    assert any(s != 0 for s in samples)

    # Pack as explicit int16 little-endian — the byte order compute_volumes
    # decodes (it normalizes to LE internally regardless of host byte order).
    pcm = struct.pack("<%dh" % num_samples, *samples)

    volumes, echoed = TTSPlayer.compute_volumes(pcm, sample_rate, slice_length_ms)

    # slice_length_ms is echoed back unchanged for the front-end slice_length.
    assert echoed == slice_length_ms

    # Chunk count matches the independent ceil(num_samples / chunk_size).
    expected_chunks = _expected_chunk_count(num_samples, sample_rate, slice_length_ms)
    assert len(volumes) == expected_chunks, (len(volumes), expected_chunks)

    # Every volume is a normalized magnitude within [0.0, 1.0].
    for v in volumes:
        assert 0.0 <= v <= 1.0, v

    # Peak-normalized: the loudest chunk maps EXACTLY to 1.0 (需求 7.3).
    assert max(volumes) == 1.0, max(volumes)

    # Degenerate all-silent branch (documented behaviour): a buffer of the SAME
    # length that is entirely zero yields zeros of the correct chunk count, and
    # the max == 1.0 invariant does NOT apply to it (no positive peak).
    silent_pcm = struct.pack("<%dh" % num_samples, *([0] * num_samples))
    silent_volumes, silent_echoed = TTSPlayer.compute_volumes(
        silent_pcm, sample_rate, slice_length_ms
    )
    assert silent_echoed == slice_length_ms
    assert len(silent_volumes) == expected_chunks
    assert silent_volumes == [0.0] * expected_chunks


# ---------------------------------------------------------------------------
# Property 14: wake-word gating state machine
#
# Target: WakeWord (pure, deterministic gate). The generator builds a random
# WakeWordSettings — a random ``enabled`` flag, a ``confidence_threshold`` over
# its full 0.0–1.0 range, and a ``listen_timeout_s`` over its configured
# 0.5–10s range — paired with a random sequence of "observations", each either
# a DETECTION carrying a confidence in [0.0, 1.0] or a SILENCE step carrying a
# duration. A fresh WakeWord is driven one observation at a time.
#
# An INDEPENDENT reference state machine (``_WakeWordReference``) re-derives the
# gate decision straight from the requirement semantics:
#   * disabled (需求 13.6)        -> the gate is ALWAYS open;
#   * enabled  (需求 13.1/2/3/4)  -> the gate is open IFF a detection whose
#     confidence reached the threshold has occurred AND the continuous silence
#     accumulated since the last qualifying detection has not yet reached
#     listen_timeout_s. A qualifying detection opens the gate and resets the
#     silence accumulator to 0 (a detection is itself voice activity); each
#     silence step accumulates; once the accumulated silence reaches the
#     timeout the gate closes and the machine returns to wake-word listening; a
#     fresh qualifying detection re-opens it and resets the accumulator.
# After EVERY observation the real gate's is_gate_open() (and the observe_*
# return value) is asserted to equal the reference model's decision.
# ---------------------------------------------------------------------------

from omnilimb_face.voice.wake_word import WakeWord


@st.composite
def _wake_word_timeline(draw):
    """Build ``(cfg, observations)`` for the Property 14 wake-word gate.

    ``cfg`` randomizes ``enabled`` (so BOTH the always-open disabled branch and
    the triggered/timeout enabled state machine are exercised),
    ``confidence_threshold`` across its full 0.0–1.0 range, and
    ``listen_timeout_s`` across its configured 0.5–10s range. ``observations``
    is a non-empty list of ``("detection", confidence)`` / ``("silence",
    duration)`` steps: detection confidences span [0.0, 1.0] so a detection is
    sometimes qualifying and sometimes sub-threshold, and silence durations are
    bounded so the accumulator can both stay below and reach the timeout across
    a run.
    """
    enabled = draw(st.booleans())
    confidence_threshold = draw(
        st.floats(min_value=0.0, max_value=1.0,
                  allow_nan=False, allow_infinity=False)
    )
    listen_timeout_s = draw(
        st.floats(min_value=0.5, max_value=10.0,
                  allow_nan=False, allow_infinity=False)
    )
    cfg = WakeWordSettings(
        enabled=enabled,
        confidence_threshold=confidence_threshold,
        listen_timeout_s=listen_timeout_s,
    )

    confidence = st.floats(min_value=0.0, max_value=1.0,
                           allow_nan=False, allow_infinity=False)
    # Silence steps up to 5s so that, against a 0.5–10s timeout, a continuous
    # silence run can both stay below the timeout and reach it within a few
    # steps — exercising the "gate stays open" and "gate closes" branches.
    duration = st.floats(min_value=0.0, max_value=5.0,
                         allow_nan=False, allow_infinity=False)

    n = draw(st.integers(min_value=1, max_value=40))
    observations: list = []
    for _ in range(n):
        if draw(st.booleans()):
            observations.append(("detection", draw(confidence)))
        else:
            observations.append(("silence", draw(duration)))
    return cfg, observations


class _WakeWordReference:
    """Independent reference model of the WakeWord gate semantics (需求 13).

    Re-derives the gate decision from first principles, mirroring the impl's
    exact reset rules without reusing its code: a qualifying detection
    (confidence >= threshold) opens the gate and resets the continuous-silence
    accumulator to 0; each silence step accumulates; once the gate is open and
    the accumulated silence reaches ``listen_timeout_s`` the gate closes
    (returns to wake-word listening). When disabled the gate is always open.
    """

    def __init__(self, enabled: bool, threshold: float, timeout: float) -> None:
        self.enabled = enabled
        self.threshold = threshold
        self.timeout = timeout
        self.triggered = False
        self.silence_accum = 0.0

    def observe_detection(self, confidence: float) -> bool:
        if not self.enabled:
            return True  # disabled -> always open (需求 13.6).
        if float(confidence) >= self.threshold:
            # Qualifying wake word: open the gate, reset the silence clock
            # (the detection is itself voice activity, 需求 13.1 / 13.4).
            self.triggered = True
            self.silence_accum = 0.0
        # else: sub-threshold -> keep listening, gate unchanged (需求 13.2/13.3).
        return self.triggered

    def observe_silence(self, silence_s: float) -> bool:
        if not self.enabled:
            return True  # disabled -> always open (需求 13.6).
        self.silence_accum += max(0.0, float(silence_s))
        if self.triggered and self.silence_accum >= self.timeout:
            # Silence reached the listen timeout -> return to listening (需求 13.4).
            self.triggered = False
        return self.triggered

    def is_gate_open(self) -> bool:
        if not self.enabled:
            return True  # 需求 13.6.
        return self.triggered  # 需求 13.1/13.2/13.3/13.4.


# Feature: open-llm-vtuber-plugin, Property 14: wake-word gating state machine
# Validates: 需求 13.1, 13.2, 13.3, 13.4, 13.6
@settings(max_examples=100, deadline=None)
@given(_wake_word_timeline())
def test_property14_wake_word_gating_state_machine(case):
    """The wake-word gate matches an independent state machine at every step.

    For ANY WakeWordSettings and ANY sequence of detection / silence
    observations, after EACH observation the real WakeWord gate agrees with an
    independent reference model:
      * disabled -> is_gate_open() is ALWAYS True; every detection/silence is a
        no-op that reports an open gate (需求 13.6).
      * enabled  -> the gate is open IFF a detection with confidence >= threshold
        has occurred (需求 13.1) AND the continuous silence accumulated since the
        last qualifying detection has not yet reached listen_timeout_s; while no
        qualifying detection has fired the gate stays closed and nothing is
        injected (需求 13.2 / 13.3); once the silence run reaches the timeout the
        gate closes and the machine returns to listening (需求 13.4); a fresh
        qualifying detection re-opens it and resets the silence accumulator.
    The observe_* return value, is_gate_open() and the reference model are all
    asserted to agree on every step.
    """
    cfg, observations = case

    gate = WakeWord(cfg)
    # Mirror the impl's (clamped) effective threshold / timeout exactly so the
    # reference model and the gate are driven by identical parameters.
    ref = _WakeWordReference(
        gate.enabled, gate.confidence_threshold, gate.listen_timeout_s
    )

    # Initial state, before any observation: open iff disabled (需求 13.6),
    # closed while enabled and not yet triggered (需求 13.2 / 13.3).
    assert gate.is_gate_open() == ref.is_gate_open()
    if not cfg.enabled:
        assert gate.is_gate_open() is True
    else:
        assert gate.is_gate_open() is False

    for kind, value in observations:
        if kind == "detection":
            returned = gate.observe_detection(value)
            exp_returned = ref.observe_detection(value)
        else:  # silence
            returned = gate.observe_silence(value)
            exp_returned = ref.observe_silence(value)

        # The observe_* return value reports the resulting gate state and
        # matches the independent reference model.
        assert returned == exp_returned, (kind, value, returned, exp_returned)
        # is_gate_open() agrees with the reference model AND with the value the
        # observe_* call just returned (internal consistency).
        assert gate.is_gate_open() == ref.is_gate_open(), (kind, value)
        assert gate.is_gate_open() == returned, (kind, value)

        if not cfg.enabled:
            # Disabled -> the gate is open for EVERY observation (需求 13.6).
            assert gate.is_gate_open() is True, (kind, value)
        elif gate.is_gate_open():
            # Open while enabled implies a qualifying detection has fired and
            # the accumulated silence is still below the timeout (需求 13.1/13.4).
            assert ref.triggered is True
            assert ref.silence_accum < ref.timeout


# ---------------------------------------------------------------------------
# Property 1: serialize/parse round-trip
#
# Target: ProtocolGateway.serialize / parse (both pure). The strategy below
# builds a VALID instance of EVERY protocol event variant — the five
# ServerEvent (FullTextEvent, SetModelEvent, AudioEvent, ControlEvent,
# ErrorEvent) and the six ClientEvent (TextInputEvent, MicAudioDataEvent,
# MicAudioEndEvent, InterruptSignalEvent, FetchConfigsEvent,
# PlaybackCompleteEvent) — combined with st.one_of, so 需求 9.4's "FOR ALL
# 合法的协议事件对象" is exercised in both directions (the gateway reconstructs
# server- and client-bound events alike from the shared type registry).
#
# The generators deliberately cover the boundaries called out by the design:
# empty AND Unicode strings (UTF-8-safe, NO lone surrogates so serialize's
# json.dumps and parse's raw.encode('utf-8') never raise), empty AND non-empty
# volumes lists, audio None vs base64-ish strings, actions None vs
# {"expressions": [ints]}, nested display_text / model_info dicts, every
# ControlEvent.text and ErrorEvent.code Literal choice, and varied
# at_text_index / sample_rate / slice_length ints plus the forwarded bool. All
# generated dict/list payloads contain only JSON-serializable, equality-stable
# values (string keys; scalars are str / bounded int / bool / None; volumes are
# finite floats), so each event survives a JSON round-trip unchanged and frozen
# dataclass equality is decisive.
# ---------------------------------------------------------------------------

import json

from omnilimb_face.protocol.events import (
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
    SetModelEvent,
    TextInputEvent,
)
from omnilimb_face.protocol.gateway import ProtocolGateway

# UTF-8-encodable text (codec="utf-8" excludes lone surrogates) spanning the
# empty string (max_size=0 draws), ASCII, and arbitrary Unicode. Used for every
# free-form str field so empty + Unicode boundaries are always in play.
_RT_TEXT = st.text(st.characters(codec="utf-8"), max_size=24)

# ControlEvent.text Literal choices: the seven frontend control signals.
_CONTROL_TEXTS = (
    "start-mic",
    "stop-mic",
    "mic-audio-end",
    "conversation-chain-start",
    "conversation-chain-end",
    "interrupt",
    "mouth-reset",
)

# ErrorEvent.code Literal choices: the four protocol error classifications.
_ERROR_CODES = ("invalid_json", "schema_invalid", "unsupported_type", "too_large")

# base64-ish audio payload (the wire shape for AudioEvent.audio /
# MicAudioDataEvent.audio). Any str round-trips, but mirror the real format and
# allow the empty string.
_B64_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
_b64ish = st.text(alphabet=_B64_ALPHABET, max_size=40)

# JSON-safe, equality-stable scalars for nested dicts (display_text /
# model_info): str / bounded int / bool / None. Floats are intentionally kept
# out of the generic dicts so dict equality is bulletproof; the typed float
# boundary lives in `volumes` below.
_json_scalar = st.one_of(
    _RT_TEXT,
    st.integers(min_value=-(10 ** 9), max_value=10 ** 9),
    st.booleans(),
    st.none(),
)

# A JSON object with UTF-8-safe string keys and nested (depth <= 2) values:
# scalars, lists of scalars, or one level of nested string-keyed objects. Every
# leaf is JSON-serializable and equality-stable, so the dict survives the JSON
# round-trip unchanged (dict equality is order-independent).
_json_object = st.dictionaries(
    _RT_TEXT,
    st.one_of(
        _json_scalar,
        st.lists(_json_scalar, max_size=4),
        st.dictionaries(_RT_TEXT, _json_scalar, max_size=4),
    ),
    max_size=6,
)

# Finite, normalized RMS volume samples. NaN / Infinity are excluded: they are
# not valid JSON and NaN is not equality-stable (NaN != NaN). All finite floats
# round-trip exactly through json (Python uses repr, which is round-trippable).
_volume = st.floats(min_value=0.0, max_value=1.0, allow_nan=False,
                    allow_infinity=False)

# actions: None OR {"expressions": [int, ...]} (covers the empty list too).
_actions = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {"expressions": st.lists(st.integers(min_value=0, max_value=64),
                                 max_size=8)}
    ),
)

# -- Per-variant builders: each yields a single VALID dataclass instance. The
# discriminant `type` field carries a default on every event, so st.builds
# leaves it at its correct literal and never overrides it.
_server_events = (
    st.builds(FullTextEvent, text=_RT_TEXT),
    st.builds(
        SetModelEvent,
        model_info=_json_object,
        conf_name=_RT_TEXT,
        conf_uid=_RT_TEXT,
    ),
    st.builds(
        AudioEvent,
        audio=st.one_of(st.none(), _b64ish),       # None vs base64-ish.
        volumes=st.lists(_volume, max_size=8),      # empty AND non-empty.
        slice_length=st.integers(min_value=0, max_value=10_000),
        display_text=_json_object,                  # nested dict.
        actions=_actions,                           # None vs {"expressions": [...]}.
        forwarded=st.booleans(),
    ),
    st.builds(ControlEvent, text=st.sampled_from(_CONTROL_TEXTS)),
    st.builds(ErrorEvent, code=st.sampled_from(_ERROR_CODES), reason=_RT_TEXT),
)
_client_events = (
    st.builds(TextInputEvent, text=_RT_TEXT),
    st.builds(
        MicAudioDataEvent,
        audio=_b64ish,
        sample_rate=st.integers(min_value=0, max_value=192_000),
    ),
    st.builds(MicAudioEndEvent),
    st.builds(
        InterruptSignalEvent,
        at_text_index=st.integers(min_value=0, max_value=10 ** 6),
    ),
    st.builds(FetchConfigsEvent),
    st.builds(PlaybackCompleteEvent),
)

# One strategy over EVERY ServerEvent + ClientEvent variant (需求 9.4: FOR ALL).
_protocol_events = st.one_of(*_server_events, *_client_events)


# Feature: open-llm-vtuber-plugin, Property 1: serialize/parse round-trip
# Validates: 需求 9.1, 9.2, 9.4
@settings(max_examples=100, deadline=None)
@given(_protocol_events)
def test_property1_serialize_parse_round_trip(event):
    """serialize then parse reproduces the original event on ALL protocol fields.

    For ANY valid ServerEvent or ClientEvent variant — covering empty/Unicode
    strings, empty and non-empty volumes, audio None vs base64-ish, actions None
    vs {"expressions": [...]}, nested display_text / model_info dicts, every
    ControlEvent.text and ErrorEvent.code Literal choice, and varied
    integer/bool fields:
      * serialize(e) is a SINGLE JSON string (需求 9.1) that json.loads to a dict
        carrying the event's discriminant "type";
      * parse(serialize(e)) == ParseOutcome(ok=True, event=e) — the round-trip
        reconstructs an event equal to the original on every protocol field via
        frozen-dataclass equality (需求 9.4), parsing a sub-1-MiB message into an
        internal event object in BOTH directions (需求 9.2; the gateway rebuilds
        server- and client-bound events alike).
    """
    gateway = ProtocolGateway()

    wire = gateway.serialize(event)

    # serialize(e) is a single, valid JSON string carrying the right type (需求 9.1).
    assert isinstance(wire, str)
    loaded = json.loads(wire)
    assert isinstance(loaded, dict)
    assert loaded["type"] == event.type

    # Round-trip (需求 9.4 / 9.2): parse rebuilds an event equal to the original
    # on ALL protocol fields, with ok=True and no error, in both directions.
    outcome = gateway.parse(wire)
    assert outcome == ParseOutcome(ok=True, event=event), (outcome, event)
    assert outcome.ok is True
    assert outcome.error is None
    assert type(outcome.event) is type(event)
    assert outcome.event == event, (outcome.event, event)

# ---------------------------------------------------------------------------
# Property 2: parse error classification
#
# Target: ProtocolGateway.parse (pure, never raises). Where Property 1 pins the
# happy path, Property 2 pins the FOUR-way error model: every adversarial
# inbound message is classified into exactly one ProtocolError.code, with
# ok=False and event=None, and parse never raises (需求 9.5). The strategy
# partitions generated messages by their EXPECTED code and carries that code
# alongside each raw message so the test asserts the precise classification:
#
#   * too_large       (需求 9.7): a message whose UTF-8 byte size EXCEEDS the
#                      gateway's configured max_message_bytes. Built against a
#                      DELIBERATELY SMALL limit (64..512 bytes) so generation is
#                      cheap, and since size is checked FIRST even an otherwise
#                      well-formed-but-oversize message classifies as too_large.
#   * invalid_json    (需求 9.3): byte-bounded text that is NOT valid JSON —
#                      guaranteed-bad prefixes + truncated braces, with a final
#                      json.loads safety filter so an accidental valid value can
#                      never slip through.
#   * unsupported_type(需求 9.6): VALID JSON that is either not an object, an
#                      object lacking "type", an object whose "type" is not a
#                      string, or a string "type" that is not one of the known
#                      registry names.
#   * schema_invalid  (需求 9.3): a KNOWN type carrying a non-conforming payload
#                      (missing required field / wrong field type / unknown
#                      extra key) so reconstruction fails AFTER the type is
#                      recognised.
#
# Unknown-type strings are filtered against the real registry names and extra
# keys against every event field name, so a generated "negative" case can never
# accidentally be a valid message. The too_large category uses a small-limit
# gateway; the other three use a default (1 MiB) gateway and stay well under it.
# ---------------------------------------------------------------------------

# The eleven known protocol discriminants (server- and client-bound). An
# adversarial "unknown type" string is filtered against this set so it can never
# accidentally name a real type.
_P2_KNOWN_TYPES = (
    "full-text",
    "set-model-and-conf",
    "audio",
    "control",
    "error",
    "text-input",
    "mic-audio-data",
    "mic-audio-end",
    "interrupt-signal",
    "fetch-configs",
    "frontend-playback-complete",
)

# Every field name across all event dataclasses; an injected "unknown extra
# key" is filtered against this set so it is guaranteed not to be a real field
# of ANY event (hence reconstruction rejects it -> schema_invalid).
_P2_ALL_FIELD_NAMES = frozenset({
    "type", "text", "model_info", "conf_name", "conf_uid", "audio",
    "volumes", "slice_length", "display_text", "actions", "forwarded",
    "code", "reason", "sample_rate", "at_text_index",
})


def _p2_json_parses(s):
    """True iff ``s`` parses as JSON (used as an invalid-JSON safety filter)."""
    try:
        json.loads(s)
        return True
    except (ValueError, UnicodeDecodeError):
        return False


# -- too_large (需求 9.7) ----------------------------------------------------
@st.composite
def _too_large_case(draw):
    """A message whose UTF-8 byte size strictly EXCEEDS a small limit.

    Returns ``(raw, "too_large", limit)``. The limit is small (64..512) so the
    oversize body stays cheap to build; ``flavor`` mixes pure garbage, padded
    ASCII text, and an otherwise-VALID JSON event padded past the limit (size is
    checked first, so it must still classify as too_large).
    """
    limit = draw(st.integers(min_value=64, max_value=512))
    overage = draw(st.integers(min_value=1, max_value=128))
    body_len = limit + overage  # ASCII chars => exactly body_len UTF-8 bytes.
    flavor = draw(st.sampled_from(("garbage", "padded_text", "valid_json")))
    if flavor == "valid_json":
        # A schema-valid text-input event whose text alone exceeds the limit.
        raw = json.dumps({"type": "text-input", "text": "x" * body_len})
    elif flavor == "padded_text":
        raw = draw(st.text(alphabet="abcXYZ0123 {}\":,",
                           min_size=body_len, max_size=body_len + 16))
    else:
        raw = "A" * body_len
    return raw, "too_large", limit


# -- invalid_json (需求 9.3) -------------------------------------------------
# Free-form text prefixed with a token that can never begin a valid JSON value,
# so the whole string is guaranteed unparseable regardless of the suffix.
_P2_BAD_PREFIX = st.sampled_from(
    ("@", "!@#", "<<<", "}{", "][", "''", "\x00bad", "not json:", "%%", ")(")
)
_p2_invalid_json_text = st.builds(lambda p, t: p + t, _P2_BAD_PREFIX,
                                  st.text(max_size=24))
# Hand-picked malformed fragments: truncated containers, single quotes, bare
# keywords, malformed numbers -- all rejected by json.loads.
_p2_truncated_json = st.sampled_from((
    "{", "[", "}", "]", ":", ",", "{,}", "[,]",
    '{"type":', '{"type": "audio"', '[1, 2,', '{"a": 1',
    "'single'", "{'type': 'audio'}", '{"x": undefined}',
    "tru", "fals", "nul", "NaNN", "01.2.3", "{\"k\": ,}",
))
# Final json.loads filter is a belt-and-suspenders guard; both branches above
# are already guaranteed invalid, so it effectively never rejects.
_p2_invalid_json = st.one_of(_p2_invalid_json_text, _p2_truncated_json).filter(
    lambda s: not _p2_json_parses(s)
)


# -- unsupported_type (需求 9.6) ---------------------------------------------
# (a) VALID JSON that is NOT an object.
_p2_non_object = st.one_of(
    st.integers(min_value=-1000, max_value=1000),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
    _RT_TEXT,
    st.booleans(),
    st.none(),
    st.lists(_json_scalar, max_size=5),
)
# (b) An object with NO "type" key (may be empty).
_p2_no_type_obj = st.dictionaries(
    st.text(max_size=8).filter(lambda k: k != "type"),
    _json_scalar,
    max_size=5,
)
# (c) An object whose "type" is present but NOT a string.
_p2_nonstring_type_obj = st.builds(
    lambda v, rest: {**rest, "type": v},
    st.one_of(
        st.integers(),
        st.booleans(),
        st.none(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=4), st.integers(), max_size=3),
    ),
    st.dictionaries(st.text(max_size=6).filter(lambda k: k != "type"),
                    _json_scalar, max_size=3),
)
# (d) An object with a STRING "type" that is not a known registry name.
_p2_unknown_type_obj = st.builds(
    lambda t, rest: {**rest, "type": t},
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz-_0123456789", max_size=20).filter(
        lambda s: s not in _P2_KNOWN_TYPES
    ),
    st.dictionaries(st.text(max_size=6).filter(lambda k: k != "type"),
                    _json_scalar, max_size=3),
)
_p2_unsupported_payload = st.one_of(
    _p2_non_object, _p2_no_type_obj, _p2_nonstring_type_obj, _p2_unknown_type_obj,
)


# -- schema_invalid (需求 9.3) -----------------------------------------------
# Minimal, schema-VALID payload per known type: the ONLY reason a corrupted copy
# fails to reconstruct is the corruption applied below.
_P2_MINIMAL_VALID = {
    "full-text": {"type": "full-text", "text": "hi"},
    "set-model-and-conf": {"type": "set-model-and-conf", "model_info": {}},
    "audio": {"type": "audio", "audio": None},
    "control": {"type": "control", "text": "start-mic"},
    "error": {"type": "error", "code": "invalid_json", "reason": "x"},
    "text-input": {"type": "text-input", "text": "hi"},
    "mic-audio-data": {"type": "mic-audio-data", "audio": "AAAA"},
    "mic-audio-end": {"type": "mic-audio-end"},
    "interrupt-signal": {"type": "interrupt-signal"},
    "fetch-configs": {"type": "fetch-configs"},
    "frontend-playback-complete": {"type": "frontend-playback-complete"},
}
# Removable required fields (no dataclass default) per type.
_P2_REQUIRED_FIELDS = {
    "full-text": ("text",),
    "set-model-and-conf": ("model_info",),
    "audio": ("audio",),
    "control": ("text",),
    "error": ("code", "reason"),
    "text-input": ("text",),
    "mic-audio-data": ("audio",),
}
# (field, wrong-value strategy) per type whose schema can be violated by type.
# Each strategy yields ONLY JSON-serializable values that the field's annotation
# rejects (audio is Optional[str], so None is excluded there; the Literal fields
# exclude their own valid members via a filter).
_P2_WRONG_TYPE = {
    "full-text": ("text", st.one_of(st.integers(), st.booleans(), st.none(),
                                    st.lists(st.integers(), max_size=2))),
    "text-input": ("text", st.one_of(st.integers(), st.booleans(), st.none(),
                                     st.lists(st.integers(), max_size=2))),
    "set-model-and-conf": ("model_info", st.one_of(
        st.integers(), st.text(max_size=5), st.booleans(), st.none(),
        st.lists(st.integers(), max_size=2))),
    "audio": ("audio", st.one_of(
        st.integers(), st.booleans(), st.lists(st.integers(), max_size=2),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=2))),
    "mic-audio-data": ("audio", st.one_of(
        st.integers(), st.booleans(), st.none(),
        st.lists(st.integers(), max_size=2))),
    "control": ("text", st.one_of(
        st.text(max_size=10).filter(lambda s: s not in _CONTROL_TEXTS),
        st.integers(), st.booleans(), st.none())),
    "error": ("code", st.one_of(
        st.text(max_size=10).filter(lambda s: s not in _ERROR_CODES),
        st.integers(), st.booleans(), st.none())),
    "interrupt-signal": ("at_text_index", st.one_of(
        st.text(max_size=5), st.booleans(), st.none(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.lists(st.integers(), max_size=2))),
}
# An unknown extra key: guaranteed not to be a field of ANY event.
_p2_extra_key = st.text(alphabet="abcdefghijkXYZ_-0123456789",
                        min_size=1, max_size=12).filter(
    lambda k: k not in _P2_ALL_FIELD_NAMES
)


@st.composite
def _schema_invalid_case(draw):
    """A KNOWN-type message corrupted so reconstruction fails (schema_invalid).

    Returns ``(raw, "schema_invalid", None)``. The discriminant ``type`` is
    always left intact (so classification reaches the schema stage), and exactly
    one applicable corruption is applied: an unknown extra key (any type), a
    removed required field (types that have one), or a wrong-typed field.
    """
    type_name = draw(st.sampled_from(tuple(_P2_MINIMAL_VALID)))
    base = dict(_P2_MINIMAL_VALID[type_name])

    corruptions = ["extra_key"]
    if type_name in _P2_REQUIRED_FIELDS:
        corruptions.append("missing_required")
    if type_name in _P2_WRONG_TYPE:
        corruptions.append("wrong_type")

    kind = draw(st.sampled_from(corruptions))
    if kind == "extra_key":
        base[draw(_p2_extra_key)] = draw(_json_scalar)
    elif kind == "missing_required":
        base.pop(draw(st.sampled_from(_P2_REQUIRED_FIELDS[type_name])), None)
    else:  # wrong_type
        field, wrong_values = _P2_WRONG_TYPE[type_name]
        base[field] = draw(wrong_values)

    return json.dumps(base), "schema_invalid", None


# One strategy drawing (raw, expected_code, limit) from every error category.
# ``limit`` is the small max_message_bytes for the too_large gateway, or None to
# use a default (1 MiB) gateway for the other three categories.
_parse_error_case = st.one_of(
    _too_large_case(),
    st.builds(lambda raw: (raw, "invalid_json", None), _p2_invalid_json),
    st.builds(lambda payload: (json.dumps(payload), "unsupported_type", None),
              _p2_unsupported_payload),
    _schema_invalid_case(),
)


# Feature: open-llm-vtuber-plugin, Property 2: parse error classification
# Validates: 需求 9.3, 9.6, 9.7
@settings(max_examples=100, deadline=None)
@given(_parse_error_case)
def test_property2_parse_error_classification(case):
    """parse classifies every adversarial message into the right error code.

    For ANY message drawn from the four error partitions:
      * too_large (需求 9.7): a body whose UTF-8 size exceeds the configured
        max_message_bytes -> code "too_large" (size is checked FIRST, so even an
        otherwise-valid-but-oversize message lands here);
      * invalid_json (需求 9.3): non-JSON text -> code "invalid_json";
      * unsupported_type (需求 9.6): valid JSON that is not an object, lacks a
        "type", has a non-string "type", or names an unknown type -> code
        "unsupported_type";
      * schema_invalid (需求 9.3): a known type with a non-conforming payload
        -> code "schema_invalid".
    In every case parse does NOT raise and returns ok=False with event=None and
    error.code equal to the expected classification (需求 9.5 keeps the
    connection usable because parse never raises).
    """
    from types import SimpleNamespace  # local import: avoid module-level redefinition

    raw, expected_code, limit = case

    if limit is None:
        gateway = ProtocolGateway()  # default 1 MiB limit.
    else:
        gateway = ProtocolGateway(SimpleNamespace(max_message_bytes=limit))
        # Sanity: the too_large body really does exceed the configured limit.
        assert len(raw.encode("utf-8")) > limit, (len(raw.encode("utf-8")), limit)

    # parse() must never raise, whatever the input (需求 9.5).
    try:
        outcome = gateway.parse(raw)
    except Exception as exc:  # pragma: no cover - parse() is contractually total.
        raise AssertionError(f"parse() raised {exc!r} for {raw!r}")

    # Failure is reported (not raised): no event, an error, and the EXACT code.
    assert outcome.ok is False, (expected_code, raw, outcome)
    assert outcome.event is None, (expected_code, raw, outcome)
    assert outcome.error is not None, (expected_code, raw, outcome)
    assert outcome.error.code == expected_code, (outcome.error, expected_code, raw)


# ---------------------------------------------------------------------------
# Property 9: playback order preservation
#
# Target: TTSPlayer ordered playback queue (enqueue + expected-seq cursor +
# worker, Task 9.3). The generator draws a segment count N (1..12) and a RANDOM
# PERMUTATION of the text-order sequence indices 0..N-1 — the order in which
# synthesis "finishes" and enqueue() is called (the READINESS order). Each
# segment's wav_bytes ENCODE its own text-order seq (seq.to_bytes(4, "big")) so
# the order the sink actually plays them in can be decoded back and checked
# exactly.
#
# A fresh TTSPlayer wired to a thread-safe recording AudioSink is fed the N
# segments in the shuffled readiness order, each carrying its EXPLICIT
# text-order seq. After wait_until_idle drains the queue, the sink must have
# received the segments in NON-DECREASING seq order — i.e. exactly
# 0,1,2,...,N-1 — regardless of the shuffled enqueue order, because the worker
# holds back any early-arriving segment until all of its lower-seq predecessors
# have played (需求 6.2). Enqueuing a full 0..N-1 permutation leaves NO gaps, so
# the queue always drains to the complete ordered sequence.
# ---------------------------------------------------------------------------


@st.composite
def _playback_permutation(draw):
    """Draw ``(n, readiness_order)`` for the Property 9 playback-order test.

    ``n`` is a modest segment count (1..12, kept small so the worker-thread test
    stays fast) and ``readiness_order`` is a RANDOM PERMUTATION of the
    text-order sequence indices ``0..n-1`` — the order in which the segments
    "finish synthesising" and are enqueued. A full permutation (no gaps) means
    the player always drains to the complete ordered sequence.
    """
    n = draw(st.integers(min_value=1, max_value=12))
    readiness_order = draw(st.permutations(list(range(n))))
    return n, list(readiness_order)


# Feature: open-llm-vtuber-plugin, Property 9: playback order preservation
# Validates: 需求 6.2
@settings(max_examples=100, deadline=None)
@given(_playback_permutation())
def test_property9_playback_order_preservation(case):
    """Segments play in non-decreasing text-order seq, whatever the enqueue order.

    For ANY segment count N (1..12) and ANY random permutation of the text-order
    sequence indices 0..N-1 used as the enqueue (readiness) order: enqueuing each
    AudioSegmentOut on a fresh TTSPlayer with its EXPLICIT text-order seq and
    then waiting for the queue to drain, the recording AudioSink must have
    received the segments in EXACTLY 0,1,2,...,N-1 order (需求 6.2) — the worker
    holds back early arrivals until every lower-seq predecessor has played, so
    out-of-order readiness can never reorder playback.
    """
    import threading  # local import: avoid top-level redefinition

    # Reuse the module-level TTSPlayer import; bring in AudioSegmentOut locally.
    from omnilimb_face.tts import AudioSegmentOut

    n, readiness_order = case

    class _RecordingSink:
        """Deterministic AudioSink recording each played wav_bytes under a lock."""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.played: list = []

        def play(self, wav_bytes: bytes) -> None:
            with self._lock:
                self.played.append(wav_bytes)

        def stop(self) -> None:  # barge-in halt; unused by this drain test.
            pass

    def _segment_for(seq: int) -> AudioSegmentOut:
        # wav_bytes ENCODES the segment's own text-order seq so the order the
        # sink plays them in can be decoded back exactly.
        return AudioSegmentOut(
            wav_bytes=seq.to_bytes(4, "big"),
            volumes=[],
            slice_length_ms=20,
            display_text=f"s{seq}",
            expressions=[],
        )

    sink = _RecordingSink()
    player = TTSPlayer(sink=sink)
    try:
        # Enqueue in the SHUFFLED readiness order, each carrying its EXPLICIT
        # text-order seq (so submission order can never matter).
        for seq in readiness_order:
            player.enqueue(_segment_for(seq), seq=seq)

        # A full 0..N-1 permutation has no gaps, so the queue drains fully and
        # wait_until_idle returns True (not timed out, not stopped).
        drained = player.wait_until_idle(timeout=5.0)
        assert drained is True, (n, readiness_order)
    finally:
        # Stop/drain the worker thread so no threads leak across examples.
        player.stop()

    # Decode each played payload back to its text-order seq.
    played_seqs = [int.from_bytes(b, "big") for b in sink.played]

    # Every enqueued segment reached the sink exactly once...
    assert len(played_seqs) == n, (played_seqs, n)
    # ...in NON-DECREASING seq order, i.e. exactly 0,1,2,...,N-1 (需求 6.2),
    # regardless of the shuffled enqueue (readiness) order.
    assert played_seqs == sorted(played_seqs), (played_seqs, readiness_order)
    assert played_seqs == list(range(n)), (played_seqs, readiness_order)


# ---------------------------------------------------------------------------
# Property 6: inject vs blank rejection
#
# Target: STTEngine.is_blank (pure predicate, 需求 4.5) + the LLM_Bridge inject
# path (LLMBridge.inject_user_utterance -> ctx.inject_message(text,
# role="user"), 需求 4.4). The generator draws arbitrary transcript text that
# deliberately spans every boundary of the blank predicate:
#   * the empty string;
#   * whitespace-ONLY text built from spaces, tabs, newlines, carriage returns,
#     vertical/form feeds, the full-width (U+3000) space and other Unicode
#     spaces — all blank;
#   * visible text (ASCII + CJK/kana) with at least one non-whitespace char —
#     never blank;
#   * arbitrary UTF-8 text — sometimes blank, sometimes not.
#
# Two invariants are pinned in one shot. First, the predicate itself:
# is_blank(t) is True IFF t.strip() == "" (需求 4.5). Second, the runtime
# "drop blank / inject non-blank" gate (需求 4.4 / 4.5), modelled by a small
# helper that mirrors the runtime exactly — it calls
# LLMBridge.inject_user_utterance ONLY when the text is not blank, against a
# recording fake ctx. The number of ctx.inject_message calls must be 0 for a
# blank transcript (dropped, never injected) and EXACTLY 1 for a non-blank one,
# and the single recorded injection must carry (content, role) == (t, "user").
# ---------------------------------------------------------------------------

# Whitespace characters that str.strip() removes: ASCII space/tab/newline/CR,
# vertical tab + form feed, the full-width (U+3000) space, the no-break space
# (U+00A0) and an em space (U+2003). A string built solely from these is blank.
_BLANK_CHARS = " \t\n\r\x0b\x0c\u3000\u00a0\u2003"

# Visible (non-whitespace) characters: ASCII letters/digits/punctuation plus
# CJK + kana. NONE of these is whitespace, so any text containing at least one
# of them is guaranteed NOT blank.
_VISIBLE_CHARS = "abcXYZ012!?.,你好世界こんにちは"


@st.composite
def _transcript_text(draw):
    """Draw a transcript ``t`` spanning every blank/non-blank boundary.

    Mixes four sources so both gate branches are exercised heavily: the empty
    string, whitespace-only text (spaces/tabs/newlines/full-width spaces — all
    blank), visible text with a guaranteed non-whitespace char (never blank),
    and arbitrary UTF-8 text (either way). The whitespace and full-width-space
    coverage makes the blank/non-blank boundary well exercised, as required.
    """
    kind = draw(st.sampled_from(("empty", "blank", "visible", "mixed")))
    if kind == "empty":
        return ""
    if kind == "blank":
        # Whitespace-only (incl. tabs/newlines/full-width spaces); may be "".
        return draw(st.text(alphabet=_BLANK_CHARS, max_size=12))
    if kind == "visible":
        # At least one visible char -> t.strip() is non-empty -> never blank.
        return draw(st.text(alphabet=_VISIBLE_CHARS, min_size=1, max_size=20))
    # Arbitrary UTF-8 (no lone surrogates): sometimes blank, sometimes not.
    return draw(st.text(st.characters(codec="utf-8"), max_size=24))


# Feature: open-llm-vtuber-plugin, Property 6: inject vs blank rejection
# Validates: 需求 4.4, 4.5
@settings(max_examples=100, deadline=None)
@given(_transcript_text())
def test_property6_inject_vs_blank_rejection(text):
    """Blank transcripts are dropped (never injected); non-blank ones inject
    exactly one user message equal to the text.

    For ANY transcript text — empty, whitespace-only (spaces/tabs/newlines/
    full-width spaces), visible, or arbitrary Unicode:
      * is_blank(t) is True IFF t.strip() == "" — the 需求 4.5 blank predicate.
      * The runtime gate (mirrored by ``_gate``) injects ONLY when the text is
        not blank: the recording fake ctx receives 0 inject_message calls for a
        blank transcript (dropped, never injected — 需求 4.5) and EXACTLY 1 for a
        non-blank one, and that single recorded call carries
        (content, role) == (t, "user") (需求 4.4).
    """
    # New names imported locally to avoid top-level redefinition; VTuberConfig
    # and SentenceChunker are reused from the module-level imports.
    from omnilimb_face.stt import STTEngine
    from omnilimb_face.llm_bridge import LLMBridge

    class _RecordingCtx:
        """Deterministic fake host ctx: records inject_message(content, role)."""

        def __init__(self) -> None:
            self.calls: list = []

        def inject_message(self, content, role):
            # LLMBridge calls inject_message(text, role="user"): content is
            # positional, role is the keyword. Record both, report success.
            self.calls.append((content, role))
            return True

    def _gate(bridge, t):
        """Mirror the runtime drop-blank / inject-non-blank gate exactly.

        Inject (trigger a host turn) ONLY when the transcript is not blank; a
        blank transcript is dropped without ever touching ctx.inject_message.
        """
        if STTEngine.is_blank(t):
            return False
        return bridge.inject_user_utterance(t)

    # Invariant 1 (需求 4.5): the blank predicate equals the strip definition.
    blank = STTEngine.is_blank(text)
    assert blank == (text.strip() == ""), (repr(text), blank)
    assert isinstance(blank, bool)

    # Invariant 2 (需求 4.4 / 4.5): drive the gate with a recording fake ctx.
    ctx = _RecordingCtx()
    bridge = LLMBridge(ctx, VTuberConfig(), SentenceChunker())

    injected = _gate(bridge, text)

    if blank:
        # Blank -> dropped: no host turn triggered, ctx.inject_message untouched.
        assert injected is False, (repr(text), injected)
        assert ctx.calls == [], (repr(text), ctx.calls)
    else:
        # Non-blank -> exactly one injection of the text as a "user" message.
        assert injected is True, (repr(text), injected)
        assert len(ctx.calls) == 1, (repr(text), ctx.calls)
        content, role = ctx.calls[0]
        assert content == text, (repr(content), repr(text))
        assert role == "user", role

    # Call count is exactly 0 when blank, exactly 1 otherwise (no double-inject).
    assert len(ctx.calls) == (0 if blank else 1), (repr(text), ctx.calls)


# ---------------------------------------------------------------------------
# Property 13: microphone availability gating
#
# Target: VoiceCapture.start_hands_free, whose activation decision is made
# SOLELY from source.list_input_devices(). The generator draws a random
# (possibly empty) list of input-device names and feeds it to a FAKE
# AudioSource whose start()/stop()/frames() are inert no-ops (frames() yields
# nothing) — so no real microphone, capture thread work, or optional [voice]
# dependency is ever involved. The fake source is driven through a real
# VoiceCapture + real VadSegmenter and start_hands_free() is invoked:
#   * NON-EMPTY device list -> hands-free ACTIVATES (StartResult.activated /
#     .success True, is_running() True);
#   * EMPTY device list      -> hands-free does NOT activate (activated /
#     success False, is_running() False) AND the result reports the microphone
#     as unavailable.
# In BOTH cases the call returns cleanly (never raises), so text interaction +
# avatar rendering stay available — the gate is purely the enumerated device
# list (需求 4.9 / 11.5 / 12.4).
# ---------------------------------------------------------------------------

# Input-device names may be arbitrary strings; the list MAY be empty so BOTH
# the activate (non-empty) and refuse (empty) branches of the gate are reached.
_device_names = st.lists(st.text(max_size=24), max_size=6)


# Feature: open-llm-vtuber-plugin, Property 13: microphone availability gating
# Validates: 需求 4.9, 11.5, 12.4
@settings(max_examples=100, deadline=None)
@given(_device_names)
def test_property13_microphone_availability_gating(devices):
    """hands-free activation is gated SOLELY on the enumerated device list.

    For ANY list of input-device names (possibly empty), driven through a real
    VoiceCapture + real VadSegmenter over an inert fake AudioSource:
      * non-empty -> start_hands_free ACTIVATES (StartResult.activated /
        .success True, is_running() True) — 需求 11.4 capture path;
      * empty     -> it REFUSES (activated / success False, is_running()
        False) and the result reports the microphone unavailable (需求 4.9 /
        11.5 / 12.4).
    The call always returns a StartResult cleanly (it never raises), so text
    interaction and avatar rendering remain available regardless — the gating
    is purely the device list. stop_hands_free() is always called at the end so
    a started consumer thread can never leak.
    """
    # New names imported locally to avoid top-level redefinition; VTuberConfig,
    # VADSettings and VadSegmenter are reused from the module-level imports.
    from omnilimb_face.voice.capture import StartResult, VoiceCapture

    class _FakeAudioSource:
        """Inert AudioSource: the device list drives the gate; capture no-ops.

        ``start``/``stop`` do nothing and ``frames`` yields no frames, so the
        consumer loop (only started in the activate branch) finishes
        immediately — no real microphone, thread work, or optional [voice]
        dependency is touched. ``list_input_devices`` returns the randomly
        generated names that the activation gate keys off.
        """

        def __init__(self, device_names):
            self._device_names = list(device_names)

        def start(self):  # inert: no real device acquisition
            return None

        def stop(self):  # inert: nothing to release
            return None

        def frames(self):  # yields nothing -> consumer loop returns at once
            return iter(())

        def list_input_devices(self):  # the SOLE input to the gate
            return list(self._device_names)

    source = _FakeAudioSource(devices)
    capture = VoiceCapture(VTuberConfig(), source, VadSegmenter(VADSettings()))

    # Precondition: nothing is running before start_hands_free is called.
    assert capture.is_running() is False

    try:
        result = capture.start_hands_free()

        # The call ALWAYS returns a StartResult cleanly (never raises), so text
        # interaction + avatar rendering stay available regardless of the gate.
        assert isinstance(result, StartResult), result

        if devices:
            # Non-empty device list -> hands-free activates.
            assert result.activated is True, (devices, result)
            assert result.success is True, (devices, result)
            assert result.error is None, (devices, result)
            assert capture.is_running() is True, (devices, result)
        else:
            # Empty device list -> do NOT activate; report the microphone as
            # unavailable and stay off (需求 4.9 / 11.5 / 12.4). Text + avatar
            # rendering remain available because nothing was started.
            assert result.activated is False, result
            assert result.success is False, result
            assert capture.is_running() is False, result
            # The refusal carries a descriptive microphone-unavailable message.
            assert result.error is not None, result
            assert result.error == result.reason, result
            assert "microphone" in result.reason.lower(), result.reason
            assert "unavailable" in result.reason.lower(), result.reason
    finally:
        # Idempotent stop so a started consumer thread is always joined and can
        # never leak, no matter which branch was taken (or if an assert fired).
        capture.stop_hands_free()

    # After stopping, hands-free is off regardless of the branch taken; a second
    # stop is a harmless no-op (idempotent).
    assert capture.is_running() is False
    capture.stop_hands_free()
    assert capture.is_running() is False


# ---------------------------------------------------------------------------
# Property 12: degraded availability
#
# Target: register(ctx) + VTuberRuntime tool handlers / check_fns. The plugin
# probes its OPTIONAL voice / wake-word / Live2D dependencies via the module
# helper omnilimb_face.runtime._module_available (importlib.util.find_spec over
# VOICE_MODULES / WAKEWORD_MODULES / LIVE2D_MODULES) and the host's TTS
# capability via a callable ctx.dispatch_tool. To simulate ANY missing subset
# WITHOUT uninstalling packages — and to keep the example deterministic and
# hermetic (no sockets / mic / real extras) — the generator draws:
#   (a) a SUBSET of the optional module names to mark "missing", and
#   (b) a bool for whether the fake ctx exposes a callable dispatch_tool
#       (which is exactly what gates avatar speech / TTS availability).
# Each example patches runtime._module_available (via unittest.mock.patch — NOT
# a pytest fixture, since @given + function-scoped fixtures don't mix) so the
# drawn "missing" names report unavailable for the duration of the example, and
# builds a recording fake ctx. register(ctx) must then ALWAYS complete and
# register BOTH tools (they stay visible in `hermes tools` even fully degraded,
# 需求 12.1 / 12.6); vtuber_status always returns valid JSON reflecting the
# degraded/missing info; and vtuber_say returns a descriptive JSON error naming
# the missing dependency/capability when TTS is unavailable (需求 12.2) or a
# JSON success when it is available — while the other tool (vtuber_status) is
# unaffected either way.
# ---------------------------------------------------------------------------


@st.composite
def _degraded_case(draw):
    """Draw ``(missing, has_dispatch)`` for the Property 12 degraded-availability test.

    ``missing`` is a (possibly empty, possibly full) subset of the OPTIONAL
    voice / wake-word / Live2D module names that the example marks unavailable —
    so "ANY subset of missing optional dependencies" is exercised, from a fully
    healthy install (empty subset) to every optional extra absent.
    ``has_dispatch`` toggles whether the fake ctx exposes a callable
    dispatch_tool, which is exactly the capability avatar speech (TTS) depends
    on. The optional module names are read locally from the runtime module so
    the draw always mirrors the real VOICE_MODULES / WAKEWORD_MODULES /
    LIVE2D_MODULES groups.
    """
    from omnilimb_face import runtime as runtime_module

    optional_names = tuple(
        runtime_module.VOICE_MODULES
        + runtime_module.WAKEWORD_MODULES
        + runtime_module.LIVE2D_MODULES
    )
    missing = draw(
        st.lists(
            st.sampled_from(optional_names),
            unique=True,
            max_size=len(optional_names),
        )
    )
    has_dispatch = draw(st.booleans())
    return frozenset(missing), has_dispatch


# Feature: open-llm-vtuber-plugin, Property 12: degraded availability
# Validates: 需求 12.1, 12.2, 12.6
@settings(max_examples=100, deadline=None)
@given(_degraded_case())
def test_property12_degraded_availability(case):
    """register ALWAYS succeeds and keeps both tools visible under ANY missing
    optional-dependency subset; a deps-missing tool errors descriptively while a
    deps-present tool works, and the other tool is unaffected.

    For ANY subset of missing optional voice / wake-word / Live2D dependencies
    (simulated by patching runtime._module_available) and ANY host TTS
    availability (whether the fake ctx exposes a callable dispatch_tool):
      * register(ctx) does NOT raise and registers BOTH the vtuber_status and
        vtuber_say tools — they stay VISIBLE in `hermes tools` even fully
        degraded (需求 12.1 / 12.6). Their check_fns reflect availability but
        never gate registration.
      * vtuber_status ALWAYS returns a valid JSON string reflecting the
        degraded/missing info (需求 12.6) — unaffected by the missing subset.
      * vtuber_say, when its required capability (TTS via ctx.dispatch_tool) is
        UNAVAILABLE, returns a descriptive JSON error naming the missing
        dependency/capability (需求 12.2); when AVAILABLE it returns a JSON
        success. The other tool (vtuber_status) is unaffected either way.
    """
    # New names imported locally (reuse module-level json / st / settings).
    import os
    import struct
    import tempfile
    import wave
    from unittest import mock

    from omnilimb_face import runtime as runtime_module
    from omnilimb_face.plugin import register

    missing, has_dispatch = case
    # The voice modules actually marked missing, in VOICE_MODULES order — this
    # is exactly what register() snapshots into runtime._missing_voice and what
    # the degraded status / say-error must report.
    missing_voice_expected = [m for m in runtime_module.VOICE_MODULES if m in missing]

    class _RecordingCtx:
        """Deterministic fake host ctx: records every register_* call.

        Exposes a dict ``config`` so ConfigManager.from_host reads it directly
        (hermetic: no host-loader fallback). ``dispatch_tool`` is present ONLY
        when ``dispatch`` is supplied, so its (un)availability drives the TTS
        capability gate exactly like a real host.
        """

        def __init__(self, dispatch=None):
            self.config: dict = {}
            self.hooks: list = []
            self.tools: list = []
            self.cli_commands: list = []
            self.commands: list = []
            if dispatch is not None:
                self.dispatch_tool = dispatch

        def register_hook(self, name, handler):
            self.hooks.append((name, handler))

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_cli_command(self, **kwargs):
            self.cli_commands.append(kwargs)

        def register_command(self, name, handler, **kwargs):
            self.commands.append((name, handler, kwargs))

    def _fake_module_available(name):
        """Report the drawn 'missing' subset as unavailable; everything else present."""
        return name not in missing

    # A TemporaryDirectory keeps the example hermetic: the fake TTS "returns" a
    # real, decodable mono int16 WAV file that is removed when the example ends.
    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = os.path.join(tmpdir, "say.wav")
        with wave.open(wav_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            # A short, non-silent frame buffer so synth + lip-sync volumes work.
            wav_file.writeframes(struct.pack("<" + "h" * 32, *([4000, -4000] * 16)))

        def _fake_dispatch(tool_name, payload=None, *args, **kwargs):
            # The avatar speech path only ever dispatches the host text_to_speech
            # tool; return a valid envelope pointing at the temp WAV file.
            return json.dumps(
                {"success": True, "file_path": wav_path, "provider": "edge"}
            )

        ctx = _RecordingCtx(dispatch=_fake_dispatch if has_dispatch else None)

        # Patch the module-level dependency probe for the DURATION of this
        # example (mock.patch in the test body, NOT a fixture — Hypothesis @given
        # and function-scoped fixtures don't mix). Covers both registration-time
        # probing and the live check_fn / status re-probes.
        with mock.patch.object(
            runtime_module, "_module_available", side_effect=_fake_module_available
        ):
            # register ALWAYS completes, even fully degraded (需求 12.1).
            register(ctx)

            tools = {t["name"]: t for t in ctx.tools}

            # Both tools remain VISIBLE regardless of the missing subset
            # (需求 12.1 / 12.6) — registration never depends on availability.
            assert "vtuber_status" in tools, ctx.tools
            assert "vtuber_say" in tools, ctx.tools
            status_tool = tools["vtuber_status"]
            say_tool = tools["vtuber_say"]

            # check_fns reflect availability but never gated registration:
            #   * vtuber_status's deps_available -> True iff NO voice module missing;
            #   * vtuber_say's tts_available     -> True iff ctx.dispatch_tool present.
            assert callable(status_tool["check_fn"])
            assert callable(say_tool["check_fn"])
            assert status_tool["check_fn"]() == (len(missing_voice_expected) == 0)
            assert say_tool["check_fn"]() is has_dispatch

            # vtuber_status ALWAYS returns valid JSON reflecting degraded/missing
            # info (需求 12.6), regardless of the missing subset or TTS state.
            status_raw = status_tool["handler"]({})
            assert isinstance(status_raw, str)
            status = json.loads(status_raw)
            assert status["ok"] is True
            assert status["tool"] == "vtuber_status"
            assert status["degraded"] is (len(missing_voice_expected) > 0)
            # The missing voice deps are reflected exactly in the status payload.
            assert (
                status["missing_dependencies"]["voice"] == missing_voice_expected
            ), (status["missing_dependencies"], missing_voice_expected)

            # vtuber_say: gated on the TTS capability (ctx.dispatch_tool).
            say_raw = say_tool["handler"]({"text": "hello avatar"})
            assert isinstance(say_raw, str)
            say = json.loads(say_raw)
            assert say["tool"] == "vtuber_say"

            if has_dispatch:
                # Required capability present -> works normally (需求 12.6); the
                # missing voice/Live2D subset does NOT affect this tool.
                assert say["ok"] is True, say
                assert say["spoken"] is True, say
            else:
                # Required capability missing -> descriptive error NAMING the
                # missing dependency/capability (需求 12.2), without raising.
                assert say["ok"] is False, say
                assert say["error"] == "tts_unavailable", say
                assert "dispatch_tool" in say["message"], say
                # The named missing items include the host TTS dispatch capability
                # plus every voice module marked missing this example.
                assert (
                    "ctx.dispatch_tool (host text_to_speech tool)" in say["missing"]
                ), say
                for module_name in missing_voice_expected:
                    assert module_name in say["missing"], (module_name, say)

            # The OTHER tool (vtuber_status) is unaffected either way: it still
            # returns a valid JSON success after vtuber_say's call (需求 12.2).
            status_again = json.loads(status_tool["handler"]({}))
            assert status_again["ok"] is True, status_again
            assert status_again["tool"] == "vtuber_status"
