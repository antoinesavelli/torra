"""Turns labeled traffic into corpus format, measures clarifier drift, and
quantifies the anchored-but-goalless gap.

Depends on: harness.telemetry.records (TaskLogger, TaskRecord), harness.telemetry.labels
(LabelStore). `clarifier` parameters are duck-typed (`detect`/`anchors_in`), so
this module has no import-time dependency on harness.gates.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .labels import LabelStore
from .records import TaskLogger, TaskRecord

__all__ = ["DriftReport", "CorpusExporter", "LoopholeReport", "LoopholeAudit"]


@dataclass
class DriftReport:
    """How the clarifier scores on real traffic versus the synthetic corpus."""

    labeled_total: int = 0
    ambiguous_total: int = 0
    specific_total: int = 0
    false_negatives: list[tuple[str, str]] = field(default_factory=list)
    false_positives: list[tuple[str, list[str]]] = field(default_factory=list)
    fn_rate: float = 0.0
    fp_rate: float = 0.0
    baseline_fn_rate: float | None = None
    baseline_fp_rate: float | None = None
    verdict: str = ""

    def to_dict(self) -> dict[str, Any]:
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
        """Store the task logger and label store this exporter reads from."""
        self.logger = logger
        self.labels = labels

    def export(self) -> tuple[list[tuple[str, str]], list[str]]:
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
        baseline_fn: float | None = None,
        baseline_fp: float | None = None,
    ) -> DriftReport:
        """Score a clarifier against labeled traffic and compare to the baseline."""
        # `verdict` is deliberately conservative: with fewer than MIN_SAMPLE
        # labels it refuses to draw a conclusion.
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


@dataclass
class LoopholeReport:
    """How often the anchored-but-goalless gap actually occurs."""

    total: int = 0
    passed_clarifier: int = 0
    anchored_but_goalless: int = 0
    examples: list[str] = field(default_factory=list)
    goalless_failure_rate: float | None = None
    anchored_failure_rate: float | None = None
    verdict: str = ""

    def to_dict(self) -> dict[str, Any]:
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
    """Measures a gap the clarifier deliberately does not close.

    `fix /etc/nginx/nginx.conf` clears the clarifier: it has a path anchor, so
    there is a target, but "fix" says nothing about what done looks like. The
    obvious response is to require an acceptance criterion for every mutating
    verb. The reason this does not is that it would raise the false positive
    rate for a problem nobody had measured the frequency of.

    This measures it. Two numbers matter:

      * how often anchored-but-goalless requests occur in real traffic
      * whether they fail more often than anchored requests that state an outcome

    If they occur rarely, the loophole is theoretical and closing it would cost
    more in interruptions than it saves. If they occur often AND fail more, the
    clarifier should require an outcome. That is a decision the data makes, not
    one a report should make.
    """

    MIN_SAMPLE: int = 30

    def __init__(self, clarifier: Any) -> None:
        """Store the clarifier under audit (needs `detect` and `anchors_in`)."""
        self.clarifier = clarifier

    @staticmethod
    def is_goalless(request: str) -> bool:
        """True when the request uses a vague verb and states no outcome."""
        if not re.search(GOALLESS_VERBS, request, re.IGNORECASE):
            return False
        return not re.search(OUTCOME_MARKERS, request, re.IGNORECASE)

    def audit(self, records: Sequence[TaskRecord]) -> LoopholeReport:
        """Quantify the loophole across logged traffic."""
        # Like DriftReport, this refuses to conclude anything from a small sample.
        report = LoopholeReport(total=len(records))
        goalless: list[TaskRecord] = []
        anchored_with_goal: list[TaskRecord] = []

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

        def failure_rate(group: list[TaskRecord]) -> float | None:
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
