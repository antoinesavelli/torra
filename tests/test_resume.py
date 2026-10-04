"""ResumePoint: a durable, forensic record of how far a task got.

No caller anywhere in this codebase reads one back to resume automatically
(see ResumePoint's own docstring) -- but Torra.execute() still writes one
after every completed step, so its correctness on disk matters even with
zero downstream readers today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from torra.core import ResumePoint, TorraError, resolve_resume
from torra.core.resume import begin_step, command_digest, end_step


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
        tmp_path: Path, monkeypatch) -> None:
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


def test_save_swallows_a_cleanup_failure_after_a_write_failure(
        tmp_path: Path, monkeypatch) -> None:
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
    ResumePoint(task_id="t", completed=["a"], total=2, in_progress="b",
                digests={"a": command_digest("echo a")}).save(target)

    loaded = ResumePoint.load(target)

    assert loaded is not None
    assert loaded.in_progress == "b" and loaded.digests == {"a": command_digest("echo a")}


def test_a_file_in_the_older_format_still_loads_but_vouches_for_nothing(tmp_path: Path) -> None:
    target = tmp_path / "t.json"
    target.write_text('{"task_id": "t", "completed": ["a"], "total": 1, "file_hashes": {},'
                      ' "at": 1.0}', encoding="utf-8")

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
