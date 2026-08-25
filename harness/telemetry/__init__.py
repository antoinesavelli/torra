"""What the harness records about itself: task log, human labels, drift and
loophole audits against the synthetic clarifier corpus, and crash reports.

Depends on: harness.telemetry.records, .labels, .audit, .crash (this package
only re-exports; .labels depends on .records for TaskRecord, .audit depends
on both for TaskLogger/TaskRecord/LabelStore, .crash depends on .records for
redact_secrets).
"""

from __future__ import annotations

from .audit import CorpusExporter, DriftReport, LoopholeAudit, LoopholeReport
from .crash import CrashRecord, CrashReporter
from .labels import Label, LabelStore
from .records import TaskLogger, TaskRecord

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
]
