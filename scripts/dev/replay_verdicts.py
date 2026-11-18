#!/usr/bin/env python3
"""Replay every real shell command seen so far through SafetyClassifier and diff runs.

Depends on: torra.gates.SafetyClassifier (in-process: nothing is executed), stdlib only
otherwise. Sources: Claude Code transcripts (Bash tool calls) and torra audit files.
Run once per commit with `--out FILE`, then `--diff A B` shows which verdicts moved.
Read-only: never edits code or the source files.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import time
import warnings
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from torra.gates import SafetyClassifier, Verdict

Record = dict[str, Any]  # {"command", "level", "programs": [names]}

PLAIN_NAME = re.compile(r"[\w.+-]+")

#: Tool names whose `input.command` is a shell command.
BASH_TOOLS = frozenset({"Bash", "bash", "mcp__terminal__run_in_terminal"})


def _records(path: Path, skipped: Counter[str]) -> Iterator[dict[str, Any]]:
    """Yield each JSON object line of one JSONL file; count what cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        skipped["files"] += 1
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped["records"] += 1
            continue
        if isinstance(record, dict):
            yield record
        else:
            skipped["records"] += 1


def _bash_command(item: object) -> str | None:
    """The command of one Bash tool_use content item, or None when it is not one."""
    if not isinstance(item, dict) or item.get("type") != "tool_use":
        return None
    if item.get("name") not in BASH_TOOLS:
        return None
    args = item.get("input")
    command = args.get("command") if isinstance(args, dict) else None
    return command if isinstance(command, str) else None


def transcript_commands(projects: Path, skipped: Counter[str]) -> Iterator[str]:
    """Every Bash command in every *.jsonl under `projects`, subfolders included."""
    for path in sorted(projects.rglob("*.jsonl")):
        for record in _records(path, skipped):
            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                skipped["records"] += 1
                continue
            for item in content:
                command = _bash_command(item)
                if command is not None:
                    yield command


def _audit_steps(path: Path, skipped: Counter[str]) -> Iterator[str]:
    """The step commands of one audit file's plan (none when the plan is null)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        skipped["files"] += 1
        return
    plan = data.get("plan") if isinstance(data, dict) else None
    if plan is None:
        return
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        skipped["records"] += 1
        return
    for item in steps:
        step = item.get("step") if isinstance(item, dict) else None
        command = step.get("command") if isinstance(step, dict) else None
        if isinstance(command, str):
            yield command
        else:
            skipped["records"] += 1


def audit_commands(audit: Path, skipped: Counter[str]) -> Iterator[str]:
    """Every planned step command in every *.audit.json under `audit`."""
    for path in sorted(audit.rglob("*.audit.json")):
        yield from _audit_steps(path, skipped)


def _programs(verdict: Verdict) -> list[str]:
    """The programs the classifier resolved, unique, in order."""
    return list(dict.fromkeys(r.program for r in verdict.resolved if r.program))


def _record(command: str, classify: Callable[[str], Verdict]) -> Record:
    """One command's record; a classifier crash becomes level "error", never an abort."""
    try:
        verdict = classify(command)
    except Exception:  # noqa: BLE001 - one bad command must not abort a 100k-command run
        return {"command": command, "level": "error", "programs": []}
    return {"command": command, "level": verdict.level.value, "programs": _programs(verdict)}


def classify_all(commands: Iterable[str], classify: Callable[[str], Verdict]) -> list[Record]:
    """One {"command", "level", "programs"} record per distinct non-empty command, sorted."""
    return [_record(c, classify) for c in sorted({c for c in commands if c.strip()})]


def write_jsonl(records: Iterable[Record], out: Path) -> None:
    """Write one JSON object per line."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def read_jsonl(path: Path) -> dict[str, Record]:
    """Read a replay file back as {command: record}."""
    records: dict[str, Record] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[record["command"]] = record
    return records


def level_counts(levels: Iterable[str]) -> dict[str, int]:
    """How many commands sit at each level, sorted by level name."""
    return dict(sorted(Counter(levels).items()))


def program_of(command: str) -> str:
    """The first shell word of a command (str.split when shlex cannot parse it)."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    return words[0] if words else ""


Moves = dict[str, list[tuple[str, str, str]]]


def _groups(record: Record) -> list[str]:
    """The group names of a record: its plain resolved programs (no `cd` unless alone), else the
    first shell word (json-quoted when it is not a plain name, so a header stays on one line)."""
    programs = record.get("programs")
    plain = (
        [p for p in programs if isinstance(p, str) and PLAIN_NAME.fullmatch(p)]
        if isinstance(programs, list)
        else []
    )
    names = [p for p in plain if p != "cd"] or plain
    if names:
        return names
    word = program_of(record["command"])
    return [word if PLAIN_NAME.fullmatch(word) else json.dumps(word)]


def diff(a: dict[str, Record], b: dict[str, Record]) -> Moves:
    """Moves from run A to run B grouped by program: (command, old, new); "-" = absent."""
    moves: Moves = defaultdict(list)
    for command in sorted(a.keys() | b.keys()):
        old = a.get(command, {}).get("level", "-")
        new = b.get(command, {}).get("level", "-")
        if old != new:
            for program in _groups(b.get(command) or a[command]):
                moves[program].append((command, old, new))
    return dict(sorted(moves.items()))


def format_diff(moves: Moves) -> list[str]:
    """Lines like `curl: 3 (safe->dangerous 3)` followed by each moved command, one line each."""
    lines: list[str] = []
    for program, items in moves.items():
        kinds = Counter(f"{old}->{new}" for _, old, new in items)
        summary = ", ".join(f"{kind} {n}" for kind, n in sorted(kinds.items()))
        lines.append(f"{program}: {len(items)} ({summary})")
        lines.extend(f"  {old}->{new}  {json.dumps(command)}" for command, old, new in items)
    return lines


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in counts.items()) or "none"


def _replay(args: argparse.Namespace) -> int:
    skipped: Counter[str] = Counter()
    started = time.monotonic()
    from_transcripts = set(transcript_commands(Path(args.projects).expanduser(), skipped))
    from_audit = set(audit_commands(Path(args.audit).expanduser(), skipped))
    with warnings.catch_warnings():
        # The gate's ast.parse and `$'...'` decoding warn on odd payloads; -W error would turn
        # one command into "error", so every run ignores all warnings and stays comparable.
        warnings.simplefilter("ignore")
        records = classify_all(from_transcripts | from_audit, SafetyClassifier().classify)
    write_jsonl(records, Path(args.out))
    print(f"commands: {len(records)} distinct in {time.monotonic() - started:.1f}s")
    print(f"sources: transcripts={len(from_transcripts)} audit={len(from_audit)}")
    print(f"skipped: files={skipped['files']} records={skipped['records']}")
    print(f"levels: {_fmt_counts(level_counts(r['level'] for r in records))}")
    return 0


def _show_diff(path_a: str, path_b: str) -> int:
    a, b = read_jsonl(Path(path_a)), read_jsonl(Path(path_b))
    print(f"A levels: {_fmt_counts(level_counts(r['level'] for r in a.values()))}")
    print(f"B levels: {_fmt_counts(level_counts(r['level'] for r in b.values()))}")
    moves = diff(a, b)
    print(f"moved: {len({c for items in moves.values() for c, _, _ in items})} distinct commands")
    lines = format_diff(moves)
    print("\n".join(lines) if lines else "no moves")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: `--out FILE` replays; `--diff A B` compares two saved runs."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", help="write the replay JSONL here")
    mode.add_argument("--diff", nargs=2, metavar=("A", "B"), help="compare two replay files")
    parser.add_argument("--projects", default="~/.claude/projects", help="Claude Code projects dir")
    parser.add_argument("--audit", default="~/.torra/audit", help="torra audit dir")
    args = parser.parse_args(argv)
    if args.diff:
        return _show_diff(*args.diff)
    for name in ("projects", "audit"):
        if not Path(getattr(args, name)).expanduser().is_dir():
            parser.error(f"--{name} is not a directory: {getattr(args, name)}")
    return _replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
