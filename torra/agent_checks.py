"""The checks an `Agent` runs on a model reply before an operator sees it: placeholder answers, dropped
tasks, plans that cannot run, and the pre-screen that hands a bad proposal back for one revision.

Depends on: torra.agent_types (AgentError, Intent, Turn), torra.core (TaskSpec), torra.core.jsonreply
(extract_object), torra.gates (Clarifier, SafetyClassifier), torra.path_warnings (missing paths and
template slots), torra.prompts (the repair texts). `torra.agent.Agent` inherits `PlanChecks`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import prompts
from .agent_types import AgentError, Intent, Turn
from .core import TaskSpec
from .core.jsonreply import ReplyError, extract_object
from .gates import Clarifier, SafetyClassifier
from .path_warnings import missing_path_warnings, placeholder_tokens

__all__ = ["PlanChecks"]


class PlanChecks:
    """Complaints about a model reply, worded for the model to act on."""

    clarifier: Clarifier
    safety: SafetyClassifier
    syntax_check: Callable[[str], str | None]
    path_exists: Callable[[str], bool] | None

    #: A message that only announces an intent to look something up, never
    #: the result -- "Checking system-library for X to answer your question."
    #: Requires both ends: an opening deferral verb alone would flag a real
    #: answer that happens to start with "Checking the logs, ..."; requiring
    #: it also close by naming the question/request itself is what tells
    #: "describes the act of answering" apart from "contains an answer".
    _DEFERRAL_OPENER = re.compile(
        r"^(?:(?:i(?:'ll|'m|\s+will|\s+am)\s+)?(?:going to\s+)?"
        r"(?:check|checking|search|searching|look|looking|verify|verifying|"
        r"determine|determining|read|reading)\b|let me\b)",
        re.IGNORECASE,
    )
    _NAMES_THE_QUESTION = re.compile(
        r"\b(your |this |the )?(question|request)\.?[\"')]*$", re.IGNORECASE
    )

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Pull a JSON object out of a model reply."""
        try:
            return extract_object(text)
        except ReplyError as exc:
            raise AgentError(str(exc)) from exc

    # Live wire: HANDOFF.md §5.9
    def _repair_complaint(self, turn: Turn) -> str:
        """What, if anything, needs fixing before this turn reaches the operator."""
        if turn.intent is Intent.PLAN and turn.spec is not None:
            return self._prescreen(turn.spec)
        if turn.intent is Intent.ANSWER:
            return self._answer_dropped_a_task(turn) or self._answer_is_a_placeholder(turn)
        return ""

    # Live wire: HANDOFF.md §5.10
    def _answer_is_a_placeholder(self, turn: Turn) -> str:
        """Complaint when `message` describes looking something up, not the result."""
        message = turn.message.strip()
        if not message or not (
            self._DEFERRAL_OPENER.match(message) and self._NAMES_THE_QUESTION.search(message)
        ):
            return ""
        return (
            "- intent was 'answer' but `message` only describes checking "
            "something, it never states what was actually found. Put the "
            "real answer directly in `message`; if you have not looked "
            "anything up yet, use intent 'plan' to propose the step that will."
        )

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
            "which torra never runs for an answer -- the operator "
            "sees only your `message` and nothing else, so anything in "
            "`task` (including steps meant to produce the real values) is "
            "silently lost. Put the full answer -- real values from the "
            "observed machine state, not a promise to look them up -- "
            "directly in `message`, and drop `task` entirely. If you "
            "actually need to run something to produce the answer, use "
            "intent 'plan' instead."
        )

    def _unrunnable(self, spec: TaskSpec) -> list[str]:
        """One line per step that cannot do what it means: bash cannot parse it, or it still holds
        a template slot such as `<found-path>`. Remote steps are torra-built."""
        problems: list[str] = []
        for step in spec.to_steps(dry_run=True):
            if step.kind in ("remote_pull", "remote_push"):
                continue
            if bad := self.syntax_check(step.command):
                problems.append(
                    f"step '{step.step_id}' is not valid bash and cannot run: {bad}. Quote any "
                    "path or argument that has spaces, parentheses or other shell characters in "
                    "it, with single quotes."
                )
            if slots := placeholder_tokens(step.command):
                problems.append(
                    f"step '{step.step_id}' still has a placeholder ({', '.join(slots)}) where a "
                    "real value belongs; torra runs the text as written, and steps cannot pass "
                    "output to each other. Use the real value, or do the lookup and the action "
                    "in one command (`find ... -exec ... {} +`)."
                )
        return problems

    def _missing_paths(self, spec: TaskSpec) -> list[str]:
        """Advisory: absolute local paths that steps needing a tick name and that are not there
        (off unless wired)."""
        return (
            missing_path_warnings(
                spec,
                exists=self.path_exists,
                only=lambda step: self.safety.classify(step.command).needs_confirmation,
            )
            if self.path_exists is not None
            else []
        )

    def _prescreen(self, spec: TaskSpec) -> str:
        """Check the model's own plan before an operator ever sees it."""
        # Not a substitute for the real gates, which still run on every
        # command. This exists so an obviously bad proposal gets one
        # automatic revision round instead of burning the operator's
        # attention on a plan that was never going to be approved.
        complaints = [f"- {why}" for why in self._unrunnable(spec)]
        if missing := self._missing_paths(spec):
            complaints += [f"- {why}" for why in missing]
            complaints.append(f"- {prompts.MISSING_PATH_REPAIR}")
        for step in spec.to_steps(dry_run=True):
            verdict = self.safety.classify(step.command)
            if verdict.level.value == "critical":
                complaints.append(
                    f"- step '{step.step_id}' is CRITICAL: {verdict.reasons[0]}. "
                    + prompts.CRITICAL_REPAIR
                )
            if verdict.level.value in ("dangerous", "critical") and not step.touches:
                complaints.append(
                    f"- step '{step.step_id}' is destructive but declares no "
                    "`touches`, so torra cannot snapshot anything to "
                    "restore. Add the files it modifies."
                )
        gaps = self.clarifier.detect(spec.request)
        if gaps:
            complaints.append(
                "- the `request` line is underspecified: "
                + "; ".join(f"{g.code} ({g.why})" for g in gaps)
                + ". Restate it with concrete targets."
            )
        return "\n".join(complaints)
