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
from harness.tui import HarnessApp, StepCard


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
