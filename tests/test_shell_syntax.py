"""A step that bash cannot parse must not reach the operator looking routine.

Seen live 2026-10-11: devstral:24b proposed `ls -l /home/user/Downloads/Telegram (2)` and
`rm -r` on the same unquoted path. Both are syntax errors in bash, the classifier rated the `ls`
safe, and the operator was asked to approve a plan that could never run. The risk call was
right; nothing asked whether the command was runnable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import plan_reply, scripted
from test_agent import build_agent

from torra.agent import AgentError, Intent
from torra.core.process import shell_syntax_error

# --------------------------------------------------------------------------- #
# The check itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "ls -l /srv/api",
        "ls -l '/home/alex/Downloads/Telegram (2)'",
        "du -ah /home/$USER | sort -rh | head -n 20",
        "find /srv -type d -name '*Telegram (2)*'",
        "if [ -d /srv ]; then echo yes; fi",
        "cat <<EOF\nhello (2)\nEOF",
        "echo a\necho b",
    ],
)
def test_valid_bash_has_no_syntax_error(command: str) -> None:
    assert shell_syntax_error(command) is None


@pytest.mark.parametrize(
    "command, fragment",
    [
        ("ls -l /home/user/Downloads/Telegram (2)", "("),
        ("rm -r /home/user/Downloads/Telegram (2)", "("),
        ("echo 'unterminated", "unexpected EOF"),
        ("if true; then echo x", "unexpected end of file"),
        ("echo a\nls (2)", "line 2"),
    ],
)
def test_invalid_bash_is_reported_in_bashs_own_words(command: str, fragment: str) -> None:
    message = shell_syntax_error(command)
    assert message is not None
    assert fragment in message
    assert "bash: -c:" not in message, "the noise bash puts in front of its message is dropped"


def test_checking_a_command_never_runs_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    assert shell_syntax_error(f"touch {marker}; echo $(touch {marker})") is None
    assert not marker.exists()


def test_checking_a_command_does_not_source_bash_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bash -c` reads $BASH_ENV; a checker that inherited one would run that file."""
    marker = tmp_path / "sourced"
    startup = tmp_path / "startup.sh"
    startup.write_text(f"touch {marker}\n")
    monkeypatch.setenv("BASH_ENV", str(startup))
    monkeypatch.setenv("ENV", str(startup))

    assert shell_syntax_error("ls /srv") is None
    assert not marker.exists()


def test_a_hung_checker_is_reported_as_no_opinion_not_an_error(monkeypatch) -> None:
    """The check is a courtesy: if bash cannot answer, the plan goes on to the real gates."""
    import subprocess

    def hang(*args: Any, **kwargs: Any):
        raise subprocess.TimeoutExpired(cmd="bash", timeout=1)

    monkeypatch.setattr(subprocess, "run", hang)
    assert shell_syntax_error("ls /srv") is None


# --------------------------------------------------------------------------- #
# In the agent: a repair round, then a refusal
# --------------------------------------------------------------------------- #

_UNQUOTED = {"id": "ls", "run": "ls -l /srv/api/Telegram (2)"}
_QUOTED = {"id": "ls", "run": "ls -l '/srv/api/Telegram (2)'"}


def _recording(*replies: dict[str, Any]):
    chats: list[dict[str, Any]] = []
    inner = scripted(*replies)

    def transport(path, payload, timeout):
        if path == "/api/chat":
            chats.append(payload)
        return inner(path, payload, timeout)

    return transport, chats


def test_a_step_bash_cannot_parse_gets_a_repair_round() -> None:
    transport, chats = _recording(
        plan_reply("t", "list /srv/api", [_UNQUOTED]), plan_reply("t", "list /srv/api", [_QUOTED])
    )

    turn = build_agent(transport).converse("list /srv/api")

    assert turn.intent is Intent.PLAN
    assert turn.repairs == 1
    assert turn.spec is not None
    assert turn.spec.commands() == ["ls -l '/srv/api/Telegram (2)'"]
    complaint = next(
        m["content"]
        for m in chats[1]["messages"]
        if m["role"] == "user" and "not valid bash" in m["content"]
    )
    assert "step 'ls'" in complaint
    assert "syntax error" in complaint
    assert "quote" in complaint.lower(), "the complaint should say what usually fixes it"


def test_only_the_steps_that_do_not_parse_are_named() -> None:
    transport, chats = _recording(
        plan_reply("t", "list /srv/api", [{"id": "fine", "run": "ls /srv/api"}, _UNQUOTED]),
        plan_reply("t", "list /srv/api", [{"id": "fine", "run": "ls /srv/api"}, _QUOTED]),
    )

    build_agent(transport).converse("list /srv/api")

    complaint = next(
        m["content"]
        for m in chats[1]["messages"]
        if m["role"] == "user" and "not valid bash" in m["content"]
    )
    assert "step 'ls'" in complaint
    assert "step 'fine'" not in complaint


def test_a_plan_that_is_still_not_valid_bash_after_repairs_never_reaches_the_operator() -> None:
    """There is no decision to leave to the human: bash would refuse the step on its own."""
    transport, chats = _recording(plan_reply("t", "list /srv/api", [_UNQUOTED]))

    with pytest.raises(AgentError, match="not valid bash"):
        build_agent(transport, max_repairs=2).converse("list /srv/api")

    assert len(chats) == 3, "one proposal plus exactly max_repairs revisions"


def test_the_syntax_check_is_injectable_like_the_agents_other_dependencies() -> None:
    transport, chats = _recording(plan_reply("t", "list /srv/api", [_UNQUOTED]))

    turn = build_agent(transport, syntax_check=lambda command: None).converse("list /srv/api")

    assert turn.repairs == 0
    assert len(chats) == 1


def test_a_valid_plan_does_not_spend_a_repair_round_on_the_syntax_check() -> None:
    transport, chats = _recording(plan_reply("t", "list /srv/api", [_QUOTED]))

    turn = build_agent(transport).converse("list /srv/api")

    assert turn.repairs == 0
    assert len(chats) == 1
