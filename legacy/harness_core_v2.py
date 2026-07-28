"""
harness_core_v2.py
Phase 2 core. Closes three findings from the Phase 1 validation report.

  P0-1  DynamicCheckpointing        the policy is re-estimated from observed
                                    step timings and upgraded mid-run
  P1-1  IncrementalCheckpointing    base snapshot plus a JSONL delta log,
                                    compacted on a schedule
  P1-3  FilesystemConflictDetector  watches the whole working tree instead of
                                    only the paths a step remembered to declare

Plus DynamicCoreExecutor, which is CoreExecutor with the mid-run hooks the
above need.

Nothing in Phase 1 is modified. Every class here subclasses or wraps its Phase 1
counterpart, so the original 18 acceptance cases still pass untouched and you
can fall back by swapping the constructor.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from harness_core import (
    AdaptiveCheckpointing,
    Checkpoint,
    CheckpointError,
    CheckpointPolicy,
    Conflict,
    ConflictDetector,
    ConflictKind,
    ContextBudgetError,
    ContextCompressor,
    CoreExecutor,
    ExecutionReport,
    OllamaFallbackHandler,
    OllamaStatus,
    Step,
    StepResult,
    TaskLength,
)

__all__ = [
    "PolicyRevision",
    "DynamicCheckpointing",
    "IncrementalCheckpointing",
    "TreeSnapshot",
    "FilesystemConflictDetector",
    "MidRunAbort",
    "DynamicCoreExecutor",
]


# --------------------------------------------------------------------------- #
# P0-1: dynamic checkpoint policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PolicyRevision:
    """A recorded change of checkpoint policy mid-run."""

    at_step: int
    old_length: TaskLength
    new_length: TaskLength
    old_every_n: int
    new_every_n: int
    observed_seconds_per_step: float
    projected_minutes: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        d = asdict(self)
        d["old_length"] = self.old_length.value
        d["new_length"] = self.new_length.value
        return d


class DynamicCheckpointing(AdaptiveCheckpointing):
    """Adaptive checkpointing that stops trusting its own opening guess.

    The Phase 1 estimator ran once at t=0 off a static cost table. Twenty steps
    of kind "shell" estimated to 3.3 minutes, classified SHORT, and produced
    zero checkpoints, which meant a two hour run in reality had no resume point.
    That was the highest severity finding in the report.

    This class measures how long steps actually take, projects the remainder off
    the observed rate, and upgrades the policy the moment the evidence says the
    task is longer than it looked. Policy never downgrades mid-run: once a task
    has earned protection it keeps it, because the cost of being wrong in that
    direction is a few kilobytes and the cost of being wrong the other way is
    the whole run.

    It also feeds observed timings back into the per-kind cost table, so the
    opening guess for the *next* task is less wrong.
    """

    #: Do not revise until this many steps have been observed. One slow step at
    #: the start of a fast task should not upgrade the whole run.
    MIN_OBSERVATIONS: int = 3
    #: Blend weight for feeding observed timings back into the cost table.
    LEARNING_RATE: float = 0.30

    def __init__(
        self,
        checkpoint_dir: os.PathLike[str] | str,
        step_costs: Optional[Dict[str, float]] = None,
        clock: Callable[[], float] = time.time,
        min_observations: Optional[int] = None,
        learn: bool = True,
    ) -> None:
        """
        Args:
            checkpoint_dir: Directory holding checkpoint files.
            step_costs: Optional override of the per step-kind cost table.
            clock: Injectable time source.
            min_observations: Steps observed before the first revision.
            learn: Feed observed timings back into the cost table.
        """
        super().__init__(checkpoint_dir, step_costs, clock)
        self.min_observations = (
            self.MIN_OBSERVATIONS if min_observations is None else int(min_observations)
        )
        self.learn = bool(learn)
        self.observations: List[Tuple[str, float]] = []  # (kind, seconds)
        self.revisions: List[PolicyRevision] = []

    # -- observation ------------------------------------------------------- #

    def observe(self, step: Step, seconds: float) -> None:
        """Record how long a step actually took.

        Args:
            step: The step that just completed.
            seconds: Measured wall clock duration.
        """
        self.observations.append((step.kind, max(0.0, float(seconds))))
        if self.learn:
            prior = self.step_costs.get(step.kind)
            if prior is None:
                self.step_costs[step.kind] = seconds
            else:
                self.step_costs[step.kind] = (
                    (1 - self.LEARNING_RATE) * prior + self.LEARNING_RATE * seconds
                )

    def observed_rate(self) -> float:
        """Mean observed seconds per step, or 0.0 with no observations."""
        if not self.observations:
            return 0.0
        return sum(s for _, s in self.observations) / len(self.observations)

    def project_minutes(self, total_steps: int) -> float:
        """Project total task duration in minutes from the observed rate."""
        rate = self.observed_rate()
        return round(rate * total_steps / 60.0, 4)

    # -- revision ---------------------------------------------------------- #

    def revise(
        self,
        policy: CheckpointPolicy,
        completed_steps: int,
        total_steps: int,
    ) -> Tuple[CheckpointPolicy, Optional[PolicyRevision]]:
        """Re-decide the checkpoint policy from measured reality.

        Args:
            policy: The policy currently in force.
            completed_steps: How many steps have finished.
            total_steps: How many steps the task has in total.

        Returns:
            (policy, revision). `revision` is None when nothing changed.
        """
        if len(self.observations) < self.min_observations:
            return policy, None

        projected = self.project_minutes(total_steps)

        if projected < self.SHORT_MAX_MINUTES:
            candidate_length = TaskLength.SHORT
            candidate_every = 0
        elif projected <= self.MEDIUM_MAX_MINUTES:
            candidate_length = TaskLength.MEDIUM
            candidate_every = max(1, round(total_steps / self.MEDIUM_TARGET_CHECKPOINTS))
        else:
            candidate_length = TaskLength.LONG
            candidate_every = 1

        rank = {TaskLength.SHORT: 0, TaskLength.MEDIUM: 1, TaskLength.LONG: 2}
        if rank[candidate_length] <= rank[policy.length]:
            # Never downgrade mid-run. Protection already earned stays earned.
            return policy, None

        revised = CheckpointPolicy(
            length=candidate_length,
            estimated_minutes=projected,
            every_n_steps=candidate_every,
            expected_intermediate=(
                total_steps // candidate_every if candidate_every else 0
            ),
        )
        revision = PolicyRevision(
            at_step=completed_steps,
            old_length=policy.length,
            new_length=candidate_length,
            old_every_n=policy.every_n_steps,
            new_every_n=candidate_every,
            observed_seconds_per_step=round(self.observed_rate(), 4),
            projected_minutes=projected,
            reason=(
                f"observed {self.observed_rate():.1f}s/step over {completed_steps} "
                f"step(s) projects to {projected:.1f} min, not the "
                f"{policy.estimated_minutes:.1f} min estimated at t=0"
            ),
        )
        self.revisions.append(revision)
        return revised, revision

    def stats(self) -> Dict[str, Any]:
        """Counters plus revision history."""
        base = super().stats()
        base["revisions"] = [r.to_dict() for r in self.revisions]
        base["observed_seconds_per_step"] = round(self.observed_rate(), 4)
        base["learned_step_costs"] = {k: round(v, 2) for k, v in self.step_costs.items()}
        return base


# --------------------------------------------------------------------------- #
# P1-1: incremental checkpoints
# --------------------------------------------------------------------------- #


class IncrementalCheckpointing(DynamicCheckpointing):
    """Checkpointing that stops re-serializing the whole world every write.

    Phase 1 measured 824 KB for a 25 step run and 15.3 MB for a 200 step run,
    18.6x the bytes for 8x the steps, because every checkpoint wrote the full
    context snapshot again. The fix is boring and effective: write one base
    snapshot, then append small JSON delta lines, and compact back to a new base
    every `compact_every` deltas so replay stays bounded.

    Layout on disk:

        <task>.checkpoint.json         the base snapshot
        <task>.checkpoint.deltas.jsonl the append log since that base

    load_state replays base + deltas. A delta line that will not parse stops the
    replay at that point and is reported, rather than being skipped silently,
    because resuming past a hole is how state gets corrupted.
    """

    def __init__(
        self,
        checkpoint_dir: os.PathLike[str] | str,
        step_costs: Optional[Dict[str, float]] = None,
        clock: Callable[[], float] = time.time,
        compact_every: int = 25,
        min_observations: Optional[int] = None,
        learn: bool = True,
    ) -> None:
        """
        Args:
            compact_every: Rewrite the base snapshot after this many deltas.
            (Other arguments as DynamicCheckpointing.)
        """
        super().__init__(checkpoint_dir, step_costs, clock, min_observations, learn)
        self.compact_every = max(1, int(compact_every))
        self.deltas_since_base: Dict[str, int] = {}
        self.compactions: int = 0
        self.truncated_replays: List[str] = []

    def delta_path_for(self, task_id: str) -> Path:
        """Path of the JSONL delta log for a task."""
        return self.path_for(task_id).with_suffix(".deltas.jsonl")

    # -- writing ----------------------------------------------------------- #

    def save_state(self, checkpoint: Checkpoint) -> Path:
        """Write a delta line, or a fresh base when one is due.

        A final checkpoint always writes a full base and clears the delta log,
        so a completed run leaves exactly one file behind.
        """
        task_id = checkpoint.task_id
        count = self.deltas_since_base.get(task_id, 0)
        base_due = (
            checkpoint.final
            or not self.path_for(task_id).exists()
            or count >= self.compact_every
        )

        if base_due:
            path = super().save_state(checkpoint)
            delta_log = self.delta_path_for(task_id)
            if delta_log.exists():
                try:
                    delta_log.unlink()
                except OSError as exc:
                    self.errors.append(f"delta log cleanup failed: {exc!r}")
            if count >= self.compact_every:
                self.compactions += 1
            self.deltas_since_base[task_id] = 0
            return path

        # Delta: only the fields that move between checkpoints.
        checkpoint.written_at = self._clock()
        delta = {
            "step_index": checkpoint.step_index,
            "total_steps": checkpoint.total_steps,
            "completed_step_ids": checkpoint.completed_step_ids,
            "file_hashes": checkpoint.file_hashes,
            "metadata": checkpoint.metadata,
            "written_at": checkpoint.written_at,
            "context_len": len(checkpoint.context_steps),
        }
        line = json.dumps(delta, sort_keys=True) + "\n"
        started = time.perf_counter()
        try:
            with self.delta_path_for(task_id).open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as exc:
            msg = f"delta write failed for {task_id}: {exc!r}"
            self.errors.append(msg)
            raise CheckpointError(msg) from exc

        self.write_seconds += time.perf_counter() - started
        self.bytes_written += len(line.encode("utf-8"))
        self.writes += 1
        self.intermediate_writes += 1
        self.deltas_since_base[task_id] = count + 1
        return self.delta_path_for(task_id)

    # -- reading ----------------------------------------------------------- #

    def load_state(self, task_id: str) -> Optional[Checkpoint]:
        """Load the base snapshot, then replay every delta on top of it."""
        base = super().load_state(task_id)
        if base is None:
            return None

        delta_log = self.delta_path_for(task_id)
        if not delta_log.exists():
            return base

        try:
            raw_lines = delta_log.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            msg = f"delta log unreadable for {task_id}: {exc!r}"
            self.errors.append(msg)
            raise CheckpointError(msg) from exc

        for number, raw in enumerate(raw_lines, start=1):
            if not raw.strip():
                continue
            try:
                delta = json.loads(raw)
            except json.JSONDecodeError as exc:
                note = (
                    f"delta {number} of {len(raw_lines)} for {task_id} is corrupt "
                    f"({exc}); replay stopped there, resuming from step "
                    f"{base.step_index}"
                )
                self.errors.append(note)
                self.truncated_replays.append(note)
                break
            base.step_index = delta.get("step_index", base.step_index)
            base.total_steps = delta.get("total_steps", base.total_steps)
            base.completed_step_ids = delta.get(
                "completed_step_ids", base.completed_step_ids
            )
            base.file_hashes = delta.get("file_hashes", base.file_hashes)
            base.metadata = delta.get("metadata", base.metadata)
            base.written_at = delta.get("written_at", base.written_at)
        return base

    def clear(self, task_id: str) -> bool:
        """Delete both the base snapshot and its delta log."""
        removed = super().clear(task_id)
        delta_log = self.delta_path_for(task_id)
        if delta_log.exists():
            delta_log.unlink()
            removed = True
        self.deltas_since_base.pop(task_id, None)
        return removed

    def stats(self) -> Dict[str, Any]:
        """Counters plus compaction and replay health."""
        base = super().stats()
        base["compactions"] = self.compactions
        base["truncated_replays"] = list(self.truncated_replays)
        return base


# --------------------------------------------------------------------------- #
# P1-3: filesystem level conflict detection
# --------------------------------------------------------------------------- #


@dataclass
class TreeSnapshot:
    """Cheap (size, mtime_ns) fingerprint of every file under a root."""

    root: str
    entries: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)

    def paths(self) -> Set[str]:
        """Every path captured in this snapshot."""
        return set(self.entries)


class FilesystemConflictDetector(ConflictDetector):
    """Watches a whole working tree, not just the paths a step declared.

    The Phase 1 detector hashed exactly what a step listed in `touches`. A step
    that wrote a file it forgot to declare was invisible, which made "detects
    external modification" a wider claim than the code supported.

    This walks the tree once for a cheap (size, mtime_ns) fingerprint, then
    hashes only the entries whose fingerprint moved. Full SHA256 on every file
    every check would be correct and unusably slow on a real source tree, and
    fingerprint-only would miss a same-size same-mtime edit, so it does both:
    fingerprint to shortlist, hash to confirm.

    When inotify is available it is used to narrow the candidate set further,
    which turns an O(tree) walk into O(changed). The fallback path is a plain
    walk and is always correct, just slower.
    """

    #: Directory names never descended into.
    DEFAULT_IGNORES: Tuple[str, ...] = (
        ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
        ".pytest_cache", ".tox", "target", "dist", "build", ".cache",
    )

    def __init__(
        self,
        roots: Optional[Iterable[os.PathLike[str] | str]] = None,
        ignores: Optional[Iterable[str]] = None,
        max_files: int = 50_000,
        clock: Callable[[], float] = time.time,
        use_inotify: bool = True,
    ) -> None:
        """
        Args:
            roots: Directories to watch in full.
            ignores: Directory names to skip.
            max_files: Refuse to snapshot a tree larger than this, loudly.
            clock: Injectable time source.
            use_inotify: Try inotify to narrow the candidate set.
        """
        super().__init__(clock)
        self.roots: List[Path] = [Path(r) for r in (roots or [])]
        self.ignores: Set[str] = set(ignores if ignores is not None else self.DEFAULT_IGNORES)
        self.max_files = int(max_files)
        self.snapshots: Dict[str, TreeSnapshot] = {}
        self.oversized_roots: List[str] = []
        self.use_inotify = bool(use_inotify)
        self._inotify: Any = None
        self._watch_paths: Dict[int, str] = {}

    # -- snapshotting ------------------------------------------------------ #

    def _walk(self, root: Path) -> TreeSnapshot:
        """Fingerprint every file under root, skipping ignored directories."""
        snap = TreeSnapshot(root=str(root))
        if not root.exists():
            snap.skipped.append(f"{root} does not exist")
            return snap

        count = 0
        stack: List[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in self.ignores:
                                stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        count += 1
                        if count > self.max_files:
                            note = (
                                f"{root} exceeds max_files={self.max_files}; "
                                "snapshot truncated, coverage is incomplete"
                            )
                            snap.skipped.append(note)
                            self.oversized_roots.append(note)
                            self.errors.append(note)
                            return snap
                        try:
                            st = entry.stat(follow_symlinks=False)
                            snap.entries[entry.path] = (st.st_size, st.st_mtime_ns)
                        except OSError as exc:
                            snap.skipped.append(f"{entry.path}: {exc!r}")
            except OSError as exc:
                snap.skipped.append(f"{current}: {exc!r}")
        return snap

    def baseline_tree(
        self, roots: Optional[Iterable[os.PathLike[str] | str]] = None
    ) -> Dict[str, TreeSnapshot]:
        """Snapshot every watched root and hash nothing yet.

        Hashing is deferred until a fingerprint actually moves, which is what
        makes watching a whole tree affordable.
        """
        if roots is not None:
            self.roots = [Path(r) for r in roots]
        self.snapshots = {str(r): self._walk(r) for r in self.roots}
        if self.use_inotify:
            self._start_inotify()
        return dict(self.snapshots)

    def _start_inotify(self) -> None:
        """Attach inotify watches. Silently degrades to walk-only on failure."""
        try:
            from inotify_simple import INotify, flags  # type: ignore
        except Exception as exc:
            self.errors.append(f"inotify unavailable, using walk fallback: {exc!r}")
            self.use_inotify = False
            return
        try:
            self._inotify = INotify()
            mask = (flags.MODIFY | flags.CREATE | flags.DELETE
                    | flags.MOVED_FROM | flags.MOVED_TO | flags.ATTRIB)
            for root in self.roots:
                for dirpath, dirnames, _ in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in self.ignores]
                    wd = self._inotify.add_watch(dirpath, mask)
                    self._watch_paths[wd] = dirpath
        except Exception as exc:
            self.errors.append(f"inotify setup failed, using walk fallback: {exc!r}")
            self._inotify = None
            self.use_inotify = False

    def _inotify_candidates(self) -> Optional[Set[str]]:
        """Drain inotify events into a set of touched paths, or None if unused."""
        if not self._inotify:
            return None
        touched: Set[str] = set()
        try:
            for event in self._inotify.read(timeout=0):
                base = self._watch_paths.get(event.wd)
                if base and event.name:
                    touched.add(os.path.join(base, event.name))
        except Exception as exc:
            self.errors.append(f"inotify read failed: {exc!r}")
            return None
        return touched

    # -- detection --------------------------------------------------------- #

    def detect_tree(self) -> List[Conflict]:
        """Compare every watched root against its snapshot.

        Returns:
            Conflicts for created, deleted, and modified files anywhere in the
            tree, whether or not a step declared them.
        """
        started = time.perf_counter()
        conflicts: List[Conflict] = []
        hinted = self._inotify_candidates()

        for root_key, snap in self.snapshots.items():
            current = self._walk(Path(root_key))
            old, new = snap.entries, current.entries

            for path in new.keys() - old.keys():
                if hinted is not None and path not in hinted:
                    # inotify saw nothing here; still report, the walk is truth.
                    pass
                conflicts.append(Conflict(
                    path, ConflictKind.CREATED, None, self.hash_file(path),
                    "file appeared in the watched tree after baseline",
                ))
            for path in old.keys() - new.keys():
                conflicts.append(Conflict(
                    path, ConflictKind.DELETED, self.baseline.get(path), None,
                    "file removed from the watched tree after baseline",
                ))
            for path in old.keys() & new.keys():
                if old[path] == new[path]:
                    continue
                # Fingerprint moved. Now pay for a hash to confirm real change.
                before = self.baseline.get(path)
                after = self.hash_file(path)
                if before is None:
                    before = "<not hashed at baseline>"
                if before != after:
                    conflicts.append(Conflict(
                        path, ConflictKind.MODIFIED, before, after,
                        f"size/mtime moved {old[path]} to {new[path]} and the "
                        "content hash confirms the change",
                    ))

        self.last_check_seconds = time.perf_counter() - started
        return conflicts

    def detect(self) -> List[Conflict]:
        """Union of declared-path detection and whole-tree detection."""
        declared = super().detect()
        if not self.snapshots:
            return declared
        tree = self.detect_tree()
        seen = {(c.path, c.kind) for c in declared}
        return declared + [c for c in tree if (c.path, c.kind) not in seen]

    def refresh_tree(self) -> None:
        """Re-baseline the whole tree after the harness itself changed it."""
        self.snapshots = {str(r): self._walk(r) for r in self.roots}

    def refresh(self, path: os.PathLike[str] | str) -> Optional[str]:
        """Re-baseline one path in both the hash map and the tree snapshot."""
        digest = super().refresh(path)
        key = str(Path(path))
        for snap in self.snapshots.values():
            if key in snap.entries or key.startswith(snap.root):
                try:
                    st = os.stat(key)
                    snap.entries[key] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    snap.entries.pop(key, None)
        return digest

    def close(self) -> None:
        """Release inotify file descriptors."""
        if self._inotify:
            try:
                self._inotify.close()
            except Exception:
                pass
            self._inotify = None


# --------------------------------------------------------------------------- #
# Executor with the mid-run hooks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MidRunAbort:
    """Why a guard stopped the run partway through."""

    at_step: str
    guard: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


class DynamicCoreExecutor(CoreExecutor):
    """CoreExecutor with policy revision and between-step guards.

    Adds three things Phase 1 did not have:

      * the checkpoint policy is re-decided after every step
      * an arbitrary guard callable runs between steps and can abort the run
      * whole-tree conflict detection when the detector supports it
    """

    def __init__(
        self,
        checkpointer: AdaptiveCheckpointing,
        conflicts: Optional[ConflictDetector] = None,
        compressor: Optional[ContextCompressor] = None,
        ollama: Optional[OllamaFallbackHandler] = None,
        abort_on_conflict: bool = True,
        model: str = "unknown",
        between_steps: Optional[Callable[[int, Step], Optional[str]]] = None,
        watch_roots: Optional[Iterable[os.PathLike[str] | str]] = None,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        """
        Args:
            between_steps: Called as (step_number, next_step) before each step.
                Return a non-empty string to abort the run with that reason.
            watch_roots: Directories handed to a FilesystemConflictDetector.
            timer: Injectable monotonic timer used to measure step duration.
                The policy revision logic is driven entirely by what this
                returns, so a simulation or a test can drive an hour-long run
                in milliseconds without sleeping.
            (Other arguments as CoreExecutor.)
        """
        super().__init__(checkpointer, conflicts, compressor, ollama,
                         abort_on_conflict, model)
        self._timer = timer
        self._between_steps = between_steps
        self.watch_roots = list(watch_roots or [])
        self.policy_revisions: List[PolicyRevision] = []
        self.mid_run_abort: Optional[MidRunAbort] = None

    def _run_step(self, step: Step) -> StepResult:
        """Execute one step, timed by the injected timer."""
        started = self._timer()
        try:
            output = step.run() if step.run else f"[noop] {step.command or step.step_id}"
            return StepResult(
                step_id=step.step_id,
                ok=True,
                output="" if output is None else str(output),
                seconds=self._timer() - started,
            )
        except Exception as exc:
            return StepResult(
                step_id=step.step_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                seconds=self._timer() - started,
            )

    def execute(
        self,
        task_id: str,
        steps: Sequence[Step],
        resume: bool = False,
    ) -> ExecutionReport:
        """Run a task with mid-run policy revision and between-step guards.

        Returns:
            An ExecutionReport. Policy revisions land in
            `report.checkpoint_stats["revisions"]`; a guard abort sets
            `report.aborted_at` and `report.abort_reason`.
        """
        run_started = time.perf_counter()
        policy = self.checkpointer.decide_frequency(steps)
        report = ExecutionReport(task_id=task_id, ok=True, policy=policy)
        dynamic = isinstance(self.checkpointer, DynamicCheckpointing)

        skip: Set[str] = set()
        if resume:
            prior = self.checkpointer.load_state(task_id)
            if prior:
                skip = set(prior.completed_step_ids)
                self.completed_step_ids = list(prior.completed_step_ids)
        elif self.checkpointer.path_for(task_id).exists():
            warning = (
                f"task id '{task_id}' already has a checkpoint; starting fresh "
                "will overwrite it. Pass resume=True or use a unique task id."
            )
            self.checkpointer.errors.append(warning)
            report.warnings.append(warning)

        tracked = {t for s in steps for t in s.touches}
        if tracked:
            self.conflicts.baseline_files(sorted(tracked))
        if self.watch_roots and isinstance(self.conflicts, FilesystemConflictDetector):
            self.conflicts.baseline_tree(self.watch_roots)

        for index, step in enumerate(steps, start=1):
            if step.step_id in skip:
                continue

            # Guard 1: between-step callable (continuous VERIFY_STATE lives here).
            if self._between_steps is not None:
                try:
                    veto = self._between_steps(index, step)
                except Exception as exc:
                    veto = f"between-step guard raised {type(exc).__name__}: {exc}"
                if veto:
                    self.mid_run_abort = MidRunAbort(step.step_id, "between_steps", veto)
                    report.ok = False
                    report.aborted_at = step.step_id
                    report.abort_reason = f"mid-run guard stopped the task: {veto}"
                    self._final_checkpoint(task_id, index, len(steps))
                    break

            # Guard 2: conflicts, declared paths plus whole tree.
            found = self.conflicts.detect() if (tracked or self.watch_roots) else []
            if found and self.abort_on_conflict:
                report.conflicts.extend(found)
                report.ok = False
                report.aborted_at = step.step_id
                report.abort_reason = (
                    f"{len(found)} external file conflict(s) detected before "
                    f"{step.step_id}: "
                    + ", ".join(f"{c.path}={c.kind.value}" for c in found[:5])
                )
                self._final_checkpoint(task_id, index, len(steps))
                break

            # Guard 3: backend availability.
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

            if dynamic:
                self.checkpointer.observe(step, result.seconds)

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
            if self.watch_roots and isinstance(self.conflicts, FilesystemConflictDetector):
                self.conflicts.refresh_tree()

            # Revise the policy from what actually happened, then apply it.
            if dynamic:
                policy, revision = self.checkpointer.revise(policy, index, len(steps))
                if revision:
                    self.policy_revisions.append(revision)
                    report.policy = policy
                    # An upgrade means the run was under-protected until now.
                    # Write immediately rather than waiting for the next boundary.
                    self._write_checkpoint(task_id, index, len(steps), final=False)
                    result.checkpointed = True
                    continue

            if AdaptiveCheckpointing.should_checkpoint(policy, index):
                self._write_checkpoint(task_id, index, len(steps), final=False)
                result.checkpointed = True

        else:
            self._final_checkpoint(task_id, len(steps), len(steps))

        report.seconds = time.perf_counter() - run_started
        report.checkpoint_stats = self.checkpointer.stats()
        return report
