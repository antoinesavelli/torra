"""What torra can prove about a program no safety rule flagged: read-only, a bounded writer, or unknown.

`SafetyClassifier._judge` asks `trust()` last. Read-only runs unticked, a bounded writer runs
flagged CAUTION, and anything unknown needs a tick (docs/INVARIANTS.md §2.60). The lists below name what
is KNOWN, so a program missing from them fails toward asking, never toward running.

Depends on: torra.gates.program_tables (subcommand sets and per-program checks),
torra.gates.program_checks (`_operands`, `_tar_paths`), torra.gates.program_basics (trust levels,
`reads_credentials`), torra.gates.options (`parse`, `OptionSpec`), torra.gates.subcommand_specs
(how a program finds its subcommand).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Sequence

from .options import OptionSpec, parse
from .program_basics import (
    BOUNDED_WRITE,
    PROTECTED_WRITE,
    READ_ONLY,
    reads_credentials,
)
from .program_checks import _operands
from .program_options import _tar_paths
from .program_tables import PREDICATES, SUBCOMMANDS
from .subcommand_specs import LEAD_ANY, LEAD_NONE

__all__ = [
    "BOUNDED_WRITE",
    "LAST_OPERAND_DESTINATION",
    "PREDICATES",
    "PROTECTED_WRITE",
    "READ_ONLY",
    "READ_ONLY_PROGRAMS",
    "STANDARD_BIN_DIRS",
    "SUBCOMMANDS",
    "WRITER_PROGRAMS",
    "reads_credentials",
    "trust",
]


#: Programs that cannot change files or system state whatever their arguments: they print.
READ_ONLY_PROGRAMS = frozenset(
    """
ls dir vdir cat tac head tail wc nl od hexdump strings cut paste join comm column fold fmt
expand unexpand rev tr grep egrep fgrep zgrep zcat bzcat xzcat basename dirname
readlink realpath stat du df diff cmp md5sum sha1sum sha224sum sha256sum
sha384sum sha512sum b2sum cksum jq seq yes sleep echo printf true false test [ [[ : pwd cd type
which whereis umask export set unset read local declare shift exit return break continue
wait alias id whoami groups who w last users logname tty uname arch nproc cal
uptime free ps pgrep pidof pstree top htop vmstat iostat mpstat pidstat lsof lscpu lsblk lspci
lsusb lsmod lshw lsattr getfacl getent locale getconf findmnt blkid netstat ping
traceroute tracepath dig nslookup host whois modinfo apt-cache dpkg-query lsb_release aa-status
xdg-user-dir fsstat lsns lvs pvs vgs acpi glxinfo fc-list upower
""".split()
)

#: Known to write files or state in a bounded way. They run flagged CAUTION, not unticked-silent.
WRITER_PROGRAMS = frozenset(
    """
tee cp touch mkdir ln install gzip gunzip bzip2 bunzip2 xz unxz zip unzip patch sync pytest
""".split()
)

#: Writers whose destination is the last operand; every other writer's operands are all paths.
LAST_OPERAND_DESTINATION = frozenset({"cp", "ln", "install", "rsync"})

STANDARD_BIN_DIRS = (
    "/bin/",
    "/usr/bin/",
    "/sbin/",
    "/usr/sbin/",
    "/usr/local/bin/",
    "/usr/local/sbin/",
    "/snap/bin/",
)

_DESTINATION_FLAGS = ("-t", "-d", "-C")


def _program(argv0: str) -> str | None:
    """The program's name, or None when it is a path torra cannot vouch for."""
    if "/" in argv0 and (
        not argv0.startswith(STANDARD_BIN_DIRS) or posixpath.normpath(argv0) != argv0
    ):
        return None
    name = argv0.rsplit("/", 1)[-1]
    return "python" if re.fullmatch(r"python[0-9.]*", name) else name


#: Programs that join their words into one command line and split it again.
_JOINS_WORDS = frozenset({"nft", "tmux"})


def _sub_index(argv: Sequence[str], spec: OptionSpec) -> int | None:
    """Where the subcommand is: the first word that is no option and no option's value, behind
    options that all parse under `spec`. None when there is none, or an option before it is off."""
    for at in range(1, len(argv)):
        if not argv[at].startswith("-"):
            head = parse(argv[:at], spec)
            if head is not None and not head.operands:
                return at
    return None


def _leads(argv: Sequence[str], at: int, lead: str) -> bool:
    """Whether the words before the subcommand suit how the program itself finds its subcommand.
    flatpak, snap, ollama, tmux... take the first word that is no flag, whatever an option before
    it expects (`flatpak --arch uninstall list` runs `uninstall`); ollama allows no option at all."""
    if lead == LEAD_ANY:
        return True
    return all(word.startswith("-") for word in argv[1:at]) and (lead != LEAD_NONE or at == 1)


def _breaks_a_command(word: str) -> bool:
    """`;`, or a newline with text around it, or an undecoded ANSI-C quote (`$'\\n'`, which the
    resolver leaves as `$` and the escape). A bare newline word is a line continuation."""
    return ";" in word or ("\n" in word and word.strip() != "") or ("\\" in word and "$" in word)


def _chained(program: str, argv: Sequence[str]) -> bool:
    """A word that ends the command for a program that reads its words as a command line: tmux ends
    one at a trailing `;`, nft joins its words with spaces and splits on `;` and newlines, so for
    them even a bare newline word (a quoted one, `nft list tables '<nl>' flush ruleset`) counts."""
    if any(_breaks_a_command(word) for word in argv[1:]):
        return True
    return program in _JOINS_WORDS and any("\n" in word for word in argv[1:])


def _by_subcommand(
    program: str,
    argv: Sequence[str],
    read: frozenset[str],
    write: frozenset[str],
    spec: OptionSpec,
    lead: str,
) -> str | None:
    """READ_ONLY for a read subcommand under allowed options, BOUNDED_WRITE for a write one.

    A write subcommand needs only the options before it on `spec` (a value option the spec does not
    know would make its value look like the subcommand); a read one needs the whole line on it."""
    at = _sub_index(argv, spec)
    if _chained(program, argv) or (at is not None and not _leads(argv, at, lead)):
        return None
    if at is not None and argv[at] in write:
        return BOUNDED_WRITE
    return READ_ONLY if _reads_whole_line(argv, at, read, spec) else None


def _reads_whole_line(
    argv: Sequence[str], at: int | None, read: frozenset[str], spec: OptionSpec
) -> bool:
    """Every option on `spec`, and a read subcommand, or none at all when `-` is in `read`. A lone
    `-` operand is not "none": apt, flatpak and ollama skip it and run the next word."""
    parsed = parse(argv, spec)
    if parsed is None:
        return False
    return argv[at] in read - {"-"} if at is not None else "-" in read and not parsed.operands


def _lookup(program: str, argv: Sequence[str]) -> str | None:
    if program in READ_ONLY_PROGRAMS:
        return READ_ONLY
    if program in PREDICATES:
        return PREDICATES[program](argv)
    if program in SUBCOMMANDS:
        return _by_subcommand(program, argv, *SUBCOMMANDS[program])
    return BOUNDED_WRITE if program in WRITER_PROGRAMS else None


def _unresolved(path: str) -> bool:
    """A destination whose root torra cannot see (`$d`, a substitution, `/dev/$x`)."""
    return path.startswith(("$", "SUBST")) or (
        path.startswith("/") and ("$" in path or "SUBST" in path)
    )


def _flag_value(tok: str, following: list[str]) -> list[str]:
    """The path a destination flag carries, if `tok` is one: `-t dir`, `-d/dir`, `--directory=dir`."""
    if tok in (*_DESTINATION_FLAGS, "--target-directory", "--directory"):
        return following
    if tok.startswith(_DESTINATION_FLAGS) and len(tok) > 2 and tok[2] in "/~.$":
        return [tok[2:]]
    return [tok.split("=", 1)[1]] if tok.startswith(("--target-directory=", "--directory=")) else []


def _flag_destinations(argv: Sequence[str]) -> list[str]:
    """Values given to a destination flag: `-t dir`, `-d/dir`, `-C dir`, `--target-directory=dir`."""
    return [
        v
        for i, tok in enumerate(argv[1:], start=1)
        for v in _flag_value(tok, list(argv[i + 1 : i + 2]))
    ]


def _destinations(program: str, argv: Sequence[str]) -> list[str]:
    if program == "tar":
        return _tar_paths(argv)
    if program == "dd":
        return [a[3:] for a in argv[1:] if a.startswith("of=")]
    operands = _operands(argv)
    return (
        operands[-1:] if program in LAST_OPERAND_DESTINATION else operands
    ) + _flag_destinations(argv)


def trust(argv: Sequence[str], protected: Callable[[str], bool]) -> str | None:
    """READ_ONLY, BOUNDED_WRITE, PROTECTED_WRITE (a known writer aimed at a system path), or None."""
    program = _program(argv[0]) if argv else None
    if program is None or "()" in argv[:2] or argv[0].endswith("()"):
        return None  # an unknown path, or a function definition hiding its body
    verdict = _lookup(program, argv)
    if verdict == BOUNDED_WRITE and any(
        protected(d) or _unresolved(d) for d in _destinations(program, argv)
    ):
        return PROTECTED_WRITE
    return verdict
