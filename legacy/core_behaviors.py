"""
core_behaviors.py
Phase 1B: the five non-negotiable behavioral guardrails.

  1. SafetyFirstBehavior            classify and confirm dangerous commands
  2. TransparencyBehavior           log the reasoning behind every decision
  3. VerifyStateBehavior            check reality before critical operations
  4. RollbackOnFailBehavior         stack based undo in LIFO order
  5. AskClarifyingQuestionsBehavior detect and resolve ambiguity

Plus ModelSelector (task to model routing) and CoreBehaviorValidator, which
pushes a task through all five gates and returns one verdict.

Every prompt, confirm, and answer is an injected callable. Nothing here reads
stdin directly, so the whole module is testable without a TTY.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "RiskLevel",
    "CommandVerdict",
    "SafetyFirstBehavior",
    "DecisionRecord",
    "TransparencyBehavior",
    "Precondition",
    "PreconditionResult",
    "PreconditionReport",
    "VerifyStateBehavior",
    "RollbackAction",
    "RollbackResult",
    "RollbackReport",
    "RollbackOnFailBehavior",
    "Ambiguity",
    "ClarificationReport",
    "AskClarifyingQuestionsBehavior",
    "ModelChoice",
    "ModelSelector",
    "BehaviorValidationReport",
    "CoreBehaviorValidator",
]


# --------------------------------------------------------------------------- #
# Behavior 1: SAFETY_FIRST
# --------------------------------------------------------------------------- #


class RiskLevel(str, Enum):
    """How much damage a command can do if it is wrong."""

    SAFE = "safe"            # read only, reversible, local
    CAUTION = "caution"      # writes state, reversible with effort
    DANGEROUS = "dangerous"  # destroys data or interrupts service
    CRITICAL = "critical"    # can brick the box or wipe a disk


#: Pattern table. Order matters: the first match at the highest tier wins.
CRITICAL_PATTERNS: List[Tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-{1,2}[a-zA-Z]*[rR][a-zA-Z]*f|\brm\s+-{1,2}f[a-zA-Z]*[rR]",
     "recursive force delete"),
    (r"\brm\b[^|;&]*\s(/|/\*|~|\$HOME)\s*$", "delete of a root or home path"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "raw write to a block device"),
    (r">\s*/dev/(sd|nvme|vd)", "redirect over a block device"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bchmod\s+(-R\s+)?777\s+/\s*$", "world writable root"),
    (r"\bchown\s+-R\b[^|;&]*\s/\s*$", "recursive chown of root"),
    (r"\b(shred|wipefs)\b", "destructive disk wipe"),
    (r"\bshutil\.rmtree\b", "python recursive tree delete"),
    (r"\bfind\b[^|;&]*\s-delete\b", "find with -delete"),
    (r"\bfind\b[^|;&]*-exec\s+(/bin/)?rm\b", "find piped into rm"),
    (r"base64\s+(-d|--decode)[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh", "obfuscated payload piped to a shell"),
    (r"\|\s*(sudo\s+)?(ba|z|k)?sh\b", "arbitrary stdin piped into a shell"),
]

DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
    (r"\b(reboot|poweroff|halt)\b", "host restart or power off"),
    (r"\bshutdown\b", "host shutdown"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "service interruption"),
    (r"\bkill\s+-9\s+1\b", "kill of PID 1"),
    (r"\bpkill\b|\bkillall\b", "broad process kill"),
    (r"\b(apt|apt-get|dnf|pacman)\s+(remove|purge|autoremove)\b", "package removal"),
    (r"\biptables\s+-F\b|\bufw\s+(disable|reset)\b", "firewall teardown"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+--force)\b",
     "unrecoverable git operation"),
    (r"\brm\b", "file deletion"),
    (r"\btruncate\b", "file truncation"),
    (r"\bdrop\s+(table|database)\b", "database drop"),
    (r"\bgit\s+checkout\s+--\s", "discards uncommitted local edits"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "discards stashed work"),
    (r"^\s*>\s*/(etc|boot|var|usr|lib)/", "truncation of a system file"),
    (r"\b(tee|dd)\b[^|;&]*\s/(etc|boot)/", "overwrite of a system file"),
    (r"\bos\.remove\b|\bos\.unlink\b|\bPath\([^)]*\)\.unlink\b", "python file delete"),
    (r"curl[^|]*\|\s*(sudo\s+)?(ba)?sh", "pipe from network into a shell"),
    (r"wget[^|]*\|\s*(sudo\s+)?(ba)?sh", "pipe from network into a shell"),
]

CAUTION_PATTERNS: List[Tuple[str, str]] = [
    (r"\bsudo\b", "privilege escalation"),
    (r"\b(apt|apt-get|dnf|pacman)\s+(install|upgrade|update)\b", "package change"),
    (r"\bsystemctl\s+(start|restart|enable)\b", "service state change"),
    (r"\bmv\b|\bcp\s+-[a-zA-Z]*f", "file move or forced copy"),
    (r">\s*[^>\s]", "output redirect that overwrites"),
    (r"\bdocker\s+(rm|rmi|prune)\b", "container teardown"),
    (r"\bchmod\b|\bchown\b", "permission change"),
]


@dataclass(frozen=True)
class CommandVerdict:
    """Safety classification of one command."""

    command: str
    level: RiskLevel
    reasons: List[str]
    requires_confirmation: bool
    approved: Optional[bool] = None
    blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        d = asdict(self)
        d["level"] = self.level.value
        return d


class SafetyFirstBehavior:
    """Gate 1. Nothing dangerous runs without an explicit human yes.

    The default confirm callback returns False. That is deliberate: a harness
    wired up wrong should block, never auto-approve.
    """

    def __init__(
        self,
        confirm: Optional[Callable[[CommandVerdict], bool]] = None,
        auto_approve_levels: Sequence[RiskLevel] = (RiskLevel.SAFE, RiskLevel.CAUTION),
    ) -> None:
        """
        Args:
            confirm: Callable that shows the verdict to a human and returns
                True to proceed. Defaults to deny.
            auto_approve_levels: Levels that run without a prompt.
        """
        self._confirm = confirm or (lambda verdict: False)
        self.auto_approve_levels = set(auto_approve_levels)
        self.history: List[CommandVerdict] = []

    def classify(self, command: str) -> CommandVerdict:
        """Classify a shell command into a RiskLevel with stated reasons."""
        text = (command or "").strip()
        reasons: List[str] = []
        level = RiskLevel.SAFE

        for pattern, why in CRITICAL_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                reasons.append(f"CRITICAL: {why}")
                level = RiskLevel.CRITICAL

        if level is not RiskLevel.CRITICAL:
            for pattern, why in DANGEROUS_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    reasons.append(f"DANGEROUS: {why}")
                    level = RiskLevel.DANGEROUS

        if level in (RiskLevel.SAFE,):
            for pattern, why in CAUTION_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    reasons.append(f"CAUTION: {why}")
                    level = RiskLevel.CAUTION

        if not reasons:
            reasons.append("no destructive pattern matched")

        return CommandVerdict(
            command=text,
            level=level,
            reasons=reasons,
            requires_confirmation=level not in self.auto_approve_levels,
        )

    def check(self, command: str) -> CommandVerdict:
        """Classify, then prompt when required.

        Returns:
            A CommandVerdict with `approved` and `blocked` filled in.
            blocked=True means the caller must not execute the command.
        """
        verdict = self.classify(command)
        if not verdict.requires_confirmation:
            final = CommandVerdict(
                verdict.command, verdict.level, verdict.reasons,
                False, approved=True, blocked=False,
            )
            self.history.append(final)
            return final

        try:
            approved = bool(self._confirm(verdict))
        except Exception as exc:
            approved = False
            verdict = CommandVerdict(
                verdict.command, verdict.level,
                verdict.reasons + [f"confirm callback raised {type(exc).__name__}: {exc}"],
                True,
            )

        final = CommandVerdict(
            verdict.command, verdict.level, verdict.reasons,
            True, approved=approved, blocked=not approved,
        )
        self.history.append(final)
        return final

    def guard(self, command: str, run: Callable[[], Any]) -> Tuple[CommandVerdict, Any]:
        """Run `run` only when the command clears the gate.

        Returns:
            (verdict, result). result is None when the command was blocked.
        """
        verdict = self.check(command)
        if verdict.blocked:
            return verdict, None
        return verdict, run()


# --------------------------------------------------------------------------- #
# Behavior 2: TRANSPARENCY
# --------------------------------------------------------------------------- #


@dataclass
class DecisionRecord:
    """One logged decision with its reasoning and supporting evidence."""

    stage: str
    decision: str
    reasoning: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return asdict(self)

    def render(self) -> str:
        """Human readable one line form for the terminal."""
        return f"[{self.stage}] {self.decision}\n    why: {self.reasoning}"


class TransparencyBehavior:
    """Gate 2. Every branch the agent takes is explained and recorded."""

    def __init__(self, clock: Callable[[], float] = time.time,
                 sink: Optional[Callable[[DecisionRecord], None]] = None) -> None:
        """Args: clock: time source. sink: optional live callback per record."""
        """Args:
            clock: time source.
            sink: optional live callback fired per record.
        """
        self.records: List[DecisionRecord] = []
        self._clock = clock
        self._sink = sink

    def log_decision(
        self,
        stage: str,
        decision: str,
        reasoning: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> DecisionRecord:
        """Record one decision. Reasoning is required, not optional."""
        if not reasoning or not reasoning.strip():
            reasoning = "NO REASONING SUPPLIED (transparency violation)"
        record = DecisionRecord(
            stage=stage,
            decision=decision,
            reasoning=reasoning,
            evidence=dict(evidence or {}),
            at=self._clock(),
        )
        self.records.append(record)
        if self._sink:
            try:
                self._sink(record)
            except Exception:
                pass  # a broken sink must not break the run
        return record

    def explain_model_choice(self, task: str, choice: "ModelChoice") -> DecisionRecord:
        """Log why a particular model was routed to this task."""
        return self.log_decision(
            stage="model_select",
            decision=f"route to {choice.model}",
            reasoning=choice.reason,
            evidence={"task": task, "matched_rule": choice.matched_rule,
                      "confidence": choice.confidence},
        )

    def audit_trail(self) -> List[Dict[str, Any]]:
        """Serializable list of every decision, in order."""
        return [r.to_dict() for r in self.records]

    def violations(self) -> List[DecisionRecord]:
        """Decisions that were logged without real reasoning."""
        return [r for r in self.records if "transparency violation" in r.reasoning]


# --------------------------------------------------------------------------- #
# Behavior 3: VERIFY_STATE
# --------------------------------------------------------------------------- #


@dataclass
class Precondition:
    """A named reality check that must pass before a critical operation."""

    name: str
    check: Callable[[], bool]
    detail: str = ""
    blocking: bool = True


@dataclass
class PreconditionResult:
    """Outcome of one precondition."""

    name: str
    passed: bool
    blocking: bool
    detail: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return asdict(self)


@dataclass
class PreconditionReport:
    """Aggregate of every precondition run for a task."""

    passed: bool
    results: List[PreconditionResult] = field(default_factory=list)

    @property
    def failures(self) -> List[PreconditionResult]:
        """Every precondition that did not pass, blocking or not."""
        return [r for r in self.results if not r.passed]

    @property
    def blocking_failures(self) -> List[PreconditionResult]:
        """Failed preconditions that halt the task."""
        return [r for r in self.results if not r.passed and r.blocking]

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {"passed": self.passed, "results": [r.to_dict() for r in self.results]}


class VerifyStateBehavior:
    """Gate 3. Check reality before acting on assumptions.

    Ships with real Pop-OS probes for service, disk, network, path, and binary.
    Every probe is also usable as a plain callable in tests.
    """

    def __init__(self) -> None:
        """Start with an empty precondition list."""
        self.preconditions: List[Precondition] = []

    # -- registration ------------------------------------------------------ #

    def add(self, precondition: Precondition) -> "VerifyStateBehavior":
        """Register a precondition. Returns self for chaining."""
        self.preconditions.append(precondition)
        return self

    def require_service(self, unit: str, user_scope: bool = False) -> "VerifyStateBehavior":
        """Require a systemd unit to be active."""
        return self.add(Precondition(
            name=f"service:{unit}",
            check=lambda: self.service_active(unit, user_scope),
            detail=f"systemd unit {unit} must be active",
        ))

    def require_disk_free(self, path: str = "/", min_gb: float = 2.0) -> "VerifyStateBehavior":
        """Require at least min_gb free on the filesystem holding path."""
        return self.add(Precondition(
            name=f"disk:{path}>={min_gb}GB",
            check=lambda: self.disk_free_gb(path) >= min_gb,
            detail=f"at least {min_gb} GB free on {path}",
        ))

    def require_network(self, host: str = "1.1.1.1", port: int = 53,
                        timeout: float = 1.0) -> "VerifyStateBehavior":
        """Require outbound TCP reachability."""
        return self.add(Precondition(
            name=f"network:{host}:{port}",
            check=lambda: self.tcp_reachable(host, port, timeout),
            detail=f"TCP {host}:{port} reachable",
        ))

    def require_path(self, path: str) -> "VerifyStateBehavior":
        """Require a filesystem path to exist."""
        return self.add(Precondition(
            name=f"path:{path}",
            check=lambda: Path(path).exists(),
            detail=f"{path} must exist",
        ))

    def require_binary(self, name: str) -> "VerifyStateBehavior":
        """Require an executable to be on PATH."""
        return self.add(Precondition(
            name=f"binary:{name}",
            check=lambda: shutil.which(name) is not None,
            detail=f"{name} must be on PATH",
        ))

    # -- probes ------------------------------------------------------------ #

    @staticmethod
    def service_active(unit: str, user_scope: bool = False) -> bool:
        """True when systemctl reports the unit active. False on any error."""
        cmd = ["systemctl"] + (["--user"] if user_scope else []) + ["is-active", unit]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return out.stdout.strip() == "active"
        except Exception:
            return False

    @staticmethod
    def disk_free_gb(path: str = "/") -> float:
        """Free space in GB on the filesystem holding path. -1.0 on error."""
        try:
            usage = shutil.disk_usage(path)
            return usage.free / (1024 ** 3)
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

    def run_all(self) -> PreconditionReport:
        """Run every registered precondition.

        A probe that raises counts as failed and the exception is recorded.
        Nothing here swallows an error quietly.
        """
        results: List[PreconditionResult] = []
        for pc in self.preconditions:
            try:
                ok = bool(pc.check())
                results.append(PreconditionResult(pc.name, ok, pc.blocking, pc.detail))
            except Exception as exc:
                results.append(PreconditionResult(
                    pc.name, False, pc.blocking, pc.detail,
                    error=f"{type(exc).__name__}: {exc}",
                ))
        blocking_failed = any(not r.passed and r.blocking for r in results)
        return PreconditionReport(passed=not blocking_failed, results=results)

    def reset(self) -> None:
        """Drop all registered preconditions."""
        self.preconditions.clear()


# --------------------------------------------------------------------------- #
# Behavior 4: ROLLBACK_ON_FAIL
# --------------------------------------------------------------------------- #


@dataclass
class RollbackAction:
    """One undoable change, pushed before the forward action runs."""

    description: str
    undo: Callable[[], Any]
    safe: bool = True
    step_id: str = ""


@dataclass
class RollbackResult:
    """Outcome of undoing one action."""

    description: str
    status: str  # "undone" | "cancelled" | "failed"
    error: str = ""
    order: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return asdict(self)


@dataclass
class RollbackReport:
    """Aggregate of one rollback sweep."""

    ok: bool
    results: List[RollbackResult] = field(default_factory=list)

    @property
    def undone(self) -> List[RollbackResult]:
        """Actions whose undo callable ran without raising."""
        return [r for r in self.results if r.status == "undone"]

    @property
    def cancelled(self) -> List[RollbackResult]:
        """Unsafe undos the operator declined."""
        return [r for r in self.results if r.status == "cancelled"]

    @property
    def failed(self) -> List[RollbackResult]:
        """Undos that raised."""
        return [r for r in self.results if r.status == "failed"]

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {"ok": self.ok, "results": [r.to_dict() for r in self.results]}


class RollbackOnFailBehavior:
    """Gate 4. A stack of undo actions, unwound in LIFO order on failure.

    An action flagged unsafe (its undo could itself destroy data) gets a
    confirmation prompt. Denying it skips that undo and everything below it
    stays untouched, because a partially unwound stack is safer than blindly
    continuing past a refused step.
    """

    def __init__(
        self,
        confirm_unsafe: Optional[Callable[[RollbackAction], bool]] = None,
        stop_on_cancel: bool = True,
    ) -> None:
        """
        Args:
            confirm_unsafe: Prompt for actions marked unsafe. Defaults to deny.
            stop_on_cancel: When True, a cancelled undo halts the sweep.
        """
        self._stack: List[RollbackAction] = []
        self._confirm = confirm_unsafe or (lambda action: False)
        self.stop_on_cancel = stop_on_cancel

    def push(self, description: str, undo: Callable[[], Any],
             safe: bool = True, step_id: str = "") -> RollbackAction:
        """Push an undo action onto the stack before making the change."""
        action = RollbackAction(description=description, undo=undo,
                                safe=safe, step_id=step_id)
        self._stack.append(action)
        return action

    @property
    def depth(self) -> int:
        """Number of pending undo actions."""
        return len(self._stack)

    def peek(self) -> Optional[RollbackAction]:
        """Top of the stack without popping."""
        return self._stack[-1] if self._stack else None

    def clear(self) -> None:
        """Drop the stack. Call on success, once changes are committed."""
        self._stack.clear()

    def rollback(self) -> RollbackReport:
        """Unwind the stack newest first.

        Returns:
            A RollbackReport. ok=False when any undo failed or was cancelled.
        """
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
                        f"confirm raised {type(exc).__name__}: {exc}", order,
                    ))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue
                if not approved:
                    results.append(RollbackResult(
                        action.description, "cancelled",
                        "operator declined an unsafe undo", order,
                    ))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue

            try:
                action.undo()
                results.append(RollbackResult(action.description, "undone", "", order))
            except Exception as exc:
                results.append(RollbackResult(
                    action.description, "failed",
                    f"{type(exc).__name__}: {exc}", order,
                ))

        ok = all(r.status == "undone" for r in results)
        return RollbackReport(ok=ok, results=results)


# --------------------------------------------------------------------------- #
# Behavior 5: ASK_CLARIFYING_QUESTIONS
# --------------------------------------------------------------------------- #


@dataclass
class Ambiguity:
    """One thing about the task request that is underspecified."""

    code: str
    question: str
    why: str
    answer: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return asdict(self)


@dataclass
class ClarificationReport:
    """Result of the ambiguity sweep."""

    original: str
    ambiguities: List[Ambiguity] = field(default_factory=list)
    resolved: bool = True
    refined_task: str = ""

    @property
    def needs_clarification(self) -> bool:
        """True when at least one ambiguity was detected."""
        return bool(self.ambiguities)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {
            "original": self.original,
            "ambiguities": [a.to_dict() for a in self.ambiguities],
            "resolved": self.resolved,
            "refined_task": self.refined_task,
        }


#: (regex, code, question, why)
AMBIGUITY_RULES: List[Tuple[str, str, str, str]] = [
    (r"^\s*(fix|clean\s*up|sort\s*out|deal\s*with|handle)\s+(it|this|that|things?)\s*\.?\s*$",
     "no_target", "Which specific file, service, or process should I act on?",
     "the request has a verb but no object"),
    (r"\b(make\s+it\s+(better|faster|work)|optimi[sz]e\s+(it|things|stuff))\b",
     "vague_goal", "What measurable outcome counts as success here?",
     "no acceptance criterion was given"),
    (r"\b(all|every|everything)\s+(the\s+)?(files?|logs?|containers?|services?|packages?)\b",
     "unbounded_scope", "What is the exact path or filter for the set you mean?",
     "an unbounded scope can hit far more than intended"),
    (r"\b(delete|remove|purge|drop|wipe)\b",
     "destructive_scope", "Should I back up or stage the targets before removing them?",
     "the request destroys state and no backup plan was stated"),
    (r"\b(deploy|release|ship|push)\b(?!.*\b(to|into)\s+\S+)",
     "no_environment", "Which environment is the target: local, staging, or production?",
     "a deploy without a named environment is a coin flip"),
    (r"\b(the\s+server|the\s+box|the\s+machine|the\s+db|the\s+database)\b",
     "ambiguous_referent", "Which host or instance are you referring to?",
     "a definite article without a prior referent is ambiguous"),
    (r"\b(later|soon|sometime|asap|when\s+you\s+can)\b",
     "no_schedule", "Do you want this run now, or scheduled for a specific time?",
     "timing was left open"),
]

#: A request is also flagged when it is this short and contains no concrete noun.
MIN_SPECIFIC_TOKENS: int = 4


class AskClarifyingQuestionsBehavior:
    """Gate 5. Refuse to guess when the request is underspecified."""

    def __init__(
        self,
        ask: Optional[Callable[[Ambiguity], Optional[str]]] = None,
        rules: Optional[List[Tuple[str, str, str, str]]] = None,
    ) -> None:
        """
        Args:
            ask: Callable that puts one question to the operator and returns the
                answer, or None when unanswered. Defaults to no answer, which
                leaves the report unresolved and blocks the pipeline.
            rules: Optional override of the ambiguity rule table.
        """
        self._ask = ask or (lambda ambiguity: None)
        self.rules = list(rules if rules is not None else AMBIGUITY_RULES)

    def detect(self, task: str) -> List[Ambiguity]:
        """Return every ambiguity found in a task description."""
        text = (task or "").strip()
        found: List[Ambiguity] = []
        seen: set[str] = set()

        for pattern, code, question, why in self.rules:
            if code in seen:
                continue
            if re.search(pattern, text, re.IGNORECASE):
                found.append(Ambiguity(code=code, question=question, why=why))
                seen.add(code)

        tokens = [t for t in re.findall(r"[A-Za-z0-9_./:-]+", text)]
        has_concrete = any(
            ("/" in t) or ("." in t) or t.isupper() or any(c.isdigit() for c in t)
            for t in tokens
        )
        if len(tokens) < MIN_SPECIFIC_TOKENS and not has_concrete and "underspecified" not in seen:
            found.append(Ambiguity(
                code="underspecified",
                question="Can you describe the task with a concrete target and outcome?",
                why=f"the request is only {len(tokens)} tokens and names nothing concrete",
            ))
        return found

    def clarify(self, task: str) -> ClarificationReport:
        """Detect ambiguity, then ask about each one.

        Returns:
            A ClarificationReport. resolved=False means the pipeline must stop
            and hand the questions back to the operator.
        """
        ambiguities = self.detect(task)
        report = ClarificationReport(original=task, ambiguities=ambiguities)
        if not ambiguities:
            report.refined_task = task
            report.resolved = True
            return report

        answered: List[str] = []
        for amb in ambiguities:
            try:
                amb.answer = self._ask(amb)
            except Exception as exc:
                amb.answer = None
                amb.why = f"{amb.why} (ask callback raised {type(exc).__name__}: {exc})"
            if amb.answer:
                answered.append(f"{amb.code}={amb.answer}")

        report.resolved = all(a.answer for a in ambiguities)
        report.refined_task = (
            f"{task} [clarified: {'; '.join(answered)}]" if answered else task
        )
        return report


# --------------------------------------------------------------------------- #
# Model routing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelChoice:
    """The model a task was routed to, with the rule that picked it."""

    model: str
    matched_rule: str
    reason: str
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return asdict(self)


#: (rule name, regex, model, why). Evaluated top down, first match wins.
DEFAULT_ROUTING_RULES: List[Tuple[str, str, str, str]] = [
    # Debug is evaluated before deploy on purpose. "deploy is broken" is a
    # debugging task that happens to mention deployment, and routing it to a
    # code model instead of a reasoning model was a real miss found in testing.
    ("debug", r"\b(debug|traceback|stack\s*trace|crash|crashing|why\s+(is|does|did)|root\s*cause|failing|broken|bug)\b",
     "qwen3:32b", "debugging needs the deepest reasoning budget available locally"),
    ("deploy", r"\b(deploy|release|ship|rollout|provision|terraform|ansible|systemd\s+unit)\b",
     "devstral", "deployment work needs strong tool and file editing behavior"),
    ("code", r"\b(refactor|implement|write\s+(a\s+)?(script|function|class)|patch|migrate\s+code)\b",
     "devstral", "code generation is devstral's trained specialty"),
    ("plan", r"\b(plan|architect|design|strategy|trade[- ]?off|evaluate\s+options)\b",
     "qwen3:32b", "planning benefits from long context reasoning"),
    ("analyze", r"\b(analy[sz]e|summar[iy]|review|audit|explain)\b",
     "qwen3:32b", "analysis is reasoning bound, not tool bound"),
    ("quick", r"\b(list|show|print|cat|what\s+is|check|status)\b",
     "qwen3:8b", "trivial lookups do not justify loading a 32b model"),
]

DEFAULT_MODEL: str = "qwen3:8b"


class ModelSelector:
    """Routes a task description to the right local model.

    Rules can be supplied inline or loaded from CORE_RULES.yaml so the routing
    table lives next to the rest of the system prompt configuration.
    """

    def __init__(
        self,
        rules: Optional[List[Tuple[str, str, str, str]]] = None,
        default_model: str = DEFAULT_MODEL,
        available: Optional[Sequence[str]] = None,
    ) -> None:
        """
        Args:
            rules: Routing table. Defaults to DEFAULT_ROUTING_RULES.
            default_model: Used when nothing matches.
            available: Optional whitelist. A rule pointing at a model that is
                not available falls through to the next rule.
        """
        self.rules = list(rules if rules is not None else DEFAULT_ROUTING_RULES)
        self.default_model = default_model
        self.available = set(available) if available else None

    @classmethod
    def from_yaml(cls, path: os.PathLike[str] | str,
                  default_model: str = DEFAULT_MODEL) -> "ModelSelector":
        """Load routing rules from CORE_RULES.yaml.

        Expected shape:

            model_routing:
              default: qwen3:8b
              rules:
                - name: deploy
                  pattern: "\\b(deploy|release)\\b"
                  model: devstral
                  reason: "deployment work needs tool use"

        Falls back to the built in table when the file is missing or the key is
        absent, and says so rather than failing silently.
        """
        import yaml  # local import keeps the module usable without PyYAML

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"routing config not found: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        block = data.get("model_routing") or {}
        raw_rules = block.get("rules") or []
        rules = [
            (r["name"], r["pattern"], r["model"], r.get("reason", "configured rule"))
            for r in raw_rules
        ]
        return cls(rules=rules or None,
                   default_model=block.get("default", default_model))

    def select(self, task: str) -> ModelChoice:
        """Pick a model for a task description."""
        text = (task or "")
        for name, pattern, model, why in self.rules:
            if self.available is not None and model not in self.available:
                continue
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return ModelChoice(
                    model=model,
                    matched_rule=name,
                    reason=f"matched '{match.group(0)}' on rule '{name}': {why}",
                    confidence=0.9,
                )
        return ModelChoice(
            model=self.default_model,
            matched_rule="default",
            reason="no routing rule matched, using the cheap default",
            confidence=0.3,
        )


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #


@dataclass
class BehaviorValidationReport:
    """Verdict from pushing a task through all five behavior gates."""

    task: str
    passed: bool
    blocked_at: Optional[str] = None
    clarification: Optional[ClarificationReport] = None
    model_choice: Optional[ModelChoice] = None
    preconditions: Optional[PreconditionReport] = None
    safety: List[CommandVerdict] = field(default_factory=list)
    rollback_depth: int = 0
    decisions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {
            "task": self.task,
            "passed": self.passed,
            "blocked_at": self.blocked_at,
            "clarification": self.clarification.to_dict() if self.clarification else None,
            "model_choice": self.model_choice.to_dict() if self.model_choice else None,
            "preconditions": self.preconditions.to_dict() if self.preconditions else None,
            "safety": [v.to_dict() for v in self.safety],
            "rollback_depth": self.rollback_depth,
            "decisions": self.decisions,
        }


class CoreBehaviorValidator:
    """Runs a task through all five gates and returns a single verdict.

    Gate order matters. Clarify first, because there is no point classifying
    commands for a task nobody has pinned down yet.
    """

    def __init__(
        self,
        safety: Optional[SafetyFirstBehavior] = None,
        transparency: Optional[TransparencyBehavior] = None,
        verify: Optional[VerifyStateBehavior] = None,
        rollback: Optional[RollbackOnFailBehavior] = None,
        clarifier: Optional[AskClarifyingQuestionsBehavior] = None,
        selector: Optional[ModelSelector] = None,
    ) -> None:
        """Every gate is injectable. Omit one to get its default, which is
        always the conservative variant: deny confirmations, no answers.
        """
        self.safety = safety or SafetyFirstBehavior()
        self.transparency = transparency or TransparencyBehavior()
        self.verify = verify or VerifyStateBehavior()
        self.rollback = rollback or RollbackOnFailBehavior()
        self.clarifier = clarifier or AskClarifyingQuestionsBehavior()
        self.selector = selector or ModelSelector()

    def validate(self, task: str, commands: Sequence[str] = ()) -> BehaviorValidationReport:
        """Push one task plus its planned commands through every gate."""
        report = BehaviorValidationReport(task=task, passed=True)

        # Gate 5 runs first: ambiguity poisons everything downstream.
        clar = self.clarifier.clarify(task)
        report.clarification = clar
        self.transparency.log_decision(
            "clarify",
            "clarification needed" if clar.needs_clarification else "task is specific",
            f"{len(clar.ambiguities)} ambiguity marker(s) found",
            {"codes": [a.code for a in clar.ambiguities]},
        )
        if clar.needs_clarification and not clar.resolved:
            report.passed = False
            report.blocked_at = "clarify"
            report.decisions = self.transparency.audit_trail()
            return report

        effective = clar.refined_task or task
        choice = self.selector.select(effective)
        report.model_choice = choice
        self.transparency.explain_model_choice(effective, choice)

        pre = self.verify.run_all()
        report.preconditions = pre
        self.transparency.log_decision(
            "verify_state",
            "preconditions passed" if pre.passed else "preconditions failed",
            "; ".join(f"{r.name}={'ok' if r.passed else 'FAIL'}" for r in pre.results)
            or "no preconditions registered",
            {"failures": [r.name for r in pre.failures]},
        )
        if not pre.passed:
            report.passed = False
            report.blocked_at = "verify_state"
            report.decisions = self.transparency.audit_trail()
            return report

        for command in commands:
            verdict = self.safety.check(command)
            report.safety.append(verdict)
            self.transparency.log_decision(
                "safety",
                f"{verdict.level.value}: {'blocked' if verdict.blocked else 'allowed'}",
                "; ".join(verdict.reasons),
                {"command": command},
            )
            if verdict.blocked:
                report.passed = False
                report.blocked_at = "safety"
                report.decisions = self.transparency.audit_trail()
                return report

        report.rollback_depth = self.rollback.depth
        self.transparency.log_decision(
            "rollback_prep",
            f"rollback stack depth {self.rollback.depth}",
            "undo actions registered before any forward change",
        )
        report.decisions = self.transparency.audit_trail()
        return report
