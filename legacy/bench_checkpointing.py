"""
bench_checkpointing.py
Measures what adaptive checkpointing actually costs, against a no-checkpoint
control and against a naive checkpoint-every-step baseline.

Run:  python3 bench_checkpointing.py
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from harness_core import (
    AdaptiveCheckpointing,
    CheckpointPolicy,
    CoreExecutor,
    OllamaFallbackHandler,
    Step,
    TaskLength,
)


def make_steps(n: int, seconds_each: float, payload_bytes: int = 2048) -> List[Step]:
    """Steps whose output is a realistic size, so checkpoints are realistic too."""
    blob = "x" * payload_bytes
    return [
        Step(step_id=f"s{i}", kind="shell", command=f"echo {i}",
             description=f"step {i}", estimated_seconds=seconds_each,
             run=lambda b=blob: b)
        for i in range(1, n + 1)
    ]


class AlwaysCheckpoint(AdaptiveCheckpointing):
    """Control: the naive policy that checkpoints after every single step."""

    def decide_frequency(self, steps) -> CheckpointPolicy:  # type: ignore[override]
        minutes = self.estimate_duration(steps)
        return CheckpointPolicy(TaskLength.LONG, minutes, 1, len(steps))


class NeverCheckpoint(AdaptiveCheckpointing):
    """Control: no checkpoints at all, not even a final one."""

    def decide_frequency(self, steps) -> CheckpointPolicy:  # type: ignore[override]
        minutes = self.estimate_duration(steps)
        return CheckpointPolicy(TaskLength.SHORT, minutes, 0, 0)

    def save_state(self, checkpoint) -> Path:  # type: ignore[override]
        return Path("/dev/null")


def run_once(cls: type[AdaptiveCheckpointing], steps: List[Step], tag: str) -> Dict[str, Any]:
    """One timed execution under a given checkpoint policy."""
    with tempfile.TemporaryDirectory() as tmp:
        cp = cls(Path(tmp) / "cp")
        ex = CoreExecutor(cp, ollama=OllamaFallbackHandler(probe=lambda: True))
        started = time.perf_counter()
        report = ex.execute(f"bench-{tag}", steps)
        wall = time.perf_counter() - started
        stats = cp.stats()
        return {
            "wall_seconds": wall,
            "checkpoint_seconds": stats["write_seconds"],
            "writes": stats["writes"],
            "intermediate_writes": stats["intermediate_writes"],
            "bytes_written": stats["bytes_written"],
            "ok": report.ok,
        }


def profile(name: str, n_steps: int, seconds_each: float, repeats: int = 5) -> Dict[str, Any]:
    """Benchmark one task shape across all three policies."""
    steps = make_steps(n_steps, seconds_each)
    out: Dict[str, Any] = {"profile": name, "steps": n_steps,
                           "estimated_minutes": round(n_steps * seconds_each / 60, 2)}

    for tag, cls in [("adaptive", AdaptiveCheckpointing),
                     ("always", AlwaysCheckpoint),
                     ("never", NeverCheckpoint)]:
        runs = [run_once(cls, steps, f"{name}-{tag}-{i}") for i in range(repeats)]
        out[tag] = {
            "wall_median_ms": round(statistics.median(r["wall_seconds"] for r in runs) * 1000, 3),
            "checkpoint_median_ms": round(
                statistics.median(r["checkpoint_seconds"] for r in runs) * 1000, 3),
            "writes": runs[0]["writes"],
            "intermediate_writes": runs[0]["intermediate_writes"],
            "kb_written": round(runs[0]["bytes_written"] / 1024, 1),
        }

    base = out["never"]["wall_median_ms"]
    for tag in ("adaptive", "always"):
        delta = out[tag]["wall_median_ms"] - base
        out[tag]["overhead_ms_vs_none"] = round(delta, 3)
        out[tag]["overhead_pct_vs_none"] = round(100 * delta / base, 2) if base else None
    saved = out["always"]["wall_median_ms"] - out["adaptive"]["wall_median_ms"]
    out["adaptive_saves_vs_always_ms"] = round(saved, 3)
    out["adaptive_saves_vs_always_pct"] = (
        round(100 * saved / out["always"]["wall_median_ms"], 2)
        if out["always"]["wall_median_ms"] else None
    )
    return out


def main() -> None:
    profiles = [
        profile("short", n_steps=10, seconds_each=10.0),     # 1.7 min
        profile("medium", n_steps=40, seconds_each=30.0),    # 20 min
        profile("long", n_steps=60, seconds_each=60.0),      # 60 min
        profile("long_heavy", n_steps=200, seconds_each=60.0),  # 200 min
    ]

    print(f"{'profile':<12}{'est_min':>9}{'policy':>10}{'writes':>8}"
          f"{'kb':>9}{'cp_ms':>9}{'wall_ms':>10}{'ovh_vs_none':>13}")
    print("-" * 80)
    for p in profiles:
        for tag in ("never", "adaptive", "always"):
            row = p[tag]
            ovh = row.get("overhead_pct_vs_none")
            ovh_s = "baseline" if ovh is None else f"{ovh:+.1f}%"
            print(f"{p['profile']:<12}{p['estimated_minutes']:>9}{tag:>10}"
                  f"{row['writes']:>8}{row['kb_written']:>9}"
                  f"{row['checkpoint_median_ms']:>9}{row['wall_median_ms']:>10}{ovh_s:>13}")
        print(f"{'':<12}{'':>9}{'adaptive vs always:':>28} "
              f"{p['adaptive_saves_vs_always_ms']} ms saved "
              f"({p['adaptive_saves_vs_always_pct']}%)")
        print("-" * 80)

    Path("bench_results.json").write_text(json.dumps(profiles, indent=2), encoding="utf-8")
    print("\nwrote bench_results.json")


if __name__ == "__main__":
    main()
