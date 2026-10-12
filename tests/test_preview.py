"""The tick prompt says what a step will act on, not just what it will run.

Real-use finding 4 (2026-10-11): the operator was asked to approve `rm -r <path>` having seen
only the command line. The preview is read-only and advisory: it looks each named path up on
disk (never following a symlink, never reading file contents) and says what is there.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import plan_reply, scripted

from torra import cli
from torra.core import OllamaClient, Step, TaskSpec
from torra.path_warnings import named_paths
from torra.pipeline import Torra
from torra.preview import describe_path, preview_targets

# --------------------------------------------------------------------------- #
# describe_path
# --------------------------------------------------------------------------- #


def _tree(root: Path) -> Path:
    """3 files (1 KiB, 2 KiB, 4 KiB) in 2 folders, the newest change on 2026-10-03."""
    (root / "sub").mkdir(parents=True)
    for rel, size, day in (("a.bin", 1024, 1), ("sub/b.bin", 2048, 2), ("sub/c.bin", 4096, 3)):
        file = root / rel
        file.write_bytes(b"x" * size)
        stamp = time.mktime((2026, 10, day, 12, 0, 0, 0, 0, -1))
        os.utime(file, (stamp, stamp))
    return root


def test_a_missing_path_says_so(tmp_path: Path) -> None:
    assert describe_path(str(tmp_path / "gone")) == "does not exist"


def test_a_file_is_described_by_size_and_date(tmp_path: Path) -> None:
    file = tmp_path / "f.txt"
    file.write_bytes(b"x" * 1536)
    stamp = time.mktime((2026, 10, 3, 12, 0, 0, 0, 0, -1))
    os.utime(file, (stamp, stamp))

    assert describe_path(str(file)) == "file, 1.5 KiB, modified 2026-10-03"


def test_a_directory_is_described_by_what_is_in_it(tmp_path: Path) -> None:
    text = describe_path(str(_tree(tmp_path / "d")))

    assert text == "directory, 3 files in 1 folder, 7.0 KiB, newest change 2026-10-03"


def test_an_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "e").mkdir()

    assert describe_path(str(tmp_path / "e")).startswith("empty directory")


def test_a_symlink_is_described_not_followed(tmp_path: Path) -> None:
    target = _tree(tmp_path / "big")
    link = tmp_path / "link"
    link.symlink_to(target)

    text = describe_path(str(link))

    assert text == f"symlink to {target}"


def test_a_symlink_inside_a_directory_is_not_followed_so_a_loop_ends(tmp_path: Path) -> None:
    root = _tree(tmp_path / "d")
    (root / "loop").symlink_to(root)
    (root / "outside").symlink_to(tmp_path)

    text = describe_path(str(root))

    assert "5 files in 1 folder" in text, "the two links count as entries, nothing behind them"


def test_a_huge_tree_stops_counting_at_the_limit(tmp_path: Path) -> None:
    root = tmp_path / "many"
    root.mkdir()
    for index in range(30):
        (root / f"f{index}").write_bytes(b"x")

    text = describe_path(str(root), limit=10)

    assert text.startswith("directory, at least 10 files")
    assert "stopped counting" in text


def test_a_slow_walk_stops_at_the_time_limit(tmp_path: Path) -> None:
    root = _tree(tmp_path / "d")
    ticks = iter(range(1000))

    text = describe_path(str(root), seconds=2.0, clock=lambda: next(ticks))

    assert "stopped counting" in text


def test_an_unreadable_folder_is_reported_not_raised(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path / "d")
    real = os.scandir

    def scandir(path: Any):
        if str(path).endswith("sub"):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)

    assert "some folders unreadable" in describe_path(str(root))


def test_a_device_is_named_as_one() -> None:
    assert describe_path("/dev/null") == "character device"
    assert stat.S_ISCHR(os.stat("/dev/null").st_mode)


@pytest.mark.parametrize("size, shown", [
    (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (5 * 1024 ** 2, "5.0 MiB"),
    (3 * 1024 ** 3, "3.0 GiB")])
def test_sizes_use_binary_units(tmp_path: Path, size: int, shown: str) -> None:
    file = tmp_path / "f"
    with open(file, "wb") as handle:
        handle.truncate(size)

    assert f", {shown}," in describe_path(str(file))


# --------------------------------------------------------------------------- #
# preview_targets
# --------------------------------------------------------------------------- #


def _step(run: str, **fields: Any) -> Step:
    spec = TaskSpec.validate({"task_id": "t", "request": "r",
                              "steps": [{"id": "s", "run": run, **fields}]}, source="test")
    return spec.to_steps(dry_run=True)[0]


def _fake(path: str) -> str:
    return f"<{path}>"


def test_every_path_the_step_names_is_listed_with_its_description() -> None:
    lines = preview_targets(_step("rm -r /srv/a /srv/b"), describe=_fake)

    assert lines == ["/srv/a: </srv/a>", "/srv/b: </srv/b>"]


def test_declared_touches_are_listed_too_and_not_twice() -> None:
    lines = preview_targets(_step("rm -r /srv/a", touches=["/srv/a", "/srv/c"]), describe=_fake)

    assert [line.split(":")[0] for line in lines] == ["/srv/a", "/srv/c"]


def test_the_destination_of_a_copy_is_listed_so_an_overwrite_shows() -> None:
    lines = preview_targets(_step("cp /srv/a /srv/b"), describe=_fake)

    assert [line.split(":")[0] for line in lines] == ["/srv/a", "/srv/b"]


def test_a_redirect_target_is_listed() -> None:
    lines = preview_targets(_step("echo hi > /srv/out.txt"), describe=_fake)

    assert [line.split(":")[0] for line in lines] == ["/srv/out.txt"]


def test_what_cannot_be_known_is_left_out() -> None:
    assert preview_targets(_step("rm -r $TARGET/x /var/log/*.old relative"), describe=_fake) == []


def test_home_is_previewed_like_a_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    """gpt-oss:20b writes `"$HOME/Downloads/..."` (seen live 2026-10-12); the operator should see
    what is there, not nothing."""
    monkeypatch.setenv("HOME", "/home/antoine")

    assert preview_targets(_step('rm -r "$HOME/x"'), describe=_fake) == [
        "/home/antoine/x: </home/antoine/x>"]


def test_a_remote_step_has_no_local_preview() -> None:
    step = _step("ignored", kind="remote_pull", remote_path="/etc/app.conf",
                 touches=["/tmp/app.conf"])

    assert preview_targets(step, describe=_fake) == []


def test_a_long_list_is_cut_and_says_how_many_are_missing() -> None:
    paths = " ".join(f"/srv/p{index}" for index in range(12))

    lines = preview_targets(_step(f"rm {paths}"), describe=_fake, limit=8)

    assert len(lines) == 9
    assert lines[-1] == "... and 4 more"


def test_a_failing_lookup_does_not_break_the_prompt() -> None:
    def broken(path: str) -> str:
        raise OSError("stale handle")

    assert preview_targets(_step("rm /srv/a"), describe=broken) == [
        "/srv/a: cannot be inspected (stale handle)"]


def test_named_paths_keeps_order_and_drops_repeats() -> None:
    assert named_paths("ls /srv/a && du -sh /srv/b /srv/a") == ["/srv/a", "/srv/b"]


# --------------------------------------------------------------------------- #
# In the plan, on the prompt, in the audit
# --------------------------------------------------------------------------- #


def _plan_for(tmp_path: Path, run: str):
    torra = Torra(workdir=tmp_path / "wd", ollama=OllamaClient(transport=lambda p, pay, t: {}))
    spec = TaskSpec.validate({"task_id": "t", "request": "r", "steps": [
        {"id": "s", "run": run, "touches": []}]}, source="test")
    return torra._gate(spec)


def test_a_step_that_needs_a_tick_carries_a_preview(tmp_path: Path) -> None:
    root = _tree(tmp_path / "victim")

    plan = _plan_for(tmp_path, f"rm -r {root}")

    step = plan.steps[0]
    assert step.needs_confirmation
    assert step.preview == [f"{root}: directory, 3 files in 1 folder, 7.0 KiB, "
                            "newest change 2026-10-03"]
    assert plan.to_dict()["steps"][0]["preview"] == step.preview


def test_a_step_that_needs_no_tick_has_no_preview(tmp_path: Path) -> None:
    plan = _plan_for(tmp_path, f"ls {tmp_path}")

    assert not plan.steps[0].needs_confirmation
    assert plan.steps[0].preview == []


def test_the_preview_is_looked_up_once_when_first_asked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import torra.pipeline_types as types
    calls: list[Any] = []

    def counting(step: Any, **kwargs: Any) -> list[str]:
        calls.append(step)
        return ["looked up"]

    monkeypatch.setattr(types, "preview_targets", counting)
    plan = _plan_for(tmp_path, f"rm -r {tmp_path}/x")

    assert calls == [], "gating must not touch the disk"
    assert plan.steps[0].preview == ["looked up"] and plan.steps[0].preview == ["looked up"]
    assert len(calls) == 1


def test_the_tick_prompt_shows_what_the_step_will_act_on_before_it_asks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path / "victim")
    plan = _plan_for(tmp_path, f"rm -r {root}")
    shown_when_asked: list[str] = []

    def answer(prompt: str = "") -> str:
        shown_when_asked.append(capsys.readouterr().out)
        return "n"

    monkeypatch.setattr("builtins.input", answer)

    approvals = cli.approve_at_prompt(plan)

    assert approvals == {"s": False}
    assert len(shown_when_asked) == 1
    assert "on disk:" in shown_when_asked[0]
    assert "3 files in 1 folder" in shown_when_asked[0]


def test_the_prompt_prints_no_preview_block_for_a_step_with_nothing_to_show(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    plan = _plan_for(tmp_path, "systemctl restart nginx")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    cli.approve_at_prompt(plan)

    assert "on disk" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_plan_card_shows_the_preview(tmp_path: Path) -> None:
    from test_tui import build, screen_text
    from textual.widgets import Input

    root = _tree(tmp_path / "victim")
    app = build(tmp_path, scripted(plan_reply(
        "t", f"delete {root}", [{"id": "rm", "run": f"rm -r {root}", "touches": [str(root)]}])))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = f"delete {root}"
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert app.is_running
        assert "3 files in 1 folder" in screen_text(app)
