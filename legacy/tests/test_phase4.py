"""
test_phase4.py
Acceptance cases for the three Phase 4 subsystems.

  P0-1  structural clarifier         cases 62 to 68
  P1-1  reflink shadow snapshots     cases 69 to 71
  P1-2  shadow reaper                cases 72 to 74
  P1-3  event driven conflicts       cases 75 to 78
  integration                        cases 79 to 81
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_core import ConflictKind, OllamaFallbackHandler, Step  # noqa: E402
from harness_core_v4 import EventDrivenConflictDetector  # noqa: E402
from core_behaviors import AskClarifyingQuestionsBehavior  # noqa: E402
from core_behaviors_v2 import ParsingSafetyClassifier  # noqa: E402
from core_behaviors_v4 import (  # noqa: E402
    ReflinkUndoGenerator,
    ShadowReaper,
    StructuralClarifier,
)
from clarifier_corpus import (  # noqa: E402
    AMBIGUOUS,
    BLIND_AMBIGUOUS,
    BLIND_SPECIFIC,
    HOLDOUT_AMBIGUOUS,
    HOLDOUT_SPECIFIC,
    SPECIFIC,
    evaluate,
)
from phase1_integrated_executor import PipelineStatus, Stage  # noqa: E402
from phase4_integrated_executor import Phase4IntegratedExecutor  # noqa: E402


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


# --------------------------------------------------------------------------- #
# P0-1: structural clarifier, cases 62 to 68
# --------------------------------------------------------------------------- #


def test_case_62_clarifier_beats_phase1_on_the_tuning_corpus() -> None:
    """Case 62: the finding. Measured recall on the corpus it was built against."""
    v1 = evaluate(AskClarifyingQuestionsBehavior())
    v4 = evaluate(StructuralClarifier())

    assert v1["fn_rate"] >= 0.30, (
        f"the Phase 1 baseline should be poor here; measured {v1['fn_rate']:.0%}"
    )
    assert v4["fn_rate"] == 0.0, v4["false_negatives"]
    assert v4["fp_rate"] == 0.0, v4["false_positives"]
    assert v4["f1"] > v1["f1"]


def test_case_63_clarifier_generalises_to_a_blind_corpus() -> None:
    """Case 63: the number that actually matters.

    Scoring a detector on the corpus it was tuned against measures memorisation.
    BLIND_AMBIGUOUS and BLIND_SPECIFIC were written after the clarifier stopped
    changing and were never used to adjust it.
    """
    v1 = evaluate(AskClarifyingQuestionsBehavior(), BLIND_AMBIGUOUS, BLIND_SPECIFIC)
    v4 = evaluate(StructuralClarifier(), BLIND_AMBIGUOUS, BLIND_SPECIFIC)

    assert v1["fn_rate"] > 0.60, (
        f"Phase 1 baseline on blind data: {v1['fn_rate']:.0%}"
    )
    assert v4["fn_rate"] == 0.0, (
        f"blind false negatives: {v4['false_negatives']}"
    )
    assert v4["fp_rate"] <= 0.15, (
        f"blind false positives: {v4['false_positives']}"
    )
    assert v4["f1"] >= 0.90


def test_case_64_holdout_generalisation_holds() -> None:
    """Case 64: the intermediate holdout, used once to diagnose, still clean."""
    v4 = evaluate(StructuralClarifier(), HOLDOUT_AMBIGUOUS, HOLDOUT_SPECIFIC)
    assert v4["fn_rate"] == 0.0, v4["false_negatives"]
    assert v4["fp_rate"] == 0.0, v4["false_positives"]


def test_case_65_verb_agnostic_detection() -> None:
    """Case 65: invented jargon cannot slip past a closed verb list any more.

    This is the structural defect the holdout exposed: Phase 4's first draft
    swapped a closed list of vague phrasings for a closed list of mutating
    verbs, which missed 'quiesce', 'drain', 'throttle', and six others.
    """
    clar = StructuralClarifier()
    invented = [
        "quiesce the writes", "drain the queue", "throttle the neighbour",
        "consolidate the dirs", "reticulate the splines", "frobnicate the widget",
        "unwedge the thing", "defenestrate the daemon",
    ]
    for request in invented:
        assert clar.detect(request), f"{request!r} was waved through"
        codes = {a.code for a in clar.detect(request)}
        assert "no_target" in codes, f"{request!r} -> {codes}"


def test_case_66_anchors_make_a_casual_request_executable() -> None:
    """Case 66: phrasing does not matter, structure does."""
    clar = StructuralClarifier()

    assert clar.detect("nuke /tmp/build-cache-2024") == [], (
        "a casually phrased request with a real path is executable"
    )
    assert clar.detect("yeet the /srv/api/old.log file") == []
    assert clar.detect("please could you possibly restart the server") != [], (
        "polite phrasing does not add a target"
    )


def test_case_67_destructive_requests_need_a_target_or_a_filter() -> None:
    """Case 67: recoverability is asked about only when scope is unclear."""
    clar = StructuralClarifier()

    assert any(a.code == "no_backup_plan" for a in clar.detect("wipe the staging data"))
    assert any(a.code == "no_backup_plan" for a in clar.detect("delete all the logs"))

    # An explicit filter or a real path means there is nothing left to ask.
    assert clar.detect("remove docker images older than 30 days") == []
    assert clar.detect("remove /tmp/build-cache-2024 and nothing else") == []

    strict = StructuralClarifier(strict_destructive=True)
    assert any(a.code == "no_backup_plan"
               for a in strict.detect("remove /tmp/build-cache-2024 and nothing else"))


def test_case_68_extra_anchors_are_configurable() -> None:
    """Case 68: a site can teach it an internal naming scheme."""
    plain = StructuralClarifier()
    assert plain.detect("restart the frobnitz gateway"), "unknown scheme, must ask"

    taught = StructuralClarifier(
        extra_anchors=[(r"\bfrobnitz\s+\w+\b", "internal frobnitz identifier")])
    assert "internal frobnitz identifier" in taught.anchors_in("restart the frobnitz gateway")
    assert taught.detect("restart the frobnitz gateway") == []

    trace = plain.explain("restart the frobnitz gateway")
    assert "anchors: NONE" in trace and "BLOCKED" in trace


# --------------------------------------------------------------------------- #
# P1-1: reflink shadow snapshots, cases 69 to 71
# --------------------------------------------------------------------------- #


def test_case_69_reflink_generator_still_restores_correctly(tmp_path: Path) -> None:
    """Case 69: whichever copy mode is used, the restore must be exact."""
    target = tmp_path / "app.conf"
    target.write_text("original contents\n", encoding="utf-8")
    os.chmod(target, 0o640)

    gen = ReflinkUndoGenerator(shadow_dir=tmp_path / "shadow")
    action = gen.capture_file(target)
    assert action is not None

    target.write_text("CLOBBERED\n", encoding="utf-8")
    os.chmod(target, 0o777)
    action.undo()

    assert target.read_text(encoding="utf-8") == "original contents\n"
    assert target.stat().st_mode & 0o7777 == 0o640
    assert action.verify() is True

    stats = gen.stats()
    assert stats["snapshots"] == 1
    assert stats["reflink_copies"] + stats["byte_copies"] == 1
    assert stats["reflink_supported"] in (True, False)


def test_case_70_falls_back_cleanly_without_reflink(tmp_path: Path) -> None:
    """Case 70: an unsupported filesystem degrades to byte copies and says so."""
    target = tmp_path / "a.conf"
    target.write_text("data", encoding="utf-8")

    gen = ReflinkUndoGenerator(shadow_dir=tmp_path / "shadow", prefer_reflink=False)
    action = gen.capture_file(target)

    assert action is not None
    assert gen.stats()["reflink_supported"] is False
    assert gen.stats()["byte_copies"] == 1
    target.write_text("changed", encoding="utf-8")
    action.undo()
    assert target.read_text(encoding="utf-8") == "data"


def test_case_71_oversized_files_are_still_refused(tmp_path: Path) -> None:
    """Case 71: the Phase 3 honesty guarantee survives the reflink path."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 5000)

    gen = ReflinkUndoGenerator(shadow_dir=tmp_path / "shadow", max_shadow_bytes=100)
    assert gen.capture_file(big) is None
    assert any("over max_shadow_bytes" in s for s in gen.skipped)

    # A file that does not exist yet still gets a delete-on-undo action.
    absent = tmp_path / "not_yet.conf"
    action = gen.capture_file(absent)
    assert action is not None
    absent.write_text("created by the step", encoding="utf-8")
    action.undo()
    assert not absent.exists()


# --------------------------------------------------------------------------- #
# P1-2: shadow reaper, cases 72 to 74
# --------------------------------------------------------------------------- #


def test_case_72_reaper_removes_aged_out_shadows(tmp_path: Path) -> None:
    """Case 72: the finding. A crashed run's shadows do not leak forever."""
    old = tmp_path / "harness-shadow-1111"
    old.mkdir()
    (old / "snap.bin").write_bytes(b"y" * 4096)
    os.utime(old, (time.time() - 90_000, time.time() - 90_000))

    reaper = ShadowReaper(roots=[tmp_path], max_age_seconds=24 * 3600,
                          min_age_seconds=300)
    report = reaper.reap()

    assert report.removed == 1
    assert report.bytes_freed >= 4096
    assert not old.exists()
    assert report.errors == []


def test_case_73_reaper_never_touches_a_live_run(tmp_path: Path) -> None:
    """Case 73: a shadow directory in use must not be reaped out from under it."""
    live = tmp_path / "harness-shadow-2222"
    live.mkdir()
    (live / "snap.bin").write_bytes(b"z" * 1024)  # mtime is now

    reaper = ShadowReaper(roots=[tmp_path], max_age_seconds=0.0,
                          min_age_seconds=300)
    report = reaper.reap()

    assert live.exists(), "a directory younger than min_age must survive"
    assert report.removed == 0
    assert report.kept_live == 1


def test_case_74_reaper_only_touches_its_own_prefix(tmp_path: Path) -> None:
    """Case 74: it cannot wander into real data."""
    theirs = tmp_path / "important-user-data"
    theirs.mkdir()
    (theirs / "irreplaceable.db").write_bytes(b"q" * 2048)
    os.utime(theirs, (time.time() - 90_000, time.time() - 90_000))

    mine = tmp_path / "harness-shadow-3333"
    mine.mkdir()
    os.utime(mine, (time.time() - 90_000, time.time() - 90_000))

    reaper = ShadowReaper(roots=[tmp_path], max_age_seconds=3600, min_age_seconds=1)
    dry = reaper.reap(dry_run=True)
    assert dry.removed == 1
    assert mine.exists() and theirs.exists(), "dry run must remove nothing"

    reaper.reap()
    assert theirs.exists(), "a non-prefixed directory must never be touched"
    assert (theirs / "irreplaceable.db").exists()
    assert not mine.exists()


# --------------------------------------------------------------------------- #
# P1-3: event driven conflicts, cases 75 to 78
# --------------------------------------------------------------------------- #


def test_case_75_fast_path_detects_a_modification(tmp_path: Path) -> None:
    """Case 75: an edit is caught without walking the whole tree."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    target = root / "sub" / "app.conf"
    target.write_text("original", encoding="utf-8")

    det = EventDrivenConflictDetector(reconcile_every=50)
    det.baseline_tree([root])
    det.baseline_files([str(target)])
    if not det.use_inotify:
        pytest.skip("inotify unavailable in this environment")

    assert det.detect_tree() == []
    target.write_text("EDITED EXTERNALLY", encoding="utf-8")
    time.sleep(0.05)

    conflicts = det.detect_tree()
    assert any(c.path == str(target) and c.kind is ConflictKind.MODIFIED
               for c in conflicts), conflicts
    assert det.stats.fast_checks >= 1
    assert det.stats.full_reconciles <= 1
    det.close()


def test_case_76_reconcile_runs_on_schedule(tmp_path: Path) -> None:
    """Case 76: the fast path is checked against reality on a schedule."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.conf").write_text("a", encoding="utf-8")

    det = EventDrivenConflictDetector(reconcile_every=3)
    det.baseline_tree([root])
    # Every 4th check is a reconcile: three fast, then a walk that resets the
    # counter. Ten checks therefore buy exactly two reconciles.
    for _ in range(10):
        det.detect_tree()

    stats = det.stats_dict()
    assert stats["full_reconciles"] >= 2, stats
    assert stats["fast_checks"] >= 6, stats
    assert 0.0 < stats["fast_path_ratio"] < 1.0
    det.close()


def test_case_77_reconcile_catches_what_events_missed(tmp_path: Path) -> None:
    """Case 77: a change inside an unwatched subdirectory is still found.

    A directory created after the watches were attached is not watched, which is
    one of inotify's three silent failure modes. The scheduled full walk is what
    makes that survivable, and the disagreement is counted rather than hidden.
    """
    root = tmp_path / "tree"
    root.mkdir()
    (root / "known.conf").write_text("k", encoding="utf-8")

    det = EventDrivenConflictDetector(reconcile_every=2)
    det.baseline_tree([root])
    det.detect_tree()

    # A whole subtree that did not exist when watches were attached.
    late = root / "created_after_watch"
    late.mkdir()
    (late / "sneaky.conf").write_text("appeared later", encoding="utf-8")

    found = det.force_reconcile()
    paths = {c.path for c in found}
    assert str(late / "sneaky.conf") in paths, (
        "the scheduled walk must find what events could not report"
    )
    assert det.stats.full_reconciles >= 1
    det.close()


def test_case_78_degrades_to_walking_without_inotify(tmp_path: Path) -> None:
    """Case 78: no inotify means every check is a full walk, and it says so."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.conf").write_text("a", encoding="utf-8")

    det = EventDrivenConflictDetector()
    det.use_inotify = False
    det.baseline_tree([root])
    assert any("full walk" in f for f in det.stats.fallbacks)

    (root / "a.conf").write_text("changed", encoding="utf-8")
    conflicts = det.detect_tree()

    assert any(c.kind is ConflictKind.MODIFIED for c in conflicts)
    assert det.stats.fast_checks == 0, "no events means no fast path"
    assert det.stats.full_reconciles >= 1
    det.close()


# --------------------------------------------------------------------------- #
# Integration, cases 79 to 81
# --------------------------------------------------------------------------- #


def build_pipeline(tmp_path: Path, **kwargs) -> Phase4IntegratedExecutor:
    """Phase 4 pipeline with external dependencies stubbed to a known state."""
    defaults = dict(
        clarifier=StructuralClarifier(ask=lambda a: "staging"),
        safety=ParsingSafetyClassifier(confirm=lambda v: True),
        ollama=healthy(),
    )
    defaults.update(kwargs)
    return Phase4IntegratedExecutor(workdir=tmp_path / "run", **defaults)


def test_case_79_vague_task_is_stopped_at_stage_one(tmp_path: Path) -> None:
    """Case 79: an unanswerable vague task never reaches execution."""
    ran: list[str] = []
    steps = [Step("go", command="echo x", description="go", estimated_seconds=20,
                  run=lambda: ran.append("go"))]

    pipe = build_pipeline(tmp_path, clarifier=StructuralClarifier())  # no answers
    result = pipe.run("p4-vague", "tidy up the box a bit", steps)

    assert result.status is PipelineStatus.NEEDS_CLARIFICATION
    assert result.stopped_at_stage_number() == 1
    assert ran == [], "nothing may execute on an unclarified task"
    assert result.clarification.ambiguities
    assert result.clarifier_anchors == [], "there was nothing concrete to anchor on"


def test_case_80_happy_path_keeps_the_stage_contract(tmp_path: Path) -> None:
    """Case 80: Phase 4 does not change the 8 stage contract."""
    ran: list[str] = []
    steps = [
        Step("pull", kind="shell", command="git pull --ff-only",
             description="fetch", estimated_seconds=20, run=lambda: ran.append("pull")),
        Step("up", kind="deploy", command="docker compose up -d",
             description="start", estimated_seconds=60, run=lambda: ran.append("up")),
    ]

    pipe = build_pipeline(tmp_path)
    result = pipe.run("p4-happy", "deploy commit a1b2c3d to staging", steps)

    assert result.status is PipelineStatus.COMPLETED, result.stages_completed
    assert result.stages_completed == [s.value for s in Stage]
    assert ran == ["pull", "up"]
    assert result.clarifier_anchors, "a specific task must show its anchors"

    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    assert payload["phase"] == 4
    assert "shadow_stats" in payload and "conflict_stats" in payload
    assert payload["clarifier_anchors"]


def test_case_81_reap_and_shadow_telemetry_reach_the_audit(tmp_path: Path) -> None:
    """Case 81: the operator can see reaping, copy mode, and event ratios."""
    workdir = tmp_path / "run"
    workdir.mkdir(parents=True)
    orphan = workdir / "harness-shadow-9999"
    orphan.mkdir()
    (orphan / "old.bin").write_bytes(b"x" * 2048)
    os.utime(orphan, (time.time() - 90_000, time.time() - 90_000))

    conf = tmp_path / "app.conf"
    conf.write_text("v1", encoding="utf-8")
    steps = [Step("edit", kind="write", command="echo x", description="edit",
                  estimated_seconds=20, touches=[str(conf)], run=lambda: "ok")]

    pipe = build_pipeline(tmp_path, reaper=ShadowReaper(
        roots=[workdir], max_age_seconds=3600, min_age_seconds=60))
    result = pipe.run("p4-telemetry", "deploy commit a1b2c3d to staging", steps)

    assert result.status is PipelineStatus.COMPLETED
    assert not orphan.exists(), "the orphaned shadow must have been reaped"
    assert result.reap_report["removed"] == 1
    assert result.reap_report["bytes_freed"] >= 2048
    assert result.shadow_stats["snapshots"] == 1
    assert result.shadow_stats["reflink_supported"] in (True, False)

    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    stages = {d["stage"] for d in payload["decisions"]}
    assert "shadow_reap" in stages
    assert "phase4_telemetry" in stages
