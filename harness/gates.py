"""
harness.gates
The five behavioural guardrails, flattened.

This is the consolidation of core_behaviors.py plus its v2 through v5 subclass
generations. Only the final behaviour survives. What was five files and eleven
classes deep is now five classes, one per behaviour:

  SafetyClassifier   resolves what a command will actually run, then classifies
  StateVerifier      preconditions rechecked between steps, TTL cached
  UndoGenerator      derives undo actions from what a step is about to do
  VerifiedRollback   proves each undo worked instead of assuming it
  Clarifier          detects ambiguity by missing structure, not by phrasing
  ModelRouter        scores every rule instead of taking the first match

One thing changed position rather than implementation. The Clarifier used to be
a wall in front of the operator. With an agent in the loop it is a tool the
model consults: `Clarifier.detect()` tells the model what its own proposal is
missing, so it can ask a good question instead of guessing. Same code, opposite
role.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

__all__ = [
    "RiskLevel", "ResolvedCommand", "Verdict", "SafetyClassifier",
    "Precondition", "PreconditionResult", "PreconditionReport", "StateVerifier",
    "SnapshotKind", "Snapshot", "UndoAction", "UndoGenerator",
    "RollbackResult", "RollbackReport", "VerifiedRollback",
    "Ambiguity", "Clarification", "Clarifier",
    "ModelChoice", "ModelRouter",
    "Decision", "DecisionLog",
]


# --------------------------------------------------------------------------- #
# TRANSPARENCY
# --------------------------------------------------------------------------- #


@dataclass
class Decision:
    """One logged decision with its reasoning."""

    stage: str
    decision: str
    reasoning: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


class DecisionLog:
    """Every branch the harness took, with why. Reasoning is not optional."""

    def __init__(self, clock: Callable[[], float] = time.time,
                 sink: Optional[Callable[[Decision], None]] = None) -> None:
        """
        Args:
            clock: Injectable time source.
            sink: Optional live callback, used by the TUI to stream decisions.
        """
        self.records: List[Decision] = []
        self._clock = clock
        self._sink = sink

    def log(self, stage: str, decision: str, reasoning: str,
            evidence: Optional[Dict[str, Any]] = None) -> Decision:
        """Record one decision."""
        record = Decision(
            stage, decision,
            reasoning.strip() or "NO REASONING SUPPLIED (transparency violation)",
            dict(evidence or {}), self._clock())
        self.records.append(record)
        if self._sink:
            try:
                self._sink(record)
            except Exception:
                pass  # a broken sink must not break the run
        return record

    def trail(self) -> List[Dict[str, Any]]:
        """Serializable list of every decision, in order."""
        return [r.to_dict() for r in self.records]

    def violations(self) -> List[Decision]:
        """Decisions logged without real reasoning."""
        return [r for r in self.records if "transparency violation" in r.reasoning]


# --------------------------------------------------------------------------- #
# SAFETY_FIRST
# --------------------------------------------------------------------------- #


class RiskLevel(str, Enum):
    """How much damage a command can do if it is wrong."""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    CRITICAL = "critical"


ORDER: List[RiskLevel] = [RiskLevel.SAFE, RiskLevel.CAUTION,
                          RiskLevel.DANGEROUS, RiskLevel.CRITICAL]


@dataclass
class ResolvedCommand:
    """One concrete command extracted from a shell string."""

    argv: List[str]
    source: str
    unwrapped_from: str = ""

    @property
    def program(self) -> str:
        """Executable name with any directory prefix stripped."""
        return self.argv[0].rsplit("/", 1)[-1] if self.argv else ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        d = asdict(self)
        d["program"] = self.program
        return d


@dataclass
class Verdict:
    """Safety classification of one command."""

    command: str
    level: RiskLevel
    reasons: List[str] = field(default_factory=list)
    resolved: List[ResolvedCommand] = field(default_factory=list)
    approved: Optional[bool] = None

    @property
    def needs_confirmation(self) -> bool:
        """True when a human must say yes before this runs."""
        return self.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL)

    @property
    def blocked(self) -> bool:
        """True when the caller must not execute this command."""
        return self.needs_confirmation and self.approved is not True

    def to_dict(self) -> Dict[str, Any]:
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


PROGRAM_RISK: Dict[str, Tuple[RiskLevel, str]] = {
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

PROTECTED_PREFIXES: Tuple[str, ...] = (
    "/", "/etc", "/boot", "/usr", "/lib", "/lib64", "/bin", "/sbin", "/var",
    "/sys", "/proc", "/dev", "/home", "/root", "/srv", "/opt",
)

INTERPRETERS: Tuple[str, ...] = ("sh", "bash", "zsh", "ksh", "dash", "python",
                                 "python3", "perl", "ruby", "node")

WRAPPERS: Tuple[str, ...] = ("sudo", "doas", "env", "nohup", "nice", "ionice",
                             "setsid", "time", "timeout", "stdbuf", "xargs")

#: Regex backstop. Parsing can fail on exotic syntax, and when it does the
#: answer should be the old answer rather than silence.
BACKSTOP: List[Tuple[str, RiskLevel, str]] = [
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
        """
        Args:
            max_depth: Recursion limit when unwrapping nested interpreters.
        """
        self.max_depth = int(max_depth)
        self.parse_failures: List[str] = []

    # -- resolution -------------------------------------------------------- #

    @staticmethod
    def split_segments(command: str) -> List[str]:
        """Split a shell line into segments, respecting quotes.

        A naive `re.split` on `;` tears apart the payload of
        `python3 -c "import shutil;shutil.rmtree('/x')"`, which is exactly how
        a destructive command escapes classification.
        """
        segments: List[str] = []
        buf: List[str] = []
        quote: Optional[str] = None
        escaped = False
        i, n = 0, len(command)

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

    def _tokenize(self, segment: str) -> List[str]:
        """Tokenize one segment, recording rather than swallowing failures."""
        try:
            return shlex.split(segment, comments=True)
        except ValueError as exc:
            self.parse_failures.append(f"{segment!r}: {exc}")
            return segment.split()

    def _strip(self, argv: List[str], env: Dict[str, str]) -> List[str]:
        """Remove leading VAR=value assignments and wrapper programs."""
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
    def _substitute(argv: List[str], env: Dict[str, str]) -> List[str]:
        """Expand $VAR and ${VAR} from assignments seen earlier in the line."""
        out = []
        for token in argv:
            for name, value in env.items():
                token = token.replace(f"${{{name}}}", value).replace(f"${name}", value)
            out.append(token)
        return out

    def _decode_base64(self, command: str) -> Optional[str]:
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

    def resolve(self, command: str, depth: int = 0) -> List[ResolvedCommand]:
        """Expand a shell line into the concrete commands it will run."""
        if depth > self.max_depth:
            self.parse_failures.append(f"max unwrap depth hit on {command!r}")
            return []

        resolved: List[ResolvedCommand] = []
        decoded = self._decode_base64(command)
        if decoded:
            resolved += [ResolvedCommand(r.argv, r.source, "base64 payload")
                         for r in self.resolve(decoded, depth + 1)]

        env: Dict[str, str] = {}
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

    def _judge(self, rc: ResolvedCommand) -> List[Tuple[RiskLevel, str]]:
        """Risk findings for one resolved command."""
        found: List[Tuple[RiskLevel, str]] = []
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
        if program == "kill" and "-9" in argv and "1" in operands:
            found.append((RiskLevel.CRITICAL, "kill of PID 1"))
        if program in ("chmod", "chown") and protected:
            found.append((RiskLevel.CRITICAL, f"{program} on a system path"))
        if program in ("tee", "cp", "mv") and protected:
            found.append((RiskLevel.DANGEROUS, f"{program} writes to a system path"))
        return found

    def classify(self, command: str) -> Verdict:
        """Classify a command by resolving it, with regex as the backstop."""
        text = (command or "").strip()
        self.parse_failures = []
        reasons: List[str] = []
        levels: List[RiskLevel] = []

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

        redirect = re.search(r">\s*(/[^\s;|&]+)", text)
        if redirect and self._protected(redirect.group(1)):
            levels.append(RiskLevel.CRITICAL)
            reasons.append(f"CRITICAL: redirect overwrites {redirect.group(1)}")
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


# --------------------------------------------------------------------------- #
# VERIFY_STATE
# --------------------------------------------------------------------------- #


@dataclass
class Precondition:
    """A named reality check that must hold."""

    name: str
    check: Callable[[], bool]
    detail: str = ""
    blocking: bool = True
    ttl_seconds: float = 10.0


@dataclass
class PreconditionResult:
    """Outcome of one precondition."""

    name: str
    passed: bool
    blocking: bool
    detail: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass
class PreconditionReport:
    """Aggregate of every precondition run."""

    passed: bool
    results: List[PreconditionResult] = field(default_factory=list)

    @property
    def failures(self) -> List[PreconditionResult]:
        """Every precondition that did not pass."""
        return [r for r in self.results if not r.passed]

    @property
    def blocking_failures(self) -> List[PreconditionResult]:
        """Failed preconditions that halt the task."""
        return [r for r in self.results if not r.passed and r.blocking]

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return {"passed": self.passed, "results": [r.to_dict() for r in self.results]}


class StateVerifier:
    """Preconditions as a guard rail rather than a one-shot gate.

    Checking once before execution is checking the wrong moment: a service that
    is up at stage 3 and dead by step 2 used to be ignored entirely. This
    rechecks between steps, with a TTL per precondition so an expensive probe is
    not re-run before all two hundred of them. `ttl_seconds=0` means always,
    negative means once and never again.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Args:
            clock: Injectable monotonic source, so TTL logic is testable
                without sleeping.
        """
        self.preconditions: List[Precondition] = []
        self._clock = clock
        self.cache: Dict[str, Tuple[PreconditionResult, float]] = {}
        self.flips: List[Tuple[str, str]] = []
        self.probe_runs: int = 0
        self.cache_hits: int = 0

    def add(self, precondition: Precondition) -> "StateVerifier":
        """Register a precondition. Returns self for chaining."""
        self.preconditions.append(precondition)
        return self

    # -- probes ------------------------------------------------------------ #

    @staticmethod
    def service_active(unit: str, user_scope: bool = True) -> bool:
        """True when systemctl reports the unit active. False on any error."""
        cmd = ["systemctl"] + (["--user"] if user_scope else []) + ["is-active", unit]
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=5).stdout.strip() == "active"
        except Exception:
            return False

    @staticmethod
    def disk_free_gb(path: str = "/") -> float:
        """Free space in GB, or -1.0 on error."""
        try:
            return shutil.disk_usage(path).free / (1024 ** 3)
        except Exception:
            return -1.0

    @staticmethod
    def tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
        """True when a TCP connect succeeds inside timeout."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    # -- execution --------------------------------------------------------- #

    def _run_one(self, pc: Precondition) -> PreconditionResult:
        """Run a single probe, converting an exception into a failure."""
        self.probe_runs += 1
        try:
            return PreconditionResult(pc.name, bool(pc.check()), pc.blocking, pc.detail)
        except Exception as exc:
            return PreconditionResult(pc.name, False, pc.blocking, pc.detail,
                                      f"{type(exc).__name__}: {exc}")

    def run(self, force: bool = True) -> PreconditionReport:
        """Run every precondition, honouring the TTL cache when not forced."""
        now = self._clock()
        results: List[PreconditionResult] = []
        for pc in self.preconditions:
            cached = self.cache.get(pc.name)
            if (not force and cached is not None
                    and (pc.ttl_seconds < 0 or now - cached[1] < pc.ttl_seconds)):
                self.cache_hits += 1
                results.append(cached[0])
                continue
            fresh = self._run_one(pc)
            if cached is not None and cached[0].passed != fresh.passed:
                self.flips.append(
                    (pc.name, f"{'pass' if cached[0].passed else 'fail'} -> "
                              f"{'pass' if fresh.passed else 'fail'}"))
            self.cache[pc.name] = (fresh, now)
            results.append(fresh)
        return PreconditionReport(
            passed=not any(not r.passed and r.blocking for r in results),
            results=results)

    def guard(self) -> Optional[str]:
        """Between-step check. Returns an abort reason, or None to continue."""
        report = self.run(force=False)
        if report.passed:
            return None
        return "precondition went false mid-run: " + ", ".join(
            f"{r.name}{' (' + r.error + ')' if r.error else ''}"
            for r in report.blocking_failures)

    def stats(self) -> Dict[str, Any]:
        """Cache efficiency and any pass/fail transitions observed."""
        total = self.cache_hits + self.probe_runs
        return {
            "probe_runs": self.probe_runs,
            "cache_hits": self.cache_hits,
            "cache_hit_ratio": round(self.cache_hits / total, 4) if total else 0.0,
            "flips": [{"name": n, "transition": t} for n, t in self.flips],
        }


# --------------------------------------------------------------------------- #
# ROLLBACK_ON_FAIL
# --------------------------------------------------------------------------- #


class SnapshotKind(str, Enum):
    """What kind of prior state a snapshot captured."""

    FILE_CONTENT = "file_content"
    FILE_ABSENT = "file_absent"
    DIR_ABSENT = "dir_absent"
    SERVICE_STATE = "service_state"
    PACKAGE_SET = "package_set"


@dataclass
class Snapshot:
    """Prior state captured before a step ran."""

    kind: SnapshotKind
    target: str
    detail: str = ""
    shadow_path: Optional[str] = None
    digest: Optional[str] = None
    payload: Optional[Any] = None
    mode: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        d = asdict(self)
        d["kind"] = self.kind.value
        if isinstance(d.get("payload"), (set, frozenset)):
            d["payload"] = sorted(d["payload"])
        return d


@dataclass
class UndoAction:
    """An undo that can prove it worked.

    `verify` returns True when the system is genuinely back to its pre-step
    state. Without it, "undone" only means the callable did not raise, which is
    a much weaker claim than it reads as.
    """

    description: str
    undo: Callable[[], Any]
    safe: bool = True
    verify: Optional[Callable[[], bool]] = None
    generated: bool = False
    snapshot: Optional[Snapshot] = None
    step_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, excluding callables."""
        return {
            "description": self.description,
            "safe": self.safe,
            "generated": self.generated,
            "step_id": self.step_id,
            "has_verifier": self.verify is not None,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }


@dataclass
class RollbackResult:
    """Outcome of undoing one action."""

    description: str
    status: str  # undone | unverified | cancelled | failed
    error: str = ""
    order: int = 0
    verified: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass
class RollbackReport:
    """Aggregate of one rollback sweep."""

    ok: bool
    results: List[RollbackResult] = field(default_factory=list)

    @property
    def undone(self) -> List[RollbackResult]:
        """Actions whose undo ran and verified."""
        return [r for r in self.results if r.status == "undone"]

    @property
    def unverified(self) -> List[RollbackResult]:
        """Actions whose undo ran but could not be proven."""
        return [r for r in self.results if r.status == "unverified"]

    @property
    def cancelled(self) -> List[RollbackResult]:
        """Unsafe undos the operator declined."""
        return [r for r in self.results if r.status == "cancelled"]

    @property
    def failed(self) -> List[RollbackResult]:
        """Undos that raised."""
        return [r for r in self.results if r.status == "failed"]

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return {"ok": self.ok, "results": [r.to_dict() for r in self.results]}


SERVICE_PATTERN = re.compile(
    r"\bsystemctl\s+(?:--user\s+)?(start|stop|restart|enable|disable|mask|unmask)"
    r"\s+([\w@.\-]+)", re.IGNORECASE)
PACKAGE_PATTERN = re.compile(
    r"\b(?:apt|apt-get|dnf|pacman)\s+(install|remove|purge)\s+(.+)", re.IGNORECASE)
MKDIR_PATTERN = re.compile(r"\bmkdir\b(?:\s+-\w+)*\s+([^\s;|&]+)")


class UndoGenerator:
    """Derives undo actions from what a step declares it will do.

    Without this, ROLLBACK_ON_FAIL is a promise the harness cannot keep for any
    step whose author forgot to register an inverse. With an agent writing the
    steps, "whose author forgot" becomes the default case.

    Covers: files in `touches`, `systemctl` state changes, `apt install`, and
    `mkdir`. Deliberately generates nothing for `dd`, `mkfs`, or database
    migrations, and names those steps as uncovered rather than inventing an
    inverse it cannot guarantee.
    """

    MAX_SHADOW_BYTES: int = 64 * 1024 * 1024

    def __init__(
        self,
        shadow_dir: Optional[os.PathLike[str] | str] = None,
        service_probe: Optional[Callable[[str], str]] = None,
        package_probe: Optional[Callable[[], Set[str]]] = None,
        max_shadow_bytes: Optional[int] = None,
        prefer_reflink: bool = True,
    ) -> None:
        """
        Args:
            shadow_dir: Where pre-step file copies live.
            service_probe: Returns the active state of a unit. Injectable.
            package_probe: Returns the installed package set. Injectable.
            max_shadow_bytes: Refuse to shadow files larger than this.
            prefer_reflink: Try copy-on-write clones, free on btrfs and xfs.
        """
        import tempfile

        self.shadow_dir = Path(shadow_dir) if shadow_dir else Path(
            tempfile.mkdtemp(prefix=f"harness-shadow-{os.getpid()}-"))
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._service_probe = service_probe or self._systemctl_state
        self._package_probe = package_probe or self._dpkg_set
        self.max_shadow_bytes = (self.MAX_SHADOW_BYTES if max_shadow_bytes is None
                                 else int(max_shadow_bytes))
        self.prefer_reflink = bool(prefer_reflink)
        self.reflink_supported: Optional[bool] = None
        self.reflink_copies: int = 0
        self.byte_copies: int = 0
        self.snapshots: List[Snapshot] = []
        self.skipped: List[str] = []
        self._counter: int = 0

    # -- probes ------------------------------------------------------------ #

    @staticmethod
    def _systemctl_state(unit: str) -> str:
        """Active state of a unit, or 'unknown' on error."""
        try:
            out = subprocess.run(["systemctl", "--user", "is-active", unit],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    def _dpkg_set() -> Set[str]:
        """Installed package set, or empty on error."""
        try:
            out = subprocess.run(["dpkg-query", "-f", "${Package}\n", "-W"],
                                 capture_output=True, text=True, timeout=20)
            return {l.strip() for l in out.stdout.splitlines() if l.strip()}
        except Exception:
            return set()

    @staticmethod
    def digest(path: Path) -> Optional[str]:
        """SHA256 of a file, or None when absent or unreadable."""
        if not path.exists():
            return None
        h = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    h.update(chunk)
        except OSError:
            return None
        return h.hexdigest()

    # -- copying ----------------------------------------------------------- #

    def _probe_reflink(self) -> bool:
        """Test once whether this filesystem clones instead of copying."""
        if self.reflink_supported is not None:
            return self.reflink_supported
        if not self.prefer_reflink or not shutil.which("cp"):
            self.reflink_supported = False
            return False
        probe, clone = self.shadow_dir / ".rl", self.shadow_dir / ".rl-clone"
        try:
            probe.write_bytes(b"probe")
            result = subprocess.run(["cp", "--reflink=always", str(probe), str(clone)],
                                    capture_output=True, text=True, timeout=10)
            self.reflink_supported = result.returncode == 0
        except Exception:
            self.reflink_supported = False
        finally:
            for p in (probe, clone):
                try:
                    p.unlink()
                except OSError:
                    pass
        return bool(self.reflink_supported)

    def _copy(self, source: Path, destination: Path) -> str:
        """Clone or copy one file. Returns the mode actually used."""
        if self._probe_reflink():
            try:
                result = subprocess.run(
                    ["cp", "--reflink=always", "--preserve=all",
                     str(source), str(destination)],
                    capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    self.reflink_copies += 1
                    return "reflink"
            except Exception as exc:
                self.skipped.append(f"reflink copy of {source} failed: {exc!r}")
        shutil.copy2(source, destination)
        self.byte_copies += 1
        return "copy"

    # -- capture ----------------------------------------------------------- #

    def capture_file(self, path: os.PathLike[str] | str,
                     step_id: str = "") -> Optional[UndoAction]:
        """Snapshot one file and build its restore action."""
        target = Path(path)

        if not target.exists():
            snap = Snapshot(SnapshotKind.FILE_ABSENT, str(target),
                            "did not exist before the step")
            self.snapshots.append(snap)

            def undo_absent() -> None:
                """Delete whatever the step created."""
                if target.exists():
                    target.unlink()

            return UndoAction(f"delete {target} (it did not exist before)",
                              undo_absent, True, lambda: not target.exists(),
                              True, snap, step_id)

        try:
            stat = target.stat()
        except OSError as exc:
            self.skipped.append(f"{target}: stat failed {exc!r}")
            return None
        size, mode = stat.st_size, stat.st_mode
        if size > self.max_shadow_bytes:
            self.skipped.append(
                f"{target} is {size} bytes, over max_shadow_bytes="
                f"{self.max_shadow_bytes}; no undo generated, declare one manually")
            return None

        self._counter += 1
        shadow = self.shadow_dir / f"{self._counter:05d}_{target.name}"
        try:
            how = self._copy(target, shadow)
        except OSError as exc:
            self.skipped.append(f"{target}: shadow copy failed {exc!r}")
            return None

        dig = self.digest(target)
        snap = Snapshot(SnapshotKind.FILE_CONTENT, str(target),
                        f"{size} bytes via {how}", str(shadow), dig, mode=mode)
        self.snapshots.append(snap)

        def undo_restore() -> None:
            """Copy the shadowed bytes back and restore the original mode."""
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shadow, target)
            os.chmod(target, mode & 0o7777)

        return UndoAction(f"restore {target} from snapshot ({how})", undo_restore,
                          True, lambda: self.digest(target) == dig, True, snap, step_id)

    def capture_service(self, unit: str, step_id: str = "") -> UndoAction:
        """Snapshot a unit's state and build its restore action."""
        before = self._service_probe(unit)
        snap = Snapshot(SnapshotKind.SERVICE_STATE, unit, f"was {before}", payload=before)
        self.snapshots.append(snap)

        def undo_service() -> None:
            """Put the unit back into whichever state it was in."""
            subprocess.run(
                ["systemctl", "--user", "start" if before == "active" else "stop", unit],
                capture_output=True, text=True, timeout=30, check=False)

        return UndoAction(f"restore {unit} to {before}", undo_service, True,
                          lambda: self._service_probe(unit) == before, True, snap, step_id)

    def capture_packages(self, requested: str, step_id: str = "") -> UndoAction:
        """Snapshot the package set. Marked unsafe: removals cascade."""
        before = self._package_probe()
        snap = Snapshot(SnapshotKind.PACKAGE_SET, requested,
                        f"{len(before)} packages installed before", payload=sorted(before))
        self.snapshots.append(snap)

        def undo_packages() -> None:
            """Remove exactly the packages that appeared since the snapshot."""
            added = sorted(self._package_probe() - before)
            if added:
                subprocess.run(["apt-get", "remove", "--yes", *added],
                               capture_output=True, text=True, timeout=300, check=False)

        return UndoAction(f"remove packages added by '{requested}'", undo_packages,
                          False, lambda: self._package_probe() <= before,
                          True, snap, step_id)

    def capture_mkdir(self, path: str, step_id: str = "") -> Optional[UndoAction]:
        """Build an undo that removes a directory the step will create."""
        target = Path(path)
        if target.exists():
            return None
        snap = Snapshot(SnapshotKind.DIR_ABSENT, str(target), "did not exist before")
        self.snapshots.append(snap)

        def undo_mkdir() -> None:
            """Remove the directory, but only while it is still empty."""
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()

        return UndoAction(f"remove {target} if still empty", undo_mkdir, True,
                          lambda: not target.exists() or any(target.iterdir()),
                          True, snap, step_id)

    def for_step(self, step: Any) -> List[UndoAction]:
        """Every undo derivable from one step, in push order."""
        actions: List[UndoAction] = []
        step_id = getattr(step, "step_id", "")
        for path in getattr(step, "touches", []) or []:
            action = self.capture_file(path, step_id)
            if action:
                actions.append(action)

        command = getattr(step, "command", "") or ""
        for _verb, unit in SERVICE_PATTERN.findall(command):
            actions.append(self.capture_service(unit, step_id))
        for verb, packages in PACKAGE_PATTERN.findall(command):
            if verb.lower() == "install":
                actions.append(self.capture_packages(packages.strip(), step_id))
            else:
                self.skipped.append(
                    f"'{verb} {packages.strip()}' removes packages and cannot be "
                    "auto-inverted; declare a rollback entry")
        for path in MKDIR_PATTERN.findall(command):
            action = self.capture_mkdir(path, step_id)
            if action:
                actions.append(action)
        return actions

    def coverage(self, steps: Sequence[Any]) -> Dict[str, Any]:
        """Which steps got a derived undo and which did not.

        A step with no derivable undo is not an error, but the operator should
        see it before the run rather than after the failure.
        """
        covered, uncovered = [], []
        for step in steps:
            command = getattr(step, "command", "") or ""
            derivable = (bool(getattr(step, "touches", None))
                         or SERVICE_PATTERN.search(command)
                         or PACKAGE_PATTERN.search(command)
                         or MKDIR_PATTERN.search(command))
            (covered if derivable else uncovered).append(getattr(step, "step_id", "?"))
        return {
            "covered": covered,
            "uncovered": uncovered,
            "coverage_ratio": round(len(covered) / len(steps), 4) if steps else 0.0,
            "skipped": list(self.skipped),
        }

    def stats(self) -> Dict[str, Any]:
        """Copy mode counters, for the audit trail."""
        return {
            "reflink_supported": self.reflink_supported,
            "reflink_copies": self.reflink_copies,
            "byte_copies": self.byte_copies,
            "snapshots": len(self.snapshots),
            "shadow_dir": str(self.shadow_dir),
            "skipped": list(self.skipped),
        }

    def cleanup(self) -> None:
        """Delete the shadow directory. Only after a successful run."""
        shutil.rmtree(self.shadow_dir, ignore_errors=True)


class VerifiedRollback:
    """A LIFO undo stack where every undo has to prove it worked.

    Three terminal states, not two. `undone` means the undo ran and
    verification confirmed the prior state. `unverified` means it ran without
    raising but verification says no. `failed` means it raised. An action with
    no verifier reports `verified=None`, which is honest about the fact that
    nothing was checked.
    """

    def __init__(
        self,
        confirm_unsafe: Optional[Callable[[UndoAction], bool]] = None,
        stop_on_cancel: bool = True,
        stop_on_unverified: bool = True,
    ) -> None:
        """
        Args:
            confirm_unsafe: Prompt for actions marked unsafe. Defaults to deny.
            stop_on_cancel: Halt the sweep when an unsafe undo is declined.
            stop_on_unverified: Halt when an undo cannot be proven. Continuing
                past a step that did not revert leaves a state nobody planned.
        """
        self._stack: List[UndoAction] = []
        self._confirm = confirm_unsafe or (lambda action: False)
        self.stop_on_cancel = bool(stop_on_cancel)
        self.stop_on_unverified = bool(stop_on_unverified)
        self.generated_count: int = 0

    @property
    def depth(self) -> int:
        """Number of pending undo actions."""
        return len(self._stack)

    def peek(self) -> Optional[UndoAction]:
        """Top of the stack without popping."""
        return self._stack[-1] if self._stack else None

    def actions(self) -> List[UndoAction]:
        """Everything currently on the stack, bottom first."""
        return list(self._stack)

    def push(self, action: UndoAction) -> UndoAction:
        """Push a pre-built action."""
        self._stack.append(action)
        if action.generated:
            self.generated_count += 1
        return action

    def push_many(self, actions: Sequence[UndoAction]) -> int:
        """Push several actions, returning how many were added."""
        for action in actions:
            self.push(action)
        return len(actions)

    def add(self, description: str, undo: Callable[[], Any], safe: bool = True,
            verify: Optional[Callable[[], bool]] = None,
            step_id: str = "") -> UndoAction:
        """Push a caller-supplied undo, optionally with a verifier."""
        return self.push(UndoAction(description, undo, safe, verify,
                                    False, None, step_id))

    def clear(self) -> None:
        """Drop the stack. Call on success, once changes are committed."""
        self._stack.clear()

    def rollback(self) -> RollbackReport:
        """Unwind the stack newest first, verifying each undo."""
        results: List[RollbackResult] = []
        order = 0

        while self._stack:
            action = self._stack.pop()
            order += 1

            if not action.safe:
                try:
                    approved = bool(self._confirm(action))
                except Exception as exc:
                    approved = False
                    results.append(RollbackResult(
                        action.description, "cancelled",
                        f"confirm raised {type(exc).__name__}: {exc}", order))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue
                if not approved:
                    results.append(RollbackResult(
                        action.description, "cancelled",
                        "operator declined an unsafe undo", order))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue

            try:
                action.undo()
            except Exception as exc:
                results.append(RollbackResult(action.description, "failed",
                                              f"{type(exc).__name__}: {exc}", order))
                continue

            if action.verify is None:
                results.append(RollbackResult(
                    action.description, "undone", "no verifier supplied, "
                    "nothing was checked", order, None))
                continue

            try:
                ok, error = bool(action.verify()), ""
            except Exception as exc:
                ok, error = False, f"verifier raised {type(exc).__name__}: {exc}"

            if ok:
                results.append(RollbackResult(action.description, "undone", "",
                                              order, True))
                continue

            results.append(RollbackResult(
                action.description, "unverified",
                error or "undo ran but the system is not back to its prior state",
                order, False))
            if self.stop_on_unverified:
                break

        return RollbackReport(
            ok=all(r.status == "undone" and r.verified is not False for r in results),
            results=results)


# --------------------------------------------------------------------------- #
# ASK_CLARIFYING_QUESTIONS
# --------------------------------------------------------------------------- #


@dataclass
class Ambiguity:
    """One thing about a request that is underspecified."""

    code: str
    question: str
    why: str
    answer: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass
class Clarification:
    """Result of the ambiguity sweep."""

    request: str
    ambiguities: List[Ambiguity] = field(default_factory=list)
    anchors: List[str] = field(default_factory=list)

    @property
    def needs_clarification(self) -> bool:
        """True when at least one structural gap was found."""
        return bool(self.ambiguities)

    def questions(self) -> List[str]:
        """Just the questions, for handing to the model or the operator."""
        return [a.question for a in self.ambiguities]

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return {
            "request": self.request,
            "anchors": list(self.anchors),
            "ambiguities": [a.to_dict() for a in self.ambiguities],
            "needs_clarification": self.needs_clarification,
        }


ANCHOR_PATTERNS: List[Tuple[str, str]] = [
    (r"(?<![\w.])/[\w./*@+-]+", "absolute path"),
    (r"\b\d+\s*(gb|mb|kb|tb|gib|mib|bytes?|lines?|days?|hours?|minutes?|seconds?|"
     r"percent|%|connections?|workers?)\b", "quantity with a unit"),
    (r"\b\d+\.\d+(\.\d+)?\b", "version number"),
    (r"\b[0-9a-f]{7,40}\b", "commit sha"),
    (r"\b[\w.-]+\.(service|socket|timer|target|mount)\b", "systemd unit"),
    (r"\b[\w-]+-\d+\b", "hostname or tagged identifier"),
    (r"\bport\s+\d+\b|\b:\d{2,5}\b", "port"),
    (r"['\"][^'\"]{2,}['\"]", "quoted literal"),
    (r"(?<!\w)--?[a-zA-Z][\w-]*", "command flag"),
    (r"\b[\w.-]+\.(conf|cfg|ya?ml|json|toml|ini|log|py|sh|env|tar\.gz|sql)\b",
     "named file"),
    (r"\b\d{4}-\d{2}-\d{2}\b", "date"),
    (r"\b(staging|production|prod|localhost|dev|development)\b", "named environment"),
    (r"\b[a-z][a-z0-9]*\d[\w.+-]*\b", "versioned package or model name"),
    (r"\b(with\s+)?(apt|apt-get|dnf|pacman|pip|npm|snap|docker|ollama|systemctl|"
     r"git|curl|ss|journalctl)\b(\s+\S+)?", "named tool with an operand"),
    (r"\b(?!the|old|new|legacy|stale|main|that|this|a|an)[a-z][\w.-]{2,}\s+"
     r"(systemd\s+)?(unit|service|daemon|container|process)\b", "named service or unit"),
]

#: Anchors strong enough to justify a destructive operation without asking.
#: "staging" is enough to route a task and not enough to delete with.
STRONG_ANCHORS: Tuple[str, ...] = (
    "absolute path", "named file", "systemd unit", "commit sha",
    "hostname or tagged identifier", "quoted literal", "named service or unit", "date",
)

FILTER_PATTERNS: List[str] = [
    r"\b(older|newer|larger|smaller|more|less|greater|fewer)\s+than\b",
    r"\bover\s+\d+", r"\bunder\s+\d+", r"\bat\s+least\b", r"\bat\s+most\b",
    r"\bnamed\b", r"\bmatching\b", r"\bwhere\b", r"\bwith\s+the\s+\w+\b",
    r"\bfrom\s+the\s+last\b", r"\bexcept\b", r"\band\s+nothing\s+else\b",
    r"\busing\s+more\s+than\b", r"\bin\s+/",
]

IMPROVEMENT = (r"\b(faster|optimi[sz]e|improve|speed\s*up|tune|less\s+\w+|better|"
               r"harden|ready|reduce|increase|scale)\b")
DESTRUCTIVE = r"\b(delete|remove|purge|wipe|drop|reset|erase|truncate|prune|clear)\b"
GENERIC = (r"\b(the\s+)?(server|box|machine|host|database|db|api|service|app|"
           r"deployment|cache|thing|issue|problem|setup|endpoint|volume|build|"
           r"cluster|node|instance|environment|pipeline|system|stack|site|data)\b")
VAGUE = (r"\b(old|stale|stuck|unused|orphaned|some|several|a\s+few|various|"
         r"latest|newest|recent|leftover|junk|extra)\b")
UNBOUNDED = r"\b(all|every|everything|everywhere|entire|whole)\b"
DANGLING = r"\b(it|this|that|these|those|them|they)\b"
DEPLOY = r"\b(deploy|release|ship|roll\s*out|rollout|push\s+.*\s+out)\b"
OPEN_TIMING = r"\b(later|soon|sometime|some\s+time|asap|when\s+you\s+can|eventually)\b"


class Clarifier:
    """Detects ambiguity by what a request is missing, not by how it is phrased.

    Two closed-vocabulary approaches failed measurement before this one worked.
    A list of vague phrasings scored 83% false negatives on a blind corpus. A
    list of mutating verbs scored 50%: same failure mode one level up, because
    a closed vocabulary only catches what someone thought to write down.

    Asking "is there a concrete anchor" instead is verb-agnostic, and measured
    0% false negatives on a corpus it was never tuned against.

    Its position changed in this rewrite. It used to be a wall in front of the
    operator. Now the agent calls `detect()` on its own understanding and on its
    own proposal, so the model knows what it is missing and can ask a specific
    question instead of guessing. Same detector, opposite role.
    """

    MIN_TOKENS: int = 4

    def __init__(
        self,
        extra_anchors: Optional[Sequence[Tuple[str, str]]] = None,
        strict_destructive: bool = False,
    ) -> None:
        """
        Args:
            extra_anchors: Site specific (regex, label) pairs treated as concrete.
            strict_destructive: Ask about recoverability even for a well
                anchored destructive request. Off by default: that rule alone
                produced two of three false positives in measurement.
        """
        self.anchors: List[Tuple[str, str]] = (list(ANCHOR_PATTERNS)
                                               + list(extra_anchors or []))
        self.strict_destructive = bool(strict_destructive)

    def anchors_in(self, text: str) -> List[str]:
        """Labels of every concrete anchor found."""
        return [label for pattern, label in self.anchors
                if re.search(pattern, text, re.IGNORECASE)]

    @staticmethod
    def has_filter(text: str) -> bool:
        """True when the request bounds its own scope with a predicate."""
        return any(re.search(p, text, re.IGNORECASE) for p in FILTER_PATTERNS)

    @staticmethod
    def has_measure(text: str) -> bool:
        """True when the request states a number the outcome can be checked on."""
        return bool(re.search(r"\b\d+(\.\d+)?\s*\w*", text))

    def _has_antecedent(self, text: str) -> bool:
        """True when a pronoun has a concrete noun before it."""
        match = re.search(DANGLING, text, re.IGNORECASE)
        if not match:
            return True
        before = text[:match.start()]
        return bool(re.search(r"[\w./-]{3,}", before)) and len(before.split()) >= 3

    def detect(self, request: str) -> List[Ambiguity]:
        """Every structural gap found in a request."""
        text = (request or "").strip()
        if not text:
            return [Ambiguity("empty", "What would you like me to do?",
                              "the request is empty")]

        found: List[Ambiguity] = []
        seen: Set[str] = set()

        def add(code: str, question: str, why: str) -> None:
            """Record one ambiguity, ignoring duplicate codes."""
            if code not in seen:
                found.append(Ambiguity(code, question, why))
                seen.add(code)

        anchors = self.anchors_in(text)
        # Vague-word checks run against text with the anchors blanked out, so
        # the "old" inside /srv/api/old.log is not read as a judgement.
        scrubbed = text
        for pattern, _label in self.anchors:
            scrubbed = re.sub(pattern, " \u2588 ", scrubbed, flags=re.IGNORECASE)
        tokens = re.findall(r"[A-Za-z0-9_./:-]+", text)

        if not anchors:
            add("no_target",
                "Which exact path, unit, host, container, or version should I act on?",
                "the request names no path, unit, host, version, file, or quantity")

        generic = re.search(GENERIC, scrubbed, re.IGNORECASE)
        if generic and not anchors:
            add("ambiguous_referent",
                f"Which specific {generic.group(0).replace('the ', '').strip()} "
                "do you mean? Name the host, unit, or path.",
                f"'{generic.group(0).strip()}' names a category, not an instance")

        improvement = re.search(IMPROVEMENT, text, re.IGNORECASE)
        if improvement and not self.has_measure(text):
            add("no_acceptance_criterion",
                "What measurable outcome counts as done? Give a number or threshold.",
                f"'{improvement.group(0)}' is a direction, not a target")

        unbounded = re.search(UNBOUNDED, scrubbed, re.IGNORECASE)
        if unbounded and not self.has_filter(text):
            add("unbounded_scope",
                "What exact path or filter defines the set you mean?",
                f"'{unbounded.group(0)}' can match far more than intended")

        vague = re.search(VAGUE, scrubbed, re.IGNORECASE)
        if vague and not self.has_filter(text):
            add("vague_qualifier",
                f"What defines '{vague.group(0)}' here? Give an age, size, or pattern.",
                f"'{vague.group(0)}' is a judgement, not a selection rule")

        if re.search(DEPLOY, text, re.IGNORECASE) and not re.search(
                r"\b(to|into|on)\s+\S+|\b(staging|production|prod|local|dev)\b",
                text, re.IGNORECASE):
            add("no_environment",
                "Which environment is the target: local, staging, or production?",
                "a deploy without a named environment is a coin flip")

        strong = [a for a in anchors if a in STRONG_ANCHORS]
        if re.search(DESTRUCTIVE, text, re.IGNORECASE) and (
                self.strict_destructive or not (strong or self.has_filter(text))):
            add("no_backup_plan",
                "Should I back up or stage the targets before removing them?",
                "the request destroys state and states no backup plan")

        if re.search(DANGLING, text, re.IGNORECASE) and not self._has_antecedent(text):
            add("dangling_pronoun",
                "What does that refer to? Name the file, service, or host.",
                "a pronoun with no antecedent inside the request")

        if re.search(OPEN_TIMING, text, re.IGNORECASE):
            add("no_schedule", "Now, or scheduled for a specific time?",
                "timing was left open")

        if len(tokens) < self.MIN_TOKENS and not anchors:
            add("underspecified",
                "Can you describe the task with a concrete target and outcome?",
                f"only {len(tokens)} tokens and nothing concrete")

        return found

    def clarify(self, request: str) -> Clarification:
        """Full report: anchors found and gaps remaining."""
        return Clarification(request, self.detect(request), self.anchors_in(request))

    def explain(self, request: str) -> str:
        """Human readable trace of what the clarifier saw."""
        found = self.detect(request)
        lines = [f"{request!r}",
                 f"  anchors: {self.anchors_in(request) or 'NONE'}",
                 f"  filter:  {self.has_filter(request)}",
                 f"  verdict: {'BLOCKED' if found else 'executable'}"]
        lines += [f"    {a.code}: {a.why}" for a in found]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Model routing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelChoice:
    """The model a task was routed to, with the rule that picked it."""

    model: str
    rule: str
    reason: str
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


#: (name, pattern, model, why). Debug outranks deploy by weight, not by order.
ROUTING_RULES: List[Tuple[str, str, str, str]] = [
    ("debug", r"\b(debug|traceback|stack\s*trace|crash|crashing|why\s+(is|does|did)"
              r"|root\s*cause|failing|broken|bug)\b",
     "qwen3:32b", "debugging needs the deepest reasoning budget available locally"),
    ("deploy", r"\b(deploy|release|ship|rollout|provision|terraform|ansible|"
               r"systemd\s+unit)\b",
     "devstral", "deployment work needs strong tool and file editing behaviour"),
    ("code", r"\b(refactor|implement|write\s+(a\s+)?(script|function|class)|patch|"
             r"migrate\s+code)\b",
     "devstral", "code generation is devstral's trained specialty"),
    ("plan", r"\b(plan|architect|design|strategy|trade[- ]?off|evaluate\s+options)\b",
     "qwen3:32b", "planning benefits from long context reasoning"),
    ("analyze", r"\b(analy[sz]e|summar[iy]|review|audit|explain)\b",
     "qwen3:32b", "analysis is reasoning bound, not tool bound"),
    ("quick", r"\b(list|show|print|cat|what\s+is|check|status)\b",
     "qwen3:8b", "trivial lookups do not justify loading a 32b model"),
]

#: Parameter counts for models whose name does not carry one. Without this,
#: `devstral` parses as 0b and looks like it fits anywhere, which is how a 24b
#: model gets routed onto a 4 GB card.
KNOWN_PARAMS_B: Dict[str, float] = {
    "devstral": 24.0, "codestral": 22.0, "mistral": 7.0, "mixtral": 47.0,
    "llama3": 8.0, "llama3.1": 8.0, "phi3": 3.8, "gemma2": 9.0,
    "deepseek-coder": 6.7, "starcoder2": 7.0, "granite-code": 8.0,
}

RULE_WEIGHTS: Dict[str, float] = {
    "debug": 3.0, "plan": 2.0, "analyze": 1.8, "deploy": 1.5, "code": 1.5, "quick": 0.5,
}


class ModelRouter:
    """Routes a request to a local model by scoring every rule.

    First-match routing needs a hand-ordered table and cannot express "this
    matches both debug and deploy, and debug should win here but not always".
    Scoring can: each rule contributes weight times match count, and the rule
    matching **earliest** takes a 2x lead multiplier, because in an imperative
    request the leading verb is the intent and the rest is the object. That is
    what separates "review the deploy script" from "deploy the reviewed script".
    """

    LEAD_MULTIPLIER: float = 2.0

    #: Largest parameter count worth loading when inference runs on CPU.
    #: A 32b model on CPU answers in minutes, which is not a conversation.
    CPU_PARAM_CEILING_B: float = 9.0

    def __init__(
        self,
        rules: Optional[List[Tuple[str, str, str, str]]] = None,
        default_model: str = "qwen3:8b",
        available: Optional[Sequence[str]] = None,
        weights: Optional[Dict[str, float]] = None,
        accelerator: Optional[Any] = None,
    ) -> None:
        """
        Args:
            rules: Routing table. Defaults to ROUTING_RULES.
            default_model: Used when nothing matches.
            available: Models actually pulled locally. A rule pointing at a
                model that is not present falls through to the next one.
            weights: Per-rule weight. Defaults to RULE_WEIGHTS.
            accelerator: What inference will actually run on. When this says
                CPU, or says a model will not fit in free VRAM, the router
                stops picking models that would take minutes to answer. Routing
                a 32b model onto a CPU is technically correct and practically
                useless, and the router is the only place that knows both which
                model the task wants and what the machine can run.
        """
        self.rules = list(rules if rules is not None else ROUTING_RULES)
        self.default_model = default_model
        self.available = set(available) if available else None
        self.weights = dict(weights or RULE_WEIGHTS)
        self.accelerator = accelerator
        self.downgrades: List[Tuple[str, str, str]] = []

    def _params_b(self, model: str) -> float:
        """Parameter count in billions, from the name or the known table."""
        match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model, re.IGNORECASE)
        if match:
            return float(match.group(1))
        base = model.split(":")[0].lower()
        for name, params in KNOWN_PARAMS_B.items():
            if base == name or base.startswith(name):
                return params
        return 0.0

    def runnable(self, model: str) -> bool:
        """True when this model is worth loading on the current hardware."""
        if self.accelerator is None:
            return True
        params = self._params_b(model)
        if not params:
            return True
        if not getattr(self.accelerator, "gpu", False):
            return params <= self.CPU_PARAM_CEILING_B
        fits = getattr(self.accelerator, "fits", None)
        return bool(fits(model)) if callable(fits) else True

    def _downgrade(self, model: str, rule: str) -> Tuple[str, str]:
        """Swap an unrunnable model for the largest one that will run.

        Returns (model, note). The note is empty when nothing changed, and is
        surfaced in the routing reason so the operator can see the swap rather
        than wondering why a debugging question got a small model.
        """
        if self.runnable(model):
            return model, ""
        candidates = sorted(
            (m for m in (self.available or set()) if self.runnable(m)),
            key=self._params_b, reverse=True)
        replacement = candidates[0] if candidates else self.default_model
        why = ("no GPU" if not getattr(self.accelerator, "gpu", False)
               else f"only {getattr(self.accelerator, 'free_vram_mb', 0)} MB VRAM free")
        note = f"; downgraded from {model} because {why}"
        self.downgrades.append((rule, model, replacement))
        return replacement, note

    def score(self, request: str) -> List[Tuple[str, str, float, List[str]]]:
        """Score every rule. Returns (rule, model, score, terms), best first."""
        text = request or ""
        raw: List[Tuple[str, str, float, List[str], int]] = []
        for name, pattern, model, _why in self.rules:
            if self.available is not None and model not in self.available:
                continue
            terms, first_at = [], -1
            for match in re.finditer(pattern, text, re.IGNORECASE):
                if first_at < 0:
                    first_at = match.start()
                terms.append(match.group(0))
            if terms:
                raw.append((name, model, self.weights.get(name, 1.0) * len(terms),
                            terms, first_at))
        if not raw:
            return []
        lead = min(row[4] for row in raw)
        scored = [(n, m, round(s * (self.LEAD_MULTIPLIER if at == lead else 1.0), 4), t)
                  for n, m, s, t, at in raw]
        scored.sort(key=lambda row: (row[2], self.weights.get(row[0], 1.0)), reverse=True)
        return scored

    def select(self, request: str) -> ModelChoice:
        """Pick the highest scoring model, with the margin as confidence."""
        scored = self.score(request)
        if not scored:
            model, note = self._downgrade(self.default_model, "default")
            return ModelChoice(model, "default",
                               "no routing rule matched, using the cheap "
                               f"default{note}", 0.3)
        name, model, score, terms = scored[0]
        runner_up = scored[1][2] if len(scored) > 1 else 0.0
        margin = (score - runner_up) / score if score else 0.0
        contest = (" over " + ", ".join(f"{n}({s:g})" for n, _m, s, _t in scored[1:3])
                   if len(scored) > 1 else "")
        model, note = self._downgrade(model, name)
        return ModelChoice(
            model, name,
            f"rule '{name}' scored {score:g} on "
            f"{sorted(set(t for t in terms if t))}{contest}{note}",
            round(0.6 + 0.4 * margin, 4))

    def explain(self, request: str) -> str:
        """Human readable score table."""
        rows = self.score(request)
        if not rows:
            return f"{request!r}: no rule matched, default {self.default_model}"
        return "\n".join([f"{request!r}:"] + [
            f"  {s:6.2f}  {n:<9} -> {m:<11} {sorted(set(t for t in te if t))}"
            for n, m, s, te in rows])
