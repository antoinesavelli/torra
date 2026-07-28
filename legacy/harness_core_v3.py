"""
harness_core_v3.py
Phase 3 core. Closes the task id collision finding.

  P0-3  TaskLock                     exclusive flock per task id, with stale
                                     lock detection and holder identification
        LockedCheckpointing          IncrementalCheckpointing that refuses to
                                     write without holding the lock

Phase 1 raised this as a MED: a second run silently overwrote the first run's
checkpoint. Phase 2 made it sharper rather than safer, because two concurrent
runs now interleave appends into a shared JSONL delta log, and an interleaved
append corrupts replay instead of merely clobbering a file.

Phase 1 and Phase 2 files are unmodified.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from harness_core import Checkpoint, CheckpointError
from harness_core_v2 import IncrementalCheckpointing

__all__ = ["LockInfo", "LockBusy", "TaskLock", "LockedCheckpointing"]


class LockBusy(CheckpointError):
    """Raised when a task id is already locked by a live process."""

    def __init__(self, message: str, holder: Optional["LockInfo"] = None) -> None:
        """Args:
            holder: Who currently holds the lock, when known.
        """
        super().__init__(message)
        self.holder = holder


@dataclass(frozen=True)
class LockInfo:
    """Who holds a lock, and since when."""

    task_id: str
    pid: int
    host: str
    acquired_at: float
    argv: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)

    def describe(self, now: Optional[float] = None) -> str:
        """Human readable holder description."""
        age = (now if now is not None else time.time()) - self.acquired_at
        return (f"pid {self.pid} on {self.host} since {age:.0f}s ago"
                + (f" ({self.argv})" if self.argv else ""))


class TaskLock:
    """Exclusive advisory lock for one task id.

    Uses `fcntl.flock` on a sidecar file. flock is the right primitive here for
    two reasons: the kernel releases it automatically when the holding process
    dies, so a crashed run cannot wedge a task id forever, and it is genuinely
    atomic, unlike the create-a-pidfile-and-check-it dance which always has a
    race between the check and the create.

    The file also carries JSON identifying the holder, so a blocked run can say
    "pid 4821 on pop-os since 340s ago" instead of "locked".

    Usable as a context manager:

        with TaskLock(lock_dir, "deploy-2026-07-27"):
            ...

    Raises LockBusy on contention unless `block=True`, in which case it waits up
    to `timeout` seconds.
    """

    def __init__(
        self,
        lock_dir: os.PathLike[str] | str,
        task_id: str,
        block: bool = False,
        timeout: float = 0.0,
        poll_interval: float = 0.05,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            lock_dir: Directory holding `<task_id>.lock` files.
            task_id: The task being locked.
            block: Wait for the lock instead of failing immediately.
            timeout: Maximum wait in seconds when block is True.
            poll_interval: How often to retry while blocking.
            clock: Injectable time source.
        """
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.task_id = task_id
        self.block = bool(block)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._clock = clock
        self._fh: Optional[Any] = None
        self.held: bool = False

    @property
    def path(self) -> Path:
        """Path of the lock file for this task id."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.task_id)
        return self.lock_dir / f"{safe}.lock"

    # -- inspection -------------------------------------------------------- #

    def read_holder(self) -> Optional[LockInfo]:
        """Read whoever last claimed this lock, if anyone.

        Returns None when the file is missing or unparseable. A stale or torn
        holder record is not an error: the flock itself is the source of truth,
        this is only for a readable message.
        """
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            return LockInfo(
                task_id=raw.get("task_id", self.task_id),
                pid=int(raw.get("pid", -1)),
                host=raw.get("host", "?"),
                acquired_at=float(raw.get("acquired_at", 0.0)),
                argv=raw.get("argv", ""),
            )
        except Exception:
            return None

    def is_locked(self) -> bool:
        """True when another process currently holds this lock."""
        if self.held:
            return False
        try:
            probe = self.path.open("a+")
        except OSError:
            return False
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            probe.close()

    # -- acquisition ------------------------------------------------------- #

    def acquire(self) -> "TaskLock":
        """Take the lock.

        Raises:
            LockBusy: when another live process holds it and we are not
                blocking, or the block timed out.
        """
        if self.held:
            return self

        deadline = self._clock() + self.timeout if self.block else self._clock()
        self._fh = self.path.open("a+")

        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    self._fh.close()
                    self._fh = None
                    raise CheckpointError(
                        f"lock on {self.path} failed: {exc!r}") from exc
                holder = self.read_holder()
                if not self.block or self._clock() >= deadline:
                    self._fh.close()
                    self._fh = None
                    detail = holder.describe(self._clock()) if holder else "unknown holder"
                    raise LockBusy(
                        f"task id '{self.task_id}' is already running: {detail}. "
                        "Use a unique task id, or resume the existing run.",
                        holder,
                    )
                time.sleep(self.poll_interval)

        info = LockInfo(
            task_id=self.task_id,
            pid=os.getpid(),
            host=socket.gethostname(),
            acquired_at=self._clock(),
            argv=" ".join(os.sys.argv[:3]) if hasattr(os, "sys") else "",
        )
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(json.dumps(info.to_dict()))
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:
            # The lock is held; failing to annotate it is not fatal, but it must
            # not pass unnoticed either.
            raise CheckpointError(
                f"acquired lock but could not write holder info: {exc!r}") from exc

        self.held = True
        return self

    def release(self) -> None:
        """Release the lock and remove the file. Safe to call twice."""
        if not self._fh:
            self.held = False
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        finally:
            self._fh = None
            self.held = False
        try:
            self.path.unlink()
        except OSError:
            pass  # another waiter may have already recreated it

    def __enter__(self) -> "TaskLock":
        """Acquire on entry."""
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        """Release on exit, even when the body raised."""
        self.release()


class LockedCheckpointing(IncrementalCheckpointing):
    """Incremental checkpointing that refuses to write without the lock.

    Phase 2 warned about a task id collision after the fact. This makes the
    collision impossible: the first write for a task id takes an exclusive lock,
    and a second process either fails fast with the holder's identity or waits,
    depending on how it was constructed.

    The lock is held for the life of the checkpointer, not per write, because
    the thing being protected is the base-plus-deltas sequence as a whole. An
    interleaved append to a shared delta log corrupts replay, which is a worse
    failure than the file overwrite this started as.
    """

    def __init__(
        self,
        checkpoint_dir: os.PathLike[str] | str,
        step_costs: Optional[Dict[str, float]] = None,
        clock: Callable[[], float] = time.time,
        compact_every: int = 25,
        min_observations: Optional[int] = None,
        learn: bool = True,
        block: bool = False,
        lock_timeout: float = 0.0,
    ) -> None:
        """
        Args:
            block: Wait for a contended lock instead of failing fast.
            lock_timeout: Maximum wait when block is True.
            (Other arguments as IncrementalCheckpointing.)
        """
        super().__init__(checkpoint_dir, step_costs, clock, compact_every,
                         min_observations, learn)
        self.lock_dir = self.checkpoint_dir / "locks"
        self.block = bool(block)
        self.lock_timeout = float(lock_timeout)
        self.locks: Dict[str, TaskLock] = {}
        self.contentions: list[str] = []

    def acquire(self, task_id: str) -> TaskLock:
        """Take the lock for a task id, or raise LockBusy.

        Idempotent: calling twice for the same task id returns the held lock.
        """
        existing = self.locks.get(task_id)
        if existing and existing.held:
            return existing
        lock = TaskLock(self.lock_dir, task_id, block=self.block,
                        timeout=self.lock_timeout, clock=self._clock)
        try:
            lock.acquire()
        except LockBusy as exc:
            note = f"task '{task_id}' contended: {exc}"
            self.contentions.append(note)
            self.errors.append(note)
            raise
        self.locks[task_id] = lock
        return lock

    def save_state(self, checkpoint: Checkpoint) -> Path:
        """Write, taking the task lock first if it is not already held."""
        self.acquire(checkpoint.task_id)
        return super().save_state(checkpoint)

    def release(self, task_id: str) -> None:
        """Release one task's lock."""
        lock = self.locks.pop(task_id, None)
        if lock:
            lock.release()

    def release_all(self) -> None:
        """Release every lock this checkpointer holds."""
        for task_id in list(self.locks):
            self.release(task_id)

    def stats(self) -> Dict[str, Any]:
        """Counters plus lock contention history."""
        base = super().stats()
        base["contentions"] = list(self.contentions)
        base["locks_held"] = [t for t, l in self.locks.items() if l.held]
        return base
