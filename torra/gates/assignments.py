"""Which variables a shell call assigns besides a leading `NAME=value`: builtins, `for`, `${NAME:=x}`.

`SafetyClassifier` notes each name the way it notes a leading `NAME=value`, so `read PATH <<< /x`,
`printf -v PATH`, `declare -n P=PATH` or `for PATH in /x` cannot change what a later bare command
runs without a tick (HANDOFF.md
§2.75, §2.83). A name the line builds at run time (an expansion, a substitution, a brace list, a
nameref with no target yet) comes back as UNSEEN, and asks.

Depends on: the standard library only (re).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

UNSEEN = "a variable named only at run time"

#: Builtins whose `NAME=value` operands set a variable exactly as a leading assignment does.
DECLARING = frozenset({"export", "declare", "typeset", "local", "readonly"})
#: The ones where `-n` makes a nameref (for `export`, `-n` un-exports).
NAMEREF = frozenset({"declare", "typeset", "local"})
NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?")
#: Characters that make bash build the word at run time: expansion, substitution, braces, globs.
BUILT = re.compile(r"[$`{}*?\[]|SUBST")
DEFAULTED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?=")
REDIRECT = re.compile(r"\d*(?:<<<|<<-?|<>|<&|<|>>|>\||>&|&>>|&>|>)")
#: `read` options that take a value; `-a`'s value is the array it assigns.
READ_VALUE_FLAGS = "adinNptu"


def _named(word: str) -> str | None:
    """The variable a name word sets, UNSEEN when bash builds it at run time, None when it is no name."""
    match = None if "SUBST" in word else NAME.fullmatch(word)  # SUBST: a blanked `$(...)`
    if match:
        return match.group(1)
    return UNSEEN if BUILT.search(word) else None


def _makes_nameref(argv: Sequence[str]) -> bool:
    """`declare -n`, `local -rn`: a nameref (for `export`, `-n` un-exports instead)."""
    return argv[0] in NAMEREF and any(a.startswith("-") and "n" in a for a in argv[1:])


def _declared(argv: Sequence[str]) -> list[str | None]:
    """`declare PATH=/x`, `export $X`, `declare -n P=PATH`: each name set, and each nameref's target."""
    nameref = _makes_nameref(argv)
    found: list[str | None] = []
    # Every operand, redirect-looking or not: bash's `export '>' PATH=/x` skips the bad word and
    # still assigns PATH.
    for word in (a for a in argv[1:] if not a.startswith(("-", "+"))):
        left, eq, value = word.partition("=")
        name = _named(left.removesuffix("+"))
        # A bare literal declares without assigning; a built word may expand to `NAME=value`.
        found.append(name if eq or name == UNSEEN else None)
        if nameref:
            found.append(
                _named(value) if value else UNSEEN
            )  # no target yet: the next assignment picks it
    return found


def _read_flag(word: str, following: str) -> tuple[str | None, bool]:
    """For one `read` option cluster: the array `-a` names, and whether it took the next word."""
    for at, letter in enumerate(word[1:], start=2):
        if letter in READ_VALUE_FLAGS:
            value = word[at:] or following
            return (_named(value) if letter == "a" else None), not word[at:]
    return None, False


def _read(argv: Sequence[str]) -> list[str | None]:
    """`read [-ers] [-a NAME] [-p PROMPT] ... [NAME ...]`: every name, never a redirect's target."""
    found: list[str | None] = []
    words, i = list(argv[1:]), 0
    while i < len(words):
        word, following = words[i], (words[i + 1 : i + 2] or [""])[0]
        if word.startswith("-") and word != "-":
            name, took_next = _read_flag(word, following)
            found.append(name)
            i += took_next
        elif REDIRECT.match(word):
            # `<<< "$v"` feeds read; it names nothing. A quoted `'<'` in a name slot only looks the
            # same, and bash stops assigning at that invalid name, so skipping past it hides nothing.
            i += REDIRECT.fullmatch(word) is not None
        else:
            found.append(_named(word))
        i += 1
    return found


def _printf(argv: Sequence[str]) -> list[str | None]:
    """`printf -v NAME` or `-vNAME`, printf's only option."""
    if argv[1:2] and argv[1].startswith("-v"):
        return [_named(argv[1][2:] or (argv[2:3] or [""])[0])]
    return []


def _for(argv: Sequence[str]) -> list[str | None]:
    """`for NAME in ...`: the loop variable keeps its last value after `done`."""
    return [_named(word) for word in argv[1:2]]


PARSERS: dict[str, Callable[[Sequence[str]], list[str | None]]] = {
    "read": _read,
    "printf": _printf,
    "for": _for,
    **dict.fromkeys(DECLARING, _declared),
}


def assigned_names(argv: Sequence[str]) -> list[str]:
    """Every variable this call sets, with UNSEEN for one named only at run time."""
    # `: ${LD_PRELOAD:=/x.so}` assigns an unset name in any program's arguments.
    found: list[str | None] = [m.group(1) for word in argv for m in DEFAULTED.finditer(word)]
    parse = PARSERS.get(argv[0]) if argv else None
    if parse:
        found += parse(argv)
    return [name for name in found if name]
