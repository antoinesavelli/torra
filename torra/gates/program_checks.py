"""Per-program argv checks, part one: the flag helpers and `python -c`, `sed`, `awk`, `curl`, `tar`, `rsync`, `git`.

Depends on: torra.gates.program_basics (trust levels, `reads_credentials`, `_subcommand`),
torra.gates.script_rules (sed and awk programs).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence

from .program_basics import BOUNDED_WRITE, READ_ONLY, _subcommand, reads_credentials
from .script_rules import awk_program_is_safe, sed_script_is_simple

#: Modules a `python -c` payload may import and still count as pure computation: none of them can
#: write a file, start a process or reach the network.
#: `openpyxl` is here because reading spreadsheets is the measured use (HANDOFF.md §2.54, §2.55);
#: its one write path is `.save()`, which is refused below.
PURE_PYTHON_MODULES = frozenset(
    """
json sys re math datetime time collections itertools functools statistics string textwrap csv
base64 binascii hashlib decimal fractions copy typing random uuid openpyxl
""".split()
)
UNSAFE_PYTHON_NAMES = frozenset(
    """
open exec eval compile __import__ getattr setattr delattr globals locals vars breakpoint input
help memoryview exit quit
""".split()
)
#: Attribute names that reach a module able to write, spawn or escape (`uuid.os`, `json.codecs`).
UNSAFE_PYTHON_ATTRS = frozenset(
    """
modules save save_workbook os subprocess codecs io builtins shutil pathlib socket importlib
ctypes popen system truncate unlink remove rmdir rename replace write_text write_bytes zipfile
tarfile tempfile pickle marshal multiprocessing threading signal glob attrgetter itemgetter
methodcaller
""".split()
)
OPEN_KEYWORDS = frozenset({"mode", "encoding", "errors", "newline"})

_CURL_LONG_WRITES = (
    "--output",
    "--remote-name",
    "--upload-file",
    "--data",
    "--form",
    "--json",
    "--config",
    "--post",
    "--create-dirs",
    "--trace",
    "--dump-header",
    "--cookie-jar",
    "--stderr",
    "--libcurl",
    "--etag-save",
    "--hsts",
    "--alt-svc",
    "--next",
)
_TAR_UNSAFE_LONG = (
    "--checkpoint-action",
    "--to-command",
    "--use-compress-program",
    "--rsh-command",
    "--info-script",
    "--new-volume-script",
    "--remove-files",
    "--absolute-names",
)


def _short(argv: Sequence[str]) -> set[str]:
    """Letters of every single-dash flag cluster (`-sSL` gives s, S, L)."""
    return {c for a in argv[1:] if a.startswith("-") and not a.startswith("--") for c in a[1:]}


def _long(argv: Sequence[str]) -> set[str]:
    """Every `--long` flag name, without any `=value`."""
    return {a.split("=", 1)[0] for a in argv[1:] if a.startswith("--")}


def _operands(argv: Sequence[str]) -> list[str]:
    return [a for a in argv[1:] if not a.startswith("-")]


def _hits(flag: str, denied: Sequence[str]) -> bool:
    """Whether a `--long` flag is one of `denied` or an abbreviation of one (`--out` for `--output`)."""
    return any(flag.startswith(d) or (len(flag) >= 4 and d.startswith(flag)) for d in denied)


def _avoiding(
    short: str = "", long: tuple[str, ...] = (), tokens: frozenset[str] = frozenset()
) -> Callable[[Sequence[str]], str | None]:
    """Read-only unless a flag from the given short letters, long prefixes or exact tokens shows."""

    def judge(argv: Sequence[str]) -> str | None:
        bad = (
            _short(argv) & set(short)
            or any(_hits(f, long) for f in _long(argv))
            or tokens & set(argv[1:])
        )
        return None if bad else READ_ONLY

    return judge


def _opens_for_reading(call: ast.Call) -> bool:
    """`open(path)` or `open(path, 'r')`: a file opened to read, never to write."""
    if len(call.args) > 2 or any(k.arg not in OPEN_KEYWORDS for k in call.keywords):
        return False
    mode = (
        call.args[1]
        if len(call.args) > 1
        else next((k.value for k in call.keywords if k.arg == "mode"), None)
    )
    return mode is None or (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and set(mode.value) <= set("rbt")
    )


def _reaches_outside(node: ast.AST, reading: set[int]) -> bool:
    """True for an import, name or attribute that could start a process, write or escape."""
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] not in PURE_PYTHON_MODULES for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return bool(node.level) or (node.module or "").split(".")[0] not in PURE_PYTHON_MODULES
    if isinstance(node, ast.Name):
        return node.id.startswith("__") or (
            node.id in UNSAFE_PYTHON_NAMES and id(node) not in reading
        )
    if isinstance(node, ast.Attribute):
        return node.attr.startswith("_") or node.attr in UNSAFE_PYTHON_ATTRS
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value) < 300 and reads_credentials(node.value)
    return False


def _pure_python(source: str) -> bool:
    """True when the payload only computes or reads: no process, no network, no file written."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    reading = {
        id(n.func)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "open"
        and _opens_for_reading(n)
    }
    return not any(_reaches_outside(node, reading) for node in ast.walk(tree))


def _python(argv: Sequence[str]) -> str | None:
    rest = [a for a in argv[1:] if a not in ("-u", "-B", "-I", "-E", "-s", "-S", "-q")]
    if rest[:1] in (["--version"], ["-V"]) or rest[:2] == ["-m", "json.tool"]:
        return READ_ONLY
    if rest[:2] == ["-m", "pip"] and rest[2:3] in (["list"], ["show"], ["freeze"], ["check"]):
        return READ_ONLY
    if rest[:2] in (["-m", "pytest"], ["-m", "unittest"]):
        return BOUNDED_WRITE
    if rest[:1] == ["-c"] and len(rest) == 2 and _pure_python(rest[1]):
        return READ_ONLY
    return None


def _only_version(argv: Sequence[str]) -> str | None:
    return (
        READ_ONLY
        if list(argv[1:]) in (["--version"], ["-v"], ["-V"], ["-version"], ["version"])
        else None
    )


def _sed(argv: Sequence[str]) -> str | None:
    """Read-only only when every script is one `sed_script_is_simple` accepts."""
    if "f" in _short(argv) or any(_hits(f, ("--file",)) for f in _long(argv)):
        return None  # a script file: unknown content
    if "i" in _short(argv) or any(_hits(f, ("--in-place",)) for f in _long(argv)):
        return BOUNDED_WRITE
    scripts = _sed_scripts(argv)
    return READ_ONLY if scripts and all(sed_script_is_simple(x) for x in scripts) else None


def _sed_scripts(argv: Sequence[str]) -> list[str]:
    """The scripts on a sed command line: every `-e` value, else the first operand."""
    scripts, operands, tokens = [], [], iter(argv[1:])
    for tok in tokens:
        if tok == "--expression" or (
            tok.startswith("-") and not tok.startswith("--") and tok.endswith("e")
        ):
            scripts.append(next(tokens, ""))
        elif tok.startswith("--expression="):
            scripts.append(tok.split("=", 1)[1])
        elif not tok.startswith("-"):
            operands.append(tok)
    return scripts or operands[:1]


def _awk(argv: Sequence[str]) -> str | None:
    if _short(argv) & set("fiEl") or _long(argv) & {"--file", "--include", "--load", "--exec"}:
        return None
    return READ_ONLY if all(awk_program_is_safe(o) for o in _operands(argv)) else None


def _curl(argv: Sequence[str]) -> str | None:
    short, long = _short(argv), _long(argv)
    outputs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-o", "--output")]
    if outputs and all(o == "/dev/null" for o in outputs):
        short, long = short - {"o"}, long - {"--output"}
    if short & set("oOTdFKDc:") or any(_hits(f, _CURL_LONG_WRITES) for f in long):
        return None
    return READ_ONLY if all(m.upper() in ("GET", "HEAD") for m in _curl_methods(argv)) else None


def _curl_methods(argv: Sequence[str]) -> list[str]:
    """Every method given with `-X` or `--request` (also abbreviated, `--req`)."""
    return [
        argv[i + 1]
        for i, a in enumerate(argv[:-1])
        if a == "-X" or (a.startswith("--") and len(a) >= 5 and "--request".startswith(a))
    ]


def _tar_modes(argv: Sequence[str]) -> set[str]:
    modes = _short(argv) | (set(argv[1]) if len(argv) > 1 and argv[1][:1] != "-" else set())
    names = {
        "--create": "c",
        "--extract": "x",
        "--get": "x",
        "--list": "t",
        "--delete": "d",
        "--append": "r",
        "--update": "u",
        "--concatenate": "A",
        "--diff": "d",
    }
    return modes | {names[f] for f in _long(argv) if f in names}


def _tar(argv: Sequence[str]) -> str | None:
    modes = _tar_modes(argv)
    if modes & set("drAuIFP") or any(_hits(f, _TAR_UNSAFE_LONG) for f in _long(argv)):
        return None
    if modes & set("cx"):
        return BOUNDED_WRITE
    return READ_ONLY if "t" in modes else None


def _tar_long_or_attached(tok: str, following: list[str]) -> list[str]:
    """A path given as `--file=x`, `--file x`, or attached (`-C/etc`)."""
    if tok[:2] in ("-C", "-f") and len(tok) > 2 and tok[2] in "/~.":
        return [tok[2:]]
    if tok in ("--directory", "--file"):
        return following
    return [tok.split("=", 1)[1]] if tok.startswith(("--directory=", "--file=")) else []


def _tar_paths(argv: Sequence[str]) -> list[str]:
    """Where tar writes: its archive (`-f`, when creating) and its `-C` directory."""
    toks, paths = list(argv[1:]), []
    for i, tok in enumerate(toks):
        cluster = (
            tok
            if i == 0 and not tok.startswith("-")
            else (tok[1:] if tok.startswith("-") and not tok.startswith("--") else "")
        )
        if i + 1 < len(toks) and ("C" in cluster or "f" in cluster):
            paths.append(toks[i + 1])
        paths += _tar_long_or_attached(tok, toks[i + 1 : i + 2])
    return paths


#: `host:path`, `user@host:path` and `rsync://host/...`: an operand that leaves this machine.
_REMOTE_OPERAND = re.compile(r"^(?:rsync://|(?:[\w.-]+@)?[\w.-]+::?)")


def _rsync(argv: Sequence[str]) -> str | None:
    """A local sync is bounded; deleting, or any remote operand, needs a tick."""
    deleting = any(f.startswith(("--delete", "--remove-source-files")) for f in _long(argv))
    remote = any(_REMOTE_OPERAND.match(a) for a in argv[1:] if not a.startswith("-"))
    return None if deleting or remote else BOUNDED_WRITE


_GIT_READ = frozenset(
    """
- status log diff show rev-parse rev-list ls-files ls-tree ls-remote describe blame shortlog grep
cat-file name-rev show-ref count-objects diff-tree diff-index for-each-ref merge-base var version
help check-ignore
""".split()
)
_GIT_WRITE = frozenset(
    "pull fetch add commit clone init merge switch checkout cherry-pick revert".split()
)
_GIT_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_GIT_EDITING = frozenset("-d -D -m -M -f -c -C --delete --move --force --copy".split())
_GIT_LISTING = frozenset(
    "-a -r -l -v -vv --list --all --remotes --verbose --show-current --contains --merged --no-merged --points-at --sort".split()
)
_GIT_CONFIG_READS = frozenset("--get --get-all --get-regexp --list -l --show-origin".split())
_GIT_CONFIG_EDITS = frozenset(
    "--unset --unset-all --add --replace-all --edit -e --remove-section --rename-section".split()
)
_GIT_SCOPE_FLAGS = frozenset({"--global", "--local", "--system", "--worktree"})


def _git(argv: Sequence[str]) -> str | None:
    """Reads run, a `pull`-like write is bounded, and everything else (push, reset, rebase) asks."""
    sub = _subcommand(argv, _GIT_VALUE_FLAGS)
    at = argv.index(sub) if sub in argv else len(argv)
    rest = list(argv[at + 1 :])
    free = [a for a in rest if not a.startswith("-")]
    if "-c" in argv[1:at] or any(_hits(a, ("--exec-path", "--config-env")) for a in argv[1:at]):
        return None  # a per-command config setting can name a program to run
    if sub in _GIT_READ:
        return None if _git_read_writes(rest) else READ_ONLY
    if sub in ("checkout", "switch") and (
        {"-f", "--force", "--discard-changes", "."} & set(rest) or "--" in rest
    ):
        return None  # throws away uncommitted work
    if sub in _GIT_WRITE:
        return BOUNDED_WRITE
    return _git_by_shape(sub, rest, free)


def _git_read_writes(rest: Sequence[str]) -> bool:
    """A read subcommand given an option that writes a file or runs a program."""
    longs = (a.split("=", 1)[0] for a in rest if a.startswith("--"))
    return any(_hits(f, ("--output", "--open-files-in-pager", "--ext-diff")) for f in longs) or any(
        a.startswith("-O") for a in rest
    )


def _git_config(rest: list[str], free: list[str]) -> str | None:
    plain_key = len(free) == 1 and set(rest) - set(free) <= _GIT_SCOPE_FLAGS  # `git config <key>`
    reading = _GIT_CONFIG_READS & set(rest) or plain_key
    return READ_ONLY if reading and not _GIT_CONFIG_EDITS & set(rest) else None


def _git_by_shape(sub: str, rest: list[str], free: list[str]) -> str | None:
    """Subcommands whose effect depends on their arguments: listing reads, naming creates."""
    if sub in ("branch", "tag"):
        if _GIT_EDITING & set(rest):
            return None
        return READ_ONLY if not free or _GIT_LISTING & set(rest) else BOUNDED_WRITE
    if sub == "remote":
        return READ_ONLY if not free or free[0] in ("show", "get-url") else None
    if sub == "stash":
        return READ_ONLY if free[:1] in (["list"], ["show"]) else BOUNDED_WRITE
    return _git_config(rest, free) if sub == "config" else None
