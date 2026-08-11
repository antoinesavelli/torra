"""ResumePoint: a durable, forensic record of how far a task got.

No caller anywhere in this codebase reads one back to resume automatically
(see ResumePoint's own docstring) -- but Harness.execute() still writes one
after every completed step, so its correctness on disk matters even with
zero downstream readers today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.core import HarnessError, ResumePoint


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

    with pytest.raises(HarnessError, match="corrupt resume point"):
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

    with pytest.raises(HarnessError, match="resume point write failed"):
        ResumePoint(task_id="t").save(target)

    assert not target.exists()
    assert not target.with_suffix(".tmp").exists(), "a stray .tmp file was left behind"
