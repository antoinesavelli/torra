"""The model loop: turns a conversation into a validated TaskSpec, never a shell.

Depends on: harness.core (ContextCompressor, MachineState, OllamaClient,
TaskSpec, observe_machine), harness.gates (Clarifier, ModelRouter, SafetyClassifier).

The design rule throughout: the model produces a TaskSpec and nothing else. It
does not execute, it does not get a shell, it does not decide what is safe. It
writes a proposal into the same structure a human writes by hand, and that
proposal travels the exact code path the existing gates already cover. A model
that could execute directly would need every gate reimplemented on a second
path, and the second path is always the one nobody tested.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .core import (
    ContextCompressor,
    MachineState,
    OllamaClient,
    TaskSpec,
    TaskSpecError,
    observe_machine,
)
from .gates import Clarifier, ModelRouter, SafetyClassifier, model_available

__all__ = ["Intent", "Turn", "Agent", "AgentError", "PLAN_SCHEMA"]


class AgentError(RuntimeError):
    """Raised when the model cannot produce a usable proposal."""


class Intent(str, Enum):
    """What the agent decided to do with a turn."""

    ANSWER = "answer"        # a question, no system change needed
    ASK = "ask"              # underspecified, needs the operator
    PLAN = "plan"            # enough information, here is a proposal
    REFUSE = "refuse"        # will not do this


@dataclass
class Turn:
    """One exchange: what the operator said and what the agent decided."""

    request: str
    intent: Intent
    message: str = ""
    questions: list[str] = field(default_factory=list)
    spec: TaskSpec | None = None
    model: str = ""
    routing_reason: str = ""
    clarifier_gaps: list[str] = field(default_factory=list)
    machine_observed: bool = False
    repairs: int = 0
    seconds: float = 0.0
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return {
            "request": self.request,
            "intent": self.intent.value,
            "message": self.message,
            "questions": list(self.questions),
            "spec": self.spec.to_dict() if self.spec else None,
            "model": self.model,
            "routing_reason": self.routing_reason,
            "clarifier_gaps": list(self.clarifier_gaps),
            "machine_observed": self.machine_observed,
            "repairs": self.repairs,
            "seconds": round(self.seconds, 3),
        }


#: The contract the model must satisfy. Kept in one place and injected into the
#: prompt verbatim, so the schema the model is told about and the schema
#: TaskSpec.validate enforces cannot drift apart.
PLAN_SCHEMA = """{
  "intent": "plan" | "ask" | "answer" | "refuse",
  "message": "one or two sentences to the operator",
  "questions": ["only when intent is ask"],
  "task": {
    "task_id": "short-kebab-id",
    "request": "one line restating what this accomplishes, with concrete targets",
    "watch_roots": ["/dirs/to/watch/for/external/edits"],
    "steps": [
      {
        "id": "short-id",
        "kind": "shell|write|deploy|build|install|read",
        "run": "the exact shell command",
        "description": "what this step does",
        "estimated_seconds": 10,
        "touches": ["/files/this/step/modifies"]
      }
    ],
    "rollback": [
      {"run": "command that undoes a step", "description": "why", "safe": true}
    ]
  }
}"""

SYSTEM_PROMPT = """You are the planning half of a Pop-OS automation harness.

You do NOT execute anything. You write a plan. A separate safety layer resolves
every command you propose down to argv, classifies it, snapshots the files it
touches, and asks the operator before anything dangerous runs. Your job is to
produce a correct and specific plan; the harness's job is to make running it safe.

Rules that matter:

1. Reply with JSON only. No prose outside the JSON, no markdown fences.
2. `run` is the literal shell command. It is both what executes and what gets
   safety-classified, so never put one command in `run` and describe a
   different one in `description`.
3. List every file a step modifies in `touches`. That is what lets the harness
   snapshot and restore it if the task fails. Omitting it means no undo.
4. Prefer specific over clever. Absolute paths, explicit filters, named units.
5. If the request is missing something you genuinely need, use intent "ask" and
   give the smallest set of questions that would unblock you. Do not guess a
   path, a hostname, or a version.
6. If the request is a question about the machine rather than a change to it,
   use intent "answer" and reply from the observed state. Do not plan a task.
7. For anything you cannot express as a reversible step (database migrations,
   dd, mkfs), add an explicit `rollback` entry. The harness derives undos for
   files, services, packages and mkdir automatically, nothing else.
8. Use intent "refuse" if the request would damage the system with no
   legitimate purpose you can identify.
9. Check the request against the observed machine state above before
   planning. If it asserts something that state directly contradicts (e.g.
   claims hardware or a service is down when it is reported present and
   working), use intent "ask" and name the discrepancy. Do not plan around
   an unverified claim just because the operator stated it as fact.

Output schema:
""" + PLAN_SCHEMA


class Agent:
    """Turns a conversation into a validated TaskSpec.

    The clarifier is used here as a *tool the model consults*, not as a wall in
    front of it. Before generation the agent runs `Clarifier.detect()` on the
    request and hands the model the resulting gaps, so instead of guessing at a
    vague request it is told precisely what structure is missing and can ask one
    good question. That inverts six phases of the clarifier's role without
    changing a line of its logic.
    """

    #: How many times to hand a rejection back to the model before giving up.
    MAX_REPAIRS: int = 2

    def __init__(
        self,
        ollama: OllamaClient,
        router: ModelRouter | None = None,
        clarifier: Clarifier | None = None,
        safety: SafetyClassifier | None = None,
        compressor: ContextCompressor | None = None,
        observer: Callable[[], MachineState] | None = None,
        max_repairs: int | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        """Wire the model client, router, clarifier, safety pre-screen, and context."""
        self.ollama = ollama
        self.router = router or ModelRouter()
        self.clarifier = clarifier or Clarifier()
        self.safety = safety or SafetyClassifier()
        self.context = compressor or ContextCompressor(max_tokens=24_000)
        self._observe = observer or (lambda: observe_machine(ollama=ollama))
        self.max_repairs = self.MAX_REPAIRS if max_repairs is None else int(max_repairs)
        self.system_prompt = system_prompt

        self.state: MachineState | None = None
        self.turns: list[Turn] = []
        self._turn_counter: int = 0

    # -- observation ------------------------------------------------------- #

    def observe(self, refresh: bool = False) -> MachineState:
        """Read the machine, caching until asked to refresh."""
        # A model given real `docker system df` output works from what is
        # actually there instead of inventing plausible commands. Cheapest
        # accuracy improvement available, and every command here is read-only.
        if self.state is None or refresh:
            self.state = self._observe()
            # The router only learns what the hardware can run once the machine
            # has been looked at, so this is the moment to tell it.
            accelerator = getattr(self.state, "accelerator", None)
            if accelerator is not None:
                self.router.accelerator = accelerator
                if not self.router.available:
                    self.router.available = set(self.ollama.models()) or None
        return self.state

    # -- prompting --------------------------------------------------------- #

    def _messages(self, request: str, gaps: Sequence[str],
                  extra: Sequence[dict[str, str]] = ()) -> list[dict[str, str]]:
        """Assemble the full message list for one generation."""
        state = self.observe()
        preamble = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": "Current machine state:\n" + state.render()},
        ]
        if gaps:
            preamble.append({
                "role": "system",
                "content": (
                    "A structural check of the operator's request found these "
                    "gaps. If any of them genuinely blocks you, use intent "
                    "\"ask\". If you can proceed anyway, say why in `message`:\n"
                    + "\n".join(f"- {g}" for g in gaps)),
            })
        return preamble + self.context.as_messages() + list(extra) + [
            {"role": "user", "content": request},
        ]

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Pull a JSON object out of a model reply."""
        # Models add markdown fences and preamble no matter what the prompt
        # says, so this strips fences and falls back to the outermost brace pair.
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                         flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError as exc:
                raise AgentError(f"model did not return valid JSON: {exc}") from exc
        raise AgentError("model returned no JSON object")

    # -- generation -------------------------------------------------------- #

    def converse(self, request: str, model: str | None = None) -> Turn:
        """Take one operator message and decide what to do with it."""
        # Returns a Turn whose intent is PLAN (validated TaskSpec attached),
        # ASK, ANSWER, or REFUSE. Raises OllamaError when the backend cannot
        # be reached; the caller should present ollama.recovery_options().
        started = time.perf_counter()
        self._turn_counter += 1

        self.observe()  # so routing knows the hardware before it picks
        clarification = self.clarifier.clarify(request)
        gaps = [f"{a.code}: {a.why}" for a in clarification.ambiguities]

        choice = self.router.select(request)
        chosen = model or choice.model
        if self.ollama.last_status.value != "healthy":
            available = self.ollama.models()
            if available and not model_available(chosen, available):
                chosen = self.ollama.fallback_model if model_available(
                    self.ollama.fallback_model, available) else available[0]

        raw = self.ollama.chat(chosen, self._messages(request, gaps), json_mode=True)
        turn = self._parse(request, raw, chosen, choice.reason, gaps,
                           bool(self.state))

        repairs = 0
        while (turn.intent is Intent.PLAN and turn.spec is not None
               and repairs < self.max_repairs):
            complaint = self._prescreen(turn.spec)
            if not complaint:
                break
            repairs += 1
            raw = self.ollama.chat(
                chosen,
                self._messages(request, gaps, extra=[
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content":
                     "That plan was rejected before reaching the operator:\n"
                     + complaint + "\nRevise it. Same JSON schema."},
                ]),
                json_mode=True)
            turn = self._parse(request, raw, chosen, choice.reason, gaps,
                               bool(self.state))
            turn.repairs = repairs

        turn.seconds = time.perf_counter() - started
        self._remember(turn)
        self.turns.append(turn)
        return turn

    def _parse(self, request: str, raw: str, model: str, routing_reason: str,
               gaps: Sequence[str], observed: bool) -> Turn:
        """Turn a raw model reply into a Turn, validating any proposed task."""
        data = self._extract_json(raw)
        intent_text = str(data.get("intent") or "plan").lower()
        try:
            intent = Intent(intent_text)
        except ValueError:
            intent = Intent.PLAN if data.get("task") else Intent.ANSWER

        turn = Turn(
            request=request, intent=intent,
            message=str(data.get("message") or ""),
            questions=[str(q) for q in (data.get("questions") or [])],
            model=model, routing_reason=routing_reason,
            clarifier_gaps=list(gaps), machine_observed=observed, raw=raw)

        if intent is Intent.PLAN:
            task = data.get("task")
            if not isinstance(task, dict):
                raise AgentError("intent was 'plan' but no task object was returned")
            task.setdefault("task_id", f"agent-{self._turn_counter}")
            task.setdefault("request", request)
            try:
                turn.spec = TaskSpec.validate(task, source=f"{model} proposal")
            except TaskSpecError as exc:
                raise AgentError(f"proposed task is malformed: {exc}") from exc
        return turn

    def _prescreen(self, spec: TaskSpec) -> str:
        """Check the model's own plan before an operator ever sees it."""
        # Not a substitute for the real gates, which still run on every
        # command. This exists so an obviously bad proposal gets one
        # automatic revision round instead of burning the operator's
        # attention on a plan that was never going to be approved.
        complaints: list[str] = []
        for step in spec.to_steps(dry_run=True):
            verdict = self.safety.classify(step.command)
            if verdict.level.value == "critical":
                complaints.append(
                    f"- step '{step.step_id}' is CRITICAL: {verdict.reasons[0]}. "
                    "Find a narrower command or a safer approach.")
            if verdict.level.value in ("dangerous", "critical") and not step.touches:
                complaints.append(
                    f"- step '{step.step_id}' is destructive but declares no "
                    "`touches`, so the harness cannot snapshot anything to "
                    "restore. Add the files it modifies.")
        gaps = self.clarifier.detect(spec.request)
        if gaps:
            complaints.append(
                "- the `request` line is underspecified: "
                + "; ".join(f"{g.code} ({g.why})" for g in gaps)
                + ". Restate it with concrete targets.")
        return "\n".join(complaints)

    def repair(self, turn: Turn, complaint: str,
               model: str | None = None) -> Turn:
        """Hand a gate rejection back to the model and return a revised Turn."""
        raw = self.ollama.chat(
            model or turn.model,
            self._messages(turn.request, turn.clarifier_gaps, extra=[
                {"role": "assistant", "content": turn.raw},
                {"role": "user", "content":
                 f"The harness rejected that plan:\n{complaint}\n"
                 "Revise it. Same JSON schema."},
            ]),
            json_mode=True)
        revised = self._parse(turn.request, raw, model or turn.model,
                              turn.routing_reason, turn.clarifier_gaps,
                              turn.machine_observed)
        revised.repairs = turn.repairs + 1
        self._remember(revised)
        self.turns.append(revised)
        return revised

    def answer_questions(self, turn: Turn, answers: Sequence[str],
                         model: str | None = None) -> Turn:
        """Feed one answer per question (in order) back and re-run the turn."""
        combined = "; ".join(
            f"{q} -> {a}" for q, a in zip(turn.questions, answers, strict=True) if a)
        return self.converse(f"{turn.request} [clarified: {combined}]",
                             model or turn.model)

    # -- conversation ------------------------------------------------------ #

    def _remember(self, turn: Turn) -> None:
        """Add the exchange to the compressed context window."""
        try:
            self.context.add(f"u{self._turn_counter}", "user", turn.request)
            summary = turn.message or turn.intent.value
            if turn.spec:
                summary += "\nProposed:\n" + "\n".join(
                    f"  {s['id'] if 'id' in s else '?'}: {s['run']}"
                    for s in turn.spec.steps)
            self.context.add(f"a{self._turn_counter}", "assistant", summary)
            self.context.maybe_compress()
        except Exception:
            pass  # context bookkeeping must never break a turn

    def reset(self) -> None:
        """Start a fresh conversation, keeping the observed machine state."""
        self.context.entries.clear()
        self.turns.clear()
        self._turn_counter = 0

    def stats(self) -> dict[str, Any]:
        """Counters, for the audit trail."""
        return {
            "turns": len(self.turns),
            "repairs": sum(t.repairs for t in self.turns),
            "context_tokens": self.context.total_tokens,
            "context_usage": round(self.context.usage_ratio, 3),
            "compressions": self.context.compressions,
            "ollama": self.ollama.stats(),
        }
