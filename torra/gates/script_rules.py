"""Recognising the sed and awk programs torra will run without a tick.

sed is judged by parsing: only a short list of commands is accepted, so a `w`, `e` or `r` command, a
write or exec flag on `s`, or anything unfamiliar asks. awk is judged by pattern, after string
literals are blanked so a `>` or `;` inside a string neither hides a redirect nor invents one.

Depends on: the standard library only (re).
"""

from __future__ import annotations

import re

_ADDRESS = re.compile(r"(?:\d+|\$|/(?:[^/\\]|\\.)*/[IM]?)")
_AWK_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_AWK_UNSAFE = re.compile(r"system\s*\(|\|\s*&?\s*getline|getline\s*<|\bprintf?\b[^;}\n]*(?:>|\|)")


def _skip_address(script: str, i: int) -> int:
    """Index after an optional `addr` or `addr,addr` at `i`, or -1 when a range is malformed."""
    match = _ADDRESS.match(script, i)
    if not match:
        return i
    i = match.end()
    if i < len(script) and script[i] == ",":
        second = _ADDRESS.match(script, i + 1)
        return second.end() if second else -1
    return i


def _skip_substitution(script: str, i: int) -> int:
    """Index after `s<d>regexp<d>replacement<d>flags` starting just after the `s`, or -1."""
    n = len(script)
    if i >= n or script[i].isalnum() or script[i] in "\\\n ;":
        return -1
    delimiter = script[i]
    i += 1
    for _ in range(2):
        while i < n and script[i] != delimiter:
            i += 2 if script[i] == "\\" else 1
        if i >= n:
            return -1
        i += 1
    end = i
    while end < n and script[end] not in " \t;\n":
        end += 1
    return end if re.fullmatch(r"[gpiI0-9]*", script[i:end]) else -1


def sed_script_is_simple(script: str) -> bool:
    """True when every command is print, delete, quit, next or `=`, or a plain `s///` with
    harmless flags, each with an optional address. Nothing that can write a file or run a command."""
    i, n = 0, len(script)
    seen = False
    while True:
        while i < n and script[i] in " \t;\n":
            i += 1
        if i >= n:
            return seen
        i = _skip_address(script, i)
        while i != -1 and i < n and script[i] in " \t!":
            i += 1
        if i == -1 or i >= n:
            return False
        command = script[i]
        i += 1
        if command in "pdqnN=":
            seen = True
        elif command == "s":
            i = _skip_substitution(script, i)
            if i == -1:
                return False
            seen = True
        else:
            return False


def awk_program_is_safe(program: str) -> bool:
    """True when the awk program runs no command and writes no file."""
    return not _AWK_UNSAFE.search(_AWK_STRING.sub('""', program))
