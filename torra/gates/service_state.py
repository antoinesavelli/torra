"""Reading and restoring systemd unit state for a step's undo.

Split out of undo.py when the enablement handling pushed it past the 500-line cap
(HANDOFF.md §2.63).

Depends on: the standard library only.
"""

from __future__ import annotations

import re
import subprocess

SERVICE_PATTERN = re.compile(
    r"\bsystemctl\b([^;&|\n]*)", re.IGNORECASE)
SERVICE_VERBS = frozenset({"start", "stop", "restart", "enable", "disable", "mask", "unmask"})


def service_units(command: str) -> list[str]:
    """Units a `systemctl start|stop|restart|enable|disable|mask|unmask` in `command` acts on.

    Read from the arguments, not a fixed shape: flags (`--now`, `--user`) are skipped, and
    `systemctl restart a b` names two units."""
    units: list[str] = []
    for match in SERVICE_PATTERN.finditer(command):
        words = [w for w in match.group(1).split() if not w.startswith("-")]
        if words and words[0].lower() in SERVICE_VERBS:
            units += [w for w in words[1:] if re.fullmatch(r"[\w@.\-:]+", w)]
    return units


# Live wire: HANDOFF.md §5.20
def systemctl_scope(unit: str) -> str:
    """'user' when systemd's user instance knows this unit, else 'system'.

    `is-active` alone can't tell "not loaded in this scope" apart from "loaded and genuinely
    inactive" -- both report inactive/exit-4 under --user for a unit that is actually a system
    service. LoadState does distinguish them ("not-found" vs "loaded"). Found live:
    `ollama.service` is a system unit on this project's reference machine, and every systemctl
    call here once hardcoded --user, so a generated undo probed and acted on the wrong systemd
    instance entirely. Duplicated from the identical check in gates/state.py rather than
    imported: the gates are deliberately self-contained (gates/__init__.py's own docstring).
    """
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=LoadState"],
            capture_output=True, text=True, timeout=5)
        if out.stdout.strip() == "LoadState=loaded":
            return "user"
    except Exception:
        pass
    return "system"


def systemctl_query(verb: str, unit: str, scope: str) -> str:
    """Output of `systemctl is-active|is-enabled <unit>` in the unit's scope, or 'unknown'."""
    cmd = ["systemctl", *(["--user"] if scope == "user" else []), verb, unit]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def restore_verbs(enabled_now: str, active_before: str, enabled_before: str) -> list[str]:
    """The systemctl verbs that put a unit's enablement and running state back, in order."""
    verbs: list[str] = []
    if enabled_now == "masked" and enabled_before != "masked":
        verbs.append("unmask")  # whatever it was: static and indirect units can be masked too
    if enabled_now != enabled_before and enabled_before in ("enabled", "disabled"):
        verbs.append("enable" if enabled_before == "enabled" else "disable")
    if active_before == "active":
        verbs.append("start")
    elif active_before != "unknown":
        verbs.append("stop")  # never stop a unit whose state could not be read
    if enabled_before == "masked" and enabled_now != "masked":
        verbs.append("mask")
    return verbs
