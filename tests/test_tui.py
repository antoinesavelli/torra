"""
The interface.

Textual's test harness drives the real app headless, so these assert what an
operator would actually see rather than what the widgets contain in principle.

Depends on: harness.gates (Precondition, StateVerifier, RollbackReport,
RollbackResult) for the blocked-plan and rollback-report coverage below, and
harness.pipeline (Outcome, RunResult) for the outcome-dispatch coverage that
constructs a RunResult directly rather than round-tripping the whole pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import plan_reply, scripted
from textual.widgets import Checkbox, Input, OptionList, Static, TabbedContent

from harness.agent import Intent, Turn
from harness.core import MachineState, OllamaClient
from harness.gates import Precondition, RollbackReport, RollbackResult, StateVerifier
from harness.pipeline import Harness, Outcome, RunResult
from harness.tui import SPINNER_FRAMES, HarnessApp, SessionPicker, StepCard, run_tui


def screen_text(app: HarnessApp) -> str:
    """Every bit of text currently on screen.

    Widgets differ in where they keep their content, and Header in particular
    has no `renderable` at all, so this asks each one politely and moves on.
    """
    parts = []
    for widget in app.query(Static):
        for attribute in ("content", "renderable", "_content"):
            value = getattr(widget, attribute, None)
            if value:
                parts.append(str(value))
                break
    return " ".join(parts)


def status_text(app: HarnessApp) -> str:
    """The status line's current text, however this Textual version stores it."""
    status = app.query_one("#status", Static)
    for attribute in ("content", "renderable", "_content"):
        value = getattr(status, attribute, None)
        if value:
            return str(value)
    return ""


def build(tmp_path: Path, transport, **harness_kwargs: Any) -> HarnessApp:
    """A HarnessApp wired to a scripted backend."""
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=transport, probe_timeout=0.1),
                      **harness_kwargs)
    harness.agent._observe = lambda: MachineState(hostname="pop-os")
    return HarnessApp(harness=harness)


@pytest.mark.asyncio
async def test_the_app_starts_and_reports_the_backend(tmp_path: Path) -> None:
    """First thing an operator sees is whether the model is reachable."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        text = screen_text(app)
        assert "qwen3:32b" in text or "models available" in text
        assert app.query_one("#prompt", Input) is not None


@pytest.mark.asyncio
async def test_ctrl_c_copies_rather_than_quitting(tmp_path: Path) -> None:
    """Found live: BINDINGS bound ctrl+c to "quit", overriding Textual's own
    deliberate default for that key (Screen sends ctrl+c to copy_text,
    selected-text copy; App's own help_quit action docstring says outright
    "Ctrl+C is now bound to copy... this makes it possible, even likely,
    that a user may do it accidentally -- which would be maddening"). Any
    attempt to copy selected chat/plan text via ctrl+c closed the whole
    app instead of copying anything. Quit moved to ctrl+q so ctrl+c falls
    through to Textual's native copy behavior."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause(0.2)
        assert app.is_running, "ctrl+c must not quit the app"

        await pilot.press("ctrl+q")
        await pilot.pause(0.2)
        assert not app.is_running, "ctrl+q must still quit the app"


@pytest.mark.asyncio
async def test_the_status_line_shows_a_live_spinner_while_busy(tmp_path: Path) -> None:
    """A local model can take anywhere from a couple seconds to over a
    minute (scripts/dev/model_bench/REPORT.md) -- a static "thinking..."
    label looks identical whether it's been one second or frozen for
    sixty. The status line must animate and count up, and stop doing
    either the moment busy clears."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()

        app._set_busy(app.active_session_id, True, "thinking...")
        await pilot.pause()
        first = status_text(app)
        assert "thinking..." in first
        assert any(frame in first for frame in SPINNER_FRAMES)
        assert app.sessions[app.active_session_id].spinner_timer is not None

        await pilot.pause(0.3)
        second = status_text(app)
        assert second != first, "the spinner/elapsed time never advanced"

        app._set_busy(app.active_session_id, False, "")
        assert app.sessions[app.active_session_id].spinner_timer is None


@pytest.mark.asyncio
async def test_a_plan_renders_with_verdicts_and_a_checkbox(tmp_path: Path) -> None:
    """A dangerous step must arrive with an UNTICKED box, never pre-approved."""
    app = build(tmp_path, scripted(plan_reply(
        "t", f"remove {tmp_path}/v.txt",
        [{"id": "rm", "run": f"rm -f {tmp_path}/v.txt",
          "touches": [f"{tmp_path}/v.txt"]}])))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = f"delete {tmp_path}/v.txt"
        await pilot.press("enter")
        await pilot.pause(0.4)

        cards = app.query(StepCard)
        assert len(cards) == 1
        boxes = [b for b in app.query(Checkbox) if (b.id or "").startswith("approve-")]
        assert len(boxes) == 1, "a dangerous step needs an approve box"
        assert boxes[0].value is False, "the box must start unticked"


@pytest.mark.asyncio
async def test_a_step_id_with_a_space_does_not_crash_the_plan_pane(tmp_path: Path) -> None:
    """Found live: a DANGEROUS/CRITICAL step's id is arbitrary model-chosen
    text -- "delete old logs" is exactly as plausible as "rm". Textual DOM
    ids must be [A-Za-z0-9_-]+; embedding step_id directly into one used to
    raise BadIdentifier out of Checkbox's own constructor, crashing the
    whole plan pane before a human ever saw the approval box -- for exactly
    the highest-risk steps this checkbox exists to gate."""
    app = build(tmp_path, scripted(plan_reply(
        "t", f"remove {tmp_path}/v.txt",
        [{"id": "delete old logs", "run": f"rm -f {tmp_path}/v.txt",
          "touches": [f"{tmp_path}/v.txt"]}])))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = f"delete {tmp_path}/v.txt"
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert app.is_running, "the app crashed on a non-identifier-safe step id"
        boxes = [b for b in app.query(Checkbox) if (b.id or "").startswith("approve-")]
        assert len(boxes) == 1, "a dangerous step needs an approve box"

        boxes[0].value = True
        assert app._approvals(app.active_session_id) == {"delete old logs": True}, \
            "approvals must be keyed by the real step id, not the sanitized DOM id"


@pytest.mark.asyncio
async def test_a_safe_plan_gets_no_checkbox(tmp_path: Path) -> None:
    """Ticking boxes for `df -h` is how the one that matters gets ticked too."""
    app = build(tmp_path, scripted(plan_reply(
        "t", "report disk usage on /", [{"id": "df", "run": "df -h /"}])))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "report disk usage on /"
        await pilot.press("enter")
        await pilot.pause(0.4)

        boxes = [b for b in app.query(Checkbox) if (b.id or "").startswith("approve-")]
        assert boxes == []


@pytest.mark.asyncio
async def test_reject_records_the_decision(tmp_path: Path) -> None:
    """Found in a full-file audit: clicking Reject used to bypass the
    pipeline entirely -- no audit record, no telemetry entry, just
    self.current = None. HANDOFF.md §6.1 lists "how often a human
    rejected a plan outright" as something to measure from telemetry;
    the most direct expression of that, the Reject button, couldn't
    produce it."""
    target = tmp_path / "v.txt"
    target.write_text("x", encoding="utf-8")
    app = build(tmp_path, scripted(plan_reply(
        "t", f"remove {target}",
        [{"id": "rm", "run": f"rm -f {target}", "touches": [str(target)]}])))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = f"delete {target}"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#reject")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()

        assert app.current is None
        assert target.exists(), "reject must not run anything"
        records = app.harness.telemetry.read()
        assert records and records[-1].status == "rejected"


@pytest.mark.asyncio
async def test_bracket_text_does_not_crash_the_chat_pane(tmp_path: Path) -> None:
    """Found live: `rich.markup` parses `[...]` as style tags. A request as
    ordinary as "what's the free space in [/mnt/ParamoStorage]?" reads
    `[/mnt/ParamoStorage]` as a *closing* tag with no opener and raises
    MarkupError deep in Textual's compositor -- an unhandled exception that
    kills the whole app. User-visible text (the typed request, the model's
    own message) must render as literal text, brackets included, not be
    interpreted as formatting."""
    app = build(tmp_path, scripted({
        "intent": "answer",
        "message": "checked [/mnt/ParamoStorage], 189G used of 814G",
    }))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt", Input).value = "what's the free space in [/mnt/ParamoStorage]?"
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert app.is_running, "the app crashed on bracket-containing text"
        text = screen_text(app)
        assert "[/mnt/ParamoStorage]" in text, \
            "the bracketed path should render literally, not be swallowed as markup"


@pytest.mark.asyncio
async def test_bracket_text_in_a_step_does_not_crash_the_plan_pane(tmp_path: Path) -> None:
    """Same failure mode as the chat pane, but for StepCard/PlanPane text
    that comes straight from the model's proposal (command, description,
    undo description) rather than the user -- these are just as untrusted
    and just as likely to contain a `[...]` shell glob or bracketed path."""
    app = build(tmp_path, scripted(plan_reply(
        "t", "list files in [/mnt/x]",
        [{"id": "ls", "run": "ls [abc]*.txt",
          "description": "list matching [abc]*.txt files"}])))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "list files in [/mnt/x]"
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert app.is_running, "the app crashed on bracket-containing step text"
        assert app.query(StepCard), "plan did not render"
        text = screen_text(app)
        assert "[abc]*.txt" in text


@pytest.mark.asyncio
async def test_ctrl_d_during_a_real_run_does_not_start_a_second_execution(
        tmp_path: Path) -> None:
    """Found live: unlike the Run/Dry-run buttons (disabled while busy),
    action_dry_run() (Ctrl+D) had no widget to gate it, so pressing it while
    a real run's step was still in flight dispatched a second execute_plan()
    worker. Textual's "exclusive" thread worker does not preempt an
    already-running thread, so both ran Harness.execute() concurrently
    against the same mutable RunResult -- observed live as two overlapping
    calls, 113ms apart, one of them landing on a state neither caller
    intended. execute_plan() now refuses to start a second run while busy,
    regardless of which of its three callers (both buttons, the keybinding)
    triggered it."""
    marker = tmp_path / "counter.txt"
    marker.write_text("0", encoding="utf-8")
    app = build(tmp_path, scripted(plan_reply(
        "t", f"append to {marker}",
        [{"id": "slow", "run": f"sleep 0.3 && echo x >> {marker}",
          "touches": [str(marker)]}])))

    calls: list[str] = []
    orig_execute = app.harness.execute

    def spy(result: RunResult, approvals: dict[str, bool] | None = None,
            dry_run: bool = False) -> RunResult:
        calls.append("enter")
        return orig_execute(result, approvals, dry_run)

    app.harness.execute = spy  # type: ignore[method-assign]

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = f"append to {marker}"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#run")
        await pilot.pause(0.05)
        assert app.busy, "the run should still be in flight"
        app.action_dry_run()  # the race attempt: Ctrl+D while genuinely busy
        await pilot.pause(0.6)
        await app.workers.wait_for_complete()

        assert len(calls) == 1, \
            f"execute() should run exactly once, ran {len(calls)} times"
        assert marker.read_text(encoding="utf-8") == "0x\n", \
            "the step must not have run twice"


@pytest.mark.asyncio
async def test_reject_after_a_real_run_does_not_lie_about_it(tmp_path: Path) -> None:
    """Found in a full-file audit: the Reject button was never disabled
    after a run completed (unlike Run/Dry-run), and clicking it on an
    already-executed plan used to unconditionally say "Plan rejected.
    Nothing ran." and clear self.current -- even though something real
    had already happened. Harness.reject() now refuses to overwrite that
    outcome; this checks the button's own message stops lying about it
    too, and that the completed result is not discarded."""
    marker = tmp_path / "marker"
    app = build(tmp_path, scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = f"touch {marker}"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#run")
        await pilot.pause(0.4)
        assert marker.exists(), "the step never actually ran"
        completed = app.current

        await pilot.click("#reject")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()

        assert marker.exists(), "a late Reject click undid nothing on disk, but must "\
            "also not misreport the run in the audit trail"
        assert app.current is completed, "the completed result was discarded"
        text = screen_text(app)
        assert "already ran" in text
        assert "Plan rejected. Nothing ran." not in text, (
            "the wrong-outcome message was shown for a plan that already ran"
        )


@pytest.mark.asyncio
async def test_questions_are_shown_not_guessed(tmp_path: Path) -> None:
    app = build(tmp_path, scripted(
        {"intent": "ask", "message": "Which host?",
         "questions": ["Which host do you mean?"]}))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "tidy up the box"
        await pilot.press("enter")
        await pilot.pause(0.4)

        text = screen_text(app)
        assert "Which host do you mean?" in text
        assert not app.query(StepCard), "a question is not a plan"


@pytest.mark.asyncio
async def test_on_mount_reports_an_unreachable_backend_with_recovery_options(
        tmp_path: Path) -> None:
    """Complement to test_the_app_starts_and_reports_the_backend: when the
    daemon can't even be probed, on_mount must say so plainly and list a way
    out, not just silently show an empty model list."""
    def transport(path: str, payload: dict, timeout: float) -> dict:
        raise ConnectionRefusedError("nobody home")

    app = build(tmp_path, transport)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = screen_text(app)
        assert "unreachable" in text or "degraded" in text
        assert "nobody home" in text
        assert "restart" in text.lower() and "manual" in text.lower(), \
            "recovery options must be listed when the backend is down"


@pytest.mark.asyncio
async def test_blank_and_busy_submissions_are_ignored(tmp_path: Path) -> None:
    """An empty prompt or a submission while a plan is already in flight
    must not dispatch a second plan_request() worker."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(list(app.query("ChatLine")))

        app.query_one("#prompt", Input).value = "   "
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(list(app.query("ChatLine"))) == before, \
            "a blank prompt must not add a chat line or plan"

        app.busy = True
        app.query_one("#prompt", Input).value = "do something"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(list(app.query("ChatLine"))) == before, \
            "a submission while busy must be ignored"


@pytest.mark.asyncio
async def test_plan_request_reports_an_unexpected_exception(tmp_path: Path) -> None:
    """harness.plan() already narrows OllamaError/AgentError into
    BACKEND_DOWN/FAILED outcomes -- this covers what happens when something
    else entirely goes wrong, which plan_request()'s own try/except guards
    against so the worker thread doesn't just die silently."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    app.harness.plan = lambda request, model=None, on_thinking=None: (  # type: ignore[method-assign]
        _ for _ in ()).throw(ValueError("boom"))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "do something"
        await pilot.press("enter")
        await pilot.pause(0.3)

        text = screen_text(app)
        assert "ValueError" in text and "boom" in text
        assert not app.busy, "busy must clear even when plan() raises"


@pytest.mark.asyncio
async def test_append_thinking_mounts_and_grows_a_live_reasoning_line(
        tmp_path: Path) -> None:
    """`_append_thinking` is `on_thinking`'s destination once it crosses
    back onto the event-loop thread via `call_from_thread` -- called
    directly here since the streaming callback itself is exercised at the
    OllamaClient/Agent layer (test_core.py/test_agent.py)."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test():
        session_id = app.active_session_id
        tab = app.sessions[session_id]
        assert tab.reasoning_line is None

        app._append_thinking(session_id, "Let me ")
        app._append_thinking(session_id, "think about this.")

        assert tab.reasoning_line is not None
        assert tab.reasoning_text == "Let me think about this."
        assert "Let me think about this." in screen_text(app)


@pytest.mark.asyncio
async def test_clear_thinking_removes_the_live_reasoning_line(tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        session_id = app.active_session_id
        app._append_thinking(session_id, "reasoning in progress")
        assert "reasoning in progress" in screen_text(app)

        app._clear_thinking(session_id)
        await pilot.pause(0.1)

        tab = app.sessions[session_id]
        assert tab.reasoning_line is None
        assert tab.reasoning_text == ""
        assert "reasoning in progress" not in screen_text(app)


@pytest.mark.asyncio
async def test_a_completed_plan_request_leaves_no_reasoning_line_behind(
        tmp_path: Path) -> None:
    """End to end through the real worker-thread path: a model that streams
    reasoning during `plan()` must not leave the live line mounted once the
    turn is done -- ephemeral by design (SessionTab.reasoning_line's
    docstring), not a permanent part of the transcript."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))

    def fake_plan(request: str, model: str | None = None,
                  on_thinking: Any | None = None) -> RunResult:
        if on_thinking is not None:
            on_thinking("Let me ")
            on_thinking("check.")
        return RunResult(request=request, outcome=Outcome.ANSWERED,
                         turn=Turn(request=request, intent=Intent.ANSWER, message="done"))

    app.harness.plan = fake_plan  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "what is the disk usage"
        await pilot.press("enter")
        await pilot.pause(0.3)

        tab = app.sessions[app.active_session_id]
        assert tab.reasoning_line is None
        assert tab.reasoning_text == ""
        assert "done" in screen_text(app)


@pytest.mark.asyncio
async def test_a_refusal_is_shown_in_the_status_line(tmp_path: Path) -> None:
    app = build(tmp_path, scripted(
        {"intent": "refuse", "message": "I won't do that."}))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "wipe the whole disk"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert "I won't do that." in screen_text(app)
        assert "declined" in status_text(app)


@pytest.mark.asyncio
async def test_backend_down_during_planning_shows_recovery_options(tmp_path: Path) -> None:
    """The daemon answers the startup probe fine, but generation itself
    fails -- distinct from test_on_mount_reports_an_unreachable_backend,
    which fails before the app ever gets that far."""
    def transport(path: str, payload: dict, timeout: float) -> dict:
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:32b"}]}
        if path == "/api/show":
            return {}
        raise TimeoutError("generation timed out")

    app = build(tmp_path, transport)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt", Input).value = "do something"
        await pilot.press("enter")
        await pilot.pause(0.3)

        text = screen_text(app)
        assert "generation timed out" in text
        assert "restart" in text.lower()
        assert "unreachable" in status_text(app)


@pytest.mark.asyncio
async def test_a_failed_precondition_blocks_the_plan(tmp_path: Path) -> None:
    """A blocking Precondition that fails must stop the plan before
    execution and say exactly what failed, in both the plan pane
    (PlanPane.show's own preconditions branch) and the outcome handler."""
    verifier = StateVerifier().add(
        Precondition("disk space", lambda: False, detail="disk full", blocking=True))
    app = build(tmp_path, scripted(plan_reply(
        "t", "clean up", [{"id": "df", "run": "df -h /"}])), verifier=verifier)

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "clean up"
        await pilot.press("enter")
        await pilot.pause(0.4)

        text = screen_text(app)
        assert "preconditions failed" in text
        assert "disk space" in text
        assert "blocked" in status_text(app)


@pytest.mark.asyncio
async def test_an_unparseable_model_reply_fails_the_plan(tmp_path: Path) -> None:
    """No JSON object anywhere in the reply -- Agent._extract_json's own
    "returned no JSON object" AgentError, surfaced as Outcome.FAILED."""
    def transport(path: str, payload: dict, timeout: float) -> dict:
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:32b"}]}
        if path == "/api/show":
            return {}
        return {"message": {"content": "sure, here you go, no braces at all"}}

    app = build(tmp_path, transport)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt", Input).value = "do something"
        await pilot.press("enter")
        await pilot.pause(0.3)

        text = screen_text(app)
        assert "no usable plan" in status_text(app)
        assert "did not produce a usable plan" in text


@pytest.mark.asyncio
async def test_outcome_plan_ready_is_a_no_op_with_no_plan(tmp_path: Path) -> None:
    """Defensive guard: the AWAITING_APPROVAL default handler bails out if
    somehow called with no plan attached. Every real pipeline path that
    reaches AWAITING_APPROVAL has already assigned a plan, so this branch
    is dispatched directly rather than contorting the pipeline into
    producing an impossible RunResult."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test():
        result = RunResult(request="x", outcome=Outcome.AWAITING_APPROVAL, plan=None)
        app._outcome_plan_ready(app.active_session_id, result)  # must not raise
        assert not app.query(StepCard)


@pytest.mark.asyncio
async def test_pressed_dry_button_runs_a_dry_run(tmp_path: Path) -> None:
    """Clicking Dry run (as opposed to Ctrl+D) must run every gate and
    execute nothing."""
    target = tmp_path / "v.txt"
    app = build(tmp_path, scripted(plan_reply(
        "t", f"touch {target}", [{"id": "touch", "run": f"touch {target}"}])))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = f"touch {target}"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#dryrun")
        await pilot.pause(0.3)

        assert not target.exists(), "a dry run must not touch the filesystem"
        assert "[dry run]" in screen_text(app)


@pytest.mark.asyncio
async def test_ctrl_d_runs_a_dry_run_when_idle(tmp_path: Path) -> None:
    """The keyboard shortcut takes the same path as the button when the app
    isn't busy."""
    target = tmp_path / "v.txt"
    app = build(tmp_path, scripted(plan_reply(
        "t", f"touch {target}", [{"id": "touch", "run": f"touch {target}"}])))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = f"touch {target}"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.press("ctrl+d")
        await pilot.pause(0.3)

        assert not target.exists()
        assert "[dry run]" in screen_text(app)


@pytest.mark.asyncio
async def test_ctrl_d_with_no_plan_says_so(tmp_path: Path) -> None:
    """The Ctrl+D shortcut before ever asking for a plan must say there's
    nothing to run, not silently do nothing or crash. Driven by calling
    action_dry_run() directly, same as the existing race-condition test
    above: Input's own BINDINGS claim ctrl+d for delete_right while the
    prompt has focus (which it does at startup), so pilot.press("ctrl+d")
    here would just exercise Input's editing, never reach the app."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current is None

        app.action_dry_run()
        await pilot.pause(0.4)

        assert "No plan to run." in screen_text(app)


@pytest.mark.asyncio
async def test_execute_plan_ignores_a_second_call_while_busy(tmp_path: Path) -> None:
    """execute_plan()'s own busy guard is the single choke point all three
    callers share -- this drives it directly rather than through
    action_dry_run(), which has its own separate busy check that would
    otherwise short-circuit before this guard is ever reached."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    calls: list[str] = []

    def fake_execute(*a: object, **k: object) -> Any:
        calls.append("called")

    app.harness.execute = fake_execute  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        app.busy = True
        app.execute_plan(app.active_session_id, dry_run=True)
        await pilot.pause(0.2)

        assert calls == [], "execute() must not run while the app is already busy"


@pytest.mark.asyncio
async def test_execute_plan_sets_busy_synchronously_before_the_worker_starts(
        tmp_path: Path) -> None:
    """Found by a full-codebase audit: `busy` used to flip to True only
    once the worker thread itself got around to it (a call_from_thread
    round trip after fetching approvals), leaving a real window where a
    fast second dispatch (a double-click, or Ctrl+D right after a click)
    still read busy=False and started a second, genuinely concurrent
    Harness.execute(). It must now be True the instant execute_plan()
    returns, with no pilot.pause() at all -- proving there is no worker
    round trip in between."""
    app = build(tmp_path, scripted(plan_reply(
        "t", "report disk usage on /", [{"id": "df", "run": "df -h /"}])))
    app.harness.execute = lambda *a, **k: RunResult(  # type: ignore[method-assign]
        request="r", outcome=Outcome.COMPLETED)

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "report disk usage on /"
        await pilot.press("enter")
        await pilot.pause(0.4)
        session_id = app.active_session_id
        assert not app.sessions[session_id].busy

        app.execute_plan(session_id, dry_run=True)

        assert app.sessions[session_id].busy, \
            "busy must flip synchronously, before the worker is even dispatched"

        # Let the dispatched worker actually finish before the `async with`
        # block tears the app down -- found flaky on CI (not locally):
        # without this, the worker's call_from_thread() calls can land
        # after the screen has already started unmounting, raising
        # NoMatches from inside the thread and failing the test on an
        # assertion that had already passed.
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_plan_request_sets_busy_synchronously_before_the_worker_starts(
        tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))

    async with app.run_test() as pilot:
        await pilot.pause()
        session_id = app.active_session_id
        assert not app.sessions[session_id].busy

        app.plan_request(session_id, "hello")

        assert app.sessions[session_id].busy, \
            "busy must flip synchronously, before the worker is even dispatched"

        # Same fix as execute_plan's sibling test above: let the dispatched
        # worker actually finish before the `async with` block tears the
        # app down, or its call_from_thread() calls can land after the
        # screen starts unmounting and fail the test on an already-passed
        # assertion.
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_execute_plan_reports_an_unexpected_exception(tmp_path: Path) -> None:
    app = build(tmp_path, scripted(plan_reply(
        "t", "report disk usage on /", [{"id": "df", "run": "df -h /"}])))
    app.harness.execute = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("disk exploded"))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = "report disk usage on /"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#run")
        await pilot.pause(0.3)

        text = screen_text(app)
        assert "RuntimeError" in text and "disk exploded" in text
        assert not app.busy


@pytest.mark.asyncio
async def test_run_without_ticking_the_box_is_rejected(tmp_path: Path) -> None:
    """The implicit-refusal path through execute(): a DANGEROUS step left
    unticked and Run clicked anyway must be reported as rejected, distinct
    from clicking the Reject button."""
    target = tmp_path / "v.txt"
    target.write_text("x", encoding="utf-8")
    app = build(tmp_path, scripted(plan_reply(
        "t", f"remove {target}",
        [{"id": "rm", "run": f"rm -f {target}", "touches": [str(target)]}])))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = f"delete {target}"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#run")
        await pilot.pause(0.3)

        assert target.exists(), "an unticked box must not let the step run"
        text = screen_text(app)
        assert "not approved" in text


@pytest.mark.asyncio
async def test_a_failing_step_is_reported_as_failed(tmp_path: Path) -> None:
    """A step whose command genuinely fails must land in the "else" branch
    of _executed()'s outcome reporting -- neither COMPLETED nor REJECTED."""
    app = build(tmp_path, scripted(plan_reply(
        "t", "do the impossible", [{"id": "bad", "run": "exit 1"}])))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = "do the impossible"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#run")
        await pilot.pause(0.4)

        text = screen_text(app)
        assert "bad failed" in text
        assert app.current is not None and app.current.outcome is Outcome.FAILED


@pytest.mark.asyncio
async def test_a_rollback_report_is_summarised_with_every_entry(tmp_path: Path) -> None:
    """_executed()'s rollback block (undone/unverified/failed counts, plus a
    line per unverified/failed entry) is driven directly with a hand-built
    RollbackReport -- forcing a real undo into "unverified" or "failed"
    would mean deliberately sabotaging the filesystem mid-rollback, which
    tests VerifiedRollback's own logic (already covered in test_gates.py)
    far more than anything in tui.py."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    report = RollbackReport(ok=False, results=[
        RollbackResult("remove /tmp/x", "unverified", "", order=1, verified=None),
        RollbackResult("restart ollama", "failed", "systemctl exited 1", order=2),
    ])
    result = RunResult(request="break things", outcome=Outcome.FAILED,
                       abort_reason="step 2 failed", rollback=report)

    async with app.run_test():
        app._executed(app.active_session_id, result, dry_run=False)
        text = screen_text(app)
        assert "1 unverified" in text and "1 failed" in text
        assert "remove /tmp/x" in text
        assert "restart ollama" in text and "systemctl exited 1" in text


@pytest.mark.asyncio
async def test_ctrl_r_reruns_the_last_request(tmp_path: Path) -> None:
    app = build(tmp_path, scripted(
        {"intent": "answer", "message": "first answer"},
        {"intent": "answer", "message": "second answer"}))

    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "what's the disk usage"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert "first answer" in screen_text(app)

        await pilot.press("ctrl+r")
        await pilot.pause(0.3)

        text = screen_text(app)
        assert "re-plan" in text
        assert "second answer" in text


@pytest.mark.asyncio
async def test_ctrl_l_clears_the_chat(tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert "ok" in screen_text(app)

        await pilot.press("ctrl+l")
        await pilot.pause(0.1)

        text = screen_text(app)
        assert "ok" not in text
        assert "Conversation cleared." in text


@pytest.mark.asyncio
async def test_ctrl_t_opens_an_independent_second_tab(tmp_path: Path) -> None:
    """A new tab gets its own Harness/Agent, not a shared one."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        first_id = app.active_session_id

        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        second_id = app.active_session_id

        assert second_id != first_id
        assert len(app.sessions) == 2
        assert app.sessions[first_id].harness is not app.sessions[second_id].harness
        assert (app.sessions[first_id].harness.undo.shadow_dir
                != app.sessions[second_id].harness.undo.shadow_dir)


@pytest.mark.asyncio
async def test_click_inside_the_sidebars_plan_tab_resolves_the_right_session(
        tmp_path: Path) -> None:
    """Regression test for HANDOFF.md §2.30: _session_id_of used to stop at
    the sidebar's own inner TabPane (id="sidebar-plan") before reaching the
    outer session TabPane, silently producing session_id == "sidebar-plan"
    -- never a real key in self.sessions, so every click of Run/Reject/
    Dry-run inside the sidebar raised an unconditional KeyError, which
    _handle_exception (§2.29) turns the whole app exiting."""
    marker = tmp_path / "v.txt"
    marker.write_text("x", encoding="utf-8")
    app = build(tmp_path, scripted(plan_reply(
        "t", f"remove {marker}",
        [{"id": "rm", "run": f"rm -f {marker}", "touches": [str(marker)]}])))

    async with app.run_test(size=(120, 50)) as pilot:
        app.query_one("#prompt", Input).value = f"delete {marker}"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.query(StepCard), "plan did not render"

        await pilot.click("#reject")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()

        assert app.is_running, "a KeyError inside the click handler would have crashed the app"
        assert app.current is None
        assert marker.exists(), "reject must not run anything"


@pytest.mark.asyncio
async def test_the_sidebar_evidence_tab_renders_real_content_on_activation(
        tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt", Input).value = "hi"
        await pilot.press("enter")
        await pilot.pause(0.4)
        await app.workers.wait_for_complete()

        sidebar = app.query_one("#sidebar", TabbedContent)
        sidebar.active = "sidebar-evidence"
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()

        text = screen_text(app)
        assert "outcomes:" in text
        assert "no crashes recorded" in text


@pytest.mark.asyncio
async def test_ctrl_tab_with_two_open_tabs_switches_the_session_tab(tmp_path: Path) -> None:
    """No test exercised action_next_tab()/action_prev_tab() before this
    (HANDOFF.md §2.30) -- both now query "#sessions > Tabs" explicitly
    rather than a bare Tabs query, since the sidebar's own inner
    TabbedContent composes a second Tabs widget per open session tab, and
    query_one doesn't raise on ambiguity in this Textual version."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        first_id = app.active_session_id
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        second_id = app.active_session_id
        assert second_id != first_id

        app.action_prev_tab()
        await pilot.pause(0.2)
        assert app.active_session_id == first_id

        app.action_next_tab()
        await pilot.pause(0.2)
        assert app.active_session_id == second_id


class _FakeBackend:
    """A minimal ModelBackend, for TUI backend-wiring tests."""

    def chat(self, model: str, messages: object, json_mode: bool = False,
              temperature: float = 0.2, on_thinking: Any | None = None) -> str:
        return '{"intent": "answer", "message": "ok"}'

    def recovery_options(self, current_model: str = "unknown") -> list[Any]:
        return []

    def stats(self) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_a_new_tab_inherits_the_apps_registered_backends(tmp_path: Path) -> None:
    """Found by design review before shipping: a per-tab Harness that
    doesn't inherit `backends` would silently lose OpenRouter access after
    the first tab (HANDOFF.md §2.27)."""
    fake = _FakeBackend()
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}),
               backends={"openrouter": fake})
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        second_id = app.active_session_id

        assert app.sessions[second_id].harness.agent.backends["openrouter"] is fake


@pytest.mark.asyncio
async def test_model_command_sets_the_override(tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}),
               backends={"openrouter": _FakeBackend()})
    async with app.run_test() as pilot:
        session_id = app.active_session_id
        app.query_one("#prompt", Input).value = "/model openrouter/anthropic/claude-3.5-sonnet"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.sessions[session_id].model_override == "openrouter/anthropic/claude-3.5-sonnet"
        assert "model override set to" in screen_text(app)


@pytest.mark.asyncio
async def test_bare_model_command_clears_the_override(tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}),
               backends={"openrouter": _FakeBackend()})
    async with app.run_test() as pilot:
        session_id = app.active_session_id
        app.sessions[session_id].model_override = "openrouter/some-model"

        app.query_one("#prompt", Input).value = "/model"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.sessions[session_id].model_override is None
        assert "cleared" in screen_text(app)


@pytest.mark.asyncio
async def test_an_unconfigured_openrouter_override_fails_clearly_not_silently(
        tmp_path: Path) -> None:
    """Found live by manual testing: an unconfigured openrouter/... request
    used to silently fall through to a real local model with no error at
    all, if "openrouter" wasn't registered as a backend. A real
    OpenRouterClient with no key IS registered (matching build_harness()'s
    corrected behavior, HANDOFF.md §2.27), so submitting through it must
    surface OpenRouterClient's own clear error, not a local model's answer."""
    from harness.core import OpenRouterClient

    app = build(tmp_path, scripted({"intent": "answer", "message": "should not be reached"}),
               backends={"openrouter": OpenRouterClient(api_key="")})
    async with app.run_test() as pilot:
        session_id = app.active_session_id
        app.sessions[session_id].model_override = "openrouter/anthropic/claude-3.5-sonnet"

        app.query_one("#prompt", Input).value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert "OPENROUTER_API_KEY is not set" in screen_text(app)
        assert "should not be reached" not in screen_text(app)


@pytest.mark.asyncio
async def test_model_override_reaches_the_plan_call(tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}),
               backends={"openrouter": _FakeBackend()})
    seen: dict[str, object] = {}
    real_plan = app.harness.plan

    def spy_plan(request: str, model: str | None = None,
                on_thinking: Any | None = None) -> RunResult:
        seen["model"] = model
        return real_plan(request, model=model, on_thinking=on_thinking)

    async with app.run_test() as pilot:
        session_id = app.active_session_id
        app.sessions[session_id].model_override = "openrouter/anthropic/claude-3.5-sonnet"
        app.sessions[session_id].harness.plan = spy_plan  # type: ignore[method-assign]

        app.query_one("#prompt", Input).value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert seen["model"] == "openrouter/anthropic/claude-3.5-sonnet"


@pytest.mark.asyncio
async def test_close_tab_refuses_to_close_the_last_tab(tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.sessions) == 1
        app.action_close_tab()
        await pilot.pause()
        assert len(app.sessions) == 1


@pytest.mark.asyncio
async def test_close_tab_refuses_a_busy_tab(tmp_path: Path) -> None:
    """Closing a busy tab would race a still-in-flight worker's callback
    against `del self.sessions[session_id]` -- refuse instead."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        second_id = app.active_session_id
        app.sessions[second_id].busy = True

        app.action_close_tab()
        await pilot.pause()

        assert second_id in app.sessions


@pytest.mark.asyncio
async def test_close_tab_removes_the_active_tab_and_falls_back_to_another(
        tmp_path: Path) -> None:
    """Driven via action_close_tab() directly, not pilot.press("ctrl+w"):
    Input's own BINDINGS also claim ctrl+w (for delete_left_word), and the
    prompt has focus once a tab's greeting finishes -- same reason the
    existing Ctrl+D tests above call action_dry_run() directly."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        first_id = app.active_session_id
        await pilot.press("ctrl+t")
        await app.workers.wait_for_complete()
        second_id = app.active_session_id

        app.action_close_tab()
        # wait_for_complete(), not a fixed pilot.pause() -- found flaky on
        # CI: action_close_tab() dispatches an async worker to await
        # remove_pane() before del self.sessions[session_id] runs
        # (tui.py's action_close_tab, see its own comment on why that
        # ordering has to be a real coroutine, not a bare run_worker value).
        # A fixed duration is a guess at how long that takes; on a slower
        # or more loaded CI runner the guess was sometimes wrong and the
        # assertion below ran before the worker had actually finished.
        await app.workers.wait_for_complete()

        assert second_id not in app.sessions
        assert first_id in app.sessions
        assert len(app.sessions) == 1


@pytest.mark.asyncio
async def test_plan_request_runs_in_a_per_session_worker_group(tmp_path: Path) -> None:
    """Each session's plan/execute must live in its own exclusive group --
    a single shared group would mean tab B's click cancels tab A's
    in-flight run instead of running alongside it."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        session_id = app.active_session_id
        groups: list[Any] = []
        original_run_worker = app.run_worker

        def spy(work: Any, **kwargs: Any) -> Any:
            groups.append(kwargs.get("group"))
            return original_run_worker(work, **kwargs)

        app.run_worker = spy  # type: ignore[method-assign]
        app.plan_request(session_id, "hello")
        await pilot.pause(0.3)

        # The plan's own dispatch is the first run_worker() call; later
        # calls in the chain (e.g. the plan pane's async UI update) use
        # their own ad hoc groups and aren't what this test is about.
        assert groups[0] == f"session-{session_id}"


@pytest.mark.asyncio
async def test_a_turn_is_persisted_to_the_session_transcript(tmp_path: Path) -> None:
    """`Agent.on_context_change` must actually be wired to this session's
    `SessionStore`, not just constructed and left unused."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        session_id = app.active_session_id

        app.query_one("#prompt", Input).value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.3)

        records = app.session_store.load_transcript(session_id)
        assert any(r.role == "user" and r.content == "hello" for r in records)
        assert any(r.role == "assistant" for r in records)


@pytest.mark.asyncio
async def test_the_first_request_names_the_session(tmp_path: Path) -> None:
    """Found live: a name built straight from arbitrary request text and
    handed to Tab.label/TabPane as a plain string crashes the whole app --
    Textual parses a plain str as its own markup, and ordinary shell-ish
    text like "list files in [/mnt/x]" is not valid markup."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        session_id = app.active_session_id

        app.query_one("#prompt", Input).value = "list files in [/mnt/x]"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert app.is_running, "the app crashed on a bracket-bearing session name"
        assert app.sessions[session_id].meta.name == "list files in [/mnt/x]"
        on_disk = app.session_store.load_meta(session_id)
        assert on_disk is not None
        assert on_disk.name == "list files in [/mnt/x]"


@pytest.mark.asyncio
async def test_clear_chat_persists_a_marker_not_a_gap(tmp_path: Path) -> None:
    """Ctrl+L clears the live context but must not erase persisted
    history -- a reopened transcript should show the boundary, not a gap."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        session_id = app.active_session_id

        app.query_one("#prompt", Input).value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.3)

        await pilot.press("ctrl+l")
        await pilot.pause(0.1)

        records = app.session_store.load_transcript(session_id)
        assert records, "clearing must not wipe the persisted transcript"
        assert records[-1].role == "system"
        assert records[-1].content == "[cleared by operator]"


@pytest.mark.asyncio
async def test_reopening_an_already_open_session_switches_tabs_without_duplicating(
        tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        first_id = app.active_session_id
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        assert app.active_session_id != first_id
        assert len(app.sessions) == 2

        app._open_or_switch_to(first_id)
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()

        assert app.active_session_id == first_id
        assert len(app.sessions) == 2, "must not open a duplicate tab"


@pytest.mark.asyncio
async def test_reopening_a_session_that_no_longer_exists_shows_an_error(
        tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._open_or_switch_to("does-not-exist")
        await pilot.pause(0.1)

        assert "no longer exists" in status_text(app)
        assert len(app.sessions) == 1


@pytest.mark.asyncio
async def test_reopening_a_past_session_after_a_restart_replays_its_history(
        tmp_path: Path) -> None:
    """The point of persistence: kill the process, come back, and the
    conversation is still there -- simulated here as a second HarnessApp
    sharing the first one's workdir, the same way a real restart would."""
    app1 = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app1.run_test() as pilot:
        await pilot.pause()
        session_id = app1.active_session_id
        app1.query_one("#prompt", Input).value = "remember this"
        await pilot.press("enter")
        await pilot.pause(0.3)

    app2 = build(tmp_path, scripted({"intent": "answer", "message": "second run"}))
    async with app2.run_test() as pilot:
        await pilot.pause()
        assert session_id not in app2.sessions, "a fresh app must not already have it open"

        app2._open_or_switch_to(session_id)
        await pilot.pause(0.3)

        assert session_id in app2.sessions
        assert "remember this" in screen_text(app2)
        assert app2.sessions[session_id].last_request == "remember this"


@pytest.mark.asyncio
async def test_ctrl_o_lists_saved_sessions_and_reopening_one_dismisses_the_picker(
        tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt", Input).value = "hi"
        await pilot.press("enter")
        await pilot.pause(0.3)

        await pilot.press("ctrl+o")
        await pilot.pause(0.2)

        assert isinstance(app.screen, SessionPicker)
        option_list = app.screen.query_one(OptionList)
        assert option_list.option_count >= 1

        await pilot.press("enter")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()

        assert not isinstance(app.screen, SessionPicker)


@pytest.mark.asyncio
async def test_escape_dismisses_the_picker_without_opening_anything(
        tmp_path: Path) -> None:
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await pilot.pause(0.1)
        assert isinstance(app.screen, SessionPicker)

        await pilot.press("escape")
        await pilot.pause(0.1)

        assert not isinstance(app.screen, SessionPicker)
        assert len(app.sessions) == 1


@pytest.mark.asyncio
async def test_set_busy_false_with_a_message_updates_the_status_line(
        tmp_path: Path) -> None:
    """No current caller passes a non-empty message alongside busy=False,
    but the method itself must still honour it rather than silently drop
    it -- called directly since there is no real caller to drive it."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    async with app.run_test():
        app._set_busy(app.active_session_id, True, "thinking...")
        app._set_busy(app.active_session_id, False, "wrapped up")
        assert "wrapped up" in status_text(app)
        assert app.sessions[app.active_session_id].spinner_timer is None


@pytest.mark.asyncio
async def test_handle_exception_records_a_crash_and_still_calls_super(
        tmp_path: Path) -> None:
    """The only hook Textual ever calls for an unhandled TUI exception
    (HANDOFF.md §2.29) -- must record it AND still leave the app in the
    same panicked state Textual's own default produces. run_test()'s own
    teardown re-raises app._exception for pytest's awareness (its own
    documented behavior) -- pytest.raises around the whole block is what
    consumes that, not a bug in the app itself."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    error = RuntimeError("simulated unhandled crash")

    with pytest.raises(RuntimeError, match="simulated unhandled crash"):
        async with app.run_test():
            app._handle_exception(error)

            assert app._exception is error, "super()._handle_exception must still run"
            assert app.return_code == 1
            crashes = app._crash_reporter.read()
            assert len(crashes) == 1
            assert crashes[0].site == "tui"
            assert crashes[0].exception_type == "RuntimeError"
            assert crashes[0].message == "simulated unhandled crash"


@pytest.mark.asyncio
async def test_handle_exception_still_calls_super_if_the_reporter_itself_raises(
        tmp_path: Path) -> None:
    """A crash reporter that itself fails must never block the one hook
    responsible for the app actually exiting."""
    app = build(tmp_path, scripted({"intent": "answer", "message": "ok"}))
    error = RuntimeError("simulated unhandled crash")

    with pytest.raises(RuntimeError, match="simulated unhandled crash"):
        async with app.run_test():
            def broken_record(error: BaseException, site: str) -> bool:
                raise OSError("the reporter itself is broken")

            app._crash_reporter.record = broken_record  # type: ignore[method-assign]

            app._handle_exception(error)  # must not raise

            assert app._exception is error, "super()._handle_exception must still run"
            assert app.return_code == 1


def test_run_tui_builds_and_runs_the_app(tmp_path: Path, monkeypatch) -> None:
    """run_tui()'s own body is a single call into Textual's real event loop
    (App.run(), which takes over the terminal) -- genuinely impractical to
    drive headlessly, so this checks it builds the right app and hands off
    to run() exactly once, with App.run() itself replaced."""
    calls: list[HarnessApp] = []
    monkeypatch.setattr(HarnessApp, "run", lambda self: calls.append(self))

    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=scripted(
                          {"intent": "answer", "message": "ok"}), probe_timeout=0.1))
    run_tui(harness=harness)

    assert len(calls) == 1
    assert calls[0].harness is harness
