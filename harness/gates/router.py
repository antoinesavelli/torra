"""Model routing: scores every rule instead of taking the first match.

Depends on: stdlib only. `accelerator` is duck-typed (expects `.gpu`/`.fits()`)
so this module has no import-time dependency on harness.core.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["ModelChoice", "ModelRouter"]


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
ROUTING_RULES: list[tuple[str, str, str, str]] = [
    ("debug", r"\b(debug|traceback|stack\s*trace|crash|crashing|why\s+(is|does|did)"
              r"|root\s*cause|failing|broken|bug)\b",
     "qwen3:32b", "debugging needs the deepest reasoning budget available locally"),
    ("deploy", r"\b(deploy|release|ship|rollout|provision|terraform|ansible|"
               r"systemd\s+unit)\b",
     "devstral", "deployment work needs strong tool and file editing behaviour"),
    ("code", r"\b(refactor|implement|write\s+(a\s+)?(script|function|class)|patch|"
             r"migrate\s+code)\b",
     "devstral", "code generation is devstral's trained specialty"),
    ("plan", r"\b(plan|architect|design|strategy|trade[- ]?off|evaluate\s+options)\b",
     "qwen3:32b", "planning benefits from long context reasoning"),
    ("analyze", r"\b(analy[sz]e|summar[iy]|review|audit|explain)\b",
     "qwen3:32b", "analysis is reasoning bound, not tool bound"),
    ("quick", r"\b(list|show|print|cat|what\s+is|check|status)\b",
     "qwen3:8b", "trivial lookups do not justify loading a 32b model"),
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
        default_model: str = "qwen3:8b",
        available: Sequence[str] | None = None,
        weights: dict[str, float] | None = None,
        accelerator: Any | None = None,
    ) -> None:
        """Configure routing rules, weights, available models, and the accelerator.

        `accelerator` is what inference will actually run on: when it reports
        CPU, or reports a model won't fit in free VRAM, the router stops
        picking models that would take minutes to answer. Routing a 32b model
        onto a CPU is technically correct and practically useless, and the
        router is the only place that knows both what the task wants and what
        the machine can run.
        """
        self.rules = list(rules if rules is not None else ROUTING_RULES)
        self.default_model = default_model
        self.available = set(available) if available else None
        self.weights = dict(weights or RULE_WEIGHTS)
        self.accelerator = accelerator
        self.downgrades: list[tuple[str, str, str]] = []

    def _params_b(self, model: str) -> float:
        """Parameter count in billions, from the name or the known table."""
        match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model, re.IGNORECASE)
        if match:
            return float(match.group(1))
        base = model.split(":")[0].lower()
        for name, params in KNOWN_PARAMS_B.items():
            if base == name or base.startswith(name):
                return params
        return 0.0

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
        """Swap an unrunnable model for the largest one that will run."""
        # Returns (model, note); the note is surfaced in the routing reason so
        # the operator can see the swap rather than wondering why a debugging
        # question got a small model.
        if self.runnable(model):
            return model, ""
        candidates = sorted(
            (m for m in (self.available or set()) if self.runnable(m)),
            key=self._params_b, reverse=True)
        replacement = candidates[0] if candidates else self.default_model
        why = ("no GPU" if not getattr(self.accelerator, "gpu", False)
               else f"only {getattr(self.accelerator, 'free_vram_mb', 0)} MB VRAM free")
        note = f"; downgraded from {model} because {why}"
        self.downgrades.append((rule, model, replacement))
        return replacement, note

    def score(self, request: str) -> list[tuple[str, str, float, list[str]]]:
        """Score every rule. Returns (rule, model, score, terms), best first."""
        text = request or ""
        raw: list[tuple[str, str, float, list[str], int]] = []
        for name, pattern, model, _why in self.rules:
            if self.available is not None and model not in self.available:
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
        lead = min(row[4] for row in raw)
        scored = [(n, m, round(s * (self.LEAD_MULTIPLIER if at == lead else 1.0), 4), t)
                  for n, m, s, t, at in raw]
        scored.sort(key=lambda row: (row[2], self.weights.get(row[0], 1.0)), reverse=True)
        return scored

    def select(self, request: str) -> ModelChoice:
        """Pick the highest scoring model, with the margin as confidence."""
        scored = self.score(request)
        if not scored:
            model, note = self._downgrade(self.default_model, "default")
            return ModelChoice(model, "default",
                               "no routing rule matched, using the cheap "
                               f"default{note}", 0.3)
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
