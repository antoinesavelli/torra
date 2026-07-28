"""
test_phase5.py
Acceptance cases for Phase 5 instrumentation.

  task logging          cases 82 to 87
  labeling and drift    cases 88 to 92
  loophole audit        cases 93 to 96
  strict reaper         cases 97 to 98
  integration           cases 99 to 101
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_core import OllamaFallbackHandler, Step  # noqa: E402
from core_behaviors_v2 import ParsingSafetyClassifier  # noqa: E402
from core_behaviors_v4 import StructuralClarifier  # noqa: E402
from core_behaviors_v5 import StrictShadowReaper  # noqa: E402
from harness_telemetry import (  # noqa: E402
    CorpusExporter,
    LabelStore,
    LoopholeAudit,
    TaskLogger,
    TaskRecord,
)
from phase1_integrated_executor import PipelineStatus, Stage  # noqa: E402
from phase5_integrated_executor import Phase5IntegratedExecutor  # noqa: E402


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


def rec(request: str, **kw) -> TaskRecord:
    """Build a TaskRecord with sensible defaults."""
    base = dict(task_id="t", request=request, at=1.0)
    base.update(kw)
    return TaskRecord(**base)


# --------------------------------------------------------------------------- #
# Task logging, cases 82 to 87
# --------------------------------------------------------------------------- #


def test_case_82_records_round_trip(tmp_path: Path) -> None:
    """Case 82: what goes into the log comes back out intact."""
    log = TaskLogger(tmp_path / "tasks.jsonl")
    written = rec("deploy commit a1b2c3d to staging", status="completed",
                  model="devstral", stages_completed=["clarify", "execute"],
                  steps_total=3, steps_run=3)

    assert log.record(written) is True
    back = log.read()

    assert len(back) == 1
    assert back[0].request == written.request
    assert back[0].model == "devstral"
    assert back[0].executed is True
    assert back[0].succeeded is True
    assert log.stats()["errors"] == []


def test_case_83_secrets_are_redacted_before_disk(tmp_path: Path) -> None:
    """Case 83: task text is operator input and will eventually hold a secret."""
    log = TaskLogger(tmp_path / "tasks.jsonl")
    secrets = [
        ("deploy with token=ghp_abcdefghijklmnopqrstuvwxyz012345", "ghp_"),
        ("set API_KEY=sk-abcdefghijklmnopqrstuvwxyz", "sk-"),
        ("use password: hunter2seriously", "hunter2"),
        ("aws key AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
    ]
    for text, leak in secrets:
        log.record(rec(text))

    blob = (tmp_path / "tasks.jsonl").read_text(encoding="utf-8")
    for _text, leak in secrets:
        assert leak not in blob, f"{leak!r} reached disk"
    assert "<REDACTED>" in blob or "<GITHUB_TOKEN>" in blob or "<API_KEY>" in blob


def test_case_84_telemetry_failure_never_raises(tmp_path: Path) -> None:
    """Case 84: a logger that raises during a deploy is worse than no logger."""
    # A plain chmod is meaningless when the suite runs as root, which it does
    # in a container, so this uses a path that cannot work for anyone: a
    # parent directory that is actually a file.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")

    log = TaskLogger(tmp_path / "tasks.jsonl")
    log.path = blocker / "tasks.jsonl"

    ok = log.record(rec("deploy commit a1b2c3d to staging"))
    assert ok is False, "the write should have failed"
    assert log.errors, "and the failure must be recorded, not swallowed"
    assert log.stats()["errors"]

    # Construction against an unusable path must also not raise.
    doomed = TaskLogger(blocker / "nested" / "tasks.jsonl")
    assert doomed.errors, "an unusable log directory must be reported at build time"
    assert doomed.record(rec("anything")) is False
    assert doomed.read() == []


def test_case_85_torn_line_is_skipped_and_counted(tmp_path: Path) -> None:
    """Case 85: a crash mid-write costs one record, not the whole log."""
    path = tmp_path / "tasks.jsonl"
    log = TaskLogger(path)
    log.record(rec("first task with /srv/api"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"task_id": "torn", "request": "half a lin\n')
    log.record(rec("third task with /srv/api"))

    back = log.read()
    assert len(back) == 2, "the two intact records must survive"
    assert any("unreadable" in e for e in log.errors)


def test_case_86_forward_compatible_with_unknown_fields(tmp_path: Path) -> None:
    """Case 86: a log written by a later harness is still readable."""
    path = tmp_path / "tasks.jsonl"
    path.write_text(json.dumps({
        "task_id": "future", "request": "deploy to staging", "at": 1.0,
        "status": "completed", "a_field_from_phase_9": {"nested": True},
    }) + "\n", encoding="utf-8")

    back = TaskLogger(path).read()
    assert len(back) == 1
    assert back[0].task_id == "future"
    assert back[0].status == "completed"


def test_case_87_builds_a_record_from_any_pipeline_result(tmp_path: Path) -> None:
    """Case 87: the logger reads results from any phase without knowing which."""
    pipe = Phase5IntegratedExecutor(
        workdir=tmp_path / "run",
        clarifier=StructuralClarifier(ask=lambda a: "staging"),
        safety=ParsingSafetyClassifier(confirm=lambda v: True),
        ollama=healthy())
    steps = [Step("s1", command="echo x", description="d", estimated_seconds=20,
                  run=lambda: "ok")]
    result = pipe.run("t1", "deploy commit a1b2c3d to staging", steps)

    records = pipe.records()
    assert len(records) == 1
    assert records[0].request == "deploy commit a1b2c3d to staging"
    assert records[0].status == "completed"
    assert records[0].harness_phase == 5
    assert records[0].clarifier_anchors, "anchors must be captured"
    assert result.logged is True
    assert result.telemetry_errors == []


# --------------------------------------------------------------------------- #
# Labeling and drift, cases 88 to 92
# --------------------------------------------------------------------------- #


def test_case_88_unlabeled_is_ordered_by_frequency(tmp_path: Path) -> None:
    """Case 88: labeling effort goes to phrasings that actually recur."""
    log = TaskLogger(tmp_path / "tasks.jsonl")
    labels = LabelStore(tmp_path / "labels.jsonl")
    for _ in range(5):
        log.record(rec("restart the server"))
    for _ in range(2):
        log.record(rec("check the logs"))
    log.record(rec("deploy commit a1b2c3d to staging"))

    pending = labels.unlabeled(log.read())
    assert pending[0] == "restart the server"
    assert pending[1] == "check the logs"

    labels.label("restart the server", True, "which host")
    assert "restart the server" not in labels.unlabeled(log.read())


def test_case_89_latest_label_supersedes(tmp_path: Path) -> None:
    """Case 89: a labeler can change their mind without editing the file."""
    labels = LabelStore(tmp_path / "labels.jsonl")
    labels.label("purge the cache", True, "which cache")
    labels.label("purge the cache", False, "on reflection there is only one")

    stored = labels.all()
    assert len(stored) == 1
    assert stored["purge the cache"].ambiguous is False
    assert "only one" in stored["purge the cache"].why


def test_case_90_drift_refuses_to_conclude_from_a_small_sample(tmp_path: Path) -> None:
    """Case 90: a rate computed on four requests is noise wearing a percentage."""
    log = TaskLogger(tmp_path / "tasks.jsonl")
    labels = LabelStore(tmp_path / "labels.jsonl")
    labels.label("fix it", True, "no target")
    labels.label("deploy commit a1b2c3d to staging", False)

    report = CorpusExporter(log, labels).drift(
        StructuralClarifier(), baseline_fn=0.0, baseline_fp=0.10)

    assert "insufficient data" in report.verdict
    assert report.labeled_total == 2
    assert "2 labeled of 30" in report.verdict


def test_case_91_drift_flags_a_real_regression(tmp_path: Path) -> None:
    """Case 91: live traffic worse than the synthetic baseline is called out."""
    log = TaskLogger(tmp_path / "tasks.jsonl")
    labels = LabelStore(tmp_path / "labels.jsonl")

    # 20 ambiguous requests the clarifier passes, plus 15 it handles correctly.
    for i in range(20):
        labels.label(f"fix /srv/api/file{i}.conf", True, "no acceptance criterion")
    for i in range(15):
        labels.label(f"restart unit{i}.service and confirm it reports active", False)

    report = CorpusExporter(log, labels).drift(
        StructuralClarifier(), baseline_fn=0.0, baseline_fp=0.10)

    assert report.labeled_total >= CorpusExporter.MIN_SAMPLE
    assert report.fn_rate > 0.5, report.fn_rate
    assert report.verdict.startswith("DRIFT"), report.verdict
    assert "retune against live labels" in report.verdict
    assert "MISSED" in report.render()


def test_case_92_corpus_export_is_importable(tmp_path: Path) -> None:
    """Case 92: labeled traffic becomes a corpus module, not a spreadsheet."""
    log = TaskLogger(tmp_path / "tasks.jsonl")
    labels = LabelStore(tmp_path / "labels.jsonl")
    labels.label("tidy the box", True, "no target")
    labels.label("restart ollama.service and confirm active", False)

    target = CorpusExporter(log, labels).write_corpus(tmp_path / "live_corpus.py")
    assert target.exists()

    namespace: dict = {}
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), namespace)
    assert ("tidy the box", "no target") in namespace["LIVE_AMBIGUOUS"]
    assert "restart ollama.service and confirm active" in namespace["LIVE_SPECIFIC"]


# --------------------------------------------------------------------------- #
# Loophole audit, cases 93 to 96
# --------------------------------------------------------------------------- #


def test_case_93_identifies_anchored_but_goalless_requests() -> None:
    """Case 93: the Phase 4 gap, detected."""
    audit = LoopholeAudit(StructuralClarifier())

    assert audit.is_goalless("fix /etc/nginx/nginx.conf") is True
    assert audit.is_goalless("clean up /var/log") is True
    assert audit.is_goalless("check /srv/api/config.yaml") is True

    # A stated outcome is not goalless.
    assert audit.is_goalless(
        "fix /etc/nginx/nginx.conf so that nginx -t returns 0") is False
    assert audit.is_goalless(
        "check /srv/api and report anything over 85 percent") is False
    assert audit.is_goalless("restart ollama.service") is False


def test_case_94_audit_refuses_to_conclude_from_a_small_sample() -> None:
    """Case 94: the same discipline as drift."""
    audit = LoopholeAudit(StructuralClarifier())
    report = audit.audit([rec("fix /etc/nginx/nginx.conf") for _ in range(5)])

    assert "insufficient data" in report.verdict
    assert report.total == 5


def test_case_95_audit_says_act_when_goalless_requests_fail_more() -> None:
    """Case 95: the decision is made by the data, not by a report author."""
    records = []
    # 20 goalless requests, 15 of which failed.
    for i in range(20):
        records.append(rec(f"fix /srv/api/f{i}.conf",
                           stages_completed=["clarify", "execute"],
                           status="execution_failed" if i < 15 else "completed"))
    # 20 requests that state an outcome, 2 of which failed.
    for i in range(20):
        records.append(rec(f"set workers to {i + 2} in /srv/api/c{i}.conf",
                           stages_completed=["clarify", "execute"],
                           status="execution_failed" if i < 2 else "completed"))

    report = LoopholeAudit(StructuralClarifier()).audit(records)

    assert report.total == 40
    assert report.anchored_but_goalless == 20
    assert report.goalless_failure_rate == pytest.approx(0.75)
    assert report.anchored_failure_rate == pytest.approx(0.10)
    assert report.verdict.startswith("ACT"), report.verdict
    assert "Require an acceptance criterion" in report.verdict


def test_case_96_audit_says_leave_it_when_the_gap_is_harmless() -> None:
    """Case 96: a real but non-costly loophole should not be coded around."""
    records = []
    for i in range(5):
        records.append(rec(f"fix /srv/api/f{i}.conf",
                           stages_completed=["clarify", "execute"],
                           status="completed"))
    for i in range(30):
        records.append(rec(f"set workers to {i + 2} in /srv/api/c{i}.conf",
                           stages_completed=["clarify", "execute"],
                           status="completed"))

    report = LoopholeAudit(StructuralClarifier()).audit(records)

    assert report.anchored_but_goalless == 5
    assert not report.verdict.startswith("ACT")
    assert "keep measuring" in report.verdict


# --------------------------------------------------------------------------- #
# Strict reaper, cases 97 to 98
# --------------------------------------------------------------------------- #


def test_case_97_strict_reaper_requires_the_pid_suffix(tmp_path: Path) -> None:
    """Case 97: the Phase 4 finding. Naming accidents no longer decide."""
    old = time.time() - 90_000
    names = {
        "harness-shadow-4821": True,       # the harness made this
        "harness-shadow-backup-of-everything": False,
        "harness-shadowy-business": False,
        "harness-shadow-": False,
        "important-user-data": False,
    }
    for name in names:
        d = tmp_path / name
        d.mkdir()
        (d / "content.bin").write_bytes(b"x" * 256)
        os.utime(d, (old, old))

    reaper = StrictShadowReaper(roots=[tmp_path], max_age_seconds=3600,
                                min_age_seconds=1)
    report = reaper.reap()

    for name, should_be_removed in names.items():
        exists = (tmp_path / name).exists()
        assert exists is not should_be_removed, f"{name}: exists={exists}"

    assert report.removed == 1
    assert reaper.near_misses, "near misses must be reported, not silently ignored"
    assert any("harness-shadowy-business" in n or "backup-of-everything" in n
               for n in reaper.near_misses)


def test_case_98_strict_reaper_keeps_the_phase4_guarantees(tmp_path: Path) -> None:
    """Case 98: the min-age and dry-run safeguards survive the tightening."""
    live = tmp_path / "harness-shadow-1234"
    live.mkdir()
    (live / "x.bin").write_bytes(b"y" * 128)

    reaper = StrictShadowReaper(roots=[tmp_path], max_age_seconds=0.0,
                                min_age_seconds=300)
    assert reaper.reap().removed == 0
    assert live.exists(), "a young directory may be in use"

    os.utime(live, (time.time() - 90_000,) * 2)
    dry = StrictShadowReaper(roots=[tmp_path], max_age_seconds=3600,
                             min_age_seconds=1).reap(dry_run=True)
    assert dry.removed == 1
    assert live.exists(), "a dry run removes nothing"


# --------------------------------------------------------------------------- #
# Integration, cases 99 to 101
# --------------------------------------------------------------------------- #


def build_pipeline(tmp_path: Path, **kwargs) -> Phase5IntegratedExecutor:
    """Phase 5 pipeline with external dependencies stubbed to a known state."""
    defaults = dict(
        clarifier=StructuralClarifier(ask=lambda a: "staging"),
        safety=ParsingSafetyClassifier(confirm=lambda v: True),
        ollama=healthy(),
    )
    defaults.update(kwargs)
    return Phase5IntegratedExecutor(workdir=tmp_path / "run", **defaults)


def test_case_99_a_refused_task_is_still_recorded(tmp_path: Path) -> None:
    """Case 99: the task stopped at stage 1 is the one most worth recording."""
    ran: list[str] = []
    steps = [Step("go", command="echo x", description="go", estimated_seconds=20,
                  run=lambda: ran.append("go"))]

    pipe = build_pipeline(tmp_path, clarifier=StructuralClarifier())  # no answers
    result = pipe.run("refused", "tidy up the box a bit", steps)

    assert result.status is PipelineStatus.NEEDS_CLARIFICATION
    assert ran == []
    assert result.logged is True, "an early exit must still be logged"

    records = pipe.records()
    assert len(records) == 1
    assert records[0].clarifier_blocked is True
    assert records[0].clarifier_codes, "the reasons must be recorded"
    assert records[0].executed is False


def test_case_100_evidence_report_summarises_everything(tmp_path: Path) -> None:
    """Case 100: one command tells an operator what the harness knows."""
    pipe = build_pipeline(tmp_path)
    steps = [Step("s1", command="echo x", description="d", estimated_seconds=20,
                  run=lambda: "ok")]

    pipe.run("t1", "deploy commit a1b2c3d to staging", steps)
    pipe.run("t2", "fix /etc/nginx/nginx.conf", steps)
    pipe.labels.label("deploy commit a1b2c3d to staging", False)
    pipe.labels.label("fix /etc/nginx/nginx.conf", True, "no acceptance criterion")

    text = pipe.evidence_report()

    assert "tasks recorded      : 2" in text
    assert "LOOPHOLE AUDIT" in text
    assert "insufficient data" in text, "small samples must say so"
    assert "fix /etc/nginx/nginx.conf" in text

    exported = pipe.export_corpus()
    assert exported.exists()


def test_case_101_stage_contract_and_prior_phases_intact(tmp_path: Path) -> None:
    """Case 101: instrumentation changes nothing about how a task runs."""
    ran: list[str] = []
    steps = [
        Step("pull", kind="shell", command="git pull --ff-only",
             description="fetch", estimated_seconds=20, run=lambda: ran.append("pull")),
        Step("up", kind="deploy", command="docker compose up -d",
             description="start", estimated_seconds=60, run=lambda: ran.append("up")),
    ]

    pipe = build_pipeline(tmp_path)
    result = pipe.run("p5-happy", "deploy commit a1b2c3d to staging", steps)

    assert result.status is PipelineStatus.COMPLETED
    assert result.stages_completed == [s.value for s in Stage]
    assert ran == ["pull", "up"]
    assert result.model_choice.model == "devstral"

    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    assert payload["phase"] == 5
    assert payload["logged"] is True
    assert payload["telemetry_errors"] == []
    assert "clarifier_anchors" in payload      # phase 4 survives
    assert "undo_coverage" in payload          # phase 3 survives
    assert "policy_revisions" in payload       # phase 2 survives
    assert "stages" in payload                 # phase 1 survives
