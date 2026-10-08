#!/usr/bin/env python3
"""Benchmark local Ollama models as torra's planner, via the real Agent/gates.

Depends on: torra.agent (Agent, Intent), torra.core (OllamaClient, observe_machine),
torra.gates (SafetyClassifier, Clarifier, RiskLevel). Talks to a locally running
`ollama serve` over HTTP. A custom transport wraps every call to capture latency and
token counts without touching torra code.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tasks import TASKS, Task  # noqa: E402

from torra.agent import Agent, AgentError, Intent  # noqa: E402
from torra.core import OllamaClient, OllamaError, observe_machine  # noqa: E402
from torra.gates import Clarifier, RiskLevel, SafetyClassifier  # noqa: E402

OLLAMA_HOST = "http://127.0.0.1:11434"
RESULTS_PATH = Path(__file__).resolve().parent / "results.json"

#: The models torra's router actually sends work to today (`ROUTING_RULES`).
MODELS = ["gpt-oss:20b", "devstral:24b", "devstral-small-2:24b"]

#: Ollama's default 32768-token context pushes qwen3:32b to 9%/91% CPU/GPU on a
#: 24GB card (measured: 25GB total working set). 16384 fits at 100% GPU (22GB).
#: torra.core.OllamaClient.chat() has no num_ctx parameter, so this is
#: injected into the request here rather than changing torra/ source for a
#: benchmark-only concern -- see the bench report for the upstream gap this
#: surfaces in ModelRouter.fits().
MODEL_NUM_CTX: dict[str, int] = {"qwen3:32b": 16384}


@dataclass
class CallMetrics:
    """Timing and token counts for one /api/chat round trip."""

    wall_seconds: float
    eval_count: int = 0
    eval_seconds: float = 0.0
    prompt_eval_count: int = 0
    load_seconds: float = 0.0

    @property
    def tokens_per_second(self) -> float:
        """Generation throughput, excluding prompt processing and model load."""
        return self.eval_count / self.eval_seconds if self.eval_seconds else 0.0


def make_metrics_transport(sink: list[CallMetrics]):
    """A transport for OllamaClient that records CallMetrics for every call."""

    def transport(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        url = f"{OLLAMA_HOST}{path}"
        started = time.perf_counter()
        if payload:
            num_ctx = MODEL_NUM_CTX.get(payload.get("model", ""))
            if num_ctx is not None:
                payload.setdefault("options", {})["num_ctx"] = num_ctx
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json"})
        else:
            request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
        wall = time.perf_counter() - started
        if path == "/api/chat":
            sink.append(CallMetrics(
                wall_seconds=wall,
                eval_count=result.get("eval_count", 0),
                eval_seconds=result.get("eval_duration", 0) / 1e9,
                prompt_eval_count=result.get("prompt_eval_count", 0),
                load_seconds=result.get("load_duration", 0) / 1e9,
            ))
        return result

    return transport


def gpu_residency(model: str) -> str:
    """What `ollama ps` reports for PROCESSOR right now, or 'not loaded'."""
    # Ollama reports a bare tag like "devstral" back as "devstral:latest" in
    # /api/ps, so match on either the exact name or its untagged prefix.
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/ps")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for m in data.get("models", []):
            name = m.get("name", "")
            if name == model or name.split(":")[0] == model:
                size_vram = m.get("size_vram", 0)
                size = m.get("size", 1)
                pct = round(size_vram / size * 100) if size else 0
                return f"{pct}% GPU"
        return "not loaded"
    except urllib.error.URLError as exc:
        return f"error: {exc}"


@dataclass
class TaskResult:
    """Everything recorded for one (model, task) pair."""

    model: str
    task_id: str
    tier: int
    ok: bool
    error: str = ""
    intent: str = ""
    intent_correct: bool = False
    schema_valid: bool = False
    repairs: int = 0
    rollback_ok: bool | None = None
    risky_step_ok: bool | None = None
    worst_risk: str = ""
    message: str = ""
    step_commands: list[str] = field(default_factory=list)
    total_wall_seconds: float = 0.0
    total_eval_tokens: int = 0
    total_eval_seconds: float = 0.0
    gpu_residency: str = ""

    @property
    def tokens_per_second(self) -> float:
        return (self.total_eval_tokens / self.total_eval_seconds
                if self.total_eval_seconds else 0.0)


def run_model(model: str) -> list[TaskResult]:
    """Run the full task battery against one model, reusing one Agent/machine observation."""
    print(f"\n{'=' * 70}\n{model}\n{'=' * 70}")

    metrics: list[CallMetrics] = []
    ollama = OllamaClient(host=OLLAMA_HOST, timeout=300.0,
                          transport=make_metrics_transport(metrics))

    warmup_started = time.perf_counter()
    state = observe_machine(ollama=ollama)
    agent = Agent(ollama=ollama, clarifier=Clarifier(), safety=SafetyClassifier(),
                  observer=lambda: state)
    agent.observe()  # populates router.accelerator/available from the observed state
    print(f"  machine observed in {time.perf_counter() - warmup_started:.1f}s, "
          f"accelerator: {state.accelerator.render()}")

    results: list[TaskResult] = []
    for task in TASKS:
        agent.reset()
        residency_before = gpu_residency(model)
        r = grade_task(task, agent, metrics, model)
        r.model = model
        r.gpu_residency = gpu_residency(model)
        results.append(r)
        status = "OK " if r.ok else "ERR"
        verdict = "PASS" if r.intent_correct else "FAIL"
        print(f"  [{status}] t{task.tier} {task.id:28} intent={r.intent or '?':8} "
              f"{verdict}  {r.total_wall_seconds:5.1f}s  {r.tokens_per_second:5.1f} tok/s  "
              f"gpu={r.gpu_residency}  repairs={r.repairs}"
              + (f"  ERROR: {r.error}" if r.error else ""))
        if residency_before != "not loaded" and "100%" not in residency_before:
            print(f"    WARNING: GPU residency was {residency_before} before this call "
                 "-- may include CPU offload")
    return results


def grade_task(task: Task, agent: Agent, metrics: list[CallMetrics],
               model: str) -> TaskResult:
    """Run one task through the real Agent (model pinned explicitly) and grade the result."""
    before = len(metrics)
    started = time.perf_counter()
    result = TaskResult(model="", task_id=task.id, tier=task.tier, ok=False)

    try:
        turn = agent.converse(task.request, model=model)
    except (AgentError, OllamaError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.total_wall_seconds = time.perf_counter() - started
        return result

    result.ok = True
    result.intent = turn.intent.value
    result.intent_correct = turn.intent.value in task.acceptable_intents
    result.repairs = turn.repairs
    result.message = turn.message

    if turn.intent is Intent.PLAN and turn.spec is not None:
        result.schema_valid = True
        safety = SafetyClassifier()
        steps = turn.spec.to_steps(dry_run=True)
        result.step_commands = [s.command for s in steps]
        levels = [safety.classify(s.command).level for s in steps]
        order = [RiskLevel.SAFE, RiskLevel.CAUTION, RiskLevel.DANGEROUS, RiskLevel.CRITICAL]
        result.worst_risk = max(levels, key=order.index).value if levels else "safe"

        if task.requires_rollback:
            result.rollback_ok = bool(turn.spec.rollback)
        if task.expect_risky_step:
            result.risky_step_ok = any(
                lv in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL) for lv in levels)

    result.total_wall_seconds = time.perf_counter() - started
    call_metrics = metrics[before:]
    result.total_eval_tokens = sum(m.eval_count for m in call_metrics)
    result.total_eval_seconds = sum(m.eval_seconds for m in call_metrics)
    return result


def summarize(all_results: list[TaskResult]) -> None:
    """Per-model aggregate table."""
    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    by_model: dict[str, list[TaskResult]] = {}
    for r in all_results:
        by_model.setdefault(r.model, []).append(r)

    for model, results in by_model.items():
        n = len(results)
        errors = sum(1 for r in results if not r.ok)
        intent_correct = sum(1 for r in results if r.intent_correct)
        schema_valid = sum(1 for r in results if r.schema_valid or r.intent != "plan")
        repairs = sum(r.repairs for r in results)
        latencies = [r.total_wall_seconds for r in results if r.ok]
        tps = [r.tokens_per_second for r in results if r.ok and r.tokens_per_second]
        rollback_checks = [r for r in results if r.rollback_ok is not None]
        risky_checks = [r for r in results if r.risky_step_ok is not None]

        print(f"\n{model}")
        print(f"  errors            : {errors}/{n}")
        print(f"  intent correct    : {intent_correct}/{n}"
             f" ({intent_correct/n*100:.0f}%)")
        print(f"  schema valid      : {schema_valid}/{n}"
             f" ({schema_valid/n*100:.0f}%)")
        print(f"  total repairs     : {repairs}")
        if latencies:
            print(f"  latency           : mean {statistics.mean(latencies):.1f}s, "
                 f"median {statistics.median(latencies):.1f}s, max {max(latencies):.1f}s")
        if tps:
            print(f"  tokens/sec        : mean {statistics.mean(tps):.1f}")
        if rollback_checks:
            ok = sum(1 for r in rollback_checks if r.rollback_ok)
            print(f"  rollback declared : {ok}/{len(rollback_checks)} when required")
        if risky_checks:
            ok = sum(1 for r in risky_checks if r.risky_step_ok)
            print(f"  risk flagged      : {ok}/{len(risky_checks)} when expected")


def main() -> int:
    models = sys.argv[1:] or MODELS
    all_results: list[TaskResult] = []
    for model in models:
        all_results.extend(run_model(model))

    summarize(all_results)

    RESULTS_PATH.write_text(
        json.dumps([vars(r) for r in all_results], indent=2, default=str),
        encoding="utf-8")
    print(f"\nFull results written to {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
