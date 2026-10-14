"""Keeps the conversation under a token budget by compressing the oldest history.

Depends on: stdlib only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from .tasks import ContextBudgetError

__all__ = ["ContextStep", "CompressionResult", "ContextCompressor"]


@dataclass
class ContextStep:
    """One entry in the running context window."""

    entry_id: str
    role: str
    content: str
    tokens: int = 0
    compressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass(frozen=True)
class CompressionResult:
    """Outcome of one compression pass."""

    triggered: bool
    tokens_before: int
    tokens_after: int
    entries_before: int
    entries_after: int
    reduction_ratio: float
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """About four characters per token. Fast, offline, good enough to budget on."""
    return max(1, (len(text) + 3) // 4)


class ContextCompressor:
    """Keeps the conversation under a token budget.

    Built in phase 1 and wired to nothing for six phases, because until there
    was an agent there was no conversation to compress. It now holds the actual
    dialogue between operator and model, which is what it was always for.
    """

    def __init__(
        self,
        max_tokens: int = 32_000,
        trigger_ratio: float = 0.90,
        target_ratio: float = 0.45,
        keep_recent: int = 4,
        token_counter: Callable[[str], int] = estimate_tokens,
    ) -> None:
        """Configure the token ceiling, compression trigger/target ratios, and estimator."""
        if not 0.0 < trigger_ratio <= 1.0:
            raise ValueError("trigger_ratio must be in (0, 1]")
        self.max_tokens = int(max_tokens)
        self.trigger_ratio = float(trigger_ratio)
        self.target_ratio = float(target_ratio)
        self.keep_recent = int(keep_recent)
        self._count = token_counter
        self.entries: list[ContextStep] = []
        self.compressions: int = 0

    @property
    def total_tokens(self) -> int:
        """Current token usage across all retained entries."""
        return sum(e.tokens for e in self.entries)

    @property
    def usage_ratio(self) -> float:
        """Current usage as a fraction of max_tokens."""
        return self.total_tokens / self.max_tokens if self.max_tokens else 0.0

    def at_capacity(self) -> bool:
        """True when usage has reached the compression trigger."""
        return self.usage_ratio >= self.trigger_ratio

    def add(self, entry_id: str, role: str, content: str) -> ContextStep:
        """Append one entry."""
        # Raises ContextBudgetError when a single entry alone blows the
        # budget -- compression cannot save that case, so it is loud.
        tokens = self._count(content)
        if tokens > self.max_tokens:
            raise ContextBudgetError(
                f"{entry_id} needs {tokens} tokens, budget is {self.max_tokens}")
        item = ContextStep(entry_id, role, content, tokens)
        self.entries.append(item)
        return item

    def remove(self, entry_id: str) -> bool:
        """Drop one entry by id -- the model saying a previously-read file's
        content no longer helps (Agent._forget). True if it was actually
        found and removed; False for an id that's already gone (already
        compressed away, or never existed), which the caller treats as a
        harmless no-op, not an error -- the operator's memory of what it
        read and the model's are not the same clock."""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.entry_id != entry_id]
        return len(self.entries) != before

    @staticmethod
    def _summarize(chunk: list[ContextStep]) -> str:
        """Compact extractive summary of a slice of history."""
        lines = [f"[COMPRESSED: {len(chunk)} earlier entries]"]
        for e in chunk:
            head = e.content.strip().splitlines()
            first = (head[0] if head else "")[:80]
            if first.startswith(("<file", "<command-output", "<machine-state")):
                first = "(file contents omitted)"  # a path or label is text nobody vouched for
            lines.append(f"- {e.role}: {first}")
        return "\n".join(lines)

    def compress(self, force: bool = False) -> CompressionResult:
        """Fold the oldest history into a summary, guaranteeing a net saving."""
        before, count = self.total_tokens, len(self.entries)
        if not force and not self.at_capacity():
            return CompressionResult(False, before, before, count, count, 0.0)

        compressible = max(0, count - self.keep_recent)
        if compressible < 1:
            return CompressionResult(False, before, before, count, count, 0.0)

        target = int(self.max_tokens * self.target_ratio)
        cut = 1
        while cut < compressible:
            projected = (sum(e.tokens for e in self.entries[cut:])
                         + self._count(self._summarize(self.entries[:cut])))
            if projected <= target:
                break
            cut += 1

        old, tail = self.entries[:cut], self.entries[cut:]
        old_tokens = sum(e.tokens for e in old)
        text = self._summarize(old)

        # Live wire: HANDOFF.md §5.5
        # Degrade to terser summaries rather than making things worse. With many
        # small entries the per-entry line costs more than the content it
        # replaces, which would turn compression into a net loss.
        if self._count(text) > old_tokens // 2:
            text = f"[COMPRESSED: {len(old)} earlier entries elided]"
        if self._count(text) >= old_tokens:
            return CompressionResult(False, before, before, count, count, 0.0,
                                     "skipped: summary would exceed source")

        summary = ContextStep(f"summary::{old[0].entry_id}..{old[-1].entry_id}",
                              "system", text, self._count(text), compressed=True)
        self.entries = [summary] + tail
        after = self.total_tokens
        self.compressions += 1
        return CompressionResult(
            True, before, after, count, len(self.entries),
            round((before - after) / before, 4) if before else 0.0, text)

    def maybe_compress(self) -> CompressionResult:
        """Compress only if the trigger has been crossed."""
        return self.compress(force=False)

    def as_messages(self) -> list[dict[str, str]]:
        """The window as chat messages, ready for the model."""
        return [{"role": e.role, "content": e.content} for e in self.entries]
