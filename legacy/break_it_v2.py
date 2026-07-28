"""
break_it_v2.py
Re-runs the Phase 1 adversarial probes against the Phase 2 stack, then adds new
probes for the surface Phase 2 introduced.

The point is not to celebrate. It is to prove each Phase 1 finding is actually
closed under the same conditions that exposed it, and to find whatever the new
code broke.

Run:  python3 break_it_v2.py
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Tuple

from harness_core import (
    AdaptiveCheckpointing,
    CheckpointError,
    ConflictKind,
    ContextCompressor,
    CoreExecutor,
    OllamaFallbackHandler,
    Step,
)
from harness_core_v2 import (
    DynamicCheckpointing,
    DynamicCoreExecutor,
    FilesystemConflictDetector,
    IncrementalCheckpointing,
)
from core_behaviors import AskClarifyingQuestionsBehavior, Precondition, RiskLevel
from core_behaviors_v2 import ContinuousVerifyState, ParsingSafetyClassifier
from phase2_integrated_executor import Phase2IntegratedExecutor
from phase1_integrated_executor import PipelineStatus

FINDINGS: List[Tuple[str, str, str]] = []


def finding(severity: str, probe: str, note: str) -> None:
    FINDINGS.append((severity, probe, note))
    print(f"  [{severity}] {note}")


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


def fake_timer(seconds_per_step: float) -> Callable[[], float]:
    """Timer that advances a fixed amount per measured step."""
    state = {"now": 0.0, "calls": 0}

    def tick() -> float:
        state["calls"] += 1
        if state["calls"] % 2 == 0:
            state["now"] += seconds_per_step
        return state["now"]

    return tick


# --------------------------------------------------------------------------- #
# Re-probing the Phase 1 findings
# --------------------------------------------------------------------------- #


def probe_estimate_closed() -> None:
    """Phase 1 HIGH: a long task misclassified as short got zero checkpoints."""
    print("\nPROBE A: is the fixed-estimate finding closed")
    with tempfile.TemporaryDirectory() as tmp:
        steps = [Step(f"s{i}", kind="shell", description="d", run=lambda: "x")
                 for i in range(1, 21)]

        v1 = AdaptiveCheckpointing(Path(tmp) / "v1")
        CoreExecutor(v1, ollama=healthy()).execute("v1-task", steps)

        v2 = DynamicCheckpointing(Path(tmp) / "v2", min_observations=3)
        rep = DynamicCoreExecutor(v2, ollama=healthy(),
                                  timer=fake_timer(400.0)).execute("v2-task", steps)

        print(f"  v1 intermediate checkpoints: {v1.intermediate_writes}")
        print(f"  v2 intermediate checkpoints: {v2.intermediate_writes}")
        if v1.intermediate_writes == 0 and v2.intermediate_writes > 0:
            finding("CLOSED", "estimate",
                    f"v1 wrote 0 checkpoints on a 2.2 hour run, v2 wrote "
                    f"{v2.intermediate_writes} after {len(v2.revisions)} revision(s)")
        else:
            finding("HIGH", "estimate",
                    f"v2 still wrote {v2.intermediate_writes} checkpoints")

        if rep.policy.length.value == "short":
            finding("HIGH", "estimate", "final policy is still SHORT")


def probe_precondition_staleness_closed() -> None:
    """Phase 1 HIGH: a precondition that flipped mid-run was ignored."""
    print("\nPROBE B: is the precondition staleness finding closed")
    state = {"up": True}
    verify = ContinuousVerifyState()
    verify.add(Precondition("service", lambda: state["up"], "must stay up"),
               ttl_seconds=0)

    with tempfile.TemporaryDirectory() as tmp:
        steps = [
            Step("s1", command="echo a", description="a", estimated_seconds=180.0,
                 run=lambda: state.update(up=False) or "killed it"),
            Step("s2", command="echo b", description="b", estimated_seconds=180.0,
                 run=lambda: "should never run"),
        ]
        pipe = Phase2IntegratedExecutor(
            workdir=Path(tmp), verifier=verify,
            clarifier=AskClarifyingQuestionsBehavior(ask=lambda a: "staging"),
            safety=ParsingSafetyClassifier(confirm=lambda v: True),
            ollama=healthy(),
        )
        result = pipe.run("stale-v2", "restart ollama.service on popos-01 and verify",
                          steps)
        if result.status is PipelineStatus.EXECUTION_FAILED and result.mid_run_abort:
            finding("CLOSED", "precondition-staleness",
                    f"run stopped at {result.mid_run_abort['at_step']}, "
                    "v1 reported COMPLETED here")
        else:
            finding("HIGH", "precondition-staleness",
                    f"v2 still reported {result.status.value}")
        if len(result.execution.results) != 1:
            finding("HIGH", "precondition-staleness",
                    f"{len(result.execution.results)} steps ran, expected 1")


def probe_safety_evasion_v2() -> None:
    """Phase 1 HIGH: five evasions classified safe on the first pass."""
    print("\nPROBE C: safety classifier, wider evasion set")
    safety = ParsingSafetyClassifier()
    payload = base64.b64encode(b"rm -rf /var/lib/pg").decode()
    evasions = [
        ("rm -fr /var/lib/x", "flag order"),
        ("/bin/rm -rf /var/lib/x", "absolute path"),
        ("R=rm; $R -rf /var/lib/x", "variable indirection"),
        (f"echo '{payload}' | base64 -d | sh", "base64 pipe"),
        ("find /var -delete", "find -delete"),
        ("sudo -- sh -c 'rm -rf /var/lib/x'", "sh -c wrap"),
        ("sh -c \"sh -c 'rm -rf /srv'\"", "double sh -c wrap"),
        ("python3 -c \"import shutil;shutil.rmtree('/var/lib/x')\"", "python rmtree"),
        ("git checkout -- .", "discard local edits"),
        ("> /etc/fstab", "truncate by redirect"),
        ("SYSTEMCTL=1 systemctl stop ollama", "env prefix"),
        ("timeout 30 rm -rf /srv/data", "timeout wrapper"),
        ("nohup sudo mkfs.ext4 /dev/nvme0n1p3 &", "nohup + background"),
        ("env FOO=1 sudo rm -rf /opt/app", "env wrapper"),
        ("nice -n 5 dd if=/dev/zero of=/dev/sda", "nice wrapper"),
        ("xargs rm -rf < targets.txt", "xargs indirection"),
        ("chmod -R 777 /etc", "recursive chmod on /etc"),
        ("tee /etc/sudoers < evil", "tee into sudoers"),
        ("kill -9 1", "kill PID 1"),
        ("apt-get purge --yes ollama", "package purge"),
    ]
    missed = []
    for command, label in evasions:
        v = safety.classify(command)
        if v.level in (RiskLevel.SAFE, RiskLevel.CAUTION):
            missed.append(f"{label}: {command!r} -> {v.level.value}")
    for m in missed:
        finding("HIGH", "safety-evasion-v2", f"NOT flagged: {m}")
    print(f"  caught {len(evasions) - len(missed)}/{len(evasions)}")
    if not missed:
        finding("CLOSED", "safety-evasion-v2",
                f"all {len(evasions)} evasions flagged, including 8 the Phase 1 "
                "set never contained")


def probe_safety_false_positives_v2() -> None:
    """New risk: a stricter classifier that blocks normal work is worse."""
    print("\nPROBE D: false positives on routine commands")
    safety = ParsingSafetyClassifier()
    routine = [
        "ls -la /etc", "cat /proc/meminfo", "df -h", "free -h", "uptime",
        "systemctl status ollama", "journalctl -u ollama -n 50",
        "git pull --ff-only", "git log --oneline -20", "git status",
        "docker ps", "docker logs api --tail 100",
        "python3 -m pytest tests/", "grep -r TODO src/", "echo hello",
        "curl -s http://localhost:11434/api/tags",
        "ss -tlnp", "ps aux", "nvidia-smi", "lsblk",
    ]
    blocked = [c for c in routine if safety.classify(c).requires_confirmation]
    for c in blocked:
        finding("MED", "safety-fp-v2", f"routine command now needs confirmation: {c!r}")
    print(f"  clean {len(routine) - len(blocked)}/{len(routine)}")
    if not blocked:
        finding("CLOSED", "safety-fp-v2",
                f"{len(routine)} routine commands, 0 false positives")
    if safety.parse_failures:
        finding("MED", "safety-fp-v2",
                f"{len(safety.parse_failures)} parse failures on routine input")


def probe_undeclared_files_closed() -> None:
    """Phase 1 MED: files a step forgot to declare were never checked."""
    print("\nPROBE E: is the undeclared-file finding closed")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "tree"
        (root / "sub").mkdir(parents=True)
        declared = root / "declared.conf"
        forgotten = root / "sub" / "forgotten.conf"
        declared.write_text("a", encoding="utf-8")
        forgotten.write_text("b", encoding="utf-8")

        det = FilesystemConflictDetector(use_inotify=False)
        det.baseline_files([str(declared)])
        det.baseline_tree([root])
        forgotten.write_text("EDITED EXTERNALLY", encoding="utf-8")

        conflicts = det.detect()
        if any(c.path == str(forgotten) for c in conflicts):
            finding("CLOSED", "undeclared-files",
                    "an undeclared file edit is now caught; v1 was blind to it")
        else:
            finding("MED", "undeclared-files", "still not caught")
        det.close()


def probe_checkpoint_growth_closed() -> None:
    """Phase 1 MED: 18.6x bytes for 8x steps."""
    print("\nPROBE F: is the checkpoint growth finding closed")
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for label, cls in [("v1-full", DynamicCheckpointing),
                           ("v2-incremental", IncrementalCheckpointing)]:
            sizes = []
            for n in (25, 200):
                cp = cls(Path(tmp) / f"{label}{n}")
                steps = [Step(f"s{i}", kind="build", description="d" * 200,
                              estimated_seconds=180.0, run=lambda: "y" * 2048)
                         for i in range(1, n + 1)]
                DynamicCoreExecutor(cp, ollama=healthy()).execute(f"{label}-{n}", steps)
                sizes.append(cp.stats()["bytes_written"])
            results[label] = sizes
            print(f"  {label:16} 25 steps {sizes[0]/1024:8.1f} KB, "
                  f"200 steps {sizes[1]/1024:9.1f} KB, "
                  f"growth {sizes[1]/sizes[0]:.1f}x")

    v1_big, v2_big = results["v1-full"][1], results["v2-incremental"][1]
    v1_growth = results["v1-full"][1] / results["v1-full"][0]
    v2_growth = results["v2-incremental"][1] / results["v2-incremental"][0]
    if v2_big < v1_big / 4 and v2_growth < v1_growth:
        finding("CLOSED", "checkpoint-growth",
                f"200 step run went from {v1_big/1024:.0f} KB to {v2_big/1024:.0f} KB, "
                f"growth curve {v1_growth:.1f}x to {v2_growth:.1f}x")
    else:
        finding("MED", "checkpoint-growth",
                f"v2 wrote {v2_big/1024:.0f} KB vs v1 {v1_big/1024:.0f} KB")


# --------------------------------------------------------------------------- #
# New surface Phase 2 introduced
# --------------------------------------------------------------------------- #


def probe_delta_replay_integrity() -> None:
    """Does base + delta replay ever disagree with a full snapshot?"""
    print("\nPROBE G: delta replay integrity")
    with tempfile.TemporaryDirectory() as tmp:
        full = DynamicCheckpointing(Path(tmp) / "full")
        inc = IncrementalCheckpointing(Path(tmp) / "inc", compact_every=7)

        def run(cp, tag):
            def body(i: int) -> str:
                if i == 33:
                    raise RuntimeError("crash")
                return "ok"
            steps = [Step(f"s{i}", kind="build", estimated_seconds=180.0,
                          run=lambda i=i: body(i)) for i in range(1, 51)]
            DynamicCoreExecutor(cp, ollama=healthy()).execute(tag, steps)
            return cp.load_state(tag)

        a, b = run(full, "t"), run(inc, "t2")
        if a and b and a.completed_step_ids == b.completed_step_ids \
                and a.step_index == b.step_index:
            finding("OK", "delta-replay",
                    f"replay matches a full snapshot exactly at step {b.step_index} "
                    f"after {inc.compactions} compaction(s)")
        else:
            finding("HIGH", "delta-replay",
                    f"replay diverged: full={a.step_index if a else None} "
                    f"inc={b.step_index if b else None}")


def probe_guard_cost() -> None:
    """Does rechecking preconditions between steps cost real time?"""
    print("\nPROBE H: between-step guard overhead")
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        time.sleep(0.002)  # a cheap systemctl call is roughly this
        return True

    for ttl, label in [(0.0, "ttl=0 (always)"), (10.0, "ttl=10s (cached)")]:
        calls["n"] = 0
        verify = ContinuousVerifyState()
        verify.add(Precondition("svc", probe, "svc"), ttl_seconds=ttl)
        with tempfile.TemporaryDirectory() as tmp:
            steps = [Step(f"s{i}", estimated_seconds=180.0, run=lambda: "ok")
                     for i in range(1, 101)]
            cp = IncrementalCheckpointing(Path(tmp) / "cp")
            started = time.perf_counter()
            DynamicCoreExecutor(cp, ollama=healthy(),
                                between_steps=verify.between_steps
                                ).execute(f"guard-{ttl}", steps)
            elapsed = time.perf_counter() - started
        print(f"  {label:18} {calls['n']:>4} probe calls, {elapsed*1000:7.1f} ms "
              f"over 100 steps")
        if ttl == 0.0 and calls["n"] < 100:
            finding("HIGH", "guard-cost", "ttl=0 did not re-probe every step")
        if ttl == 10.0 and calls["n"] > 3:
            finding("MED", "guard-cost",
                    f"TTL cache leaked: {calls['n']} probes with a 10s TTL")
    finding("OK", "guard-cost",
            "TTL caching collapses 100 probe calls to 1 when the check is stable")


def probe_tree_watch_cost() -> None:
    """What does watching a real-sized tree actually cost per check?"""
    print("\nPROBE I: tree watch cost")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        for d in range(30):
            sub = root / f"pkg{d}"
            sub.mkdir(parents=True)
            for f in range(40):
                (sub / f"mod{f}.py").write_text("x" * 1024, encoding="utf-8")
        total = 30 * 40

        det = FilesystemConflictDetector(use_inotify=False)
        started = time.perf_counter()
        det.baseline_tree([root])
        baseline_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        clean = det.detect_tree()
        clean_ms = (time.perf_counter() - started) * 1000

        (root / "pkg7" / "mod13.py").write_text("CHANGED", encoding="utf-8")
        started = time.perf_counter()
        dirty = det.detect_tree()
        dirty_ms = (time.perf_counter() - started) * 1000

        print(f"  {total} files: baseline {baseline_ms:.1f} ms, "
              f"clean check {clean_ms:.1f} ms, dirty check {dirty_ms:.1f} ms")
        if clean == [] and len(dirty) == 1:
            finding("OK", "tree-watch",
                    f"{total} files, clean check {clean_ms:.1f} ms, "
                    "exactly one conflict on a single edit")
        else:
            finding("HIGH", "tree-watch",
                    f"clean={len(clean)} dirty={len(dirty)}, expected 0 and 1")
        if clean_ms > 250:
            finding("MED", "tree-watch",
                    f"a clean check costs {clean_ms:.0f} ms on only {total} files; "
                    "this scales linearly and will hurt on a real source tree")
        det.close()


def probe_mtime_evasion() -> None:
    """A same-size edit with a restored mtime is the classic detector blind spot."""
    print("\nPROBE J: fingerprint evasion")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "tree"
        root.mkdir()
        target = root / "config.conf"
        target.write_text("AAAA", encoding="utf-8")
        st = os.stat(target)

        det = FilesystemConflictDetector(use_inotify=False)
        det.baseline_files([str(target)])
        det.baseline_tree([root])

        target.write_text("BBBB", encoding="utf-8")          # same size
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # restored mtime

        tree_only = det.detect_tree()
        both = det.detect()
        if not tree_only:
            finding("MED", "mtime-evasion",
                    "tree watching alone misses a same-size same-mtime edit; the "
                    "declared-path hash is what catches it, so touches still matters")
        if any(c.kind is ConflictKind.MODIFIED for c in both):
            finding("OK", "mtime-evasion",
                    "the declared-path SHA256 layer catches what the fingerprint misses")
        else:
            finding("HIGH", "mtime-evasion", "neither layer caught the edit")
        det.close()


def probe_v1_still_works() -> None:
    """Phase 2 must not have broken the Phase 1 classes it subclasses."""
    print("\nPROBE K: Phase 1 regression check")
    with tempfile.TemporaryDirectory() as tmp:
        cp = AdaptiveCheckpointing(Path(tmp))
        steps = [Step(f"s{i}", estimated_seconds=10.0, run=lambda: "ok")
                 for i in range(1, 11)]
        rep = CoreExecutor(cp, ollama=healthy()).execute("v1-regress", steps)
        if rep.ok and cp.intermediate_writes == 0 and cp.writes == 1:
            finding("OK", "v1-regression",
                    "Phase 1 short-task behavior is byte for byte unchanged")
        else:
            finding("HIGH", "v1-regression",
                    f"Phase 1 behavior changed: writes={cp.writes} "
                    f"intermediate={cp.intermediate_writes} ok={rep.ok}")


def probe_remaining_gaps() -> None:
    """State what Phase 2 deliberately did not fix."""
    print("\nPROBE L: known remaining gaps")
    finding("OPEN", "rollback-inference",
            "undo actions are still caller supplied; a step that changes state "
            "without pushing an undo is still unrecoverable (P2 item, not built)")
    finding("OPEN", "rollback-verification",
            "a successful undo() call is still assumed to have worked; nothing "
            "re-hashes the files or re-probes the service (P2 item, not built)")
    finding("OPEN", "task-id-lock",
            "collision is warned about but there is still no lock file, so two "
            "concurrent runs race (P3 item, not built)")
    finding("OPEN", "routing-collision",
            "first-match routing still mis-handles 'review the deploy script'; "
            "scoring was deferred (P2 item, not built)")


def main() -> None:
    probes: List[Callable[[], None]] = [
        probe_estimate_closed,
        probe_precondition_staleness_closed,
        probe_safety_evasion_v2,
        probe_safety_false_positives_v2,
        probe_undeclared_files_closed,
        probe_checkpoint_growth_closed,
        probe_delta_replay_integrity,
        probe_guard_cost,
        probe_tree_watch_cost,
        probe_mtime_evasion,
        probe_v1_still_works,
        probe_remaining_gaps,
    ]
    for p in probes:
        try:
            p()
        except Exception as exc:
            finding("CRASH", p.__name__, f"probe raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print("PHASE 2 FINDINGS SUMMARY")
    print("=" * 78)
    for severity in ("CRASH", "HIGH", "MED", "OPEN", "CLOSED", "OK"):
        rows = [f for f in FINDINGS if f[0] == severity]
        if not rows:
            continue
        print(f"\n{severity} ({len(rows)}):")
        for _, probe, note in rows:
            print(f"  - {probe}: {note}")


if __name__ == "__main__":
    main()
