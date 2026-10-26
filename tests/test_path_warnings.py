"""A plan that acts on a path that is not there should say so before the operator is asked.

Seen live 2026-10-11 (real-use finding 2): devstral:24b planned `rm -r /home/user/Downloads/Telegram (2)`.
The home was `/home/alex` and the folder was `td-setup-linux-x64-7.2 (2).7`; nothing checked, the plan
reached the operator, and it failed only when step 1 ran. The check is advisory, like
`dependency_warnings`: a path can be made by an earlier step, so it never blocks.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from conftest import plan_reply, scripted
from test_agent import build_agent

from torra import cli
from torra.agent import Intent
from torra.core import OllamaClient, TaskSpec
from torra.path_warnings import missing_path_warnings, placeholder_tokens
from torra.pipeline import Torra

# --------------------------------------------------------------------------- #
# The check itself
# --------------------------------------------------------------------------- #


def _spec(*steps: dict[str, Any]) -> TaskSpec:
    return TaskSpec.validate({"task_id": "t", "request": "r", "steps": list(steps)}, source="test")


def _warn(*steps: dict[str, Any], exist: tuple[str, ...] = ()) -> list[str]:
    return missing_path_warnings(_spec(*steps), exists=lambda path: path in exist)


def _step(run: str, step_id: str = "s", **extra: Any) -> dict[str, Any]:
    return {"id": step_id, "run": run, **extra}


def test_a_missing_path_is_named_with_its_step() -> None:
    warnings = _warn(_step("ls -l /home/user/Downloads", "look"))

    assert len(warnings) == 1
    assert "look" in warnings[0]
    assert "/home/user/Downloads" in warnings[0]
    assert "does not exist" in warnings[0]


def test_a_path_that_exists_is_not_named() -> None:
    assert _warn(_step("ls -l /srv/api"), exist=("/srv/api",)) == []


def test_the_live_plan_is_flagged_step_by_step_and_touches_do_not_excuse_it() -> None:
    steps = [
        _step("find /home/user/Downloads -type d -name '*Telegram (2)*'", "find"),
        _step("ls -l '/home/user/Downloads/Telegram (2)'", "list"),
        _step(
            "rm -r '/home/user/Downloads/Telegram (2)'",
            "delete",
            touches=["/home/user/Downloads/Telegram (2)"],
        ),
    ]

    warnings = _warn(*steps, exist=("/home",))

    assert [w for w in warnings if "'find'" in w or "step find" in w], warnings
    assert any("Telegram (2)" in w and "delete" in w for w in warnings), (
        "a destructive step cannot make its own target exist by declaring it in touches"
    )
    assert len(warnings) == 3


@pytest.mark.parametrize(
    "earlier",
    [
        "mkdir -p /srv/new/sub",
        "touch /srv/new/sub",
        "echo hi > /srv/new/sub",
        "echo hi >> /srv/new/sub",
        "cp /etc/hostname /srv/new/sub",
        "mv /etc/hostname /srv/new/sub",
        "tee /srv/new/sub",
    ],
)
def test_a_path_an_earlier_step_creates_is_not_named(earlier: str) -> None:
    assert (
        _warn(_step(earlier, "make"), _step("cat /srv/new/sub", "use"), exist=("/etc/hostname",))
        == []
    )


def test_a_path_under_one_an_earlier_step_creates_is_not_named() -> None:
    assert _warn(_step("mkdir -p /srv/new", "make"), _step("ls /srv/new/deep/er", "use")) == []


def test_a_path_used_before_a_later_step_creates_it_is_named() -> None:
    warnings = _warn(_step("cat /srv/new/f", "use"), _step("touch /srv/new/f", "make"))

    assert len(warnings) == 1 and "use" in warnings[0]


def test_creating_and_using_a_path_in_one_step_is_fine() -> None:
    assert _warn(_step("mkdir -p /srv/new && cd /srv/new && ls /srv/new")) == []


def test_the_source_of_a_copy_is_checked_but_not_its_destination() -> None:
    warnings = _warn(_step("cp /srv/missing.conf /srv/backup/missing.conf"), exist=("/srv",))

    assert len(warnings) == 1
    assert "/srv/missing.conf" in warnings[0]
    assert "/srv/backup" not in warnings[0]


def test_a_path_inside_a_shell_payload_is_found() -> None:
    assert len(_warn(_step("sh -c 'ls /srv/gone'"))) == 1


def test_a_command_substitution_is_left_alone() -> None:
    """The resolver hands `$(...)` over as a placeholder; it is as unknowable as a variable.
    Found replaying past plans: `ls -l /proc/$(pgrep -n ollama)/fd` was flagged."""
    assert _warn(_step("ls -l /proc/$(pgrep -n ollama)/fd")) == []


def test_only_limits_which_steps_report_but_every_step_still_creates() -> None:
    spec = _spec(
        _step("ls /srv/gone", "look"),
        _step("rm -r /srv/gone2", "rm"),
        _step("mkdir -p /srv/new", "make"),
        _step("rm -r /srv/new/x", "rm2"),
    )

    warnings = missing_path_warnings(
        spec, exists=lambda p: False, only=lambda step: step.step_id.startswith("rm")
    )

    assert len(warnings) == 1 and "'rm'" in warnings[0], (
        "look is not reported, and rm2's path was made by make, which is not reported either"
    )


def test_a_value_after_equals_is_checked() -> None:
    assert len(_warn(_step("nginx -t --conf=/etc/nginx/gone.conf"))) == 1


@pytest.mark.parametrize(
    "run",
    [
        "ls /srv/$TARGET",  # a variable torra cannot know
        "ls /var/log/*.log",  # a glob
        "ls /srv/{a,b}",  # brace expansion
        "ls relative/dir",  # relative: depends on cwd
        "echo /srv/gone",  # text, not a path
        "printf '%s' /srv/gone",
        "grep -r /srv/gone",  # a pattern
        "sed -n '/srv/gone/p'",
        "awk '/srv/gone/' file",
        "ssh box ls /srv/gone",  # the path is on the other machine
        "scp box:/srv/gone .",
        "python3 -c 'print(1)'",
    ],
)
def test_things_that_are_not_local_paths_are_left_alone(run: str) -> None:
    assert _warn(_step(run)) == []


def test_a_remote_step_is_left_alone() -> None:
    step = {
        "id": "pull",
        "kind": "remote_pull",
        "run": "ignored",
        "remote_path": "/etc/app.conf",
        "touches": ["/tmp/app.conf"],
    }

    assert _warn(step) == []


def test_home_is_expanded_before_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/alex")
    seen: list[str] = []

    def exists(path: str) -> bool:
        seen.append(path)
        return True

    assert missing_path_warnings(_spec(_step("ls ~/Downloads")), exists=exists) == []
    assert seen == ["/home/alex/Downloads"]


@pytest.mark.parametrize(
    "run, expected",
    [
        ('rm -r "$HOME/Downloads/Telegram (2)"', "/home/alex/Downloads/Telegram (2)"),
        ("ls ${HOME}/x", "/home/alex/x"),
        ("du -sh /home/$USER", "/home/alex"),
        ("ls /home/${USER}/notes", "/home/alex/notes"),
    ],
)
def test_home_and_user_are_expanded_like_a_tilde(
    monkeypatch: pytest.MonkeyPatch, run: str, expected: str
) -> None:
    """Seen live 2026-10-12: gpt-oss:20b writes `"$HOME/Downloads/..."`, which the check skipped,
    so a guessed folder name went unflagged. Steps run as the operator, so these are known."""
    monkeypatch.setenv("HOME", "/home/alex")
    monkeypatch.setenv("USER", "alex")
    seen: list[str] = []

    def exists(path: str) -> bool:
        seen.append(path)
        return True

    missing_path_warnings(_spec(_step(run)), exists=exists)
    assert seen == [expected]


def test_an_unset_home_leaves_the_path_unknowable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USER", raising=False)

    assert _warn(_step('rm -r "$HOME/x" /home/$USER/y')) == []


@pytest.mark.parametrize(
    "run, expected",
    [
        ("rm -rf '<found-path>'", ["<found-path>"]),  # seen live 2026-10-12, devstral:24b
        ("ls -l /home/<user>/notes", ["<user>"]),
        ("kill -9 <PID>", ["<PID>"]),
        ("<command> --flag", ["<command>"]),
        ("rm -rf /srv/a", []),
        ("grep '<html>' /srv/a", []),  # a pattern, not a slot
        ("echo 'deleting <found-path>'", []),  # text that only prints
        ("diff <(ls a) <(ls b)", []),  # process substitution
        ("cat < /srv/in.txt", []),  # a redirect
    ],
)
def test_placeholder_tokens(run: str, expected: list[str]) -> None:
    assert placeholder_tokens(run) == expected


def test_one_warning_per_path_per_step() -> None:
    warnings = _warn(_step("ls /srv/gone && du -sh /srv/gone"))

    assert len(warnings) == 1


def test_checking_does_not_disturb_the_safety_classifiers_state() -> None:
    from torra.gates import SafetyClassifier

    safety = SafetyClassifier()
    safety.classify("ls /a")
    safety.parse_failures.append("kept")

    missing_path_warnings(_spec(_step("ls /gone")), exists=lambda p: False)

    assert safety.parse_failures == ["kept"]


# --------------------------------------------------------------------------- #
# In the agent: a repair round, then a warning
# --------------------------------------------------------------------------- #

#: Needs a tick (DANGEROUS file deletion) and declares touches, so the risk screen has nothing
#: to say about it: any repair round below is the path check's.
_MISSING = {"id": "rm", "run": "rm -f /data/old.log", "touches": ["/data/old.log"]}
_REAL = {"id": "ls", "run": "ls -l /srv/api"}


def _recording(*replies: dict[str, Any]):
    chats: list[dict[str, Any]] = []
    inner = scripted(*replies)

    def transport(path, payload, timeout):
        if path == "/api/chat":
            chats.append(payload)
        return inner(path, payload, timeout)

    return transport, chats


def _exists(*real: str) -> Callable[[str], bool]:
    return lambda path: path in real


def test_a_plan_naming_a_missing_path_gets_a_repair_round() -> None:
    transport, chats = _recording(
        plan_reply("t", "list /srv/api", [_MISSING]), plan_reply("t", "list /srv/api", [_REAL])
    )

    turn = build_agent(transport, path_exists=_exists("/srv/api")).converse("list /srv/api")

    assert turn.intent is Intent.PLAN
    assert turn.repairs == 1
    assert turn.warnings == []
    assert turn.spec is not None and turn.spec.commands() == ["ls -l /srv/api"]
    complaint = next(
        m["content"]
        for m in chats[1]["messages"]
        if m["role"] == "user" and "does not exist" in m["content"]
    )
    assert "/data/old.log" in complaint
    assert "do not guess" in complaint.lower(), "tell the model what to do instead"
    assert "single quotes" in complaint, (
        "the request's own path, copied exactly, is the first choice"
    )
    assert "cannot pass output" in complaint, (
        "live, 'use a command that finds it' produced find-then-act with a literal <found-path>"
    )


def test_a_path_still_missing_after_repairs_is_a_warning_not_a_refusal() -> None:
    """Unlike an unparseable step, this one may be right: the operator decides."""
    transport, chats = _recording(plan_reply("t", "list /srv/api", [_MISSING]))

    turn = build_agent(transport, max_repairs=2, path_exists=_exists()).converse("list /srv/api")

    assert turn.intent is Intent.PLAN
    assert turn.spec is not None
    assert len(chats) == 3
    assert len(turn.warnings) == 1 and "/data/old.log" in turn.warnings[0]


def test_the_path_check_is_off_unless_it_is_wired_in() -> None:
    transport, chats = _recording(plan_reply("t", "list /srv/api", [_MISSING]))

    turn = build_agent(transport).converse("list /srv/api")

    assert turn.repairs == 0 and turn.warnings == [] and len(chats) == 1


def test_the_agent_checks_paths_only_on_steps_that_need_a_tick() -> None:
    """Replaying 121 past plans, the check flagged 18% of them, mostly read-only probes such
    as `cat /etc/motd` or `ls /var/run/ollama.pid 2>/dev/null` that ask whether a file is there.
    A read-only step on a wrong path fails harmlessly; a destructive one is what matters."""
    probe = {"id": "probe", "run": "ls -l /home/user/Downloads/Telegram"}
    transport, chats = _recording(plan_reply("t", "list /srv/api", [probe]))

    turn = build_agent(transport, path_exists=_exists()).converse("list /srv/api")

    assert turn.repairs == 0 and turn.warnings == [] and len(chats) == 1


def test_a_plan_whose_paths_exist_spends_no_repair_round() -> None:
    transport, chats = _recording(plan_reply("t", "list /srv/api", [_REAL]))

    turn = build_agent(transport, path_exists=_exists("/srv/api")).converse("list /srv/api")

    assert turn.repairs == 0 and turn.warnings == [] and len(chats) == 1


# --------------------------------------------------------------------------- #
# In the plan and on the screen
# --------------------------------------------------------------------------- #


def test_the_plan_carries_the_warning_and_the_audit_trail_records_it(
    make_torra, tmp_path: Path
) -> None:
    torra = make_torra(
        scripted(plan_reply("t", "list /srv/api", [_MISSING])), path_exists=_exists()
    )
    torra.agent.max_repairs = 0

    result = torra.plan("list /srv/api")

    assert result.plan is not None
    assert len(result.plan.warnings) == 1 and "/data/old.log" in result.plan.warnings[0]
    assert result.plan.to_dict()["warnings"] == result.plan.warnings


def test_print_plan_shows_the_warning_above_the_steps(make_torra) -> None:
    torra = make_torra(
        scripted(plan_reply("t", "list /srv/api", [_MISSING])), path_exists=_exists()
    )
    torra.agent.max_repairs = 0
    plan = torra.plan("list /srv/api").plan
    assert plan is not None

    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_plan(plan)

    text = out.getvalue()
    assert "warning:" in text and "/data/old.log" in text
    assert text.index("warning:") < text.index("1. [")


def test_print_plan_prints_nothing_extra_when_there_is_no_warning(tmp_path: Path) -> None:
    spec = _spec(_step("df -h"))
    torra = Torra(workdir=tmp_path / "wd", ollama=OllamaClient(transport=lambda p, pay, t: {}))
    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_plan(torra._gate(spec))

    assert "warning:" not in out.getvalue()


def test_the_cli_wires_in_the_real_filesystem(tmp_path: Path) -> None:
    import os

    torra = cli.build_torra(tmp_path / "wd", offline=True)

    assert torra.agent.path_exists is os.path.exists


def test_every_tui_tab_gets_the_real_filesystem_check(tmp_path: Path) -> None:
    """Tabs are built by the TUI itself, not by `build_torra`."""
    import os
    from types import SimpleNamespace

    from torra.tui import TorraApp

    app = SimpleNamespace(
        ollama=OllamaClient(transport=lambda p, pay, t: {"models": []}), backends={}
    )
    torra = TorraApp._build_session_torra(app, tmp_path, "abc123")  # type: ignore[arg-type]

    assert torra.agent.path_exists is os.path.exists


@pytest.mark.asyncio
async def test_the_plan_pane_shows_the_warning(tmp_path: Path) -> None:
    from test_tui import build, screen_text
    from textual.widgets import Input

    app = build(
        tmp_path, scripted(plan_reply("t", "list /srv/api", [_MISSING])), path_exists=_exists()
    )

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "list /srv/api"
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert app.is_running
        text = screen_text(app)
        assert "does not exist" in text and "/data/old.log" in text


# --------------------------------------------------------------------------- #
# A placeholder left in a command
# --------------------------------------------------------------------------- #

_TEMPLATE = {"id": "rm", "run": "rm -rf '<found-path>'"}
_CONCRETE = {"id": "rm", "run": "rm -f /data/old.log", "touches": ["/data/old.log"]}


def test_a_placeholder_gets_a_repair_round_that_names_it() -> None:
    transport, chats = _recording(
        plan_reply("t", "delete /data/old.log", [_TEMPLATE]),
        plan_reply("t", "delete /data/old.log", [_CONCRETE]),
    )

    turn = build_agent(transport).converse("delete /data/old.log")

    assert turn.repairs == 1
    assert turn.spec is not None and turn.spec.commands() == ["rm -f /data/old.log"]
    complaint = next(
        m["content"]
        for m in chats[1]["messages"]
        if m["role"] == "user" and "placeholder" in m["content"]
    )
    assert "<found-path>" in complaint
    assert "cannot pass output" in complaint


def test_a_placeholder_still_there_after_repairs_never_reaches_the_operator() -> None:
    """torra runs the text as written: `rm -rf '<found-path>'` can never do what was meant."""
    from torra.agent import AgentError

    transport, chats = _recording(plan_reply("t", "delete /data/old.log", [_TEMPLATE]))

    with pytest.raises(AgentError, match="placeholder"):
        build_agent(transport, max_repairs=2).converse("delete /data/old.log")

    assert len(chats) == 3
