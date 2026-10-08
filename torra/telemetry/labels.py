"""Human labels attached to logged requests, keyed on the request text.

Depends on: torra.telemetry.records (TaskRecord, for `unlabeled`).
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .records import TaskRecord, redact_secrets

__all__ = ["Label", "LabelStore"]


@dataclass
class Label:
    """A human verdict on one logged request."""

    request: str
    ambiguous: bool
    why: str = ""
    labeled_at: float = 0.0
    labeler: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, one JSONL line."""
        return asdict(self)


class LabelStore:
    """Human labels attached to logged requests, keyed on the request text.

    Keyed on the text rather than the task id on purpose. The same request
    phrasing recurs across many task ids, and what is being labeled is the
    phrasing, not the run.
    """

    def __init__(self, path: os.PathLike[str] | str,
                 clock: Callable[[], float] = time.time) -> None:
        """Configure the JSONL path and injectable clock."""
        self.path = Path(path)
        self._clock = clock
        self.errors: list[str] = []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.errors.append(f"label directory unusable: {exc!r}")

    def label(self, request: str, ambiguous: bool, why: str = "",
              labeler: str = "") -> Label:
        """Record one verdict. A later label for the same text supersedes it."""
        # `why` is a human's free-text rationale for the verdict -- just as
        # capable of quoting a secret as the request it's commenting on
        # ("ambiguous because it says password=hunter2 with no context"),
        # but unlike `request` (scrubbed by TaskLogger before it ever
        # reaches here) nothing redacted it. Same patterns, not a second
        # copy of them.
        entry = Label(request=request, ambiguous=bool(ambiguous),
                      why=redact_secrets(why), labeled_at=self._clock(),
                      labeler=labeler)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
                fh.flush()
        except OSError as exc:
            self.errors.append(f"label write failed: {exc!r}")
        return entry

    def all(self) -> dict[str, Label]:
        """Every label, latest wins, keyed by request text."""
        out: dict[str, Label] = {}
        if not self.path.exists():
            return out
        try:
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    data = json.loads(raw)
                    known = Label.__dataclass_fields__
                    out[data["request"]] = Label(**{k: v for k, v in data.items() if k in known})
                except Exception as exc:
                    self.errors.append(f"label line unreadable: {exc}")
        except OSError as exc:
            self.errors.append(f"label read failed: {exc!r}")
        return out

    def unlabeled(self, records: Sequence[TaskRecord]) -> list[str]:
        """Distinct request texts in `records` that have no label yet."""
        # Ordered by frequency, so labeling effort goes to the phrasings that
        # actually recur rather than to one-offs.
        labeled = set(self.all())
        counts = Counter(r.request for r in records if r.request not in labeled)
        return [request for request, _n in counts.most_common()]
