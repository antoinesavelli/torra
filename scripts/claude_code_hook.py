"""Claude Code PreToolUse hook: torra's SafetyClassifier judges every Bash command Claude is about to run.

CRITICAL answers "ask", so the operator ticks it in Claude Code's own prompt. A DANGEROUS verdict that names
a concrete rule (a changed PATH, a system path, a credentials file) reaches Claude as context; one that only
says torra cannot vouch for the program is too common to show (41.6% of 84,078 commands measured 2026-10-23).
The hook never answers "allow", so Claude Code's own permission rules still decide everything else. Each
verdict that needs a tick is appended, scrubbed, to ~/.torra/claude_code/verdicts.jsonl for tuning.

If torra cannot load or the event is unreadable, the command goes through with a warning: torra is a
second gate here, and a broken one must not stop every session. A command the classifier chokes on asks.

Depends on: torra.gates.safety (SafetyClassifier, RiskLevel, Verdict), torra.telemetry.records
(redact_secrets), torra.pipeline_types (DEFAULT_WORKDIR). Wired in ~/.claude/settings.json (README.md).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from torra.gates.safety import RiskLevel, SafetyClassifier, Verdict
from torra.pipeline_types import DEFAULT_WORKDIR
from torra.telemetry.records import redact_secrets

LOG = DEFAULT_WORKDIR / "claude_code" / "verdicts.jsonl"
MAX_LOG_BYTES = 20_000_000
UNVOUCHED = "DANGEROUS: unrecognised program"


def _ask(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def decide(verdict: Verdict) -> dict[str, Any]:
    """The hook's answer for one verdict; empty leaves the call to Claude Code's own rules."""
    if verdict.level is RiskLevel.CRITICAL:
        return _ask("torra: " + "; ".join(verdict.reasons))
    specific = [
        r for r in verdict.reasons if r.startswith("DANGEROUS") and not r.startswith(UNVOUCHED)
    ]
    if verdict.level is RiskLevel.DANGEROUS and specific:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "torra: " + "; ".join(specific),
            }
        }
    return {}


def record(event: dict[str, Any], verdict: Verdict, log: Path) -> None:
    """Append one scrubbed verdict, keeping a single rotated file once the log passes MAX_LOG_BYTES."""
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > MAX_LOG_BYTES:
        log.replace(log.with_name(log.name + ".1"))
    row = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "session": event.get("session_id", ""),
        "cwd": event.get("cwd", ""),
        "level": verdict.level.value,
        "command": redact_secrets(verdict.command)[:500],
        "reasons": [redact_secrets(r) for r in verdict.reasons],
    }
    with log.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row) + "\n")


def main(stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout, log: Path = LOG) -> int:
    """Read one PreToolUse event, print the answer (if any). Always exits 0: the answer is the JSON."""
    try:
        event = json.load(stdin)
        command = event["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError(f"command is {type(command).__name__}, not a string")
    except (ValueError, KeyError, TypeError) as exc:
        stdout.write(
            json.dumps(
                {
                    "systemMessage": f"torra gate could not read the hook event ({exc}); "
                    "the command was not checked"
                }
            )
        )
        return 0
    try:
        verdict = SafetyClassifier().classify(command)
    except Exception as exc:  # noqa: BLE001 - a command that crashes the classifier is unseen, so it asks
        stdout.write(
            json.dumps(
                _ask(f"torra could not classify this command ({type(exc).__name__}), so it asks")
            )
        )
        return 0
    answer = decide(verdict)
    if verdict.needs_confirmation:
        try:
            record(event, verdict, log)
        except OSError:
            pass  # the log is for tuning; losing a row never changes the answer
    if answer:
        stdout.write(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
