"""The model loop: turns a conversation into a validated TaskSpec, never a shell.

Depends on: harness.core (ContextCompressor, ContextStep, MachineState, ModelBackend,
OllamaClient, Step, StepResult, TaskSpec, observe_machine, resolve_backend,
build_ssh_runner, observe_executor, resolve_executor_config), harness.gates
(Clarifier, ModelRouter, SafetyClassifier), harness.prompts (PLAN_SCHEMA,
SYSTEM_PROMPT), harness.read_memory (ReadMemory).

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
    ContextStep,
    MachineState,
    ModelBackend,
    OllamaClient,
    Step,
    StepResult,
    TaskSpec,
    TaskSpecError,
    build_ssh_runner,
    observe_executor,
    observe_machine,
    resolve_executor_config,
)
from .core import (
    resolve_backend as _resolve_backend,
)
from .gates import Clarifier, ModelRouter, SafetyClassifier, model_available
from .prompts import PLAN_SCHEMA, SYSTEM_PROMPT
from .read_memory import ReadMemory

__all__ = ["Intent", "Turn", "Agent", "AgentError", "PLAN_SCHEMA", "SYSTEM_PROMPT"]


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
    anchors: list[str] = field(default_factory=list)
    machine_observed: bool = False
    repairs: int = 0
    seconds: float = 0.0
    raw: str = ""
    #: Paths of previously-read files the model says no longer help --
    #: `Agent.converse()` acts on this against `_read_context` right after
    #: parsing (§2.46). Kept on `Turn`, not consumed and discarded, so the
    #: audit trail records what the model chose to forget, not just that
    #: something happened to `self.context`.
    forget: list[str] = field(default_factory=list)

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
            "anchors": list(self.anchors),
            "machine_observed": self.machine_observed,
            "repairs": self.repairs,
            "seconds": round(self.seconds, 3),
            "forget": list(self.forget),
        }


def _observe_with_executor(
    ollama: OllamaClient, knowledge_dir: str | None,
    executor_config: tuple[str, str, str] | None,
) -> MachineState:
    """Today's observe_machine() call, plus the optional executor
    sub-observation attached afterward -- composition, not a change to what
    observe_machine() itself does (HANDOFF.md §2.52). `executor_config` is
    None (the common case) whenever HARNESS_EXECUTOR_HOST isn't set, and
    then this behaves exactly like the plain observe_machine() call it
    replaces -- no SSH is ever attempted."""
    state = observe_machine(ollama=ollama, knowledge_dir=knowledge_dir)
    if executor_config is not None:
        host, user, key_path = executor_config
        state.executor = observe_executor(build_ssh_runner(host, user, key_path))
    return state


class Agent:
    """Turns a conversation into a validated TaskSpec.

    The clarifier is a *tool the model consults*, not a wall in front of
    it: `Clarifier.detect()` runs before generation and its gaps are handed
    to the model, so a vague request gets one good question instead of a
    guess -- inverting the clarifier's role without changing its logic."""

    #: How many times to hand a rejection back to the model before giving up.
    MAX_REPAIRS: int = 2

    #: A message that only announces an intent to look something up, never
    #: the result -- "Checking system-library for X to answer your question."
    #: Requires both ends: an opening deferral verb alone would flag a real
    #: answer that happens to start with "Checking the logs, ..."; requiring
    #: it also close by naming the question/request itself is what tells
    #: "describes the act of answering" apart from "contains an answer".
    _DEFERRAL_OPENER = re.compile(
        r"^(?:(?:i(?:'ll|'m|\s+will|\s+am)\s+)?(?:going to\s+)?"
        r"(?:check|checking|search|searching|look|looking|verify|verifying|"
        r"determine|determining|read|reading)\b|let me\b)", re.IGNORECASE)
    _NAMES_THE_QUESTION = re.compile(
        r"\b(your |this |the )?(question|request)\.?[\"')]*$", re.IGNORECASE)

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
        knowledge_dir: str | None = None,
        on_context_change: Callable[[ContextStep], None] | None = None,
        task_id_prefix: str = "",
        backends: dict[str, ModelBackend] | None = None,
    ) -> None:
        """Wire the model client, router, clarifier, safety pre-screen, and context."""
        self.ollama = ollama
        self.router = router or ModelRouter()
        self.clarifier = clarifier or Clarifier()
        self.safety = safety or SafetyClassifier()
        self.context = compressor or ContextCompressor(max_tokens=24_000)
        self._executor_config = resolve_executor_config()
        self._observe = observer or (
            lambda: _observe_with_executor(ollama, knowledge_dir, self._executor_config))
        self.max_repairs = self.MAX_REPAIRS if max_repairs is None else int(max_repairs)
        self.system_prompt = system_prompt
        self._on_context_change = on_context_change
        self.task_id_prefix = task_id_prefix
        self.backends = dict(backends or {})

        self.state: MachineState | None = None
        self.turns: list[Turn] = []
        self._turn_counter: int = 0
        #: A read-only step's real output, tagged by path, once it's
        #: actually run -- lets a later `Turn.forget` find and remove
        #: exactly that entry from `self.context` (§2.46). `Harness`
        #: writes to this directly (`remember_step_reads`); `Agent` only
        #: reads from it, via `.forget()` below.
        self.read_memory = ReadMemory(self.context, on_change=self._on_context_change)

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
                # `refresh=True` means the caller wants current facts, not
                # cached ones -- an explicit `not refresh` here would only
                # re-populate this the very first time, which is exactly
                # the same short-circuit `router.available` already
                # provides. Found in a full-file audit: `refresh=True` has
                # no caller anywhere in this codebase today, so this was
                # latent, but wiring model_params_b/model_chat_capable
                # once and never again would leave them silently stale for
                # any future caller that pulls or deletes a model mid
                # session and then calls observe(refresh=True) expecting
                # the routing data to catch up too.
                if refresh or not self.router.available:
                    models = self.ollama.models()
                    self.router.available = set(models) or None
                    # Real size and chat capability per model, once per
                    # session -- see ModelRouter._params_b/_chat_capable
                    # for why guessing either from the name alone found a
                    # freshly-pulled embedding model (nomic-embed-text) as
                    # if it were the smallest, safest chat model to fall
                    # back to.
                    sizes: dict[str, float] = {}
                    capable: dict[str, bool] = {}
                    thinking: dict[str, bool] = {}
                    for name in models:
                        info = self.ollama.model_info(name)
                        if info.get("params_b"):
                            sizes[name] = info["params_b"]
                        if "chat_capable" in info:
                            capable[name] = info["chat_capable"]
                        if "thinking_capable" in info:
                            thinking[name] = info["thinking_capable"]
                    if sizes:
                        self.router.model_params_b = sizes
                        # Accelerator.fits() has its own independent
                        # name-guessing fallback (harness/core/machine.py)
                        # -- router.runnable() delegates the actual VRAM
                        # fit check to it, so the router's real sizes have
                        # to reach it too, or a model whose name carries no
                        # digit still gets waved through as "fits anywhere"
                        # one call later. Found live: fixing _params_b
                        # alone still let a 14.8B model onto a card with
                        # under half the VRAM it needed.
                        accelerator.model_params_b = sizes
                    if capable:
                        self.router.model_chat_capable = capable
                    if thinking:
                        self.router.model_thinking_capable = thinking
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
        candidate = Agent._find_json_object(cleaned)
        if candidate is not None:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise AgentError(f"model did not return valid JSON: {exc}") from exc
        raise AgentError("model returned no JSON object")

    @staticmethod
    def _find_json_object(text: str) -> str | None:
        """The first brace-balanced substring that also parses as JSON --
        found live: preamble like `${VAR}` before the real reply otherwise
        fools a naive find-first/find-last pair into the wrong object."""
        pos = 0
        while True:
            start = text.find("{", pos)
            if start < 0:
                return None
            end = Agent._matching_brace(text, start)
            if end is not None:
                candidate = text[start:end + 1]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass
            pos = start + 1

    @staticmethod
    def _matching_brace(text: str, start: int) -> int | None:
        """Index of the `}` closing the `{` at `start`, skipping braces inside string literals."""
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        return None

    # -- generation -------------------------------------------------------- #

    def _degraded_fallback(self, available: list[str]) -> str:
        """Prefers one the router knows can chat -- a bare `available[0]`
        pick used to reintroduce §2.9's embedding-only-model failure."""
        capable = [m for m in available if self.router._chat_capable(m)]
        candidates = capable or available
        return (self.ollama.fallback_model
                if model_available(self.ollama.fallback_model, candidates)
                else candidates[0])

    def converse(self, request: str, model: str | None = None,
                on_thinking: Callable[[str], None] | None = None) -> Turn:
        """Take one operator message and decide what to do with it.

        `on_thinking`, when given, is only ever handed down to `backend.chat`
        for a model `router._thinking_capable` marks capable -- a remote
        `openrouter/...` override, or a local model with no measured
        thinking support, silently gets no callback at all, same contract
        `OllamaClient.chat` itself has for `on_thinking=None`.
        """
        # Returns a Turn whose intent is PLAN (validated TaskSpec attached),
        # ASK, ANSWER, or REFUSE. Raises OllamaError when the backend cannot
        # be reached; the caller should present ollama.recovery_options().
        started = time.perf_counter()
        self._turn_counter += 1

        self.observe()  # so routing knows the hardware before it picks
        clarification = self.clarifier.clarify(request)
        gaps = [f"{a.code}: {a.why}" for a in clarification.ambiguities]
        anchors = clarification.anchors

        choice = self.router.select(request, ambiguous=bool(gaps))
        chosen = model or choice.model
        # self.router never returns a prefixed model, so an alt-backend
        # choice only ever comes from an explicit operator override -- the
        # degraded-daemon fallback below is entirely about the *local
        # Ollama catalog's* availability and must not run for one, or it
        # would try to swap an explicit OpenRouter choice for a local model.
        is_alt_backend = any(chosen.startswith(f"{p}/") for p in self.backends)
        if not is_alt_backend and self.ollama.last_status.value != "healthy":
            available = self.ollama.models()
            if available and not model_available(chosen, available):
                chosen = self._degraded_fallback(available)

        backend, real_model = self.resolve_backend(chosen)
        stream_cb = (on_thinking if on_thinking is not None
                    and not is_alt_backend and self.router._thinking_capable(real_model)
                    else None)
        raw = backend.chat(real_model, self._messages(request, gaps), json_mode=True,
                           on_thinking=stream_cb)
        turn = self._parse(request, raw, chosen, choice.reason, gaps, anchors,
                           bool(self.state))

        repairs = 0
        while repairs < self.max_repairs:
            complaint = self._repair_complaint(turn)
            if not complaint:
                break
            repairs += 1
            raw = backend.chat(
                real_model,
                self._messages(request, gaps, extra=[
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content":
                     "That response needs a revision before reaching the operator:\n"
                     + complaint + "\nRevise it. Same JSON schema."},
                ]),
                json_mode=True, on_thinking=stream_cb)
            turn = self._parse(request, raw, chosen, choice.reason, gaps, anchors,
                               bool(self.state))
            turn.repairs = repairs

        turn.seconds = time.perf_counter() - started
        if turn.forget:
            self.read_memory.forget(turn.forget)
        self._remember(turn)
        self.turns.append(turn)
        return turn

    def _parse(self, request: str, raw: str, model: str, routing_reason: str,
               gaps: Sequence[str], anchors: Sequence[str], observed: bool) -> Turn:
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
            clarifier_gaps=list(gaps), anchors=list(anchors),
            machine_observed=observed, raw=raw,
            forget=[str(p) for p in (data.get("forget") or [])])

        if intent is Intent.PLAN:
            task = data.get("task")
            if not isinstance(task, dict):
                raise AgentError("intent was 'plan' but no task object was returned")
            task.setdefault("task_id", f"{self.task_id_prefix}agent-{self._turn_counter}")
            task.setdefault("request", request)
            try:
                turn.spec = TaskSpec.validate(task, source=f"{model} proposal")
            except TaskSpecError as exc:
                raise AgentError(f"proposed task is malformed: {exc}") from exc
        return turn

    def _repair_complaint(self, turn: Turn) -> str:
        """What, if anything, needs fixing before this turn reaches the operator."""
        if turn.intent is Intent.PLAN and turn.spec is not None:
            return self._prescreen(turn.spec)
        if turn.intent is Intent.ANSWER:
            return self._answer_dropped_a_task(turn) or self._answer_is_a_placeholder(turn)
        return ""

    def _answer_is_a_placeholder(self, turn: Turn) -> str:
        """Complaint when `message` describes looking something up, not the result."""
        message = turn.message.strip()
        if not message or not (self._DEFERRAL_OPENER.match(message)
                               and self._NAMES_THE_QUESTION.search(message)):
            return ""
        return (
            "- intent was 'answer' but `message` only describes checking "
            "something, it never states what was actually found. Put the "
            "real answer directly in `message`; if you have not looked "
            "anything up yet, use intent 'plan' to propose the step that will.")

    def _answer_dropped_a_task(self, turn: Turn) -> str:
        """Complaint when intent was 'answer' but a `task` was also silently
        discarded (`_parse` only reads `task` for 'plan') -- found live."""
        try:
            data = self._extract_json(turn.raw)
        except AgentError:
            return ""
        if not isinstance(data.get("task"), dict):
            return ""
        return (
            "- intent was 'answer' but a `task` object was also attached, "
            "which the harness never runs for an answer -- the operator "
            "sees only your `message` and nothing else, so anything in "
            "`task` (including steps meant to produce the real values) is "
            "silently lost. Put the full answer -- real values from the "
            "observed machine state, not a promise to look them up -- "
            "directly in `message`, and drop `task` entirely. If you "
            "actually need to run something to produce the answer, use "
            "intent 'plan' instead.")

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
        backend, real_model = self.resolve_backend(model or turn.model)
        raw = backend.chat(
            real_model,
            self._messages(turn.request, turn.clarifier_gaps, extra=[
                {"role": "assistant", "content": turn.raw},
                {"role": "user", "content":
                 f"The harness rejected that plan:\n{complaint}\n"
                 "Revise it. Same JSON schema."},
            ]),
            json_mode=True)
        revised = self._parse(turn.request, raw, model or turn.model,
                              turn.routing_reason, turn.clarifier_gaps,
                              turn.anchors, turn.machine_observed)
        revised.repairs = turn.repairs + 1
        if revised.forget:
            self.read_memory.forget(revised.forget)
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

    # -- reporting ----------------------------------------------------------- #

    def summarize(self, request: str, steps: Sequence[Step],
                  results: Sequence[StepResult], model: str) -> str:
        """Turn already-executed step output into the plain-language answer
        the operator asked for -- a single fixed-shape call, not a tool-use
        loop: the reply is plain text, never parsed as JSON or a task."""
        by_id = {r.step_id: r for r in results}
        lines = []
        for step in steps:
            result = by_id.get(step.step_id)
            if result is None:
                continue
            outcome = result.output if result.ok else f"FAILED: {result.error}"
            lines.append(f"- {step.step_id} (`{step.command}`): {outcome[:2000]}")
        prompt = (
            f"The operator asked: {request}\n\n"
            "These steps already ran; here is exactly what each returned:\n"
            + "\n".join(lines) +
            "\n\nWrite a short, direct answer to the operator's question using "
            "only what is shown above. This is a report, not a plan -- do not "
            "propose any further command.")
        backend, real_model = self.resolve_backend(model)
        return backend.chat(
            real_model, [{"role": "user", "content": prompt}], json_mode=False).strip()

    # -- conversation ------------------------------------------------------ #

    def _remember(self, turn: Turn) -> None:
        """Add the exchange to the compressed context window."""
        try:
            user_step = self.context.add(f"u{self._turn_counter}", "user", turn.request)
            if self._on_context_change:
                self._on_context_change(user_step)
            summary = turn.message or turn.intent.value
            if turn.spec:
                summary += "\nProposed:\n" + "\n".join(
                    f"  {s['id'] if 'id' in s else '?'}: {s['run']}"
                    for s in turn.spec.steps)
            assistant_step = self.context.add(f"a{self._turn_counter}", "assistant", summary)
            if self._on_context_change:
                self._on_context_change(assistant_step)
            self.context.maybe_compress()
        except Exception:
            pass  # context bookkeeping must never break a turn

    def reset(self) -> None:
        """Start a fresh conversation, keeping the observed machine state."""
        self.context.entries.clear()
        self.turns.clear()
        self._turn_counter = 0
        self.read_memory.reset()

    def fast_forward_turn_counter(self, at_least: int) -> None:
        """Never backwards -- keeps a reopened session reusing old task_ids (§2.26)."""
        self._turn_counter = max(self._turn_counter, at_least)

    def resolve_backend(self, model: str) -> tuple[ModelBackend, str]:
        """See `harness.core.backend.resolve_backend` -- thin instance wrapper."""
        return _resolve_backend(model, self.backends, self.ollama)

    def stats(self) -> dict[str, Any]:
        """Counters, for the audit trail."""
        return {
            "turns": len(self.turns),
            "repairs": sum(t.repairs for t in self.turns),
            "context_tokens": self.context.total_tokens,
            "context_usage": round(self.context.usage_ratio, 3),
            "compressions": self.context.compressions,
            "ollama": self.ollama.stats(),
            "backends": {prefix: backend.stats() for prefix, backend in self.backends.items()},
        }
