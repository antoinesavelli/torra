"""Human-readable summaries over what's already logged -- no new collection.

Depends on: torra.telemetry.records (TaskRecord), torra.telemetry.crash
(CrashReporter). Consumed by both cli.py's `torra evidence` and tui.py's
sidebar Evidence tab -- lives here, not in either entry point, so neither
depends on the other.
"""

from __future__ import annotations

import time
from collections import Counter

from .crash import CrashReporter
from .records import TaskRecord

__all__ = ["health_summary", "crash_summary"]


def _outcome_lines(records: list[TaskRecord]) -> list[str]:
    """`completed` used to be the only outcome `torra evidence` ever
    counted; blocked/refused/backend_down/failed were invisible in a
    summary that read as complete."""
    lines = ["outcomes:"]
    for status, count in Counter(r.status for r in records if r.status).most_common():
        lines.append(f"  {status:<16} : {count}")
    return lines


def _backend_line(records: list[TaskRecord]) -> str | None:
    local = sum(1 for r in records if r.model and not r.model.startswith("openrouter/"))
    remote = sum(1 for r in records if r.model.startswith("openrouter/"))
    return f"backend: {local} local, {remote} openrouter" if local or remote else None


def _latency_line(records: list[TaskRecord]) -> str | None:
    timed = [r.seconds for r in records if r.seconds > 0]
    if not timed:
        return None
    return f"mean turn time: {sum(timed) / len(timed):.2f}s over {len(timed)} timed task(s)"


def _rollback_line(records: list[TaskRecord]) -> str | None:
    rolled_back = [r for r in records if r.rollback_ran]
    if not rolled_back:
        return None
    verified = sum(1 for r in rolled_back if r.rollback_verified)
    return f"rollback: {len(rolled_back)} ran, {verified} fully verified"


def health_summary(records: list[TaskRecord]) -> str:
    """Outcome/backend/latency/rollback trends already sitting in
    tasks.jsonl -- pure aggregation, no new data collection."""
    if not records:
        return "no tasks recorded yet"
    lines = _outcome_lines(records)
    for line in (_backend_line(records), _latency_line(records), _rollback_line(records)):
        if line is not None:
            lines.append(line)
    return "\n".join(lines)


def crash_summary(reporter: CrashReporter) -> str:
    """Unhandled-exception history -- distinct from the outcomes above,
    which are ordinary gated-task failures TaskLogger already records.
    A crash here means a bug escaped every other guard (HANDOFF.md §2.29)."""
    crashes = reporter.read()
    if not crashes:
        return "no crashes recorded"
    latest = crashes[-1]
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest.at))
    return (
        f"{len(crashes)} crash(es) recorded\n"
        f"  most recent: {latest.exception_type} ({latest.site}) at {when}\n"
        f"    {latest.message[:200]}"
    )
