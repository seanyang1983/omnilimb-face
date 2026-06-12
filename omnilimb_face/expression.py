"""omnilimb_face.expression — emotion/expression mapping for the Live2D avatar.

This module implements the ``Expression_Mapper`` component from the design
(Requirement 8), aligned with Open-LLM-VTuber's ``Live2dModel`` behaviour:
``emotionMap`` (emotion keyword -> expression index), ``extract_emotion``
(scan ``[key]`` markers in order), ``remove_emotion_keywords`` (strip those
markers from the displayable text) and ``emo_str`` (the available-keyword
string injected into the LLM prompt, e.g. ``"[joy], [anger], [neutral]"``).

Scope (Task 8.1): the **pure** mapping logic only. :class:`ExpressionMapper`
exposes two side-effect free methods:

* :meth:`ExpressionMapper.emo_prompt` builds the available emotion-keyword
  string from the configured ``emotion_map`` keys, for injection into the LLM
  prompt so the model is guided to emit ``[key]`` markers.
* :meth:`ExpressionMapper.map_reply` is a deterministic pure function (the
  Property 11 target; its Hypothesis test is added later by Task 8.2). It
  extracts ``[key]`` markers in order of appearance, maps the recognized ones
  to their expression indices, records unrecognized markers, strips all markers
  from the displayable text, and selects a primary expression.

Mapping rules (design.md -> "Components and Interfaces" -> "Expression_Mapper",
Requirements 8.1/8.3/8.4/8.5):

* ``display_text`` is ``reply_text`` with every ``[key]`` bracket tag stripped
  (mirrors ``remove_emotion_keywords``), with surrounding whitespace normalized
  deterministically so removal never leaves awkward doubled spaces.
* ``expressions`` holds the expression indices for the **recognized** markers
  (a marker is recognized iff its key is present in ``emotion_map``), in their
  order of appearance (Requirement 8.1).
* ``unmatched`` holds the keys of unrecognized markers, in order of appearance,
  and those markers contribute no expression index (Requirement 8.3).
* ``primary`` is the index of the **first recognized** marker when any marker is
  recognized (so conflicting markers resolve to the earliest, Requirement 8.4);
  otherwise it falls back to the configured default/neutral expression's index
  resolved via ``emotion_map`` (Requirement 8.5). When the reply contains no
  markers at all, ``expressions`` is empty and ``primary`` is the default/neutral
  index. If ``default_expression`` is not present in ``emotion_map``, ``primary``
  gracefully degrades to ``None``.

The tag syntax is matched precisely by :data:`TAG_PATTERN` (``\\[([^\\[\\]]+)\\]``):
a ``[`` and ``]`` enclosing one or more characters that are neither ``[`` nor
``]``. Empty brackets (``[]``) and nested brackets are therefore not treated as
tags. Key recognition is an exact (case-sensitive) membership test against the
provided ``emotion_map`` keys, matching the design's "recognized = key present
in emotion_map".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: Precompiled pattern for a single ``[key]`` bracket tag. The capture group is
#: the key: one or more characters that are neither ``[`` nor ``]`` enclosed in
#: a single pair of square brackets. This intentionally rejects empty brackets
#: (``[]``) and any nested/unbalanced bracket constructs.
TAG_PATTERN = re.compile(r"\[([^\[\]]+)\]")

#: Pattern used to collapse runs of whitespace introduced by tag removal into a
#: single space, so stripping a surrounded tag does not leave doubled spaces.
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class ExpressionResult:
    """Outcome of mapping a single agent reply to Live2D expressions.

    Attributes:
        display_text: ``reply_text`` with every ``[key]`` bracket tag removed and
            whitespace normalized (Requirement 8.x, mirrors
            ``remove_emotion_keywords``).
        expressions: Expression indices for the recognized markers, in order of
            appearance (Requirement 8.1).
        primary: The first recognized marker's index, or the configured
            default/neutral expression index when nothing is recognized, or
            ``None`` when the default is itself unmapped (Requirements 8.4/8.5).
        unmatched: Keys of unrecognized markers, in order of appearance
            (Requirement 8.3). These contribute no entry to ``expressions``.
    """

    display_text: str
    expressions: list[int]
    primary: Optional[int]
    unmatched: list[str]


class ExpressionMapper:
    """Maps emotion markers in agent replies to Live2D expression indices.

    Aligned with Open-LLM-VTuber's ``Live2dModel`` (``emotionMap`` /
    ``extract_emotion`` / ``remove_emotion_keywords`` / ``emo_str``). The mapper
    is constructed from a model's emotion map and the configured default
    (neutral) expression name, and exposes pure, deterministic methods only.

    Args:
        emotion_map: Mapping of emotion keyword -> Live2D expression index, as
            provided by the model's ``emotionMap`` (e.g. ``{"joy": 0,
            "anger": 1, "neutral": 2}``). Iteration order is preserved for
            :meth:`emo_prompt`.
        default_expression: The configured default/neutral expression keyword
            (``Live2DSettings.default_expression``). Resolved against
            ``emotion_map`` to obtain the fallback ``primary`` index; if the key
            is absent from ``emotion_map`` the fallback gracefully becomes
            ``None``.
    """

    def __init__(self, emotion_map: dict[str, int], default_expression: str) -> None:
        # Copy to a plain dict so external mutation of the caller's object can't
        # change our behaviour, while preserving insertion order for emo_prompt.
        self._emotion_map: dict[str, int] = dict(emotion_map)
        self._default_expression: str = default_expression
        # Resolve the default/neutral index once. ``None`` when the configured
        # default expression is not present in the emotion map (graceful
        # degradation per Requirement 8.5).
        self._default_index: Optional[int] = self._emotion_map.get(default_expression)

    @property
    def default_index(self) -> Optional[int]:
        """The resolved default/neutral expression index (``None`` if unmapped)."""
        return self._default_index

    def emo_prompt(self) -> str:
        """Build the available emotion-keyword string for the LLM prompt.

        Produces a comma-separated list of the configured emotion keywords in
        bracket-tag form, in ``emotion_map`` iteration order, e.g.
        ``"[joy], [anger], [neutral]"``. Mirrors Open-LLM-VTuber's ``emo_str``,
        which is injected into the prompt so the model emits ``[key]`` markers.
        Returns an empty string when ``emotion_map`` is empty.
        """
        return ", ".join(f"[{key}]" for key in self._emotion_map)

    def map_reply(self, reply_text: str) -> ExpressionResult:
        """Map ``reply_text`` to expressions (pure function, Property 11 target).

        Scans ``reply_text`` for ``[key]`` bracket tags (see :data:`TAG_PATTERN`)
        in order of appearance and, for each:

        * if ``key`` is present in ``emotion_map`` (recognized), appends its
          index to ``expressions`` (Requirement 8.1);
        * otherwise records ``key`` in ``unmatched`` and contributes no index
          (Requirement 8.3).

        ``display_text`` is ``reply_text`` with **all** bracket tags removed and
        whitespace normalized so removal leaves no doubled/awkward spaces.
        ``primary`` is the first recognized index when any marker is recognized
        (earliest wins on conflicts, Requirement 8.4); otherwise the configured
        default/neutral index (Requirement 8.5), which may be ``None`` when the
        default is unmapped. When ``reply_text`` has no markers, ``expressions``
        is empty and ``primary`` is the default/neutral index.

        Args:
            reply_text: The raw agent reply, possibly containing ``[key]``
                emotion markers.

        Returns:
            An :class:`ExpressionResult` with the normalized display text,
            recognized expression indices, primary index and unmatched keys.
        """
        expressions: list[int] = []
        unmatched: list[str] = []
        primary: Optional[int] = None

        # Single left-to-right scan preserves appearance order for both the
        # recognized indices and the unmatched keys (Requirements 8.1/8.3).
        for match in TAG_PATTERN.finditer(reply_text):
            key = match.group(1)
            if key in self._emotion_map:
                index = self._emotion_map[key]
                expressions.append(index)
                if primary is None:
                    # Earliest recognized marker wins on conflicts (Req 8.4).
                    primary = index
            else:
                unmatched.append(key)

        # No recognized marker -> fall back to the default/neutral expression
        # index (Requirement 8.5); ``None`` if the default is itself unmapped.
        if primary is None:
            primary = self._default_index

        display_text = self._strip_tags(reply_text)

        return ExpressionResult(
            display_text=display_text,
            expressions=expressions,
            primary=primary,
            unmatched=unmatched,
        )

    @staticmethod
    def _strip_tags(text: str) -> str:
        """Remove every ``[key]`` bracket tag and normalize whitespace.

        Mirrors ``remove_emotion_keywords`` (strip the markers) but additionally
        applies deterministic whitespace normalization so that removing a tag
        surrounded by spaces does not leave a doubled space: runs of whitespace
        are collapsed to a single space and leading/trailing whitespace is
        trimmed. Text containing no tags whose whitespace is already normal is
        returned with at most surrounding-whitespace trimming applied.
        """
        without_tags = TAG_PATTERN.sub("", text)
        # Collapse any whitespace run (including those created by removal) into a
        # single space, then trim. Deterministic and free of doubled spaces.
        return _WHITESPACE_RUN.sub(" ", without_tags).strip()


def _self_check() -> None:
    """Inline invariant checks for a few representative replies (Property 11).

    Exercises the verification scenarios from Task 8.1: recognized markers map
    in appearance order; an unknown marker is recorded in ``unmatched`` and
    contributes no expression; a reply with no markers yields an empty
    ``expressions`` list and ``primary`` equal to the neutral/default index; and
    ``display_text`` has all bracket tags stripped.
    """
    mapper = ExpressionMapper({"joy": 0, "anger": 1, "neutral": 2}, "neutral")

    # emo_prompt reflects map order in bracket-tag form.
    assert mapper.emo_prompt() == "[joy], [anger], [neutral]", mapper.emo_prompt()

    # Mixed recognized + unknown markers.
    result = mapper.map_reply("Hello [joy] there [unknown] bye")
    assert result.expressions == [0], result.expressions
    assert result.unmatched == ["unknown"], result.unmatched
    assert result.primary == 0, result.primary
    assert "[" not in result.display_text and "]" not in result.display_text, (
        result.display_text
    )
    assert result.display_text == "Hello there bye", repr(result.display_text)

    # Conflicting markers: earliest recognized wins for ``primary`` (Req 8.4),
    # while ``expressions`` keeps every recognized marker in order.
    conflict = mapper.map_reply("[anger] grr [joy] yay")
    assert conflict.expressions == [1, 0], conflict.expressions
    assert conflict.primary == 1, conflict.primary
    assert conflict.unmatched == [], conflict.unmatched

    # No markers -> empty expressions, primary == neutral/default index.
    plain = mapper.map_reply("Just a calm sentence.")
    assert plain.expressions == [], plain.expressions
    assert plain.primary == 2, plain.primary
    assert plain.unmatched == [], plain.unmatched
    assert plain.display_text == "Just a calm sentence.", repr(plain.display_text)

    # Only an unknown marker -> falls back to default index, marker unmatched.
    only_unknown = mapper.map_reply("[mystery] hmm")
    assert only_unknown.expressions == [], only_unknown.expressions
    assert only_unknown.primary == 2, only_unknown.primary
    assert only_unknown.unmatched == ["mystery"], only_unknown.unmatched

    # Graceful degradation: default expression not present in the emotion map.
    no_default = ExpressionMapper({"joy": 0}, "neutral")
    degraded = no_default.map_reply("no tags here")
    assert degraded.primary is None, degraded.primary
    assert degraded.expressions == [], degraded.expressions

    print("ExpressionMapper self-check passed")


if __name__ == "__main__":
    _self_check()
