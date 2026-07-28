"""
harness_telemetry.py
Phase 5. Instrumentation, not features.

Four phases of hardening ended with a clarifier scoring 0% false negatives on a
22 entry blind corpus that I wrote myself. That is encouraging and it is not
evidence. The Phase 4 report said the next real finding would come from running
against real traffic rather than from another synthetic probe, and this module
is what makes that possible.

  TaskLogger      append-only JSONL of every task the harness is given, with
                  what each gate decided and what actually happened
  LabelStore      attaches human labels to logged tasks
  CorpusExporter  turns labeled traffic into corpus format and reports drift
                  against the synthetic blind set
  LoopholeAudit   quantifies the anchored-but-goalless gap Phase 4 found but
                  deliberately did not fix

The design rule here is different from the rest of the harness: telemetry must
never be able to break a run. Every public method catches its own exceptions and
records them, because a logger that raises during a deploy is worse than no
logger at all.

Phases 1 through 4 are unmodified.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "TaskRecord",
    "TaskLogger",
    "Label",
    "LabelStore",
    "CorpusExporter",
    "DriftReport",
    "LoopholeAudit",
    "LoopholeReport",
]


# --------------------------------------------------------------------------- #
# Task logging
# --------------------------------------------------------------------------- #


@dataclass
class TaskRecord:
    """One task, from the request text to the final outcome.

    This is the unit of evidence. Everything Phase 5 can tell you is derived
    from a pile of these.
    """

    task_id: str
    request: str
    at: float
    clarifier_blocked: bool = False
    clarifier_codes: List[str] = field(default_factory=list)
    clarifier_anchors: List[str] = field(default_factory=list)
    clarifier_resolved: bool = True
    model: str = ""
    routing_rule: str = ""
    status: str = ""
    stages_completed: List[str] = field(default_factory=list)
    steps_total: int = 0
    steps_run: int = 0
    abort_reason: str = ""
    rollback_ran: bool = False
    rollback_verified: Optional[bool] = None
    undo_coverage: float = 0.0
    uncovered_steps: List[str] = field(default_factory=list)
    seconds: float = 0.0
    harness_phase: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, one JSONL line."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TaskRecord":
        """Rebuild from a JSONL line, ignoring fields this version does not know.

        Forward compatible on purpose: a log written by a later harness must
        still be readable, because the whole point of the log is that it
        outlives any one version of the code.
        """
        known = {f for f in cls.__dataclass_fields__}
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
    REDACTIONS: List[Tuple[str, str]] = [
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
        """
        Args:
            path: JSONL file to append to. Parent directories are created.
            clock: Injectable time source.
            redact: Scrub secret-looking substrings before writing.
            max_request_chars: Truncate request text beyond this.
        """
        self.path = Path(path)
        self._clock = clock
        self.errors: List[str] = []
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
        """Append one record.

        Returns:
            True when the line was written. Never raises: a telemetry failure
            must not take down the task it is describing, so the error is
            collected in `self.errors` and reported by `stats()`.
        """
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

    def from_result(self, result: Any, request: str = "") -> Optional[TaskRecord]:
        """Build a TaskRecord from any pipeline result object.

        Reads whatever attributes are present, so it works against a
        PipelineResult from Phase 1 or a Phase4Result, and does not break when a
        later phase adds fields.
        """
        try:
            clarification = getattr(result, "clarification", None)
            execution = getattr(result, "execution", None)
            rollback = getattr(result, "rollback", None)
            coverage = getattr(result, "undo_coverage", {}) or {}
            choice = getattr(result, "model_choice", None)
            status = getattr(result, "status", None)

            return TaskRecord(
                task_id=str(getattr(result, "task_id", "")),
                request=request or str(getattr(result, "task", "")),
                at=self._clock(),
                clarifier_blocked=bool(
                    clarification and clarification.needs_clarification
                    and not clarification.resolved),
                clarifier_codes=[a.code for a in (
                    clarification.ambiguities if clarification else [])],
                clarifier_anchors=list(getattr(result, "clarifier_anchors", []) or []),
                clarifier_resolved=bool(clarification.resolved) if clarification else True,
                model=getattr(choice, "model", "") if choice else "",
                routing_rule=getattr(choice, "matched_rule", "") if choice else "",
                status=getattr(status, "value", str(status or "")),
                stages_completed=list(getattr(result, "stages_completed", []) or []),
                steps_total=len(getattr(execution, "results", []) or []) if execution else 0,
                steps_run=len([r for r in (getattr(execution, "results", []) or [])
                               if getattr(r, "ok", False)]) if execution else 0,
                abort_reason=getattr(execution, "abort_reason", "") if execution else "",
                rollback_ran=rollback is not None,
                rollback_verified=getattr(result, "rollback_verified", None),
                undo_coverage=float(coverage.get("coverage_ratio", 0.0)),
                uncovered_steps=list(coverage.get("uncovered", []) or []),
                seconds=float(getattr(result, "seconds", 0.0)),
                harness_phase=int(getattr(result, "harness_phase", 0)) or self._phase_of(result),
            )
        except Exception as exc:
            self.errors.append(f"from_result failed: {type(exc).__name__}: {exc}")
            return None

    @staticmethod
    def _phase_of(result: Any) -> int:
        """Infer the harness phase from the result class name."""
        name = type(result).__name__
        match = re.search(r"Phase(\d)", name)
        return int(match.group(1)) if match else 1

    def read(self, limit: Optional[int] = None) -> List[TaskRecord]:
        """Read records back, newest last. A torn line is skipped and counted."""
        if not self.path.exists():
            return []
        records: List[TaskRecord] = []
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

    def stats(self) -> Dict[str, Any]:
        """Counters, including telemetry failures."""
        return {
            "path": str(self.path),
            "written": self.written,
            "on_disk": len(self.read()),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# Labeling
# --------------------------------------------------------------------------- #


@dataclass
class Label:
    """A human verdict on one logged request."""

    request: str
    ambiguous: bool
    why: str = ""
    labeled_at: float = 0.0
    labeler: str = ""

    def to_dict(self) -> Dict[str, Any]:
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
        """
        Args:
            path: JSONL file holding labels.
            clock: Injectable time source.
        """
        self.path = Path(path)
        self._clock = clock
        self.errors: List[str] = []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.errors.append(f"label directory unusable: {exc!r}")

    def label(self, request: str, ambiguous: bool, why: str = "",
              labeler: str = "") -> Label:
        """Record one verdict. A later label for the same text supersedes it."""
        entry = Label(request=request, ambiguous=bool(ambiguous), why=why,
                      labeled_at=self._clock(), labeler=labeler)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
                fh.flush()
        except OSError as exc:
            self.errors.append(f"label write failed: {exc!r}")
        return entry

    def all(self) -> Dict[str, Label]:
        """Every label, latest wins, keyed by request text."""
        out: Dict[str, Label] = {}
        if not self.path.exists():
            return out
        try:
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    data = json.loads(raw)
                    out[data["request"]] = Label(**data)
                except Exception as exc:
                    self.errors.append(f"label line unreadable: {exc}")
        except OSError as exc:
            self.errors.append(f"label read failed: {exc!r}")
        return out

    def unlabeled(self, records: Sequence[TaskRecord]) -> List[str]:
        """Distinct request texts in `records` that have no label yet.

        Ordered by frequency, so labeling effort goes to the phrasings that
        actually recur rather than to one-offs.
        """
        labeled = set(self.all())
        counts = Counter(r.request for r in records if r.request not in labeled)
        return [request for request, _n in counts.most_common()]


# --------------------------------------------------------------------------- #
# Corpus export and drift
# --------------------------------------------------------------------------- #


@dataclass
class DriftReport:
    """How the clarifier scores on real traffic versus the synthetic corpus."""

    labeled_total: int = 0
    ambiguous_total: int = 0
    specific_total: int = 0
    false_negatives: List[Tuple[str, str]] = field(default_factory=list)
    false_positives: List[Tuple[str, List[str]]] = field(default_factory=list)
    fn_rate: float = 0.0
    fp_rate: float = 0.0
    baseline_fn_rate: Optional[float] = None
    baseline_fp_rate: Optional[float] = None
    verdict: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)

    def render(self) -> str:
        """Human readable drift summary."""
        lines = [
            f"labeled traffic: {self.labeled_total} "
            f"({self.ambiguous_total} ambiguous, {self.specific_total} specific)",
            f"  live false negative rate : {self.fn_rate:.0%}"
            + (f"  (synthetic baseline {self.baseline_fn_rate:.0%})"
               if self.baseline_fn_rate is not None else ""),
            f"  live false positive rate : {self.fp_rate:.0%}"
            + (f"  (synthetic baseline {self.baseline_fp_rate:.0%})"
               if self.baseline_fp_rate is not None else ""),
            f"  verdict: {self.verdict}",
        ]
        if self.false_negatives:
            lines.append("  MISSED (ambiguous, waved through):")
            lines.extend(f"    {r!r}  ({why})" for r, why in self.false_negatives)
        if self.false_positives:
            lines.append("  BLOCKED (specific, should have passed):")
            lines.extend(f"    {r!r}  {codes}" for r, codes in self.false_positives)
        return "\n".join(lines)


class CorpusExporter:
    """Turns labeled traffic into corpus format and measures drift.

    The point of this class is one number: the false negative rate on requests
    a human labeled, from a real log, rather than on requests I invented. If
    that number is materially worse than the synthetic baseline, the synthetic
    corpus was measuring the wrong thing and the clarifier needs work.

    `MIN_SAMPLE` exists because a false negative rate computed on four requests
    is noise wearing a percentage sign, and reporting it as though it were a
    result is exactly the failure this whole phase is meant to correct.
    """

    #: Below this many labeled entries the drift verdict is "insufficient data".
    MIN_SAMPLE: int = 30

    def __init__(self, logger: TaskLogger, labels: LabelStore) -> None:
        """
        Args:
            logger: Where task traffic is recorded.
            labels: Where human verdicts live.
        """
        self.logger = logger
        self.labels = labels

    def export(self) -> Tuple[List[Tuple[str, str]], List[str]]:
        """Return (ambiguous, specific) sets in `clarifier_corpus` format."""
        labeled = self.labels.all()
        ambiguous = [(text, entry.why or "labeled ambiguous")
                     for text, entry in labeled.items() if entry.ambiguous]
        specific = [text for text, entry in labeled.items() if not entry.ambiguous]
        return ambiguous, specific

    def write_corpus(self, path: os.PathLike[str] | str) -> Path:
        """Write the labeled traffic as an importable Python corpus module."""
        ambiguous, specific = self.export()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = [
            '"""',
            "live_corpus.py",
            "Generated from labeled harness traffic by CorpusExporter.",
            "Do not hand edit: regenerate it instead, so the labels stay the",
            "source of truth.",
            '"""',
            "",
            "from typing import List, Tuple",
            "",
            "LIVE_AMBIGUOUS: List[Tuple[str, str]] = [",
        ]
        body += [f"    ({r!r}, {w!r})," for r, w in sorted(ambiguous)]
        body += ["]", "", "LIVE_SPECIFIC: List[str] = ["]
        body += [f"    {r!r}," for r in sorted(specific)]
        body += ["]", ""]
        target.write_text("\n".join(body), encoding="utf-8")
        return target

    def drift(
        self,
        clarifier: Any,
        baseline_fn: Optional[float] = None,
        baseline_fp: Optional[float] = None,
    ) -> DriftReport:
        """Score a clarifier against labeled traffic and compare to the baseline.

        Args:
            clarifier: Anything with `detect(task) -> list`.
            baseline_fn: Synthetic corpus false negative rate, for comparison.
            baseline_fp: Synthetic corpus false positive rate.

        Returns:
            A DriftReport. `verdict` is deliberately conservative: with fewer
            than MIN_SAMPLE labels it refuses to draw a conclusion.
        """
        ambiguous, specific = self.export()
        report = DriftReport(
            labeled_total=len(ambiguous) + len(specific),
            ambiguous_total=len(ambiguous),
            specific_total=len(specific),
            baseline_fn_rate=baseline_fn,
            baseline_fp_rate=baseline_fp,
        )

        for request, why in ambiguous:
            try:
                if not clarifier.detect(request):
                    report.false_negatives.append((request, why))
            except Exception as exc:
                report.false_negatives.append((request, f"detect raised {exc!r}"))

        for request in specific:
            try:
                found = clarifier.detect(request)
                if found:
                    report.false_positives.append(
                        (request, [a.code for a in found]))
            except Exception as exc:
                report.false_positives.append((request, [f"detect raised {exc!r}"]))

        report.fn_rate = round(
            len(report.false_negatives) / len(ambiguous), 4) if ambiguous else 0.0
        report.fp_rate = round(
            len(report.false_positives) / len(specific), 4) if specific else 0.0

        if report.labeled_total < self.MIN_SAMPLE:
            report.verdict = (
                f"insufficient data: {report.labeled_total} labeled of "
                f"{self.MIN_SAMPLE} needed. No conclusion drawn."
            )
        elif baseline_fn is not None and report.fn_rate > baseline_fn + 0.10:
            report.verdict = (
                f"DRIFT: live false negatives {report.fn_rate:.0%} against a "
                f"synthetic baseline of {baseline_fn:.0%}. The synthetic corpus "
                "is measuring the wrong thing; retune against live labels."
            )
        elif baseline_fp is not None and report.fp_rate > baseline_fp + 0.15:
            report.verdict = (
                f"DRIFT: live false positives {report.fp_rate:.0%} against "
                f"{baseline_fp:.0%}. The clarifier is interrupting real work."
            )
        else:
            report.verdict = "live performance is consistent with the baseline"
        return report


# --------------------------------------------------------------------------- #
# The Phase 4 loophole, quantified
# --------------------------------------------------------------------------- #


@dataclass
class LoopholeReport:
    """How often the anchored-but-goalless gap actually occurs."""

    total: int = 0
    passed_clarifier: int = 0
    anchored_but_goalless: int = 0
    examples: List[str] = field(default_factory=list)
    goalless_failure_rate: Optional[float] = None
    anchored_failure_rate: Optional[float] = None
    verdict: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


#: Verbs whose completion is not self-evident: naming a target does not say what
#: "done" looks like.
GOALLESS_VERBS = (
    r"\b(fix|clean\s*up|sort\s*out|handle|deal\s+with|check|look\s+at|review|"
    r"investigate|sort|tidy|address|update|improve|adjust|tweak|refresh)\b"
)

#: Structures that state an outcome.
OUTCOME_MARKERS = (
    r"\b(to|so\s+that|until|and\s+(confirm|verify|check|report|ensure)|"
    r"reports?|returns?|equals?|becomes?|matches?|=|under|over|below|above|"
    r"at\s+least|at\s+most|within)\b"
)


class LoopholeAudit:
    """Measures the gap Phase 4 found and deliberately did not close.

    Phase 4's probe B showed that `fix /etc/nginx/nginx.conf` clears the
    clarifier: it has a path anchor, so there is a target, but "fix" says
    nothing about what done looks like. The obvious response is to require an
    acceptance criterion for every mutating verb. The reason Phase 4 did not do
    that is that it would raise the false positive rate for a problem nobody had
    measured the frequency of.

    This measures it. Two numbers matter:

      * how often anchored-but-goalless requests occur in real traffic
      * whether they fail more often than anchored requests that state an outcome

    If they occur rarely, the loophole is theoretical and closing it would cost
    more in interruptions than it saves. If they occur often AND fail more, the
    clarifier should require an outcome. That is a decision the data makes, not
    one I should make in a report.
    """

    MIN_SAMPLE: int = 30

    def __init__(self, clarifier: Any) -> None:
        """
        Args:
            clarifier: Anything with `detect(task)` and `anchors_in(task)`.
        """
        self.clarifier = clarifier

    @staticmethod
    def is_goalless(request: str) -> bool:
        """True when the request uses a vague verb and states no outcome."""
        if not re.search(GOALLESS_VERBS, request, re.IGNORECASE):
            return False
        return not re.search(OUTCOME_MARKERS, request, re.IGNORECASE)

    def audit(self, records: Sequence[TaskRecord]) -> LoopholeReport:
        """Quantify the loophole across logged traffic.

        Returns:
            A LoopholeReport. Like DriftReport, it refuses to conclude anything
            from a small sample.
        """
        report = LoopholeReport(total=len(records))
        goalless: List[TaskRecord] = []
        anchored_with_goal: List[TaskRecord] = []

        for record in records:
            try:
                blocked = bool(self.clarifier.detect(record.request))
            except Exception:
                continue
            if blocked:
                continue
            report.passed_clarifier += 1

            has_anchor = bool(record.clarifier_anchors) or bool(
                getattr(self.clarifier, "anchors_in", lambda _t: [])(record.request))
            if not has_anchor:
                continue

            if self.is_goalless(record.request):
                goalless.append(record)
                if len(report.examples) < 10:
                    report.examples.append(record.request)
            else:
                anchored_with_goal.append(record)

        report.anchored_but_goalless = len(goalless)

        def failure_rate(group: List[TaskRecord]) -> Optional[float]:
            """Fraction of executed tasks in a group that did not complete."""
            executed = [r for r in group if r.executed]
            if not executed:
                return None
            return round(sum(1 for r in executed if not r.succeeded) / len(executed), 4)

        report.goalless_failure_rate = failure_rate(goalless)
        report.anchored_failure_rate = failure_rate(anchored_with_goal)

        if report.total < self.MIN_SAMPLE:
            report.verdict = (
                f"insufficient data: {report.total} tasks of {self.MIN_SAMPLE} "
                "needed. No conclusion drawn."
            )
        elif not report.passed_clarifier:
            report.verdict = "no tasks cleared the clarifier, nothing to audit"
        elif report.anchored_but_goalless == 0:
            report.verdict = (
                "the loophole did not occur in this traffic; closing it would "
                "cost interruptions and save nothing"
            )
        elif (report.goalless_failure_rate is not None
              and report.anchored_failure_rate is not None
              and report.goalless_failure_rate > report.anchored_failure_rate + 0.15):
            report.verdict = (
                f"ACT: {report.anchored_but_goalless} goalless request(s) fail at "
                f"{report.goalless_failure_rate:.0%} against "
                f"{report.anchored_failure_rate:.0%} for requests that state an "
                "outcome. Require an acceptance criterion."
            )
        else:
            share = report.anchored_but_goalless / max(report.passed_clarifier, 1)
            report.verdict = (
                f"{report.anchored_but_goalless} goalless request(s), "
                f"{share:.0%} of those that passed, with no elevated failure "
                "rate. The loophole is real but not yet costly; keep measuring."
            )
        return report
