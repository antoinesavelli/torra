"""Model routing: scores every rule instead of taking the first match.

Depends on: stdlib only. `accelerator` is duck-typed (expects `.gpu`/`.fits()`)
so this module has no import-time dependency on harness.core.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["ModelChoice", "ModelRouter", "model_available"]


def _normalize_tag(name: str) -> str:
    """Ollama defaults an untagged pull to ':latest'; normalize before comparing."""
    return name if ":" in name else f"{name}:latest"


def model_available(name: str, available: Iterable[str]) -> bool:
    """True when `name` matches an entry in `available`, tolerating an implicit ':latest'.

    A model pulled as `ollama pull devstral` is registered by the daemon as
    `devstral:latest`; a caller asking for the bare name it typed would
    otherwise silently miss the match and fall back to a different model.
    """
    target = _normalize_tag(name)
    return any(_normalize_tag(a) == target for a in available)


@dataclass(frozen=True)
class ModelChoice:
    """The model a task was routed to, with the rule that picked it."""

    model: str
    rule: str
    reason: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


#: (name, pattern, model, why). Debug outranks deploy by weight, not by order.
#:
#: Model targets were pruned to four models on 2026-07-28 (qwen3:32b,
#: qwen3-coder:30b, devstral:24b, qwen3.5:latest) -- everything else this
#: session had pulled for exploration is gone. "debug"/"plan"/"analyze" ->
#: qwen3:32b and "deploy" -> devstral:24b carry over the actual benchmark
#: result in scripts/dev/model_bench/REPORT.md (only the tag changed, from
#: bare "devstral" to the real pulled tag "devstral:24b" -- it silently
#: stopped matching `available` the moment anything else was pulled under
#: a different tag). "code" -> qwen3-coder:30b and "quick" -> qwen3.5:latest
#: are NOT benchmarked against this harness's JSON-planning task; they are
#: the obvious fit by name (a coder-specialized model for code generation,
#: the smallest pulled model for trivial lookups) standing in until they
#: are actually measured. Do not read them as a benchmark result the way
#: the other four rules are.
ROUTING_RULES: list[tuple[str, str, str, str]] = [
    ("debug", r"\b(debug|traceback|stack\s*trace|crash|crashing|why\s+(is|does|did)"
              r"|root\s*cause|failing|broken|bug)\b",
     "qwen3:32b", "debugging needs the deepest reasoning budget available locally"),
    ("deploy", r"\b(deploy|release|ship|rollout|provision|terraform|ansible|"
               r"systemd\s+unit)\b",
     "devstral:24b", "deployment work needs strong tool and file editing behaviour"),
    ("code", r"\b(refactor|implement|write\s+(a\s+)?(script|function|class)|patch|"
             r"migrate\s+code)\b",
     "qwen3-coder:30b", "code generation is qwen3-coder's namesake specialty "
                        "(not yet benchmarked against this harness)"),
    ("plan", r"\b(plan|architect|design|strategy|trade[- ]?off|evaluate\s+options)\b",
     "qwen3:32b", "planning benefits from long context reasoning"),
    ("analyze", r"\b(analy[sz]e|summar[iy]|review|audit|explain)\b",
     "qwen3:32b", "analysis is reasoning bound, not tool bound"),
    ("quick", r"\b(list|show|print|cat|what\s+is|check|status)\b",
     "qwen3.5:latest", "trivial lookups do not justify loading a 20b+ model"),
]

#: Parameter counts for models whose name does not carry one. Without this,
#: `devstral` parses as 0b and looks like it fits anywhere, which is how a 24b
#: model gets routed onto a 4 GB card.
KNOWN_PARAMS_B: dict[str, float] = {
    "devstral": 24.0, "codestral": 22.0, "mistral": 7.0, "mixtral": 47.0,
    "llama3": 8.0, "llama3.1": 8.0, "phi3": 3.8, "gemma2": 9.0,
    "deepseek-coder": 6.7, "starcoder2": 7.0, "granite-code": 8.0,
}

RULE_WEIGHTS: dict[str, float] = {
    "debug": 3.0, "plan": 2.0, "analyze": 1.8, "deploy": 1.5, "code": 1.5, "quick": 0.5,
}


class ModelRouter:
    """Routes a request to a local model by scoring every rule.

    First-match routing needs a hand-ordered table and cannot express "this
    matches both debug and deploy, and debug should win here but not always".
    Scoring can: each rule contributes weight times match count, and the rule
    matching **earliest** takes a 2x lead multiplier, because in an imperative
    request the leading verb is the intent and the rest is the object. That is
    what separates "review the deploy script" from "deploy the reviewed script".
    """

    LEAD_MULTIPLIER: float = 2.0

    #: Largest parameter count worth loading when inference runs on CPU.
    #: A 32b model on CPU answers in minutes, which is not a conversation.
    CPU_PARAM_CEILING_B: float = 9.0

    def __init__(
        self,
        rules: list[tuple[str, str, str, str]] | None = None,
        default_model: str = "devstral:24b",
        available: Sequence[str] | None = None,
        weights: dict[str, float] | None = None,
        accelerator: Any | None = None,
        model_params_b: dict[str, float] | None = None,
        model_chat_capable: dict[str, bool] | None = None,
    ) -> None:
        """Configure routing rules, weights, available models, and the accelerator.

        `accelerator` is what inference will actually run on: when it reports
        CPU, or reports a model won't fit in free VRAM, the router stops
        picking models that would take minutes to answer. Routing a 32b model
        onto a CPU is technically correct and practically useless, and the
        router is the only place that knows both what the task wants and what
        the machine can run.

        `default_model` is what a request gets when it matches none of
        `rules` at all -- not a downgrade, a genuinely unmatched phrasing.
        It defaults to devstral:24b rather than the smallest/cheapest model
        on purpose: a live benchmark (scripts/dev/model_bench/REPORT.md)
        found qwen3:8b was the only one of the three shipped models with a
        hard JSON-schema failure, while devstral had zero across the same
        battery and ties the largest model on accuracy at a quarter of the
        latency. An unmatched request is exactly the case with no other
        signal to route on, so it should land on the model measured most
        reliable, not the cheapest one. (The tag changed from bare
        "devstral" to "devstral:24b" on 2026-07-28 when the model catalog
        was pruned -- the benchmark result is about the model, not the tag;
        see ROUTING_RULES' own note for the rest of that pruning.)

        `model_params_b` and `model_chat_capable` are the daemon's own
        `/api/show` metadata (`OllamaClient.model_info`, name normalized to
        `tag:latest`), keyed the same way `available` is. When present they
        are checked before the name-guessing fallback in `_params_b` and
        `_chat_capable` -- see those methods for why the guess alone is not
        safe to route on for an arbitrary, freshly-pulled model.
        """
        self.rules = list(rules if rules is not None else ROUTING_RULES)
        self.default_model = default_model
        self.available = set(available) if available else None
        self.weights = dict(weights or RULE_WEIGHTS)
        self.accelerator = accelerator
        self.model_params_b = model_params_b
        self.model_chat_capable = model_chat_capable
        self.downgrades: list[tuple[str, str, str]] = []

    def _params_b(self, model: str) -> float:
        """Parameter count in billions: the daemon's own report first.

        The name-guessing fallback (a `\\d+b\\b` regex, then a small
        hardcoded table) only covers names someone already thought to add.
        Found live: a freshly-pulled catalog with names like "qwen3.5" and
        "deepseek-r1-heavy" -- no digit in either -- both guessed 0B, the
        same "looks like it fits anywhere" failure `KNOWN_PARAMS_B` was
        created to stop for devstral, just on new names nobody had reason
        to add ahead of time. `/api/show` reports the real size regardless
        of what the name looks like.
        """
        if self.model_params_b is not None:
            known = self.model_params_b.get(_normalize_tag(model))
            if known:
                return known
        match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model, re.IGNORECASE)
        if match:
            return float(match.group(1))
        base = model.split(":")[0].lower()
        for name, params in KNOWN_PARAMS_B.items():
            if base == name or base.startswith(name):
                return params
        return 0.0

    def _chat_capable(self, model: str) -> bool:
        """False only when the daemon reports this model cannot hold a
        conversation at all -- e.g. an embedding-only model. Nothing about
        a name says this (`nomic-embed-text` looks like any other pull),
        so this is `/api/show`-only; unknown defaults to True, the same
        stance `runnable()` takes on an unparseable size, because refusing
        to route on missing information would block every model whose
        `/api/show` call failed rather than just the ones that earned it.
        """
        if self.model_chat_capable is None:
            return True
        return self.model_chat_capable.get(_normalize_tag(model), True)

    def runnable(self, model: str) -> bool:
        """True when this model is worth loading on the current hardware."""
        if self.accelerator is None:
            return True
        params = self._params_b(model)
        if not params:
            return True
        if not getattr(self.accelerator, "gpu", False):
            return params <= self.CPU_PARAM_CEILING_B
        fits = getattr(self.accelerator, "fits", None)
        return bool(fits(model)) if callable(fits) else True

    def _downgrade(self, model: str, rule: str) -> tuple[str, str]:
        """Swap an unrunnable or unavailable model for the largest one that will run."""
        # Returns (model, note); the note is surfaced in the routing reason so
        # the operator can see the swap rather than wondering why a debugging
        # question got a small model. Checks availability *and* hardware fit
        # -- `runnable()` alone only reasons about VRAM/CPU, so without this,
        # a model that was never pulled (e.g. the class default when the
        # caller's `available` list doesn't happen to include it) could be
        # returned as though it were a safe, ready-to-call choice.
        available_ok = self.available is None or model_available(model, self.available)
        if available_ok and self.runnable(model) and self._chat_capable(model):
            return model, ""
        # Chat-capable candidates only: an embedding-only model (e.g.
        # nomic-embed-text) can't hold a plan/answer conversation at all,
        # and being small enough to always pass `runnable()` -- an
        # unparseable name defaults to "fits everywhere" -- made it the
        # top pick here whenever every real chat model was VRAM-starved.
        # Found live: a request with a tight VRAM budget downgraded onto
        # exactly that model instead of the smallest real chat candidate.
        candidates = sorted(
            (m for m in (self.available or set())
             if self.runnable(m) and self._chat_capable(m)),
            key=self._params_b, reverse=True)
        if candidates:
            replacement = candidates[0]
        elif self.available:
            # Nothing chat-capable fits either -- pick the smallest
            # footprint among whatever's chat-capable as the least-bad
            # option, falling back to the full available set only if
            # every pulled model claims it can't chat at all. Falling
            # through to `self.default_model` here is what produced a
            # routing_reason of "downgraded from devstral because ... "
            # that resolved right back to devstral: the class default
            # *is* devstral, so when nothing in `available` passed the
            # fit check this used to re-offer the exact model that had
            # just failed it. Found live in a real audit log
            # (disk-usage-analysis), not in a test.
            pool = [m for m in self.available if self._chat_capable(m)] or list(self.available)
            replacement = min(pool, key=self._params_b)
        else:
            replacement = self.default_model
        if not available_ok:
            why = f"{model} is not pulled locally"
        elif not getattr(self.accelerator, "gpu", False):
            why = "no GPU"
        else:
            why = f"only {getattr(self.accelerator, 'free_vram_mb', 0)} MB VRAM free"
        note = f"; downgraded from {model} because {why}"
        self.downgrades.append((rule, model, replacement))
        return replacement, note

    def score(self, request: str) -> list[tuple[str, str, float, list[str]]]:
        """Score every rule. Returns (rule, model, score, terms), best first."""
        text = request or ""
        raw: list[tuple[str, str, float, list[str], int]] = []
        for name, pattern, model, _why in self.rules:
            if self.available is not None and not model_available(model, self.available):
                continue
            if not self._chat_capable(model):
                continue
            terms, first_at = [], -1
            for match in re.finditer(pattern, text, re.IGNORECASE):
                if first_at < 0:
                    first_at = match.start()
                terms.append(match.group(0))
            if terms:
                raw.append((name, model, self.weights.get(name, 1.0) * len(terms),
                            terms, first_at))
        if not raw:
            return []
        # `lead` is the earliest match position across every rule that hit at
        # all; only the rule(s) that matched there get the lead multiplier,
        # which is what makes "the deploy is broken" score debug over deploy
        # even though both match -- debug's term starts earlier in the text.
        lead = min(row[4] for row in raw)
        scored = [(n, m, round(s * (self.LEAD_MULTIPLIER if at == lead else 1.0), 4), t)
                  for n, m, s, t, at in raw]
        # Tie-break on the rule's configured weight, not match count or name,
        # so two rules landing on the same final score still resolve the same
        # way `select()`'s confidence calculation expects.
        scored.sort(key=lambda row: (row[2], self.weights.get(row[0], 1.0)), reverse=True)
        return scored

    def select(self, request: str) -> ModelChoice:
        """Pick the highest scoring model, with the margin as confidence."""
        scored = self.score(request)
        if not scored:
            model, note = self._downgrade(self.default_model, "default")
            return ModelChoice(model, "default",
                               "no routing rule matched, using the measured-"
                               f"reliable default{note}", 0.3)
        name, model, score, terms = scored[0]
        runner_up = scored[1][2] if len(scored) > 1 else 0.0
        margin = (score - runner_up) / score if score else 0.0
        contest = (" over " + ", ".join(f"{n}({s:g})" for n, _m, s, _t in scored[1:3])
                   if len(scored) > 1 else "")
        model, note = self._downgrade(model, name)
        return ModelChoice(
            model, name,
            f"rule '{name}' scored {score:g} on "
            f"{sorted({t for t in terms if t})}{contest}{note}",
            round(0.6 + 0.4 * margin, 4))

    def explain(self, request: str) -> str:
        """Human readable score table."""
        rows = self.score(request)
        if not rows:
            return f"{request!r}: no rule matched, default {self.default_model}"
        return "\n".join([f"{request!r}:"] + [
            f"  {s:6.2f}  {n:<9} -> {m:<11} {sorted({t for t in te if t})}"
            for n, m, s, te in rows])
