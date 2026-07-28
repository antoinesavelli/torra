"""
harness_core.py
Phase 1A: Core Executor for the Pop-OS Terminal Agent Harness.

Implements the four critical risk mitigations:

  1. AdaptiveCheckpointing   no overhead for short tasks, full state for long tasks
  2. ConflictDetector        SHA256 baseline, detects external file modification
  3. ContextCompressor       compresses old steps when the token budget depletes
  4. OllamaFallbackHandler   health probe plus three recovery paths

CoreExecutor wires all four together into a single step loop.

Design rules enforced here:
  * No silent failures. Every exception is caught, recorded, and surfaced.
  * Every external dependency (clock, model backend, health probe, confirm
    prompt) is injectable so the suite can test without a live Ollama.
  * Type hints on every public function.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "TaskLength",
    "CheckpointPolicy",
    "Checkpoint",
    "AdaptiveCheckpointing",
    "ConflictKind",
    "Conflict",
    "ConflictDetector",
    "ContextStep",
    "CompressionResult",
    "ContextCompressor",
    "OllamaStatus",
    "RecoveryOption",
    "RecoveryMenu",
    "OllamaFallbackHandler",
    "Step",
    "StepResult",
    "ExecutionReport",
    "CoreExecutor",
    "HarnessError",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class HarnessError(RuntimeError):
    """Base class for every non-recoverable harness fault."""


class CheckpointError(HarnessError):
    """Raised when a checkpoint cannot be written or restored."""


class ContextBudgetError(HarnessError):
    """Raised when a single step alone exceeds the whole token budget."""


# --------------------------------------------------------------------------- #
# 1. Adaptive checkpointing
# --------------------------------------------------------------------------- #


class TaskLength(str, Enum):
    """Coarse duration bucket that drives checkpoint frequency."""

    SHORT = "short"      # under 5 estimated minutes
    MEDIUM = "medium"    # 5 to 30 estimated minutes
    LONG = "long"        # over 30 estimated minutes


#: Per step-kind cost estimate in seconds. Tuned for Pop-OS shell automation.
STEP_COST_SECONDS: Dict[str, float] = {
    "noop": 0.5,
    "read": 2.0,
    "shell": 10.0,
    "write": 5.0,
    "model": 45.0,
    "install": 90.0,
    "build": 180.0,
    "deploy": 240.0,
}
DEFAULT_STEP_COST_SECONDS: float = 15.0


@dataclass(frozen=True)
class CheckpointPolicy:
    """Resolved checkpoint plan for one task.

    Attributes:
        length: The duration bucket the estimator landed on.
        estimated_minutes: Estimated wall clock duration of the whole task.
        every_n_steps: Write an intermediate checkpoint every N steps.
            0 means write no intermediate checkpoints at all.
        expected_intermediate: How many intermediate checkpoints this plan
            should produce for the given step count. Used by the bench harness.
    """

    length: TaskLength
    estimated_minutes: float
    every_n_steps: int
    expected_intermediate: int


@dataclass
class Checkpoint:
    """A resumable snapshot of executor state."""

    task_id: str
    step_index: int
    total_steps: int
    completed_step_ids: List[str] = field(default_factory=list)
    context_steps: List[Dict[str, Any]] = field(default_factory=list)
    file_hashes: Dict[str, str] = field(default_factory=dict)
    rollback_descriptions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    written_at: float = 0.0
    final: bool = False

    def to_json(self) -> str:
        """Serialize to a stable JSON string."""
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> "Checkpoint":
        """Rebuild a Checkpoint from a JSON string produced by to_json."""
        raw = json.loads(blob)
        return cls(**raw)


class AdaptiveCheckpointing:
    """Decides how often to persist state, then persists and restores it.

    The whole point is that a 30 second task pays zero checkpoint cost while a
    90 minute task can resume from the last completed step after a crash.
    """

    #: Thresholds in minutes.
    SHORT_MAX_MINUTES: float = 5.0
    MEDIUM_MAX_MINUTES: float = 30.0
    #: Target number of checkpoints across the life of a medium task.
    MEDIUM_TARGET_CHECKPOINTS: int = 5

    def __init__(
        self,
        checkpoint_dir: os.PathLike[str] | str,
        step_costs: Optional[Dict[str, float]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            checkpoint_dir: Directory that will hold `<task_id>.checkpoint.json`.
            step_costs: Optional override of the per step-kind cost table.
            clock: Injectable time source, so tests do not depend on real time.
        """
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.step_costs: Dict[str, float] = dict(step_costs or STEP_COST_SECONDS)
        self._clock = clock
        self.writes: int = 0
        self.intermediate_writes: int = 0
        self.bytes_written: int = 0
        self.write_seconds: float = 0.0
        self.errors: List[str] = []

    # -- estimation -------------------------------------------------------- #

    def estimate_duration(self, steps: Sequence["Step"]) -> float:
        """Estimate total task duration in minutes.

        Uses an explicit `estimated_seconds` on a step when present, otherwise
        falls back to the per-kind cost table, otherwise the global default.

        Args:
            steps: The ordered steps the executor is about to run.

        Returns:
            Estimated duration in minutes. Never negative.
        """
        total_seconds = 0.0
        for step in steps:
            if step.estimated_seconds is not None:
                total_seconds += max(0.0, float(step.estimated_seconds))
            else:
                total_seconds += self.step_costs.get(
                    step.kind, DEFAULT_STEP_COST_SECONDS
                )
        return round(total_seconds / 60.0, 4)

    def decide_frequency(self, steps: Sequence["Step"]) -> CheckpointPolicy:
        """Resolve the checkpoint plan for a task.

        Rules, straight from the spec:
          * short  (< 5 min)   zero intermediate checkpoints
          * medium (5 to 30)   about 5 checkpoints across the task
          * long   (> 30 min)  checkpoint after every single step

        Args:
            steps: The ordered steps the executor is about to run.

        Returns:
            A CheckpointPolicy describing frequency and expected volume.
        """
        minutes = self.estimate_duration(steps)
        n = len(steps)

        if minutes < self.SHORT_MAX_MINUTES:
            return CheckpointPolicy(TaskLength.SHORT, minutes, 0, 0)

        if minutes <= self.MEDIUM_MAX_MINUTES:
            every = max(1, round(n / self.MEDIUM_TARGET_CHECKPOINTS)) if n else 1
            expected = n // every if every else 0
            return CheckpointPolicy(TaskLength.MEDIUM, minutes, every, expected)

        return CheckpointPolicy(TaskLength.LONG, minutes, 1, n)

    @staticmethod
    def should_checkpoint(policy: CheckpointPolicy, step_number: int) -> bool:
        """Return True when an intermediate checkpoint is due.

        Args:
            policy: The plan returned by decide_frequency.
            step_number: 1-based index of the step that just completed.
        """
        if policy.every_n_steps <= 0:
            return False
        return step_number % policy.every_n_steps == 0

    # -- persistence ------------------------------------------------------- #

    def path_for(self, task_id: str) -> Path:
        """Return the on-disk checkpoint path for a task id."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in task_id)
        return self.checkpoint_dir / f"{safe}.checkpoint.json"

    def save_state(self, checkpoint: Checkpoint) -> Path:
        """Atomically write a checkpoint to disk.

        Writes to a temp file then renames, so a crash mid-write cannot leave a
        truncated checkpoint that would corrupt a later resume.

        Raises:
            CheckpointError: if the write or rename fails.
        """
        checkpoint.written_at = self._clock()
        target = self.path_for(checkpoint.task_id)
        tmp = target.with_suffix(".tmp")
        started = time.perf_counter()
        try:
            blob = checkpoint.to_json()
            tmp.write_text(blob, encoding="utf-8")
            os.replace(tmp, target)
        except Exception as exc:  # no silent failures
            msg = f"checkpoint write failed for {checkpoint.task_id}: {exc!r}"
            self.errors.append(msg)
            raise CheckpointError(msg) from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        elapsed = time.perf_counter() - started
        self.write_seconds += elapsed
        self.bytes_written += len(blob.encode("utf-8"))
        self.writes += 1
        if not checkpoint.final:
            self.intermediate_writes += 1
        return target

    def load_state(self, task_id: str) -> Optional[Checkpoint]:
        """Load the most recent checkpoint for a task.

        Returns:
            The Checkpoint, or None when no checkpoint exists.

        Raises:
            CheckpointError: when a checkpoint file exists but is unreadable
                or corrupt. A corrupt checkpoint is louder than a missing one
                on purpose, because resuming from garbage is worse than
                starting over.
        """
        path = self.path_for(task_id)
        if not path.exists():
            return None
        try:
            return Checkpoint.from_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            msg = f"checkpoint load failed for {task_id}: {exc!r}"
            self.errors.append(msg)
            raise CheckpointError(msg) from exc

    def clear(self, task_id: str) -> bool:
        """Delete a task checkpoint. Returns True when a file was removed."""
        path = self.path_for(task_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def stats(self) -> Dict[str, Any]:
        """Return counters for the benchmark harness."""
        return {
            "writes": self.writes,
            "intermediate_writes": self.intermediate_writes,
            "bytes_written": self.bytes_written,
            "write_seconds": round(self.write_seconds, 6),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# 2. Conflict detection
# --------------------------------------------------------------------------- #


class ConflictKind(str, Enum):
    """How a tracked path diverged from its baseline."""

    MODIFIED = "modified"
    DELETED = "deleted"
    CREATED = "created"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Conflict:
    """One detected divergence between baseline and current filesystem."""

    path: str
    kind: ConflictKind
    baseline_hash: Optional[str]
    current_hash: Optional[str]
    detail: str = ""


class ConflictDetector:
    """SHA256 baseline tracker that spots files changed outside the harness.

    The agent hashes every file it intends to touch before it starts. Any
    divergence at check time means a human, an editor, or another process
    moved under us, and the executor must stop rather than clobber the change.
    """

    CHUNK: int = 1 << 20  # 1 MiB

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        """Args:
            clock: injectable time source for deterministic tests.
        """
        self.baseline: Dict[str, Optional[str]] = {}
        self.errors: List[str] = []
        self.last_check_seconds: float = 0.0
        self._clock = clock

    def hash_file(self, path: os.PathLike[str] | str) -> Optional[str]:
        """Return the SHA256 hex digest of a file, or None when absent.

        Raises:
            never. Read errors are recorded in self.errors and return None,
            because an unreadable file is reported as an UNREADABLE conflict
            rather than crashing the run.
        """
        p = Path(path)
        if not p.exists():
            return None
        digest = hashlib.sha256()
        try:
            with p.open("rb") as fh:
                while chunk := fh.read(self.CHUNK):
                    digest.update(chunk)
        except OSError as exc:
            self.errors.append(f"hash failed for {p}: {exc!r}")
            return None
        return digest.hexdigest()

    def baseline_files(self, paths: Iterable[os.PathLike[str] | str]) -> Dict[str, Optional[str]]:
        """Record the current hash of every path. Returns the baseline map."""
        for path in paths:
            key = str(Path(path))
            self.baseline[key] = self.hash_file(key)
        return dict(self.baseline)

    def detect(self) -> List[Conflict]:
        """Compare every baselined path against the filesystem right now.

        Returns:
            A list of Conflict records. Empty means nothing moved.
        """
        started = time.perf_counter()
        conflicts: List[Conflict] = []
        for path, base_hash in self.baseline.items():
            p = Path(path)
            exists = p.exists()
            current = self.hash_file(path) if exists else None

            if base_hash is None and current is not None:
                conflicts.append(
                    Conflict(path, ConflictKind.CREATED, None, current,
                             "path did not exist at baseline")
                )
            elif base_hash is not None and current is None and not exists:
                conflicts.append(
                    Conflict(path, ConflictKind.DELETED, base_hash, None,
                             "path removed since baseline")
                )
            elif base_hash is not None and current is None and exists:
                conflicts.append(
                    Conflict(path, ConflictKind.UNREADABLE, base_hash, None,
                             "path exists but could not be hashed")
                )
            elif base_hash != current:
                conflicts.append(
                    Conflict(path, ConflictKind.MODIFIED, base_hash, current,
                             "content hash changed since baseline")
                )
        self.last_check_seconds = time.perf_counter() - started
        return conflicts

    def refresh(self, path: os.PathLike[str] | str) -> Optional[str]:
        """Re-baseline one path after the harness itself changed it."""
        key = str(Path(path))
        self.baseline[key] = self.hash_file(key)
        return self.baseline[key]


# --------------------------------------------------------------------------- #
# 3. Context compression
# --------------------------------------------------------------------------- #


@dataclass
class ContextStep:
    """One entry in the running context window."""

    step_id: str
    role: str
    content: str
    tokens: int = 0
    compressed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return asdict(self)


@dataclass(frozen=True)
class CompressionResult:
    """Outcome of one compression pass."""

    triggered: bool
    tokens_before: int
    tokens_after: int
    steps_before: int
    steps_after: int
    reduction_ratio: float
    summary: str = ""


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: about four characters per token.

    Deliberately does not import a tokenizer. The harness needs a fast, offline,
    dependency-free number to decide when to compress, not an exact count.
    """
    return max(1, (len(text) + 3) // 4)


class ContextCompressor:
    """Keeps the running context under a token budget.

    When usage crosses the trigger ratio, the oldest slice of history is folded
    into a single dense summary entry and the recent tail is kept verbatim.
    """

    def __init__(
        self,
        max_tokens: int = 32_000,
        trigger_ratio: float = 0.90,
        compress_fraction: float = 0.70,
        target_ratio: float = 0.45,
        keep_recent: int = 3,
        summarizer: Optional[Callable[[List[ContextStep]], str]] = None,
        token_counter: Callable[[str], int] = estimate_tokens,
    ) -> None:
        """
        Args:
            max_tokens: Hard ceiling for the context window.
            trigger_ratio: Fraction of max_tokens that triggers compression.
            compress_fraction: Minimum fraction of the oldest compressible steps
                folded into a summary.
            target_ratio: Post-compression usage target. The cut is widened past
                compress_fraction until usage lands at or under this, which is
                what keeps a single pass from freeing a trivial amount.
            keep_recent: Minimum number of trailing steps always kept verbatim.
            summarizer: Optional callable that builds the summary text. Defaults
                to a local extractive summarizer so the harness never needs a
                model call just to stay under budget.
            token_counter: Injectable token estimator.
        """
        if not 0.0 < trigger_ratio <= 1.0:
            raise ValueError("trigger_ratio must be in (0, 1]")
        if not 0.0 < compress_fraction < 1.0:
            raise ValueError("compress_fraction must be in (0, 1)")
        self.max_tokens = int(max_tokens)
        self.trigger_ratio = float(trigger_ratio)
        self.compress_fraction = float(compress_fraction)
        self.target_ratio = float(target_ratio)
        self.keep_recent = int(keep_recent)
        self.steps: List[ContextStep] = []
        self._summarizer = summarizer or self._default_summarizer
        self._count = token_counter
        self.compressions: int = 0
        self.tokens_saved: int = 0

    # -- accounting -------------------------------------------------------- #

    @property
    def total_tokens(self) -> int:
        """Current token usage across all retained steps."""
        return sum(s.tokens for s in self.steps)

    @property
    def usage_ratio(self) -> float:
        """Current usage as a fraction of max_tokens."""
        return self.total_tokens / self.max_tokens if self.max_tokens else 0.0

    def at_capacity(self) -> bool:
        """True when usage has reached the compression trigger."""
        return self.usage_ratio >= self.trigger_ratio

    def add_step(self, step_id: str, role: str, content: str) -> ContextStep:
        """Append one entry to the context window.

        Raises:
            ContextBudgetError: when a single entry alone blows the whole
                budget. Compression cannot save that case, so it is loud.
        """
        tokens = self._count(content)
        if tokens > self.max_tokens:
            raise ContextBudgetError(
                f"step {step_id} needs {tokens} tokens, budget is {self.max_tokens}"
            )
        entry = ContextStep(step_id=step_id, role=role, content=content, tokens=tokens)
        self.steps.append(entry)
        return entry

    # -- compression ------------------------------------------------------- #

    @staticmethod
    def _default_summarizer(chunk: List[ContextStep]) -> str:
        """Build a compact extractive summary of a slice of history.

        Keeps step ids, roles, and the first line of each entry. That is enough
        for the model to know what already happened without re-reading full
        command output.
        """
        lines: List[str] = [f"[COMPRESSED HISTORY: {len(chunk)} steps]"]
        for s in chunk:
            head = s.content.strip().splitlines()
            first = head[0][:80] if head else ""
            lines.append(f"- {s.step_id} ({s.role}): {first}")
        return "\n".join(lines)

    def compress(self, force: bool = False) -> CompressionResult:
        """Fold the oldest history into a summary when over the trigger.

        Args:
            force: Compress even when under the trigger ratio.

        Returns:
            A CompressionResult. `triggered` is False when nothing was done.
        """
        tokens_before = self.total_tokens
        steps_before = len(self.steps)

        if not force and not self.at_capacity():
            return CompressionResult(False, tokens_before, tokens_before,
                                     steps_before, steps_before, 0.0)

        compressible = max(0, steps_before - self.keep_recent)
        cut = int(compressible * self.compress_fraction)
        if cut < 1:
            cut = compressible  # tiny histories: fold everything foldable
        if cut < 1:
            return CompressionResult(False, tokens_before, tokens_before,
                                     steps_before, steps_before, 0.0)

        # Widen the cut until projected usage lands under the target. Without
        # this a pass can free almost nothing when the tail is the fat part.
        target_tokens = int(self.max_tokens * self.target_ratio)
        while cut < compressible:
            projected = (
                sum(s.tokens for s in self.steps[cut:])
                + self._count(self._summarizer(self.steps[:cut]))
            )
            if projected <= target_tokens:
                break
            cut += 1

        old, tail = self.steps[:cut], self.steps[cut:]
        summary_text = self._summarizer(old)

        # Guarantee the pass actually frees space. With many tiny steps the
        # per-step summary line costs more than the content it replaces, which
        # would make compression a net loss. Degrade to terser summaries until
        # the summary is at most half the size of what it replaces.
        old_tokens = sum(s.tokens for s in old)
        if self._count(summary_text) > old_tokens // 2:
            summary_text = (
                f"[COMPRESSED HISTORY: {len(old)} steps] "
                + ", ".join(s.step_id for s in old)
            )
        if self._count(summary_text) > old_tokens // 2:
            summary_text = (
                f"[COMPRESSED HISTORY: {len(old)} steps elided, "
                f"{old[0].step_id} through {old[-1].step_id}]"
            )
        if self._count(summary_text) >= old_tokens:
            # Nothing can be gained here. Say so rather than making it worse.
            return CompressionResult(False, tokens_before, tokens_before,
                                     steps_before, steps_before, 0.0,
                                     "compression skipped: summary would exceed source")

        summary = ContextStep(
            step_id=f"summary::{old[0].step_id}..{old[-1].step_id}",
            role="system",
            content=summary_text,
            tokens=self._count(summary_text),
            compressed=True,
        )
        self.steps = [summary] + tail

        tokens_after = self.total_tokens
        ratio = 0.0 if tokens_before == 0 else (tokens_before - tokens_after) / tokens_before
        self.compressions += 1
        self.tokens_saved += max(0, tokens_before - tokens_after)
        return CompressionResult(
            triggered=True,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            steps_before=steps_before,
            steps_after=len(self.steps),
            reduction_ratio=round(ratio, 4),
            summary=summary_text,
        )

    def maybe_compress(self) -> CompressionResult:
        """Compress only if the trigger has been crossed."""
        return self.compress(force=False)

    def snapshot(self) -> List[Dict[str, Any]]:
        """Serializable view of the context, for checkpoints."""
        return [s.to_dict() for s in self.steps]


# --------------------------------------------------------------------------- #
# 4. Ollama fallback
# --------------------------------------------------------------------------- #


class OllamaStatus(str, Enum):
    """Result of a health probe."""

    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecoveryOption:
    """One path out of an Ollama outage."""

    key: str
    title: str
    detail: str
    action: str
    destructive: bool = False


@dataclass(frozen=True)
class RecoveryMenu:
    """The menu shown to the operator when the backend dies mid-task."""

    status: OllamaStatus
    reason: str
    options: List[RecoveryOption]
    built_in_seconds: float
    checkpoint_path: Optional[str] = None

    def render(self) -> str:
        """Human readable menu for the terminal."""
        lines = [
            f"Ollama status: {self.status.value}",
            f"Reason: {self.reason}",
        ]
        if self.checkpoint_path:
            lines.append(f"State saved at: {self.checkpoint_path}")
        lines.append("Recovery options:")
        for i, opt in enumerate(self.options, 1):
            flag = " [DESTRUCTIVE]" if opt.destructive else ""
            lines.append(f"  {i}. {opt.title}{flag}")
            lines.append(f"     {opt.detail}")
            lines.append(f"     run: {opt.action}")
        return "\n".join(lines)


class OllamaFallbackHandler:
    """Health probing plus graceful degradation for the local Ollama daemon.

    Three recovery paths, always offered in this order:
      1. restart the daemon and resume from the last checkpoint
      2. fall back to a smaller local model that fits in remaining VRAM
      3. degrade to dry-run mode, emitting the plan for manual execution
    """

    DEFAULT_HOST: str = "http://localhost:11434"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        timeout: float = 0.5,
        probe: Optional[Callable[[], bool]] = None,
        fallback_model: str = "qwen3:8b",
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            host: Ollama base URL.
            timeout: Probe timeout in seconds. Kept small so the recovery menu
                can be built well inside the 2 second budget.
            probe: Injectable health probe returning True when healthy. When
                omitted, an HTTP GET against /api/tags is used.
            fallback_model: Smaller model used by recovery path 2.
            clock: Injectable time source.
        """
        self.host = host.rstrip("/")
        self.timeout = float(timeout)
        self.fallback_model = fallback_model
        self._probe = probe or self._http_probe
        self._clock = clock
        self.last_status: OllamaStatus = OllamaStatus.UNKNOWN
        self.last_error: str = ""
        self.consecutive_failures: int = 0
        self.health_history: List[Tuple[float, OllamaStatus]] = []
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._lock = threading.Lock()

    # -- probing ----------------------------------------------------------- #

    def _http_probe(self) -> bool:
        """Default probe. GET /api/tags with a short timeout."""
        import urllib.error
        import urllib.request

        try:
            req = urllib.request.Request(f"{self.host}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def health_check(self) -> OllamaStatus:
        """Probe the daemon once and record the result.

        Returns:
            HEALTHY, DEGRADED (intermittent), or UNREACHABLE.
        """
        try:
            ok = bool(self._probe())
        except Exception as exc:
            self.last_error = f"probe raised {type(exc).__name__}: {exc}"
            ok = False

        with self._lock:
            if ok:
                self.consecutive_failures = 0
                status = OllamaStatus.HEALTHY
            else:
                self.consecutive_failures += 1
                status = (
                    OllamaStatus.DEGRADED
                    if self.consecutive_failures == 1
                    else OllamaStatus.UNREACHABLE
                )
            self.last_status = status
            self.health_history.append((self._clock(), status))
        return status

    # -- recovery ---------------------------------------------------------- #

    def build_recovery_menu(
        self,
        reason: str,
        checkpoint_path: Optional[str] = None,
        current_model: str = "unknown",
    ) -> RecoveryMenu:
        """Build the three-path recovery menu. Never touches the network."""
        started = time.perf_counter()
        options = [
            RecoveryOption(
                key="restart",
                title="Restart Ollama and resume from checkpoint",
                detail=(
                    "Bounces the systemd unit, waits for the health probe, then "
                    "replays from the last saved step. No work is lost."
                ),
                action="systemctl --user restart ollama && harness resume",
            ),
            RecoveryOption(
                key="fallback_model",
                title=f"Fall back to {self.fallback_model}",
                detail=(
                    f"{current_model} may have exhausted VRAM. The smaller model "
                    "keeps the task moving with reduced reasoning depth."
                ),
                action=f"harness resume --model {self.fallback_model}",
            ),
            RecoveryOption(
                key="dry_run",
                title="Degrade to dry-run and hand off",
                detail=(
                    "Stops calling the model, prints the remaining plan as shell "
                    "commands for manual execution, keeps the rollback stack intact."
                ),
                action="harness resume --dry-run",
            ),
        ]
        elapsed = time.perf_counter() - started
        return RecoveryMenu(
            status=self.last_status,
            reason=reason,
            options=options,
            built_in_seconds=elapsed,
            checkpoint_path=checkpoint_path,
        )

    def handle_connection_loss(
        self,
        reason: str = "connection to Ollama lost",
        checkpoint_path: Optional[str] = None,
        current_model: str = "unknown",
    ) -> RecoveryMenu:
        """Confirm the outage with one probe, then return the recovery menu.

        The whole call is bounded by `timeout` plus menu construction, which is
        why the default timeout is 0.5 seconds against a 2 second budget.
        """
        self.health_check()
        return self.build_recovery_menu(reason, checkpoint_path, current_model)

    # -- background monitoring --------------------------------------------- #

    def start_monitor(self, interval: float = 5.0) -> threading.Thread:
        """Start non-blocking background health polling.

        Returns immediately. The caller keeps executing while the daemon thread
        polls. Calling twice is a no-op that returns the existing thread.
        """
        if self._monitor_thread and self._monitor_thread.is_alive():
            return self._monitor_thread

        self._monitor_stop.clear()

        def _loop() -> None:
            while not self._monitor_stop.is_set():
                try:
                    self.health_check()
                except Exception:  # a monitor must never kill the process
                    with self._lock:
                        self.last_error = traceback.format_exc()
                self._monitor_stop.wait(interval)

        thread = threading.Thread(target=_loop, name="ollama-health", daemon=True)
        self._monitor_thread = thread
        thread.start()
        return thread

    def stop_monitor(self, join_timeout: float = 2.0) -> None:
        """Signal the monitor to stop and join it."""
        self._monitor_stop.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=join_timeout)
        self._monitor_thread = None

    def is_monitoring(self) -> bool:
        """True when the background monitor thread is alive."""
        return bool(self._monitor_thread and self._monitor_thread.is_alive())


# --------------------------------------------------------------------------- #
# 5. Core executor
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """One unit of work in a task."""

    step_id: str
    kind: str = "shell"
    command: str = ""
    description: str = ""
    estimated_seconds: Optional[float] = None
    touches: List[str] = field(default_factory=list)
    run: Optional[Callable[[], Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "command": self.command,
            "description": self.description,
            "estimated_seconds": self.estimated_seconds,
            "touches": list(self.touches),
        }


@dataclass
class StepResult:
    """Outcome of a single step."""

    step_id: str
    ok: bool
    output: str = ""
    error: str = ""
    seconds: float = 0.0
    checkpointed: bool = False
    conflicts: List[Conflict] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        d = asdict(self)
        d["conflicts"] = [asdict(c) for c in self.conflicts]
        return d


@dataclass
class ExecutionReport:
    """Everything the executor learned during one run."""

    task_id: str
    ok: bool
    policy: Optional[CheckpointPolicy]
    results: List[StepResult] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    compressions: List[CompressionResult] = field(default_factory=list)
    recovery_menu: Optional[RecoveryMenu] = None
    aborted_at: Optional[str] = None
    abort_reason: str = ""
    warnings: List[str] = field(default_factory=list)
    seconds: float = 0.0
    checkpoint_stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {
            "task_id": self.task_id,
            "ok": self.ok,
            "policy": asdict(self.policy) if self.policy else None,
            "results": [r.to_dict() for r in self.results],
            "conflicts": [asdict(c) for c in self.conflicts],
            "compressions": [asdict(c) for c in self.compressions],
            "recovery_menu": (
                {
                    "status": self.recovery_menu.status.value,
                    "reason": self.recovery_menu.reason,
                    "options": [asdict(o) for o in self.recovery_menu.options],
                }
                if self.recovery_menu
                else None
            ),
            "aborted_at": self.aborted_at,
            "abort_reason": self.abort_reason,
            "warnings": list(self.warnings),
            "seconds": round(self.seconds, 6),
            "checkpoint_stats": self.checkpoint_stats,
        }


class CoreExecutor:
    """Main orchestrator for Phase 1A.

    Runs an ordered list of Steps while enforcing the four risk mitigations.
    Aborts on the first external file conflict, on backend loss, or on a step
    failure, and always leaves a resumable checkpoint behind when the policy
    calls for one.
    """

    def __init__(
        self,
        checkpointer: AdaptiveCheckpointing,
        conflicts: Optional[ConflictDetector] = None,
        compressor: Optional[ContextCompressor] = None,
        ollama: Optional[OllamaFallbackHandler] = None,
        abort_on_conflict: bool = True,
        model: str = "unknown",
    ) -> None:
        """Args: abort_on_conflict: stop the run on the first external file
        change. model: name recorded in checkpoints and recovery menus.
        """
        self.checkpointer = checkpointer
        self.conflicts = conflicts or ConflictDetector()
        self.compressor = compressor or ContextCompressor()
        self.ollama = ollama or OllamaFallbackHandler()
        self.abort_on_conflict = abort_on_conflict
        self.model = model
        self.completed_step_ids: List[str] = []

    def _run_step(self, step: Step) -> StepResult:
        """Execute one step, catching every exception."""
        started = time.perf_counter()
        try:
            output = step.run() if step.run else f"[noop] {step.command or step.step_id}"
            return StepResult(
                step_id=step.step_id,
                ok=True,
                output="" if output is None else str(output),
                seconds=time.perf_counter() - started,
            )
        except Exception as exc:
            return StepResult(
                step_id=step.step_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                seconds=time.perf_counter() - started,
            )

    def execute(
        self,
        task_id: str,
        steps: Sequence[Step],
        resume: bool = False,
    ) -> ExecutionReport:
        """Run a task end to end.

        Args:
            task_id: Stable identifier used for checkpoint filenames.
            steps: Ordered steps to run.
            resume: When True, skip steps already recorded in a checkpoint.

        Returns:
            An ExecutionReport. Never raises for step-level faults; a fault
            sets ok=False and populates abort_reason.
        """
        run_started = time.perf_counter()
        policy = self.checkpointer.decide_frequency(steps)
        report = ExecutionReport(task_id=task_id, ok=True, policy=policy)

        skip: set[str] = set()
        if resume:
            prior = self.checkpointer.load_state(task_id)
            if prior:
                skip = set(prior.completed_step_ids)
                self.completed_step_ids = list(prior.completed_step_ids)
        elif self.checkpointer.path_for(task_id).exists():
            # A fresh run over an existing task id will clobber that run's only
            # resume point. Loud, because silently losing it is how state gets
            # corrupted.
            warning = (
                f"task id '{task_id}' already has a checkpoint at "
                f"{self.checkpointer.path_for(task_id)}; starting fresh will "
                "overwrite it. Pass resume=True or use a unique task id."
            )
            self.checkpointer.errors.append(warning)
            report.warnings.append(warning)

        tracked = {t for s in steps for t in s.touches}
        if tracked:
            self.conflicts.baseline_files(sorted(tracked))

        for index, step in enumerate(steps, start=1):
            if step.step_id in skip:
                continue

            found = self.conflicts.detect() if tracked else []
            if found and self.abort_on_conflict:
                report.conflicts.extend(found)
                report.ok = False
                report.aborted_at = step.step_id
                report.abort_reason = (
                    f"{len(found)} external file conflict(s) detected before "
                    f"{step.step_id}: "
                    + ", ".join(f"{c.path}={c.kind.value}" for c in found)
                )
                self._final_checkpoint(task_id, index, len(steps))
                break

            if self.ollama.last_status is OllamaStatus.UNREACHABLE:
                cp = self._final_checkpoint(task_id, index, len(steps))
                report.recovery_menu = self.ollama.build_recovery_menu(
                    "backend unreachable before step " + step.step_id,
                    checkpoint_path=str(cp) if cp else None,
                    current_model=self.model,
                )
                report.ok = False
                report.aborted_at = step.step_id
                report.abort_reason = "ollama unreachable"
                break

            result = self._run_step(step)
            report.results.append(result)

            try:
                self.compressor.add_step(
                    step.step_id, "assistant", f"{step.description}\n{result.output}"
                )
                squeeze = self.compressor.maybe_compress()
                if squeeze.triggered:
                    report.compressions.append(squeeze)
            except ContextBudgetError as exc:
                report.ok = False
                report.aborted_at = step.step_id
                report.abort_reason = f"context budget exceeded: {exc}"
                self._final_checkpoint(task_id, index, len(steps))
                break

            if not result.ok:
                report.ok = False
                report.aborted_at = step.step_id
                report.abort_reason = f"step failed: {result.error}"
                self._final_checkpoint(task_id, index, len(steps))
                break

            self.completed_step_ids.append(step.step_id)
            for touched in step.touches:
                self.conflicts.refresh(touched)

            if AdaptiveCheckpointing.should_checkpoint(policy, index):
                self._write_checkpoint(task_id, index, len(steps), final=False)
                result.checkpointed = True

        else:
            self._final_checkpoint(task_id, len(steps), len(steps))

        report.seconds = time.perf_counter() - run_started
        report.checkpoint_stats = self.checkpointer.stats()
        return report

    def _write_checkpoint(
        self, task_id: str, index: int, total: int, final: bool
    ) -> Optional[Path]:
        """Persist state. Records the error rather than crashing the run."""
        cp = Checkpoint(
            task_id=task_id,
            step_index=index,
            total_steps=total,
            completed_step_ids=list(self.completed_step_ids),
            context_steps=self.compressor.snapshot(),
            file_hashes={k: v for k, v in self.conflicts.baseline.items() if v},
            metadata={"model": self.model},
            final=final,
        )
        try:
            return self.checkpointer.save_state(cp)
        except CheckpointError:
            return None

    def _final_checkpoint(self, task_id: str, index: int, total: int) -> Optional[Path]:
        """Write the terminal checkpoint. Always safe to call."""
        return self._write_checkpoint(task_id, index, total, final=True)
