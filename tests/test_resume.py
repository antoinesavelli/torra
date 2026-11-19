"""ResumePoint: a durable, forensic record of how far a task got.

No caller anywhere in this codebase reads one back to resume automatically
(see ResumePoint's own docstring) -- but Torra.execute() still writes one
after every completed step, so its correctness on disk matters even with
zero downstream readers today.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from torra.core import ResumePoint, TorraError, resolve_resume
from torra.core.resume import RESUME_MAX_AGE_SECONDS, begin_step, command_digest, end_step
from torra.gates.undo_shadow import ShadowStore


def test_load_of_an_absent_file_returns_none(tmp_path: Path) -> None:
    assert ResumePoint.load(tmp_path / "nope.json") is None


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "resume" / "t.json"
    point = ResumePoint(task_id="t", completed=["a", "b"], total=3)

    point.save(target)
    loaded = ResumePoint.load(target)

    assert loaded is not None
    assert loaded.task_id == "t"
    assert loaded.completed == ["a", "b"]
    assert loaded.total == 3
    assert loaded.at > 0, "save() must stamp a real timestamp"


def test_a_corrupt_file_raises_rather_than_returning_garbage(tmp_path: Path) -> None:
    """Resuming from garbage is worse than starting over -- this must be
    loud, not silently return an empty or partially-populated ResumePoint."""
    target = tmp_path / "corrupt.json"
    target.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(TorraError, match="corrupt resume point"):
        ResumePoint.load(target)


def test_save_is_atomic_no_partial_file_left_on_a_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Write to the temp file, then rename -- a crash between the two must
    never leave a torn target file, and a failure must not leave a stray
    .tmp file lying around either."""
    target = tmp_path / "t.json"

    def broken_replace(src, dst):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr("os.replace", broken_replace)

    with pytest.raises(TorraError, match="resume point write failed"):
        ResumePoint(task_id="t").save(target)

    assert not target.exists()
    assert not target.with_suffix(".tmp").exists(), "a stray .tmp file was left behind"


def test_save_swallows_a_cleanup_failure_after_a_write_failure(tmp_path: Path, monkeypatch) -> None:
    """The original write failure is what must reach the caller -- a
    second failure while merely trying to clean up the stray .tmp file
    must not replace or mask it."""
    target = tmp_path / "t.json"
    tmp = target.with_suffix(".tmp")

    def broken_replace(src, dst):
        raise OSError("simulated crash mid-rename")

    real_unlink = Path.unlink

    def broken_unlink(self, *args, **kwargs):
        if self == tmp:
            raise OSError("simulated cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr("os.replace", broken_replace)
    monkeypatch.setattr(Path, "unlink", broken_unlink)

    with pytest.raises(TorraError, match="resume point write failed"):
        ResumePoint(task_id="t").save(target)

    assert tmp.exists(), "the unlink failure means the stray .tmp file survives"


def test_the_in_flight_mark_and_digests_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "t.json"
    ResumePoint(
        task_id="t",
        completed=["a"],
        total=2,
        in_progress="b",
        digests={"a": command_digest("echo a")},
    ).save(target)

    loaded = ResumePoint.load(target)

    assert loaded is not None
    assert loaded.in_progress == "b" and loaded.digests == {"a": command_digest("echo a")}


def test_a_file_in_the_older_format_still_loads_but_vouches_for_nothing(tmp_path: Path) -> None:
    target = tmp_path / "t.json"
    target.write_text(
        json.dumps(
            {"task_id": "t", "completed": ["a"], "total": 1, "file_hashes": {}, "at": time.time()}
        ),
        encoding="utf-8",
    )

    resolution = resolve_resume("t", {"a": command_digest("echo a")}, 1, target, dry_run=False)

    assert resolution.resumed_steps == frozenset()
    assert "doesn't match" in (resolution.note or "")


def test_end_step_counts_only_a_real_successful_step(tmp_path: Path) -> None:
    target = tmp_path / "t.json"
    point = ResumePoint(task_id="t", total=3)
    begin_step(point, target, "a")
    end_step(point, target, "a", "echo a", ok=True, dry_run=False)
    begin_step(point, target, "b")
    end_step(point, target, "b", "false", ok=False, dry_run=False)
    end_step(point, tmp_path / "never.json", "c", "echo c", ok=True, dry_run=True)

    assert point.completed == ["a"] and point.in_progress == ""
    assert not (tmp_path / "never.json").exists()


def test_begin_and_end_step_really_write_the_file_each_time(tmp_path: Path) -> None:
    """`_save_quietly` swallows write errors, so only the file itself shows a failed save."""
    target = tmp_path / "deep" / "t.json"
    point = ResumePoint(task_id="t", total=2)

    begin_step(point, target, "a")
    assert ResumePoint.load(target).in_progress == "a"  # type: ignore[union-attr]
    end_step(point, target, "a", "echo a", ok=True, dry_run=False)

    loaded = ResumePoint.load(target)
    assert loaded is not None and loaded.completed == ["a"] and loaded.in_progress == ""
    assert loaded.digests == {"a": command_digest("echo a")}


@pytest.mark.parametrize(
    "body",
    [
        '{"task_id": "t", "completed": null, "total": 1}',
        '{"task_id": "t", "completed": ["a"], "total": 1, "digests": [], "in_progress": ""}',
        '{"task_id": "t", "completed": ["a"], "total": 1, "digests": null}',
        '{"task_id": "t", "completed": [], "total": 1, "in_progress": ["x"]}',
        '{"task_id": "t", "completed": [], "total": 1, "file_hashes": null}',
        '{"task_id": "t", "completed": [1, 2], "total": 2}',
        '{"task_id": "t", "completed": [], "total": "many"}',
        '{"task_id": 7, "completed": [], "total": 1}',
    ],
)
def test_a_resume_file_with_wrong_field_types_starts_fresh_instead_of_crashing(
    tmp_path: Path, body: str
) -> None:
    target = tmp_path / "t.json"
    target.write_text(body, encoding="utf-8")

    resolution = resolve_resume("t", {"a": command_digest("echo a")}, 1, target, dry_run=False)

    assert resolution.resumed_steps == frozenset() and resolution.interrupted == ""
    assert "corrupt" in (resolution.note or "")


NOW = 1_800_000_000.0
_DIGESTS = {"a": command_digest("echo a"), "b": command_digest("echo b")}


def _aged_point(tmp_path: Path, at: object, in_progress: str = "") -> Path:
    """Write a resume point for step `a` of a two-step plan with an exact `at` (save() stamps now)."""
    target = tmp_path / "t.json"
    point = ResumePoint(
        task_id="t",
        completed=["a"],
        total=2,
        digests={"a": _DIGESTS["a"]},
        in_progress=in_progress,
    )
    point.save(target)
    body = json.loads(target.read_text(encoding="utf-8"))
    body["at"] = at
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


def test_the_resume_age_limit_matches_the_shadow_staleness_limit() -> None:
    assert RESUME_MAX_AGE_SECONDS == ShadowStore.STALE_SHADOW_SECONDS


def test_a_resume_point_over_the_age_limit_is_ignored_with_a_note(tmp_path: Path) -> None:
    target = _aged_point(tmp_path, NOW - RESUME_MAX_AGE_SECONDS - 1)

    resolution = resolve_resume("t", _DIGESTS, 2, target, dry_run=False, now=NOW)

    assert resolution.resumed_steps == frozenset() and resolution.interrupted == ""
    assert resolution.resume.completed == []
    assert "old" in (resolution.note or "") and "starting fresh" in (resolution.note or "")


@pytest.mark.parametrize(
    "age", [RESUME_MAX_AGE_SECONDS, 60.0, 0.0, -3600.0, -RESUME_MAX_AGE_SECONDS]
)
def test_a_resume_point_within_the_limit_of_now_in_either_direction_still_resumes(
    tmp_path: Path, age: float
) -> None:
    target = _aged_point(tmp_path, NOW - age)

    resolution = resolve_resume("t", _DIGESTS, 2, target, dry_run=False, now=NOW)

    assert resolution.resumed_steps == frozenset({"a"})


def test_an_old_resume_point_with_a_step_in_flight_never_expires(tmp_path: Path) -> None:
    """A step whose effect is unknown stays unknown however old the mark (docs/INVARIANTS.md §2.64)."""
    target = _aged_point(tmp_path, NOW - 30 * RESUME_MAX_AGE_SECONDS, in_progress="b")

    resolution = resolve_resume("t", _DIGESTS, 2, target, dry_run=False, now=NOW)

    assert resolution.interrupted == "b"
    assert resolution.resumed_steps == frozenset({"a"})


@pytest.mark.parametrize("bad_at", ["yesterday", True, None, [1.0]])
def test_a_wrong_typed_timestamp_is_a_corrupt_file_and_starts_fresh(
    tmp_path: Path, bad_at: object
) -> None:
    target = _aged_point(tmp_path, bad_at)

    resolution = resolve_resume("t", _DIGESTS, 2, target, dry_run=False, now=NOW)

    assert resolution.resumed_steps == frozenset() and resolution.interrupted == ""
    assert "corrupt" in (resolution.note or "")


@pytest.mark.parametrize("age", [-(RESUME_MAX_AGE_SECONDS + 1), -25 * 3600.0, -1e308])
def test_a_resume_point_dated_far_in_the_future_is_ignored(tmp_path: Path, age: float) -> None:
    target = _aged_point(tmp_path, NOW - age)

    resolution = resolve_resume("t", _DIGESTS, 2, target, dry_run=False, now=NOW)

    assert resolution.resumed_steps == frozenset() and resolution.interrupted == ""
    assert "ahead" in (resolution.note or "")


@pytest.mark.parametrize("bad_at", [10**400, float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_or_huge_timestamp_is_a_corrupt_file_and_starts_fresh(
    tmp_path: Path, bad_at: object
) -> None:
    target = _aged_point(tmp_path, bad_at)

    resolution = resolve_resume("t", _DIGESTS, 2, target, dry_run=False, now=NOW)

    assert resolution.resumed_steps == frozenset() and resolution.interrupted == ""
    assert "corrupt" in (resolution.note or "")


def test_an_old_point_whose_in_flight_step_is_not_in_the_plan_still_expires(
    tmp_path: Path,
) -> None:
    """A re-plan renamed the crashed step: nothing blocks, so nothing may vouch for old steps."""
    target = _aged_point(tmp_path, NOW - 30 * RESUME_MAX_AGE_SECONDS, in_progress="renamed")

    resolution = resolve_resume("t", _DIGESTS, 2, target, dry_run=False, now=NOW)

    assert resolution.resumed_steps == frozenset() and resolution.interrupted == ""
    assert "old" in (resolution.note or "") and "starting fresh" in (resolution.note or "")
