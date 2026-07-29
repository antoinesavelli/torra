"""Append-only JSONL log of every task the harness is given.

Depends on: stdlib only. Every public method catches its own exceptions --
telemetry must never be able to break the run it is describing.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["TaskRecord", "TaskLogger"]


@dataclass
class TaskRecord:
    """One task, from the request text to the final outcome.

    This is the unit of evidence. Everything telemetry can tell you is derived
    from a pile of these.
    """

    task_id: str
    request: str
    at: float
    clarifier_blocked: bool = False
    clarifier_codes: list[str] = field(default_factory=list)
    clarifier_anchors: list[str] = field(default_factory=list)
    clarifier_resolved: bool = True
    model: str = ""
    routing_rule: str = ""
    status: str = ""
    stages_completed: list[str] = field(default_factory=list)
    steps_total: int = 0
    steps_run: int = 0
    abort_reason: str = ""
    rollback_ran: bool = False
    rollback_verified: bool | None = None
    undo_coverage: float = 0.0
    uncovered_steps: list[str] = field(default_factory=list)
    seconds: float = 0.0
    harness_phase: int = 0
    # -- agent era fields ------------------------------------------------- #
    intent: str = ""
    proposed_steps: int = 0
    repairs: int = 0
    worst_level: str = ""
    approved: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, one JSONL line."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskRecord:
        """Rebuild from a JSONL line, ignoring fields this version does not know."""
        # Forward compatible on purpose: a log written by a later harness must
        # still be readable, because the whole point of the log is that it
        # outlives any one version of the code.
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def executed(self) -> bool:
        """True when the task actually reached the execute stage."""
        return "execute" in self.stages_completed

    @property
    def succeeded(self) -> bool:
        """True when the pipeline completed without an early exit."""
        return self.status == "completed"


class TaskLogger:
    """Append-only JSONL log of every task the harness is given.

    Append-only and one JSON object per line, so a crash mid-write costs at most
    the last record, a `tail -f` works, and the file can be read by anything.
    No database, no schema migration, no daemon.

    Redaction runs before anything is written. Task text is operator input and
    will eventually contain a token or a password, so patterns that look like
    secrets are replaced rather than stored.
    """

    #: Patterns replaced before a request is written to disk.
    REDACTIONS: list[tuple[str, str]] = [
        (r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}\b", "<JWT>"),
        (r"\b(gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b", "<GITHUB_TOKEN>"),
        (r"\bsk-[A-Za-z0-9]{20,}\b", "<API_KEY>"),
        (r"\bAKIA[0-9A-Z]{16}\b", "<AWS_KEY>"),
        (r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+",
         r"\1=<REDACTED>"),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
         "<PRIVATE_KEY>"),
    ]

    def __init__(
        self,
        path: os.PathLike[str] | str,
        clock: Callable[[], float] = time.time,
        redact: bool = True,
        max_request_chars: int = 2000,
    ) -> None:
        """Configure the JSONL path, injectable clock, and redaction policy."""
        self.path = Path(path)
        self._clock = clock
        self.errors: list[str] = []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Telemetry must never break a run, and that includes construction.
            # A logger pointed at an unusable path degrades to a no-op that
            # says so, rather than raising before the task even starts.
            self.errors.append(f"log directory unusable: {exc!r}")
        self.redact = bool(redact)
        self.max_request_chars = int(max_request_chars)
        self.written: int = 0

    def scrub(self, text: str) -> str:
        """Replace secret-looking substrings and truncate."""
        out = text or ""
        if self.redact:
            for pattern, replacement in self.REDACTIONS:
                out = re.sub(pattern, replacement, out)
        if len(out) > self.max_request_chars:
            out = out[: self.max_request_chars] + " ...<truncated>"
        return out

    def record(self, record: TaskRecord) -> bool:
        """Append one record. Never raises; see class docstring."""
        try:
            record.request = self.scrub(record.request)
            if not record.at:
                record.at = self._clock()
            line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
            self.written += 1
            return True
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return False

    def from_result(self, result: Any, request: str = "") -> TaskRecord | None:
        """Build a TaskRecord from any result object, old pipeline or new."""
        # Reads whatever attributes are present via getattr/duck-typing: a
        # log written before the agent existed stays readable, and a log
        # written now stays readable after whatever comes next.
        try:
            turn = getattr(result, "turn", None)
            plan = getattr(result, "plan", None)
            rollback = getattr(result, "rollback", None)
            outcome = getattr(result, "outcome", None) or getattr(result, "status", None)
            coverage = (getattr(plan, "coverage", None)
                        or getattr(result, "undo_coverage", None) or {})

            record = TaskRecord(
                task_id=str(getattr(getattr(plan, "spec", None), "task_id", "")
                            or getattr(result, "task_id", "")),
                request=request or str(getattr(result, "request", "")
                                       or getattr(result, "task", "")),
                at=self._clock(),
                status=getattr(outcome, "value", str(outcome or "")),
                stages_completed=list(getattr(result, "stages", None)
                                      or getattr(result, "stages_completed", []) or []),
                abort_reason=str(getattr(result, "abort_reason", "")),
                rollback_ran=rollback is not None,
                rollback_verified=(getattr(rollback, "ok", None) if rollback
                                   else getattr(result, "rollback_verified", None)),
                undo_coverage=float(coverage.get("coverage_ratio", 0.0)),
                uncovered_steps=list(coverage.get("uncovered", []) or []),
                seconds=float(getattr(result, "seconds", 0.0)),
                harness_phase=7,
            )

            results = getattr(result, "results", []) or []
            record.steps_total = len(results)
            record.steps_run = len([r for r in results if getattr(r, "ok", False)])

            if turn is not None:
                record.intent = getattr(getattr(turn, "intent", None), "value", "")
                record.model = str(getattr(turn, "model", ""))
                record.routing_rule = str(getattr(turn, "routing_reason", ""))[:80]
                record.repairs = int(getattr(turn, "repairs", 0))
                record.clarifier_codes = [g.split(":")[0] for g
                                          in getattr(turn, "clarifier_gaps", [])]
                # Found in a full-file audit: this field was declared on
                # TaskRecord but never assigned here, so every persisted
                # record carried an empty anchor list. LoopholeAudit only
                # worked because it falls back to recomputing anchors via
                # the clarifier at audit time -- this makes the JSONL log
                # self-contained instead of depending on that recompute.
                record.clarifier_anchors = list(getattr(turn, "anchors", []))
                record.clarifier_blocked = record.intent == "ask"
            if plan is not None:
                record.proposed_steps = len(getattr(plan, "steps", []))
                record.worst_level = getattr(
                    getattr(plan, "worst_level", None), "value", "")
                record.approved = all(
                    getattr(sp, "approved", None) is not False
                    for sp in getattr(plan, "steps", []))
            return record
        except Exception as exc:
            self.errors.append(f"from_result failed: {type(exc).__name__}: {exc}")
            return None

    def read(self, limit: int | None = None) -> list[TaskRecord]:
        """Read records back, newest last. A torn line is skipped and counted."""
        if not self.path.exists():
            return []
        records: list[TaskRecord] = []
        try:
            for number, raw in enumerate(
                    self.path.read_text(encoding="utf-8").splitlines(), start=1):
                if not raw.strip():
                    continue
                try:
                    records.append(TaskRecord.from_dict(json.loads(raw)))
                except Exception as exc:
                    self.errors.append(f"line {number} unreadable: {exc}")
        except OSError as exc:
            self.errors.append(f"read failed: {exc!r}")
        return records[-limit:] if limit else records

    def stats(self) -> dict[str, Any]:
        """Counters, including telemetry failures."""
        return {
            "path": str(self.path),
            "written": self.written,
            "on_disk": len(self.read()),
            "errors": list(self.errors),
        }
