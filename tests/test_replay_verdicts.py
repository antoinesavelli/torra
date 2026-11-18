"""The verdict replay tool reads real commands from transcripts and audit files and diffs runs.

Depends on: scripts/dev/replay_verdicts.py (loaded by path: scripts/ is not a package),
torra.gates.SafetyClassifier (the real classifier; nothing is executed).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import warnings
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The replay module, loaded by path."""
    spec = importlib.util.spec_from_file_location(
        "replay_verdicts", ROOT / "scripts" / "dev" / "replay_verdicts.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _bash(command: str) -> dict[str, Any]:
    item = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"content": [item]}}


def _write_lines(path: Path, lines: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))


def _audit(path: Path, commands: list[str] | None) -> None:
    plan = None if commands is None else {"steps": [{"step": {"command": c}} for c in commands]}
    path.write_text(json.dumps({"plan": plan}))


def test_transcript_extracts_only_assistant_bash_calls(replay: ModuleType, tmp_path: Path) -> None:
    other_tool = {"type": "tool_use", "name": "Read", "input": {"command": "cat nope"}}
    user_record = {
        "type": "user",
        "message": {"content": [_bash("echo user")["message"]["content"][0]]},
    }
    _write_lines(
        tmp_path / "a.jsonl",
        [
            _bash("ls -la"),
            {"type": "assistant", "message": {"content": [other_tool]}},
            user_record,
            {"type": "assistant", "message": {"content": "text"}},
        ],
    )
    skipped: Counter[str] = Counter()
    assert list(replay.transcript_commands(tmp_path, skipped)) == ["ls -la"]


def test_transcript_searches_subfolders(replay: ModuleType, tmp_path: Path) -> None:
    _write_lines(tmp_path / "proj" / "sess" / "subagents" / "x.jsonl", [_bash("pwd")])
    assert list(replay.transcript_commands(tmp_path, Counter())) == ["pwd"]


def test_malformed_lines_and_unreadable_files_are_skipped_and_counted(
    replay: ModuleType, tmp_path: Path
) -> None:
    _write_lines(tmp_path / "a.jsonl", ["{not json", "[1, 2]", _bash("whoami")])
    (tmp_path / "dir.jsonl").mkdir()  # reading a directory raises OSError
    skipped: Counter[str] = Counter()
    assert list(replay.transcript_commands(tmp_path, skipped)) == ["whoami"]
    assert skipped["records"] == 2
    assert skipped["files"] == 1


def test_audit_extracts_plan_steps_and_ignores_null_plan(
    replay: ModuleType, tmp_path: Path
) -> None:
    _audit(tmp_path / "1.audit.json", ["df -h", "uptime"])
    _audit(tmp_path / "2.audit.json", None)
    (tmp_path / "3.audit.json").write_text("{broken")
    (tmp_path / "ignored.json").write_text("{}")
    skipped: Counter[str] = Counter()
    assert sorted(replay.audit_commands(tmp_path, skipped)) == ["df -h", "uptime"]
    assert skipped["files"] == 1


class _Fake:
    """A stand-in verdict: level "safe" for command "b", "caution" otherwise."""

    def __init__(self, command: str) -> None:
        self.level = type("L", (), {"value": "safe" if command == "b" else "caution"})()
        self.resolved = [
            type("R", (), {"program": p})() for p in ("git", "grep", "git") if command == "a"
        ]


def test_classify_all_dedups_across_sources_drops_empty_and_sorts(replay: ModuleType) -> None:
    records = replay.classify_all(["b", "a", "b", "", "  "], _Fake)
    assert records == [
        {"command": "a", "level": "caution", "programs": ["git", "grep"]},
        {"command": "b", "level": "safe", "programs": []},
    ]


def test_a_classifier_crash_becomes_an_error_record(replay: ModuleType) -> None:
    def classify(command: str) -> Any:
        if command == "boom":
            raise MemoryError
        return _Fake(command)

    records = replay.classify_all(["boom", "b"], classify)
    assert records[0] == {"command": "b", "level": "safe", "programs": []}
    assert records[1] == {"command": "boom", "level": "error", "programs": []}
    assert replay.level_counts(r["level"] for r in records) == {"error": 1, "safe": 1}


def test_real_classifier_levels(replay: ModuleType) -> None:
    from torra.gates import SafetyClassifier

    records = replay.classify_all(["ls -la", "rm -rf /"], SafetyClassifier().classify)
    levels = {r["command"]: r["level"] for r in records}
    assert levels["ls -la"] == "safe"
    assert levels["rm -rf /"] != "safe"
    assert records[0]["programs"] == ["ls"]


def test_jsonl_round_trip_and_level_counts(replay: ModuleType, tmp_path: Path) -> None:
    records = [
        {"command": "a b", "level": "safe", "programs": ["a"]},
        {"command": "c", "level": "critical", "programs": []},
    ]
    replay.write_jsonl(records, tmp_path / "out" / "r.jsonl")
    back = replay.read_jsonl(tmp_path / "out" / "r.jsonl")
    assert back == {r["command"]: r for r in records}
    assert replay.level_counts(["safe", "safe", "critical"]) == {"critical": 1, "safe": 2}


def _runs(**levels: str) -> dict[str, dict[str, Any]]:
    return {c: {"command": c, "level": lv} for c, lv in levels.items()}


def test_diff_groups_changed_added_and_removed_by_program(replay: ModuleType) -> None:
    a = _runs(**{"curl http://x": "safe", "ls": "safe", "gone --x": "caution", "same": "safe"})
    b = _runs(
        **{"curl http://x": "dangerous", "ls": "safe", "new 'quoted": "caution", "same": "safe"}
    )
    moves = replay.diff(a, b)
    assert moves["curl"] == [("curl http://x", "safe", "dangerous")]
    assert moves["gone"] == [("gone --x", "caution", "-")]
    assert moves["new"] == [("new 'quoted", "-", "caution")]  # unparsable quote falls back to split
    assert "ls" not in moves and "same" not in moves
    assert replay.format_diff(moves)[0] == "curl: 1 (safe->dangerous 1)"


def test_diff_lists_a_command_under_every_program_but_cd_and_keeps_entries_on_one_line(
    replay: ModuleType,
) -> None:
    command = "cd /tmp\ngit status | grep x"
    only_cd = "cd /tmp"
    a = {
        command: {"command": command, "level": "safe", "programs": ["cd", "git", "grep"]},
        only_cd: {"command": only_cd, "level": "safe", "programs": ["cd"]},
    }
    b = {
        command: {**a[command], "level": "caution"},
        only_cd: {**a[only_cd], "level": "caution"},
    }
    moves = replay.diff(a, b)
    assert sorted(moves) == ["cd", "git", "grep"]
    assert [m[0] for m in moves["git"]] == [command] == [m[0] for m in moves["grep"]]
    assert [m[0] for m in moves["cd"]] == [only_cd]  # cd only when it is the sole program
    entry = next(x for x in replay.format_diff(moves) if "git status" in x)
    assert "\n" not in entry and json.dumps(command) in entry


def _moved(replay: ModuleType, command: str, programs: Any) -> list[str]:
    """The group names one safe->caution move of `command` lands under."""
    a = {command: {"command": command, "level": "safe", "programs": programs}}
    b = {command: {"command": command, "level": "caution", "programs": programs}}
    return list(replay.diff(a, b))


def test_a_non_list_programs_value_falls_back_to_the_first_word(replay: ModuleType) -> None:
    assert _moved(replay, "git status", "git+grep") == [
        "git"
    ]  # an old "+"-joined string, not per character
    assert _moved(replay, "git status", None) == ["git"]
    assert _moved(replay, "git status", 7) == ["git"]


def test_group_keys_are_plain_names_with_a_one_line_fallback(replay: ModuleType) -> None:
    assert _moved(replay, "x", ["$(evil)", "git", "a\nb", 3, "python3.12", "g++"]) == [
        "g++",
        "git",
        "python3.12",
    ]
    assert _moved(replay, "x", ["cd", "$(evil)"]) == [
        "cd"
    ]  # cd survives when it is the only plain name
    assert _moved(replay, "$HOME/bin/tool a", ["$(evil)"]) == ['"$HOME/bin/tool"']
    assert _moved(replay, "", []) == ['""']
    assert "\n" not in _moved(replay, "a\nb", ["a\nb"])[0]


def test_bash_tool_name_variants_are_read(replay: ModuleType, tmp_path: Path) -> None:
    def call(name: str, command: str) -> dict[str, Any]:
        item = {"type": "tool_use", "name": name, "input": {"command": command}}
        return {"type": "assistant", "message": {"content": [item]}}

    _write_lines(
        tmp_path / "a.jsonl",
        [call("bash", "one"), call("mcp__terminal__run_in_terminal", "two"), call("Grep", "no")],
    )
    assert list(replay.transcript_commands(tmp_path, Counter())) == ["one", "two"]


def test_main_replays_then_diffs(
    replay: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    projects, audit = tmp_path / "projects", tmp_path / "audit"
    audit.mkdir()
    _write_lines(projects / "p" / "s.jsonl", [_bash("ls -la"), _bash("rm -rf /")])
    _audit(audit / "1.audit.json", ["ls -la", "uptime"])
    out = tmp_path / "run.jsonl"
    assert replay.main(["--out", str(out), "--projects", str(projects), "--audit", str(audit)]) == 0
    printed = capsys.readouterr().out
    assert "transcripts=2 audit=2" in printed
    assert "skipped: files=0 records=0" in printed
    assert "commands: 3 distinct" in printed
    levels = replay.read_jsonl(out)
    assert set(levels) == {"ls -la", "rm -rf /", "uptime"}

    other = tmp_path / "run2.jsonl"
    replay.write_jsonl([{"command": "ls -la", "level": "critical"}], other)  # old file: no programs
    assert replay.main(["--diff", str(out), str(other)]) == 0
    printed = capsys.readouterr().out
    assert "moved: 3 distinct commands" in printed
    assert "ls: 1 (safe->critical 1)" in printed
    assert "uptime: 1 (" in printed


def test_main_refuses_a_missing_source_dir_and_both_modes(
    replay: ModuleType, tmp_path: Path
) -> None:
    out = str(tmp_path / "run.jsonl")
    with pytest.raises(SystemExit) as missing:
        replay.main(["--out", out, "--projects", str(tmp_path / "nope"), "--audit", str(tmp_path)])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as both:
        replay.main(["--out", out, "--diff", out, out])
    assert both.value.code == 2
    assert not (tmp_path / "run.jsonl").exists()


def test_replay_ignores_every_warning_so_runs_stay_comparable(
    replay: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects, audit = tmp_path / "projects", tmp_path / "audit"
    audit.mkdir()
    # A redirect to `$'\s'` makes the gate warn (DeprecationWarning), `\d` in `python -c` (SyntaxWarning).
    risky = ["echo x > $'\\s'", "python3 -c 'print(\"\\d\")'"]
    _write_lines(projects / "s.jsonl", [_bash(c) for c in risky])
    out = tmp_path / "run.jsonl"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        args = ["--out", str(out), "--projects", str(projects), "--audit", str(audit)]
        assert replay.main(args) == 0
    levels = {c: r["level"] for c, r in replay.read_jsonl(out).items()}
    assert levels == {risky[0]: "caution", risky[1]: "safe"}
