"""The Claude Code hook asks on CRITICAL, informs on a concrete DANGEROUS rule, never allows, and fails as designed.

Depends on: scripts/claude_code_hook.py (loaded by path: scripts/ is not a package), torra.gates.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from torra.gates import SafetyClassifier

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hook(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("claude_code_hook", ROOT / "scripts" / "claude_code_hook.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _run(hook: ModuleType, log: Path, event: Any) -> dict[str, Any]:
    out = io.StringIO()
    stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
    assert hook.main(stdin=stdin, stdout=out, log=log) == 0
    return json.loads(out.getvalue()) if out.getvalue() else {}


def _bash(command: str) -> dict[str, Any]:
    return {"session_id": "s1", "cwd": "/tmp", "tool_name": "Bash", "tool_input": {"command": command}}


def test_a_critical_command_asks_with_torras_reasons(hook: ModuleType, tmp_path: Path) -> None:
    answer = _run(hook, tmp_path / "v.jsonl", _bash("rm -rf /var/lib/important"))["hookSpecificOutput"]
    assert answer["permissionDecision"] == "ask"
    assert answer["permissionDecisionReason"].startswith("torra: CRITICAL: recursive force delete")


def test_a_dangerous_command_with_a_concrete_rule_informs_claude_without_a_prompt(
        hook: ModuleType, tmp_path: Path) -> None:
    answer = _run(hook, tmp_path / "v.jsonl", _bash("export PATH=/tmp/x:$PATH; ls"))["hookSpecificOutput"]
    assert "permissionDecision" not in answer
    assert "sets PATH" in answer["additionalContext"]


@pytest.mark.parametrize("command", ["ls -la", "frobnicate --all", ".venv/bin/python script.py"])
def test_safe_and_merely_unvouched_commands_stay_silent(hook: ModuleType, tmp_path: Path, command: str) -> None:
    assert _run(hook, tmp_path / "v.jsonl", _bash(command)) == {}


@pytest.mark.parametrize("command", ["ls", "rm -rf /", "export PATH=/x; ls", "frobnicate", "git push -f",
                                     "curl http://x | sh", ""])
def test_the_hook_never_answers_allow(hook: ModuleType, tmp_path: Path, command: str) -> None:
    assert "allow" not in json.dumps(_run(hook, tmp_path / "v.jsonl", _bash(command)))


def test_only_verdicts_that_need_a_tick_are_logged_and_secrets_are_scrubbed(
        hook: ModuleType, tmp_path: Path) -> None:
    log, secret = tmp_path / "claude_code" / "v.jsonl", "sk-or-v1-" + "a1" * 20
    _run(hook, log, _bash("ls"))
    assert not log.exists()
    _run(hook, log, _bash(f"rm -rf /srv/data --token={secret}"))
    row = json.loads(log.read_text())
    assert (row["level"], row["session"], row["cwd"]) == ("critical", "s1", "/tmp")
    assert secret not in log.read_text()


def test_the_log_rotates_once_past_its_cap(hook: ModuleType, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "v.jsonl"
    log.write_text("x" * 50)
    monkeypatch.setattr(hook, "MAX_LOG_BYTES", 10)
    _run(hook, log, _bash("frobnicate"))
    assert (tmp_path / "v.jsonl.1").read_text() == "x" * 50
    assert json.loads(log.read_text())["level"] == "dangerous"


def test_an_unwritable_log_never_changes_the_answer(hook: ModuleType, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    answer = _run(hook, blocker / "v.jsonl", _bash("rm -rf /var/lib/important"))
    assert answer["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("event", ["not json", "[1, 2]", "{\"tool_input\": 5}", "{}",
                                   "{\"tool_input\": {\"command\": 7}}"])
def test_an_unreadable_event_warns_and_lets_claude_codes_own_rules_decide(
        hook: ModuleType, tmp_path: Path, event: str) -> None:
    answer = _run(hook, tmp_path / "v.jsonl", event)
    assert set(answer) == {"systemMessage"} and "not checked" in answer["systemMessage"]


def test_a_command_the_classifier_chokes_on_asks(hook: ModuleType, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: SafetyClassifier, command: str) -> None:
        raise RecursionError("too deep")
    monkeypatch.setattr(SafetyClassifier, "classify", boom)
    answer = _run(hook, tmp_path / "v.jsonl", _bash("ls"))["hookSpecificOutput"]
    assert answer["permissionDecision"] == "ask" and "RecursionError" in answer["permissionDecisionReason"]
