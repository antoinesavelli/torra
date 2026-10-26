"""Append-only JSONL log of every task torra is given.

Depends on: stdlib, plus `.scrub.redact_tree` (imported lazily in `TaskLogger.record`, because
`scrub` imports this module). Every public method catches its own exceptions --
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

__all__ = ["TaskRecord", "TaskLogger", "redact_secrets"]

#: Patterns replaced before free-form operator/human text is written to
#: disk. Module level (not just a TaskLogger attribute) so labels.py's
#: Label.why -- a human's free-text rationale, which can just as easily
#: quote a secret as the request it's commenting on -- goes through the
#: same scrub instead of a second, drifting copy of these patterns.
REDACTIONS: list[tuple[str, str]] = [
    (r"\b[A-Za-z0-9_-]{20,200}\.[A-Za-z0-9_-]{10,200}\.[A-Za-z0-9_-]{20,200}\b", "<JWT>"),
    (r"\b(gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b", "<GITHUB_TOKEN>"),
    (r"\bsk-[A-Za-z0-9_-]{20,}", "<API_KEY>"),
    (r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}", "<API_KEY>"),
    (r"\b(?:npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{30,}|glpat-[A-Za-z0-9_-]{16,})", "<API_KEY>"),
    (r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", "<API_KEY>"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "<SLACK_TOKEN>"),
    (r"hooks\.slack\.com/services/[A-Za-z0-9/]+", "hooks.slack.com/services/<REDACTED>"),
    (r"\bAIza[0-9A-Za-z_-]{30,}", "<API_KEY>"),
    (r"\bhf_[A-Za-z0-9]{30,}", "<API_KEY>"),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "<AWS_KEY>"),
    (r"(?i)\bauthorization:\s*(?:basic|bearer|token)\s+\S+", "Authorization: <REDACTED>"),
    (r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", "Bearer <TOKEN>"),
    (r"(?i)\b((?:set-)?cookie:[ \t]*)[^\r\n]{1,4000}", r"\1<REDACTED>"),
    (r"(?i)\b([a-z][a-z0-9+.-]{1,20}://)[^\s/@]{1,200}@", r"\1<REDACTED>@"),
    (
        r"(?i)([?&](?:key|api_?key|access_token|token|sig|signature|secret)=)[^&\s]+",
        r"\1<REDACTED>",
    ),
    (r"(?i)(\s(?:-u|--user)[ =]+)[^\s:]+:\S+", r"\1<REDACTED>"),
    (r"(?i)(--(?:password|passwd|token|secret|api-?key)[ =]+)\S+", r"\1<REDACTED>"),
    (r"(?i)(\bsshpass\s+-p\s*)\S+", r"\1<REDACTED>"),
    (r"(?im)^(\s*(?:password|passwd)\s+)\S+$", r"\1<REDACTED>"),
    # A value under a secret-looking key in JSON or a Python dict, either quote style.
    (
        r"(?i)([\"'][A-Za-z0-9_.-]{0,40}(?:pass(?:word|wd)?|secret|token|credentials?|api[_-]?key|access[_-]?key|private[_-]?key|account[_-]?key|client[_-]?secret|psk)[A-Za-z0-9_.-]{0,40}[\"']\s*[:：]\s*)([\"'])[^\"'\n]{0,500}\2",
        r"\1\2<REDACTED>\2",
    ),
    (r"(?i)([\"']auth[\"']\s*[:：]\s*)([\"'])[^\"'\n]{0,500}\2", r"\1\2<REDACTED>\2"),
    # Live wire: HANDOFF.md §5.25
    # \S.*$ (to end of line), not \S+ (one token): a multi-word secret like
    # "password = super secret phrase" used to redact only "password = super".
    # Over-redacting the rest of the line is far cheaper than under-redacting a
    # secret. The name may carry a prefix or suffix (OPENROUTER_API_KEY, DATABASE_PASS); both
    # are bounded so hostile text cannot make the match slow.
    (
        r"(?im)(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]{0,40}(?:pass(?:word|wd)?|secret|token|credentials?|api[_-]?key|access[_-]?key|private[_-]?key|account[_-]?key|client[_-]?secret|psk)[A-Za-z0-9_.-]{0,40})\s*[=:：]\s*\S.*$",
        r"\1=<REDACTED>",
    ),
    (
        r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|KEY BLOCK)-----[\s\S]*?"
        r"(?:-----END [A-Z ]*(?:PRIVATE KEY|KEY BLOCK)-----|\Z)",
        "<PRIVATE_KEY>",
    ),
]


def redact_secrets(text: str) -> str:
    """Replace secret-looking substrings in `text`, no truncation."""
    out = text or ""
    for pattern, replacement in REDACTIONS:
        out = re.sub(pattern, replacement, out)
    return out


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
    torra_phase: int = 0
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
        # Forward compatible on purpose: a log written by a later torra must
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
    """Append-only JSONL log of every task torra is given.

    Append-only and one JSON object per line, so a crash mid-write costs at most
    the last record, a `tail -f` works, and the file can be read by anything.
    No database, no schema migration, no daemon.

    Redaction runs before anything is written. Task text is operator input and
    will eventually contain a token or a password, so patterns that look like
    secrets are replaced rather than stored.
    """

    #: Kept as an alias to the module-level list (not a second copy) so any
    #: existing reference to `TaskLogger.REDACTIONS` keeps working.
    REDACTIONS = REDACTIONS

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
        out = redact_secrets(text) if self.redact else (text or "")
        if len(out) > self.max_request_chars:
            out = out[: self.max_request_chars] + " ...<truncated>"
        return out

    def record(self, record: TaskRecord) -> bool:
        """Append one record. Never raises; see class docstring."""
        try:
            from .scrub import redact_tree  # lazily: scrub imports this module

            record.request = self.scrub(record.request)
            record.abort_reason = self.scrub(record.abort_reason)
            if not record.at:
                record.at = self._clock()
            data = record.to_dict()
            line = json.dumps(redact_tree(data) if self.redact else data, sort_keys=True) + "\n"
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
            self.written += 1
            return True
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _resolve_task_id(result: Any, plan: Any) -> str:
        return str(
            getattr(getattr(plan, "spec", None), "task_id", "") or getattr(result, "task_id", "")
        )

    @staticmethod
    def _resolve_request(result: Any, request: str) -> str:
        return request or str(getattr(result, "request", "") or getattr(result, "task", ""))

    @staticmethod
    def _resolve_stages(result: Any) -> list[str]:
        return list(
            getattr(result, "stages", None) or getattr(result, "stages_completed", []) or []
        )

    @staticmethod
    def _resolve_coverage(result: Any, plan: Any) -> dict[str, Any]:
        return getattr(plan, "coverage", None) or getattr(result, "undo_coverage", None) or {}

    def _base_record(self, result: Any, request: str) -> TaskRecord:
        """Everything derivable without knowing if turn/plan are present."""
        plan = getattr(result, "plan", None)
        rollback = getattr(result, "rollback", None)
        outcome = getattr(result, "outcome", None) or getattr(result, "status", None)
        coverage = self._resolve_coverage(result, plan)

        record = TaskRecord(
            task_id=self._resolve_task_id(result, plan),
            request=self._resolve_request(result, request),
            at=self._clock(),
            status=getattr(outcome, "value", str(outcome or "")),
            stages_completed=self._resolve_stages(result),
            abort_reason=str(getattr(result, "abort_reason", "")),
            rollback_ran=rollback is not None,
            rollback_verified=(
                getattr(rollback, "ok", None)
                if rollback
                else getattr(result, "rollback_verified", None)
            ),
            undo_coverage=float(coverage.get("coverage_ratio", 0.0)),
            uncovered_steps=list(coverage.get("uncovered", []) or []),
            seconds=float(getattr(result, "seconds", 0.0)),
            torra_phase=7,
        )
        results = getattr(result, "results", []) or []
        record.steps_total = len(results)
        record.steps_run = len([r for r in results if getattr(r, "ok", False)])
        return record

    @staticmethod
    def _apply_turn_fields(record: TaskRecord, turn: Any) -> None:
        record.intent = getattr(getattr(turn, "intent", None), "value", "")
        record.model = str(getattr(turn, "model", ""))
        record.routing_rule = str(getattr(turn, "routing_reason", ""))[:80]
        record.repairs = int(getattr(turn, "repairs", 0))
        record.clarifier_codes = [g.split(":")[0] for g in getattr(turn, "clarifier_gaps", [])]
        # Found in a full-file audit: this field was declared on TaskRecord
        # but never assigned here, so every persisted record carried an
        # empty anchor list. LoopholeAudit only worked because it falls
        # back to recomputing anchors via the clarifier at audit time --
        # this makes the JSONL log self-contained instead of depending on
        # that recompute.
        record.clarifier_anchors = list(getattr(turn, "anchors", []))
        record.clarifier_blocked = record.intent == "ask"

    @staticmethod
    def _apply_plan_fields(record: TaskRecord, plan: Any) -> None:
        record.proposed_steps = len(getattr(plan, "steps", []))
        record.worst_level = getattr(getattr(plan, "worst_level", None), "value", "")
        # `is True`, not `is not False`: a step's `approved` is None until
        # Torra._apply_approvals() actually runs (inside execute()), and
        # that only happens once the operator has been shown the plan. A
        # plan blocked by a failed precondition (Torra.plan() calling
        # _finish() before execute() is ever reached) leaves every step's
        # `approved` at None -- `is not False` treated that as approved,
        # so a BLOCKED task that was never shown to anyone still recorded
        # `approved: true`. _apply_approvals() itself always resolves every
        # step to a real True/False (steps needing no confirmation are
        # auto-True), so after a real execute() this is exactly as strict.
        record.approved = all(
            getattr(sp, "approved", None) is True for sp in getattr(plan, "steps", [])
        )

    def from_result(self, result: Any, request: str = "") -> TaskRecord | None:
        """Build a TaskRecord from any result object, old pipeline or new."""
        # Reads whatever attributes are present via getattr/duck-typing: a
        # log written before the agent existed stays readable, and a log
        # written now stays readable after whatever comes next.
        try:
            record = self._base_record(result, request)
            turn = getattr(result, "turn", None)
            if turn is not None:
                self._apply_turn_fields(record, turn)
            plan = getattr(result, "plan", None)
            if plan is not None:
                self._apply_plan_fields(record, plan)
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
                self.path.read_text(encoding="utf-8").splitlines(), start=1
            ):
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
