"""
Agent loop: the part that was missing for six phases.

Organised by what the agent has to get right, not by which phase built it.
"""

from __future__ import annotations

import json

import pytest
from conftest import plan_reply, scripted

from harness.agent import Agent, AgentError, Intent
from harness.core import MachineState, OllamaClient, OllamaError, TaskSpec


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
