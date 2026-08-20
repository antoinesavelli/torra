"""
The interface.

Textual's test harness drives the real app headless, so these assert what an
operator would actually see rather than what the widgets contain in principle.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import plan_reply, scripted
from textual.widgets import Checkbox, Input, Static

from harness.core import MachineState, OllamaClient
from harness.pipeline import Harness
from harness.tui import SPINNER_FRAMES, HarnessApp, StepCard


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


def build(tmp_path: Path, transport) -> HarnessApp:
    """A HarnessApp wired to a scripted backend."""
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=transport, probe_timeout=0.1))
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

        app._set_busy(True, "thinking...")
        await pilot.pause()
        first = status_text(app)
        assert "thinking..." in first
        assert any(frame in first for frame in SPINNER_FRAMES)
        assert app._spinner_timer is not None

        await pilot.pause(0.3)
        second = status_text(app)
        assert second != first, "the spinner/elapsed time never advanced"

        app._set_busy(False, "")
        assert app._spinner_timer is None


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

    def spy(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append("enter")
        return orig_execute(*args, **kwargs)

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
