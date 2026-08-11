"""Terminal interface: chat on the left, the gated plan on the right.

Depends on: textual, harness.core (OllamaStatus), harness.gates (RiskLevel),
harness.pipeline (Harness and friends).

You never approve a command you have not seen classified: nothing executes
until every step showing DANGEROUS or CRITICAL has been individually ticked.
"""

from __future__ import annotations

import time
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Static,
)

from .core import OllamaStatus
from .pipeline import Harness, Outcome, Plan, RunResult, StepPlan

__all__ = ["HarnessApp", "run_tui"]

#: A local model can take anywhere from a couple seconds to over a minute
#: (see scripts/dev/model_bench/REPORT.md) -- a static "thinking..." label
#: looks identical whether it's been one second or frozen for sixty, so the
#: status line animates and counts up instead of just sitting there.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


#: Risk level to a colour the eye can sort at a glance without reading.
LEVEL_STYLE: dict[str, str] = {
    "safe": "green",
    "caution": "yellow",
    "dangerous": "dark_orange",
    "critical": "bold red",
}

OUTCOME_STYLE: dict[str, str] = {
    "completed": "bold green",
    "answered": "cyan",
    "needs_answer": "yellow",
    "awaiting_approval": "bold yellow",
    "rejected": "dim",
    "refused": "magenta",
    "blocked": "bold red",
    "failed": "bold red",
    "backend_down": "bold red",
}


class ChatLine(Static):
    """One line in the conversation pane."""

    def __init__(self, who: str, text: str, style: str = "") -> None:
        """Render one speaker-prefixed, optionally styled line."""
        prefix = {"you": "[bold cyan]you[/]", "agent": "[bold green]agent[/]",
                  "harness": "[bold blue]harness[/]",
                  "error": "[bold red]error[/]"}.get(who, who)
        body = f"[{style}]{text}[/]" if style else text
        super().__init__(f"{prefix}  {body}", markup=True)


class StepCard(Vertical):
    """One proposed step, with its gate verdicts and an approve toggle.

    A step that needs confirmation gets a checkbox that starts unticked. A safe
    step gets no checkbox at all, because asking for approval on `df -h` trains
    people to tick without reading, which is how the one that mattered gets
    ticked too.
    """

    def __init__(self, index: int, plan_step: StepPlan) -> None:
        """Store the 1-based plan position and the step's gate verdicts."""
        super().__init__(classes="step-card")
        self.index = index
        self.plan_step = plan_step

    def compose(self) -> ComposeResult:
        """Build the card."""
        sp = self.plan_step
        level = sp.verdict.level.value
        style = LEVEL_STYLE.get(level, "")

        yield Static(
            f"[dim]{self.index}.[/] [bold]{sp.step.step_id}[/]  "
            f"[{style}]{level.upper()}[/]", markup=True)
        yield Static(f"  [white]{sp.step.command}[/]", markup=True,
                     classes="step-command")
        if sp.step.description and sp.step.description != sp.step.command:
            yield Static(f"  [dim]{sp.step.description}[/]", markup=True)

        for reason in sp.verdict.reasons[:2]:
            yield Static(f"  [dim]{reason[:96]}[/]", markup=True)

        if sp.undos:
            yield Static(
                f"  [green]undo:[/] {sp.undos[0].description[:80]}"
                + (f" [dim](+{len(sp.undos) - 1} more)[/]" if len(sp.undos) > 1 else ""),
                markup=True)
        else:
            yield Static("  [yellow]no automatic undo for this step[/]", markup=True)

        if sp.needs_confirmation:
            yield Checkbox(f"approve {sp.step.step_id}", value=False,
                           id=f"approve-{sp.step.step_id}")


class PlanPane(VerticalScroll):
    """The right hand pane: the current proposal and its verdicts."""

    def compose(self) -> ComposeResult:
        """Start empty."""
        yield Static("[dim]No plan yet. Ask for something on the left.[/]",
                     markup=True, id="plan-empty")

    async def show(self, plan: Plan) -> None:
        """Replace the pane contents with a new plan."""
        await self.remove_children()
        worst = plan.worst_level.value
        style = LEVEL_STYLE.get(worst, "")

        await self.mount(Static(
            f"[bold]{plan.spec.task_id}[/]\n"
            f"[dim]{plan.spec.request}[/]\n"
            f"model [bold]{plan.model}[/]  worst [{style}]{worst.upper()}[/]  "
            f"undo coverage {plan.coverage.get('coverage_ratio', 0):.0%}"
            + (f"  [dim]({plan.repairs} repair(s))[/]" if plan.repairs else ""),
            markup=True, classes="plan-header"))

        if plan.uncovered:
            await self.mount(Static(
                f"[yellow]no automatic undo for: {', '.join(plan.uncovered)}[/]",
                markup=True))
        if plan.preconditions and not plan.preconditions.passed:
            await self.mount(Static(
                "[bold red]preconditions failed: "
                + ", ".join(r.name for r in plan.preconditions.blocking_failures)
                + "[/]", markup=True))

        for index, sp in enumerate(plan.steps, start=1):
            await self.mount(StepCard(index, sp))

        pending = len(plan.needs_confirmation)
        await self.mount(Horizontal(
            Button("Run", variant="success", id="run"),
            Button("Reject", variant="error", id="reject"),
            Button("Dry run", id="dryrun"),
            classes="plan-actions"))
        await self.mount(Static(
            f"[dim]{pending} step(s) need an explicit tick before Run does "
            "anything.[/]" if pending else
            "[dim]Nothing in this plan needs confirmation.[/]", markup=True))

    async def clear(self, message: str = "No plan.") -> None:
        """Empty the pane with a message."""
        await self.remove_children()
        await self.mount(Static(f"[dim]{message}[/]", markup=True))


class HarnessApp(App):
    """The two-pane terminal interface."""

    CSS = """
    Screen { layout: horizontal; }
    #left  { width: 50%; border-right: solid $primary; }
    #right { width: 50%; }
    #chat  { height: 1fr; padding: 0 1; }
    #plan  { height: 1fr; padding: 0 1; }
    #prompt { dock: bottom; }
    .step-card {
        height: auto; border: round $panel; margin: 0 0 1 0; padding: 0 1;
    }
    .step-command { color: $text; }
    .plan-header { padding: 0 0 1 0; }
    .plan-actions { height: auto; padding: 1 0; }
    ChatLine { height: auto; padding: 0 0 1 0; }
    #status { dock: bottom; height: 1; background: $panel; padding: 0 1; }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+r", "rerun", "Re-plan"),
        ("ctrl+d", "dry_run", "Dry run"),
        ("ctrl+l", "clear_chat", "Clear chat"),
    ]

    busy: reactive[bool] = reactive(False)

    def __init__(self, harness: Harness | None = None,
                 workdir: Path | None = None) -> None:
        """Use the injected harness for tests, or build a default one."""
        super().__init__()
        self.harness = harness or Harness(workdir=workdir or Path.home() / ".harness")
        self.current: RunResult | None = None
        self.last_request: str = ""
        self._spinner_timer: Timer | None = None
        self._busy_message: str = ""
        self._busy_started: float = 0.0

    # -- layout ------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Two panes, a prompt, and a status line."""
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                yield VerticalScroll(id="chat")
                yield Input(placeholder="what do you want done?", id="prompt")
            with Vertical(id="right"):
                yield PlanPane(id="plan")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Greet, and say plainly whether the model backend is reachable."""
        self.title = "harness"
        status = self.harness.ollama.health()
        models = self.harness.ollama.models()
        self.sub_title = f"{self.harness.ollama.host}  {status.value}"

        self._say("harness", "Describe what you want done. I plan it, every gate "
                             "checks it, you approve it, then it runs.")
        if status is OllamaStatus.HEALTHY:
            self._say("harness", f"models available: {', '.join(models) or 'none'}",
                      "dim")
        else:
            self._say("error",
                      f"Ollama is {status.value} at {self.harness.ollama.host}. "
                      f"{self.harness.ollama.last_error}")
            for option in self.harness.ollama.recovery_options():
                self._say("harness", f"{option.title}  ->  {option.action}", "dim")
        self.query_one("#prompt", Input).focus()

    # -- helpers ----------------------------------------------------------- #

    def _say(self, who: str, text: str, style: str = "") -> None:
        """Append a line to the chat pane and scroll to it."""
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(ChatLine(who, text, style))
        chat.scroll_end(animate=False)

    def _status(self, text: str) -> None:
        """Update the status line."""
        self.query_one("#status", Static).update(text)

    def _approvals(self) -> dict[str, bool]:
        """Read every approve checkbox in the plan pane."""
        out: dict[str, bool] = {}
        for box in self.query(Checkbox):
            box_id = box.id or ""
            if box_id.startswith("approve-"):
                out[box_id[len("approve-"):]] = bool(box.value)
        return out

    # -- input ------------------------------------------------------------- #

    @on(Input.Submitted, "#prompt")
    def submitted(self, event: Input.Submitted) -> None:
        """Send the typed request to the model."""
        request = event.value.strip()
        if not request or self.busy:
            return
        event.input.value = ""
        self.last_request = request
        self._say("you", request)
        self.plan_request(request)

    @work(thread=True, exclusive=True)
    def plan_request(self, request: str) -> None:
        """Plan in a worker thread so the UI stays alive while the model thinks."""
        self.call_from_thread(self._set_busy, True, "thinking...")
        try:
            result = self.harness.plan(request)
        except Exception as exc:
            self.call_from_thread(self._say, "error",
                                  f"{type(exc).__name__}: {exc}")
            self.call_from_thread(self._set_busy, False, "")
            return
        self.call_from_thread(self._planned, result)

    def _planned(self, result: RunResult) -> None:
        """Render whatever the model decided."""
        self.current = result
        self._set_busy(False, "")
        outcome = result.outcome
        style = OUTCOME_STYLE.get(outcome.value, "")

        if result.turn and result.turn.message:
            self._say("agent", result.turn.message)

        if outcome is Outcome.ANSWERED:
            self._status("[cyan]answered, nothing to run[/]")
            self.run_worker(self.query_one("#plan", PlanPane).clear(
                "That was a question. Nothing to run."), exclusive=False)
            return

        if outcome is Outcome.NEEDS_ANSWER:
            for question in (result.turn.questions if result.turn else []):
                self._say("agent", f"? {question}", "yellow")
            self._say("harness", "Answer in the box and I will re-plan.", "dim")
            self._status("[yellow]waiting for your answer[/]")
            return

        if outcome is Outcome.REFUSED:
            self._status("[magenta]the model declined this request[/]")
            return

        if outcome is Outcome.BACKEND_DOWN:
            self._say("error", result.abort_reason)
            for option in self.harness.ollama.recovery_options():
                self._say("harness", f"{option.title}  ->  {option.action}", "dim")
            self._status("[bold red]backend unreachable[/]")
            return

        if outcome is Outcome.BLOCKED:
            self._say("harness", result.abort_reason, "bold red")
            self._status(f"[{style}]blocked before execution[/]")
            if result.plan:
                self.run_worker(self.query_one("#plan", PlanPane).show(result.plan),
                                exclusive=False)
            return

        if outcome is Outcome.FAILED:
            self._say("error", result.abort_reason)
            self._status("[bold red]no usable plan[/]")
            return

        if result.plan is None:
            return
        self.run_worker(self.query_one("#plan", PlanPane).show(result.plan),
                        exclusive=False)
        pending = len(result.plan.needs_confirmation)
        self._status(
            f"[bold yellow]plan ready: {len(result.plan.steps)} step(s), "
            f"{pending} need a tick[/]" if pending else
            f"[green]plan ready: {len(result.plan.steps)} step(s), "
            "nothing needs confirmation[/]")

    # -- actions ----------------------------------------------------------- #

    @on(Button.Pressed, "#run")
    def pressed_run(self) -> None:
        """Execute the approved plan."""
        self.execute_plan(dry_run=False)

    @on(Button.Pressed, "#dryrun")
    def pressed_dry(self) -> None:
        """Run every gate and guard, execute nothing."""
        self.execute_plan(dry_run=True)

    @on(Button.Pressed, "#reject")
    def pressed_reject(self) -> None:
        """Throw the plan away."""
        # For the audit record and telemetry (HANDOFF.md §6.1's "how often
        # a human rejected a plan outright") -- Reject used to skip the
        # pipeline entirely, so this specific, most-direct rejection could
        # never be measured.
        if self.current is not None:
            done = self.harness.reject(self.current)
            if done.outcome is not Outcome.REJECTED:
                # Harness.reject() refuses rather than overwrite a plan
                # that already ran for real -- see its own docstring.
                # Found in a full-file audit alongside that fix: this
                # button was never disabled after a run completed (unlike
                # Run/Dry-run), and this handler said "Plan rejected.
                # Nothing ran." unconditionally, which would have been an
                # outright lie about a real, possibly destructive run.
                # Leave self.current and the plan pane alone -- they still
                # reflect what actually happened.
                self._say("harness", "Nothing to reject -- this plan already ran.", "dim")
                return
        self._say("harness", "Plan rejected. Nothing ran.", "dim")
        self._status("[dim]rejected[/]")
        self.current = None
        self.run_worker(self.query_one("#plan", PlanPane).clear("Rejected."),
                        exclusive=False)

    def action_dry_run(self) -> None:
        """Keyboard shortcut for a dry run."""
        self.execute_plan(dry_run=True)

    def action_rerun(self) -> None:
        """Re-plan the last request."""
        if self.last_request and not self.busy:
            self._say("you", f"{self.last_request} [re-plan]")
            self.plan_request(self.last_request)

    def action_clear_chat(self) -> None:
        """Clear the conversation and the model's context."""
        self.harness.agent.reset()
        chat = self.query_one("#chat", VerticalScroll)
        self.run_worker(chat.remove_children(), exclusive=False)  # type: ignore[arg-type]  # textual stub is stricter than the accepted runtime API
        self._say("harness", "Conversation cleared.", "dim")

    @work(thread=True, exclusive=True)
    def execute_plan(self, dry_run: bool = False) -> None:
        """Execute in a worker thread, reading approvals from the checkboxes."""
        result = self.current
        if result is None or result.plan is None:
            self.call_from_thread(self._say, "harness", "No plan to run.", "dim")
            return
        approvals = self.call_from_thread(self._approvals)
        self.call_from_thread(self._set_busy, True,
                              "dry run..." if dry_run else "running...")
        try:
            done = self.harness.execute(result, approvals, dry_run=dry_run)
        except Exception as exc:
            self.call_from_thread(self._say, "error", f"{type(exc).__name__}: {exc}")
            self.call_from_thread(self._set_busy, False, "")
            return
        self.call_from_thread(self._executed, done, dry_run)

    def _executed(self, result: RunResult, dry_run: bool) -> None:
        """Report what happened."""
        self._set_busy(False, "")
        self.current = result
        style = OUTCOME_STYLE.get(result.outcome.value, "")
        prefix = "[dry run] " if dry_run else ""

        for step_result in result.results:
            mark = "[green]ok[/]" if step_result.ok else "[bold red]fail[/]"
            self._say("harness",
                      f"{prefix}{mark} {step_result.step_id} "
                      f"[dim]{step_result.seconds:.2f}s[/]"
                      + (f"\n     {step_result.error}" if step_result.error else ""))

        if result.outcome is Outcome.COMPLETED:
            self._say("harness", f"{prefix}completed.", "bold green")
        elif result.outcome is Outcome.REJECTED:
            self._say("harness", result.abort_reason, "dim")
        else:
            self._say("error", result.abort_reason)

        if result.rollback:
            report = result.rollback
            self._say("harness",
                      f"rollback: {len(report.undone)} undone, "
                      f"{len(report.unverified)} unverified, "
                      f"{len(report.failed)} failed",
                      "green" if report.ok else "bold red")
            for entry in report.unverified + report.failed:
                self._say("error", f"  {entry.description}: {entry.error}")

        if result.audit_path:
            self._say("harness", f"audit: {result.audit_path}", "dim")
        self._status(f"[{style}]{result.outcome.value}[/]")

    def _set_busy(self, busy: bool, message: str) -> None:
        """Toggle the busy state and the status line together."""
        self.busy = busy
        self.query_one("#prompt", Input).disabled = busy
        # Run/dry-run stayed clickable mid-execution, so a double-click (or
        # a click landing right as a run finished) could dispatch a second
        # execute_plan() worker on the same already-executed result --
        # Harness.execute() now refuses that outright, but disabling the
        # buttons here stops the wasted second execution before it starts.
        # #reject joined this list for the same reason, found in a later
        # full-file audit: it was never disabled at all, and a click on it
        # after a real run completed used to silently overwrite that run's
        # outcome in the audit trail -- Harness.reject() and this handler's
        # caller both refuse that now, but disabling the button here is
        # still what stops the doomed click from being dispatched at all.
        # query(), not query_one(): the plan pane (and its buttons) only
        # exists once a plan has been rendered, so there is often nothing
        # to disable yet, and that must not be an error.
        for button_id in ("#run", "#dryrun", "#reject"):
            for button in self.query(button_id):
                button.disabled = busy

        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

        if busy:
            self._busy_message = message
            self._busy_started = time.monotonic()
            self._tick_spinner()
            self._spinner_timer = self.set_interval(0.1, self._tick_spinner)
        elif message:
            self._status(f"[dim]{message}[/]")

    def _tick_spinner(self) -> None:
        """Advance the status line's spinner frame and elapsed time."""
        frame = SPINNER_FRAMES[int(time.monotonic() * 10) % len(SPINNER_FRAMES)]
        elapsed = time.monotonic() - self._busy_started
        self._status(f"[dim]{frame} {self._busy_message} {elapsed:.1f}s[/]")


def run_tui(workdir: Path | None = None,
            harness: Harness | None = None) -> None:
    """Launch the interface."""
    HarnessApp(harness=harness, workdir=workdir).run()
