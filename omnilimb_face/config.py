"""Config_Manager — strongly-typed plugin configuration and merge logic.

This module defines the frozen configuration dataclasses for the omnilimb-face
plugin and the :class:`ConfigManager` that merges raw host configuration into a
fully-defaulted :class:`VTuberConfig`.

Source partitioning (design "Config_Manager", 需求 2.4 / 2.7):

* Non-secret settings are read **only** from ``config.yaml`` — the plugin
  section ``plugins.entries.omnilimb-face`` plus the reused top-level ``stt`` /
  ``tts`` sections. ``ConfigManager.merge`` therefore never consults ``env`` to
  populate a :class:`VTuberConfig` field.
* Secrets (API keys, tokens, passphrases) are read **only** from ``.env`` via
  :meth:`ConfigManager.required_secret`; they are never part of
  :class:`VTuberConfig`, so they can never be pulled out of the config dict.

``ConfigManager.merge`` is a **pure** function (the property-test target,
Property 3 / 4): missing optional keys fall back to documented defaults
(需求 2.5 / 2.9); type-invalid keys record a :class:`ConfigIssue` naming the
setting path and expected type, fall back to the default, and merging continues
for the remaining keys (需求 2.6).

TTS ``voice`` / ``model`` are **provider-scoped**: they resolve from
``tts.<provider>.voice`` / ``tts.<provider>.model`` (e.g. ``tts.edge.voice``,
``tts.openai.voice``, ``tts.openai.model``) — never a flat ``tts.voice`` — to
match the host's real ``tools/tts_tool.py`` layout. STT ``provider`` / ``model``
reuse the host ``stt`` section.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Discovery / enablement key; matches plugin.yaml ``name`` and the
# ``plugins.entries.<key>`` config section. Defined locally to avoid importing
# ``omnilimb_face.plugin`` (which depends on this module).
PLUGIN_NAME = "omnilimb-face"


# ---------------------------------------------------------------------------
# Configuration dataclasses (frozen; defaults are the documented defaults).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class STTSettings:
    """Speech-to-text settings; reuses the host ``stt`` section (需求 2.2)."""

    enabled: bool = True
    provider: str = "local"           # reuses hermes stt.provider
    model: str = "base"
    language: str = ""
    transcribe_timeout_s: float = 10.0  # 需求 4.7 default 10s


@dataclass(frozen=True)
class TTSSettings:
    """Text-to-speech settings; reuses the host ``tts`` section (需求 2.3).

    ``voice`` / ``model`` are provider-scoped (``tts.<provider>.voice`` /
    ``tts.<provider>.model``), not a flat ``tts.voice``.
    """

    provider: str = "edge"            # reuses hermes tts.provider
    voice: str = "en-US-AriaNeural"   # resolved from tts.<provider>.voice
    model: str = ""                   # resolved from tts.<provider>.model
    synth_timeout_s: float = 10.0     # 需求 6.1 / 6.4 per-attempt 10s
    max_attempts: int = 3             # 需求 6.4 first attempt + 2 retries


@dataclass(frozen=True)
class VADSettings:
    """Voice-activity-detection / segmentation settings (plugin-scoped)."""

    silence_threshold_s: float = 2.0  # 需求 4.3 range 0.5–10, default 2
    max_record_s: float = 60.0        # 需求 4.8 default 60
    barge_in_min_speech_ms: int = 200  # 需求 5.2 at least 200ms
    sample_rate: int = 16000
    frame_ms: int = 20


@dataclass(frozen=True)
class WakeWordSettings:
    """Optional wake-word activation settings (需求 13, default off)."""

    enabled: bool = False             # 需求 13 default off (optional feature)
    phrase: str = "hey hermes"
    confidence_threshold: float = 0.7  # 需求 13.1 default 0.7, range 0.0–1.0
    listen_timeout_s: float = 3.0     # 需求 13.4 silence 3s returns to listening


@dataclass(frozen=True)
class Live2DSettings:
    """Live2D avatar rendering settings."""

    model_name: str = "default"
    model_dict_path: str = "models/model_dict.json"
    default_expression: str = "neutral"  # 需求 8.3 / 8.5 default/neutral expression
    target_fps: int = 30                  # 需求 7.2


@dataclass(frozen=True)
class ProtocolSettings:
    """/client-ws protocol gateway settings."""

    host: str = "127.0.0.1"
    port: int = 12393                 # aligns with Open-LLM-VTuber default
    ws_path: str = "/client-ws"
    max_message_bytes: int = 1_048_576  # 需求 9.2 / 9.7 1 MiB cap


@dataclass(frozen=True)
class InterruptionSettings:
    """Barge-in / interruption settings (需求 5.5 toggleable)."""

    enabled: bool = True              # 需求 5.5 can be disabled in config


@dataclass(frozen=True)
class VTuberConfig:
    """Composed, fully-typed plugin configuration."""

    stt: STTSettings = field(default_factory=STTSettings)
    tts: TTSSettings = field(default_factory=TTSSettings)
    vad: VADSettings = field(default_factory=VADSettings)
    wake_word: WakeWordSettings = field(default_factory=WakeWordSettings)
    live2d: Live2DSettings = field(default_factory=Live2DSettings)
    protocol: ProtocolSettings = field(default_factory=ProtocolSettings)
    interruption: InterruptionSettings = field(default_factory=InterruptionSettings)


@dataclass(frozen=True)
class ConfigIssue:
    """A single config-merge problem: a type-invalid setting that fell back.

    Records the dotted ``setting_path`` of the offending key and the
    ``expected_type`` (需求 2.6), plus a human-readable ``message``.
    """

    setting_path: str
    expected_type: str
    message: str


@dataclass(frozen=True)
class SecretResolution:
    """Result of resolving a required secret from ``.env`` (需求 2.8).

    ``available`` is True only when the secret is present and non-blank.
    When missing, ``blocks_startup`` is True and ``message`` names the missing
    key so dependent features can be blocked from starting.
    """

    key: str
    value: Optional[str]
    available: bool
    blocks_startup: bool
    message: str


# ---------------------------------------------------------------------------
# Coercion helpers (pure, module-private).
# ---------------------------------------------------------------------------

# Sentinel returned by ``_coerce`` when a raw value is not of the expected type.
_INVALID = object()


def _coerce(raw: Any, kind: str) -> Any:
    """Validate/coerce ``raw`` to ``kind``; return ``_INVALID`` on type mismatch.

    ``bool`` is intentionally excluded from the numeric kinds because in Python
    ``bool`` is a subclass of ``int`` — accepting ``True`` as an int/float would
    silently admit a type-invalid value.
    """
    if kind == "bool":
        return raw if isinstance(raw, bool) else _INVALID
    if kind == "int":
        if isinstance(raw, bool):
            return _INVALID
        return raw if isinstance(raw, int) else _INVALID
    if kind == "float":
        if isinstance(raw, bool):
            return _INVALID
        if isinstance(raw, (int, float)):
            return float(raw)
        return _INVALID
    if kind == "str":
        return raw if isinstance(raw, str) else _INVALID
    return _INVALID


def _section(value: Any) -> dict:
    """Return ``value`` if it is a dict, else an empty dict (treat as absent)."""
    return value if isinstance(value, dict) else {}


def _extract(
    container: dict,
    key: str,
    default: Any,
    kind: str,
    path: str,
    issues: list,
) -> Any:
    """Read ``container[key]`` validated as ``kind``.

    * key absent  -> ``default`` (no issue; 需求 2.5)
    * wrong type  -> ``default`` + append a :class:`ConfigIssue` (需求 2.6)
    * valid       -> the (coerced) value
    """
    if key not in container:
        return default
    raw = container[key]
    coerced = _coerce(raw, kind)
    if coerced is _INVALID:
        issues.append(
            ConfigIssue(
                setting_path=path,
                expected_type=kind,
                message=(
                    f"Config setting '{path}' expected type {kind} but got "
                    f"{type(raw).__name__}; falling back to default {default!r}."
                ),
            )
        )
        return default
    return coerced


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Reads/validates plugin config and reuses the host ``stt`` / ``tts``."""

    DEFAULTS: VTuberConfig = VTuberConfig()

    @classmethod
    def from_host(cls, ctx: Any) -> VTuberConfig:
        """Read host ``config.yaml`` + ``.env`` and delegate to :meth:`merge`.

        Thin host adapter: the pure :meth:`merge` is the property-test target.
        The host config dict is obtained from ``ctx`` when it exposes one
        (``ctx.config`` / ``ctx.get_config()``) — which also lets tests inject a
        config dict — otherwise it falls back to the host's read-only loader
        (``hermes_cli.config.load_config_readonly``). Assumption: the plugin's
        non-secret settings live under ``plugins.entries.omnilimb-face`` and the
        reused engine settings under the top-level ``stt`` / ``tts`` sections;
        secrets come from the process environment (``.env`` loaded by the host).
        """
        config = cls._load_host_config(ctx)

        raw_plugin_section: dict = {}
        stt_section: dict = {}
        tts_section: dict = {}
        if isinstance(config, dict):
            entries = _section(_section(config.get("plugins")).get("entries"))
            raw_plugin_section = _section(entries.get(PLUGIN_NAME))
            stt_section = _section(config.get("stt"))
            tts_section = _section(config.get("tts"))

        env = dict(os.environ)
        cfg, issues = cls.merge(raw_plugin_section, stt_section, tts_section, env)
        for issue in issues:
            logger.warning("omnilimb-face config: %s", issue.message)
        return cfg

    @staticmethod
    def _load_host_config(ctx: Any) -> dict:
        """Best-effort, read-only retrieval of the host config dict."""
        # Prefer an explicit config supplied via ctx (keeps `merge` the real
        # unit under test and lets callers/tests inject a dict directly).
        for attr in ("config", "_config"):
            candidate = getattr(ctx, attr, None)
            if isinstance(candidate, dict):
                return candidate
        getter = getattr(ctx, "get_config", None)
        if callable(getter):
            try:
                candidate = getter()
            except Exception:  # pragma: no cover - defensive host guard
                logger.debug(
                    "ctx.get_config() raised; falling back to host loader",
                    exc_info=True,
                )
            else:
                if isinstance(candidate, dict):
                    return candidate
        # Fall back to the host's own read-only loader.
        try:
            from hermes_cli.config import load_config_readonly

            loaded = load_config_readonly()
            return loaded if isinstance(loaded, dict) else {}
        except Exception:  # pragma: no cover - outside a hermes host
            logger.debug(
                "hermes_cli.config.load_config_readonly unavailable; "
                "using defaults",
                exc_info=True,
            )
            return {}

    @classmethod
    def merge(
        cls,
        raw_plugin_section: dict,
        stt_section: dict,
        tts_section: dict,
        env: dict,
    ) -> tuple[VTuberConfig, list[ConfigIssue]]:
        """Pure merge of raw host config into a fully-typed :class:`VTuberConfig`.

        - Missing optional keys -> documented defaults (需求 2.5 / 2.9).
        - Type-invalid keys -> record a :class:`ConfigIssue` (``setting_path`` +
          ``expected_type``), fall back to the default, and CONTINUE merging the
          rest (需求 2.6).
        - Non-secret settings come ONLY from the config sections (需求 2.7); this
          function deliberately never reads ``env`` to populate a field, so
          non-secret values can never be pulled from ``.env``. Secrets are not
          part of :class:`VTuberConfig` and are resolved separately by
          :meth:`required_secret` (需求 2.4).

        Returns ``(config, issues)``. The property-test target for Property 3/4.
        """
        # ``env`` is accepted for interface symmetry but intentionally unused for
        # non-secret population — see source-partition note above (需求 2.4/2.7).
        del env

        defaults = cls.DEFAULTS
        issues: list[ConfigIssue] = []

        plugin = _section(raw_plugin_section)
        stt_raw = _section(stt_section)
        tts_raw = _section(tts_section)

        # -- STT: reuses the host ``stt`` section ---------------------------
        d_stt = defaults.stt
        stt = STTSettings(
            enabled=_extract(stt_raw, "enabled", d_stt.enabled, "bool", "stt.enabled", issues),
            provider=_extract(stt_raw, "provider", d_stt.provider, "str", "stt.provider", issues),
            model=_extract(stt_raw, "model", d_stt.model, "str", "stt.model", issues),
            language=_extract(stt_raw, "language", d_stt.language, "str", "stt.language", issues),
            transcribe_timeout_s=_extract(
                stt_raw, "transcribe_timeout_s", d_stt.transcribe_timeout_s,
                "float", "stt.transcribe_timeout_s", issues,
            ),
        )

        # -- TTS: reuses the host ``tts`` section; voice/model provider-scoped
        d_tts = defaults.tts
        provider = _extract(tts_raw, "provider", d_tts.provider, "str", "tts.provider", issues)
        provider_section = _section(tts_raw.get(provider))
        tts = TTSSettings(
            provider=provider,
            voice=_extract(
                provider_section, "voice", d_tts.voice,
                "str", f"tts.{provider}.voice", issues,
            ),
            model=_extract(
                provider_section, "model", d_tts.model,
                "str", f"tts.{provider}.model", issues,
            ),
            synth_timeout_s=_extract(
                tts_raw, "synth_timeout_s", d_tts.synth_timeout_s,
                "float", "tts.synth_timeout_s", issues,
            ),
            max_attempts=_extract(
                tts_raw, "max_attempts", d_tts.max_attempts,
                "int", "tts.max_attempts", issues,
            ),
        )

        # -- VAD (plugin-scoped) -------------------------------------------
        vad_raw = _section(plugin.get("vad"))
        d_vad = defaults.vad
        vad = VADSettings(
            silence_threshold_s=_extract(
                vad_raw, "silence_threshold_s", d_vad.silence_threshold_s,
                "float", "vad.silence_threshold_s", issues,
            ),
            max_record_s=_extract(
                vad_raw, "max_record_s", d_vad.max_record_s,
                "float", "vad.max_record_s", issues,
            ),
            barge_in_min_speech_ms=_extract(
                vad_raw, "barge_in_min_speech_ms", d_vad.barge_in_min_speech_ms,
                "int", "vad.barge_in_min_speech_ms", issues,
            ),
            sample_rate=_extract(
                vad_raw, "sample_rate", d_vad.sample_rate,
                "int", "vad.sample_rate", issues,
            ),
            frame_ms=_extract(
                vad_raw, "frame_ms", d_vad.frame_ms,
                "int", "vad.frame_ms", issues,
            ),
        )

        # -- Wake word (plugin-scoped, optional) ---------------------------
        wake_raw = _section(plugin.get("wake_word"))
        d_wake = defaults.wake_word
        wake_word = WakeWordSettings(
            enabled=_extract(
                wake_raw, "enabled", d_wake.enabled,
                "bool", "wake_word.enabled", issues,
            ),
            phrase=_extract(
                wake_raw, "phrase", d_wake.phrase,
                "str", "wake_word.phrase", issues,
            ),
            confidence_threshold=_extract(
                wake_raw, "confidence_threshold", d_wake.confidence_threshold,
                "float", "wake_word.confidence_threshold", issues,
            ),
            listen_timeout_s=_extract(
                wake_raw, "listen_timeout_s", d_wake.listen_timeout_s,
                "float", "wake_word.listen_timeout_s", issues,
            ),
        )

        # -- Live2D (plugin-scoped) ----------------------------------------
        live2d_raw = _section(plugin.get("live2d"))
        d_live2d = defaults.live2d
        live2d = Live2DSettings(
            model_name=_extract(
                live2d_raw, "model_name", d_live2d.model_name,
                "str", "live2d.model_name", issues,
            ),
            model_dict_path=_extract(
                live2d_raw, "model_dict_path", d_live2d.model_dict_path,
                "str", "live2d.model_dict_path", issues,
            ),
            default_expression=_extract(
                live2d_raw, "default_expression", d_live2d.default_expression,
                "str", "live2d.default_expression", issues,
            ),
            target_fps=_extract(
                live2d_raw, "target_fps", d_live2d.target_fps,
                "int", "live2d.target_fps", issues,
            ),
        )

        # -- Protocol (plugin-scoped) --------------------------------------
        protocol_raw = _section(plugin.get("protocol"))
        d_protocol = defaults.protocol
        protocol = ProtocolSettings(
            host=_extract(
                protocol_raw, "host", d_protocol.host,
                "str", "protocol.host", issues,
            ),
            port=_extract(
                protocol_raw, "port", d_protocol.port,
                "int", "protocol.port", issues,
            ),
            ws_path=_extract(
                protocol_raw, "ws_path", d_protocol.ws_path,
                "str", "protocol.ws_path", issues,
            ),
            max_message_bytes=_extract(
                protocol_raw, "max_message_bytes", d_protocol.max_message_bytes,
                "int", "protocol.max_message_bytes", issues,
            ),
        )

        # -- Interruption (plugin-scoped) ----------------------------------
        interruption_raw = _section(plugin.get("interruption"))
        d_interruption = defaults.interruption
        interruption = InterruptionSettings(
            enabled=_extract(
                interruption_raw, "enabled", d_interruption.enabled,
                "bool", "interruption.enabled", issues,
            ),
        )

        config = VTuberConfig(
            stt=stt,
            tts=tts,
            vad=vad,
            wake_word=wake_word,
            live2d=live2d,
            protocol=protocol,
            interruption=interruption,
        )
        return config, issues

    @staticmethod
    def required_secret(env: dict, key: str) -> SecretResolution:
        """Resolve a required secret from ``.env`` only (需求 2.4 / 2.8).

        A missing (or blank) required secret yields a result that blocks startup
        and names the missing key, so dependent features can refuse to start
        while unrelated functionality is unaffected.
        """
        raw = env.get(key) if isinstance(env, dict) else None
        if isinstance(raw, str) and raw.strip():
            return SecretResolution(
                key=key,
                value=raw,
                available=True,
                blocks_startup=False,
                message=f"Required secret '{key}' resolved from .env.",
            )
        return SecretResolution(
            key=key,
            value=None,
            available=False,
            blocks_startup=True,
            message=(
                f"Required secret '{key}' is missing from .env; features "
                f"depending on it cannot start."
            ),
        )
