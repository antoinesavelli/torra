"""Terminal interface: chat on the left, a Plan/Evidence sidebar on the right,
one tab per session.

Depends on: textual, harness.core (OllamaClient, OllamaStatus, ContextStep, SessionMeta,
SessionStore), harness.gates (UndoGenerator), harness.pipeline (Harness and friends),
harness.telemetry (CrashReporter, health_summary, crash_summary).

You never approve a command you have not seen classified: nothing executes
until every step showing DANGEROUS or CRITICAL has been individually ticked.

Each tab is an independent conversation -- its own Harness/Agent/context,
its own SessionStore-backed transcript on disk (see HANDOFF.md §2.23 for
why multiple Harness instances sharing a workdir need session-scoped
undo=/task_id_prefix, the two collisions this design has to avoid).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)
from textual.widgets.option_list import Option

from .core import ContextStep, OllamaStatus, SessionMeta, SessionStore
from .gates import UndoGenerator
from .pipeline import Harness, Outcome, Plan, RunResult, StepPlan
from .telemetry import CrashReporter, crash_summary, health_summary

__all__ = ["HarnessApp", "run_tui"]

#: A local model can take anywhere from a couple seconds to over a minute
#: (see scripts/dev/model_bench/REPORT.md) -- a static "thinking..." label
#: looks identical whether it's been one second or frozen for sixty, so the
#: status line animates and counts up instead of just sitting there.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Live step output is capped for display, not for what actually gets
#: captured -- StepResult.output stays the full text regardless. A step
#: with pathological output volume (a runaway loop, a verbose install)
#: would otherwise grow this widget, and the render cost of re-drawing it,
#: without bound. Generous on purpose: this is the only place an operator
#: ever sees a successful step's output at all (see SessionTab.output_lines'
#: docstring), so losing the head of long output under a tight cap would
#: hide real information, not just noise.
STEP_OUTPUT_DISPLAY_CAP = 8000

#: OpenRouter's catalog is hundreds of models deep, and OpenRouterClient
#: has no `models()` call to enumerate it anyway -- not something a
#: picker meant for a handful of taps should try to fetch live even if
#: it did. This is only the models actually confirmed end-to-end with a
#: real request (a live slug lookup plus a real `harness plan`/`ask`
#: call, see HANDOFF.md §2.38). Add one here only after doing that, not
#: on a slug guess.
VERIFIED_OPENROUTER_MODELS: list[str] = [
    "openrouter/x-ai/grok-4.6",
    "openrouter/deepseek/deepseek-v4-pro",
]


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
    """One line in the conversation pane.

    Mutable via `set_text` (not just constructed once) so a live-streaming
    reasoning block can grow in place -- see `HarnessApp._append_thinking`
    -- instead of remounting a new widget per token.
    """

    _PREFIXES = {"you": "[bold cyan]you[/]", "agent": "[bold green]agent[/]",
                "harness": "[bold blue]harness[/]", "error": "[bold red]error[/]"}

    def __init__(self, who: str, text: str, style: str = "") -> None:
        """Render one speaker-prefixed, optionally styled line."""
        super().__init__(markup=True)
        self._prefix = self._PREFIXES.get(who, who)
        self._style = style
        self.set_text(text)

    def set_text(self, text: str) -> None:
        """Replace this line's body, re-rendering with the same prefix/style."""
        body = f"[{self._style}]{text}[/]" if self._style else text
        self.update(f"{self._prefix}  {body}")


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
            f"[dim]{self.index}.[/] [bold]{escape(sp.step.step_id)}[/]  "
            f"[{style}]{level.upper()}[/]", markup=True)
        yield Static(f"  [white]{escape(sp.step.command)}[/]", markup=True,
                     classes="step-command")
        if sp.step.description and sp.step.description != sp.step.command:
            yield Static(f"  [dim]{escape(sp.step.description)}[/]", markup=True)

        for reason in sp.verdict.reasons[:2]:
            yield Static(f"  [dim]{escape(reason[:96])}[/]", markup=True)

        if sp.undos:
            yield Static(
                f"  [green]undo:[/] {escape(sp.undos[0].description[:80])}"
                + (f" [dim](+{len(sp.undos) - 1} more)[/]" if len(sp.undos) > 1 else ""),
                markup=True)
        else:
            yield Static("  [yellow]no automatic undo for this step[/]", markup=True)

        if sp.needs_confirmation:
            # id must be a Textual-safe identifier (letters/digits/_/- only)
            # -- sp.step.step_id is arbitrary model- or human-chosen text (a
            # space, period, or colon is entirely ordinary), so the DOM id
            # is built from the always-safe 1-based index instead. Found
            # live: a DANGEROUS step id like "delete old logs" raised
            # BadIdentifier out of Checkbox's own constructor, crashing the
            # whole plan pane before a human ever saw the approval box --
            # for exactly the highest-risk steps this checkbox exists for.
            # _approvals() below reads the real step_id back off
            # `plan_step`, never off this id string.
            yield Checkbox(f"approve {escape(sp.step.step_id)}", value=False,
                           id=f"approve-{self.index}")


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
            f"[bold]{escape(plan.spec.task_id)}[/]\n"
            f"[dim]{escape(plan.spec.request)}[/]\n"
            f"model [bold]{escape(plan.model)}[/]  worst [{style}]{worst.upper()}[/]  "
            f"undo coverage {plan.coverage.get('coverage_ratio', 0):.0%}"
            + (f"  [dim]({plan.repairs} repair(s))[/]" if plan.repairs else ""),
            markup=True, classes="plan-header"))

        if plan.uncovered:
            await self.mount(Static(
                f"[yellow]no automatic undo for: {escape(', '.join(plan.uncovered))}[/]",
                markup=True))
        if plan.preconditions and not plan.preconditions.passed:
            await self.mount(Static(
                "[bold red]preconditions failed: "
                + escape(", ".join(r.name for r in plan.preconditions.blocking_failures))
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


class EvidencePane(VerticalScroll):
    """The sidebar's other tab: a live health/crash summary, recomputed
    each time the operator switches to it (see `_sidebar_evidence_activated`).
    Same source data `harness evidence` prints, just in the TUI."""

    def compose(self) -> ComposeResult:
        """Start empty -- filled in on first activation, not at mount time."""
        yield Static("[dim]Switch to this tab to see the current summary.[/]",
                     markup=True, id="evidence-empty")

    async def refresh_summary(self, harness: Harness, crash_reporter: CrashReporter) -> None:
        """Replace the pane contents with a fresh summary.

        `escape(...)` before `markup=True`, same as ChatLine/StepCard/
        PlanPane.show -- an exception message inside crash_summary()'s
        output is arbitrary text and can contain `[`/`]` (HANDOFF.md
        §2.25's MarkupError lesson applies here too).
        """
        await self.remove_children()
        await self.mount(Static(escape(health_summary(harness.telemetry.read())),
                                markup=True, classes="evidence-block"))
        await self.mount(Static(escape(crash_summary(crash_reporter)),
                                markup=True, classes="evidence-block"))


@dataclass
class SessionTab:
    """One tab's state: its own Harness/Agent/context, its own busy flag.

    Everything a handler used to read off `HarnessApp` directly (`.harness`,
    `.current`, `.last_request`, busy/spinner state) now lives here instead,
    keyed by session_id on `HarnessApp.sessions`.
    """

    session_id: str
    meta: SessionMeta
    harness: Harness
    current: RunResult | None = None
    last_request: str = ""
    busy: bool = False
    busy_message: str = ""
    busy_started: float = 0.0
    spinner_timer: Timer | None = None
    # Runtime-only, deliberately never persisted through SessionStore/
    # meta.json: reopening a session must drop back to local-only routing,
    # not silently keep making paid remote calls -- the conservative
    # failure mode. Don't "fix" this into a SessionMeta schema change.
    model_override: str | None = None
    # Also runtime-only and just as deliberately not persisted: reasoning
    # tokens are a live look at the model thinking, not a durable record --
    # see HANDOFF.md's note on why this stays out of SessionStore/telemetry.
    reasoning_line: ChatLine | None = None
    reasoning_text: str = ""
    # Live per-step execution output. Unlike reasoning_line, these are not
    # cleared when the run finishes -- StepResult's captured stdout is
    # never shown anywhere else in the TUI, so the live line is the only
    # place an operator ever sees a successful step's output, and stays as
    # part of that run's visible transcript. Keyed by step_id so a
    # multi-step plan gets one growing line per step, not one shared line.
    output_lines: dict[str, ChatLine] = field(default_factory=dict)
    output_text: dict[str, str] = field(default_factory=dict)


class SessionPicker(ModalScreen[str | None]):
    """Pick a past session to reopen. Dismisses with a session_id, or None."""

    BINDINGS = [("escape", "dismiss_picker", "Cancel")]

    CSS = """
    SessionPicker { align: center middle; }
    #picker-box {
        width: 70%; height: auto; max-height: 80%;
        border: round $primary; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, metas: list[SessionMeta]) -> None:
        """Hold the sessions to list -- fetched by the caller, not here."""
        super().__init__()
        self._metas = metas

    def compose(self) -> ComposeResult:
        """A titled box: one option per session, newest first."""
        with Vertical(id="picker-box"):
            yield Static("Reopen a session  [dim](Escape to cancel)[/]", markup=True)
            if not self._metas:
                yield Static("[dim]No saved sessions yet.[/]", markup=True)
                return
            yield OptionList(*[
                Option(f"{escape(meta.name) or '(unnamed)'}  "
                      f"[dim]{time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.updated_at))}[/]",
                      id=meta.session_id)
                for meta in self._metas])

    @on(OptionList.OptionSelected)
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        """Hand the chosen session_id back to whoever opened the picker."""
        self.dismiss(event.option_id)

    def action_dismiss_picker(self) -> None:
        """Escape: reopen nothing."""
        self.dismiss(None)


class ModelPicker(ModalScreen[str | None]):
    """Pick a model for this tab's `/model` override. Dismisses with the
    chosen model name, `""` for automatic routing, or None to cancel."""

    BINDINGS = [("escape", "dismiss_picker", "Cancel")]

    CSS = """
    ModelPicker { align: center middle; }
    #picker-box {
        width: 70%; height: auto; max-height: 80%;
        border: round $primary; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, models: list[str], current_override: str | None) -> None:
        """`models` is the router's own chat-capable local set plus
        `VERIFIED_OPENROUTER_MODELS` -- fetched/assembled by the caller,
        not here, so this stays free of harness/router wiring.
        `current_override` is highlighted with a marker, not pre-selected
        -- OptionList has no notion of a default highlight that survives
        `Option` construction cleanly."""
        super().__init__()
        self._models = models
        self._current = current_override

    def compose(self) -> ComposeResult:
        """A titled box: automatic routing first, then each model --
        local ones plain, remote ones tagged so a tap doesn't accidentally
        trigger a paid call."""
        with Vertical(id="picker-box"):
            yield Static("Choose a model  [dim](Escape to cancel)[/]", markup=True)
            auto_marker = " [dim](current)[/]" if not self._current else ""
            options = [Option(f"(automatic routing){auto_marker}", id="")]
            for model in self._models:
                marker = " [dim](current)[/]" if model == self._current else ""
                tag = " [dim](remote, paid)[/]" if model.startswith("openrouter/") else ""
                options.append(Option(f"{escape(model)}{tag}{marker}", id=model))
            yield OptionList(*options)
            yield Static("[dim]Only hand-verified openrouter/... models are listed "
                         "-- use /model <name> for any other remote model.[/]",
                         markup=True)

    @on(OptionList.OptionSelected)
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        """Hand the chosen model name back to whoever opened the picker."""
        self.dismiss(event.option_id)

    def action_dismiss_picker(self) -> None:
        """Escape: leave the override untouched."""
        self.dismiss(None)


class HarnessApp(App):
    """The tabbed terminal interface -- one tab per independent session."""

    CSS = """
    Screen { layout: vertical; }
    #sessions { height: 1fr; }
    #left  { width: 50%; border-right: solid $primary; }
    #right { width: 50%; }
    #chat  { height: 1fr; padding: 0 1; }
    #plan  { height: 1fr; padding: 0 1; }
    #evidence { height: 1fr; padding: 0 1; }
    .evidence-block { padding: 0 0 1 0; }
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
        # Not ctrl+c: Textual's own default for that key is "help_quit",
        # which deliberately does NOT quit -- its docstring says so
        # explicitly ("Ctrl+C is now bound to copy if an input / textarea
        # is focused... this makes it possible, even likely, that a user
        # may do it accidentally -- which would be maddening"). Screen's
        # own default binding sends ctrl+c to copy_text (selected-text
        # copy) instead. Overriding it back to "quit" here silently
        # re-broke exactly what Textual's maintainers fixed: found live,
        # any attempt to copy selected chat/plan text via ctrl+c closed
        # the whole app instead.
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+r", "rerun", "Re-plan"),
        ("ctrl+d", "dry_run", "Dry run"),
        ("ctrl+l", "clear_chat", "Clear chat"),
        ("ctrl+t", "new_tab", "New tab"),
        ("ctrl+w", "close_tab", "Close tab"),
        ("ctrl+tab", "next_tab", "Next tab"),
        ("ctrl+shift+tab", "prev_tab", "Previous tab"),
        ("ctrl+o", "open_picker", "Reopen session"),
        # Not ctrl+m: it's the terminal's ANSI carriage-return code, the
        # same byte a physical Enter key sends -- Textual's own keys.py
        # documents it as a hard alias of "enter"
        # (KEY_ALIASES = {"enter": ["ctrl+m"], ...}), so a real terminal
        # can never deliver it as a distinct key event; it just submits
        # the focused Input instead. Found live: `pilot.press("ctrl+m")`
        # in tests passed anyway, because Pilot.press() injects the named
        # key directly and never round-trips through the byte-level ANSI
        # decoder that swallows it for real -- the tests validated the
        # binding's wiring, not that a real terminal could ever produce
        # the keypress. F2 sidesteps the whole class of C0-control-code
        # collisions (also see tab/ctrl+i, escape/ctrl+[, newline/ctrl+j
        # in the same KEY_ALIASES table) and isn't claimed by Input's own
        # BINDINGS the way ctrl+d/ctrl+w already are (HANDOFF.md §2.38).
        ("f2", "choose_model", "Choose model"),
    ]

    def __init__(self, harness: Harness | None = None,
                 workdir: Path | None = None) -> None:
        """Use the injected harness for tests (as the first tab), or build a default one."""
        super().__init__()
        initial_harness = harness or Harness(workdir=workdir or Path.home() / ".harness")
        self.ollama = initial_harness.ollama
        self.backends = initial_harness.agent.backends
        self.session_store = SessionStore(initial_harness.workdir)
        self._crash_reporter = CrashReporter(
            initial_harness.workdir / "telemetry" / "crashes.jsonl")
        self.sessions: dict[str, SessionTab] = {}
        self.active_session_id = ""
        self._register_session(initial_harness, self.session_store.create())

    def _handle_exception(self, error: Exception) -> None:
        """The one hook Textual ever calls for an unhandled TUI exception --
        `App.run()` never re-raises past this, confirmed by reading its own
        source (HANDOFF.md §2.29). A crash reporter that itself fails must
        never block the app from actually exiting, hence the bare try."""
        try:
            self._crash_reporter.record(error, site="tui")
        except Exception:
            pass
        super()._handle_exception(error)

    # -- session lifecycle --------------------------------------------------- #

    def _register_session(self, harness: Harness, meta: SessionMeta) -> str:
        """Wire persistence into `harness.agent` and track it as a new tab.

        Does not mount any widget -- `compose()` handles the very first
        session, `action_new_tab()` mounts every one after that.
        """
        def _persist(step: ContextStep, session_id: str = meta.session_id) -> None:
            self.session_store.append(session_id, step)

        harness.agent._on_context_change = _persist
        harness.agent.task_id_prefix = f"{meta.session_id[:8]}-"
        raw_record_count = self.session_store.rehydrate_compressor(
            meta.session_id, harness.agent.context)
        # A reopened session's Agent starts at turn 0 -- without this, its
        # first few turns would reuse task_id/audit-filename values its
        # earlier life already used, silently overwriting those audit
        # records (HANDOFF.md §2.26). The *raw* record count, not
        # len(context.entries): rehydrate_compressor may have just
        # compressed that down to a handful of summaries, which would
        # under-count and reopen the exact collision this closes.
        harness.agent.fast_forward_turn_counter(raw_record_count)
        self.sessions[meta.session_id] = SessionTab(
            session_id=meta.session_id, meta=meta, harness=harness)
        self.active_session_id = meta.session_id
        return meta.session_id

    def _session_id_of(self, widget: object) -> str:
        """Walk up from any widget inside a tab to that tab's session_id.

        Matches specifically the session-level TabPane (id prefix "tab-"),
        not just "the nearest TabPane" -- the sidebar's own inner
        TabbedContent (HANDOFF.md §2.30) has its own TabPanes ("sidebar-
        plan"/"sidebar-evidence") nested *inside* every session tab. Found
        live: stopping at the first ancestor TabPane meant a click on Run/
        Reject/Dry-run inside the sidebar's Plan tab resolved
        session_id == "sidebar-plan" (str.removeprefix is a no-op on a
        non-matching prefix) -- never a real key in self.sessions, so
        every such click raised an unconditional KeyError, which
        _handle_exception (§2.29) turns into the whole app exiting."""
        node = widget
        while node is not None and not (
                isinstance(node, TabPane) and (node.id or "").startswith("tab-")):
            node = getattr(node, "parent", None)
        if node is None or not node.id:
            return self.active_session_id
        return node.id.removeprefix("tab-")

    @staticmethod
    def _build_sidebar() -> TabbedContent:
        """The right-hand pane: Plan (the gated proposal) + Evidence (a live
        health/crash summary) as tabs, not two stacked panes.

        `TabbedContent(*children)` is NOT `Horizontal`/`Vertical`'s generic
        `*children: Widget` slot -- it overrides the positional-args slot
        to mean tab-label strings (`*titles: ContentType`) and never
        forwards them to `super().__init__()`. Passing `TabPane(...)`
        instances positionally crashes immediately inside `render_str()`
        ("AttributeError: 'TabPane' object has no attribute 'translate'"),
        confirmed live, not read off documentation. `compose_add_child()`
        is the actual hook -- the same one Textual's own `with
        TabbedContent(): yield TabPane(...)` compose syntax calls
        internally, but with no `self.app` dependency, so it works from a
        plain `@staticmethod` invoked before the app is even running
        (HANDOFF.md §2.30)."""
        sidebar = TabbedContent(id="sidebar")
        sidebar.compose_add_child(TabPane("Plan", PlanPane(id="plan"), id="sidebar-plan"))
        sidebar.compose_add_child(
            TabPane("Evidence", EvidencePane(id="evidence"), id="sidebar-evidence"))
        return sidebar

    def _build_tab_pane(self, session_id: str, title: str) -> TabPane:
        # Content(title), not the raw string: a session's name is derived
        # from arbitrary operator/model text (agent.py:build/first request),
        # and TabPane/Tab both parse a plain str as Textual markup -- found
        # live, a request as ordinary as "list files in [/mnt/x]" raised
        # MarkupError out of the tab's own constructor before anything was
        # ever mounted. A Content instance is returned unparsed.
        return TabPane(
            Content(title),
            Horizontal(
                Vertical(
                    VerticalScroll(id="chat"),
                    Input(placeholder="what do you want done?", id="prompt"),
                    id="left"),
                Vertical(self._build_sidebar(), id="right")),
            id=f"tab-{session_id}")

    # -- layout ------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """One TabbedContent, one tab per session, a shared status line."""
        yield Header(show_clock=True)
        initial_id = self.active_session_id
        with TabbedContent(id="sessions"):
            yield self._build_tab_pane(initial_id, self.sessions[initial_id].meta.name or "1")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Greet the initial session, and say plainly whether the model backend is reachable."""
        self.title = "harness"
        status = self.ollama.health()
        self.sub_title = f"{self.ollama.host}  {status.value}"
        self._greet(self.active_session_id, status)
        self._prompt(self.active_session_id).focus()

    def _greet(self, session_id: str, status: OllamaStatus) -> None:
        """Print the standard opening lines into one session's chat pane."""
        self._say(session_id, "harness", "Describe what you want done. I plan it, every gate "
                             "checks it, you approve it, then it runs.")
        if status is OllamaStatus.HEALTHY:
            self._say(session_id, "harness", "f2 to choose a model", "dim")
        else:
            self._say(session_id, "error",
                      f"Ollama is {status.value} at {escape(self.ollama.host)}. "
                      f"{escape(self.ollama.last_error)}")
            for option in self.ollama.recovery_options():
                self._say(session_id, "harness",
                          f"{escape(option.title)}  ->  {escape(option.action)}", "dim")

    # -- backward-compat: single-session attribute access ------------------- #

    @property
    def harness(self) -> Harness:
        """The active tab's Harness -- kept for callers/tests written before tabs existed."""
        return self.sessions[self.active_session_id].harness

    @property
    def current(self) -> RunResult | None:
        """The active tab's current plan/result."""
        return self.sessions[self.active_session_id].current

    @current.setter
    def current(self, value: RunResult | None) -> None:
        self.sessions[self.active_session_id].current = value

    @property
    def last_request(self) -> str:
        """The active tab's last request."""
        return self.sessions[self.active_session_id].last_request

    @property
    def busy(self) -> bool:
        """Whether the active tab is busy."""
        return self.sessions[self.active_session_id].busy

    @busy.setter
    def busy(self, value: bool) -> None:
        self.sessions[self.active_session_id].busy = value

    # -- helpers ----------------------------------------------------------- #

    def _tab_pane(self, session_id: str) -> TabPane:
        return self.query_one(f"#tab-{session_id}", TabPane)

    def _chat(self, session_id: str) -> VerticalScroll:
        return self._tab_pane(session_id).query_one("#chat", VerticalScroll)

    def _plan_pane(self, session_id: str) -> PlanPane:
        return self._tab_pane(session_id).query_one("#plan", PlanPane)

    def _evidence_pane(self, session_id: str) -> EvidencePane:
        return self._tab_pane(session_id).query_one("#evidence", EvidencePane)

    def _prompt(self, session_id: str) -> Input:
        return self._tab_pane(session_id).query_one("#prompt", Input)

    def _say(self, session_id: str, who: str, text: str, style: str = "") -> None:
        """Append a line to one session's chat pane and scroll to it."""
        chat = self._chat(session_id)
        chat.mount(ChatLine(who, text, style))
        chat.scroll_end(animate=False)

    def _append_thinking(self, session_id: str, chunk: str) -> None:
        """Grow this session's live reasoning line by one delta, mounting
        it on the first chunk. Called via `call_from_thread` from the plan
        worker thread -- `on_thinking` fires from inside `OllamaClient.chat`'s
        streaming read loop, which runs on that same worker thread."""
        tab = self.sessions.get(session_id)
        if tab is None:
            return  # the tab was closed while a stream was still in flight
        if tab.reasoning_line is None:
            tab.reasoning_line = ChatLine("agent", "", "dim italic")
            self._chat(session_id).mount(tab.reasoning_line)
        tab.reasoning_text += chunk
        tab.reasoning_line.set_text(escape(tab.reasoning_text))
        self._chat(session_id).scroll_end(animate=False)

    def _clear_thinking(self, session_id: str) -> None:
        """Drop the live reasoning line once a turn is done -- ephemeral by
        design, never written to SessionStore or telemetry (see
        `SessionTab.reasoning_line`'s docstring)."""
        tab = self.sessions.get(session_id)
        if tab is None or tab.reasoning_line is None:
            return
        self.run_worker(tab.reasoning_line.remove(), exclusive=False)  # type: ignore[arg-type]  # textual stub is stricter than the accepted runtime API
        tab.reasoning_line = None
        tab.reasoning_text = ""

    def _reset_step_output(self, session_id: str) -> None:
        """Forget the previous run's step-output widgets before a new one
        starts -- otherwise a step id reused by a fresh run (the common
        case: ctrl+r re-runs the same plan) would keep growing an old,
        already-finalized line instead of starting a new one."""
        tab = self.sessions.get(session_id)
        if tab is None:
            return
        tab.output_lines = {}
        tab.output_text = {}

    def _append_step_output(self, session_id: str, step_id: str, chunk: str) -> None:
        """Grow this step's live output line by one chunk, mounting it on
        the first chunk for this step_id. Called via `call_from_thread`
        from the execute worker thread -- `on_output` fires from inside
        `shell()`'s streaming read loop, which runs on that same thread."""
        tab = self.sessions.get(session_id)
        if tab is None:
            return  # the tab was closed while a step was still running
        if step_id not in tab.output_lines:
            line = ChatLine("harness", "", "dim")
            tab.output_lines[step_id] = line
            tab.output_text[step_id] = ""
            self._chat(session_id).mount(line)
        tab.output_text[step_id] += chunk
        text = tab.output_text[step_id]
        shown = text[-STEP_OUTPUT_DISPLAY_CAP:] if len(text) > STEP_OUTPUT_DISPLAY_CAP else text
        tab.output_lines[step_id].set_text(f"[{escape(step_id)}] {escape(shown)}")
        self._chat(session_id).scroll_end(animate=False)

    def _status(self, session_id: str, text: str) -> None:
        """Update the shared status line -- only if `session_id` is the active tab."""
        if session_id == self.active_session_id:
            self.query_one("#status", Static).update(text)

    def _approvals(self, session_id: str) -> dict[str, bool]:
        """Read every approve checkbox in one session's plan pane, keyed by the
        step's real id -- not the checkbox's own DOM id, which is built
        from the card's index and cannot hold an arbitrary step id."""
        out: dict[str, bool] = {}
        for card in self._tab_pane(session_id).query(StepCard):
            for box in card.query(Checkbox):
                out[card.plan_step.step.step_id] = bool(box.value)
        return out

    # -- input ------------------------------------------------------------- #

    @on(Input.Submitted, "#prompt")
    def submitted(self, event: Input.Submitted) -> None:
        """Send the typed request to the model."""
        session_id = self._session_id_of(event.input)
        tab = self.sessions[session_id]
        request = event.value.strip()
        if not request or tab.busy:
            return
        event.input.value = ""
        if request == "/model" or request.startswith("/model "):
            self._set_model_override(session_id, request.removeprefix("/model").strip())
            return
        tab.last_request = request
        if not tab.meta.name:
            self._name_session(session_id, request)
        self._say(session_id, "you", escape(request))
        self.plan_request(session_id, request)

    def _set_model_override(self, session_id: str, override: str) -> None:
        """`/model <name>` pins this tab's next requests to one model,
        bypassing routing entirely; bare `/model` clears it back to
        routing's own choice. An unconfigured `openrouter/...` override
        isn't checked here -- `OpenRouterClient.chat()` itself raises a
        specific "OPENROUTER_API_KEY is not set" error the moment it's
        actually used, which reaches the operator through the normal
        backend-down path (`_outcome_backend_down`) with the right
        recovery options already attached. A second, shallower check here
        would just be a second place for that fact to drift out of sync."""
        tab = self.sessions[session_id]
        tab.model_override = override or None
        if not override:
            self._say(session_id, "harness",
                      "model override cleared -- routing decides again", "dim")
            return
        self._say(session_id, "harness", f"model override set to {escape(override)}", "dim")

    def _name_session(self, session_id: str, request: str) -> None:
        """Give an unnamed session a name from its first real request."""
        tab = self.sessions[session_id]
        name = request[:40]
        renamed = self.session_store.rename(session_id, name)
        if renamed is not None:
            tab.meta = renamed
        tabbed = self.query_one("#sessions", TabbedContent)
        tabbed.get_tab(f"tab-{session_id}").label = Content(name)

    def plan_request(self, session_id: str, request: str) -> None:
        """Plan in a worker thread, one exclusivity group per session so
        tab A's execute/plan never blocks or gets cancelled by tab B's.

        `busy` flips synchronously here, before dispatch -- see
        `execute_plan()`'s docstring for why: without it, a second
        request landing before the worker thread got around to setting
        `busy` itself (a `submitted()`/`action_rerun()` guard check racing
        the first call's own thread round trip) could start a second
        `Harness.plan()` concurrently against the same Agent."""
        self._set_busy(session_id, True, "thinking...")
        self.run_worker(partial(self._plan_request_body, session_id, request),
                        thread=True, exclusive=True, group=f"session-{session_id}")

    def _plan_request_body(self, session_id: str, request: str) -> None:
        tab = self.sessions[session_id]

        def on_thinking(chunk: str) -> None:
            # Runs on this same worker thread, inside OllamaClient.chat's
            # streaming read loop -- call_from_thread is the only safe way
            # from here to touch a widget the event loop owns.
            self.call_from_thread(self._append_thinking, session_id, chunk)

        try:
            result = tab.harness.plan(request, model=tab.model_override,
                                      on_thinking=on_thinking)
        except Exception as exc:
            self.call_from_thread(self._clear_thinking, session_id)
            self.call_from_thread(self._say, session_id, "error",
                                  f"{type(exc).__name__}: {escape(str(exc))}")
            self.call_from_thread(self._set_busy, session_id, False, "")
            return
        self.call_from_thread(self._planned, session_id, result)

    def _outcome_answered(self, session_id: str, result: RunResult) -> None:
        self._status(session_id, "[cyan]answered, nothing to run[/]")
        self.run_worker(self._plan_pane(session_id).clear(
            "That was a question. Nothing to run."), exclusive=False)

    def _outcome_needs_answer(self, session_id: str, result: RunResult) -> None:
        for question in (result.turn.questions if result.turn else []):
            self._say(session_id, "agent", f"? {escape(question)}", "yellow")
        self._say(session_id, "harness", "Answer in the box and I will re-plan.", "dim")
        self._status(session_id, "[yellow]waiting for your answer[/]")

    def _outcome_refused(self, session_id: str, result: RunResult) -> None:
        self._status(session_id, "[magenta]the model declined this request[/]")

    def _outcome_backend_down(self, session_id: str, result: RunResult) -> None:
        self._say(session_id, "error", escape(result.abort_reason))
        # The backend that actually failed, not always self.ollama: result.turn
        # is None on this path, so tab.model_override is the only place the
        # attempted model is still known -- "restart Ollama" is exactly the
        # wrong advice for a failed openrouter/... request.
        tab = self.sessions[session_id]
        backend, _ = tab.harness.agent.resolve_backend(tab.model_override or "")
        for option in backend.recovery_options(tab.model_override or "unknown"):
            self._say(session_id, "harness",
                      f"{escape(option.title)}  ->  {escape(option.action)}", "dim")
        self._status(session_id, "[bold red]backend unreachable[/]")

    def _outcome_blocked(self, session_id: str, result: RunResult) -> None:
        style = OUTCOME_STYLE.get(result.outcome.value, "")
        self._say(session_id, "harness", escape(result.abort_reason), "bold red")
        self._status(session_id, f"[{style}]blocked before execution[/]")
        if result.plan:
            self.run_worker(self._plan_pane(session_id).show(result.plan), exclusive=False)

    def _outcome_failed(self, session_id: str, result: RunResult) -> None:
        self._say(session_id, "error", escape(result.abort_reason))
        self._status(session_id, "[bold red]no usable plan[/]")

    def _outcome_plan_ready(self, session_id: str, result: RunResult) -> None:
        if result.plan is None:
            return
        self.run_worker(self._plan_pane(session_id).show(result.plan), exclusive=False)
        pending = len(result.plan.needs_confirmation)
        self._status(session_id,
            f"[bold yellow]plan ready: {len(result.plan.steps)} step(s), "
            f"{pending} need a tick[/]" if pending else
            f"[green]plan ready: {len(result.plan.steps)} step(s), "
            "nothing needs confirmation[/]")

    def _planned(self, session_id: str, result: RunResult) -> None:
        """Render whatever the model decided, dispatched by outcome."""
        tab = self.sessions.get(session_id)
        if tab is None:
            return  # the tab was closed while this was in flight
        tab.current = result
        self._clear_thinking(session_id)
        self._set_busy(session_id, False, "")
        if result.turn and result.turn.message:
            self._say(session_id, "agent", escape(result.turn.message))

        handlers: dict[Outcome, Callable[[str, RunResult], None]] = {
            Outcome.ANSWERED: self._outcome_answered,
            Outcome.NEEDS_ANSWER: self._outcome_needs_answer,
            Outcome.REFUSED: self._outcome_refused,
            Outcome.BACKEND_DOWN: self._outcome_backend_down,
            Outcome.BLOCKED: self._outcome_blocked,
            Outcome.FAILED: self._outcome_failed,
        }
        handlers.get(result.outcome, self._outcome_plan_ready)(session_id, result)

    # -- actions ----------------------------------------------------------- #

    @on(Button.Pressed, "#run")
    def pressed_run(self, event: Button.Pressed) -> None:
        """Execute the approved plan."""
        self.execute_plan(self._session_id_of(event.button), dry_run=False)

    @on(Button.Pressed, "#dryrun")
    def pressed_dry(self, event: Button.Pressed) -> None:
        """Run every gate and guard, execute nothing."""
        self.execute_plan(self._session_id_of(event.button), dry_run=True)

    @on(Button.Pressed, "#reject")
    def pressed_reject(self, event: Button.Pressed) -> None:
        """Throw the plan away."""
        # For the audit record and telemetry (HANDOFF.md §6.1's "how often
        # a human rejected a plan outright") -- Reject used to skip the
        # pipeline entirely, so this specific, most-direct rejection could
        # never be measured.
        session_id = self._session_id_of(event.button)
        tab = self.sessions[session_id]
        if tab.current is not None:
            done = tab.harness.reject(tab.current)
            if done.outcome is not Outcome.REJECTED:
                # Harness.reject() refuses rather than overwrite a plan
                # that already ran for real -- see its own docstring.
                # Found in a full-file audit alongside that fix: this
                # button was never disabled after a run completed (unlike
                # Run/Dry-run), and this handler said "Plan rejected.
                # Nothing ran." unconditionally, which would have been an
                # outright lie about a real, possibly destructive run.
                # Leave tab.current and the plan pane alone -- they still
                # reflect what actually happened.
                self._say(session_id, "harness",
                          "Nothing to reject -- this plan already ran.", "dim")
                return
        self._say(session_id, "harness", "Plan rejected. Nothing ran.", "dim")
        self._status(session_id, "[dim]rejected[/]")
        tab.current = None
        self.run_worker(self._plan_pane(session_id).clear("Rejected."), exclusive=False)

    def action_dry_run(self) -> None:
        """Keyboard shortcut for a dry run, on the active tab."""
        session_id = self.active_session_id
        if not self.sessions[session_id].busy:
            self.execute_plan(session_id, dry_run=True)

    def action_rerun(self) -> None:
        """Re-plan the active tab's last request."""
        session_id = self.active_session_id
        tab = self.sessions[session_id]
        if tab.last_request and not tab.busy:
            self._say(session_id, "you", f"{escape(tab.last_request)} \\[re-plan]")
            self.plan_request(session_id, tab.last_request)

    def action_clear_chat(self) -> None:
        """Clear the active tab's conversation and the model's context.

        Does not delete persisted history -- writes a marker record to the
        transcript instead, so a reopened session shows the boundary
        rather than a gap. Consistent with this codebase's bias toward
        loud/honest over silently destructive (ResumePoint.load raises
        rather than guessing; TaskLogger never truncates)."""
        session_id = self.active_session_id
        tab = self.sessions[session_id]
        tab.harness.agent.reset()
        self.session_store.append(session_id, ContextStep(
            f"clear-{time.time()}", "system", "[cleared by operator]"))
        chat = self._chat(session_id)
        self.run_worker(chat.remove_children(), exclusive=False)  # type: ignore[arg-type]  # textual stub is stricter than the accepted runtime API
        self._say(session_id, "harness", "Conversation cleared.", "dim")

    def action_new_tab(self) -> None:
        """Open a new, independent, empty session as a new tab."""
        base_workdir = self.sessions[self.active_session_id].harness.workdir
        meta = self.session_store.create()
        self._mount_session_tab(self._build_session_harness(base_workdir, meta.session_id),
                                meta, greet=True)

    def _build_session_harness(self, base_workdir: Path, session_id: str) -> Harness:
        """A Harness for one tab -- session-scoped `undo=` so its shadow dir
        never collides with another tab sharing the same `base_workdir`
        (HANDOFF.md §2.23). `backends=self.backends`: without it, every tab
        after the first would silently lose access to any alt backend
        (e.g. OpenRouter) the app was started with (HANDOFF.md §2.27)."""
        return Harness(
            workdir=base_workdir, ollama=self.ollama, backends=self.backends,
            undo=UndoGenerator(shadow_dir=base_workdir / f"harness-shadow-{session_id}"))

    def _mount_session_tab(self, harness: Harness, meta: SessionMeta, *, greet: bool) -> None:
        """Register, mount, and activate a tab -- greeted if new, replayed if reopened."""
        self._register_session(harness, meta)
        pane = self._build_tab_pane(meta.session_id, meta.name or str(len(self.sessions)))

        async def _mount_and_activate() -> None:
            tabbed = self.query_one("#sessions", TabbedContent)
            await tabbed.add_pane(pane)
            tabbed.active = f"tab-{meta.session_id}"
            if greet:
                self._greet(meta.session_id, self.ollama.last_status)
            else:
                self._replay_transcript(meta.session_id)
            self._prompt(meta.session_id).focus()

        self.run_worker(_mount_and_activate(), exclusive=False)

    def action_open_picker(self) -> None:
        """Ctrl+O: browse and reopen a past session."""
        metas = self.session_store.list_sessions()

        def _handle(chosen: str | None) -> None:
            if chosen:
                self._open_or_switch_to(chosen)

        self.push_screen(SessionPicker(metas), _handle)

    def action_choose_model(self) -> None:
        """F2: pick this tab's model override from the locally
        available, chat-capable set plus `VERIFIED_OPENROUTER_MODELS` --
        OpenRouter's full catalog isn't enumerable here (no `models()`
        call exists, and it would be hundreds of entries anyway), so
        anything beyond that hand-verified list still goes through
        `/model <name>`."""
        session_id = self.active_session_id
        tab = self.sessions[session_id]
        router = tab.harness.agent.router
        local = sorted(m for m in self.ollama.models() if router._chat_capable(m))
        models = local + VERIFIED_OPENROUTER_MODELS

        def _handle(chosen: str | None) -> None:
            if chosen is not None:
                self._set_model_override(session_id, chosen)

        self.push_screen(ModelPicker(models, tab.model_override), _handle)

    def _open_or_switch_to(self, session_id: str) -> None:
        """Switch to a session already open in a tab, or reopen it in a new one."""
        if session_id in self.sessions:
            self.query_one("#sessions", TabbedContent).active = f"tab-{session_id}"
            return
        meta = self.session_store.load_meta(session_id)
        if meta is None:
            self._status(self.active_session_id,
                        "[bold red]that session no longer exists[/]")
            return
        base_workdir = self.sessions[self.active_session_id].harness.workdir
        self._mount_session_tab(self._build_session_harness(base_workdir, session_id),
                                meta, greet=False)

    def _replay_transcript(self, session_id: str) -> None:
        """Show a reopened session's saved history in its chat pane.

        The live model context is already rehydrated by `_register_session`
        (`rehydrate_compressor`) -- this is purely the visible replay, plus
        recovering `last_request` so Ctrl+R still works after a reopen.
        """
        speaker = {"user": "you", "assistant": "agent"}
        tab = self.sessions[session_id]
        for record in self.session_store.load_transcript(session_id):
            who = speaker.get(record.role, "harness")
            self._say(session_id, who, escape(record.content),
                      "" if record.role in speaker else "dim")
            if record.role == "user":
                tab.last_request = record.content

    def action_close_tab(self) -> None:
        """Close the active tab. Refuses to close the last tab or a busy one."""
        session_id = self.active_session_id
        if len(self.sessions) <= 1:
            self._status(session_id, "[dim]can't close the last tab[/]")
            return
        if self.sessions[session_id].busy:
            self._status(session_id, "[dim]can't close a tab while it's busy[/]")
            return

        async def _remove() -> None:
            # del self.sessions[session_id] must happen after the pane is
            # actually gone, not just requested -- remove_pane() returns an
            # AwaitComplete, so this has to run inside a real coroutine to
            # await it, not be handed to run_worker() as a bare value (which
            # only starts it, doesn't wait for it before the dict mutation).
            await self.query_one("#sessions", TabbedContent).remove_pane(f"tab-{session_id}")
            del self.sessions[session_id]

        self.run_worker(_remove(), exclusive=False)

    def action_next_tab(self) -> None:
        """Focus the next tab, wrapping around."""
        # next/previous live on the Tabs bar TabbedContent composes
        # internally (ContentTabs), not on TabbedContent itself. Scoped to
        # "#sessions > Tabs" (direct child), not a bare Tabs query -- the
        # sidebar's own inner TabbedContent (HANDOFF.md §2.30) composes one
        # too, and query_one doesn't raise on ambiguity in this Textual
        # version, it silently returns the first breadth-first match.
        self.query_one("#sessions > Tabs", Tabs).action_next_tab()

    def action_prev_tab(self) -> None:
        """Focus the previous tab, wrapping around."""
        self.query_one("#sessions > Tabs", Tabs).action_previous_tab()

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Keep active_session_id in sync, and repaint the shared status line."""
        pane_id = event.pane.id or ""
        session_id = pane_id.removeprefix("tab-")
        if session_id not in self.sessions:
            return
        self.active_session_id = session_id
        tab = self.sessions[session_id]
        if tab.busy:
            self._tick_spinner(session_id)
        else:
            self._status(session_id, "")
        self._prompt(session_id).focus()

    @on(TabbedContent.TabActivated, "#sidebar", pane="#sidebar-evidence")
    def _sidebar_evidence_activated(self, event: TabbedContent.TabActivated) -> None:
        """Recompute the Evidence tab's content each time the operator
        switches to it -- not continuously, not after every task; a
        proportionate first version. `pane="#sidebar-evidence"`, not a
        bare "#sidebar" selector: confirmed live that a bare selector also
        fires (uselessly) on every Plan-tab (re)activation and on initial
        mount, which would need filtering out by hand; this fires exactly
        once, only when the Evidence tab itself activates (HANDOFF.md
        §2.30). Unscoped handlers like `_tab_activated` above still see
        this same event too -- `@on` selectors don't shadow each other."""
        session_id = self._session_id_of(event.tabbed_content)
        tab = self.sessions.get(session_id)
        if tab is None:
            return  # the tab was closed while this was in flight
        self.run_worker(
            self._evidence_pane(session_id).refresh_summary(tab.harness, self._crash_reporter),
            exclusive=False)

    def execute_plan(self, session_id: str, dry_run: bool = False) -> None:
        """Execute in a worker thread, reading approvals from the checkboxes.

        Imperative `run_worker(..., group=...)`, not `@work` -- `@work`'s
        `group` is fixed at decoration time, one string shared by every
        call regardless of which session it's for, which would have put
        every tab's execute back in a single exclusive group (tab B's
        click cancelling tab A's in-flight run) or, with exclusive=False,
        dropped single-flight protection for a second click in the *same*
        session. Per-session groups need a group computed per call.

        Found live: `busy` used to flip to True only once the worker
        thread itself got around to it (a `call_from_thread` round trip
        after fetching approvals) -- a second dispatch landing inside that
        window (a fast double-click, or Ctrl+D right after a click) still
        read `busy=False` and started a second, genuinely concurrent
        `Harness.execute()` against the same mutable `RunResult`. Checking
        *and* flipping `busy` here, synchronously, before the worker is
        even dispatched, closes the window instead of just shrinking it --
        this method only ever runs on the main thread, so there is no
        yield point between the check and the flip for a second call to
        land in. `plan_request()` has the same fix for the same reason."""
        tab = self.sessions[session_id]
        if tab.busy:
            return
        self._reset_step_output(session_id)
        self._set_busy(session_id, True, "dry run..." if dry_run else "running...")
        self.run_worker(partial(self._execute_plan_body, session_id, dry_run),
                        thread=True, exclusive=True, group=f"session-{session_id}")

    def _execute_plan_body(self, session_id: str, dry_run: bool) -> None:
        tab = self.sessions[session_id]
        result = tab.current
        if result is None or result.plan is None:
            self.call_from_thread(self._say, session_id, "harness", "No plan to run.", "dim")
            self.call_from_thread(self._set_busy, session_id, False, "")
            return
        approvals = self.call_from_thread(self._approvals, session_id)

        def on_output(step_id: str, chunk: str) -> None:
            self.call_from_thread(self._append_step_output, session_id, step_id, chunk)

        try:
            done = tab.harness.execute(result, approvals, dry_run=dry_run,
                                       on_output=on_output)
        except Exception as exc:
            self.call_from_thread(self._say, session_id, "error",
                                  f"{type(exc).__name__}: {escape(str(exc))}")
            self.call_from_thread(self._set_busy, session_id, False, "")
            return
        self.call_from_thread(self._executed, session_id, done, dry_run)

    def _executed(self, session_id: str, result: RunResult, dry_run: bool) -> None:
        """Report what happened."""
        tab = self.sessions.get(session_id)
        if tab is None:
            return  # the tab was closed while this was in flight
        self._set_busy(session_id, False, "")
        tab.current = result
        style = OUTCOME_STYLE.get(result.outcome.value, "")
        prefix = "[dry run] " if dry_run else ""

        for step_result in result.results:
            mark = "[green]ok[/]" if step_result.ok else "[bold red]fail[/]"
            self._say(session_id, "harness",
                      f"{prefix}{mark} {escape(step_result.step_id)} "
                      f"[dim]{step_result.seconds:.2f}s[/]"
                      + (f"\n     {escape(step_result.error)}"
                         if step_result.error else ""))

        if result.outcome is Outcome.COMPLETED:
            self._say(session_id, "harness", f"{prefix}completed.", "bold green")
        elif result.outcome is Outcome.REJECTED:
            self._say(session_id, "harness", escape(result.abort_reason), "dim")
        else:
            self._say(session_id, "error", escape(result.abort_reason))

        if result.summary:
            self._say(session_id, "agent", escape(result.summary))

        if result.rollback:
            report = result.rollback
            self._say(session_id, "harness",
                      f"rollback: {len(report.undone)} undone, "
                      f"{len(report.unverified)} unverified, "
                      f"{len(report.failed)} failed",
                      "green" if report.ok else "bold red")
            for entry in report.unverified + report.failed:
                self._say(session_id, "error",
                          f"  {escape(entry.description)}: {escape(entry.error)}")

        if result.audit_path:
            self._say(session_id, "harness", f"audit: {escape(str(result.audit_path))}", "dim")
        self._status(session_id, f"[{style}]{result.outcome.value}[/]")

    def _set_busy(self, session_id: str, busy: bool, message: str) -> None:
        """Toggle one session's busy state and, if it's the active tab, the status line."""
        tab = self.sessions.get(session_id)
        if tab is None:
            return  # the tab was closed while this was in flight
        tab.busy = busy
        self._prompt(session_id).disabled = busy
        # Run/dry-run stayed clickable mid-execution, so a double-click (or
        # a click landing right as a run finished) could dispatch a second
        # execute on the same already-executed result -- Harness.execute()
        # now refuses that outright, but disabling the buttons here stops
        # the wasted second execution before it starts. #reject joined this
        # list for the same reason, found in a later full-file audit: it
        # was never disabled at all, and a click on it after a real run
        # completed used to silently overwrite that run's outcome in the
        # audit trail -- Harness.reject() and this handler's caller both
        # refuse that now, but disabling the button here is still what
        # stops the doomed click from being dispatched at all.
        # query(), not query_one(): the plan pane (and its buttons) only
        # exists once a plan has been rendered, so there is often nothing
        # to disable yet, and that must not be an error.
        pane = self._tab_pane(session_id)
        for button_id in ("#run", "#dryrun", "#reject"):
            for button in pane.query(button_id):
                button.disabled = busy

        if tab.spinner_timer is not None:
            tab.spinner_timer.stop()
            tab.spinner_timer = None

        if busy:
            tab.busy_message = message
            tab.busy_started = time.monotonic()
            self._tick_spinner(session_id)
            tab.spinner_timer = self.set_interval(0.1, partial(self._tick_spinner, session_id))
        elif message:
            self._status(session_id, f"[dim]{message}[/]")

    def _tick_spinner(self, session_id: str) -> None:
        """Advance one session's spinner frame and elapsed time, if it's the active tab."""
        tab = self.sessions.get(session_id)
        if tab is None:
            return  # the tab was closed while this was in flight
        frame = SPINNER_FRAMES[int(time.monotonic() * 10) % len(SPINNER_FRAMES)]
        elapsed = time.monotonic() - tab.busy_started
        self._status(session_id, f"[dim]{frame} {tab.busy_message} {elapsed:.1f}s[/]")


def run_tui(workdir: Path | None = None,
            harness: Harness | None = None) -> None:
    """Launch the interface."""
    HarnessApp(harness=harness, workdir=workdir).run()
