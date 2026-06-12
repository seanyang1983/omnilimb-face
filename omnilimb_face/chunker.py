"""Incremental, lossless sentence chunking for streamed LLM output.

The :class:`SentenceChunker` accumulates streamed text tokens (as intercepted
from the host agent's turn via ``transform_llm_output`` / ``post_llm_call``)
and emits *complete* sentences as soon as a sentence terminator is seen, so the
downstream ``TTS_Player`` and ``Expression_Mapper`` can begin synthesising and
driving the avatar at the earliest sentence boundary (Requirements 3.3 and
6.3).

Design (design.md -> "Components and Interfaces" -> "Sentence_Chunker"):

* ``push(token)`` accumulates a streamed token and returns the list of complete
  sentences flushable so far. A single token may contain zero, one, or several
  terminators, or none at all.
* ``flush()`` returns any trailing partial sentence buffered at end-of-stream.

The implementation is pure and deterministic (no I/O, no globals) and upholds
the Property 8 invariant validated by Task 6.2:

* concatenating, in order, every sentence returned by successive ``push`` calls
  plus the final ``flush`` residual reproduces the concatenation of all input
  tokens exactly (no characters lost, duplicated, or reordered); and
* every flushed sentence *except* the final ``flush`` residual ends with a
  sentence terminator.
"""

from __future__ import annotations


class SentenceChunker:
    """Accumulate streamed tokens and split them at sentence terminators.

    A sentence is considered complete the moment a terminator character is
    observed; the completed sentence includes that terminator. Text following
    the final terminator is retained in an internal buffer until either a later
    ``push`` completes it or ``flush`` drains it at end-of-stream.
    """

    #: Characters that terminate a sentence. Covers ASCII and CJK full-width
    #: punctuation, the horizontal ellipsis, and the newline boundary.
    TERMINATORS = "。．.!?！？…\n"

    def __init__(self) -> None:
        # Trailing text that has not yet been terminated. After every ``push``
        # this holds at most the current partial (unterminated) sentence, so it
        # never itself contains a terminator character.
        self._buffer: str = ""

    def push(self, token: str) -> list[str]:
        """Accumulate ``token`` and return the complete sentences now flushable.

        The token is appended to the internal buffer, which is then scanned for
        terminator characters. Each terminator closes a sentence spanning from
        the start of the buffer up to and including that terminator; the text
        after the last terminator is retained for a subsequent ``push`` or
        ``flush``. Returns an empty list when no terminator has been seen yet.
        """
        self._buffer += token

        sentences: list[str] = []
        start = 0
        for index, char in enumerate(self._buffer):
            if char in self.TERMINATORS:
                sentences.append(self._buffer[start : index + 1])
                start = index + 1

        # Keep only the trailing residual (everything after the last
        # terminator); it contains no terminator by construction.
        self._buffer = self._buffer[start:]
        return sentences

    def flush(self) -> list[str]:
        """Return the trailing partial sentence at end-of-stream, if any.

        Returns a single-element list with the buffered residual when it is
        non-empty (this is the only flushed sentence permitted not to end with a
        terminator), or an empty list when nothing remains buffered. The buffer
        is cleared so the chunker can be reused for another stream.
        """
        if not self._buffer:
            return []
        residual = self._buffer
        self._buffer = ""
        return [residual]


def _self_check() -> None:
    """Inline invariant checks (Property 8) for a few representative streams.

    Verifies, for each token sequence, that joining every pushed sentence with
    the final flush residual reproduces the joined input exactly, and that every
    flushed sentence except the final residual ends with a terminator.
    """
    token_sequences: list[list[str]] = [
        ["Hello world. How are", " you?"],
        ["No terminator here"],
        [""],
        ["Multiple!! terminators?? in", " one… token.\n"],
        ["你好。", "今天天气怎么样？", "很好！"],
        ["Split across", " a sing", "le sentence."],
        ["Trailing terminator at end."],
        ["...", "leading ellipsis"],
        ["line one\nline two\n", "line three"],
        ["mix。．.!?！？…\nof every terminator"],
    ]

    for tokens in token_sequences:
        chunker = SentenceChunker()
        emitted: list[str] = []
        non_final_sentences: list[str] = []
        for token in tokens:
            pushed = chunker.push(token)
            # Every sentence produced by push() is non-final and must end with
            # a terminator.
            non_final_sentences.extend(pushed)
            emitted.extend(pushed)

        residual = chunker.flush()
        # flush() returns at most the single trailing partial sentence.
        assert len(residual) <= 1, f"flush returned more than one item: {residual!r}"
        emitted.extend(residual)

        # Losslessness: pushed sentences + flush residual == input tokens.
        assert "".join(emitted) == "".join(tokens), (
            f"lossless invariant violated for {tokens!r}: "
            f"{''.join(emitted)!r} != {''.join(tokens)!r}"
        )

        # Boundary: every non-final flushed sentence ends with a terminator.
        for sentence in non_final_sentences:
            assert sentence and sentence[-1] in SentenceChunker.TERMINATORS, (
                f"non-final sentence does not end with a terminator: {sentence!r}"
            )

    print("SentenceChunker self-check passed for", len(token_sequences), "sequences")


if __name__ == "__main__":
    _self_check()
