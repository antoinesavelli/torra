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
