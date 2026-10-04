"""What torra can prove about a program no safety rule flagged: read-only, a bounded writer, or unknown.

`SafetyClassifier._judge` asks `trust()` last. Read-only runs unticked, a bounded writer runs
flagged CAUTION, and anything unknown needs a tick (HANDOFF.md §2.60). The lists below name what
is KNOWN, so a program missing from them fails toward asking, never toward running.

Depends on: the standard library only (ast, re).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence

READ_ONLY = "read_only"
BOUNDED_WRITE = "bounded_write"
PROTECTED_WRITE = "protected_write"

#: Programs that cannot change files or system state whatever their arguments: they print.
READ_ONLY_PROGRAMS = frozenset("""
ls dir vdir cat tac head tail wc nl od xxd hexdump strings cut paste join comm column fold fmt
expand unexpand rev tr grep egrep fgrep rg zgrep zcat bzcat xzcat less more basename dirname
readlink realpath stat file du df tree diff cmp sdiff md5sum sha1sum sha224sum sha256sum
sha384sum sha512sum b2sum cksum jq seq yes sleep echo printf true false test [ [[ : pwd cd type
which whereis hash umask export set unset read local declare shift exit return break continue
wait alias printenv id whoami groups who w last users logname tty uname hostname arch nproc cal
uptime free ps pgrep pidof pstree top htop vmstat iostat mpstat pidstat lsof lscpu lsblk lspci
lsusb lsmod lshw lsattr getfacl getent locale getconf sensors findmnt blkid netstat ping
traceroute tracepath dig nslookup host whois modinfo apt-cache dpkg-query lsb_release aa-status
xdg-user-dir fsstat
""".split())

#: Known to write files or state in a bounded way. They run flagged CAUTION, not unticked-silent.
WRITER_PROGRAMS = frozenset("""
tee cp touch mkdir ln install gzip gunzip bzip2 bunzip2 xz unxz zip unzip patch sync su pytest
""".split())

#: Writers whose destination is the last operand; every other writer's operands are all paths.
LAST_OPERAND_DESTINATION = frozenset({"cp", "ln", "install", "rsync"})

STANDARD_BIN_DIRS = ("/bin/", "/usr/bin/", "/sbin/", "/usr/sbin/", "/usr/local/bin/",
                     "/usr/local/sbin/", "/snap/bin/")

#: Modules a `python -c` payload may import and still count as pure computation: none of them can
#: write a file, start a process or reach the network.
#: `openpyxl` is here because reading spreadsheets is the measured use (HANDOFF.md §2.54, §2.55);
#: its one write path is `.save()`, which is refused below.
PURE_PYTHON_MODULES = frozenset("""
json sys re math datetime time collections itertools functools statistics string textwrap csv
base64 binascii hashlib decimal fractions operator copy typing random uuid openpyxl
""".split())
UNSAFE_PYTHON_NAMES = frozenset("""
open exec eval compile __import__ getattr setattr delattr globals locals vars breakpoint input
help memoryview exit quit
""".split())
UNSAFE_PYTHON_ATTRS = frozenset({"modules", "save", "save_workbook"})
OPEN_KEYWORDS = frozenset({"mode", "encoding", "errors", "newline"})

_SED_WRITES = re.compile(r"(?:^|[;{}\s])[wWeE](?:\s|$)|/[0-9gpiImM]*[we](?:\s|;|}|$)")
_AWK_UNSAFE = re.compile(r"system\s*\(|\|\s*getline|getline\s*<|\bprintf?\b[^;}\n]*(?:>|\|)")
_CURL_LONG_WRITES = ("--output", "--remote-name", "--upload-file", "--data", "--form", "--json",
                     "--config", "--post", "--create-dirs", "--trace", "--dump-header",
                     "--cookie-jar", "--stderr", "--libcurl")
_FIND_WRITES = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
                          "-fprintf", "-fls"})
_IP_WRITES = frozenset("add del delete change replace set flush append prepend exec save restore"
                       .split())
_NVIDIA_WRITES = frozenset("""
-pm -e -p -c -r -pl -ac -rac -acp -lgc -rgc -lmc -rmc -mig -dm -fdm
""".split())


def _short(argv: Sequence[str]) -> set[str]:
    """Letters of every single-dash flag cluster (`-sSL` gives s, S, L)."""
    return {c for a in argv[1:] if a.startswith("-") and not a.startswith("--") for c in a[1:]}


def _long(argv: Sequence[str]) -> set[str]:
    """Every `--long` flag name, without any `=value`."""
    return {a.split("=", 1)[0] for a in argv[1:] if a.startswith("--")}


def _operands(argv: Sequence[str]) -> list[str]:
    return [a for a in argv[1:] if not a.startswith("-")]


def _avoiding(short: str = "", long: tuple[str, ...] = (),
              tokens: frozenset[str] = frozenset()) -> Callable[[Sequence[str]], str | None]:
    """Read-only unless a flag from the given short letters, long prefixes or exact tokens shows."""
    def judge(argv: Sequence[str]) -> str | None:
        bad = (_short(argv) & set(short) or any(f.startswith(long) for f in _long(argv))
               or tokens & set(argv[1:]))
        return None if bad else READ_ONLY
    return judge


def _opens_for_reading(call: ast.Call) -> bool:
    """`open(path)` or `open(path, 'r')`: a file opened to read, never to write."""
    if len(call.args) > 2 or any(k.arg not in OPEN_KEYWORDS for k in call.keywords):
        return False
    mode = call.args[1] if len(call.args) > 1 else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    return mode is None or (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                            and set(mode.value) <= set("rbt"))


def _reaches_outside(node: ast.AST, reading: set[int]) -> bool:
    """True for an import, name or attribute that could start a process, write or escape."""
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] not in PURE_PYTHON_MODULES for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return bool(node.level) or (node.module or "").split(".")[0] not in PURE_PYTHON_MODULES
    if isinstance(node, ast.Name):
        return node.id.startswith("__") or (node.id in UNSAFE_PYTHON_NAMES
                                            and id(node) not in reading)
    if isinstance(node, ast.Attribute):
        return node.attr.startswith("_") or node.attr in UNSAFE_PYTHON_ATTRS
    return False


def _pure_python(source: str) -> bool:
    """True when the payload only computes or reads: no process, no network, no file written."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    reading = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "open" and _opens_for_reading(n)}
    return not any(_reaches_outside(node, reading) for node in ast.walk(tree))


def _python(argv: Sequence[str]) -> str | None:
    rest = [a for a in argv[1:] if a not in ("-u", "-B", "-I", "-E", "-s", "-S", "-q")]
    if rest[:1] in (["--version"], ["-V"]) or rest[:2] == ["-m", "json.tool"]:
        return READ_ONLY
    if rest[:2] in (["-m", "pytest"], ["-m", "unittest"]):
        return BOUNDED_WRITE
    if rest[:1] == ["-c"] and len(rest) == 2 and _pure_python(rest[1]):
        return READ_ONLY
    return None


def _only_version(argv: Sequence[str]) -> str | None:
    return READ_ONLY if list(argv[1:]) in (["--version"], ["-v"], ["-V"]) else None


def _sed(argv: Sequence[str]) -> str | None:
    if "f" in _short(argv):
        return None
    if "i" in _short(argv) or any(f.startswith("--in-place") for f in _long(argv)):
        return BOUNDED_WRITE
    return None if any(_SED_WRITES.search(o) for o in _operands(argv)) else READ_ONLY


def _awk(argv: Sequence[str]) -> str | None:
    if _short(argv) & set("fiEl") or _long(argv) & {"--file", "--include", "--load", "--exec"}:
        return None
    return None if any(_AWK_UNSAFE.search(o) for o in _operands(argv)) else READ_ONLY


def _curl(argv: Sequence[str]) -> str | None:
    short, long = _short(argv), _long(argv)
    outputs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-o", "--output")]
    if outputs and all(o == "/dev/null" for o in outputs):
        short, long = short - {"o"}, long - {"--output"}
    if short & set("oOTdFKDc") or any(f.startswith(_CURL_LONG_WRITES) for f in long):
        return None
    method = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-X", "--request")), "GET")
    return READ_ONLY if method.upper() in ("GET", "HEAD") else None


def _tar_modes(argv: Sequence[str]) -> set[str]:
    modes = _short(argv) | (set(argv[1]) if len(argv) > 1 and argv[1][:1] != "-" else set())
    names = {"--create": "c", "--extract": "x", "--get": "x", "--list": "t", "--delete": "d",
             "--append": "r", "--update": "u", "--concatenate": "A", "--diff": "d"}
    return modes | {names[f] for f in _long(argv) if f in names}


def _tar(argv: Sequence[str]) -> str | None:
    modes = _tar_modes(argv)
    if modes & set("drAu"):
        return None
    if modes & set("cx"):
        return BOUNDED_WRITE
    return READ_ONLY if "t" in modes else None


def _tar_paths(argv: Sequence[str]) -> list[str]:
    """Where tar writes: its archive (`-f`, when creating) and its `-C` directory."""
    toks, paths = list(argv[1:]), []
    for i, tok in enumerate(toks):
        cluster = tok if i == 0 and not tok.startswith("-") else (
            tok[1:] if tok.startswith("-") and not tok.startswith("--") else "")
        if i + 1 < len(toks) and ("C" in cluster or "f" in cluster):
            paths.append(toks[i + 1])
        if tok in ("--directory", "--file") and i + 1 < len(toks):
            paths.append(toks[i + 1])
        if tok.startswith(("--directory=", "--file=")):
            paths.append(tok.split("=", 1)[1])
    return paths


def _rsync(argv: Sequence[str]) -> str | None:
    """A local sync is bounded; deleting, or any remote operand, needs a tick."""
    deleting = any(f.startswith(("--delete", "--remove-source-files")) for f in _long(argv))
    return None if deleting else BOUNDED_WRITE


_GIT_READ = frozenset("""
- status log diff show rev-parse rev-list ls-files ls-tree ls-remote describe blame shortlog grep
cat-file name-rev show-ref count-objects diff-tree diff-index for-each-ref merge-base var version
help check-ignore
""".split())
_GIT_WRITE = frozenset("pull fetch add commit clone init merge switch checkout cherry-pick revert"
                       .split())
_GIT_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_GIT_EDITING = frozenset("-d -D -m -M -f -c -C --delete --move --force --copy".split())
_GIT_LISTING = frozenset("-a -r -l -v -vv --list --all --remotes --verbose --show-current --contains --merged --no-merged --points-at --sort".split())
_GIT_CONFIG_READS = frozenset("--get --get-all --get-regexp --list -l --show-origin".split())
_GIT_CONFIG_EDITS = frozenset("--unset --unset-all --add --replace-all --edit -e".split())


def _git(argv: Sequence[str]) -> str | None:
    """Reads run, a `pull`-like write is bounded, and everything else (push, reset, rebase) asks."""
    sub = _subcommand(argv, _GIT_VALUE_FLAGS)
    rest = list(argv[argv.index(sub) + 1:]) if sub in argv else []
    free = [a for a in rest if not a.startswith("-")]
    if sub in _GIT_READ:
        return READ_ONLY
    if sub in _GIT_WRITE:
        return BOUNDED_WRITE
    return _git_by_shape(sub, rest, free)


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
    if sub == "config":
        reading = _GIT_CONFIG_READS & set(rest) or len(free) == 1  # `git config <key>` reads
        return READ_ONLY if reading and not _GIT_CONFIG_EDITS & set(rest) else None
    return None


def _crontab(argv: Sequence[str]) -> str | None:
    short = _short(argv)
    return READ_ONLY if "l" in short and not short & set("re") else None


def _dpkg(argv: Sequence[str]) -> str | None:
    reads = {"-l", "-L", "-s", "-S", "-p", "-C", "-V", "-w", "--list", "--listfiles", "--status",
             "--search", "--print-avail", "--get-selections", "--print-architecture", "--version",
             "--audit"}
    return READ_ONLY if argv[1:2] and argv[1] in reads else None


def _iptables(argv: Sequence[str]) -> str | None:
    short, long = _short(argv), _long(argv)
    writes = short & set("ADIRFZNXPE") or long & {
        "--append", "--delete", "--insert", "--replace", "--flush", "--zero", "--new-chain",
        "--delete-chain", "--policy", "--rename-chain"}
    reads = short & set("LS") or long & {"--list", "--list-rules"}
    return READ_ONLY if reads and not writes else None


def _sysctl(argv: Sequence[str]) -> str | None:
    writing = (_short(argv) & set("wp") or _long(argv) & {"--write", "--load", "--system"}
               or any("=" in a for a in argv[1:]))
    return None if writing else READ_ONLY


def _positive(allowed_short: str = "", allowed_long: frozenset[str] = frozenset(),
              needs: str = "") -> Callable[[Sequence[str]], str | None]:
    """Read-only only when every flag is on the allowed list (and one named flag is present)."""
    def judge(argv: Sequence[str]) -> str | None:
        short, long = _short(argv), _long(argv)
        if short - set(allowed_short) or long - allowed_long:
            return None
        return READ_ONLY if not needs or needs in short or needs in long else None
    return judge


_NMCLI_WRITES = frozenset("up down add modify delete edit clone import reload connect disconnect "
                          "set on off hotspot".split())


def _nmcli(argv: Sequence[str]) -> str | None:
    words = set(argv[1:])
    return READ_ONLY if words & {"show", "status", "list"} and not words & _NMCLI_WRITES else None


def _command(argv: Sequence[str]) -> str | None:
    return READ_ONLY if argv[1:2] in (["-v"], ["-V"]) else None


def _pactl(argv: Sequence[str]) -> str | None:
    sub = (_operands(argv) or [""])[0]
    return READ_ONLY if sub in ("list", "info", "stat") or sub.startswith("get-") else None


def _sub(read: str, write: str = "", value_flags: str = "") -> tuple[frozenset[str], ...]:
    """Subcommand tables: read-only names, bounded-write names, flags that take a value.
    A lone `-` in `read` means the program with no subcommand at all is read-only too."""
    return frozenset(read.split()), frozenset(write.split()), frozenset(value_flags.split())


SUBCOMMANDS: dict[str, tuple[frozenset[str], ...]] = {
    "systemctl": _sub(
        "- status is-active is-enabled is-failed show list-units list-unit-files list-timers "
        "list-sockets list-dependencies list-jobs list-machines cat get-default help",
        "reload reload-or-restart try-restart reset-failed daemon-reexec", "-H -M"),
    "apt": _sub("list search show policy depends rdepends showpkg madison changelog -"),
    "apt-get": _sub("-"),
    "snap": _sub("list info find version services changes connections interfaces -"),
    "flatpak": _sub("list info search remote-ls -"),
    "pip": _sub("list show freeze check config inspect -", "install"),
    "pip3": _sub("list show freeze check config inspect -", "install"),
    "loginctl": _sub("- list-sessions list-users list-seats show-session show-user show-seat "
                     "session-status user-status seat-status status"),
    "timedatectl": _sub("- status show list-timezones show-timesync timesync-status"),
    "localectl": _sub("- status list-locales list-keymaps"),
    "hostnamectl": _sub("- status"),
    "networkctl": _sub("- list status lldp"),
    "resolvectl": _sub("- status query statistics"),
    "ufw": _sub("status show version"),
    "nft": _sub("list"),
    "ollama": _sub("- list ps show help"),
    "borg": _sub("list info version"),
    "amixer": _sub("- get sget scontents controls scontrols info"),
    "bluetoothctl": _sub("show devices info paired-devices list version"),
    "systemd-analyze": _sub("- blame critical-chain time verify security calendar timespan "
                            "cat-config unit-paths plot dot"),
    "tmux": _sub("ls list-sessions list-windows list-panes list-clients list-commands "
                 "show-options show-environment show-messages has-session capture-pane "
                 "display-message"),
}

PREDICATES: dict[str, Callable[[Sequence[str]], str | None]] = {
    "sort": _avoiding("o", ("--output",)),
    "date": _avoiding("s", ("--set",)),
    "dmesg": _avoiding("cCDEn", ("--clear", "--read-clear", "--console")),
    "journalctl": _avoiding("", ("--vacuum", "--rotate", "--flush", "--sync", "--relinquish-var",
                                 "--smart-relinquish-var", "--setup-keys", "--update-catalog")),
    "dmidecode": _avoiding("", ("--dump-file",)),
    "ss": _avoiding("K", ("--kill",)),
    "nvidia-smi": _avoiding("", ("--persistence-mode", "--ecc-config", "--reset-ecc-errors",
                                 "--compute-mode", "--gpu-reset", "--power-limit",
                                 "--applications-clocks", "--lock-gpu-clocks",
                                 "--lock-memory-clocks", "--multi-instance-gpu"),
                            _NVIDIA_WRITES),
    "smartctl": _avoiding("sStXCeo", ("--smart", "--test", "--abort", "--captive", "--saveauto",
                                      "--offlineauto", "--set")),
    "kill": lambda argv: READ_ONLY if {"-l", "-L", "--list", "--table"} & set(argv[1:])
    else BOUNDED_WRITE,
    "uniq": lambda argv: READ_ONLY if len(_operands(argv)) <= 1 else None,
    "find": lambda argv: None if _FIND_WRITES & set(argv[1:]) else READ_ONLY,
    "dd": lambda argv: BOUNDED_WRITE if any(a.startswith("of=") for a in argv[1:]) else READ_ONLY,
    "mount": lambda argv: None if _operands(argv) else READ_ONLY,
    "ip": lambda argv: None if _IP_WRITES & set(argv[1:]) else READ_ONLY,
    "sed": _sed, "awk": _awk, "gawk": _awk, "mawk": _awk, "curl": _curl, "tar": _tar,
    "rsync": _rsync, "git": _git, "crontab": _crontab, "dpkg": _dpkg,
    "iptables": _iptables, "ip6tables": _iptables, "sysctl": _sysctl, "command": _command,
    "python": _python, "perl": _only_version, "ruby": _only_version, "node": _only_version,
    "bash": _only_version, "sh": _only_version, "zsh": _only_version, "dash": _only_version,
    "ksh": _only_version, "pactl": _pactl, "nmcli": _nmcli,
    "aplay": lambda argv: READ_ONLY if {"-l", "-L"} & set(argv[1:]) else None,
    "ssh-keygen": _positive("lfEvB", needs="l"),
    "mokutil": _positive("", frozenset({"--sb-state", "--list-enrolled", "--list-new",
                                        "--list-delete", "--db", "--dbx", "--kek", "--pk",
                                        "--mokx", "--help", "--version"})),
}


_CREDENTIAL_NAME = re.compile(
    r"^(?:\.env(?:\.(?!example$|sample$|template$|dist$)[\w.-]+)?|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|credentials|\.netrc|\.pgpass|\.git-credentials|\.npmrc|\.pypirc|g?shadow"
    r"|secrets?\.(?:ya?ml|json|toml)|ssh_host_\w+_key|.+\.(?:key|p12|pfx|kdbx)|secring\.gpg)$")
_SSH_PUBLIC = re.compile(r"^(?:.+\.pub|known_hosts.*|config|authorized_keys.*)$")


def reads_credentials(path: str) -> bool:
    """True when `path` names a file that holds credentials: an env file, a private key, a cloud
    or git credentials file, anything under ~/.aws or ~/.gnupg. Reading one puts the secret in
    front of the model, and from there possibly a remote one, so it needs a tick."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if "/.aws/" in path or "/.gnupg/" in path:
        return True
    if "/.ssh/" in path and not _SSH_PUBLIC.match(name):
        return True
    return bool(_CREDENTIAL_NAME.match(name))


def _program(argv0: str) -> str | None:
    """The program's name, or None when it is a path torra cannot vouch for."""
    if "/" in argv0 and not argv0.startswith(STANDARD_BIN_DIRS):
        return None
    name = argv0.rsplit("/", 1)[-1]
    return "python" if re.fullmatch(r"python[0-9.]*", name) else name


def _subcommand(argv: Sequence[str], value_flags: frozenset[str]) -> str:
    """The first operand that is not a flag's value; `-` when there is none."""
    skip = False
    for tok in argv[1:]:
        if skip:
            skip = False
        elif tok in value_flags:
            skip = True
        elif not tok.startswith("-"):
            return tok
    return "-"


def _lookup(program: str, argv: Sequence[str]) -> str | None:
    if program in READ_ONLY_PROGRAMS:
        return READ_ONLY
    if program in PREDICATES:
        return PREDICATES[program](argv)
    if program in SUBCOMMANDS:
        read, write, value_flags = SUBCOMMANDS[program]
        sub = _subcommand(argv, value_flags)
        return READ_ONLY if sub in read else BOUNDED_WRITE if sub in write else None
    return BOUNDED_WRITE if program in WRITER_PROGRAMS else None


def _destinations(program: str, argv: Sequence[str]) -> list[str]:
    if program == "tar":
        return _tar_paths(argv)
    if program == "dd":
        return [a[3:] for a in argv[1:] if a.startswith("of=")]
    operands = _operands(argv)
    return operands[-1:] if program in LAST_OPERAND_DESTINATION else operands


def trust(argv: Sequence[str], protected: Callable[[str], bool]) -> str | None:
    """READ_ONLY, BOUNDED_WRITE, PROTECTED_WRITE (a known writer aimed at a system path), or None."""
    program = _program(argv[0]) if argv else None
    if program is None:
        return None
    verdict = _lookup(program, argv)
    if verdict == BOUNDED_WRITE and any(protected(d) for d in _destinations(program, argv)):
        return PROTECTED_WRITE
    return verdict
