"""SAFETY_FIRST: resolves what a command will actually run, then classifies it.

Depends on: stdlib only.
"""

from __future__ import annotations

import base64
import re
import shlex
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

INTERPRETERS: tuple[str, ...] = ("sh", "bash", "zsh", "ksh", "dash", "python",
                                 "python3", "perl", "ruby", "node")

WRAPPERS: tuple[str, ...] = ("sudo", "doas", "env", "nohup", "nice", "ionice",
                             "setsid", "time", "timeout", "stdbuf", "xargs")

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

    Regex over raw text caught five evasions out of twelve on the first pass in
    testing. This resolves first: quote-aware segment splitting, `VAR=value`
    substitution, wrapper unwrapping, recursion into `sh -c` payloads, base64
    decode of blobs piped to a shell, then classification on resolved argv. The
    regex table is kept as a backstop.

    With an agent generating commands this stops being a convenience and starts
    being the thing standing between a hallucinated `rm -rf /var` and the disk.
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
            if out and out[0].rsplit("/", 1)[-1] in WRAPPERS:
                out.pop(0)
                changed = True
                while out and (out[0] == "--" or out[0].startswith("-")):
                    if out.pop(0) == "--":
                        break
                if out and re.fullmatch(r"\d+[smhd]?", out[0]):
                    out.pop(0)
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

    def _decode_base64(self, command: str) -> str | None:
        """Decode a literal base64 blob piped into a shell."""
        if not (re.search(r"base64\s+(-d|--decode)", command)
                and re.search(r"\|\s*(sudo\s+)?(ba|z|k|da)?sh\b", command)):
            return None
        for blob in re.findall(r"['\"]([A-Za-z0-9+/=]{8,})['\"]", command):
            try:
                decoded = base64.b64decode(blob, validate=True).decode("utf-8", "replace")
                if decoded.strip():
                    return decoded
            except Exception as exc:
                self.parse_failures.append(f"base64 decode failed: {exc}")
        return None

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

        env: dict[str, str] = {}
        for segment in self.split_segments(command):
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

    def _judge(self, rc: ResolvedCommand) -> list[tuple[RiskLevel, str]]:
        """Risk findings for one resolved command."""
        # Deliberately flat: one `if program == ...` block per recognized
        # program, not a lookup table or a class per program. A table would
        # need its own escalation-by-target-path logic duplicated per entry
        # anyway (see the repeated `protected` checks below), and a class
        # hierarchy would turn a one-line rule into a subclass for a program
        # that will never again be extended. classify() below merges this
        # with the regex BACKSTOP and takes the max across both, so a program
        # missing here is caught by the backstop rather than passing silently.
        found: list[tuple[RiskLevel, str]] = []
        program, argv = rc.program, rc.argv
        flags = [a for a in argv[1:] if a.startswith("-")]
        operands = [a for a in argv[1:] if not a.startswith("-")]
        chars = set("".join(f.lstrip("-") for f in flags if not f.startswith("--")))
        protected = any(self._protected(o) for o in operands)

        if program in PROGRAM_RISK:
            level, why = PROGRAM_RISK[program]
            found.append((level, f"{why} via {program}"))

        if program == "rm":
            recursive = "r" in chars or "R" in chars or "--recursive" in flags
            forced = "f" in chars or "--force" in flags
            if recursive and forced:
                found.append((RiskLevel.CRITICAL, "recursive force delete"))
            elif recursive or protected:
                found.append((RiskLevel.CRITICAL if protected else RiskLevel.DANGEROUS,
                              "recursive or system-path delete"))
            else:
                found.append((RiskLevel.DANGEROUS, "file deletion"))

        if program == "dd" and any(a.startswith("of=/dev/") for a in argv):
            found.append((RiskLevel.CRITICAL, "raw write to a block device"))
        if program == "find" and ("-delete" in argv or "-exec" in argv):
            found.append((RiskLevel.CRITICAL if protected else RiskLevel.DANGEROUS,
                          "find with -delete or -exec"))
        if program == "systemctl":
            if any(a in ("stop", "disable", "mask") for a in argv[1:]):
                found.append((RiskLevel.DANGEROUS, "service interruption"))
            elif any(a in ("start", "restart", "enable", "daemon-reload") for a in argv[1:]):
                found.append((RiskLevel.CAUTION, "service state change"))
        if program == "git" and re.search(
                r"\breset\s+--hard\b|\bclean\s+-[a-z]*f|\bpush\s+--force\b"
                r"|\bcheckout\s+--\s|\bstash\s+(drop|clear)\b", " ".join(argv[1:])):
            found.append((RiskLevel.DANGEROUS, "unrecoverable git operation"))
        if program in ("apt", "apt-get", "dnf", "pacman", "snap"):
            if any(a in ("remove", "purge", "autoremove", "uninstall") for a in argv[1:]):
                found.append((RiskLevel.DANGEROUS, "package removal"))
            elif any(a in ("install", "upgrade", "update", "full-upgrade") for a in argv[1:]):
                found.append((RiskLevel.CAUTION, "package change"))
        if program in ("python", "python3", "perl", "ruby", "node") and re.search(
                r"rmtree|os\.remove|os\.unlink|\.unlink\(|subprocess|os\.system"
                r"|unlinkSync|rmSync", " ".join(argv)):
            found.append((RiskLevel.DANGEROUS,
                          f"{program} source performs filesystem or process operations"))
        if program == "kill":
            # `-9` alone used to be the only signal spec recognized here --
            # `kill -s KILL 1`, `kill -SIGKILL 1`, `kill -s 9 1`, and plain
            # `kill 1` (default SIGTERM) all classified SAFE. Found in a
            # full-file audit. Walking argv properly (rather than reusing
            # the generic `operands` split) matters here specifically:
            # `-s`/`-n`/`--signal` consume the *next* token as their
            # argument, not a PID -- `kill -s 1 12345` sends SIGHUP to PID
            # 12345 and never touches PID 1, and naively treating "1" as
            # an operand there would flag a command that is not dangerous.
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
                found.append((RiskLevel.CRITICAL, "kill of PID 1"))
        if program in ("chmod", "chown") and protected:
            found.append((RiskLevel.CRITICAL, f"{program} on a system path"))
        if program in ("tee", "cp", "mv") and protected:
            found.append((RiskLevel.DANGEROUS, f"{program} writes to a system path"))
        return found

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

        for pattern, level, why in BACKSTOP:
            if re.search(pattern, text, re.IGNORECASE):
                levels.append(level)
                reasons.append(f"{level.value.upper()}: {why} [regex backstop]")

        # Captures the operator itself (>` or `>>`) alongside the target --
        # found in a full-file audit: the classification here was already
        # correct for `>>` (a protected path is CRITICAL either way, and
        # this regex matches starting at the second `>` of `>>` regardless),
        # but the reason text always said "overwrites" even for an append,
        # which is simply the wrong word for what the command does.
        redirect = re.search(r"(>{1,2})\s*(/[^\s;|&]+)", text)
        if redirect and redirect.group(2) in SAFE_REDIRECT_TARGETS:
            pass  # a stream sink or documented no-op; nothing is overwritten
        elif redirect and self._protected(redirect.group(2)):
            verb = "appends to" if redirect.group(1) == ">>" else "overwrites"
            levels.append(RiskLevel.CRITICAL)
            reasons.append(f"CRITICAL: redirect {verb} {redirect.group(2)}")
        elif re.search(r">\s*[^>\s]", text):
            levels.append(RiskLevel.CAUTION)
            reasons.append("CAUTION: output redirect that overwrites")

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
