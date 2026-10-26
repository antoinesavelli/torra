"""Local crash reporting: unhandled exceptions that escape every other guard.

Depends on: torra.telemetry.records (redact_secrets -- the same scrub
already applied to request text and label rationale).
"""

from __future__ import annotations

import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .records import redact_secrets

__all__ = ["CrashRecord", "CrashReporter"]


@dataclass
class CrashRecord:
    """One unhandled exception, from wherever it was finally caught.

    Not a `TaskRecord`: those describe a gated task's outcome (including
    ordinary failures like `backend_down`); this describes torra's
    own code breaking in a way nothing else caught.
    """

    at: float
    site: str
    exception_type: str
    message: str
    traceback: str

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CrashRecord:
        """Rebuild from a JSONL line, ignoring fields this version does not know."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


class CrashReporter:
    """Append-only JSONL log of unhandled exceptions, mirroring `TaskLogger`.

    `message`/`traceback` are redacted the same way request text is --
    a traceback's local-variable reprs can contain a secret exactly the
    way free-form operator text can (Textual's own panic screen already
    renders locals via `rich.traceback`; persisting an unredacted copy of
    the same information indefinitely would be strictly worse than what's
    already shown transiently on screen).
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Configure the JSONL path and an injectable clock."""
        self.path = Path(path)
        self._clock = clock
        self.errors: list[str] = []
        self.written: int = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Same reasoning as TaskLogger: a reporter that can't reach its
            # own directory must say so at construction time and degrade to
            # a no-op, not raise before the crash it was about to record.
            self.errors.append(f"log directory unusable: {exc!r}")

    def record(self, error: BaseException, site: str) -> bool:
        """Append one crash record. Never raises.

        A crash reporter that itself crashes while reporting a crash would
        be the worst possible failure mode -- the whole body is inside one
        try/except, same as `TaskLogger.record()`.
        """
        try:
            tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            record = CrashRecord(
                at=self._clock(),
                site=site,
                exception_type=type(error).__name__,
                message=redact_secrets(str(error)),
                traceback=redact_secrets(tb),
            )
            line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
            self.written += 1
            return True
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return False

    def read(self, limit: int | None = None) -> list[CrashRecord]:
        """Read crashes back, newest last. A torn line is skipped and counted."""
        if not self.path.exists():
            return []
        records: list[CrashRecord] = []
        try:
            for number, raw in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not raw.strip():
                    continue
                try:
                    records.append(CrashRecord.from_dict(json.loads(raw)))
                except Exception as exc:
                    self.errors.append(f"line {number} unreadable: {exc}")
        except OSError as exc:
            self.errors.append(f"read failed: {exc!r}")
        return records[-limit:] if limit else records

    def stats(self) -> dict[str, Any]:
        """Counters, including reporter failures."""
        return {
            "path": str(self.path),
            "written": self.written,
            "on_disk": len(self.read()),
            "errors": list(self.errors),
        }
