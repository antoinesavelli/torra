"""SAFETY_FIRST: resolves what a command will actually run, then classifies it.

Depends on: torra.gates.known_programs (what torra can vouch for as read-only),
torra.gates.assignments (which variables a call assigns).
"""

from __future__ import annotations

import base64
import codecs
import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .assignments import UNSEEN, assigned_names
from .known_programs import BOUNDED_WRITE, PROTECTED_WRITE, READ_ONLY, reads_credentials, trust

__all__ = ["RiskLevel", "ResolvedCommand", "Verdict", "SafetyClassifier"]


class RiskLevel(str, Enum):
    """How much damage a command can do if it is wrong."""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    CRITICAL = "critical"


ORDER: list[RiskLevel] = [RiskLevel.SAFE, RiskLevel.CAUTION,
                          RiskLevel.DANGEROUS, RiskLevel.CRITICAL]


@dataclass
class ResolvedCommand:
    """One concrete command extracted from a shell string."""

    argv: list[str]
    source: str
    unwrapped_from: str = ""

    @property
    def program(self) -> str:
        """Executable name with any directory prefix stripped; `python3.12` is `python3`."""
        name = self.argv[0].rsplit("/", 1)[-1] if self.argv else ""
        return "python3" if re.fullmatch(r"python3\.[0-9]+", name) else name

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        d = asdict(self)
        d["program"] = self.program
        return d


@dataclass
class Verdict:
    """Safety classification of one command."""

    command: str
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)
    resolved: list[ResolvedCommand] = field(default_factory=list)
    approved: bool | None = None

    @property
    def needs_confirmation(self) -> bool:
        """True when a human must say yes before this runs."""
        return self.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL)

    @property
    def blocked(self) -> bool:
        """True when the caller must not execute this command."""
        return self.needs_confirmation and self.approved is not True

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return {
            "command": self.command,
            "level": self.level.value,
            "reasons": list(self.reasons),
            "resolved": [r.to_dict() for r in self.resolved],
            "approved": self.approved,
            "needs_confirmation": self.needs_confirmation,
            "blocked": self.blocked,
        }


PROGRAM_RISK: dict[str, tuple[RiskLevel, str]] = {
    "mkfs": (RiskLevel.CRITICAL, "filesystem format"),
    "shred": (RiskLevel.CRITICAL, "destructive overwrite"),
    "wipefs": (RiskLevel.CRITICAL, "filesystem signature wipe"),
    "fdisk": (RiskLevel.CRITICAL, "partition table edit"),
    "parted": (RiskLevel.CRITICAL, "partition table edit"),
    "sgdisk": (RiskLevel.CRITICAL, "partition table edit"),
    "sfdisk": (RiskLevel.CRITICAL, "partition table edit"),
    "blkdiscard": (RiskLevel.CRITICAL, "block device discard"),
    "mkswap": (RiskLevel.CRITICAL, "swap format"),
    "cryptsetup": (RiskLevel.CRITICAL, "disk encryption change"),
    "lvremove": (RiskLevel.CRITICAL, "logical volume removal"),
    "vgremove": (RiskLevel.CRITICAL, "volume group removal"),
    "pvremove": (RiskLevel.CRITICAL, "physical volume removal"),
    "reboot": (RiskLevel.DANGEROUS, "host restart"),
    "poweroff": (RiskLevel.DANGEROUS, "host power off"),
    "halt": (RiskLevel.DANGEROUS, "host halt"),
    "shutdown": (RiskLevel.DANGEROUS, "host shutdown"),
    "pkill": (RiskLevel.DANGEROUS, "broad process kill"),
    "killall": (RiskLevel.DANGEROUS, "broad process kill"),
    "userdel": (RiskLevel.DANGEROUS, "account removal"),
    "truncate": (RiskLevel.DANGEROUS, "file truncation"),
    "chmod": (RiskLevel.CAUTION, "permission change"),
    "chown": (RiskLevel.CAUTION, "ownership change"),
    "mv": (RiskLevel.CAUTION, "file move"),
    "docker": (RiskLevel.CAUTION, "container operation"),
}

PROTECTED_PREFIXES: tuple[str, ...] = (
    "/", "/etc", "/boot", "/usr", "/lib", "/lib64", "/bin", "/sbin", "/var",
    "/sys", "/proc", "/dev", "/home", "/root", "/srv", "/opt",
)

#: Writing here can never lose data or damage the system -- these are stream
#: sinks and documented no-ops, not storage. `/dev` is a protected prefix
#: because `> /dev/sda` should stay CRITICAL, but that same prefix rule was
#: catching the extremely common `> /dev/null` output-suppression idiom and
#: escalating harmless, often read-only commands (e.g. `systemctl status
#: nginx > /dev/null`) to CRITICAL. Exempting these specific targets keeps
#: the block-device case caught without crying wolf on the common one.
SAFE_REDIRECT_TARGETS: frozenset[str] = frozenset({
    "/dev/null", "/dev/zero", "/dev/full", "/dev/stdout", "/dev/stderr",
})
#: Characters that end a shell word when unquoted (besides blanks).
WORD_ENDS = ";|&()<>"
#: `$'...'`, ANSI-C quoting: bash decodes the backslash escapes inside, so `$'\x2fetc'` is /etc.
ANSI_C_QUOTED = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
#: A `$` that starts an expansion (a name, `${`, `$(`, a special parameter), a backtick, or the placeholder
#: `_blank_substitutions` leaves for a `$(...)`; a lone `$` is literal.
EXPANSION = re.compile(r"\$[\w{(@*#?$!-]|`|SUBST")

#: xlsx/docx/pptx/odt/ods/odp are all zip containers -- `PROTECTED_PREFIXES`
#: only recognizes absolute system locations, so a step with a declared
#: `cwd` inside the user's own document folder (an entirely ordinary way to
#: write a task) edits with a bare relative filename and never trips it.
#: Found testing §2.54 for real: `zip -r budget.xlsx ...` with no leading
#: `/` classified SAFE, the exact repack-in-place shape that section closed
#: for absolute paths. A small, closed set of long-stable file formats, not
#: an open-ended list of things to detect -- these extensions haven't
#: changed in over a decade.
DOCUMENT_CONTAINER_EXTENSIONS: tuple[str, ...] = (
    ".xlsx", ".xlsm", ".docx", ".pptx", ".odt", ".ods", ".odp",
)

#: `su`'s `-c` payload is executed by a shell exactly like `sh -c`'s is, even
#: though `su` isn't itself an interpreter -- listing it here is what makes
#: `resolve()` recurse into it as shell syntax instead of leaving it as one
#: opaque, unjudged argv. Found live: a bare `su` was in neither WRAPPERS nor
#: here, so `su -c 'rm -rf /x'` resolved to program "su", no PROGRAM_RISK
#: entry, no `_judge` rule, and silently fell through to SAFE unless the
#: regex BACKSTOP happened to still see the literal text.
INTERPRETERS: tuple[str, ...] = ("sh", "bash", "zsh", "ksh", "dash", "su", "python",
                                 "python3", "perl", "ruby", "node")

WRAPPERS: tuple[str, ...] = ("sudo", "doas", "env", "nohup", "nice", "ionice",
                             "setsid", "time", "timeout", "stdbuf", "xargs")

#: Shell keywords that sit in front of a command (`do rm -rf /x`, `then cat f`) and end a segment
#: that is only loop or branch structure (`for x in a b`, `done`, `fi`). Judging them as programs
#: hid the real command behind them. `case` is deliberately absent: its arms share one segment
#: with the pattern, so it stays an unrecognised program and asks.
CONTROL_PREFIXES: frozenset[str] = frozenset({"do", "then", "else", "elif", "while", "until",
                                              "if", "!", "{"})
STRUCTURE_ONLY: frozenset[str] = frozenset({"for", "done", "fi", "in", "}", ")"})

#: Assignments whose value changes what a later command runs (a search path, a pager, a git helper).
RISKY_ENV: frozenset[str] = frozenset("""
PATH LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT BASH_ENV ENV PAGER MANPAGER EDITOR VISUAL BROWSER SHELL
IFS PYTHONPATH PYTHONSTARTUP PYTHONHOME PERL5OPT PERL5LIB RUBYOPT NODE_OPTIONS SSH_ASKPASS
LESSOPEN LESSCLOSE
""".split())
#: Programs that report only names, sizes and existence, never a file's contents: pointing them at a
#: credentials file reveals no secret.
METADATA_ONLY: frozenset[str] = frozenset({"ls", "dir", "vdir", "stat", "test", "[", "[[", "du", "df",
                                           "find", "realpath", "readlink", "basename", "dirname"})
#: Disk tools that are read-only when they only list or print (see `known_programs._disk_listing`).
LISTING_TOOLS: frozenset[str] = frozenset({"fdisk", "sfdisk", "parted", "sgdisk"})
#: Units whose restart or stop can cut the session torra is running in.
CRITICAL_UNITS: tuple[str, ...] = ("display-manager", "gdm", "lightdm", "sddm", "networkmanager",
                                   "network-manager", "systemd-logind", "ssh", "sshd", "dbus")

#: Package managers whose `install` runs code from whatever source it is pointed at. A plain
#: install from the manager's default index is the ordinary workflow (CAUTION), but an install
#: aimed at a non-default source -- a custom/plain-HTTP index, a URL, a VCS ref, a sideloaded
#: local archive via `--dangerous` -- is supply-chain / RCE territory and must ask for a tick.
#: Found by the 2026-10-24 Phase-5 blind red-team: `pip install --index-url http://<host> pkg`
#: classified CAUTION and so ran without confirmation (the one miss in 30).
PACKAGE_INSTALLERS: frozenset[str] = frozenset({
    "pip", "pip3", "npm", "pnpm", "yarn", "gem", "cargo",
    "apt", "apt-get", "dnf", "pacman", "snap",
})
#: An install subcommand (`npm i`, `yarn add`, `npm ci` included), told apart from a package
#: literally named "install" only by position -- here we only need to know one is present.
INSTALL_VERBS: frozenset[str] = frozenset({"install", "i", "add", "ci"})
#: Long flags that aim an install at a source other than the manager's default index/registry:
#: pip's index/find-links, a custom registry (npm/yarn/cargo), gem's/cargo's source, cargo's
#: `--git`, snap's sideload `--dangerous`. Their mere presence means "not the default source".
UNTRUSTED_SOURCE_FLAGS: frozenset[str] = frozenset({
    "--index-url", "--extra-index-url", "--find-links", "--registry", "--source",
    "--git", "--index", "--clear-sources", "--dangerous",
})
#: An operand (or an attached flag value) that names a remote or VCS source rather than a package
#: name: an http/ftp/git/file URL, or a pip-style VCS ref such as `git+https://...`.
UNTRUSTED_SOURCE_OPERAND = re.compile(
    r"(?i)^(?:(?:git|hg|svn|bzr)\+[a-z]+://|(?:https?|ftps?|git|file)://)")

HEREDOC = re.compile(r"(?<!<)<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(?:.*?\n)?[ \t]*\2[ \t]*(?=\n|$)",
                     re.DOTALL)

# Live wire: HANDOFF.md §5.2
#: Flags that consume the *next* token as their own value, per wrapper --
#: used so _strip doesn't mistake a flag's value for the start of the
#: wrapped command. Found live: `timeout -s KILL 5 kill -9 1` left "KILL"
#: (the value of `-s`, not a flag) unconsumed; _strip's duration-skip only
#: matches digits, so "KILL" became the resolved argv's program name --
#: never "kill" -- and `_judge_kill`'s PID-1 check never ran. There is no
#: regex BACKSTOP entry for "kill of PID 1" (that check is argv-only) to
#: catch it as a fallback the way rm/mkfs/dd are, so the whole command
#: classified SAFE outright for an entirely ordinary shell idiom, not a
#: contrived adversarial string.
WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "xargs": frozenset({"-n", "--max-args", "-I", "-P", "--max-procs", "-L",
                        "--max-lines", "-s", "--max-chars", "-a",
                        "--arg-file", "-d", "--delimiter", "-E"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-h", "--host",
                       "-p", "--prompt"}),
}

#: Regex backstop. Parsing can fail on exotic syntax, and when it does the
#: answer should be the old answer rather than silence.
BACKSTOP: list[tuple[str, RiskLevel, str]] = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-{1,2}[a-zA-Z]*[rR][a-zA-Z]*f|\brm\s+-{1,2}f[a-zA-Z]*[rR]",
     RiskLevel.CRITICAL, "recursive force delete"),
    (r"\bmkfs(\.\w+)?\b", RiskLevel.CRITICAL, "filesystem format"),
    (r"\bdd\b[^|;&]*\bof=/dev/", RiskLevel.CRITICAL, "raw write to a block device"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;\s*:", RiskLevel.CRITICAL, "fork bomb"),
    (r"\bshutil\.rmtree\b", RiskLevel.CRITICAL, "python recursive tree delete"),
    (r"\b(reboot|poweroff|halt|shutdown)\b", RiskLevel.DANGEROUS, "host power state"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", RiskLevel.DANGEROUS, "service interruption"),
    (r"(?<![-\w])rm\b", RiskLevel.DANGEROUS, "file deletion"),  # not `docker run --rm`
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+--force|checkout\s+--\s)",
     RiskLevel.DANGEROUS, "unrecoverable git operation"),
    (r"\|\s*(sudo\s+)?(ba|z|k)?sh\b", RiskLevel.DANGEROUS, "stdin piped into a shell"),
    (r"\bsudo\b", RiskLevel.CAUTION, "privilege escalation"),
    (r"\bsystemctl\s+(start|restart|enable)\b", RiskLevel.CAUTION, "service state change"),
]


def _part_of_a_redirect(command: str, i: int) -> bool:
    """`&` in `2>&1` or `&>file`, `|` in `>|` (overriding noclobber): an operator's, not a separator."""
    ch, before, after = command[i], command[i - 1:i], command[i + 1:i + 2]
    return (ch == "&" and (before in (">", "<") or after == ">")) or (ch == "|" and before == ">")


class SafetyClassifier:
    """Classifies what a command will actually run, not what it looks like.

    Regex over raw text caught five evasions out of twelve on the first pass
    in testing. This resolves first (quotes, `VAR=value`, wrappers, `sh -c`
    and `$(...)` recursion, base64) then classifies resolved argv, keeping
    the regex table only as a backstop -- what stands between an agent's
    hallucinated `rm -rf /var` and the disk.
    """

    def __init__(self, max_depth: int = 4) -> None:
        """Set the recursion limit for unwrapping nested interpreters."""
        self.max_depth = int(max_depth)
        self.parse_failures: list[str] = []
        self.env_risks: list[str] = []
        self.unwrap_limit_hit = False
        #: Every shell text resolve() parsed (the line, each `sh -c`/`eval`/`$(...)`/base64 payload) with
        #: its own `NAME=value` assignments, so classify() checks the redirects of each (HANDOFF.md §2.89).
        self.shell_texts: list[tuple[str, dict[str, str]]] = []

    # -- resolution -------------------------------------------------------- #

    @staticmethod
    def split_segments(command: str) -> list[str]:
        """Split a shell line into segments, respecting quotes."""
        # A naive re.split on ";" tears apart the payload of
        # `python3 -c "import shutil;shutil.rmtree('/x')"`, which is exactly
        # how a destructive command escapes classification.
        segments: list[str] = []
        buf: list[str] = []
        quote: str | None = None
        escaped = False
        i, n = 0, len(command)

        # Character-by-character state machine; the branch order below is
        # the precedence it depends on: a backslash-escaped char is never a
        # separator even inside a quote, a quoted char is never a separator
        # even if it's a quote of the other kind, and only once neither of
        # those applies does an operator or separator actually split.
        while i < n:
            ch = command[i]
            if escaped:
                buf.append(ch)
                escaped = False
            elif ch == "\\":
                buf.append(ch)
                escaped = True
            elif quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif command[i:i + 2] in ("&&", "||"):
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            elif _part_of_a_redirect(command, i):
                buf.append(ch)
            elif ch in (";", "|", "&", "\n"):
                segments.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1

        segments.append("".join(buf))
        return [s.strip() for s in segments if s.strip()]

    @staticmethod
    def _mask_quoted(text: str) -> str:
        """Blank every character inside a quoted span (space, same length
        and position) so `_check_redirect`'s raw-text `>` scan can't mistake
        an ordinary in-string character for a real shell redirect, which has
        no meaning inside quotes. Found testing real xlsx-editing scripts:
        `print(wb.active.max_row > 2)` -- an ordinary Python comparison
        inside a `-c` payload -- read as an output redirect and escalated to
        CAUTION. Same quote/escape state machine as split_segments above,
        repurposed to mask instead of split, rather than a second,
        potentially-diverging implementation of the same quoting rules."""
        out: list[str] = []
        quote: str | None = None
        escaped = False
        for ch in text:
            in_quote = quote is not None
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif quote:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            out.append(" " if in_quote else ch)
        return "".join(out)

    def _tokenize(self, segment: str) -> list[str]:
        """Tokenize one segment, recording rather than swallowing failures."""
        try:
            return shlex.split(segment, comments=True)
        except ValueError as exc:
            self.parse_failures.append(f"{segment!r}: {exc}")
            return segment.split()

    @staticmethod
    def _env_runs_a_string(out: list[str]) -> bool:
        """`env -S 'cmd args'` and `env --split-string=...` run a command string."""
        return out[0].rsplit("/", 1)[-1] == "env" and any(
            a.startswith(("-S", "--split-string")) for a in out[1:4])

    @staticmethod
    def _consume_wrapper(out: list[str]) -> bool:
        """Pop a leading wrapper program with its flags and duration, if
        present. Mutates `out` in place; returns whether anything was consumed."""
        if not out or out[0].rsplit("/", 1)[-1] not in WRAPPERS:
            return False
        if SafetyClassifier._env_runs_a_string(out):
            return False  # leave `env` as the program, which asks
        wrapper = out.pop(0).rsplit("/", 1)[-1]
        value_flags = WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
        while out and (out[0] == "--" or out[0].startswith("-")):
            flag = out.pop(0)
            if flag == "--":
                break
            # A flag known to take a value consumes the next token too --
            # unless it was given as "--flag=value", which already carries
            # its value in the one token just popped.
            if flag in value_flags and "=" not in flag and out:
                out.pop(0)
        if out and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", out[0]):
            out.pop(0)
        return True

    def _strip(self, argv: list[str], env: dict[str, str]) -> list[str]:
        """Remove leading VAR=value assignments and wrapper programs."""
        # Repeats until nothing changes because wrappers nest: `env FOO=bar
        # sudo timeout 5 rm -rf /x` has an assignment, then a wrapper, then
        # another assignment-shaped token could reappear after a wrapper is
        # peeled (some wrappers accept their own VAR=value-looking flags). A
        # single pass would stop after the first layer and misclassify by
        # whatever program was still hidden underneath.
        out = list(argv)
        changed = True
        while out and changed:
            changed = False
            while out and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", out[0]):
                name, _, value = out[0].partition("=")
                env[name] = value
                self._note_assignment(name)
                out.pop(0)
                changed = True
            if out and out[0] in CONTROL_PREFIXES:
                out.pop(0)  # `do rm -rf /x`: the keyword is not the command
                changed = True
            if self._consume_wrapper(out):
                changed = True
        return out

    def _note_assignment(self, name: str) -> None:
        """Record a variable whose value changes what a later command runs."""
        if name in RISKY_ENV or name.startswith("GIT_") or name == UNSEEN:
            self.env_risks.append(name)

    def _note_builtin_assignments(self, argv: list[str]) -> None:
        """`export PATH=/x`, `read PATH`, `for PATH in /x`: every assignment besides a leading `NAME=`."""
        for name in assigned_names(argv):
            self._note_assignment(name)

    @staticmethod
    def _unparen(segment: str) -> str:
        """Drop the grouping parentheses `(cd /x && ls)` leaves glued to its first and last word."""
        segment = segment.lstrip("( \t")
        while segment.endswith(")") and segment.count(")") > segment.count("("):
            segment = segment[:-1].rstrip()
        return segment

    @staticmethod
    def _substitute(argv: list[str], env: dict[str, str]) -> list[str]:
        """Expand $VAR and ${VAR} from assignments seen earlier in the line."""
        out = []
        for token in argv:
            for name, value in env.items():
                token = token.replace(f"${{{name}}}", value).replace(f"${name}", value)
            out.append(token)
        return out

    def _decode_base64(self, command: str, require_pipe_to_shell: bool = True) -> str | None:
        """Decode a literal base64 blob piped into a shell.

        Found live: base64's own alphabet has no whitespace, so unquoted
        `echo BLOB | base64 -d | sh` used to evade the quote-requiring blob
        pattern. Matching a bounded charset run instead (no quotes needed)
        catches both forms -- the charset itself is the boundary.
        `require_pipe_to_shell=False` (for `_resolve_substitutions`) skips
        the `| sh` check: `$(echo BLOB | base64 -d)` proves it will run
        just by being inside a substitution span.
        """
        if not re.search(r"base64\s+(-d|--decode)", command):
            return None
        if require_pipe_to_shell and not re.search(r"\|\s*(sudo\s+)?(ba|z|k|da)?sh\b", command):
            return None
        # Live wire: HANDOFF.md §5.1
        for blob in re.findall(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{8,}(?![A-Za-z0-9+/=])",
                               command):
            try:
                decoded = base64.b64decode(blob, validate=True).decode("utf-8", "replace")
                if decoded.strip():
                    return decoded
            except Exception as exc:
                self.parse_failures.append(f"base64 decode failed: {exc}")
        return None

    @staticmethod
    def _matching_paren(command: str, open_at: int) -> int | None:
        """Index just past the `)` balancing the `(` at `open_at`, or None."""
        depth, j, n = 1, open_at, len(command)
        while j < n and depth > 0:
            depth += 1 if command[j] == "(" else -1 if command[j] == ")" else 0
            j += 1
        return j if depth == 0 else None

    def _find_command_substitutions(self, command: str) -> list[str]:
        """Every `$(...)`/backtick span's inner text -- captured stdout
        becomes the text that runs next, like a `sh -c` payload.

        Found live: `$(echo BLOB|base64 -d)` classified SAFE outright --
        `split_segments` split on the `|` *inside* the substitution, and
        `_decode_base64` never saw a pipe to `sh`, only to `base64`.
        Ignores quote context deliberately (a false positive here costs
        one prompt; a false negative is the bypass this closes)."""
        spans: list[str] = []
        i, n = 0, len(command)
        while i < n:
            if command[i:i + 2] in ("$(", "<(", ">("):
                j = self._matching_paren(command, i + 2)
                if j is None:
                    break
                spans.append(command[i + 2:j - 1])
                i = j
            elif command[i] == "`":
                j = command.find("`", i + 1)
                if j == -1:
                    break
                spans.append(command[i + 1:j])
                i = j + 1
            else:
                i += 1
        return spans

    def _blank_substitutions(self, command: str) -> str:
        """Swap each `$(...)`/backtick span for a placeholder word. The span is judged on its own
        (`_resolve_substitutions`); its inner spaces and `|` must not split or garble the outer
        command, as in `VERSION=$(git rev-parse HEAD)`."""
        for span in self._find_command_substitutions(command):
            for opener in ("$(", "<(", ">(", "`"):
                closer = "`" if opener == "`" else ")"
                command = command.replace(f"{opener}{span}{closer}", "SUBST", 1)
        return command

    @staticmethod
    def _strip_heredocs(command: str) -> str:
        """Drop a heredoc body fed to a program that only reads it (`cat <<EOF`): it is data. One
        fed to an interpreter stays, because the interpreter runs it."""
        def keep_or_drop(m: re.Match[str]) -> str:
            lead = command[command.rfind("\n", 0, m.start()) + 1:m.start()].split()[:1]
            runner = lead[0].rsplit("/", 1)[-1] if lead else ""
            if runner in INTERPRETERS or runner in WRAPPERS:
                return m.group(0)
            return f"<<{m.group(2)}{m.group(3)}"
        return HEREDOC.sub(keep_or_drop, command)

    def _resolve_substitutions(self, command: str, depth: int) -> list[ResolvedCommand]:
        """Recurse into every command-substitution span -- captured stdout is what runs next."""
        out: list[ResolvedCommand] = []
        for sub in self._find_command_substitutions(command):
            decoded = self._decode_base64(sub, require_pipe_to_shell=False)
            if decoded:
                out += [ResolvedCommand(r.argv, r.source, "base64 via substitution")
                       for r in self.resolve(decoded, depth + 1)]
            out += [ResolvedCommand(r.argv, r.source, "command substitution")
                   for r in self.resolve(sub, depth + 1)]
        return out

    @staticmethod
    def _expand_ifs(segment: str) -> str:
        """`${IFS}`/`$IFS` between tokens is a well-known obfuscation: bash
        expands IFS (default: space/tab/newline) and field-splits on it, so
        `rm${IFS}-rf${IFS}/` runs like `rm -rf /`. Found live: a whitespace
        tokenizer saw one token instead, and the command downgraded from
        CRITICAL to DANGEROUS -- still gated, but the wrong severity."""
        return re.sub(r"\$\{?IFS\}?", " ", segment)

    def resolve(self, command: str, depth: int = 0) -> list[ResolvedCommand]:
        """Expand a shell line into the concrete commands it will run."""
        # `depth` bounds two different recursions that could otherwise not
        # terminate on adversarial input: a base64 blob that decodes to
        # another base64-piped-to-sh command, and a `sh -c` payload that
        # itself contains another `sh -c`. Hitting the limit is recorded, and
        # `_context_findings` makes the line DANGEROUS: what runs past this
        # point is unseen, and unseen used to read as SAFE (GRADE_TO_A_PLAN P1.3).
        if depth > self.max_depth:
            self.parse_failures.append(f"max unwrap depth hit on {command!r}")
            self.unwrap_limit_hit = True
            return []

        resolved: list[ResolvedCommand] = []
        decoded = self._decode_base64(command)
        if decoded:
            resolved += [ResolvedCommand(r.argv, r.source, "base64 payload")
                         for r in self.resolve(decoded, depth + 1)]
        resolved += self._resolve_substitutions(command, depth)

        env: dict[str, str] = {}
        # env is filled below. A payload's data-only heredoc bodies are dropped first, so the message
        # in `git commit -m "$(cat <<'EOF' ... EOF)"` is not read as shell; the line itself is kept whole.
        self.shell_texts.append((command if depth == 0 else self._strip_heredocs(command), env))
        for segment in self.split_segments(self._blank_substitutions(self._strip_heredocs(command))):
            segment = self._unparen(self._expand_ifs(segment))
            argv = self._substitute(self._strip(self._tokenize(segment), env), env)
            if not argv:
                continue
            self._note_builtin_assignments(argv)  # before the skip: `for PATH in /x` assigns
            if argv[0] in STRUCTURE_ONLY:
                continue
            resolved += self._unwrap(argv, segment, depth)
        return resolved

    def _unwrap(self, argv: list[str], segment: str, depth: int) -> list[ResolvedCommand]:
        """The commands one segment runs: its `eval` or `sh -c` payload, or itself."""
        program = ResolvedCommand(argv, segment).program
        out: list[ResolvedCommand] = []
        if program == "eval" and len(argv) > 1:
            out += [ResolvedCommand(r.argv, r.source, "eval")
                    for r in self.resolve(" ".join(argv[1:]), depth + 1)]
        at = next((i for i, a in enumerate(argv[:-1]) if re.fullmatch(r"-[A-Za-z]*c", a)), None)
        payload = argv[at + 1] if program in INTERPRETERS and at is not None else ""
        if not payload:
            return out + [ResolvedCommand(argv, segment)]
        # Only a *shell* interpreter's -c payload is worth recursing into as more shell
        # syntax. Python/Perl/Ruby/Node source is not shell grammar, so it is kept as one
        # opaque ResolvedCommand and left to _judge's checks of the source text.
        if program.startswith("python") or program in ("perl", "ruby", "node"):
            return out + [ResolvedCommand(argv, segment, f"{program} -c source")]
        return out + [ResolvedCommand(r.argv, r.source, f"{program} -c")
                      for r in self.resolve(payload, depth + 1)]

    # -- classification ---------------------------------------------------- #

    @staticmethod
    def _protected(path: str) -> bool:
        """True when a path targets a system location worth protecting."""
        if path.startswith(("~", "$HOME", "${HOME}")):
            return True  # the home directory, which /home already protects by its absolute path
        cleaned = posixpath.normpath(re.sub(r"/{2,}", "/", path.rstrip("/*") or "/"))
        return cleaned in PROTECTED_PREFIXES or any(
            cleaned.startswith(p + "/") for p in PROTECTED_PREFIXES if p != "/")

    def protects(self, path: str) -> bool:
        """Public form of `_protected`, for steps torra builds itself (a remote pull's local file)."""
        return self._protected(path)

    @staticmethod
    def _looks_like_document_container(path: str) -> bool:
        """True when a path's extension is a zip-based office document
        format, regardless of whether it's absolute or relative -- see
        DOCUMENT_CONTAINER_EXTENSIONS for why this exists alongside, not
        instead of, `_protected`."""
        return path.rsplit("/", 1)[-1].lower().endswith(DOCUMENT_CONTAINER_EXTENSIONS)

    @staticmethod
    def _argv_shape(argv: list[str]) -> tuple[list[str], list[str], set[str]]:
        """Split argv into flags, operands, and single-char short-flag letters."""
        flags = [a for a in argv[1:] if a.startswith("-")]
        operands = [a for a in argv[1:] if not a.startswith("-")]
        chars = set("".join(f.lstrip("-") for f in flags if not f.startswith("--")))
        return flags, operands, chars

    def _judge(self, rc: ResolvedCommand) -> list[tuple[RiskLevel, str]]:
        """Risk findings for one resolved command, dispatched per program.

        One recognized program, one rule, one `_judge_*` helper holding
        that program's real logic -- not a lookup table encoding risk
        levels or a class per program. classify() takes the max of this
        and the regex BACKSTOP, so a program missing here is still caught.
        """
        found: list[tuple[RiskLevel, str]] = []
        program, argv = rc.program, rc.argv
        flags, operands, chars = self._argv_shape(argv)
        protected = any(self._protected(o) for o in operands)
        overwrites_a_document = any(self._looks_like_document_container(o) for o in operands)

        if program not in METADATA_ONLY and any(reads_credentials(o) for o in operands):
            found.append((RiskLevel.DANGEROUS, "touches a file that holds credentials"))

        if program in PROGRAM_RISK and not (
                program in LISTING_TOOLS and trust(rc.argv, self._protected) == READ_ONLY):
            level, why = PROGRAM_RISK[program]
            found.append((level, f"{why} via {program}"))

        rules: list[tuple[bool, Callable[[], list[tuple[RiskLevel, str]]]]] = [
            (program == "rm", lambda: self._judge_rm(chars, flags, protected)),
            (program == "dd", lambda: self._judge_dd(argv)),
            (program == "find", lambda: self._judge_find(argv, protected)),
            (program == "systemctl", lambda: self._judge_systemctl(argv)),
            (program == "git", lambda: self._judge_git(argv)),
            (program in ("apt", "apt-get", "dnf", "pacman", "snap"),
             lambda: self._judge_package_manager(argv)),
            (program in PACKAGE_INSTALLERS,
             lambda: self._judge_package_install(program, argv)),
            (program in ("python", "python3", "perl", "ruby", "node"),
             lambda: self._judge_script_source(program, argv)),
            (program == "kill", lambda: self._judge_kill(argv)),
            (program == "docker", lambda: self._judge_docker(argv)),
            (program in ("chmod", "chown"),
             lambda: self._judge_chmod_chown(program, protected)),
            (program in ("tee", "cp", "mv", "zip"),
             # `zip`'s output archive is a write target exactly like cp/mv's
             # destination -- xlsx/odt are both zip containers, and repacking
             # one over an existing path (extract, edit the inner XML,
             # `zip -r original.file ...`) is as much an overwrite as `cp`/
             # `mv` onto the same path, which this same protected-prefix
             # check already catches. `unzip` deliberately excluded: its
             # archive argument is a read source, not a write target, so
             # reusing this generic operand-protected check for it would
             # misclassify an ordinary extraction of an existing file as
             # "writes to a system path".
             lambda: self._judge_tee_cp_mv_zip(program, protected, overwrites_a_document)),
        ]
        for matched, call in rules:
            if matched:
                found += call()
        return found or self._judge_by_trust(rc)

    def _judge_by_trust(self, rc: ResolvedCommand) -> list[tuple[RiskLevel, str]]:
        """No rule objected, so only a program torra can vouch for runs without a tick."""
        verdict = trust(rc.argv, self._protected)
        if verdict == READ_ONLY:
            return []
        if verdict == BOUNDED_WRITE:
            return [(RiskLevel.CAUTION, f"{rc.program} writes files or state")]
        if verdict == PROTECTED_WRITE:
            return [(RiskLevel.DANGEROUS, f"{rc.program} writes to a system path")]
        return [(RiskLevel.DANGEROUS,
                 f"unrecognised program {rc.program!r}: not known to be read-only")]

    @staticmethod
    def _judge_rm(chars: set[str], flags: list[str],
                  protected: bool) -> list[tuple[RiskLevel, str]]:
        recursive = "r" in chars or "R" in chars or "--recursive" in flags
        forced = "f" in chars or "--force" in flags
        if recursive and forced:
            return [(RiskLevel.CRITICAL, "recursive force delete")]
        if recursive or protected:
            return [(RiskLevel.CRITICAL if protected else RiskLevel.DANGEROUS,
                     "recursive or system-path delete")]
        return [(RiskLevel.DANGEROUS, "file deletion")]

    @staticmethod
    def _judge_dd(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if any(a.startswith("of=/dev/") for a in argv):
            return [(RiskLevel.CRITICAL, "raw write to a block device")]
        return []

    @staticmethod
    def _judge_find(argv: list[str], protected: bool) -> list[tuple[RiskLevel, str]]:
        if "-delete" in argv or "-exec" in argv:
            return [(RiskLevel.CRITICAL if protected else RiskLevel.DANGEROUS,
                     "find with -delete or -exec")]
        return []

    @staticmethod
    def _judge_systemctl(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if any(a in ("stop", "disable", "mask") for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "service interruption")]
        if any(a in ("restart", "reload", "isolate") for a in argv[1:]) and any(
                a.lower().removesuffix(".service") in CRITICAL_UNITS for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "restarting a unit the current session depends on")]
        if any(a in ("start", "restart", "enable", "daemon-reload") for a in argv[1:]):
            return [(RiskLevel.CAUTION, "service state change")]
        return []

    @staticmethod
    def _judge_git(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if re.search(
                r"\breset\s+--hard\b|\bclean\s+-[a-z]*f|\bpush\s+--force\b"
                r"|\bcheckout\s+--\s|\bstash\s+(drop|clear)\b", " ".join(argv[1:])):
            return [(RiskLevel.DANGEROUS, "unrecoverable git operation")]
        return []

    @staticmethod
    def _judge_package_manager(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if any(a in ("remove", "purge", "autoremove", "uninstall") for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "package removal")]
        if "install" in argv[1:] and any(a.endswith("-") and not a.startswith("-") for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "an install that also removes a package (name-)")]
        if any(a in ("install", "upgrade", "update", "full-upgrade") for a in argv[1:]):
            return [(RiskLevel.CAUTION, "package change")]
        return []

    @staticmethod
    def _judge_package_install(program: str, argv: list[str]) -> list[tuple[RiskLevel, str]]:
        """A package install from a non-default source runs code torra cannot vouch for.

        `install` runs the fetched package's build/setup code as the invoking user, so pointing
        it at a custom index, a URL, a VCS ref, or a sideloaded archive is supply-chain / RCE
        territory and must ask for a tick -- not run at the CAUTION a plain default-index install
        gets. A plain `pip install <name>` (or a local `pip install .` / `-e .`) stays CAUTION:
        it is the ordinary workflow, and over-blocking it would train the operator to wave every
        install through. Returning `[]` here lets that normal path fall through to its usual
        CAUTION; only a non-default source is escalated.
        """
        rest = argv[1:]
        operands = [a for a in rest if not a.startswith("-")]
        if not INSTALL_VERBS & set(operands):
            return []
        flag_names = {a.split("=", 1)[0] for a in rest if a.startswith("-")}
        flag_values = [a.split("=", 1)[1] for a in rest if a.startswith("-") and "=" in a]
        untrusted = bool(flag_names & UNTRUSTED_SOURCE_FLAGS) or any(
            UNTRUSTED_SOURCE_OPERAND.match(a) for a in operands + flag_values)
        if untrusted:
            return [(RiskLevel.DANGEROUS,
                     f"{program} install from a non-default source (custom index, URL, or VCS) "
                     "-- runs fetched code, a supply-chain / RCE risk")]
        return []

    @staticmethod
    def _call_signature(source: str, start: int, end: int) -> str | None:
        """The call a quoted literal at source[start:end] is an argument to
        -- "load_workbook" for `openpyxl.load_workbook('x.xlsx')`, or
        "open:write"/"open:read" for Python's own `open()`, whose read and
        write paths share one name and are told apart only by the mode
        argument that follows. None when the literal isn't immediately
        `name(` -- e.g. a bare comparison operand -- rather than a call
        argument."""
        before = re.search(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(\s*$", source[:start])
        if not before:
            return None
        name = before.group(1).rsplit(".", 1)[-1]
        if name != "open":
            return name
        mode = re.match(r"""^\s*,\s*(?:mode\s*=\s*)?['"]([rwaxb+]{1,4})['"]""", source[end:])
        return "open:write" if mode and any(c in mode.group(1) for c in "wax+") else "open:read"

    @staticmethod
    def _repeated_path_literal(source: str) -> str | None:
        """A quoted, path-shaped literal appearing 2+ times in interpreter
        source is the shape of a read-then-write-back on the same file --
        `openpyxl.load_workbook('x.xlsx') ... wb.save('x.xlsx')`,
        `Document('x.docx') ... doc.save('x.docx')` -- regardless of which
        library is doing it. Matching the repeated literal itself, not a
        per-library method name, is deliberate: enumerating "save" spellings
        library by library is exactly the open-ended list HANDOFF.md §3
        already warns is a losing shape. Found via
        scripts/dev/model_bench/domain_exploration.py's xlsx-edit scenario
        (2026-08-28, devstral:24b): a `python3 -c` step loaded an existing
        spreadsheet and saved back over the same path with no backup or
        declared rollback, and classified SAFE -- neither the regex BACKSTOP
        nor this function (before this check existed) has any notion of an
        interpreter payload overwriting a path it also read.

        Counting bare occurrences over-fired on ordinary reads of the same
        path twice (comparing a file to itself, validating then processing
        it) -- found testing this for real against actual xlsx files, zero
        writes anywhere, still classified DANGEROUS. A read-then-write has a
        real, checkable signature instead: two *differently*-named calls
        touching the same literal (`load_workbook`/`save`,
        `read_excel`/`to_excel`) versus the same call repeated
        (`load_workbook`/`load_workbook` -- just read twice). `open()` is
        the one call whose read and write paths share a name, so it gets
        its own mode-aware signature via `_call_signature` rather than
        falling through this same-name-means-safe rule."""
        occurrences: dict[str, list[str | None]] = {}
        for m in re.finditer(r"'([^']*)'|\"([^\"]*)\"", source):
            literal = m.group(1) or m.group(2)
            if not literal or " " in literal:
                continue
            if "/" in literal or re.search(r"\.[A-Za-z0-9]{1,5}$", literal):
                occurrences.setdefault(literal, []).append(
                    SafetyClassifier._call_signature(source, m.start(), m.end()))
        for literal, sigs in occurrences.items():
            if len(sigs) < 2:
                continue
            if "open:write" in sigs or len(set(sigs) - {"open:read"}) >= 2:
                return literal
        return None

    @staticmethod
    def _judge_script_source(program: str, argv: list[str]) -> list[tuple[RiskLevel, str]]:
        # `\bunlink\b`, not `\.unlink\(` -- Perl's bareword `unlink(...)` /
        # `unlink glob(...)` has no leading dot, unlike Python's os.unlink,
        # and the old pattern required the dot so it never matched.
        found: list[tuple[RiskLevel, str]] = []
        source = " ".join(argv)
        if re.search(r"rmtree|os\.remove|\bunlink\b|subprocess|os\.system"
                     r"|unlinkSync|rmSync", source):
            found.append((RiskLevel.DANGEROUS,
                         f"{program} source performs filesystem or process operations"))
        reopened = SafetyClassifier._repeated_path_literal(source)
        if reopened:
            found.append((RiskLevel.DANGEROUS,
                         f"{program} source reopens {reopened!r} -- looks like an "
                         "in-place overwrite with no declared backup or rollback"))
        return found

    @staticmethod
    def _kill_targets(argv: list[str]) -> tuple[list[str], bool]:
        """The pid operands of a `kill` line, and whether it only lists signals."""
        skip_next, list_mode, signalled, pids = False, False, False, []
        for tok in argv[1:]:
            if skip_next:
                skip_next = False
            elif tok in ("-s", "-n", "--signal"):
                skip_next = signalled = True
            elif tok in ("-l", "-L", "--list", "--table"):
                list_mode = True
            elif signalled and re.fullmatch(r"-\d+", tok):
                pids.append(tok)  # `kill -9 -1`: the signal is chosen, so this is a pid
            elif tok.startswith("-"):
                signalled = True
            else:
                pids.append(tok)
        return pids, list_mode

    @staticmethod
    def _judge_kill(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        # `-9` alone used to be the only signal spec recognized here --
        # `kill -s KILL 1`, `kill -SIGKILL 1`, `kill -s 9 1`, and plain
        # `kill 1` (default SIGTERM) all classified SAFE. Found in a
        # full-file audit. Walking argv properly (rather than reusing the
        # generic `operands` split) matters here specifically: `-s`/`-n`/
        # `--signal` consume the *next* token as their argument, not a
        # PID -- `kill -s 1 12345` sends SIGHUP to PID 12345 and never
        # touches PID 1, and naively treating "1" as an operand there
        # would flag a command that is not dangerous.
        pids, list_mode = SafetyClassifier._kill_targets(argv)
        if list_mode:
            return []
        if "1" in pids:
            return [(RiskLevel.CRITICAL, "kill of PID 1")]
        if any(p == "0" or p.startswith("-") for p in pids):
            return [(RiskLevel.CRITICAL, "kill of a whole process group or every process")]
        if not pids or not all(p.isdigit() for p in pids):
            return [(RiskLevel.DANGEROUS, "kill of a process torra cannot name (computed pid)")]
        return []

    @staticmethod
    def _judge_docker(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        words = set(argv[1:])
        if words & {"rm", "rmi", "kill", "prune"} or (
                "down" in words and words & {"-v", "--volumes"}):
            return [(RiskLevel.DANGEROUS, "removes containers, images or volumes")]
        if "--privileged" in words or words & {"--pid=host", "--cap-add=ALL"}:
            return [(RiskLevel.DANGEROUS, "runs a container with host-level privileges")]
        mounts = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-v", "--volume")]
        mounts += [a.split("=", 1)[1] for a in argv if a.startswith("--volume=")]
        if any(SafetyClassifier._protected(m.split(":", 1)[0]) for m in mounts):
            return [(RiskLevel.DANGEROUS, "mounts a system or home directory into a container")]
        return []

    @staticmethod
    def _judge_chmod_chown(program: str, protected: bool) -> list[tuple[RiskLevel, str]]:
        if protected:
            return [(RiskLevel.CRITICAL, f"{program} on a system path")]
        return []

    @staticmethod
    def _judge_tee_cp_mv_zip(program: str, protected: bool,
                              overwrites_a_document: bool) -> list[tuple[RiskLevel, str]]:
        if protected:
            return [(RiskLevel.DANGEROUS, f"{program} writes to a system path")]
        if overwrites_a_document:
            return [(RiskLevel.DANGEROUS,
                     f"{program} writes to a path that looks like an existing office "
                     "document (xlsx/docx/pptx/odt/ods/odp are zip containers -- a "
                     "relative path here still isn't a protected system prefix)")]
        return []

    @staticmethod
    def _check_backstop(text: str) -> list[tuple[RiskLevel, str]]:
        """Regex backstop: catches whatever resolve() failed to parse."""
        return [(level, f"{why} [regex backstop]") for pattern, level, why in BACKSTOP
                if re.search(pattern, text, re.IGNORECASE)]

    @staticmethod
    def _redirect_word(rest: str) -> str | None:
        """The word bash reads at the start of `rest`: quotes removed, `$'..'` escapes decoded, ended
        by a blank or an unquoted `;|&()<>`. A `$(` becomes a backtick, so the substitution stays in the
        word instead of ending it. None when it cannot be read (an unclosed quote, a bad escape)."""
        try:
            rest = ANSI_C_QUOTED.sub(lambda m: shlex.quote(codecs.decode(m.group(1), "unicode_escape")), rest)
            rest = rest.replace('$"', '"').replace("$(", "`")
            lexer = shlex.shlex(rest, posix=True, punctuation_chars=WORD_ENDS)
            lexer.whitespace_split, lexer.commenters = True, ""
            return lexer.get_token() or ""
        except ValueError:
            return None

    @staticmethod
    def _masked_word_end(masked: str, at: int) -> int:
        """Where the word after `at` ends in quote-masked text, whose quoted characters are already blank."""
        while at < len(masked) and masked[at] in " \t":
            at += 1
        while at < len(masked) and not masked[at].isspace() and masked[at] not in WORD_ENDS:
            at += 1
        return at

    def _check_redirects(self) -> list[tuple[RiskLevel, str]]:
        """`_check_redirect` over every shell text resolve() parsed, each finding once: a payload is
        quoted in the line that carries it, so the line's own scan never sees its `>`."""
        return list(dict.fromkeys(f for text, env in self.shell_texts for f in self._check_redirect(text, env)))

    def _check_redirect(self, text: str, env: dict[str, str]) -> list[tuple[RiskLevel, str]]:
        """Redirect-target risk: a concern neither _judge nor BACKSTOP looks
        at. Captures the operator (`>`/`>>`) alongside the target -- found
        in a full-file audit: classification was already correct for `>>`,
        but the reason text always said "overwrites" even for an append.

        Matches against `_mask_quoted(text)`, not the raw text: a bare `>`
        has no meaning inside a shell quote, and this used to fire on any
        `>` character anywhere, including a Python comparison operator
        inside a `-c` payload's own quotes. The target is then read from
        the original `text` after that operator, as bash reads the word
        (quotes removed) -- masking only blanks in-quote characters, so
        positions are identical in both strings."""
        masked = self._mask_quoted(text)
        writes_a_file = False
        # Only an unquoted `>`, `>>`, `>|` or `>&` redirects, but its target is read from the original
        # text the way bash reads the word: `> "/etc/passwd"` writes /etc/passwd (HANDOFF.md §2.86-§2.88).
        for redirect in re.finditer(r">[>|&]?", masked):
            op, target = redirect.group(), self._redirect_word(text[redirect.end():])
            if target is None:
                return [(RiskLevel.DANGEROUS, "redirect target torra cannot read")]
            target = self._expand(target, env)
            if op == ">&" and re.fullmatch(r"\d*-?", target):
                continue  # `2>&1`, `>&2`, `>&-`: a descriptor, not a file
            if target in SAFE_REDIRECT_TARGETS:
                # A stream sink or documented no-op; nothing is overwritten. Blanked so the
                # generic check below ignores it too, while a later redirect is still judged.
                end = self._masked_word_end(masked, redirect.end())
                masked = masked[:redirect.start()] + " " * (end - redirect.start()) + masked[end:]
                continue
            found = self._judge_redirect_target(op, target)
            if found:
                return [found]
            writes_a_file = writes_a_file or op == ">&"
        if writes_a_file or re.search(r">(?!&)\s*[^>\s]", masked):
            return [(RiskLevel.CAUTION, "output redirect that overwrites")]
        return []

    @staticmethod
    def _expand(word: str, env: dict[str, str]) -> str:
        """`$NAME` and `${NAME}` replaced by a value the line assigned; any other expansion is left as it is."""
        return re.sub(r"\$(?:\{(\w+)\}|(\w+))", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), word)

    def _judge_redirect_target(self, op: str, target: str) -> tuple[RiskLevel, str] | None:
        """CRITICAL for a system path, DANGEROUS for a target only known at run time, else None."""
        if target in ("", "/"):
            return None  # a bare `/` cannot be written ("Is a directory"); markdown `<dir>/<name>` reads as one
        if self._protected(target):
            return RiskLevel.CRITICAL, f"redirect {'appends to' if op == '>>' else 'overwrites'} {target}"
        if EXPANSION.search(target) and target.startswith(("$", "`", "/")):
            return RiskLevel.DANGEROUS, f"redirect target {target} is only known at run time"
        return None

    def classify(self, command: str) -> Verdict:
        """Classify a command by resolving it, with regex as the backstop."""
        # Three independent signal sources feed `levels`, and the final
        # verdict is the max across all of them, never just the first or the
        # most specific: argv-based _judge (resolved, evasion-resistant),
        # the regex BACKSTOP (catches whatever resolve() failed to parse),
        # and the redirect-target check (a concern neither of the other two
        # looks at). A command only needs one of the three to flag it; this
        # is deliberately not an either/or so a gap in one layer doesn't
        # silently downgrade what another layer already caught.
        text = (command or "").strip()
        self.parse_failures = []
        self.shell_texts = []
        self.env_risks = []
        self.unwrap_limit_hit = False
        reasons: list[str] = []
        levels: list[RiskLevel] = []

        resolved = self.resolve(text)
        for rc in resolved:
            for level, why in self._judge(rc):
                origin = f" (via {rc.unwrapped_from})" if rc.unwrapped_from else ""
                reasons.append(f"{level.value.upper()}: {why}{origin} "
                               f"[argv: {' '.join(rc.argv[:6])}]")
                levels.append(level)

        for level, why in self._check_backstop(text) + self._check_redirects():
            levels.append(level)
            reasons.append(f"{level.value.upper()}: {why}")

        if self.parse_failures:
            reasons.append(f"NOTE: {len(self.parse_failures)} parse issue(s), "
                           "the regex backstop carried the classification")

        for level_, why in self._context_findings(resolved, levels, text):
            levels.append(level_)
            reasons.append(f"{level_.value.upper()}: {why}")
        level = max(levels, key=ORDER.index) if levels else RiskLevel.SAFE
        if not reasons:
            reasons.append(f"no destructive pattern in "
                           f"{len(resolved)} resolved command(s)")
        return Verdict(text, level, reasons, resolved)

    def _context_findings(self, resolved: list[ResolvedCommand], levels: list[RiskLevel],
                          text: str) -> list[tuple[RiskLevel, str]]:
        """Risks that come from the line as a whole: a changed search path, nesting too deep to
        see into, or a `cd` into a system directory before a write, after which relative paths
        cannot be judged."""
        found = [(RiskLevel.DANGEROUS, f"sets {n}, which changes what later commands run")
                 for n in self.env_risks]
        if self.unwrap_limit_hit:
            found.append((RiskLevel.DANGEROUS, f"nests commands more than {self.max_depth} "
                          "levels deep, past what torra unwraps, so what runs is unseen"))
        entered = any(rc.program == "cd" and any(self._protected(o) for o in rc.argv[1:])
                      for rc in resolved)
        if entered and any(lv in (RiskLevel.CAUTION, RiskLevel.DANGEROUS) for lv in levels):
            found.append((RiskLevel.DANGEROUS, "changes into a system directory and then writes"))
        fed = re.findall(r"(?<![<\d&])<\s*(?![<(])([^\s;|&<>()]+)", self._mask_quoted(text))
        if any(reads_credentials(f) for f in fed):
            found.append((RiskLevel.DANGEROUS, "feeds a file that holds credentials to a command"))
        return found

    def explain(self, command: str) -> str:
        """Human readable trace of how a command was resolved and scored."""
        verdict = self.classify(command)
        lines = [f"command: {command}", f"verdict: {verdict.level.value}",
                 f"resolved into {len(verdict.resolved)} concrete command(s):"]
        for rc in verdict.resolved:
            origin = f"  <- {rc.unwrapped_from}" if rc.unwrapped_from else ""
            lines.append(f"  {' '.join(rc.argv)}{origin}")
        lines.append("reasons:")
        lines += [f"  {r}" for r in verdict.reasons]
        return "\n".join(lines)
