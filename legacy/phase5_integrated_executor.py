"""
phase5_integrated_executor.py
Phase 5 pipeline. Same 8 stages, now instrumented.

    on every run      -> TaskLogger records the request, every gate verdict,
                         and the outcome, whether the run succeeded or not
    shadow reaping    -> StrictShadowReaper

Logging is on by default and is not a stage. A task that is refused at stage 1
is exactly the task most worth recording, so the log write happens outside the
stage machinery and covers early exits, lock refusals, and crashes alike.

Phases 1 through 4 are unmodified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from harness_core import Step
from core_behaviors_v4 import StructuralClarifier
from core_behaviors_v5 import StrictShadowReaper
from harness_telemetry import (
    CorpusExporter,
    DriftReport,
    LabelStore,
    LoopholeAudit,
    LoopholeReport,
    TaskLogger,
    TaskRecord,
)
from phase4_integrated_executor import Phase4IntegratedExecutor, Phase4Result

__all__ = ["Phase5Result", "Phase5IntegratedExecutor"]


@dataclass
class Phase5Result(Phase4Result):
    """Phase4Result plus the telemetry outcome."""

    logged: bool = False
    log_path: str = ""
    telemetry_errors: List[str] = field(default_factory=list)
    reaper_near_misses: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        base = super().to_dict()
        base.update({
            "phase": 5,
            "logged": self.logged,
            "log_path": self.log_path,
            "telemetry_errors": self.telemetry_errors,
            "reaper_near_misses": self.reaper_near_misses,
        })
        return base


class Phase5IntegratedExecutor(Phase4IntegratedExecutor):
    """The Phase 4 pipeline that records evidence about itself.

    Everything Phase 4 guaranteed still holds. What changes is that every run
    leaves a labeled-able record behind, so the next round of hardening can be
    driven by what the harness actually saw rather than by what I could imagine
    while writing a probe.
    """

    def __init__(
        self,
        workdir: Path | str,
        log_path: Optional[Path | str] = None,
        label_path: Optional[Path | str] = None,
        logger: Optional[TaskLogger] = None,
        labels: Optional[LabelStore] = None,
        log_tasks: bool = True,
        **kwargs: Any,
    ) -> None:
        """
        Args:
            log_path: JSONL task log. Defaults to `<workdir>/telemetry/tasks.jsonl`.
            label_path: JSONL label store. Defaults to
                `<workdir>/telemetry/labels.jsonl`.
            logger: Pre-built TaskLogger, overrides log_path.
            labels: Pre-built LabelStore, overrides label_path.
            log_tasks: Record every run. Turn off only for throwaway testing.
            **kwargs: Passed through to Phase4IntegratedExecutor.
        """
        workdir = Path(workdir)
        telemetry_dir = workdir / "telemetry"
        kwargs.setdefault("reaper", StrictShadowReaper(roots=[workdir]))
        super().__init__(workdir=workdir, **kwargs)

        self.logger = logger or TaskLogger(
            log_path or telemetry_dir / "tasks.jsonl")
        self.labels = labels or LabelStore(
            label_path or telemetry_dir / "labels.jsonl")
        self.log_tasks = bool(log_tasks)

    # -- pipeline ---------------------------------------------------------- #

    def run(
        self,
        task_id: str,
        task: str,
        steps: Sequence[Step],
        commands: Optional[Sequence[str]] = None,
        rollback_actions: Optional[Sequence[tuple[str, Callable[[], Any], bool]]] = None,
        rollback_on_fail: bool = True,
    ) -> Phase5Result:
        """Run one task through all 8 stages and record what happened.

        Returns:
            A Phase5Result. The log write is best effort: a telemetry failure
            sets `logged=False` and populates `telemetry_errors` rather than
            affecting the run's outcome.
        """
        base = super().run(task_id, task, steps, commands,
                           rollback_actions, rollback_on_fail)
        result = self._upgrade5(base)

        if isinstance(self.reaper, StrictShadowReaper):
            result.reaper_near_misses = list(self.reaper.near_misses)

        if self.log_tasks:
            before = len(self.logger.errors)
            record = self.logger.from_result(result, request=task)
            result.logged = bool(record and self.logger.record(record))
            result.log_path = str(self.logger.path)
            result.telemetry_errors = self.logger.errors[before:]
            if result.telemetry_errors:
                self.transparency.log_decision(
                    "telemetry", "task log write failed",
                    "; ".join(result.telemetry_errors),
                    {"path": result.log_path},
                )
            else:
                self.transparency.log_decision(
                    "telemetry", "task recorded",
                    f"appended to {self.logger.path.name}; "
                    f"{self.logger.written} record(s) this session",
                    {"path": result.log_path},
                )
            result.decisions = self.transparency.audit_trail()
            if result.audit_path:
                try:
                    import json
                    Path(result.audit_path).write_text(
                        json.dumps(result.to_dict(), indent=2, default=str),
                        encoding="utf-8")
                except Exception:
                    pass
        return result

    def _upgrade5(self, base: Phase4Result) -> Phase5Result:
        """Copy a Phase4Result into a Phase5Result with the extra telemetry."""
        result = Phase5Result(task_id=base.task_id, task=base.task,
                              status=base.status)
        for name, value in vars(base).items():
            setattr(result, name, value)
        return result

    # -- evidence ---------------------------------------------------------- #

    def records(self, limit: Optional[int] = None) -> List[TaskRecord]:
        """Every task this harness has recorded."""
        return self.logger.read(limit)

    def unlabeled(self) -> List[str]:
        """Distinct request texts awaiting a human label, most frequent first."""
        return self.labels.unlabeled(self.records())

    def drift(
        self,
        baseline_fn: Optional[float] = None,
        baseline_fp: Optional[float] = None,
    ) -> DriftReport:
        """Score the live clarifier against labeled traffic.

        Args:
            baseline_fn: The synthetic corpus false negative rate to compare to.
                Pass the number from `clarifier_corpus.py`, not a guess.
            baseline_fp: The synthetic corpus false positive rate.
        """
        exporter = CorpusExporter(self.logger, self.labels)
        return exporter.drift(self.clarifier, baseline_fn, baseline_fp)

    def loophole(self) -> LoopholeReport:
        """Quantify the anchored-but-goalless gap in recorded traffic."""
        return LoopholeAudit(self.clarifier).audit(self.records())

    def export_corpus(self, path: Optional[Path | str] = None) -> Path:
        """Write labeled traffic as an importable corpus module."""
        exporter = CorpusExporter(self.logger, self.labels)
        return exporter.write_corpus(
            path or self.workdir / "telemetry" / "live_corpus.py")

    def evidence_report(self) -> str:
        """One readable summary of everything the harness knows about itself."""
        records = self.records()
        drift = self.drift()
        loophole = self.loophole()
        pending = self.unlabeled()

        blocked = sum(1 for r in records if r.clarifier_blocked)
        executed = sum(1 for r in records if r.executed)
        failed = sum(1 for r in records if r.executed and not r.succeeded)
        unverified = sum(1 for r in records if r.rollback_ran
                         and r.rollback_verified is False)

        lines = [
            "HARNESS EVIDENCE REPORT",
            "=" * 40,
            f"tasks recorded      : {len(records)}",
            f"  stopped at clarify: {blocked}",
            f"  reached execute   : {executed}",
            f"  execution failed  : {failed}",
            f"  rollback unverified: {unverified}",
            "",
            drift.render(),
            "",
            "LOOPHOLE AUDIT (anchored but goalless)",
            f"  passed clarifier  : {loophole.passed_clarifier}",
            f"  goalless          : {loophole.anchored_but_goalless}",
            f"  verdict           : {loophole.verdict}",
        ]
        if loophole.examples:
            lines.append("  examples:")
            lines.extend(f"    {e!r}" for e in loophole.examples[:5])
        if pending:
            lines += ["", f"UNLABELED ({len(pending)}), most frequent first:"]
            lines.extend(f"  {r!r}" for r in pending[:10])
        if self.logger.errors:
            lines += ["", "TELEMETRY ERRORS:"]
            lines.extend(f"  {e}" for e in self.logger.errors[:5])
        return "\n".join(lines)
