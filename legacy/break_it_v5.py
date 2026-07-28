"""
break_it_v5.py
Phase 5 adversarial probes, plus the final sweep across all five phases.

Telemetry gets probed harder than the features did, because it is the component
most likely to fail quietly. A gate that breaks stops a task; a logger that
breaks just stops telling you things, and you find out months later when the
evidence you were counting on is not there.

Run:  python3 break_it_v5.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List, Tuple

from harness_core import OllamaFallbackHandler, Step
from core_behaviors_v2 import ParsingSafetyClassifier
from core_behaviors_v4 import ShadowReaper, StructuralClarifier
from core_behaviors_v5 import StrictShadowReaper
from harness_telemetry import (
    CorpusExporter, LabelStore, LoopholeAudit, TaskLogger, TaskRecord,
)
from phase1_integrated_executor import PipelineStatus
from phase5_integrated_executor import Phase5IntegratedExecutor

FINDINGS: List[Tuple[str, str, str]] = []


def finding(severity: str, probe: str, note: str) -> None:
    FINDINGS.append((severity, probe, note))
    print(f"  [{severity}] {note}")


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


def rec(request: str, **kw) -> TaskRecord:
    base = dict(task_id="t", request=request, at=1.0)
    base.update(kw)
    return TaskRecord(**base)


def probe_secret_leakage() -> None:
    """Task text is operator input. What reaches disk?"""
    print("\nPROBE A: secret leakage into the task log")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tasks.jsonl"
        log = TaskLogger(path)
        leaks = [
            ("ghp_abcdefghijklmnopqrstuvwxyz012345", "github token"),
            ("sk-abcdefghijklmnopqrstuvwxyz1234", "api key"),
            ("AKIAIOSFODNN7EXAMPLE", "aws access key"),
            ("hunter2istheworst", "password value"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "jwt"),
        ]
        log.record(rec(f"deploy with token={leaks[0][0]}"))
        log.record(rec(f"export API_KEY={leaks[1][0]}"))
        log.record(rec(f"aws key {leaks[2][0]} for the bucket"))
        log.record(rec(f"set password: {leaks[3][0]}"))
        log.record(rec(f"authorization bearer {leaks[4][0]}"))

        blob = path.read_text(encoding="utf-8")
        found = [label for value, label in leaks if value in blob]
        for label in found:
            finding("HIGH", "secret-leak", f"{label} reached the log verbatim")
        print(f"  redacted {len(leaks) - len(found)}/{len(leaks)}")
        if not found:
            finding("OK", "secret-leak",
                    f"all {len(leaks)} secret shapes redacted before disk")

        # The inverse risk: over-redaction destroying useful task text.
        log.record(rec("deploy /srv/api and restart nginx.service"))
        assert "/srv/api" in path.read_text(encoding="utf-8")
        finding("OK", "secret-leak",
                "ordinary paths and unit names survive redaction intact")


def probe_telemetry_cannot_break_a_run() -> None:
    """A logger that raises during a deploy is worse than no logger."""
    print("\nPROBE B: telemetry failure isolation")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        blocker = root / "blocker"
        blocker.write_text("a file, not a directory", encoding="utf-8")

        pipe = Phase5IntegratedExecutor(
            workdir=root / "run",
            log_path=blocker / "nested" / "tasks.jsonl",
            clarifier=StructuralClarifier(ask=lambda a: "staging"),
            safety=ParsingSafetyClassifier(confirm=lambda v: True),
            ollama=healthy())

        ran: List[str] = []
        steps = [Step("s1", command="echo x", description="d",
                      estimated_seconds=20, run=lambda: ran.append("s1"))]
        result = pipe.run("t1", "deploy commit a1b2c3d to staging", steps)

        if result.status is PipelineStatus.COMPLETED and ran == ["s1"]:
            finding("OK", "telemetry-isolation",
                    "the task completed normally with the log destination broken")
        else:
            finding("HIGH", "telemetry-isolation",
                    f"a broken log changed the run: {result.status.value}")
        if result.logged is False and (result.telemetry_errors or pipe.logger.errors):
            finding("OK", "telemetry-isolation",
                    "the failure is reported rather than swallowed")
        else:
            finding("HIGH", "telemetry-isolation",
                    "a failed log write was not surfaced")


def probe_concurrent_logging() -> None:
    """Several threads appending at once. Does the log survive?"""
    print("\nPROBE C: concurrent log appends")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tasks.jsonl"
        log = TaskLogger(path)
        writers, per_writer = 8, 40

        def write(n: int) -> None:
            for i in range(per_writer):
                log.record(rec(f"task {n}-{i} on /srv/api"))

        threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        parsed = log.read()
        expected = writers * per_writer
        print(f"  {writers} threads x {per_writer}: {len(raw_lines)} lines, "
              f"{len(parsed)} parsed, expected {expected}")
        if len(parsed) == expected and not log.errors:
            finding("OK", "concurrent-logging",
                    f"{expected} records from {writers} threads, none interleaved "
                    "or lost")
        else:
            finding("MED", "concurrent-logging",
                    f"{len(parsed)} of {expected} records survived; line-buffered "
                    "appends are not atomic above the pipe buffer size")


def probe_small_sample_discipline() -> None:
    """Does the harness ever report a rate it should not?"""
    print("\nPROBE D: small sample discipline")
    with tempfile.TemporaryDirectory() as tmp:
        log = TaskLogger(Path(tmp) / "tasks.jsonl")
        labels = LabelStore(Path(tmp) / "labels.jsonl")
        exporter = CorpusExporter(log, labels)

        overclaims = []
        for n in (1, 3, 10, 29):
            labels_path = Path(tmp) / f"labels{n}.jsonl"
            store = LabelStore(labels_path)
            for i in range(n):
                store.label(f"fix /srv/f{i}.conf", True, "no criterion")
            report = CorpusExporter(log, store).drift(
                StructuralClarifier(), baseline_fn=0.0)
            if "insufficient data" not in report.verdict:
                overclaims.append((n, report.verdict))

        for n, verdict in overclaims:
            finding("HIGH", "small-sample",
                    f"drew a conclusion from {n} labels: {verdict}")
        if not overclaims:
            finding("OK", "small-sample",
                    "refused to conclude at 1, 3, 10, and 29 labels; the "
                    "threshold is 30 and it is enforced, not advisory")

        audit = LoopholeAudit(StructuralClarifier())
        report = audit.audit([rec("fix /srv/a.conf") for _ in range(29)])
        if "insufficient data" not in report.verdict:
            finding("HIGH", "small-sample",
                    "the loophole audit concluded from 29 records")


def probe_drift_detects_a_real_regression() -> None:
    """If the clarifier degrades on live traffic, does anyone find out?"""
    print("\nPROBE E: drift detection")
    with tempfile.TemporaryDirectory() as tmp:
        log = TaskLogger(Path(tmp) / "tasks.jsonl")
        labels = LabelStore(Path(tmp) / "labels.jsonl")
        # Traffic full of anchored-but-goalless requests the clarifier passes.
        for i in range(25):
            labels.label(f"fix /srv/api/file{i}.conf", True, "no acceptance criterion")
        for i in range(15):
            labels.label(f"restart u{i}.service and confirm it reports active", False)

        report = CorpusExporter(log, labels).drift(
            StructuralClarifier(), baseline_fn=0.0, baseline_fp=0.10)
        print(f"  live fn {report.fn_rate:.0%} vs baseline 0%, verdict: "
              f"{report.verdict[:60]}")
        if report.verdict.startswith("DRIFT"):
            finding("OK", "drift-detection",
                    f"a {report.fn_rate:.0%} live false negative rate against a 0% "
                    "synthetic baseline is flagged as drift, not averaged away")
        else:
            finding("HIGH", "drift-detection",
                    f"a {report.fn_rate:.0%} regression was not flagged")


def probe_loophole_decision_is_data_driven() -> None:
    """Does the audit recommend action only when the data supports it?"""
    print("\nPROBE F: loophole verdict")
    audit = LoopholeAudit(StructuralClarifier())

    harmful = ([rec(f"fix /srv/f{i}.conf", stages_completed=["clarify", "execute"],
                    status="execution_failed" if i < 16 else "completed")
                for i in range(20)]
               + [rec(f"set workers to {i + 2} in /srv/c{i}.conf",
                      stages_completed=["clarify", "execute"], status="completed")
                  for i in range(20)])
    harmless = ([rec(f"fix /srv/f{i}.conf", stages_completed=["clarify", "execute"],
                     status="completed") for i in range(4)]
                + [rec(f"set workers to {i + 2} in /srv/c{i}.conf",
                       stages_completed=["clarify", "execute"], status="completed")
                   for i in range(30)])

    a, b = audit.audit(harmful), audit.audit(harmless)
    print(f"  harmful traffic  -> {a.verdict[:64]}")
    print(f"  harmless traffic -> {b.verdict[:64]}")
    if a.verdict.startswith("ACT") and not b.verdict.startswith("ACT"):
        finding("OK", "loophole-verdict",
                "recommends closing the gap only when goalless requests actually "
                "fail more, so the decision is made by the data")
    else:
        finding("MED", "loophole-verdict",
                f"verdicts did not separate: {a.verdict[:40]} / {b.verdict[:40]}")


def probe_reaper_naming_accidents() -> None:
    """The Phase 4 finding: prefix matching decided by accident of naming."""
    print("\nPROBE G: reaper naming")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = time.time() - 90_000
        traps = ["harness-shadow-4821", "harness-shadow-backup-of-everything",
                 "harness-shadowy-business", "harness-shadow-", "my-harness-shadow-1"]
        for name in traps:
            d = root / name
            d.mkdir()
            os.utime(d, (old, old))

        loose = ShadowReaper(roots=[root], max_age_seconds=3600, min_age_seconds=1)
        loose_taken = {n for n in loose.candidates()}
        strict = StrictShadowReaper(roots=[root], max_age_seconds=3600,
                                    min_age_seconds=1)
        strict_report = strict.reap()

        survivors = [n for n in traps if (root / n).exists()]
        print(f"  loose policy would take {len(loose_taken)}, strict took "
              f"{strict_report.removed}")
        if strict_report.removed == 1 and set(survivors) == set(traps) - {"harness-shadow-4821"}:
            finding("CLOSED", "reaper-naming",
                    "only the pid-suffixed directory was removed; three plausible "
                    "human-named directories survived that the loose policy "
                    "would have taken")
        else:
            finding("HIGH", "reaper-naming",
                    f"strict reaper removed {strict_report.removed}, survivors "
                    f"{survivors}")
        if strict.near_misses:
            finding("OK", "reaper-naming",
                    f"{len(strict.near_misses)} near miss(es) reported so an "
                    "operator finds out rather than wondering")


def probe_log_growth() -> None:
    """An append-only log on a busy box. How fast does it grow?"""
    print("\nPROBE H: log growth")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tasks.jsonl"
        log = TaskLogger(path)
        started = time.perf_counter()
        for i in range(1000):
            log.record(rec(f"deploy commit a1b2c3d{i} to staging",
                           status="completed",
                           stages_completed=["clarify", "model_select", "execute"],
                           clarifier_anchors=["commit sha", "named environment"]))
        elapsed = time.perf_counter() - started
        size = path.stat().st_size
        print(f"  1000 records: {size / 1024:.0f} KB in {elapsed * 1000:.0f} ms, "
              f"{size / 1000:.0f} bytes each")
        per_year = size / 1000 * 50 * 365 / 1024 / 1024
        if per_year > 200:
            finding("MED", "log-growth",
                    f"{size / 1000:.0f} bytes per task means {per_year:.0f} MB/year "
                    "at 50 tasks a day, with no rotation")
        else:
            finding("OK", "log-growth",
                    f"{size / 1000:.0f} bytes per task, about {per_year:.0f} MB/year "
                    "at 50 tasks a day")
        if elapsed > 2.0:
            finding("MED", "log-growth", f"1000 appends took {elapsed:.1f}s")


def probe_full_regression() -> None:
    """All 101 cases across five phases."""
    print("\nPROBE I: full regression, phases 1 through 5")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    tail = (result.stdout or result.stderr).strip().splitlines()[-1:]
    print(f"  {tail[0] if tail else 'no output'}")
    if result.returncode == 0:
        finding("OK", "full-regression", "all 101 cases pass across five phases")
    else:
        finding("HIGH", "full-regression", f"regression: {tail}")


def probe_remaining_gaps() -> None:
    """What is still open after five phases."""
    print("\nPROBE J: known remaining gaps")
    finding("OPEN", "no-real-traffic",
            "every number in five phases still comes from data I generated. "
            "The telemetry now exists; nobody has fed it a real week yet")
    finding("OPEN", "log-rotation",
            "the task log is append-only with no rotation or compaction")
    finding("OPEN", "clarifier-semantics",
            "the anchored-but-goalless gap is now measurable but still open; "
            "the audit will say whether to close it once there is traffic")
    finding("OPEN", "undo-coverage-ceiling",
            "database migrations, dd, and mkfs still need a caller-supplied undo")


def main() -> None:
    probes: List[Callable[[], None]] = [
        probe_secret_leakage,
        probe_telemetry_cannot_break_a_run,
        probe_concurrent_logging,
        probe_small_sample_discipline,
        probe_drift_detects_a_real_regression,
        probe_loophole_decision_is_data_driven,
        probe_reaper_naming_accidents,
        probe_log_growth,
        probe_full_regression,
        probe_remaining_gaps,
    ]
    for p in probes:
        try:
            p()
        except Exception as exc:
            finding("CRASH", p.__name__, f"probe raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print("PHASE 5 FINDINGS SUMMARY")
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
