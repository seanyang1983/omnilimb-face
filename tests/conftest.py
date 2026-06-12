"""Shared pytest + Hypothesis configuration for the omnilimb-face test suite.

Registers and loads a Hypothesis settings profile so every property-based test
runs at least 100 random examples, as mandated by the design's Testing
Strategy. The active profile is selected by the ``HYPOTHESIS_PROFILE``
environment variable (default ``vtuber``); set ``HYPOTHESIS_PROFILE=ci`` for a
heavier run in CI.
"""

from __future__ import annotations

import os

from hypothesis import settings

# Minimum example count required for every property-based test (>= 100).
DEFAULT_MAX_EXAMPLES = 100
CI_MAX_EXAMPLES = 250

# `deadline=None` disables Hypothesis' per-example timing deadline. The
# property tests added by later tasks exercise pure logic, but some run a
# non-trivial amount of work per example; disabling the deadline keeps the
# suite from flaking on slow machines without weakening the properties.
settings.register_profile(
    "vtuber",
    max_examples=DEFAULT_MAX_EXAMPLES,
    deadline=None,
)
settings.register_profile(
    "ci",
    max_examples=CI_MAX_EXAMPLES,
    deadline=None,
)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "vtuber"))
