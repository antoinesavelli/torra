"""Best-effort warnings about a step that names a local path which is not there.

Depends on: torra.core (TaskSpec), torra.gates (SafetyClassifier.resolve). Like
`dependency_warnings`, this is advisory and not one of the gates: a plan may create the path
itself (`mkdir -p /srv/new` then `cp x /srv/new/`), and torra has no model of what a program
does with an argument. It reads the argv `SafetyClassifier.resolve` already produces, so
`sh -c` payloads, `sudo` and the like are seen through, and it is deliberately lenient where
it cannot tell: an unknown program's absolute-path arguments are checked, a creating program's
are trusted. A miss costs the operator a failed first step, which is what it cost before.

Seen live 2026-10-11: devstral:24b planned `rm -r /home/user/Downloads/Telegram (2)` for a
machine whose home is `/home/antoine`. Declaring the path in `touches` does not excuse it: a
destructive step cannot make its own target exist.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from .core import Step, TaskSpec
from .gates import SafetyClassifier
from .gates.safety import ResolvedCommand

__all__ = ["missing_path_warnings"]

#: Programs whose arguments are text, patterns, code or paths on another machine.
_NOT_LOCAL = frozenset({
    "echo", "printf", "grep", "egrep", "fgrep", "rg", "sed", "awk", "gawk", "expr", "test", "[",
    "[[", "ssh", "scp", "sftp", "python", "python3", "perl", "ruby", "node"})
#: Every path argument is something the program makes (or fetches into).
_CREATES_ALL = frozenset({"mkdir", "touch", "tee", "truncate", "mkfifo", "mktemp", "wget",
                          "curl", "tar", "unzip", "dd"})
#: Sources are read, the last argument is made.
_CREATES_LAST = frozenset({"cp", "mv", "ln", "install", "rsync"})
_REDIRECT = re.compile(r"(?<![<&\d])>{1,2}\s*([^\s;|&<>()]+)")
_UNKNOWABLE = re.compile(r"[$`*?\[\]{}\n]")


def _local_path(token: str) -> str | None:
    """The absolute path an argument names, or None when it is an option, relative, or built at
    run time (a variable, glob or brace expansion)."""
    value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
    if value.startswith("-") or not (value.startswith("/") or value == "~"
                                     or value.startswith("~/")) or _UNKNOWABLE.search(value):
        return None
    return os.path.normpath(os.path.expanduser(value))


def _explained(path: str, created: set[str]) -> bool:
    """A path is explained by a creation at it, above it, or below it."""
    return any(path == c or path.startswith(c + "/") or c.startswith(path + "/")
               for c in created)


def _split(rc: ResolvedCommand) -> tuple[list[str], list[str]]:
    """(local paths one command reads or acts on, local paths it makes)."""
    if rc.program in _NOT_LOCAL:
        return [], []
    paths = [p for tok in rc.argv[1:] if (p := _local_path(tok)) is not None]
    if rc.program in _CREATES_ALL or (rc.program == "git" and rc.argv[1:2] == ["clone"]):
        return [], paths
    last = _local_path(rc.argv[-1]) if rc.program in _CREATES_LAST else None
    if last is None:
        return paths, []
    return [p for p in paths if p != last], [last]


def _step_warnings(step: Step, resolver: SafetyClassifier, exists: Callable[[str], bool],
                   created: set[str]) -> list[str]:
    """Warnings for one step; adds what the step makes to `created` for the steps after it."""
    created.update(p for target in _REDIRECT.findall(step.command)
                   if (p := _local_path(target)) is not None)
    named: set[str] = set()
    out: list[str] = []
    for rc in resolver.resolve(step.command):
        reads, made = _split(rc)
        created.update(made)
        for path in reads:
            if path in named or exists(path) or _explained(path, created):
                continue
            named.add(path)
            out.append(f"step '{step.step_id}' names {path}, which does not exist and "
                       "no earlier step creates")
    return out


def missing_path_warnings(spec: TaskSpec, exists: Callable[[str], bool] = os.path.exists
                          ) -> list[str]:
    """One line per (step, path) where a step names an absolute local path that does not exist
    and no earlier step, or earlier part of the same step, creates."""
    resolver = SafetyClassifier()  # a private instance: resolving mutates the classifier
    created: set[str] = set()
    return [warning for step in spec.to_steps(dry_run=True)
            if step.kind not in ("remote_pull", "remote_push")
            for warning in _step_warnings(step, resolver, exists, created)]
