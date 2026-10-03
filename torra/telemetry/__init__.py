"""What torra records about itself: task log, human labels, drift and
loophole audits against the synthetic clarifier corpus, crash reports, and
human-readable summaries over all of it.

Depends on: torra.telemetry.records, .labels, .audit, .crash, .summary
(this package only re-exports; .labels depends on .records for TaskRecord,
.audit depends on both for TaskLogger/TaskRecord/LabelStore, .crash depends
on .records for redact_secrets, .summary depends on .records and .crash).
"""

from __future__ import annotations

from .audit import CorpusExporter, DriftReport, LoopholeAudit, LoopholeReport
from .crash import CrashRecord, CrashReporter
from .labels import Label, LabelStore
from .records import TaskLogger, TaskRecord
from .summary import crash_summary, health_summary

__all__ = [
    "TaskRecord",
    "TaskLogger",
    "Label",
    "LabelStore",
    "CorpusExporter",
    "DriftReport",
    "LoopholeAudit",
    "LoopholeReport",
    "CrashRecord",
    "CrashReporter",
    "health_summary",
    "crash_summary",
]
