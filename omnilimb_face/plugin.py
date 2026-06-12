"""omnilimb-face plugin entry module.

The hermes ``PluginManager`` discovers this plugin either by directory
(``~/AppData/Local/hermes/plugins/omnilimb-face/`` containing ``plugin.yaml`` +
a root ``__init__.py`` exposing ``register``) or via the ``hermes_agent.plugins``
pip entry point, which resolves to this module and reads its ``register``
attribute.

``register(ctx)`` is the plugin's single entry point (design.md -> "Components
and Interfaces" -> "Plugin Entry Point: register(ctx)"). It registers the
plugin's capabilities **only** through the generic extension surface exposed by
``ctx`` and never writes to or modifies any hermes core file (Requirement 1.3):

* lifecycle hooks ``on_session_start`` / ``on_session_end`` plus the LLM-output
  observers ``transform_llm_output`` / ``post_llm_call`` (Requirement 1.2);
* the ``vtuber_status`` and ``vtuber_say`` tools — registered **even in a
  degraded state** so they stay visible in ``hermes tools`` when optional voice
  dependencies are missing (Requirements 12.1 / 12.6); their ``check_fn``
  reflects availability while registration always happens;
* the ``hermes vtuber`` CLI subcommand and the ``/vtuber`` + ``/handsfree``
  slash commands (Requirement 1.2).

Optional-dependency probing is wrapped so that missing voice / Live2D extras
yield a *degraded* registration rather than a failure (Requirement 12.1). Any
**unexpected** exception is allowed to propagate so the host ``PluginManager``
skips this plugin, reports the load failure and continues loading the other
enabled plugins (Requirement 1.7).

Each ``ctx`` registration method is guarded with ``hasattr`` so a minimal / test
context that does not implement the full surface degrades gracefully instead of
crashing; in a real hermes host every method is present.
"""

from __future__ import annotations

import logging
from typing import Any

from omnilimb_face.config import ConfigManager
from omnilimb_face.runtime import VTuberRuntime

logger = logging.getLogger(__name__)

PLUGIN_NAME = "omnilimb-face"


# ---------------------------------------------------------------------------
# Tool JSON schemas (minimal, valid function schemas: name/description/parameters).
# ---------------------------------------------------------------------------

VTUBER_STATUS_SCHEMA: dict = {
    "name": "vtuber_status",
    "description": "Report omnilimb-face subsystem status (running state, "
    "degraded state and any missing optional dependencies).",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}

VTUBER_SAY_SCHEMA: dict = {
    "name": "vtuber_say",
    "description": "Speak text through the avatar (TTS + lip-sync + expression).",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text for the avatar to speak.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


def register(ctx: Any) -> None:
    """Plugin entry point invoked by the hermes ``PluginManager`` at load time.

    Builds the plugin configuration, constructs the :class:`VTuberRuntime`, and
    registers the runtime's hooks, tools, CLI subcommand and slash commands
    through ``ctx`` only — never touching any hermes core file (Requirement 1.3,
    1.2). Missing optional voice/Live2D dependencies result in a degraded
    registration (the tools are still registered, Requirement 12.1); any
    unexpected exception propagates so the host skips this plugin and reports the
    failure (Requirement 1.7).
    """
    # Build config from the host (config.yaml sections + .env secrets). This is
    # defensive internally and falls back to documented defaults rather than
    # raising, so a missing/!partial host config never blocks registration.
    config = ConfigManager.from_host(ctx)

    # The runtime construction probes optional dependencies WITHOUT raising, so
    # absent voice/Live2D extras yield a degraded (but registered) plugin.
    runtime = VTuberRuntime(ctx=ctx, config=config)

    # -- Lifecycle hooks (Requirement 1.2 / 10) ----------------------------
    # transform_llm_output: observer that captures the host reply text to drive
    # TTS/Live2D (returns None, never rewrites). post_llm_call: final-sentence
    # fallback observer.
    _register_hook(ctx, "on_session_start", runtime.on_session_start)
    _register_hook(ctx, "on_session_end", runtime.on_session_end)
    _register_hook(ctx, "transform_llm_output", runtime.on_llm_output)
    _register_hook(ctx, "post_llm_call", runtime.on_post_llm_call)

    # -- Tools (Requirement 12.1: register even when degraded) -------------
    # check_fn reflects availability, but registration ALWAYS happens so the
    # tools remain visible in ``hermes tools`` (Requirement 12.6).
    _register_tool(
        ctx,
        name="vtuber_status",
        toolset="vtuber",
        schema=VTUBER_STATUS_SCHEMA,
        handler=runtime.tool_status,
        check_fn=runtime.deps_available,
        requires_env=[],
        description="Report omnilimb-face subsystem status.",
        emoji="🎭",
    )
    _register_tool(
        ctx,
        name="vtuber_say",
        toolset="vtuber",
        schema=VTUBER_SAY_SCHEMA,
        handler=runtime.tool_say,
        check_fn=runtime.tts_available,
        requires_env=[],
        description="Speak text through the avatar (TTS + lip-sync + expression).",
        emoji="🗣️",
    )

    # -- CLI subcommand: hermes vtuber start|stop|status|doctor (Req 10.5) --
    _register_cli_command(
        ctx,
        name="vtuber",
        help="Control the omnilimb-face avatar UI and voice loop.",
        setup_fn=runtime.build_cli_parser,
        handler_fn=runtime.handle_cli,
    )

    # -- Slash commands: /vtuber, /handsfree (Requirement 1.2 / 4.6) -------
    _register_command(
        ctx,
        "vtuber",
        runtime.slash_vtuber,
        description="Avatar/voice controls",
        args_hint="[start|stop|status]",
    )
    _register_command(
        ctx,
        "handsfree",
        runtime.slash_handsfree,
        description="Toggle hands-free voice mode",
        args_hint="[on|off]",
    )

    logger.info(
        "omnilimb-face registered (degraded=%s)",
        getattr(runtime, "_degraded", False),
    )
    return None


# ---------------------------------------------------------------------------
# ctx registration shims.
#
# Each guards the corresponding ctx method with ``hasattr`` so a minimal/test
# context degrades gracefully. When the method IS present, any exception it
# raises is intentionally allowed to propagate so the host PluginManager skips
# this plugin and reports the failure (Requirement 1.7).
# ---------------------------------------------------------------------------


def _register_hook(ctx: Any, name: str, handler: Any) -> None:
    """Register a lifecycle/LLM hook via ``ctx.register_hook`` when available."""
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        logger.debug("ctx has no register_hook; skipping hook %r", name)
        return
    register_hook(name, handler)


def _register_tool(ctx: Any, **kwargs: Any) -> None:
    """Register a tool via ``ctx.register_tool`` when available.

    Registration always happens regardless of the tool's degraded availability
    (Requirement 12.1); only the absence of ``ctx.register_tool`` itself (a
    minimal/test context) is tolerated.
    """
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        logger.debug("ctx has no register_tool; skipping tool %r", kwargs.get("name"))
        return
    register_tool(**kwargs)


def _register_cli_command(ctx: Any, **kwargs: Any) -> None:
    """Register the CLI subcommand via ``ctx.register_cli_command`` when available."""
    register_cli_command = getattr(ctx, "register_cli_command", None)
    if not callable(register_cli_command):
        logger.debug(
            "ctx has no register_cli_command; skipping CLI command %r",
            kwargs.get("name"),
        )
        return
    register_cli_command(**kwargs)


def _register_command(ctx: Any, name: str, handler: Any, **kwargs: Any) -> None:
    """Register a slash command via ``ctx.register_command`` when available."""
    register_command = getattr(ctx, "register_command", None)
    if not callable(register_command):
        logger.debug("ctx has no register_command; skipping slash command /%s", name)
        return
    register_command(name, handler, **kwargs)
