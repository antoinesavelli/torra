"""VERIFY_STATE: preconditions rechecked between steps, TTL cached.

Depends on: stdlib only.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["Precondition", "PreconditionResult", "PreconditionReport", "StateVerifier"]


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

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass
class PreconditionReport:
    """Aggregate of every precondition run."""

    passed: bool
    results: list[PreconditionResult] = field(default_factory=list)

    @property
    def failures(self) -> list[PreconditionResult]:
        """Every precondition that did not pass."""
        return [r for r in self.results if not r.passed]

    @property
    def blocking_failures(self) -> list[PreconditionResult]:
        """Failed preconditions that halt the task."""
        return [r for r in self.results if not r.passed and r.blocking]

    def to_dict(self) -> dict[str, Any]:
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
        """Store an injectable monotonic clock, so TTL logic is testable without sleeping."""
        self.preconditions: list[Precondition] = []
        self._clock = clock
        self.cache: dict[str, tuple[PreconditionResult, float]] = {}
        self.flips: list[tuple[str, str]] = []
        self.probe_runs: int = 0
        self.cache_hits: int = 0

    def add(self, precondition: Precondition) -> StateVerifier:
        """Register a precondition. Returns self for chaining."""
        self.preconditions.append(precondition)
        return self

    # -- probes ------------------------------------------------------------ #

    # Live wire: HANDOFF.md §5.20
    @staticmethod
    def systemctl_scope(unit: str) -> str:
        """'user' when systemd's user instance knows this unit, else 'system'.

        `is-active` alone can't tell "not loaded in this scope" apart from
        "loaded and genuinely inactive" -- both report inactive/exit-4
        under --user for a unit that is actually a system service, which
        is exactly the case that matters here. LoadState does distinguish
        them ("not-found" vs "loaded"). Found live: `ollama.service` is a
        system unit on this project's own reference machine, and
        `service_active`'s old `user_scope=True` default silently probed
        the wrong systemd instance for it -- reading "inactive" for a
        service that was actually running.
        """
        try:
            out = subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=LoadState"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.stdout.strip() == "LoadState=loaded":
                return "user"
        except Exception:
            pass
        return "system"

    @staticmethod
    def service_active(unit: str, user_scope: bool | None = None) -> bool:
        """True when systemctl reports the unit active. False on any error.

        `user_scope=None` (the default) autodetects via `systemctl_scope`
        instead of assuming `--user` -- see that method's docstring. Pass
        `True`/`False` explicitly only when the scope is already known and
        the extra probe isn't worth it.
        """
        scope = (
            ("user" if user_scope else "system")
            if user_scope is not None
            else StateVerifier.systemctl_scope(unit)
        )
        cmd = ["systemctl", *(["--user"] if scope == "user" else []), "is-active", unit]
        try:
            return (
                subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
                == "active"
            )
        except Exception:
            return False

    @classmethod
    def default(cls) -> StateVerifier:
        """The verifier every `Torra` session runs with: at least 1 GB free on `/`."""
        verifier = cls()
        verifier.add(
            Precondition(
                "disk:/",
                lambda: cls.disk_free_gb("/") > 1.0,
                "at least 1 GB free on /",
                ttl_seconds=30,
            )
        )
        return verifier

    @staticmethod
    def disk_free_gb(path: str = "/") -> float:
        """Free space in GB, or -1.0 on error."""
        try:
            return shutil.disk_usage(path).free / (1024**3)
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
            return PreconditionResult(
                pc.name, False, pc.blocking, pc.detail, f"{type(exc).__name__}: {exc}"
            )

    def run(self, force: bool = True) -> PreconditionReport:
        """Run every precondition, honouring the TTL cache when not forced."""
        now = self._clock()
        results: list[PreconditionResult] = []
        for pc in self.preconditions:
            cached = self.cache.get(pc.name)
            if (
                not force
                and cached is not None
                and (pc.ttl_seconds < 0 or now - cached[1] < pc.ttl_seconds)
            ):
                self.cache_hits += 1
                results.append(cached[0])
                continue
            fresh = self._run_one(pc)
            if cached is not None and cached[0].passed != fresh.passed:
                self.flips.append(
                    (
                        pc.name,
                        f"{'pass' if cached[0].passed else 'fail'} -> "
                        f"{'pass' if fresh.passed else 'fail'}",
                    )
                )
            self.cache[pc.name] = (fresh, now)
            results.append(fresh)
        return PreconditionReport(
            passed=not any(not r.passed and r.blocking for r in results), results=results
        )

    def guard(self) -> str | None:
        """Between-step check. Returns an abort reason, or None to continue."""
        report = self.run(force=False)
        if report.passed:
            return None
        return "precondition went false mid-run: " + ", ".join(
            f"{r.name}{' (' + r.error + ')' if r.error else ''}" for r in report.blocking_failures
        )

    def stats(self) -> dict[str, Any]:
        """Cache efficiency and any pass/fail transitions observed."""
        total = self.cache_hits + self.probe_runs
        return {
            "probe_runs": self.probe_runs,
            "cache_hits": self.cache_hits,
            "cache_hit_ratio": round(self.cache_hits / total, 4) if total else 0.0,
            "flips": [{"name": n, "transition": t} for n, t in self.flips],
        }
