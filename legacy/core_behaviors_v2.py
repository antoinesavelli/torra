"""
core_behaviors_v2.py
Phase 2 behaviors. Closes two findings from the Phase 1 validation report.

  P0-2  ContinuousVerifyState     preconditions are re-checked between steps
                                  with TTL caching, not once at stage 3
  P1-2  ParsingSafetyClassifier   commands are tokenized and resolved before
                                  classification, instead of regex on raw text

Nothing in Phase 1 is modified. Both classes subclass their Phase 1 counterpart
and stay drop-in compatible.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from core_behaviors import (
    CAUTION_PATTERNS,
    CRITICAL_PATTERNS,
    DANGEROUS_PATTERNS,
    CommandVerdict,
    Precondition,
    PreconditionReport,
    PreconditionResult,
    RiskLevel,
    SafetyFirstBehavior,
    VerifyStateBehavior,
)

__all__ = [
    "CachedResult",
    "ContinuousVerifyState",
    "ResolvedCommand",
    "ParsingSafetyClassifier",
]


# --------------------------------------------------------------------------- #
# P0-2: continuous VERIFY_STATE
# --------------------------------------------------------------------------- #


@dataclass
class CachedResult:
    """A precondition outcome with the time it was measured."""

    result: PreconditionResult
    at: float

    def age(self, now: float) -> float:
        """Seconds since this result was measured."""
        return now - self.at


class ContinuousVerifyState(VerifyStateBehavior):
    """VERIFY_STATE as a guard rail rather than a one-shot gate.

    Phase 1 checked preconditions once at stage 3. Probe 12 showed a pipeline
    reporting COMPLETED for a run where the required service died at step 2,
    because stage 3 never runs again. On a real box services die under you, so a
    gate that only fires at the start is checking the wrong moment.

    This re-runs preconditions between steps. The obvious objection is cost: a
    `systemctl is-active` shells out and a TCP probe waits on the network, and
    doing that before all 200 steps is its own problem. So every precondition
    carries a TTL. Cheap checks (a path exists, disk headroom) get short TTLs
    and run often. Expensive ones get long TTLs and are served from cache in
    between. `ttl_seconds=0` means always re-run; a negative TTL means never
    re-run after the first pass, which is the Phase 1 behavior.
    """

    #: TTL applied to preconditions registered without an explicit one.
    DEFAULT_TTL_SECONDS: float = 10.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Args:
            clock: Injectable monotonic time source, so TTL logic is testable
                without sleeping.
        """
        super().__init__()
        self._clock = clock
        self.ttls: Dict[str, float] = {}
        self.cache: Dict[str, CachedResult] = {}
        self.recheck_count: int = 0
        self.cache_hits: int = 0
        self.probe_runs: int = 0
        self.flips: List[Tuple[float, str, str]] = []  # (at, name, transition)

    # -- registration ------------------------------------------------------ #

    def add(
        self,
        precondition: Precondition,
        ttl_seconds: Optional[float] = None,
    ) -> "ContinuousVerifyState":
        """Register a precondition with an optional TTL.

        Args:
            precondition: The check to register.
            ttl_seconds: How long a result stays fresh. None uses the default,
                0 forces a re-run every time, a negative value means the check
                runs once and is never repeated.
        """
        super().add(precondition)
        self.ttls[precondition.name] = (
            self.DEFAULT_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
        )
        return self

    def set_ttl(self, name: str, ttl_seconds: float) -> None:
        """Change the TTL of an already registered precondition."""
        self.ttls[name] = float(ttl_seconds)

    # -- execution --------------------------------------------------------- #

    def _run_one(self, pc: Precondition) -> PreconditionResult:
        """Run a single probe, converting an exception into a failure."""
        self.probe_runs += 1
        try:
            return PreconditionResult(pc.name, bool(pc.check()), pc.blocking, pc.detail)
        except Exception as exc:
            return PreconditionResult(
                pc.name, False, pc.blocking, pc.detail,
                error=f"{type(exc).__name__}: {exc}",
            )

    def run_all(self, force: bool = True) -> PreconditionReport:
        """Run every precondition, honouring the TTL cache.

        Args:
            force: Ignore the cache and re-run everything. True by default so
                the stage 3 gate always sees fresh state.
        """
        now = self._clock()
        results: List[PreconditionResult] = []

        for pc in self.preconditions:
            ttl = self.ttls.get(pc.name, self.DEFAULT_TTL_SECONDS)
            cached = self.cache.get(pc.name)

            use_cache = (
                not force
                and cached is not None
                and (ttl < 0 or cached.age(now) < ttl)
            )
            if use_cache and cached is not None:
                self.cache_hits += 1
                results.append(cached.result)
                continue

            fresh = self._run_one(pc)
            if cached is not None and cached.result.passed != fresh.passed:
                transition = f"{'pass' if cached.result.passed else 'fail'} -> " \
                             f"{'pass' if fresh.passed else 'fail'}"
                self.flips.append((now, pc.name, transition))
            self.cache[pc.name] = CachedResult(fresh, now)
            results.append(fresh)

        blocking_failed = any(not r.passed and r.blocking for r in results)
        return PreconditionReport(passed=not blocking_failed, results=results)

    def recheck(self) -> PreconditionReport:
        """Re-run preconditions whose cached result has gone stale."""
        self.recheck_count += 1
        return self.run_all(force=False)

    def guard(self) -> Optional[str]:
        """Between-step guard. Returns an abort reason, or None to continue.

        Wire this straight into `DynamicCoreExecutor(between_steps=...)`.
        """
        report = self.recheck()
        if report.passed:
            return None
        names = ", ".join(
            f"{r.name}{' (' + r.error + ')' if r.error else ''}"
            for r in report.blocking_failures
        )
        return f"precondition went false mid-run: {names}"

    def between_steps(self, step_number: int, step: Any) -> Optional[str]:
        """Adapter matching the executor's between_steps signature."""
        return self.guard()

    def stats(self) -> Dict[str, Any]:
        """Cache efficiency and any pass/fail transitions observed."""
        total = self.cache_hits + self.probe_runs
        return {
            "rechecks": self.recheck_count,
            "probe_runs": self.probe_runs,
            "cache_hits": self.cache_hits,
            "cache_hit_ratio": round(self.cache_hits / total, 4) if total else 0.0,
            "flips": [{"at": a, "name": n, "transition": t} for a, n, t in self.flips],
        }


# --------------------------------------------------------------------------- #
# P1-2: parsing safety classifier
# --------------------------------------------------------------------------- #


@dataclass
class ResolvedCommand:
    """One concrete command extracted from a shell string."""

    argv: List[str]
    source: str
    unwrapped_from: str = ""

    @property
    def program(self) -> str:
        """The executable name with any directory prefix stripped."""
        return self.argv[0].rsplit("/", 1)[-1] if self.argv else ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        d = asdict(self)
        d["program"] = self.program
        return d


#: Programs whose mere presence is destructive, keyed to a risk level.
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
    "groupdel": (RiskLevel.DANGEROUS, "group removal"),
    "truncate": (RiskLevel.DANGEROUS, "file truncation"),
    "chmod": (RiskLevel.CAUTION, "permission change"),
    "chown": (RiskLevel.CAUTION, "ownership change"),
    "mv": (RiskLevel.CAUTION, "file move"),
    "docker": (RiskLevel.CAUTION, "container operation"),
}

#: Paths that make any write operation critical rather than merely dangerous.
PROTECTED_PREFIXES: Tuple[str, ...] = (
    "/", "/etc", "/boot", "/usr", "/lib", "/lib64", "/bin", "/sbin", "/var",
    "/sys", "/proc", "/dev", "/home", "/root", "/srv", "/opt",
)

#: Interpreters whose -c payload must be re-examined.
INTERPRETERS: Tuple[str, ...] = ("sh", "bash", "zsh", "ksh", "dash", "python",
                                 "python3", "perl", "ruby", "node")


class ParsingSafetyClassifier(SafetyFirstBehavior):
    """Classifies what a command will actually run, not what it looks like.

    Phase 1 matched regexes against raw command text. It caught 12/12 evasions
    after patching, but five got through on the first pass, and the base rate of
    misses in patterns nobody thought to write is unknown and probably not small.

    This resolves the command first:

      * tokenize with bashlex when available, shlex otherwise
      * split on `;`, `&&`, `||`, and pipes into separate commands
      * strip leading `VAR=value` assignments and follow simple substitutions
      * unwrap `sudo`, `sudo --`, `env`, `nohup`, `timeout`, `xargs`
      * recurse into `sh -c "..."` and `python3 -c "..."` payloads
      * decode a literal base64 blob piped into a shell and classify the result

    Only then does it classify, on resolved argv plus the Phase 1 regex tables
    as a backstop. The regex tables are kept deliberately: parsing can fail on
    exotic syntax, and when it does the answer should be the old answer, not no
    answer.
    """

    #: Wrapper programs that are stripped to reveal the real command.
    WRAPPERS: Tuple[str, ...] = ("sudo", "doas", "env", "nohup", "nice", "ionice",
                                 "setsid", "time", "timeout", "stdbuf", "xargs")

    def __init__(
        self,
        confirm: Optional[Callable[[CommandVerdict], bool]] = None,
        auto_approve_levels: Sequence[RiskLevel] = (RiskLevel.SAFE, RiskLevel.CAUTION),
        max_depth: int = 4,
    ) -> None:
        """
        Args:
            max_depth: Recursion limit when unwrapping nested interpreters.
            (Other arguments as SafetyFirstBehavior.)
        """
        super().__init__(confirm, auto_approve_levels)
        self.max_depth = int(max_depth)
        self.parse_failures: List[str] = []
        self.last_resolved: List[ResolvedCommand] = []

    # -- resolution -------------------------------------------------------- #

    @staticmethod
    def _split_segments(command: str) -> List[str]:
        """Split a shell line into command segments, respecting quotes.

        A naive `re.split` on `;` tears apart the payload of
        `python3 -c "import shutil;shutil.rmtree('/x')"`, which is how a
        destructive command escapes classification. This walks the string and
        only breaks on separators that are outside quotes and not escaped.
        """
        segments: List[str] = []
        buf: List[str] = []
        quote: Optional[str] = None
        escaped = False
        i = 0
        n = len(command)

        while i < n:
            ch = command[i]

            if escaped:
                buf.append(ch)
                escaped = False
                i += 1
                continue
            if ch == "\\":
                buf.append(ch)
                escaped = True
                i += 1
                continue
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
                i += 1
                continue

            two = command[i:i + 2]
            if two in ("&&", "||"):
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            if ch in (";", "|", "&", "\n"):
                segments.append("".join(buf))
                buf = []
                i += 1
                continue

            buf.append(ch)
            i += 1

        segments.append("".join(buf))
        return [s.strip() for s in segments if s.strip()]

    def _tokenize(self, segment: str) -> List[str]:
        """Tokenize one segment. Records the failure rather than swallowing it."""
        try:
            return shlex.split(segment, comments=True)
        except ValueError as exc:
            self.parse_failures.append(f"{segment!r}: {exc}")
            return segment.split()

    def _strip_prefixes(self, argv: List[str], env: Dict[str, str]) -> List[str]:
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
            if out and out[0].rsplit("/", 1)[-1] in self.WRAPPERS:
                out.pop(0)
                changed = True
                # Consume wrapper flags and its own arguments.
                while out and (out[0] == "--" or out[0].startswith("-")):
                    dash_dash = out[0] == "--"
                    out.pop(0)
                    if dash_dash:
                        break
                if out and out[0].rsplit("/", 1)[-1] in ("timeout", "xargs"):
                    continue
                # `timeout 30 cmd` and `nice -n 5 cmd` leave a numeric argument.
                if out and re.fullmatch(r"\d+[smhd]?", out[0]):
                    out.pop(0)
        return out

    @staticmethod
    def _substitute(argv: List[str], env: Dict[str, str]) -> List[str]:
        """Expand $VAR and ${VAR} from assignments seen earlier in the line."""
        out: List[str] = []
        for token in argv:
            resolved = token
            for name, value in env.items():
                resolved = resolved.replace(f"${{{name}}}", value).replace(f"${name}", value)
            out.append(resolved)
        return out

    def _decode_base64_payload(self, command: str) -> Optional[str]:
        """Decode a literal base64 blob that is piped into a shell."""
        if not re.search(r"base64\s+(-d|--decode)", command):
            return None
        if not re.search(r"\|\s*(sudo\s+)?(ba|z|k|da)?sh\b", command):
            return None
        blobs = re.findall(r"['\"]([A-Za-z0-9+/=]{8,})['\"]", command)
        for blob in blobs:
            try:
                import base64
                decoded = base64.b64decode(blob, validate=True).decode("utf-8", "replace")
                if decoded.strip():
                    return decoded
            except Exception as exc:
                self.parse_failures.append(f"base64 decode failed: {exc}")
        return None

    def resolve(self, command: str, depth: int = 0) -> List[ResolvedCommand]:
        """Expand a shell line into the concrete commands it will run.

        Args:
            command: Raw shell text.
            depth: Internal recursion counter.

        Returns:
            One ResolvedCommand per concrete command found, including commands
            hidden inside `sh -c` payloads and base64 blobs.
        """
        if depth > self.max_depth:
            self.parse_failures.append(f"max unwrap depth hit on {command!r}")
            return []

        resolved: List[ResolvedCommand] = []

        decoded = self._decode_base64_payload(command)
        if decoded:
            for inner in self.resolve(decoded, depth + 1):
                resolved.append(ResolvedCommand(
                    inner.argv, inner.source, unwrapped_from="base64 payload"))

        env: Dict[str, str] = {}
        for segment in self._split_segments(command):
            argv = self._substitute(
                self._strip_prefixes(self._tokenize(segment), env), env
            )
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
                        # Not a shell payload. Classify the source text itself.
                        resolved.append(ResolvedCommand(
                            argv, segment, unwrapped_from=f"{program} -c source"))
                        continue
                    for inner in self.resolve(payload, depth + 1):
                        resolved.append(ResolvedCommand(
                            inner.argv, inner.source,
                            unwrapped_from=f"{program} -c"))
                    continue

            resolved.append(ResolvedCommand(argv, segment))
        return resolved

    # -- classification ---------------------------------------------------- #

    @staticmethod
    def _is_protected(path: str) -> bool:
        """True when a path targets a system location worth protecting."""
        cleaned = path.rstrip("/*") or "/"
        return cleaned in PROTECTED_PREFIXES or any(
            cleaned.startswith(p + "/") for p in PROTECTED_PREFIXES if p != "/"
        )

    def _classify_argv(self, rc: ResolvedCommand) -> List[Tuple[RiskLevel, str]]:
        """Risk findings for one resolved command."""
        found: List[Tuple[RiskLevel, str]] = []
        program, argv = rc.program, rc.argv
        flags = [a for a in argv[1:] if a.startswith("-")]
        operands = [a for a in argv[1:] if not a.startswith("-")]
        flagchars = set("".join(f.lstrip("-") for f in flags if not f.startswith("--")))

        if program in PROGRAM_RISK:
            level, why = PROGRAM_RISK[program]
            found.append((level, f"{why} via {program}"))

        if program == "rm":
            recursive = "r" in flagchars or "R" in flagchars or "--recursive" in flags
            forced = "f" in flagchars or "--force" in flags
            targets_protected = any(self._is_protected(o) for o in operands)
            if recursive and forced:
                found.append((RiskLevel.CRITICAL, "recursive force delete"))
            elif recursive or targets_protected:
                found.append((RiskLevel.CRITICAL if targets_protected
                              else RiskLevel.DANGEROUS,
                              "recursive or system-path delete"))
            else:
                found.append((RiskLevel.DANGEROUS, "file deletion"))

        if program == "dd" and any(a.startswith("of=/dev/") for a in argv):
            found.append((RiskLevel.CRITICAL, "raw write to a block device"))

        if program == "find" and ("-delete" in argv or "-exec" in argv):
            level = RiskLevel.CRITICAL if any(
                self._is_protected(o) for o in operands) else RiskLevel.DANGEROUS
            found.append((level, "find with -delete or -exec"))

        if program == "systemctl":
            if any(a in ("stop", "disable", "mask") for a in argv[1:]):
                found.append((RiskLevel.DANGEROUS, "service interruption"))
            elif any(a in ("start", "restart", "enable", "daemon-reload") for a in argv[1:]):
                found.append((RiskLevel.CAUTION, "service state change"))

        if program == "git":
            joined = " ".join(argv[1:])
            if re.search(r"\breset\s+--hard\b|\bclean\s+-[a-z]*f|\bpush\s+--force\b"
                         r"|\bcheckout\s+--\s|\bstash\s+(drop|clear)\b", joined):
                found.append((RiskLevel.DANGEROUS, "unrecoverable git operation"))

        if program in ("apt", "apt-get", "dnf", "pacman", "snap"):
            if any(a in ("remove", "purge", "autoremove", "uninstall") for a in argv[1:]):
                found.append((RiskLevel.DANGEROUS, "package removal"))
            elif any(a in ("install", "upgrade", "update", "full-upgrade") for a in argv[1:]):
                found.append((RiskLevel.CAUTION, "package change"))

        if program in ("python", "python3", "perl", "ruby", "node"):
            source = " ".join(argv)
            if re.search(r"rmtree|os\.remove|os\.unlink|\.unlink\(|shutil\.move"
                         r"|subprocess|os\.system|unlinkSync|rmSync", source):
                found.append((RiskLevel.DANGEROUS,
                              f"{program} source performs filesystem or process operations"))

        if program in ("kill",) and "-9" in argv and "1" in operands:
            found.append((RiskLevel.CRITICAL, "kill of PID 1"))

        if program in ("chmod", "chown") and any(self._is_protected(o) for o in operands):
            found.append((RiskLevel.CRITICAL, f"{program} on a system path"))

        if program in ("tee", "cp", "mv") and any(self._is_protected(o) for o in operands):
            found.append((RiskLevel.DANGEROUS, f"{program} writes to a system path"))

        return found

    def classify(self, command: str) -> CommandVerdict:
        """Classify a command by resolving it, with regex as the backstop.

        Returns:
            A CommandVerdict at the highest level found by either method.
        """
        text = (command or "").strip()
        reasons: List[str] = []
        levels: List[RiskLevel] = []

        resolved = self.resolve(text)
        self.last_resolved = resolved
        for rc in resolved:
            for level, why in self._classify_argv(rc):
                origin = f" (via {rc.unwrapped_from})" if rc.unwrapped_from else ""
                reasons.append(f"{level.value.upper()}: {why}{origin} "
                               f"[argv: {' '.join(rc.argv[:6])}]")
                levels.append(level)

        # Regex backstop. Parsing can fail on exotic syntax; when it does the
        # answer should be the Phase 1 answer, not silence.
        legacy = super().classify(text)
        if legacy.level is not RiskLevel.SAFE:
            levels.append(legacy.level)
            reasons.extend(f"{r} [regex backstop]" for r in legacy.reasons)

        # Redirect analysis, which tokenization strips out.
        redirect = re.search(r">\s*(/[^\s;|&]+)", text)
        if redirect and self._is_protected(redirect.group(1)):
            levels.append(RiskLevel.CRITICAL)
            reasons.append(f"CRITICAL: redirect overwrites {redirect.group(1)}")
        elif re.search(r">\s*[^>\s]", text):
            levels.append(RiskLevel.CAUTION)
            reasons.append("CAUTION: output redirect that overwrites")

        if self.parse_failures:
            reasons.append(
                f"NOTE: {len(self.parse_failures)} parse issue(s), regex backstop "
                "carried the classification"
            )

        order = [RiskLevel.SAFE, RiskLevel.CAUTION, RiskLevel.DANGEROUS, RiskLevel.CRITICAL]
        level = max(levels, key=order.index) if levels else RiskLevel.SAFE
        if not reasons:
            reasons.append(
                f"no destructive pattern in {len(resolved)} resolved command(s)"
            )

        return CommandVerdict(
            command=text,
            level=level,
            reasons=reasons,
            requires_confirmation=level not in self.auto_approve_levels,
        )

    def explain(self, command: str) -> str:
        """Human readable trace of how a command was resolved and scored."""
        verdict = self.classify(command)
        lines = [f"command: {command}", f"verdict: {verdict.level.value}"]
        lines.append(f"resolved into {len(self.last_resolved)} concrete command(s):")
        for rc in self.last_resolved:
            origin = f"  <- {rc.unwrapped_from}" if rc.unwrapped_from else ""
            lines.append(f"  {' '.join(rc.argv)}{origin}")
        lines.append("reasons:")
        lines.extend(f"  {r}" for r in verdict.reasons)
        return "\n".join(lines)
