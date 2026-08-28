"""SAFETY_FIRST: resolves what a command will actually run, then classifies it.

Depends on: stdlib only.
"""

from __future__ import annotations

import base64
import re
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

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
        """Executable name with any directory prefix stripped."""
        return self.argv[0].rsplit("/", 1)[-1] if self.argv else ""

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
    (r"\brm\b", RiskLevel.DANGEROUS, "file deletion"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+--force|checkout\s+--\s)",
     RiskLevel.DANGEROUS, "unrecoverable git operation"),
    (r"\|\s*(sudo\s+)?(ba|z|k)?sh\b", RiskLevel.DANGEROUS, "stdin piped into a shell"),
    (r"\bsudo\b", RiskLevel.CAUTION, "privilege escalation"),
    (r"\bsystemctl\s+(start|restart|enable)\b", RiskLevel.CAUTION, "service state change"),
]


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
            elif ch in (";", "|", "&", "\n"):
                segments.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1

        segments.append("".join(buf))
        return [s.strip() for s in segments if s.strip()]

    def _tokenize(self, segment: str) -> list[str]:
        """Tokenize one segment, recording rather than swallowing failures."""
        try:
            return shlex.split(segment, comments=True)
        except ValueError as exc:
            self.parse_failures.append(f"{segment!r}: {exc}")
            return segment.split()

    @staticmethod
    def _consume_wrapper(out: list[str]) -> bool:
        """Pop a leading wrapper program with its flags and duration, if
        present. Mutates `out` in place; returns whether anything was consumed."""
        if not out or out[0].rsplit("/", 1)[-1] not in WRAPPERS:
            return False
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
        if out and re.fullmatch(r"\d+[smhd]?", out[0]):
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
                out.pop(0)
                changed = True
            if self._consume_wrapper(out):
                changed = True
        return out

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
            if command[i:i + 2] == "$(":
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
        # itself contains another `sh -c`. Hitting the limit is recorded as
        # a parse failure rather than silently truncated, so classify()'s
        # regex backstop is what carries the verdict past this point.
        if depth > self.max_depth:
            self.parse_failures.append(f"max unwrap depth hit on {command!r}")
            return []

        resolved: list[ResolvedCommand] = []
        decoded = self._decode_base64(command)
        if decoded:
            resolved += [ResolvedCommand(r.argv, r.source, "base64 payload")
                         for r in self.resolve(decoded, depth + 1)]
        resolved += self._resolve_substitutions(command, depth)

        env: dict[str, str] = {}
        for segment in self.split_segments(command):
            segment = self._expand_ifs(segment)
            argv = self._substitute(self._strip(self._tokenize(segment), env), env)
            if not argv:
                continue
            program = argv[0].rsplit("/", 1)[-1]
            if program in INTERPRETERS and "-c" in argv:
                try:
                    payload = argv[argv.index("-c") + 1]
                except IndexError:
                    payload = ""
                if payload:
                    # Only a *shell* interpreter's -c payload is worth
                    # recursing into as more shell syntax. Python/Perl/Ruby/
                    # Node source is not shell grammar -- resolving it here
                    # would misparse it -- so it is kept as one opaque
                    # ResolvedCommand and left to _judge's regex check for
                    # filesystem/process operations in the source text.
                    if program.startswith("python") or program in ("perl", "ruby", "node"):
                        resolved.append(ResolvedCommand(argv, segment,
                                                        f"{program} -c source"))
                        continue
                    resolved += [ResolvedCommand(r.argv, r.source, f"{program} -c")
                                 for r in self.resolve(payload, depth + 1)]
                    continue
            resolved.append(ResolvedCommand(argv, segment))
        return resolved

    # -- classification ---------------------------------------------------- #

    @staticmethod
    def _protected(path: str) -> bool:
        """True when a path targets a system location worth protecting."""
        cleaned = path.rstrip("/*") or "/"
        return cleaned in PROTECTED_PREFIXES or any(
            cleaned.startswith(p + "/") for p in PROTECTED_PREFIXES if p != "/")

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

        if program in PROGRAM_RISK:
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
            (program in ("python", "python3", "perl", "ruby", "node"),
             lambda: self._judge_script_source(program, argv)),
            (program == "kill", lambda: self._judge_kill(argv)),
            (program in ("chmod", "chown"),
             lambda: self._judge_chmod_chown(program, protected)),
            (program in ("tee", "cp", "mv"),
             lambda: self._judge_tee_cp_mv(program, protected)),
        ]
        for matched, call in rules:
            if matched:
                found += call()
        return found

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
        if any(a in ("install", "upgrade", "update", "full-upgrade") for a in argv[1:]):
            return [(RiskLevel.CAUTION, "package change")]
        return []

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
        interpreter payload overwriting a path it also read."""
        counts: dict[str, int] = {}
        for single, double in re.findall(r"'([^']*)'|\"([^\"]*)\"", source):
            literal = single or double
            if not literal or " " in literal:
                continue
            if "/" in literal or re.search(r"\.[A-Za-z0-9]{1,5}$", literal):
                counts[literal] = counts.get(literal, 0) + 1
        return next((literal for literal, n in counts.items() if n >= 2), None)

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
        skip_next, list_mode, pids = False, False, []
        for tok in argv[1:]:
            if skip_next:
                skip_next = False
            elif tok in ("-s", "-n", "--signal"):
                skip_next = True
            elif tok in ("-l", "-L", "--list", "--table"):
                list_mode = True
            elif not tok.startswith("-"):
                pids.append(tok)
        if "1" in pids and not list_mode:
            return [(RiskLevel.CRITICAL, "kill of PID 1")]
        return []

    @staticmethod
    def _judge_chmod_chown(program: str, protected: bool) -> list[tuple[RiskLevel, str]]:
        if protected:
            return [(RiskLevel.CRITICAL, f"{program} on a system path")]
        return []

    @staticmethod
    def _judge_tee_cp_mv(program: str, protected: bool) -> list[tuple[RiskLevel, str]]:
        if protected:
            return [(RiskLevel.DANGEROUS, f"{program} writes to a system path")]
        return []

    @staticmethod
    def _check_backstop(text: str) -> list[tuple[RiskLevel, str]]:
        """Regex backstop: catches whatever resolve() failed to parse."""
        return [(level, f"{why} [regex backstop]") for pattern, level, why in BACKSTOP
                if re.search(pattern, text, re.IGNORECASE)]

    def _check_redirect(self, text: str) -> list[tuple[RiskLevel, str]]:
        """Redirect-target risk: a concern neither _judge nor BACKSTOP looks
        at. Captures the operator (`>`/`>>`) alongside the target -- found
        in a full-file audit: classification was already correct for `>>`,
        but the reason text always said "overwrites" even for an append."""
        redirect = re.search(r"(>{1,2})\s*(/[^\s;|&]+)", text)
        if redirect and redirect.group(2) in SAFE_REDIRECT_TARGETS:
            return []  # a stream sink or documented no-op; nothing is overwritten
        if redirect and self._protected(redirect.group(2)):
            verb = "appends to" if redirect.group(1) == ">>" else "overwrites"
            return [(RiskLevel.CRITICAL, f"redirect {verb} {redirect.group(2)}")]
        if re.search(r">\s*[^>\s]", text):
            return [(RiskLevel.CAUTION, "output redirect that overwrites")]
        return []

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
        reasons: list[str] = []
        levels: list[RiskLevel] = []

        resolved = self.resolve(text)
        for rc in resolved:
            for level, why in self._judge(rc):
                origin = f" (via {rc.unwrapped_from})" if rc.unwrapped_from else ""
                reasons.append(f"{level.value.upper()}: {why}{origin} "
                               f"[argv: {' '.join(rc.argv[:6])}]")
                levels.append(level)

        for level, why in self._check_backstop(text) + self._check_redirect(text):
            levels.append(level)
            reasons.append(f"{level.value.upper()}: {why}")

        if self.parse_failures:
            reasons.append(f"NOTE: {len(self.parse_failures)} parse issue(s), "
                           "the regex backstop carried the classification")

        level = max(levels, key=ORDER.index) if levels else RiskLevel.SAFE
        if not reasons:
            reasons.append(f"no destructive pattern in "
                           f"{len(resolved)} resolved command(s)")
        return Verdict(text, level, reasons, resolved)

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
