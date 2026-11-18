"""Per-program argv checks, part two: network, packages, firewall, kernel, disks and other system tools.

Depends on: torra.gates.program_basics (trust levels), torra.gates.program_checks (the flag helpers,
`_rsync`), torra.gates.options and torra.gates.option_specs (the `ruff` judges).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .option_specs import RUFF_CHECK, RUFF_FORMAT, ruff_checks_only, ruff_no_fix
from .options import allowing
from .program_basics import BOUNDED_WRITE, READ_ONLY
from .program_checks import _hits, _long, _operands, _rsync, _short

_IP_READ_VERBS = frozenset({"show", "list", "ls", "get", "help"})


def _ip(argv: Sequence[str]) -> str | None:
    """`ip OBJECT [show|list|ls|get]`: any other verb, abbreviated or not, asks."""
    words = [a for a in argv[1:] if not a.startswith("-")]
    return READ_ONLY if len(words) < 2 or words[1] in _IP_READ_VERBS else None


def _su(argv: Sequence[str]) -> str | None:
    """A login shell for another user is bounded; `-c`/`--command` runs something as them."""
    runs = "c" in _short(argv) or any(_hits(f, ("--command",)) for f in _long(argv))
    return None if runs else BOUNDED_WRITE


def _rsync_checked(argv: Sequence[str]) -> str | None:
    """`_rsync` plus the options it must not trust: deletion (also abbreviated) and `-e`/`--rsh`."""
    risky = any(
        _hits(f, ("--delete", "--remove-source-files", "--rsh")) for f in _long(argv)
    ) or "e" in _short(argv)
    return None if risky else _rsync(argv)


def _crontab(argv: Sequence[str]) -> str | None:
    short = _short(argv)
    return READ_ONLY if "l" in short and not short & set("re") else None


def _dpkg(argv: Sequence[str]) -> str | None:
    reads = {
        "-l",
        "-L",
        "-s",
        "-S",
        "-p",
        "-C",
        "-V",
        "-w",
        "--list",
        "--listfiles",
        "--status",
        "--search",
        "--print-avail",
        "--get-selections",
        "--print-architecture",
        "--version",
        "--audit",
    }
    return READ_ONLY if argv[1:2] and argv[1] in reads else None


def _iptables(argv: Sequence[str]) -> str | None:
    short, long = _short(argv), _long(argv)
    writes = short & set("ADIRFZNXPE") or long & {
        "--append",
        "--delete",
        "--insert",
        "--replace",
        "--flush",
        "--zero",
        "--new-chain",
        "--delete-chain",
        "--policy",
        "--rename-chain",
    }
    reads = short & set("LS") or long & {"--list", "--list-rules"}
    return READ_ONLY if reads and not writes else None


def _sysctl(argv: Sequence[str]) -> str | None:
    writing = (
        _short(argv) & set("wp")
        or _long(argv) & {"--write", "--load", "--system"}
        or any("=" in a for a in argv[1:])
    )
    return None if writing else READ_ONLY


_NMCLI_WRITES = frozenset(
    "up down add modify delete edit clone import reload connect disconnect "
    "set on off hotspot".split()
)

_RUFF_CHECK = allowing(RUFF_CHECK, ruff_no_fix)
_RUFF_FORMAT = allowing(RUFF_FORMAT, ruff_checks_only)


def _ruff(argv: Sequence[str]) -> str | None:
    """`ruff check --no-fix` and `ruff format --check|--diff` with only listed flags; plain `format` writes.

    `ruff check` without `--no-fix` writes fixes when the project config sets `fix = true`."""
    rest = ["ruff", *argv[2:]]
    if argv[1:2] == ["check"]:
        return _RUFF_CHECK(rest)
    return _RUFF_FORMAT(rest) if argv[1:2] == ["format"] else None


def _torra(argv: Sequence[str]) -> str | None:
    """`torra check FILE [-v]` only reads the task file and prints its verdicts (cli.cmd_check)."""
    rest = argv[2:]
    if argv[1:2] != ["check"] or len(_operands(["torra", *rest])) != 1:
        return None
    return (
        READ_ONLY if set(rest) - set(_operands(["torra", *rest])) <= {"-v", "--verbose"} else None
    )


def _nmcli(argv: Sequence[str]) -> str | None:
    words = set(argv[1:])
    return READ_ONLY if words & {"show", "status", "list"} and not words & _NMCLI_WRITES else None


def _disk_listing(argv: Sequence[str]) -> str | None:
    """fdisk/parted/sfdisk/sgdisk are read-only only when they just list or print."""
    asks_to_list = {"-l", "--list", "-p", "--print", "print", "list"} & set(argv[1:])
    plain = all(a in ("print", "list") or a.startswith(("/dev/", "-")) for a in argv[1:])
    return READ_ONLY if asks_to_list and plain else None


def _printenv(argv: Sequence[str]) -> str | None:
    """Named variables only, and not ones that sound like secrets (`printenv API_KEY`)."""
    names = _operands(argv)
    quiet = names and not any(re.search(r"(?i)key|token|secret|pass|cred", n) for n in names)
    return READ_ONLY if quiet else None


def _command(argv: Sequence[str]) -> str | None:
    return READ_ONLY if argv[1:2] in (["-v"], ["-V"]) else None


def _pactl(argv: Sequence[str]) -> str | None:
    sub = (_operands(argv) or [""])[0]
    return READ_ONLY if sub in ("list", "info", "stat") or sub.startswith("get-") else None
