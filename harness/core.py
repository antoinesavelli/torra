"""
harness.core
Execution primitives and the Ollama client.

This module is the consolidation of what used to be harness_core.py plus its v2,
v3 and v4 subclass generations. Those existed because each phase promised not to
modify the one before it, which was right for incremental validation and wrong
for a codebase anyone has to read. Only the final behaviour survives here.

Deliberately dropped in consolidation:

  * the three-tier adaptive checkpoint policy and its mid-run revision logic.
    It existed so an unattended two hour run could resume. An interactive agent
    turn takes forty seconds. A single resume file replaces about 300 lines.
  * the declared-paths-only ConflictDetector. The event-driven whole-tree
    detector supersedes it and keeps the SHA256 layer as its confirm step.
  * the Ollama health handler that only ever health-checked. It now generates,
    which is the entire point of the rewrite.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "HarnessError", "OllamaError", "ContextBudgetError",
    "Step", "StepResult", "TaskSpec", "TaskSpecError",
    "ResumePoint", "ConflictKind", "Conflict", "ConflictDetector",
    "ContextStep", "CompressionResult", "ContextCompressor",
    "OllamaStatus", "RecoveryOption", "OllamaClient",
    "Accelerator", "detect_accelerator", "MachineState", "observe_machine",
]


class HarnessError(RuntimeError):
    """Base class for every non-recoverable harness fault."""


class OllamaError(HarnessError):
    """Raised when the model backend cannot be reached or returns nonsense."""


class ContextBudgetError(HarnessError):
    """Raised when a single entry alone exceeds the whole token budget."""


class TaskSpecError(ValueError):
    """Raised when a proposed task is malformed. Loud on purpose."""


# --------------------------------------------------------------------------- #
# Steps and task specs
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """One unit of work.

    `command` is both what executes and what the safety gate classifies. Keeping
    those identical is the single most important invariant in the harness: a
    step whose executable payload differs from its classified text would slip
    past every gate without any of them being wrong.
    """

    step_id: str
    command: str = ""
    kind: str = "shell"
    description: str = ""
    estimated_seconds: Optional[float] = None
    touches: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    timeout: Optional[float] = None
    run: Optional[Callable[[], Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, excluding the callable."""
        return {k: v for k, v in asdict(self).items() if k != "run"}


@dataclass
class StepResult:
    """Outcome of a single step."""

    step_id: str
    ok: bool
    output: str = ""
    error: str = ""
    seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


#: Fields a step may declare. Anything else is a typo, and typos in a document
#: that drives root shell commands are errors rather than shrugs.
STEP_FIELDS: Set[str] = {
    "id", "kind", "run", "description", "estimated_seconds", "touches",
    "cwd", "timeout",
}


@dataclass
class TaskSpec:
    """A task as data: what an operator writes, and what the agent proposes.

    The agent producing the same structure a human writes by hand is not a
    convenience. It means the model's output travels the exact code path that
    121 tests already cover, instead of a parallel one nobody validated.
    """

    task_id: str
    request: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    watch_roots: List[str] = field(default_factory=list)
    rollback: List[Dict[str, Any]] = field(default_factory=list)
    source: str = ""
    notes: str = ""

    # -- construction ------------------------------------------------------ #

    @classmethod
    def validate(cls, data: Dict[str, Any], source: str = "") -> "TaskSpec":
        """Build a TaskSpec from a mapping, rejecting anything malformed.

        Raises:
            TaskSpecError: missing key, empty steps, unknown step field,
                duplicate step id, or a step with no command.
        """
        if not isinstance(data, dict):
            raise TaskSpecError(f"{source or 'task'}: top level must be a mapping")
        for key in ("task_id", "request", "steps"):
            if key not in data:
                raise TaskSpecError(f"{source or 'task'}: missing required key '{key}'")
        if not isinstance(data["steps"], list) or not data["steps"]:
            raise TaskSpecError(f"{source or 'task'}: 'steps' must be a non-empty list")

        seen: Set[str] = set()
        for index, raw in enumerate(data["steps"], start=1):
            if not isinstance(raw, dict):
                raise TaskSpecError(f"{source or 'task'}: step {index} must be a mapping")
            unknown = set(raw) - STEP_FIELDS
            if unknown:
                raise TaskSpecError(
                    f"{source or 'task'}: step {index} has unknown field(s) "
                    f"{sorted(unknown)}; known fields are {sorted(STEP_FIELDS)}")
            if not raw.get("run"):
                raise TaskSpecError(f"{source or 'task'}: step {index} has no 'run' command")
            step_id = str(raw.get("id") or f"step{index}")
            if step_id in seen:
                raise TaskSpecError(f"{source or 'task'}: duplicate step id '{step_id}'")
            seen.add(step_id)

        return cls(
            task_id=str(data["task_id"]),
            request=str(data["request"]),
            steps=list(data["steps"]),
            watch_roots=[str(r) for r in (data.get("watch_roots") or [])],
            rollback=list(data.get("rollback") or []),
            source=source,
            notes=str(data.get("notes") or ""),
        )

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> "TaskSpec":
        """Read a task file from disk."""
        import yaml

        target = Path(path)
        if not target.exists():
            raise TaskSpecError(f"task file not found: {target}")
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise TaskSpecError(f"{target}: unreadable YAML: {exc}") from exc
        return cls.validate(data, source=str(target))

    # -- use --------------------------------------------------------------- #

    def commands(self) -> List[str]:
        """Every shell command this task will run, in order."""
        return [str(raw["run"]) for raw in self.steps]

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    def to_yaml(self) -> str:
        """Render as a task file an operator could save and re-run."""
        import yaml

        payload: Dict[str, Any] = {"task_id": self.task_id, "request": self.request}
        if self.watch_roots:
            payload["watch_roots"] = self.watch_roots
        payload["steps"] = self.steps
        if self.rollback:
            payload["rollback"] = self.rollback
        return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)

    def to_steps(
        self,
        dry_run: bool = False,
        runner: Optional[Callable[[str, Optional[str], Optional[float]], str]] = None,
    ) -> List[Step]:
        """Build executable Steps whose command is byte for byte the `run` text."""
        execute = runner or shell

        def build(raw: Dict[str, Any], index: int) -> Step:
            """Turn one step mapping into an executable Step."""
            command = str(raw["run"])
            cwd = raw.get("cwd")
            timeout = raw.get("timeout")

            def action() -> str:
                """Run this step's command, or describe it under dry run."""
                if dry_run:
                    return f"[dry-run] {command}"
                return execute(command, cwd, timeout)

            return Step(
                step_id=str(raw.get("id") or f"step{index}"),
                command=command,
                kind=str(raw.get("kind") or "shell"),
                description=str(raw.get("description") or command),
                estimated_seconds=(float(raw["estimated_seconds"])
                                   if raw.get("estimated_seconds") is not None else None),
                touches=[str(t) for t in (raw.get("touches") or [])],
                cwd=cwd,
                timeout=timeout,
                run=action,
            )

        return [build(raw, i) for i, raw in enumerate(self.steps, start=1)]

    def rollback_actions(
        self, runner: Optional[Callable[[str, Optional[str], Optional[float]], str]] = None
    ) -> List[Tuple[str, Callable[[], Any], bool]]:
        """Undo actions declared in the task, for what cannot be derived."""
        execute = runner or shell
        out: List[Tuple[str, Callable[[], Any], bool]] = []
        for raw in self.rollback:
            if not isinstance(raw, dict) or not raw.get("run"):
                continue
            command = str(raw["run"])
            out.append((
                str(raw.get("description") or f"undo: {command}"),
                lambda c=command: execute(c, None, None),
                bool(raw.get("safe", True)),
            ))
        return out


def shell(command: str, cwd: Optional[str] = None,
          timeout: Optional[float] = None) -> str:
    """Run one shell command, raising on a non-zero exit.

    Raising is deliberate. The executor treats a raising step as failed, which
    is what arms rollback. A step that swallowed a non-zero exit would report
    success and leave the system half changed.
    """
    import subprocess

    result = subprocess.run(command, shell=True, capture_output=True, text=True,
                            cwd=cwd, timeout=timeout or 3600)
    if result.returncode != 0:
        raise RuntimeError(
            f"exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:400]}")
    return (result.stdout or "").strip()


# --------------------------------------------------------------------------- #
# Resume points
# --------------------------------------------------------------------------- #


@dataclass
class ResumePoint:
    """Where a task got to, so a crash does not mean starting over.

    This replaces the three-tier adaptive checkpointing policy from phases 1 and
    2, along with its mid-run revision logic and JSONL delta compaction, roughly
    300 lines in total. That machinery bought resumability for unattended
    multi-hour runs. An agent turn is interactive and takes seconds, so one
    atomically written file after each completed step is the whole requirement.
    """

    task_id: str
    completed: List[str] = field(default_factory=list)
    total: int = 0
    file_hashes: Dict[str, str] = field(default_factory=dict)
    at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> Optional["ResumePoint"]:
        """Read a resume point, or None when absent.

        Raises:
            HarnessError: when the file exists but is corrupt. Resuming from
                garbage is worse than starting over, so this is loud.
        """
        target = Path(path)
        if not target.exists():
            return None
        try:
            return cls(**json.loads(target.read_text(encoding="utf-8")))
        except Exception as exc:
            raise HarnessError(f"corrupt resume point {target}: {exc}") from exc

    def save(self, path: os.PathLike[str] | str) -> Path:
        """Write atomically: temp file then rename, so a crash cannot truncate."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.at = time.time()
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            raise HarnessError(f"resume point write failed: {exc!r}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return target


# --------------------------------------------------------------------------- #
# Conflict detection
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
    baseline_hash: Optional[str] = None
    current_hash: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


class ConflictDetector:
    """Two-layer detection: cheap fingerprints to shortlist, SHA256 to confirm.

    Hashing every file on every check is correct and unusably slow on a real
    tree. Fingerprints alone miss a same-size same-mtime edit. So this does
    both, and uses inotify to narrow the candidate set to what the kernel says
    actually moved, with a periodic full walk to catch inotify's three silent
    failure modes: queue overflow, directories created after watches attached,
    and moves in from outside.
    """

    DEFAULT_IGNORES: Tuple[str, ...] = (
        ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
        ".pytest_cache", ".tox", "target", "dist", "build", ".cache",
    )

    def __init__(
        self,
        roots: Optional[Iterable[os.PathLike[str] | str]] = None,
        ignores: Optional[Iterable[str]] = None,
        max_files: int = 50_000,
        reconcile_every: int = 20,
        use_inotify: bool = True,
    ) -> None:
        """
        Args:
            roots: Directories watched in full.
            ignores: Directory names never descended into.
            max_files: Refuse to snapshot a larger tree, loudly.
            reconcile_every: Fast event checks between full walks.
            use_inotify: Try the event fast path.
        """
        self.roots: List[Path] = [Path(r) for r in (roots or [])]
        self.ignores: Set[str] = set(ignores if ignores is not None
                                     else self.DEFAULT_IGNORES)
        self.max_files = int(max_files)
        self.reconcile_every = max(1, int(reconcile_every))
        self.use_inotify = bool(use_inotify)

        self.hashes: Dict[str, Optional[str]] = {}
        self.fingerprints: Dict[str, Tuple[int, int]] = {}
        self.errors: List[str] = []
        self.fast_checks: int = 0
        self.full_walks: int = 0
        self._since_walk: int = 0
        self._inotify: Any = None
        self._watch_paths: Dict[int, str] = {}
        self._known: Dict[str, Conflict] = {}

    # -- hashing ----------------------------------------------------------- #

    def hash_file(self, path: os.PathLike[str] | str) -> Optional[str]:
        """SHA256 of a file, or None when absent or unreadable."""
        p = Path(path)
        if not p.exists():
            return None
        digest = hashlib.sha256()
        try:
            with p.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    digest.update(chunk)
        except OSError as exc:
            self.errors.append(f"hash failed for {p}: {exc!r}")
            return None
        return digest.hexdigest()

    def baseline(
        self,
        paths: Iterable[os.PathLike[str] | str] = (),
        roots: Optional[Iterable[os.PathLike[str] | str]] = None,
    ) -> None:
        """Record hashes for declared paths and fingerprints for watched trees."""
        for path in paths:
            key = str(Path(path))
            self.hashes[key] = self.hash_file(key)
        if roots is not None:
            self.roots = [Path(r) for r in roots]
        self.fingerprints = {}
        for root in self.roots:
            self.fingerprints.update(self._walk(root))
        self._since_walk = 0
        self._known.clear()
        if self.use_inotify and self.roots:
            self._start_inotify()

    def _walk(self, root: Path) -> Dict[str, Tuple[int, int]]:
        """Fingerprint every file under root, skipping ignored directories."""
        out: Dict[str, Tuple[int, int]] = {}
        if not root.exists():
            return out
        count = 0
        stack = [root]
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
                            note = (f"{root} exceeds max_files={self.max_files}; "
                                    "coverage is incomplete")
                            self.errors.append(note)
                            return out
                        try:
                            st = entry.stat(follow_symlinks=False)
                            out[entry.path] = (st.st_size, st.st_mtime_ns)
                        except OSError:
                            pass
            except OSError as exc:
                self.errors.append(f"{current}: {exc!r}")
        return out

    # -- inotify ----------------------------------------------------------- #

    def _start_inotify(self) -> None:
        """Attach watches. Degrades to walk-only and says so on failure."""
        try:
            from inotify_simple import INotify, flags  # type: ignore
        except Exception as exc:
            self.errors.append(f"inotify unavailable, walking instead: {exc!r}")
            self.use_inotify = False
            return
        try:
            self._inotify = INotify()
            mask = (flags.MODIFY | flags.CREATE | flags.DELETE
                    | flags.MOVED_FROM | flags.MOVED_TO | flags.ATTRIB)
            for root in self.roots:
                for dirpath, dirnames, _ in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in self.ignores]
                    self._watch_paths[self._inotify.add_watch(dirpath, mask)] = dirpath
        except Exception as exc:
            self.errors.append(f"inotify setup failed, walking instead: {exc!r}")
            self._inotify = None
            self.use_inotify = False

    def _events(self) -> Optional[Set[str]]:
        """Drain the event queue into touched paths, or None when unused."""
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

    def detect(self) -> List[Conflict]:
        """Every divergence from baseline, declared paths and watched trees."""
        found: List[Conflict] = list(self._detect_declared())
        if self.roots:
            seen = {(c.path, c.kind) for c in found}
            found += [c for c in self._detect_tree() if (c.path, c.kind) not in seen]
        return found

    def _detect_declared(self) -> List[Conflict]:
        """Re-hash every explicitly declared path."""
        out: List[Conflict] = []
        for path, before in self.hashes.items():
            p = Path(path)
            exists = p.exists()
            after = self.hash_file(path) if exists else None
            if before is None and after is not None:
                out.append(Conflict(path, ConflictKind.CREATED, None, after,
                                    "did not exist at baseline"))
            elif before is not None and not exists:
                out.append(Conflict(path, ConflictKind.DELETED, before, None,
                                    "removed since baseline"))
            elif before is not None and after is None and exists:
                out.append(Conflict(path, ConflictKind.UNREADABLE, before, None,
                                    "exists but could not be hashed"))
            elif before != after:
                out.append(Conflict(path, ConflictKind.MODIFIED, before, after,
                                    "content hash changed"))
        return out

    def _detect_tree(self) -> List[Conflict]:
        """Event fast path, with a full walk on schedule."""
        if not self.use_inotify or not self._inotify \
                or self._since_walk >= self.reconcile_every:
            return self._reconcile()

        touched = self._events() or set()
        self.fast_checks += 1
        self._since_walk += 1
        for path in touched:
            conflict = self._classify(path)
            if conflict:
                self._known[conflict.path] = conflict
        return list(self._known.values())

    def _classify(self, path: str) -> Optional[Conflict]:
        """Turn one event path into a Conflict, or None when it is not one."""
        target = Path(path)
        known = path in self.fingerprints
        exists = target.exists()
        if not known and exists:
            return Conflict(path, ConflictKind.CREATED, None, self.hash_file(path),
                            "inotify reported a new file")
        if known and not exists:
            return Conflict(path, ConflictKind.DELETED, self.hashes.get(path), None,
                            "inotify reported a deletion")
        if not known:
            return None
        try:
            st = target.stat()
        except OSError as exc:
            return Conflict(path, ConflictKind.UNREADABLE, None, None, repr(exc))
        if self.fingerprints[path] == (st.st_size, st.st_mtime_ns):
            return None
        before = self.hashes.get(path, "<not hashed at baseline>")
        after = self.hash_file(path)
        if before == after:
            return None
        return Conflict(path, ConflictKind.MODIFIED, before, after,
                        "event plus hash confirm the change")

    def _reconcile(self) -> List[Conflict]:
        """Full walk. The source of truth the event path is checked against."""
        self.full_walks += 1
        self._since_walk = 0
        conflicts: List[Conflict] = []
        current: Dict[str, Tuple[int, int]] = {}
        for root in self.roots:
            current.update(self._walk(root))

        for path in current.keys() - self.fingerprints.keys():
            conflicts.append(Conflict(path, ConflictKind.CREATED, None,
                                      self.hash_file(path), "appeared in the tree"))
        for path in self.fingerprints.keys() - current.keys():
            conflicts.append(Conflict(path, ConflictKind.DELETED,
                                      self.hashes.get(path), None,
                                      "removed from the tree"))
        for path in self.fingerprints.keys() & current.keys():
            if self.fingerprints[path] == current[path]:
                continue
            before = self.hashes.get(path, "<not hashed at baseline>")
            after = self.hash_file(path)
            if before != after:
                conflicts.append(Conflict(path, ConflictKind.MODIFIED, before, after,
                                          "size or mtime moved and the hash confirms"))
        self._known = {c.path: c for c in conflicts}
        return conflicts

    def refresh(self, paths: Iterable[os.PathLike[str] | str] = ()) -> None:
        """Re-baseline after the harness itself changed something."""
        for path in paths:
            key = str(Path(path))
            self.hashes[key] = self.hash_file(key)
        self.fingerprints = {}
        for root in self.roots:
            self.fingerprints.update(self._walk(root))
        self._known.clear()
        self._since_walk = 0

    def close(self) -> None:
        """Release inotify file descriptors."""
        if self._inotify:
            try:
                self._inotify.close()
            except Exception:
                pass
            self._inotify = None

    def stats(self) -> Dict[str, Any]:
        """Event versus walk counters, for the audit trail."""
        total = self.fast_checks + self.full_walks
        return {
            "fast_checks": self.fast_checks,
            "full_walks": self.full_walks,
            "fast_path_ratio": round(self.fast_checks / total, 4) if total else 0.0,
            "tracked_paths": len(self.hashes),
            "watched_files": len(self.fingerprints),
            "inotify": self.use_inotify,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# Context compression
# --------------------------------------------------------------------------- #


@dataclass
class ContextStep:
    """One entry in the running context window."""

    entry_id: str
    role: str
    content: str
    tokens: int = 0
    compressed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass(frozen=True)
class CompressionResult:
    """Outcome of one compression pass."""

    triggered: bool
    tokens_before: int
    tokens_after: int
    entries_before: int
    entries_after: int
    reduction_ratio: float
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """About four characters per token. Fast, offline, good enough to budget on."""
    return max(1, (len(text) + 3) // 4)


class ContextCompressor:
    """Keeps the conversation under a token budget.

    Built in phase 1 and wired to nothing for six phases, because until there
    was an agent there was no conversation to compress. It now holds the actual
    dialogue between operator and model, which is what it was always for.
    """

    def __init__(
        self,
        max_tokens: int = 32_000,
        trigger_ratio: float = 0.90,
        target_ratio: float = 0.45,
        keep_recent: int = 4,
        token_counter: Callable[[str], int] = estimate_tokens,
    ) -> None:
        """
        Args:
            max_tokens: Hard ceiling for the window.
            trigger_ratio: Usage fraction that triggers compression.
            target_ratio: Usage to compress back down to.
            keep_recent: Trailing entries always kept verbatim.
            token_counter: Injectable estimator.
        """
        if not 0.0 < trigger_ratio <= 1.0:
            raise ValueError("trigger_ratio must be in (0, 1]")
        self.max_tokens = int(max_tokens)
        self.trigger_ratio = float(trigger_ratio)
        self.target_ratio = float(target_ratio)
        self.keep_recent = int(keep_recent)
        self._count = token_counter
        self.entries: List[ContextStep] = []
        self.compressions: int = 0

    @property
    def total_tokens(self) -> int:
        """Current token usage across all retained entries."""
        return sum(e.tokens for e in self.entries)

    @property
    def usage_ratio(self) -> float:
        """Current usage as a fraction of max_tokens."""
        return self.total_tokens / self.max_tokens if self.max_tokens else 0.0

    def at_capacity(self) -> bool:
        """True when usage has reached the compression trigger."""
        return self.usage_ratio >= self.trigger_ratio

    def add(self, entry_id: str, role: str, content: str) -> ContextStep:
        """Append one entry.

        Raises:
            ContextBudgetError: when a single entry alone blows the budget.
                Compression cannot save that case, so it is loud.
        """
        tokens = self._count(content)
        if tokens > self.max_tokens:
            raise ContextBudgetError(
                f"{entry_id} needs {tokens} tokens, budget is {self.max_tokens}")
        item = ContextStep(entry_id, role, content, tokens)
        self.entries.append(item)
        return item

    @staticmethod
    def _summarize(chunk: List[ContextStep]) -> str:
        """Compact extractive summary of a slice of history."""
        lines = [f"[COMPRESSED: {len(chunk)} earlier entries]"]
        for e in chunk:
            head = e.content.strip().splitlines()
            lines.append(f"- {e.role}: {(head[0] if head else '')[:80]}")
        return "\n".join(lines)

    def compress(self, force: bool = False) -> CompressionResult:
        """Fold the oldest history into a summary, guaranteeing a net saving."""
        before, count = self.total_tokens, len(self.entries)
        if not force and not self.at_capacity():
            return CompressionResult(False, before, before, count, count, 0.0)

        compressible = max(0, count - self.keep_recent)
        if compressible < 1:
            return CompressionResult(False, before, before, count, count, 0.0)

        target = int(self.max_tokens * self.target_ratio)
        cut = 1
        while cut < compressible:
            projected = (sum(e.tokens for e in self.entries[cut:])
                         + self._count(self._summarize(self.entries[:cut])))
            if projected <= target:
                break
            cut += 1

        old, tail = self.entries[:cut], self.entries[cut:]
        old_tokens = sum(e.tokens for e in old)
        text = self._summarize(old)

        # Degrade to terser summaries rather than making things worse. With many
        # small entries the per-entry line costs more than the content it
        # replaces, which would turn compression into a net loss.
        if self._count(text) > old_tokens // 2:
            text = f"[COMPRESSED: {len(old)} earlier entries elided]"
        if self._count(text) >= old_tokens:
            return CompressionResult(False, before, before, count, count, 0.0,
                                     "skipped: summary would exceed source")

        summary = ContextStep(f"summary::{old[0].entry_id}..{old[-1].entry_id}",
                              "system", text, self._count(text), compressed=True)
        self.entries = [summary] + tail
        after = self.total_tokens
        self.compressions += 1
        return CompressionResult(
            True, before, after, count, len(self.entries),
            round((before - after) / before, 4) if before else 0.0, text)

    def maybe_compress(self) -> CompressionResult:
        """Compress only if the trigger has been crossed."""
        return self.compress(force=False)

    def as_messages(self) -> List[Dict[str, str]]:
        """The window as chat messages, ready for the model."""
        return [{"role": e.role, "content": e.content} for e in self.entries]


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #


class OllamaStatus(str, Enum):
    """Result of a health probe."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecoveryOption:
    """One path out of a backend outage."""

    key: str
    title: str
    detail: str
    action: str

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


class OllamaClient:
    """Talks to the local Ollama daemon, and can fall back when it cannot.

    Six phases shipped a handler that only ever called `/api/tags`. It health
    checked a daemon nothing was talking to. This one generates, which is what
    makes the difference between a safety framework and an agent.
    """

    DEFAULT_HOST: str = "http://localhost:11434"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        timeout: float = 120.0,
        probe_timeout: float = 1.0,
        fallback_model: str = "qwen3:8b",
        transport: Optional[Callable[[str, Dict[str, Any], float], Dict[str, Any]]] = None,
    ) -> None:
        """
        Args:
            host: Ollama base URL.
            timeout: Generation timeout.
            probe_timeout: Health probe timeout, kept short so a recovery menu
                can be built well inside a couple of seconds.
            fallback_model: Smaller model used when the first choice will not load.
            transport: Injectable HTTP layer, so the whole agent is testable
                without a running daemon.
        """
        self.host = host.rstrip("/")
        self.timeout = float(timeout)
        self.probe_timeout = float(probe_timeout)
        self.fallback_model = fallback_model
        self._transport = transport or self._http
        self.last_status: OllamaStatus = OllamaStatus.UNKNOWN
        self.last_error: str = ""
        self.consecutive_failures: int = 0
        self.calls: int = 0
        self.total_seconds: float = 0.0

    # -- transport --------------------------------------------------------- #

    def _http(self, path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        """POST JSON to the daemon, or GET when payload is empty."""
        url = f"{self.host}{path}"
        if payload:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json"})
        else:
            request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    # -- health ------------------------------------------------------------ #

    def health(self) -> OllamaStatus:
        """Probe once and record the result."""
        try:
            self._transport("/api/tags", {}, self.probe_timeout)
            self.consecutive_failures = 0
            self.last_status = OllamaStatus.HEALTHY
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.consecutive_failures += 1
            self.last_status = (OllamaStatus.DEGRADED
                                if self.consecutive_failures == 1
                                else OllamaStatus.UNREACHABLE)
        return self.last_status

    def models(self) -> List[str]:
        """Names of every model pulled locally. Empty on any failure."""
        try:
            data = self._transport("/api/tags", {}, self.probe_timeout)
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    def recovery_options(self, current_model: str = "unknown") -> List[RecoveryOption]:
        """The three paths out of an outage. Never touches the network."""
        return [
            RecoveryOption(
                "restart", "Restart Ollama and retry",
                "Bounces the user unit and waits for the health probe. The "
                "conversation and any resume point are kept.",
                "systemctl --user restart ollama"),
            RecoveryOption(
                "fallback", f"Fall back to {self.fallback_model}",
                f"{current_model} may not fit in available VRAM. The smaller "
                "model keeps the session moving with less reasoning depth.",
                f"use model {self.fallback_model}"),
            RecoveryOption(
                "manual", "Drop to manual mode",
                "Stops asking the model for plans. You write the task file "
                "yourself and every gate still applies.",
                "harness run <task.yaml>"),
        ]

    # -- generation -------------------------------------------------------- #

    def chat(
        self,
        model: str,
        messages: Sequence[Dict[str, str]],
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> str:
        """Send a conversation and return the assistant's reply text.

        Args:
            model: Local model name, for example `qwen3:32b`.
            messages: Chat messages with `role` and `content`.
            json_mode: Ask the daemon to constrain output to valid JSON. Used
                when the reply must parse into a task spec.
            temperature: Low by default. A model proposing shell commands
                should not be creative.

        Raises:
            OllamaError: on transport failure or an unusable response, with the
                recovery options attached for the caller to present.
        """
        payload: Dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": float(temperature)},
        }
        if json_mode:
            payload["format"] = "json"

        started = time.perf_counter()
        try:
            data = self._transport("/api/chat", payload, self.timeout)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_status = OllamaStatus.UNREACHABLE
            raise OllamaError(
                f"generation failed against {self.host}: {self.last_error}") from exc
        finally:
            self.total_seconds += time.perf_counter() - started
            self.calls += 1

        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            raise OllamaError(f"{model} returned an empty response")
        self.last_status = OllamaStatus.HEALTHY
        self.consecutive_failures = 0
        return content

    def stats(self) -> Dict[str, Any]:
        """Call counters, for the audit trail."""
        return {
            "host": self.host,
            "status": self.last_status.value,
            "calls": self.calls,
            "total_seconds": round(self.total_seconds, 3),
            "mean_seconds": round(self.total_seconds / self.calls, 3) if self.calls else 0.0,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------------------- #
# Machine observation
# --------------------------------------------------------------------------- #


#: Parameter counts for models whose name does not carry one.
KNOWN_MODEL_PARAMS_B: Dict[str, float] = {
    "devstral": 24.0, "codestral": 22.0, "mistral": 7.0, "mixtral": 47.0,
    "llama3": 8.0, "phi3": 3.8, "gemma2": 9.0, "deepseek-coder": 6.7,
}


@dataclass
class Accelerator:
    """What the model will actually run on.

    Ollama silently falls back to CPU when the driver is missing, the model does
    not fit in VRAM, or it was built without GPU support. It reports this only in
    `ollama ps`, which nobody reads before typing a request. Detecting it here
    means the router can stop sending a 32b model to a CPU that will take three
    minutes to answer.
    """

    kind: str = "cpu"           # cuda | rocm | cpu
    name: str = ""
    vram_mb: int = 0
    free_vram_mb: int = 0
    running_on: str = ""        # what `ollama ps` reports for a loaded model
    detail: str = ""

    @property
    def gpu(self) -> bool:
        """True when inference will actually be accelerated."""
        return self.kind != "cpu" and self.free_vram_mb > 0

    def fits(self, model: str) -> bool:
        """Rough guess at whether a model will fit in free VRAM.

        Parameter count from the name, two bytes per parameter for a typical
        q4 quant plus context overhead. Deliberately crude: the cost of being
        wrong is a slow answer, and the cost of not guessing at all is routing a
        32b model onto a CPU every single time.
        """
        if not self.gpu:
            return False
        match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model, re.IGNORECASE)
        params = float(match.group(1)) if match else KNOWN_MODEL_PARAMS_B.get(
            model.split(":")[0].lower(), 0.0)
        if not params:
            return True
        return self.free_vram_mb >= params * 1024 * 0.7

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return {**asdict(self), "gpu": self.gpu}

    def render(self) -> str:
        """One line for the model's context and the status bar."""
        if not self.gpu:
            return f"accelerator: CPU only ({self.detail or 'no usable GPU'})"
        return (f"accelerator: {self.kind} {self.name}, "
                f"{self.free_vram_mb} MB free of {self.vram_mb} MB"
                + (f", currently running on {self.running_on}"
                   if self.running_on else ""))


def detect_accelerator(
    runner: Optional[Callable[[str, Optional[str], Optional[float]], str]] = None,
) -> Accelerator:
    """Work out whether Ollama is actually using a GPU.

    Checks three things in order of authority: what `ollama ps` says a loaded
    model is running on, what nvidia-smi reports, then rocm. `ollama ps` wins
    because it is the only one that reflects what the daemon actually decided
    rather than what the hardware could in principle do.
    """
    execute = runner or shell

    try:
        listing = execute("ollama ps", None, 10.0)
        for line in listing.splitlines()[1:]:
            if "%" in line and ("CPU" in line.upper() or "GPU" in line.upper()):
                processor = line.split()[-2:] and " ".join(line.split()[-2:])
                if "CPU" in processor.upper() and "GPU" not in processor.upper():
                    return Accelerator(
                        kind="cpu", running_on=processor,
                        detail="ollama ps reports a loaded model on CPU")
                break
    except Exception:
        pass

    try:
        out = execute(
            "nvidia-smi --query-gpu=name,memory.total,memory.free "
            "--format=csv,noheader,nounits", None, 10.0)
        name, total, free = [p.strip() for p in out.splitlines()[0].split(",")]
        return Accelerator("cuda", name, int(float(total)), int(float(free)))
    except Exception as exc:
        nvidia_error = f"{type(exc).__name__}"

    try:
        out = execute("rocm-smi --showmeminfo vram --csv", None, 10.0)
        if out.strip():
            return Accelerator("rocm", "amd", detail="rocm-smi responded")
    except Exception:
        pass

    return Accelerator(kind="cpu",
                       detail=f"no nvidia-smi or rocm-smi ({nvidia_error})")


@dataclass
class MachineState:
    """What the box actually looks like right now.

    The agent gets this before it proposes anything. A model asked to "clean up
    docker" with no idea what is installed will invent plausible commands; the
    same model given real `docker system df` output will work from what is
    there. Observation is the cheapest accuracy improvement available.
    """

    hostname: str = ""
    os_release: str = ""
    kernel: str = ""
    uptime: str = ""
    disk: str = ""
    memory: str = ""
    cwd: str = ""
    accelerator: Accelerator = field(default_factory=Accelerator)
    ollama_models: List[str] = field(default_factory=list)
    extras: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    def render(self) -> str:
        """Compact text block for the model's context."""
        lines = [
            f"hostname: {self.hostname}",
            f"os: {self.os_release}",
            f"kernel: {self.kernel}",
            f"uptime: {self.uptime}",
            f"cwd: {self.cwd}",
            self.accelerator.render(),
            "",
            "disk:", self.disk,
            "",
            "memory:", self.memory,
        ]
        if self.ollama_models:
            lines += ["", f"local models: {', '.join(self.ollama_models)}"]
        for name, value in self.extras.items():
            lines += ["", f"{name}:", value]
        return "\n".join(lines)


#: Read-only probes. Everything here is safe to run without asking, which is
#: what makes observation free: nothing in this list can change the system.
OBSERVE_COMMANDS: List[Tuple[str, str]] = [
    ("hostname", "hostname"),
    ("os_release", "grep PRETTY_NAME /etc/os-release | cut -d'\"' -f2"),
    ("kernel", "uname -r"),
    ("uptime", "uptime -p"),
    ("disk", "df -h / /home 2>/dev/null"),
    ("memory", "free -h"),
]


def observe_machine(
    extra: Optional[Dict[str, str]] = None,
    ollama: Optional[OllamaClient] = None,
    runner: Optional[Callable[[str, Optional[str], Optional[float]], str]] = None,
) -> MachineState:
    """Gather read-only facts about the machine.

    Args:
        extra: Additional named read-only commands to include.
        ollama: When given, the local model list is included.
        runner: Injectable shell, so this is testable.

    Returns:
        A MachineState. A probe that fails is recorded rather than raised: an
        observation step that can abort a task would make the agent less
        reliable than no observation at all.
    """
    execute = runner or shell
    state = MachineState(cwd=os.getcwd())

    for name, command in OBSERVE_COMMANDS:
        try:
            setattr(state, name, execute(command, None, 15.0))
        except Exception as exc:
            state.errors.append(f"{name}: {type(exc).__name__}: {exc}")

    for name, command in (extra or {}).items():
        try:
            state.extras[name] = execute(command, None, 20.0)
        except Exception as exc:
            state.errors.append(f"{name}: {type(exc).__name__}: {exc}")

    try:
        state.accelerator = detect_accelerator(execute)
    except Exception as exc:
        state.errors.append(f"accelerator: {type(exc).__name__}: {exc}")

    if ollama is not None:
        state.ollama_models = ollama.models()

    return state
