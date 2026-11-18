"""Option parsing the way GNU getopt_long parses, and the allow-list judge built on it.

Scope: GNU getopt_long with permutation. Not modelled: `+`/POSIXLY_CORRECT stopping at the first
operand, `getopt_long_only` (`-output`), gawk's `-W word` as `--word`, or parsers that are not
getopt; a spec for such a program must account for that in its judge. Long names match in full only.

Depends on: torra.gates.program_basics (the `READ_ONLY` trust level).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .program_basics import READ_ONLY

Option = tuple[str, str | None]
_Step = tuple[list[Option], int]


@dataclass(frozen=True)
class OptionSpec:
    """The options one program accepts; anything else is unknown and so not provably read-only."""

    short: str = ""
    short_value: str = ""
    short_optional: str = ""
    long: frozenset[str] = frozenset()
    long_value: frozenset[str] = frozenset()
    long_optional: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        letters = self.short + self.short_value + self.short_optional
        if len(letters) != len(set(letters)):
            raise ValueError(f"a short option letter is listed twice: {letters!r}")
        names = [*self.long, *self.long_value, *self.long_optional]
        if len(names) != len(set(names)):
            raise ValueError(f"a long option name is listed twice: {sorted(names)!r}")


@dataclass(frozen=True)
class Parsed:
    """The options in order (canonical names) and the operands of one command line."""

    options: tuple[Option, ...]
    operands: tuple[str, ...]

    def values(self, *names: str) -> list[str | None]:
        """The value of every occurrence of any of `names` (None for an option without one)."""
        return [value for name, value in self.options if name in names]


def _short_cluster(word: str, words: Sequence[str], at: int, spec: OptionSpec) -> _Step | None:
    """Parse `-abc`; `at` indexes the next word, which a value letter may take. None if refused."""
    found: list[Option] = []
    for pos, letter in enumerate(word[1:], start=2):
        name, tail = f"-{letter}", word[pos:]
        if letter in spec.short:
            found.append((name, None))
        elif letter in spec.short_optional:
            return [*found, (name, tail or None)], at
        elif letter in spec.short_value and tail:
            return [*found, (name, tail)], at
        elif letter in spec.short_value and at < len(words):
            return [*found, (name, words[at])], at + 1
        else:
            return None
    return found, at


def _long_option(word: str, words: Sequence[str], at: int, spec: OptionSpec) -> _Step | None:
    """Parse `--name`, `--name=value` or `--name value`; None when unknown or malformed.

    The name must match in full: the real program matches its whole option table first, so an
    abbreviation of a spec name may be the full name of an option the spec leaves out.
    """
    name, has_value, value = word.partition("=")
    if name in spec.long:
        return ([(name, None)], at) if not has_value else None
    if name in spec.long_optional:
        return [(name, value if has_value else None)], at
    if name not in spec.long_value:
        return None
    if has_value:
        return [(name, value)], at
    return ([(name, words[at])], at + 1) if at < len(words) else None


def parse(argv: Sequence[str], spec: OptionSpec) -> Parsed | None:
    """Split `argv` (program name first) into options and operands; None on anything unrecognised."""
    if not argv:
        return None
    options: list[Option] = []
    operands: list[str] = []
    at = 1
    while at < len(argv):
        word, at = argv[at], at + 1
        if word == "--":
            operands.extend(argv[at:])
            break
        if word == "-" or not word.startswith("-"):
            operands.append(word)
            continue
        parse_one = _long_option if word.startswith("--") else _short_cluster
        got = parse_one(word, argv, at, spec)
        if got is None:
            return None
        options.extend(got[0])
        at = got[1]
    return Parsed(tuple(options), tuple(operands))


def _accepts(argv: Sequence[str], spec: OptionSpec, check: Callable[[Parsed], bool] | None) -> bool:
    """Whether `argv` parses under `spec` and `check` (when given) accepts the parse."""
    parsed = parse(argv, spec)
    return parsed is not None and (check is None or check(parsed))


def allowing(
    spec: OptionSpec, check: Callable[[Parsed], bool] | None = None
) -> Callable[[Sequence[str]], str | None]:
    """A judge: READ_ONLY when the options parse under `spec` and `check` accepts, else None."""

    def judge(argv: Sequence[str]) -> str | None:
        return READ_ONLY if _accepts(argv, spec, check) else None

    return judge
