"""cli.py: no test file existed for this module before this one.

Scoped to what a full-file audit actually touched here -- LoopholeAudit was
fully built and exported (harness/telemetry/audit.py) but had no caller
anywhere, cmd_evidence() included. This covers that wiring, not the rest of
cli.py's surface (argument parsing, cmd_ask/plan/run/check/doctor/label all
remain untested; a real cli.py suite is a separate piece of work).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from harness.cli import cmd_evidence
from harness.telemetry import LabelStore, TaskLogger, TaskRecord


def evidence_args(workdir: Path) -> argparse.Namespace:
    return argparse.Namespace(workdir=str(workdir), baseline_fn=0.0, baseline_fp=0.10)


def test_evidence_prints_the_loophole_audit_alongside_drift(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Found in a full-file audit: LoopholeAudit (harness/telemetry/audit.py)
    was fully implemented and exported but had no caller anywhere -- unlike
    its sibling CorpusExporter.drift(), which cmd_evidence already printed.
    A well-designed, working measurement with zero way to actually see its
    output is effectively dead code."""
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    for request in ["fix the api", "fix the api", "restart ollama.service"]:
        logger.record(TaskRecord(task_id="t", request=request, at=1.0))
    LabelStore(tmp_path / "telemetry" / "labels.jsonl")

    exit_code = cmd_evidence(evidence_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "anchored-but-goalless" in out
    assert "verdict" in out
