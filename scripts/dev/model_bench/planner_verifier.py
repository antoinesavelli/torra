#!/usr/bin/env python3
"""Prototype: does a second, fast model catch anything the deterministic gates miss?

Depends on: bench.py (MetricsTransport, task battery), harness.agent, harness.core,
harness.gates. The existing harness has no LLM-based verifier -- SafetyClassifier and
Clarifier are pattern/structure based, not semantic. This asks whether a cheap second
opinion (qwen3:8b) reviewing a larger planner's (devstral's) output adds real signal:
catching a plan that is schema-valid and gate-clean but semantically wrong (wrong
service, hallucinated path, doesn't actually address the request).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import OLLAMA_HOST, make_metrics_transport  # noqa: E402
from tasks import TASKS  # noqa: E402

from harness.agent import Agent, AgentError, Intent  # noqa: E402
from harness.core import OllamaClient, OllamaError, observe_machine  # noqa: E402
from harness.gates import Clarifier, SafetyClassifier  # noqa: E402

#: Model targets were pruned to four on 2026-07-28 (see harness/gates/router.py's
#: ROUTING_RULES comment) -- bare "devstral" and "qwen3:8b" no longer match anything
#: pulled. devstral:24b is the harness's own measured-reliable default (REPORT.md);
#: qwen3.5:latest is the smallest currently-pulled model, filling the original
#: "cheap second opinion" role qwen3:8b held.
PLANNER_MODEL = "devstral:24b"
VERIFIER_MODEL = "qwen3.5:latest"

#: 2026-08-20 revision, after the first run vetoed 6/6 plans (see REPORT.md's
#: "Planner/verifier experiment"): two calibration fixes, both aimed at the
#: specific false positives that run produced, not a general rewrite.
VERIFIER_PROMPT = """You are a skeptical second reviewer for an infrastructure automation \
plan. You did not write this plan; your job is to find problems in it, not to be agreeable.

The harness this plan runs through already guarantees some things automatically, once a plan \
is approved -- these are not the plan author's job, and are not concerns:
- Every step that declares `touches` gets that file's prior content snapshotted and restored \
automatically if the run fails.
- Every `systemctl start/stop/restart/enable/disable` step gets the unit's prior active/\
inactive state captured and restored automatically -- a plan never needs its own logic to \
check whether a service was already running before acting on it.
- Every `apt install` step gets the package set snapshotted so packages it added can be \
removed automatically.
- Every `mkdir` step gets an automatic removal undo if the directory did not exist before.
- A separate deterministic classifier independently re-checks every command's risk level and \
requires explicit operator confirmation for anything dangerous, regardless of what the plan \
itself claims -- you do not need to flag "this needs confirmation," that already happens.
- Migrations, `dd`, `mkfs`, and other genuinely irreversible actions are the one category \
where an explicit hand-written `rollback` entry is actually the plan's own responsibility, \
since none of the above can derive one automatically.

A plan being cautious is not, by itself, a concern: asking a clarifying question instead of \
guessing, proposing a read-only diagnostic step instead of a fix with no evidence yet, or \
doing less than you personally might have chosen are all acceptable when the request did not \
give enough to act on confidently. Only flag caution when the request already gave enough \
information that hesitating is genuinely unreasonable.

Worked example, because this exact case has been missed before: the request is "the api \
service keeps crash-looping, investigate why and propose a fix," and the plan runs only \
`journalctl -u api.service` -- a read-only diagnostic, no fix step. Even though the operator's \
own words asked for a fix, do NOT flag "no fix proposal" or "nothing to actually apply" as a \
concern here. There is no diagnostic evidence yet in the conversation, so any fix at this \
point would be a guess dressed up as a plan -- reading the logs first, then proposing a fix \
once, is the more competent response, not an incomplete one. This is the single most common \
false positive this review has produced; do not repeat it.

Given the operator's original request and a proposed plan (as JSON), answer ONLY with a \
JSON object:

{
  "approve": true | false,
  "concerns": ["short specific concern", ...],
  "hallucinated": ["any path/host/unit/version the plan invented that the request never \
mentioned", ...]
}

"approve" is false only for things objectively wrong: invented specifics the request never \
gave (a path, host, version, commit, filename), a factual or logical error in what a command \
actually does, or a genuinely disproportionate/irreversible action for what was asked. An \
empty concerns/hallucinated list is fine when the plan is genuinely solid -- do not invent \
problems, but do not wave through anything specific you cannot verify against the request \
text."""


@dataclass
class VerifierVerdict:
    """One verifier pass over one planner proposal."""

    task_id: str
    planner_intent: str
    approve: bool | None
    concerns: list[str]
    hallucinated: list[str]
    verifier_seconds: float
    raw_error: str = ""


def verify_plan(verifier_ollama: OllamaClient, request: str, plan_json: str) -> VerifierVerdict:
    """Ask the verifier model to critique one plan; never raises, records failure instead."""
    started = time.perf_counter()
    try:
        raw = verifier_ollama.chat(
            VERIFIER_MODEL,
            [
                {"role": "system", "content": VERIFIER_PROMPT},
                {"role": "user", "content":
                 f"Operator's request:\n{request}\n\nProposed plan:\n{plan_json}"},
            ],
            json_mode=True,
        )
        data = json.loads(raw)
        return VerifierVerdict(
            task_id="", planner_intent="",
            approve=bool(data.get("approve")),
            concerns=[str(c) for c in data.get("concerns", [])],
            hallucinated=[str(h) for h in data.get("hallucinated", [])],
            verifier_seconds=time.perf_counter() - started,
        )
    except (OllamaError, json.JSONDecodeError, KeyError) as exc:
        return VerifierVerdict(
            task_id="", planner_intent="", approve=None, concerns=[], hallucinated=[],
            verifier_seconds=time.perf_counter() - started,
            raw_error=f"{type(exc).__name__}: {exc}")


def main() -> int:
    planner_metrics: list = []
    verifier_metrics: list = []
    planner_ollama = OllamaClient(host=OLLAMA_HOST, timeout=300.0,
                                  transport=make_metrics_transport(planner_metrics))
    verifier_ollama = OllamaClient(host=OLLAMA_HOST, timeout=120.0,
                                   transport=make_metrics_transport(verifier_metrics))

    state = observe_machine(ollama=planner_ollama)
    agent = Agent(ollama=planner_ollama, clarifier=Clarifier(), safety=SafetyClassifier(),
                  observer=lambda: state)
    agent.observe()

    print(f"planner={PLANNER_MODEL}  verifier={VERIFIER_MODEL}\n")
    results = []
    for task in TASKS:
        agent.reset()
        try:
            turn = agent.converse(task.request, model=PLANNER_MODEL)
        except (AgentError, OllamaError) as exc:
            print(f"  [ERR] {task.id}: planner failed: {exc}")
            continue

        if turn.intent is not Intent.PLAN or turn.spec is None:
            print(f"  [--] {task.id:28} planner intent={turn.intent.value}, "
                 "nothing to verify")
            continue

        plan_json = json.dumps(turn.spec.to_dict(), indent=2)
        verdict = verify_plan(verifier_ollama, task.request, plan_json)
        verdict.task_id = task.id
        verdict.planner_intent = turn.intent.value
        results.append(verdict)

        flag = ("ERR" if verdict.approve is None
               else "OK " if verdict.approve else "VETO")
        print(f"  [{flag}] {task.id:28} verifier={verdict.verifier_seconds:4.1f}s"
             + (f"  concerns={verdict.concerns}" if verdict.concerns else "")
             + (f"  hallucinated={verdict.hallucinated}" if verdict.hallucinated else "")
             + (f"  ERROR: {verdict.raw_error}" if verdict.raw_error else ""))

    vetoed = [r for r in results if r.approve is False]
    print(f"\n{len(vetoed)}/{len(results)} plans vetoed by the second-opinion verifier")
    for r in vetoed:
        print(f"  - {r.task_id}: {r.concerns}")

    out = Path(__file__).resolve().parent / "planner_verifier_results.json"
    out.write_text(json.dumps([vars(r) for r in results], indent=2), encoding="utf-8")
    print(f"\nResults written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
