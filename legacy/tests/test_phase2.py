"""
test_phase2.py
Acceptance cases for the five Phase 2 subsystems.

Every case is written as "the Phase 1 finding this closes", so a failure here
means a known defect came back, not that an abstract feature broke.

  P0-1  dynamic checkpoint policy      cases 19 to 22
  P0-2  continuous VERIFY_STATE        cases 23 to 26
  P1-1  incremental checkpoints        cases 27 to 30
  P1-2  parsing safety classifier      cases 31 to 34
  P1-3  filesystem conflict detection  cases 35 to 37
  integration                          cases 38 to 40
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_core import (  # noqa: E402
    ConflictKind,
    OllamaFallbackHandler,
    Step,
    TaskLength,
)
from harness_core_v2 import (  # noqa: E402
    DynamicCheckpointing,
    DynamicCoreExecutor,
    FilesystemConflictDetector,
    IncrementalCheckpointing,
)
from core_behaviors import (  # noqa: E402
    AskClarifyingQuestionsBehavior,
    Precondition,
    RiskLevel,
)
from core_behaviors_v2 import ContinuousVerifyState, ParsingSafetyClassifier  # noqa: E402
from phase2_integrated_executor import Phase2IntegratedExecutor  # noqa: E402
from phase1_integrated_executor import PipelineStatus, Stage  # noqa: E402


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


def slow_steps(n: int, declared: float | None = None) -> list[Step]:
    """Steps the estimator thinks are cheap. How slow they really are is
    decided by the injected timer, not by sleeping."""
    return [
        Step(step_id=f"s{i}", kind="shell", command=f"echo {i}",
             description=f"step {i}", estimated_seconds=declared,
             run=lambda: "ok")
        for i in range(1, n + 1)
    ]


def fake_timer(seconds_per_step: float) -> "callable":
    """A monotonic timer that advances `seconds_per_step` per measured step.

    The executor calls it twice per step (start, end), so advancing on the
    second call of each pair yields exactly the intended duration. This is how
    a two hour run gets tested in a millisecond.
    """
    state = {"now": 0.0, "calls": 0}

    def tick() -> float:
        state["calls"] += 1
        if state["calls"] % 2 == 0:
            state["now"] += seconds_per_step
        return state["now"]

    return tick


# --------------------------------------------------------------------------- #
# P0-1: dynamic checkpoint policy, cases 19 to 22
# --------------------------------------------------------------------------- #


def test_case_19_policy_upgrades_when_steps_run_slow(tmp_path: Path) -> None:
    """Case 19: the HIGH finding. A task that looks short but runs long gets
    checkpoints anyway, instead of zero."""
    cp = DynamicCheckpointing(tmp_path / "cp", min_observations=3)
    steps = slow_steps(12, declared=None)  # kind shell -> 10s each

    opening = cp.decide_frequency(steps)
    assert opening.length is TaskLength.SHORT
    assert opening.every_n_steps == 0, "Phase 1 behavior: zero checkpoints"

    # Now feed it observations that say each step takes 400 real seconds.
    for step in steps[:4]:
        cp.observe(step, 400.0)
    revised, revision = cp.revise(opening, completed_steps=4, total_steps=12)

    assert revision is not None, "policy must be revised once evidence contradicts it"
    assert revised.length is TaskLength.LONG
    assert revised.every_n_steps == 1
    assert revision.old_length is TaskLength.SHORT
    assert revision.projected_minutes == pytest.approx(80.0, rel=0.01)
    assert "not the" in revision.reason


def test_case_20_policy_never_downgrades_mid_run(tmp_path: Path) -> None:
    """Case 20: protection already earned is never taken away."""
    cp = DynamicCheckpointing(tmp_path / "cp", min_observations=2)
    steps = [Step(f"s{i}", kind="build", estimated_seconds=200.0, run=lambda: "ok")
             for i in range(1, 21)]

    opening = cp.decide_frequency(steps)
    assert opening.length is TaskLength.LONG

    for step in steps[:5]:
        cp.observe(step, 0.001)  # they turn out to be instant
    revised, revision = cp.revise(opening, completed_steps=5, total_steps=20)

    assert revision is None, "must not downgrade LONG to SHORT mid-run"
    assert revised.length is TaskLength.LONG
    assert revised.every_n_steps == 1


def test_case_21_no_revision_before_min_observations(tmp_path: Path) -> None:
    """Case 21: one slow step at the start does not upgrade the whole run."""
    cp = DynamicCheckpointing(tmp_path / "cp", min_observations=3)
    steps = slow_steps(20, declared=1.0)
    opening = cp.decide_frequency(steps)

    cp.observe(steps[0], 600.0)  # one very slow step
    revised, revision = cp.revise(opening, completed_steps=1, total_steps=20)
    assert revision is None, "must wait for min_observations before revising"

    cp.observe(steps[1], 0.01)
    cp.observe(steps[2], 0.01)
    revised, revision = cp.revise(opening, completed_steps=3, total_steps=20)
    assert revision is not None, "three observations is enough to act on"
    assert revised.length in (TaskLength.MEDIUM, TaskLength.LONG)


def test_case_22_executor_applies_revision_and_checkpoints(tmp_path: Path) -> None:
    """Case 22: end to end. Slow steps produce checkpoints a Phase 1 run
    would never have written."""
    cp = DynamicCheckpointing(tmp_path / "cp", min_observations=2)
    steps = slow_steps(8, declared=None)  # estimator says 10s each, 1.3 min total

    # Reality: 500 seconds per step, so the real task is over an hour.
    ex = DynamicCoreExecutor(cp, ollama=healthy(), timer=fake_timer(500.0))
    report = ex.execute("dyn-1", steps)

    assert report.ok, report.abort_reason
    assert ex.policy_revisions, "a run this slow must trigger a revision"
    assert cp.intermediate_writes > 0, (
        "Phase 1 wrote zero here, which was the finding"
    )
    assert report.policy.length is not TaskLength.SHORT
    assert report.checkpoint_stats["revisions"], "revisions must reach the report"

    saved = cp.load_state("dyn-1")
    assert saved is not None and saved.completed_step_ids == [s.step_id for s in steps]
    # The learned cost table now reflects reality, not the static guess.
    assert cp.step_costs["shell"] > 10.0, cp.step_costs


# --------------------------------------------------------------------------- #
# P0-2: continuous VERIFY_STATE, cases 23 to 26
# --------------------------------------------------------------------------- #


def test_case_23_precondition_flip_aborts_mid_run(tmp_path: Path) -> None:
    """Case 23: the other HIGH finding. A service that dies at step 2 stops
    the run instead of being ignored until the end."""
    state = {"up": True}
    ran: list[str] = []

    verify = ContinuousVerifyState()
    verify.add(Precondition("service", lambda: state["up"], "must stay up"),
               ttl_seconds=0)

    def kill(_: str) -> str:
        state["up"] = False
        return "service died"

    steps = [
        Step("s1", command="echo a", estimated_seconds=180.0,
             run=lambda: ran.append("s1") or kill("s1")),
        Step("s2", command="echo b", estimated_seconds=180.0,
             run=lambda: ran.append("s2")),
        Step("s3", command="echo c", estimated_seconds=180.0,
             run=lambda: ran.append("s3")),
    ]

    assert verify.run_all().passed is True

    cp = IncrementalCheckpointing(tmp_path / "cp")
    ex = DynamicCoreExecutor(cp, ollama=healthy(),
                             between_steps=verify.between_steps)
    report = ex.execute("flip-1", steps)

    assert report.ok is False
    assert ran == ["s1"], "s2 and s3 must not run after the precondition flipped"
    assert report.aborted_at == "s2"
    assert "precondition went false mid-run" in report.abort_reason
    assert ex.mid_run_abort is not None
    assert ex.mid_run_abort.guard == "between_steps"
    assert cp.load_state("flip-1") is not None, "must stay resumable after abort"


def test_case_24_ttl_cache_prevents_probe_storms() -> None:
    """Case 24: an expensive probe is not re-run before every step."""
    clock = {"now": 0.0}
    calls = {"n": 0}

    def expensive() -> bool:
        calls["n"] += 1
        return True

    verify = ContinuousVerifyState(clock=lambda: clock["now"])
    verify.add(Precondition("expensive", expensive, "slow probe"), ttl_seconds=30.0)

    verify.run_all()          # forced, 1 call
    assert calls["n"] == 1

    for _ in range(20):       # 20 between-step guards inside the TTL
        verify.recheck()
    assert calls["n"] == 1, "TTL cache must absorb these"
    assert verify.cache_hits == 20

    clock["now"] = 31.0
    verify.recheck()
    assert calls["n"] == 2, "the probe must re-run once the TTL expires"


def test_case_25_zero_ttl_always_reruns_and_records_flips() -> None:
    """Case 25: ttl_seconds=0 means check every time, and transitions are logged."""
    state = {"up": True}
    verify = ContinuousVerifyState()
    verify.add(Precondition("svc", lambda: state["up"], "svc"), ttl_seconds=0)

    verify.run_all()
    assert verify.recheck().passed is True
    state["up"] = False
    report = verify.recheck()

    assert report.passed is False
    assert verify.flips, "a pass to fail transition must be recorded"
    assert verify.flips[-1][1] == "svc"
    assert verify.flips[-1][2] == "pass -> fail"
    assert verify.guard() is not None and "svc" in verify.guard()


def test_case_26_negative_ttl_reproduces_phase1_behavior() -> None:
    """Case 26: opting out per precondition still works, for genuinely static
    checks like 'this binary exists'."""
    calls = {"n": 0}

    def once() -> bool:
        calls["n"] += 1
        return True

    verify = ContinuousVerifyState()
    verify.add(Precondition("binary", once, "static"), ttl_seconds=-1)

    verify.run_all()
    for _ in range(10):
        verify.recheck()
    assert calls["n"] == 1, "a negative TTL means check once, never again"


# --------------------------------------------------------------------------- #
# P1-1: incremental checkpoints, cases 27 to 30
# --------------------------------------------------------------------------- #


def test_case_27_deltas_replace_full_rewrites(tmp_path: Path) -> None:
    """Case 27: intermediate checkpoints are small delta lines, not full dumps."""
    cp = IncrementalCheckpointing(tmp_path / "cp", compact_every=50)
    steps = [Step(f"s{i}", kind="build", description="d" * 400,
                  estimated_seconds=200.0, run=lambda: "y" * 4096)
             for i in range(1, 41)]

    ex = DynamicCoreExecutor(cp, ollama=healthy())
    report = ex.execute("inc-1", steps)

    assert report.ok, report.abort_reason
    assert cp.delta_path_for("inc-1").exists() is False, (
        "the final checkpoint compacts the delta log away"
    )
    assert cp.load_state("inc-1") is not None


def test_case_28_incremental_writes_far_fewer_bytes(tmp_path: Path) -> None:
    """Case 28: the MED finding. Byte growth is no longer superlinear."""
    def bytes_for(cls, n: int, tag: str) -> int:
        cp = cls(tmp_path / f"{tag}{n}")
        steps = [Step(f"s{i}", kind="build", description="d" * 400,
                      estimated_seconds=200.0, run=lambda: "y" * 4096)
                 for i in range(1, n + 1)]
        DynamicCoreExecutor(cp, ollama=healthy()).execute(f"{tag}-{n}", steps)
        return cp.stats()["bytes_written"]

    small_v2 = bytes_for(IncrementalCheckpointing, 25, "v2")
    large_v2 = bytes_for(IncrementalCheckpointing, 200, "v2")
    small_v1 = bytes_for(DynamicCheckpointing, 25, "v1")
    large_v1 = bytes_for(DynamicCheckpointing, 200, "v1")

    growth_v2 = large_v2 / small_v2
    growth_v1 = large_v1 / small_v1

    assert large_v2 < large_v1 / 4, (
        f"incremental wrote {large_v2/1024:.0f} KB vs full-rewrite "
        f"{large_v1/1024:.0f} KB, expected at least a 4x saving"
    )
    assert growth_v2 < growth_v1, (
        f"growth curve did not flatten: v2 {growth_v2:.1f}x vs v1 {growth_v1:.1f}x"
    )
    assert growth_v2 < 16.0, f"8x more steps should not cost {growth_v2:.1f}x bytes"


def test_case_29_replay_reconstructs_state(tmp_path: Path) -> None:
    """Case 29: base plus deltas replays to the same state a full dump would."""
    cp = IncrementalCheckpointing(tmp_path / "cp", compact_every=100)
    def body(i: int) -> str:
        if i == 30:
            raise RuntimeError("crash")
        return "ok"

    steps = [Step(f"s{i}", kind="build", estimated_seconds=200.0,
                  run=lambda i=i: body(i))
             for i in range(1, 41)]

    ex = DynamicCoreExecutor(cp, ollama=healthy())
    report = ex.execute("replay-1", steps)
    assert report.ok is False

    restored = cp.load_state("replay-1")
    assert restored is not None
    assert restored.completed_step_ids == [f"s{i}" for i in range(1, 30)]
    assert restored.step_index == 30


def test_case_30_corrupt_delta_stops_replay_loudly(tmp_path: Path) -> None:
    """Case 30: a torn delta line does not silently resume past the hole."""
    cp = IncrementalCheckpointing(tmp_path / "cp", compact_every=100)
    steps = [Step(f"s{i}", kind="build", estimated_seconds=200.0, run=lambda: "ok")
             for i in range(1, 11)]
    ex = DynamicCoreExecutor(cp, ollama=healthy())
    ex.execute("torn-1", steps[:5])  # leaves deltas plus a final base

    # Rebuild a delta log by hand and tear the middle of it.
    delta_log = cp.delta_path_for("torn-1")
    delta_log.write_text(
        json.dumps({"step_index": 6, "completed_step_ids": ["s1", "s2", "s3"]}) + "\n"
        + '{"step_index": 7, "completed_step_ids": ["s1"\n'
        + json.dumps({"step_index": 8, "completed_step_ids": ["s1"] * 8}) + "\n",
        encoding="utf-8",
    )

    restored = cp.load_state("torn-1")
    assert restored is not None
    assert restored.step_index == 6, "replay must stop at the torn line, not skip it"
    assert cp.truncated_replays, "the truncation must be reported, not swallowed"
    assert "corrupt" in cp.truncated_replays[0]


# --------------------------------------------------------------------------- #
# P1-2: parsing safety classifier, cases 31 to 34
# --------------------------------------------------------------------------- #


def test_case_31_resolves_wrappers_and_indirection() -> None:
    """Case 31: the classifier sees through wrappers regex could not."""
    safety = ParsingSafetyClassifier()
    evasions = [
        "rm -fr /var/lib/x",
        "rm  -r  -f  /var/lib/x",
        "/bin/rm -rf /var/lib/x",
        "R=rm; $R -rf /var/lib/x",
        "sudo -- sh -c 'rm -rf /var/lib/x'",
        "timeout 30 rm -rf /srv/data",
        "nohup sudo mkfs.ext4 /dev/nvme0n1p3 &",
        "find /var -delete",
        "git checkout -- .",
        "> /etc/fstab",
        "SYSTEMCTL=1 systemctl stop ollama",
        "env FOO=1 sudo rm -rf /opt/app",
    ]
    for command in evasions:
        verdict = safety.classify(command)
        assert verdict.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL), (
            f"{command!r} classified {verdict.level.value}: {verdict.reasons}"
        )
        assert verdict.requires_confirmation, command


def test_case_32_decodes_obfuscated_payloads() -> None:
    """Case 32: a base64 blob piped into a shell is decoded and classified."""
    import base64

    payload = "rm -rf /var/lib/postgresql"
    blob = base64.b64encode(payload.encode()).decode()
    command = f"echo '{blob}' | base64 -d | sh"

    safety = ParsingSafetyClassifier()
    verdict = safety.classify(command)

    assert verdict.level is RiskLevel.CRITICAL, verdict.reasons
    assert any("base64 payload" in r for r in verdict.reasons), verdict.reasons

    trace = safety.explain(command)
    assert "rm -rf /var/lib/postgresql" in trace


def test_case_33_recurses_into_interpreter_payloads() -> None:
    """Case 33: sh -c and python3 -c payloads are examined, not trusted."""
    safety = ParsingSafetyClassifier()

    nested = safety.classify("sh -c \"sh -c 'rm -rf /srv'\"")
    assert nested.level is RiskLevel.CRITICAL, nested.reasons

    py = safety.classify("python3 -c \"import shutil;shutil.rmtree('/var/lib/x')\"")
    assert py.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL), py.reasons
    assert any("source performs filesystem" in r for r in py.reasons), py.reasons


def test_case_34_no_false_positives_on_safe_commands() -> None:
    """Case 34: the stricter classifier must not start blocking normal work."""
    safety = ParsingSafetyClassifier()
    safe = [
        "ls -la /etc", "cat /proc/meminfo", "df -h", "systemctl status ollama",
        "git pull --ff-only", "git log --oneline -20", "echo hello",
        "grep -r TODO src/", "python3 -m pytest tests/", "free -h",
        "journalctl -u ollama -n 50", "docker ps",
    ]
    for command in safe:
        verdict = safety.classify(command)
        assert not verdict.requires_confirmation, (
            f"{command!r} wrongly needs confirmation: {verdict.reasons}"
        )
    assert safety.parse_failures == [], safety.parse_failures


# --------------------------------------------------------------------------- #
# P1-3: filesystem conflict detection, cases 35 to 37
# --------------------------------------------------------------------------- #


def test_case_35_detects_undeclared_file_changes(tmp_path: Path) -> None:
    """Case 35: the MED finding. Files no step declared are still watched."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    declared = root / "declared.conf"
    undeclared = root / "sub" / "forgotten.conf"
    declared.write_text("a", encoding="utf-8")
    undeclared.write_text("b", encoding="utf-8")

    det = FilesystemConflictDetector(use_inotify=False)
    det.baseline_files([str(declared)])
    det.baseline_tree([root])
    assert det.detect() == []

    undeclared.write_text("EDITED", encoding="utf-8")
    conflicts = det.detect()

    paths = {c.path for c in conflicts}
    assert str(undeclared) in paths, "Phase 1 was blind to this exact case"
    assert any(c.kind is ConflictKind.MODIFIED for c in conflicts)
    det.close()


def test_case_36_detects_creation_and_deletion_anywhere(tmp_path: Path) -> None:
    """Case 36: new and removed files in the tree are both caught."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "keep.txt").write_text("k", encoding="utf-8")
    (root / "doomed.txt").write_text("d", encoding="utf-8")

    det = FilesystemConflictDetector(use_inotify=False)
    det.baseline_tree([root])

    (root / "surprise.txt").write_text("new", encoding="utf-8")
    (root / "doomed.txt").unlink()
    conflicts = det.detect_tree()

    kinds = {(Path(c.path).name, c.kind) for c in conflicts}
    assert ("surprise.txt", ConflictKind.CREATED) in kinds
    assert ("doomed.txt", ConflictKind.DELETED) in kinds
    det.close()


def test_case_37_ignores_noise_directories_and_caps_size(tmp_path: Path) -> None:
    """Case 37: build noise is skipped and an oversized tree fails loudly."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "__pycache__").mkdir(parents=True)
    (root / "src").mkdir(parents=True)
    (root / ".git" / "index").write_text("git", encoding="utf-8")
    (root / "__pycache__" / "x.pyc").write_text("pyc", encoding="utf-8")
    (root / "src" / "main.py").write_text("code", encoding="utf-8")

    det = FilesystemConflictDetector(use_inotify=False)
    snaps = det.baseline_tree([root])
    watched = set(next(iter(snaps.values())).paths())

    assert any("src/main.py" in p for p in watched)
    assert not any(".git" in p for p in watched), "git internals must be ignored"
    assert not any("__pycache__" in p for p in watched)

    # Churn inside an ignored directory must not raise a conflict.
    (root / ".git" / "index").write_text("changed", encoding="utf-8")
    assert det.detect_tree() == []

    for n in range(6):
        (root / "src" / f"extra{n}.py").write_text("x", encoding="utf-8")
    capped = FilesystemConflictDetector(max_files=2, use_inotify=False)
    capped.baseline_tree([root])
    assert capped.oversized_roots, "exceeding max_files must be reported"
    assert capped.errors, "and must land in errors, not be swallowed"
    det.close()
    capped.close()


# --------------------------------------------------------------------------- #
# Integration, cases 38 to 40
# --------------------------------------------------------------------------- #


def build_pipeline(tmp_path: Path, **kwargs) -> Phase2IntegratedExecutor:
    """Phase 2 pipeline with external dependencies stubbed to a known state."""
    defaults = dict(
        clarifier=AskClarifyingQuestionsBehavior(ask=lambda a: "staging"),
        safety=ParsingSafetyClassifier(confirm=lambda v: True),
        ollama=healthy(),
    )
    defaults.update(kwargs)
    return Phase2IntegratedExecutor(workdir=tmp_path / "run", **defaults)


def test_case_38_phase2_happy_path_keeps_the_stage_contract(tmp_path: Path) -> None:
    """Case 38: Phase 2 does not change the 8 stage contract Phase 1 defined."""
    ran: list[str] = []
    steps = [
        Step("pull", kind="shell", command="git pull --ff-only",
             description="fetch", estimated_seconds=20, run=lambda: ran.append("pull")),
        Step("build", kind="build", command="docker compose build",
             description="build", estimated_seconds=200, run=lambda: ran.append("build")),
        Step("up", kind="deploy", command="docker compose up -d",
             description="start", estimated_seconds=60, run=lambda: ran.append("up")),
    ]

    pipe = build_pipeline(tmp_path)
    result = pipe.run("p2-happy", "deploy the api stack to staging", steps)

    assert result.status is PipelineStatus.COMPLETED, result.stages_completed
    assert result.stages_completed == [s.value for s in Stage]
    assert ran == ["pull", "build", "up"]
    assert result.model_choice.model == "devstral"
    assert result.resolved_commands, "resolved argv must be in the audit"
    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    assert payload["phase"] == 2
    assert payload["resolved_commands"]


def test_case_39_mid_run_precondition_failure_lands_in_the_audit(tmp_path: Path) -> None:
    """Case 39: probe 12's exact scenario now fails the run and says why."""
    state = {"up": True}
    verify = ContinuousVerifyState()
    verify.add(Precondition("service", lambda: state["up"], "must stay up"),
               ttl_seconds=0)

    ran: list[str] = []
    steps = [
        Step("s1", command="echo a", description="a", estimated_seconds=180.0,
             run=lambda: (ran.append("s1"), state.update(up=False))),
        Step("s2", command="echo b", description="b", estimated_seconds=180.0,
             run=lambda: ran.append("s2")),
    ]

    pipe = build_pipeline(tmp_path, verifier=verify)
    result = pipe.run("p2-flip", "restart ollama.service on popos-01 and verify", steps)

    assert result.status is PipelineStatus.EXECUTION_FAILED, (
        "Phase 1 reported COMPLETED here, which was the finding"
    )
    assert ran == ["s1"]
    assert result.mid_run_abort is not None
    assert result.mid_run_abort["guard"] == "between_steps"
    assert result.precondition_stats["flips"], "the flip must be recorded"

    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    stages = {d["stage"] for d in payload["decisions"]}
    assert "mid_run_guard" in stages, "the audit must explain the mid-run abort"


def test_case_40_policy_revision_reaches_the_audit(tmp_path: Path) -> None:
    """Case 40: a mid-run policy upgrade is logged with its reasoning."""
    cp = IncrementalCheckpointing(tmp_path / "cp", min_observations=2)
    steps = [
        Step(f"s{i}", kind="shell", command="echo x", description=f"step {i}",
             estimated_seconds=None, run=lambda: "ok")
        for i in range(1, 9)
    ]

    pipe = build_pipeline(tmp_path, checkpointer=cp, step_timer=fake_timer(500.0))
    result = pipe.run("p2-revise", "deploy the api stack to staging", steps)

    assert result.status is PipelineStatus.COMPLETED
    assert result.policy_revisions, "a slow run must revise its policy"

    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    revisions = [d for d in payload["decisions"] if d["stage"] == "policy_revision"]
    assert revisions, "the revision must be in the audit trail"
    assert revisions[0]["reasoning"].strip()
    assert "projects to" in revisions[0]["reasoning"]
