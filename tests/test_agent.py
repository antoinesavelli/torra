"""
Agent loop: the part that was missing for six phases.

Organised by what the agent has to get right, not by which phase built it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import plan_reply, scripted

from harness.agent import SYSTEM_PROMPT, Agent, AgentError, Intent, Turn
from harness.core import (
    MachineState,
    OllamaClient,
    OllamaError,
    OllamaStatus,
    Step,
    StepResult,
    TaskSpec,
)


def build_agent(transport, **kwargs) -> Agent:
    """An Agent wired to a scripted backend with a fixed machine state."""
    agent = Agent(OllamaClient(transport=transport, probe_timeout=0.1), **kwargs)
    agent._observe = lambda: MachineState(hostname="pop-os")
    return agent


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def test_the_client_actually_generates() -> None:
    """The defect that defined six phases: nothing ever called the model.

    This asserts a POST to /api/chat happens with the conversation attached.
    Everything else in this file is downstream of that one call existing.
    """
    seen = []

    def transport(path, payload, timeout):
        seen.append((path, payload))
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content": json.dumps(
            {"intent": "answer", "message": "40 GB free on /"})}}

    agent = build_agent(transport)
    turn = agent.converse("how much disk is free on /")

    chat_calls = [p for p, _ in seen if p == "/api/chat"]
    assert chat_calls, "the model was never asked anything"
    _path, payload = next((p, pl) for p, pl in seen if p == "/api/chat")
    assert payload["model"]
    assert payload["stream"] is False
    assert payload["format"] == "json", "a plan must be constrained to JSON"
    assert any(m["role"] == "user" and "disk" in m["content"]
               for m in payload["messages"])
    assert turn.intent is Intent.ANSWER


def test_a_plan_becomes_a_validated_taskspec() -> None:
    """The model's output travels the same path a hand-written file does."""
    agent = build_agent(scripted(plan_reply(
        "set-workers", "set workers to 4 in /srv/api/app.conf",
        [{"id": "write", "kind": "write",
          "run": "printf 'workers = 4\\n' > /srv/api/app.conf",
          "touches": ["/srv/api/app.conf"], "estimated_seconds": 3}])))

    turn = agent.converse("set the worker count to 4 in /srv/api/app.conf")

    assert turn.intent is Intent.PLAN
    assert isinstance(turn.spec, TaskSpec)
    assert turn.spec.commands() == ["printf 'workers = 4\\n' > /srv/api/app.conf"]
    steps = turn.spec.to_steps(dry_run=True)
    assert steps[0].command == turn.spec.commands()[0], (
        "what gets classified must be what gets run"
    )
    assert steps[0].touches == ["/srv/api/app.conf"]


def test_machine_state_reaches_the_prompt() -> None:
    """A model told what is installed stops inventing plausible commands."""
    captured = {}

    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        captured["messages"] = payload["messages"]
        return {"message": {"content": json.dumps(
            {"intent": "answer", "message": "ok"})}}

    agent = build_agent(transport)
    agent._observe = lambda: MachineState(hostname="pop-os", disk="40G free",
                                          ollama_models=["qwen3:32b"])
    agent.converse("what is this machine")

    system = " ".join(m["content"] for m in captured["messages"]
                      if m["role"] == "system")
    assert "pop-os" in system
    assert "40G free" in system
    assert "qwen3:32b" in system


# --------------------------------------------------------------------------- #
# The clarifier as a tool, not a wall
# --------------------------------------------------------------------------- #


def test_clarifier_gaps_are_handed_to_the_model() -> None:
    """The inversion. Six phases used this to block; now it informs.

    The clarifier no longer refuses on the operator's behalf. It tells the model
    exactly what structure is missing so the model can ask one good question
    instead of guessing, which is a better question than a regex can write.
    """
    captured = {}

    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        captured["messages"] = payload["messages"]
        return {"message": {"content": json.dumps(
            {"intent": "ask", "message": "Which box?",
             "questions": ["Which host do you mean?"]})}}

    agent = build_agent(transport)
    turn = agent.converse("tidy up the box")

    system = " ".join(m["content"] for m in captured["messages"]
                      if m["role"] == "system")
    assert "no_target" in system, "the model was not told what was missing"
    assert "ambiguous_referent" in system
    assert turn.intent is Intent.ASK
    assert turn.questions == ["Which host do you mean?"]
    assert turn.clarifier_gaps


def test_a_specific_request_carries_no_gaps() -> None:
    """A well formed request should not nag the model with phantom problems."""
    captured = {}

    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "devstral"}]}
        captured["messages"] = payload["messages"]
        return {"message": {"content": json.dumps(plan_reply(
            "t", "restart ollama.service on popos-01 and confirm it is active",
            [{"id": "r", "run": "systemctl --user restart ollama"}]))}}

    agent = build_agent(transport)
    turn = agent.converse(
        "restart ollama.service on popos-01 and confirm it reports active")

    assert turn.clarifier_gaps == []
    system = " ".join(m["content"] for m in captured["messages"]
                      if m["role"] == "system")
    assert "structural check" not in system


def test_answering_questions_re_plans() -> None:
    """An ASK turn plus answers becomes a plan."""
    agent = build_agent(scripted(
        {"intent": "ask", "message": "which host?",
         "questions": ["Which host?"]},
        plan_reply("t", "restart ollama.service on popos-01",
                   [{"id": "r", "run": "systemctl --user restart ollama"}])))

    asked = agent.converse("restart the server")
    assert asked.intent is Intent.ASK

    planned = agent.answer_questions(asked, ["popos-01"])
    assert planned.intent is Intent.PLAN
    assert "popos-01" in planned.request


# --------------------------------------------------------------------------- #
# Guarding against the model itself
# --------------------------------------------------------------------------- #


def test_a_critical_proposal_is_repaired_before_the_operator_sees_it() -> None:
    """The model gets one automatic revision round, not a free pass.

    Not a substitute for the real gates, which run on every command regardless.
    This exists so an obviously unapprovable plan does not burn an operator's
    attention.
    """
    agent = build_agent(scripted(
        plan_reply("wipe", "remove the postgres data directory",
                   [{"id": "rm", "run": "rm -rf /var/lib/postgresql"}]),
        plan_reply("safe", "move /var/lib/postgresql to a dated backup path",
                   [{"id": "mv",
                     "run": "mv /var/lib/postgresql /var/lib/postgresql.bak-2026-07-27",
                     "touches": ["/var/lib/postgresql"]}])))

    turn = agent.converse("clear out /var/lib/postgresql")

    assert turn.repairs >= 1, "a CRITICAL proposal should have been sent back"
    assert turn.spec is not None
    assert "rm -rf" not in " ".join(turn.spec.commands())


def test_a_destructive_step_without_touches_is_repaired() -> None:
    """No `touches` means no snapshot means no undo. That is worth one round."""
    agent = build_agent(scripted(
        plan_reply("t", "remove /srv/api/old.log",
                   [{"id": "rm", "run": "rm /srv/api/old.log"}]),
        plan_reply("t", "remove /srv/api/old.log",
                   [{"id": "rm", "run": "rm /srv/api/old.log",
                     "touches": ["/srv/api/old.log"]}])))

    turn = agent.converse("delete /srv/api/old.log")

    assert turn.repairs >= 1
    assert turn.spec is not None
    assert turn.spec.steps[0]["touches"] == ["/srv/api/old.log"]


def test_repair_gives_up_rather_than_looping() -> None:
    """A model that will not improve must not spin forever."""
    bad = plan_reply("t", "wipe it", [{"id": "rm", "run": "rm -rf /var/lib/x"}])
    agent = build_agent(scripted(bad), max_repairs=2)

    turn = agent.converse("wipe /var/lib/x")

    assert turn.repairs == 2, "should stop at max_repairs"
    assert turn.spec is not None, "the last proposal is still returned for gating"


def test_repair_sends_the_gates_own_complaint_back_and_returns_a_revised_turn() -> None:
    """`repair()` is the public entry point a gate rejection calls after
    the operator has already seen and approved a plan that a *later*
    gate (state/conflict, not the model's own pre-screen) then rejected
    -- distinct from the automatic in-converse() repair loop, which never
    fires here because the original proposal is not itself flagged."""
    original = plan_reply("t", "check disk usage on /srv/api",
                          [{"id": "check", "run": "df -h /srv/api"}])
    revised = plan_reply("t", "check disk usage on /srv/api",
                         [{"id": "check", "run": "df -h /srv/api2"}])
    agent = build_agent(scripted(original, revised))
    turn = agent.converse("check disk usage on /srv/api")
    assert turn.repairs == 0, "the original proposal should not need auto-repair"

    fixed = agent.repair(turn, "- the target path no longer exists")

    assert fixed.repairs == turn.repairs + 1
    assert fixed.spec is not None
    assert fixed.spec.commands() == ["df -h /srv/api2"]
    assert fixed in agent.turns


def test_an_answer_that_silently_drops_a_populated_task_is_repaired() -> None:
    """Found live: asked to summarize disk usage, the model replied intent
    "answer" with a real task attached (steps that would have run `df`)
    -- _parse only reads `task` for intent "plan", so the operator saw
    just the announcement ("here is the summary") and none of the actual
    numbers, discarded along with the task."""
    agent = build_agent(scripted(
        {"intent": "answer",
         "message": "Here is the storage summary from the observed machine state.",
         "task": {"task_id": "x", "request": "x",
                  "steps": [{"id": "s", "run": "df -h"}]}},
        {"intent": "answer", "message": "ParamoStorage: 584GB free of 814GB."}))

    turn = agent.converse("give me a summary of storage")

    assert turn.repairs >= 1
    assert turn.intent is Intent.ANSWER
    assert "584GB" in turn.message


def test_an_answer_with_an_empty_task_scaffold_is_also_repaired() -> None:
    """Same habit, different shape: no steps to lose this time, but the
    message was just as vague ("here is the summary...") with no real
    content either -- attaching a task object at all for intent "answer"
    is itself the tell, not just a populated one."""
    agent = build_agent(scripted(
        {"intent": "answer",
         "message": "Here is the storage summary from your machine state.",
         "task": {"task_id": "storage-summary", "request": "x",
                  "steps": [], "rollback": []}},
        {"intent": "answer", "message": "ParamoStorage: 584GB free of 814GB."}))

    turn = agent.converse("give me a summary of storage")

    assert turn.repairs >= 1
    assert "584GB" in turn.message


def test_a_clean_answer_with_no_task_never_wastes_a_repair_round() -> None:
    """The new check must not fire on ordinary, correct answer turns."""
    agent = build_agent(scripted(
        {"intent": "answer", "message": "ParamoStorage: 584GB free."}))

    turn = agent.converse("give me a summary of storage")

    assert turn.repairs == 0


def test_answer_dropped_a_task_check_tolerates_unparseable_raw() -> None:
    """`_answer_dropped_a_task` re-extracts JSON from `turn.raw` rather
    than caching what `_parse` already found -- in normal operation that
    string always parsed once already, so this is a defensive catch for
    a `Turn` built some other way. Calls the private method directly
    since there is no way to reach it through `converse()` with an
    already-invalid `raw`."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}))
    turn = Turn(request="r", intent=Intent.ANSWER, raw="not json at all")

    assert agent._answer_dropped_a_task(turn) == ""


def test_an_answer_that_only_promises_to_check_is_repaired() -> None:
    """Found live: asked whether SSH keys were configured, a reply was
    intent "answer" with no `task` at all (so `_answer_dropped_a_task`
    never fired) and `message` = "Checking system library documentation
    and actual SSH key presence on this machine to answer your question."
    -- no repair round caught it either, and the operator saw that
    sentence as the final answer with no SSH information anywhere in it."""
    agent = build_agent(scripted(
        {"intent": "answer",
         "message": ("Checking system library documentation and actual SSH "
                     "key presence on this machine to answer your question.")},
        {"intent": "answer", "message": "Yes: id_ed25519 (SHA256:Rx1e...)."}))

    turn = agent.converse("do I have SSH keys configured?")

    assert turn.repairs >= 1
    assert "id_ed25519" in turn.message


def test_a_short_answer_that_happens_to_open_with_checking_is_not_flagged() -> None:
    """The opening verb alone is not the tell -- a real answer that starts
    with "Checking the logs, ..." must not burn a repair round just
    because it shares a word with the placeholder pattern."""
    agent = build_agent(scripted(
        {"intent": "answer",
         "message": "Checking the logs, no errors were found in the last 24 hours."}))

    turn = agent.converse("were there any errors overnight?")

    assert turn.repairs == 0


def test_rule_6_no_longer_flatly_forbids_planning_for_a_question() -> None:
    """Found live, real traffic: rule 6 used to say "Do not plan a task"
    for any question about the machine -- which pushed the model toward
    telling the operator to run a safe read-only command themselves
    ('please run lsblk/df -h yourself') instead of the harness just
    running it, even though nothing in the *code* ever forbade intent
    "plan" for a question -- this was a pure prompt-wording gap, not a
    capability one. Live A/B tested against the real model before
    shipping (harness/agent.py's own history, not a scripted fake, since
    a scripted transport can't show whether prompt wording changes real
    model behavior): 3 of 4 real diagnostic-deflection phrases moved from
    "ask"/deflect to a real read-only plan, arithmetic and greetings
    stayed correctly answer-only. This test only pins the actual wording
    so a future edit can't quietly reintroduce the old, stricter rule."""
    assert "Do not plan a task" not in SYSTEM_PROMPT
    assert 'use intent "plan" with just that' in SYSTEM_PROMPT
    assert "telling the operator to run it themselves" in SYSTEM_PROMPT


def test_prompts_module_keeps_the_schema_the_model_sees_in_sync_with_the_prompt() -> None:
    """harness.prompts holds PLAN_SCHEMA and SYSTEM_PROMPT (split out of
    agent.py, which mixes prompt copy with conversation/repair-loop logic
    -- see prompts.py's own docstring for the real, measured LOC reason).
    Imported directly here, not just via harness.agent's re-export, so
    this module is exercised on its own. Pins the actual coupling PLAN_SCHEMA's
    own comment promises: SYSTEM_PROMPT is built by appending it verbatim,
    so the schema the model is told about and the schema TaskSpec.validate
    enforces can't drift apart."""
    from harness.prompts import PLAN_SCHEMA
    from harness.prompts import SYSTEM_PROMPT as prompts_system_prompt

    assert prompts_system_prompt.endswith(PLAN_SCHEMA)
    assert prompts_system_prompt is SYSTEM_PROMPT, \
        "harness.agent's SYSTEM_PROMPT must be the same object, not a copy"


# --------------------------------------------------------------------------- #
# Malformed model output
# --------------------------------------------------------------------------- #


def test_markdown_fences_are_survivable() -> None:
    """Models add fences no matter what the prompt says."""
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content":
                "```json\n" + json.dumps(
                    {"intent": "answer", "message": "40 GB free"}) + "\n```"}}

    assert build_agent(transport).converse("disk free?").intent is Intent.ANSWER


def test_preamble_before_the_json_is_survivable() -> None:
    """Some models narrate first. Take the outermost brace pair."""
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content":
                "Sure, here is the plan:\n"
                + json.dumps({"intent": "answer", "message": "ok"})
                + "\nLet me know if that helps."}}

    assert build_agent(transport).converse("hi").intent is Intent.ANSWER


def test_preamble_with_its_own_braces_does_not_confuse_the_json_pull() -> None:
    """Found live: this is an OS-engineering agent, so its own preamble
    routinely says things like "check {something}" or "run awk '{ print $1
    }'" before the real JSON reply -- prose that is itself brace-balanced.
    Grabbing the first balanced brace pair (or the outermost one) used to
    span from that prose straight to the real JSON's closing brace and fail
    to parse; the fix tries each `{` in turn and keeps the first one whose
    balanced span actually parses as JSON."""
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content":
                "I will check {something} first, then use ${HOME}.\n"
                + json.dumps({"intent": "answer", "message": "ok"})}}

    assert build_agent(transport).converse("hi").intent is Intent.ANSWER


def test_an_escaped_quote_inside_the_json_does_not_break_brace_matching() -> None:
    """`_matching_brace` has to skip over braces (and everything else)
    inside a string literal, including one that itself contains an
    escaped quote -- a naive scan would end the string early at the
    escaped `\\"` and lose track of whether it is still inside one."""
    raw = ('noise before {"intent": "answer", "message": "she said \\"hi\\""} '
          "noise after")
    data = Agent._extract_json(raw)
    assert data == {"intent": "answer", "message": 'she said "hi"'}


def test_an_unbalanced_brace_is_reported_as_no_json_object() -> None:
    with pytest.raises(AgentError, match="no JSON object"):
        Agent._extract_json("some unbalanced { text with no closing brace")


def test_an_unrecognized_intent_with_a_task_falls_back_to_plan() -> None:
    """`Intent(intent_text)` raises for anything outside the enum; a task
    object attached anyway is a stronger signal than the label the model
    picked, so it is treated as a plan rather than discarded."""
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content": json.dumps({
            "intent": "not-a-real-intent", "message": "ok",
            "task": {"task_id": "t", "request": "r",
                     "steps": [{"id": "a", "run": "true"}]}})}}

    turn = build_agent(transport).converse("do something")
    assert turn.intent is Intent.PLAN


def test_an_unrecognized_intent_with_no_task_falls_back_to_answer() -> None:
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content": json.dumps(
            {"intent": "not-a-real-intent", "message": "ok"})}}

    turn = build_agent(transport).converse("do something")
    assert turn.intent is Intent.ANSWER


def test_intent_plan_with_a_non_dict_task_is_rejected() -> None:
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content": json.dumps(
            {"intent": "plan", "message": "ok", "task": "not a dict"})}}

    with pytest.raises(AgentError, match="no task object"):
        build_agent(transport).converse("do something")


def test_unparseable_output_raises_rather_than_guessing() -> None:
    """Silently inventing a plan from garbage would be the worst failure here."""
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content": "I am afraid I cannot do that, Dave."}}

    with pytest.raises(AgentError, match="JSON"):
        build_agent(transport).converse("do something")


def test_a_malformed_task_is_rejected_not_executed() -> None:
    """The model does not get to bypass TaskSpec validation."""
    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content": json.dumps({
            "intent": "plan", "message": "ok",
            "task": {"task_id": "t", "request": "r",
                     "steps": [{"id": "a", "shell": "rm -rf /"}]}})}}

    with pytest.raises(AgentError, match="malformed"):
        build_agent(transport).converse("do something to /srv/api")


def test_backend_failure_surfaces_recovery_options() -> None:
    """An unreachable daemon is reported with the three ways out."""
    def transport(path, payload, timeout):
        raise ConnectionRefusedError("connection refused")

    agent = build_agent(transport)
    with pytest.raises(OllamaError):
        agent.converse("anything")

    options = agent.ollama.recovery_options("qwen3:32b")
    assert [o.key for o in options] == ["restart", "fallback", "manual"]
    assert "systemctl" in options[0].action


# --------------------------------------------------------------------------- #
# Conversation
# --------------------------------------------------------------------------- #


def test_conversation_is_kept_and_compressed() -> None:
    """The compressor was orphaned for six phases. It now holds the dialogue."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}))
    agent.context.max_tokens = 400

    for i in range(30):
        agent.converse(f"question number {i} about /srv/api and the config")

    assert agent.context.total_tokens <= agent.context.max_tokens
    assert agent.context.compressions >= 1
    assert any(e.compressed for e in agent.context.entries)
    assert len(agent.turns) == 30


def test_reset_clears_the_conversation_but_keeps_the_machine() -> None:
    """Observation is expensive and does not change when the topic does."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}))
    agent.converse("first question about /srv/api")
    agent.observe()

    agent.reset()

    assert agent.turns == []
    assert agent.context.entries == []
    assert agent.state is not None, "machine state should survive a topic change"


def test_on_context_change_fires_twice_per_turn() -> None:
    """Once for the user's request, once for the assistant's reply --
    the two self.context.add() calls _remember() already makes."""
    seen: list[tuple[str, str]] = []
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}),
                        on_context_change=lambda step: seen.append((step.role, step.content)))

    agent.converse("what is the disk usage")

    assert [role for role, _ in seen] == ["user", "assistant"]
    assert seen[0][1] == "what is the disk usage"
    assert seen[1][1] == "ok"


def test_on_context_change_fires_again_on_a_second_turn() -> None:
    calls = {"n": 0}
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}),
                        on_context_change=lambda step: calls.__setitem__("n", calls["n"] + 1))

    agent.converse("first question")
    agent.converse("second question")

    assert calls["n"] == 4


def test_a_raising_on_context_change_does_not_break_the_turn() -> None:
    """Context bookkeeping (and anything hooked into it) must never break
    a turn -- the same invariant _remember()'s own try/except already
    protects, just extended to cover an external observer too."""
    def broken_callback(step) -> None:
        raise RuntimeError("simulated persistence failure")

    agent = build_agent(scripted({"intent": "answer", "message": "ok"}),
                        on_context_change=broken_callback)

    turn = agent.converse("what is the disk usage")

    assert turn.intent is Intent.ANSWER, "the turn itself must still complete"


def test_no_on_context_change_is_the_default_and_is_fine() -> None:
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}))
    turn = agent.converse("what is the disk usage")
    assert turn.intent is Intent.ANSWER


def _reply_with_no_task_id(request: str, steps: list[dict]) -> dict:
    """A well-formed plan reply whose task object has no `task_id` key at
    all -- distinct from `plan_reply("", ...)`, which sets the key to an
    empty string and so would never trigger the fallback's own
    `setdefault` (a key that is present, even falsy, is not absent)."""
    return {"intent": "plan", "message": "here is the plan",
           "task": {"request": request, "steps": steps}}


def test_task_id_prefix_reaches_the_default_task_id() -> None:
    agent = build_agent(scripted(_reply_with_no_task_id(
        "create /srv/api/marker",
        [{"id": "touch", "run": "touch /srv/api/marker"}])),
        task_id_prefix="abc12345-")

    turn = agent.converse("create /srv/api/marker")

    assert turn.spec is not None
    assert turn.spec.task_id == "abc12345-agent-1"


def test_task_id_prefix_does_not_touch_a_model_chosen_task_id() -> None:
    agent = build_agent(scripted(plan_reply(
        "model-chosen-id", "create /srv/api/marker",
        [{"id": "touch", "run": "touch /srv/api/marker"}])),
        task_id_prefix="abc12345-")

    turn = agent.converse("create /srv/api/marker")

    assert turn.spec is not None
    assert turn.spec.task_id == "model-chosen-id"


def test_empty_task_id_prefix_is_the_default_and_matches_old_behavior() -> None:
    agent = build_agent(scripted(_reply_with_no_task_id(
        "create /srv/api/marker",
        [{"id": "touch", "run": "touch /srv/api/marker"}])))

    turn = agent.converse("create /srv/api/marker")

    assert turn.spec is not None
    assert turn.spec.task_id == "agent-1"


# --------------------------------------------------------------------------- #
# Backend dispatch (opt-in alternate model backends)
# --------------------------------------------------------------------------- #


class _FakeBackend:
    """A minimal ModelBackend, for proving dispatch without a real OpenRouterClient."""

    def __init__(self, reply: str = '{"intent": "answer", "message": "ok"}') -> None:
        self.calls: list[str] = []
        self.reply = reply

    def chat(self, model: str, messages: object, json_mode: bool = False,
              temperature: float = 0.2,
              on_thinking: Any | None = None) -> str:
        self.calls.append(model)
        return self.reply

    def recovery_options(self, current_model: str = "unknown") -> list[Any]:
        return []

    def stats(self) -> dict[str, Any]:
        return {"calls": len(self.calls)}


def test_resolve_backend_returns_the_registered_backend_for_a_matching_prefix() -> None:
    fake = _FakeBackend()
    agent = build_agent(scripted({}), backends={"openrouter": fake})
    backend, real_model = agent.resolve_backend("openrouter/anthropic/claude-3.5-sonnet")
    assert backend is fake
    assert real_model == "anthropic/claude-3.5-sonnet"


def test_resolve_backend_falls_through_to_ollama_with_no_matching_prefix() -> None:
    agent = build_agent(scripted({}), backends={"openrouter": _FakeBackend()})
    backend, real_model = agent.resolve_backend("devstral:24b")
    assert backend is agent.ollama
    assert real_model == "devstral:24b"


def test_resolve_backend_with_no_backends_registered_always_uses_ollama() -> None:
    agent = build_agent(scripted({}))
    backend, real_model = agent.resolve_backend("openrouter/anthropic/claude-3.5-sonnet")
    assert backend is agent.ollama
    assert real_model == "openrouter/anthropic/claude-3.5-sonnet"


def test_an_explicit_alt_backend_model_is_used_for_the_actual_chat_call() -> None:
    """ollama's own transport must never be hit -- an explicit openrouter/...
    model bypasses it entirely, all the way through to the real chat() call."""
    fake = _FakeBackend()

    def ollama_must_not_be_called(path: str, payload: dict, timeout: float) -> dict:
        if path == "/api/chat":
            raise AssertionError("ollama.chat() must not be called for an alt-backend model")
        return {"models": []} if path == "/api/tags" else {}

    agent = build_agent(ollama_must_not_be_called, backends={"openrouter": fake})

    turn = agent.converse("what is the disk usage",
                          model="openrouter/anthropic/claude-3.5-sonnet")

    assert fake.calls == ["anthropic/claude-3.5-sonnet"]
    assert turn.model == "openrouter/anthropic/claude-3.5-sonnet", \
        "the full prefixed string stays visible for audit transparency"


def test_the_degraded_fallback_does_not_fire_for_an_alt_backend_model() -> None:
    """The local-catalog degraded fallback is irrelevant to an explicit alt
    backend choice -- it must not swap it out even when ollama looks unhealthy."""
    fake = _FakeBackend()
    agent = build_agent(scripted({}, models=["devstral:24b"]),
                        backends={"openrouter": fake})
    agent.ollama.last_status = OllamaStatus.UNREACHABLE

    turn = agent.converse("what is the disk usage",
                          model="openrouter/anthropic/claude-3.5-sonnet")

    assert turn.model == "openrouter/anthropic/claude-3.5-sonnet"
    assert fake.calls == ["anthropic/claude-3.5-sonnet"]


def _transport_with_thinking_model(thinking_model: str, reply: dict[str, Any]):
    """Like `scripted()`, but `/api/show` reports `thinking_model` as
    thinking-capable -- `scripted()` itself always returns `{}` for
    `/api/show`, which is exactly the "unknown" case `_thinking_capable`
    defaults to False for, so a dedicated transport is needed to exercise
    the True path at all."""
    def transport(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if path == "/api/tags":
            return {"models": [{"name": thinking_model}]}
        if path == "/api/show":
            capable = payload.get("name") == thinking_model
            return {"capabilities": ["completion", "thinking"] if capable
                    else ["completion"]}
        return {"message": {"content": json.dumps(reply)}}
    return transport


def test_converse_passes_a_real_callback_for_a_thinking_capable_model() -> None:
    seen: dict[str, Any] = {}

    def fake_chat(model: str, messages: object, json_mode: bool = False,
                  temperature: float = 0.2, on_thinking: Any | None = None) -> str:
        seen["on_thinking"] = on_thinking
        return json.dumps({"intent": "answer", "message": "ok"})

    agent = build_agent(_transport_with_thinking_model(
        "deepseek-r1:latest", {"intent": "answer", "message": "ok"}))
    agent.ollama.chat = fake_chat  # type: ignore[method-assign]

    received: list[str] = []
    agent.converse("what is the disk usage", model="deepseek-r1:latest",
                   on_thinking=received.append)

    assert seen["on_thinking"] == received.append

def test_converse_passes_no_callback_for_a_non_thinking_model() -> None:
    """The common case -- devstral/qwen3 etc. report no thinking capability,
    so `on_thinking` must not reach `backend.chat` even though the caller
    asked for it, matching the "silently no-op" contract."""
    seen: dict[str, Any] = {}

    def fake_chat(model: str, messages: object, json_mode: bool = False,
                  temperature: float = 0.2, on_thinking: Any | None = None) -> str:
        seen["on_thinking"] = on_thinking
        return json.dumps({"intent": "answer", "message": "ok"})

    agent = build_agent(scripted({"intent": "answer", "message": "ok"}))
    agent.ollama.chat = fake_chat  # type: ignore[method-assign]

    agent.converse("what is the disk usage", on_thinking=lambda chunk: None)

    assert seen["on_thinking"] is None

def test_converse_never_passes_a_callback_to_an_alt_backend() -> None:
    """`router.model_thinking_capable` is only ever populated from local
    Ollama /api/show data -- an explicit openrouter/... override must
    never receive a reasoning callback, even if the caller asked for one."""
    class _RecordingFake(_FakeBackend):
        def chat(self, model: str, messages: object, json_mode: bool = False,
                  temperature: float = 0.2, on_thinking: Any | None = None) -> str:
            self.seen_on_thinking = on_thinking
            return super().chat(model, messages, json_mode, temperature, on_thinking)

    recording = _RecordingFake()
    agent = build_agent(scripted({}), backends={"openrouter": recording})
    agent.converse("what is the disk usage",
                   model="openrouter/anthropic/claude-3.5-sonnet",
                   on_thinking=lambda chunk: None)

    assert recording.seen_on_thinking is None


def test_stats_includes_every_registered_backend() -> None:
    agent = build_agent(scripted({}), backends={"openrouter": _FakeBackend()})
    assert "openrouter" in agent.stats()["backends"]


def test_stats_backends_is_empty_with_none_registered() -> None:
    agent = build_agent(scripted({}))
    assert agent.stats()["backends"] == {}


def test_fast_forward_turn_counter_moves_it_forward() -> None:
    agent = build_agent(scripted({}))
    agent.fast_forward_turn_counter(5)
    assert agent._turn_counter == 5


def test_fast_forward_turn_counter_never_moves_it_backward() -> None:
    """A reopened session's Agent starts at turn 0 -- fast-forwarding it
    past turns already taken must never accidentally rewind a counter
    that's already ahead (e.g. a turn already in flight)."""
    agent = build_agent(scripted({}))
    agent._turn_counter = 10
    agent.fast_forward_turn_counter(3)
    assert agent._turn_counter == 10


def test_fast_forward_turn_counter_reaches_the_next_default_task_id() -> None:
    """The whole point: a reopened session's next non-model-chosen task_id
    must not repeat a value its earlier life already used."""
    agent = build_agent(scripted(_reply_with_no_task_id(
        "create /srv/api/marker",
        [{"id": "touch", "run": "touch /srv/api/marker"}])))
    agent.fast_forward_turn_counter(7)

    turn = agent.converse("create /srv/api/marker")

    assert turn.spec is not None
    assert turn.spec.task_id == "agent-8"


def test_routing_falls_back_to_a_model_that_exists() -> None:
    """Routing to a model that is not pulled would fail every time."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"},
                                 models=["qwen3:8b"]))
    agent.ollama.health()
    agent.ollama.last_status = agent.ollama.last_status.__class__.DEGRADED

    turn = agent.converse("debug why nginx keeps crashing")

    assert turn.model == "qwen3:8b", (
        "routed to qwen3:32b, which is not pulled, and did not fall back"
    )


def test_degraded_fallback_skips_a_model_the_router_knows_cannot_chat() -> None:
    """Found by an independent audit: model_available() is a pure name
    check with no capability awareness -- the last-resort available[0]
    pick used to ignore router.model_chat_capable entirely, which
    observe() (called just before this fallback runs) had already
    populated with real daemon data. An embedding-only model listed
    first in the catalog used to win the fallback over a real chat
    candidate listed second."""
    import json

    calls: list[str] = []

    def transport(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if path == "/api/tags":
            return {"models": [{"name": "nomic-embed-text:latest"},
                               {"name": "qwen3.5:latest"}]}
        if path == "/api/show":
            name = payload.get("name")
            caps = ["embedding"] if name == "nomic-embed-text:latest" else ["completion"]
            return {"capabilities": caps}
        calls.append(str(payload.get("model")))
        return {"message": {"content": json.dumps({"intent": "answer", "message": "ok"})}}

    agent = Agent(OllamaClient(transport=transport, probe_timeout=0.1))
    agent._observe = lambda: MachineState(hostname="pop-os")

    turn = agent.converse("what is the disk usage?")

    assert agent.router.model_chat_capable == {
        "nomic-embed-text:latest": False, "qwen3.5:latest": True}
    assert turn.model == "qwen3.5:latest"
    assert calls == ["qwen3.5:latest"]


def test_an_explicit_model_override_that_is_not_pulled_falls_back() -> None:
    """`_degraded_fallback` only ever runs when `chosen` did not come from
    the router's own resolution -- `select()` already downgrades onto
    whatever `observe()` populated `available` with, so the one way to
    reach this path is a caller-supplied `model=` naming something never
    pulled. Two existing tests named "fallback" (routing_falls_back_to,
    degraded_fallback_skips) both exercise ModelRouter's own downgrade,
    not this method, despite the name."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"},
                                 models=["qwen3.5:latest"]))

    turn = agent.converse("anything", model="totally-not-pulled:99b")

    assert turn.model == "qwen3.5:latest", (
        "the only pulled model happens to be the class fallback model"
    )


def test_an_explicit_model_override_falls_back_to_the_first_available() -> None:
    """Same path, but the fallback model itself is not among what is
    pulled -- `_degraded_fallback` must still pick something rather than
    raising, and with no chat-capability data available it takes the
    first of what is on hand."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"},
                                 models=["qwen3:8b"]))

    turn = agent.converse("anything", model="totally-not-pulled:99b")

    assert turn.model == "qwen3:8b"


def test_an_untagged_pull_is_not_mistaken_for_absent() -> None:
    """Ollama registers `ollama pull devstral` as 'devstral:latest'. An explicit
    model= request for the bare name must still be honored on a fresh client
    (last_status not yet healthy), not silently swapped for the fallback."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"},
                                 models=["devstral:latest", "qwen3:8b"]))

    turn = agent.converse("clean up disk space", model="devstral")

    assert turn.model == "devstral", (
        "an available model, pulled without an explicit tag, was mistaken "
        "for absent and silently downgraded"
    )


def test_observe_populates_the_routers_real_model_sizes() -> None:
    """Agent.observe() fetches /api/show for every available model once and
    hands the result to the router, so it stops guessing "0B" for a name
    with no digit in it -- see ModelRouter._params_b for what that guess
    broke live."""
    show_data = {
        "qwen3.5:latest": {"details": {"parameter_size": "9.7B"},
                           "capabilities": ["completion"]},
        "nomic-embed-text:latest": {"details": {"parameter_size": "137M"},
                                    "capabilities": ["embedding"]},
    }

    def transport(path: str, payload: dict, timeout: float) -> dict:
        if path == "/api/tags":
            return {"models": [{"name": m} for m in show_data]}
        if path == "/api/show":
            return show_data[payload["name"]]
        return {"message": {"content": json.dumps({"intent": "answer", "message": "ok"})}}

    agent = Agent(OllamaClient(transport=transport, probe_timeout=0.1))
    agent._observe = lambda: MachineState(hostname="pop-os")

    agent.observe()

    assert agent.router.model_params_b == {
        "qwen3.5:latest": 9.7, "nomic-embed-text:latest": 0.137}
    assert agent.router.model_chat_capable == {
        "qwen3.5:latest": True, "nomic-embed-text:latest": False}


def test_knowledge_dir_reaches_the_default_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent's default observer is a closure built at construction time --
    this confirms knowledge_dir actually reaches observe_machine() through
    it, not just that the parameter exists."""
    import harness.agent as agent_module

    seen: dict[str, object] = {}

    def fake_observe_machine(extra=None, ollama=None, runner=None, knowledge_dir=None):
        seen["knowledge_dir"] = knowledge_dir
        return MachineState(hostname="pop-os")

    monkeypatch.setattr(agent_module, "observe_machine", fake_observe_machine)

    agent = Agent(OllamaClient(transport=lambda p, pay, t: {}),
                  knowledge_dir="/some/library")
    agent.observe()

    assert seen["knowledge_dir"] == "/some/library"


def test_observe_refresh_repopulates_stale_model_metadata() -> None:
    """Found in a full-file audit: the model-metadata block only ran when
    `router.available` was still falsy, so `observe(refresh=True)` -- whose
    entire purpose is picking up changes -- silently kept whatever sizes
    and chat capability were fetched on the very first observe(), even
    after a model was pulled or deleted mid session. `refresh=True` had no
    caller anywhere in this codebase (latent, not live), but the method's
    own contract says it re-reads the machine, and that has to include the
    model catalog it reads alongside it."""
    show_data = {"qwen3.5:latest": {"details": {"parameter_size": "9.7B"},
                                    "capabilities": ["completion"]}}

    def transport(path: str, payload: dict, timeout: float) -> dict:
        if path == "/api/tags":
            return {"models": [{"name": m} for m in show_data]}
        if path == "/api/show":
            return show_data[payload["name"]]
        return {"message": {"content": json.dumps({"intent": "answer", "message": "ok"})}}

    agent = Agent(OllamaClient(transport=transport, probe_timeout=0.1))
    agent._observe = lambda: MachineState(hostname="pop-os")

    agent.observe()
    assert agent.router.model_params_b == {"qwen3.5:latest": 9.7}

    show_data.clear()
    show_data["qwen3-coder:30b"] = {"details": {"parameter_size": "30.5B"},
                                    "capabilities": ["completion"]}
    agent.observe(refresh=True)

    assert agent.router.model_params_b == {"qwen3-coder:30b": 30.5}
    assert agent.router.available == {"qwen3-coder:30b"}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def test_summarize_sends_step_output_as_plain_text_not_json() -> None:
    """The whole point is a single fixed-shape call, not a second generation
    path -- json_mode must be off, so the reply can never be mistaken for a
    task proposal."""
    seen: dict[str, dict] = {}

    def transport(path: str, payload: dict, timeout: float) -> dict:
        seen["payload"] = payload
        return {"message": {"content": "  the backup last ran Aug 23, 12:01 PM.  "}}

    agent = build_agent(transport)
    steps = [Step(step_id="check", command="journalctl -u backup")]
    results = [StepResult(step_id="check", ok=True, output="Aug 23 12:01:00 ran backup")]

    answer = agent.summarize("when did the backup last run", steps, results, "devstral:24b")

    assert answer == "the backup last ran Aug 23, 12:01 PM."
    payload = seen["payload"]
    assert "format" not in payload, "summarize must not request json_mode"
    prompt = payload["messages"][0]["content"]
    assert "when did the backup last run" in prompt
    assert "Aug 23 12:01:00 ran backup" in prompt


def test_summarize_reports_a_failed_step_by_error_not_output() -> None:
    """A failed step has no useful `output` -- the model must see why it
    failed, not an empty string that reads as "nothing happened"."""
    seen: dict[str, dict] = {}

    def transport(path: str, payload: dict, timeout: float) -> dict:
        seen["payload"] = payload
        return {"message": {"content": "that step failed"}}

    agent = build_agent(transport)
    steps = [Step(step_id="check", command="grep x /nonexistent")]
    results = [StepResult(step_id="check", ok=False, error="No such file or directory")]

    agent.summarize("find x", steps, results, "devstral:24b")

    prompt = seen["payload"]["messages"][0]["content"]
    assert "No such file or directory" in prompt
    assert "FAILED" in prompt


def test_summarize_skips_a_step_with_no_matching_result() -> None:
    """A step the pipeline never got to (e.g. an early abort) has no
    entry in `results` at all -- it must be left out of the report
    rather than crash on a missing lookup."""
    seen: dict[str, dict] = {}

    def transport(path: str, payload: dict, timeout: float) -> dict:
        seen["payload"] = payload
        return {"message": {"content": "one step ran"}}

    agent = build_agent(transport)
    steps = [Step(step_id="ran", command="true"),
             Step(step_id="never-reached", command="true")]
    results = [StepResult(step_id="ran", ok=True, output="done")]

    agent.summarize("did it work", steps, results, "devstral:24b")

    prompt = seen["payload"]["messages"][0]["content"]
    assert "ran" in prompt
    assert "never-reached" not in prompt


def test_stats_reports_turns_repairs_and_context_usage() -> None:
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}))
    agent.converse("hello")

    stats = agent.stats()

    assert stats["turns"] == 1
    assert stats["repairs"] == 0
    assert "context_tokens" in stats
    assert "ollama" in stats


def test_remember_failure_does_not_break_the_turn() -> None:
    """Context bookkeeping is a nice-to-have; a broken compressor must
    not take down the conversation it is only supposed to be logging."""
    agent = build_agent(scripted({"intent": "answer", "message": "ok"}))

    def broken_add(*args, **kwargs):
        raise RuntimeError("simulated context failure")

    agent.context.add = broken_add  # type: ignore[method-assign]
    turn = agent.converse("hello")

    assert turn.intent is Intent.ANSWER, "the turn itself must still complete"
