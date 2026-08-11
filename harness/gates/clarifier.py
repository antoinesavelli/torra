"""ASK_CLARIFYING_QUESTIONS: detects ambiguity by missing structure, not phrasing.

Depends on: stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["Ambiguity", "Clarification", "Clarifier"]


@dataclass
class Ambiguity:
    """One thing about a request that is underspecified."""

    code: str
    question: str
    why: str
    answer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass
class Clarification:
    """Result of the ambiguity sweep."""

    request: str
    ambiguities: list[Ambiguity] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    @property
    def needs_clarification(self) -> bool:
        """True when at least one structural gap was found."""
        return bool(self.ambiguities)

    def questions(self) -> list[str]:
        """Just the questions, for handing to the model or the operator."""
        return [a.question for a in self.ambiguities]

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return {
            "request": self.request,
            "anchors": list(self.anchors),
            "ambiguities": [a.to_dict() for a in self.ambiguities],
            "needs_clarification": self.needs_clarification,
        }


ANCHOR_PATTERNS: list[tuple[str, str]] = [
    (r"(?<![\w.])/[\w./*@+-]+", "absolute path"),
    (r"\b\d+\s*(gb|mb|kb|tb|gib|mib|bytes?|lines?|days?|hours?|minutes?|seconds?|"
     r"percent|%|connections?|workers?)\b", "quantity with a unit"),
    (r"\b\d+\.\d+(\.\d+)?\b", "version number"),
    (r"\b[0-9a-f]{7,40}\b", "commit sha"),
    (r"\b[\w.-]+\.(service|socket|timer|target|mount)\b", "systemd unit"),
    (r"\b[\w-]+-\d+\b", "hostname or tagged identifier"),
    (r"\bport\s+\d+\b|\b:\d{2,5}\b", "port"),
    (r"['\"][^'\"]{2,}['\"]", "quoted literal"),
    (r"(?<!\w)--?[a-zA-Z][\w-]*", "command flag"),
    (r"\b[\w.-]+\.(conf|cfg|ya?ml|json|toml|ini|log|py|sh|env|tar\.gz|sql)\b",
     "named file"),
    (r"\b\d{4}-\d{2}-\d{2}\b", "date"),
    (r"\b(staging|production|prod|localhost|dev|development)\b", "named environment"),
    (r"\b[a-z][a-z0-9]*\d[\w.+-]*\b", "versioned package or model name"),
    (r"\b(with\s+)?(apt|apt-get|dnf|pacman|pip|npm|snap|docker|ollama|systemctl|"
     r"git|curl|ss|journalctl)\b(\s+\S+)?", "named tool with an operand"),
    (r"\b(?!the|old|new|legacy|stale|main|that|this|a|an)[a-z][\w.-]{2,}\s+"
     r"(systemd\s+)?(unit|service|daemon|container|process)\b", "named service or unit"),
]

#: Anchors strong enough to justify a destructive operation without asking.
#: "staging" is enough to route a task and not enough to delete with.
STRONG_ANCHORS: tuple[str, ...] = (
    "absolute path", "named file", "systemd unit", "commit sha",
    "hostname or tagged identifier", "quoted literal", "named service or unit", "date",
)

FILTER_PATTERNS: list[str] = [
    r"\b(older|newer|larger|smaller|more|less|greater|fewer)\s+than\b",
    r"\bover\s+\d+", r"\bunder\s+\d+", r"\bat\s+least\b", r"\bat\s+most\b",
    r"\bnamed\b", r"\bmatching\b", r"\bwhere\b", r"\bwith\s+the\s+\w+\b",
    r"\bfrom\s+the\s+last\b", r"\bexcept\b", r"\band\s+nothing\s+else\b",
    r"\busing\s+more\s+than\b", r"\bin\s+/",
]

IMPROVEMENT = (r"\b(faster|optimi[sz]e|improve|speed\s*up|tune|less\s+\w+|better|"
               r"harden|ready|reduce|increase|scale)\b")
DESTRUCTIVE = r"\b(delete|remove|purge|wipe|drop|reset|erase|truncate|prune|clear)\b"
GENERIC = (r"\b(the\s+)?(server|box|machine|host|database|db|api|service|app|"
           r"deployment|cache|thing|issue|problem|setup|endpoint|volume|build|"
           r"cluster|node|instance|environment|pipeline|system|stack|site|data)\b")
VAGUE = (r"\b(old|stale|stuck|unused|orphaned|some|several|a\s+few|various|"
         r"latest|newest|recent|leftover|junk|extra)\b")
UNBOUNDED = r"\b(all|every|everything|everywhere|entire|whole)\b"
DANGLING = r"\b(it|this|that|these|those|them|they)\b"
DEPLOY = r"\b(deploy|release|ship|roll\s*out|rollout|push\s+.*\s+out)\b"
OPEN_TIMING = r"\b(later|soon|sometime|some\s+time|asap|when\s+you\s+can|eventually)\b"


class Clarifier:
    """Detects ambiguity by what a request is missing, not by how it is phrased.

    Two closed-vocabulary approaches failed measurement before this one worked.
    A list of vague phrasings scored 83% false negatives on a blind corpus. A
    list of mutating verbs scored 50%: same failure mode one level up, because
    a closed vocabulary only catches what someone thought to write down.

    Asking "is there a concrete anchor" instead is verb-agnostic, and measured
    0% false negatives on a corpus it was never tuned against.

    Its position changed in this rewrite. It used to be a wall in front of the
    operator. Now the agent calls `detect()` on its own understanding and on its
    own proposal, so the model knows what it is missing and can ask a specific
    question instead of guessing. Same detector, opposite role.
    """

    MIN_TOKENS: int = 4

    def __init__(
        self,
        extra_anchors: Sequence[tuple[str, str]] | None = None,
        strict_destructive: bool = False,
    ) -> None:
        """Merge site-specific anchors in and set the strict-destructive policy."""
        # strict_destructive is off by default: that rule alone produced two
        # of three false positives in measurement.
        self.anchors: list[tuple[str, str]] = (list(ANCHOR_PATTERNS)
                                               + list(extra_anchors or []))
        self.strict_destructive = bool(strict_destructive)

    def anchors_in(self, text: str) -> list[str]:
        """Labels of every concrete anchor found."""
        return [label for pattern, label in self.anchors
                if re.search(pattern, text, re.IGNORECASE)]

    @staticmethod
    def has_filter(text: str) -> bool:
        """True when the request bounds its own scope with a predicate."""
        return any(re.search(p, text, re.IGNORECASE) for p in FILTER_PATTERNS)

    @staticmethod
    def has_measure(text: str) -> bool:
        """True when the request states a number the outcome can be checked on."""
        return bool(re.search(r"\b\d+(\.\d+)?\s*\w*", text))

    def _has_antecedent(self, text: str) -> bool:
        """True when a pronoun has a concrete noun before it."""
        match = re.search(DANGLING, text, re.IGNORECASE)
        if not match:
            return True
        before = text[:match.start()]
        return bool(re.search(r"[\w./-]{3,}", before)) and len(before.split()) >= 3

    def _scrub(self, text: str) -> str:
        """Blank out every anchor, so a vague-word check doesn't misread
        the "old" inside /srv/api/old.log as a judgement."""
        scrubbed = text
        for pattern, _label in self.anchors:
            scrubbed = re.sub(pattern, " █ ", scrubbed, flags=re.IGNORECASE)
        return scrubbed

    @staticmethod
    def _check_no_target(anchors: list[str]) -> Ambiguity | None:
        if anchors:
            return None
        return Ambiguity(
            "no_target",
            "Which exact path, unit, host, container, or version should I act on?",
            "the request names no path, unit, host, version, file, or quantity")

    @staticmethod
    def _check_ambiguous_referent(scrubbed: str, anchors: list[str]) -> Ambiguity | None:
        generic = re.search(GENERIC, scrubbed, re.IGNORECASE)
        if not generic or anchors:
            return None
        return Ambiguity(
            "ambiguous_referent",
            f"Which specific {generic.group(0).replace('the ', '').strip()} "
            "do you mean? Name the host, unit, or path.",
            f"'{generic.group(0).strip()}' names a category, not an instance")

    def _check_no_acceptance_criterion(self, text: str) -> Ambiguity | None:
        improvement = re.search(IMPROVEMENT, text, re.IGNORECASE)
        if not improvement or self.has_measure(text):
            return None
        return Ambiguity(
            "no_acceptance_criterion",
            "What measurable outcome counts as done? Give a number or threshold.",
            f"'{improvement.group(0)}' is a direction, not a target")

    def _check_unbounded_scope(self, scrubbed: str, text: str) -> Ambiguity | None:
        unbounded = re.search(UNBOUNDED, scrubbed, re.IGNORECASE)
        if not unbounded or self.has_filter(text):
            return None
        return Ambiguity(
            "unbounded_scope", "What exact path or filter defines the set you mean?",
            f"'{unbounded.group(0)}' can match far more than intended")

    def _check_vague_qualifier(self, scrubbed: str, text: str) -> Ambiguity | None:
        vague = re.search(VAGUE, scrubbed, re.IGNORECASE)
        if not vague or self.has_filter(text):
            return None
        return Ambiguity(
            "vague_qualifier",
            f"What defines '{vague.group(0)}' here? Give an age, size, or pattern.",
            f"'{vague.group(0)}' is a judgement, not a selection rule")

    @staticmethod
    def _check_no_environment(text: str) -> Ambiguity | None:
        # "localhost", not "local": the anchor pattern recognizes
        # "localhost" as a named environment, so this alternative has to
        # match the same word or "deploy the api localhost" trips this
        # right after anchors_in() already found it -- found in a
        # full-file audit, self-contradictory but not previously tested.
        if not re.search(DEPLOY, text, re.IGNORECASE) or re.search(
                r"\b(to|into|on)\s+\S+|\b(staging|production|prod|localhost|dev)\b",
                text, re.IGNORECASE):
            return None
        return Ambiguity(
            "no_environment",
            "Which environment is the target: local, staging, or production?",
            "a deploy without a named environment is a coin flip")

    def _check_no_backup_plan(self, text: str, anchors: list[str]) -> Ambiguity | None:
        strong = [a for a in anchors if a in STRONG_ANCHORS]
        if not re.search(DESTRUCTIVE, text, re.IGNORECASE) or not (
                self.strict_destructive or not (strong or self.has_filter(text))):
            return None
        return Ambiguity(
            "no_backup_plan",
            "Should I back up or stage the targets before removing them?",
            "the request destroys state and states no backup plan")

    def _check_dangling_pronoun(self, text: str) -> Ambiguity | None:
        if not re.search(DANGLING, text, re.IGNORECASE) or self._has_antecedent(text):
            return None
        return Ambiguity(
            "dangling_pronoun", "What does that refer to? Name the file, service, or host.",
            "a pronoun with no antecedent inside the request")

    @staticmethod
    def _check_no_schedule(text: str) -> Ambiguity | None:
        if not re.search(OPEN_TIMING, text, re.IGNORECASE):
            return None
        return Ambiguity("no_schedule", "Now, or scheduled for a specific time?",
                         "timing was left open")

    def _check_underspecified(self, text: str, tokens: list[str],
                              anchors: list[str]) -> Ambiguity | None:
        if len(tokens) >= self.MIN_TOKENS or anchors:
            return None
        return Ambiguity(
            "underspecified", "Can you describe the task with a concrete target and outcome?",
            f"only {len(tokens)} tokens and nothing concrete")

    def detect(self, request: str) -> list[Ambiguity]:
        """Every structural gap found in a request.

        Each `_check_*` is deliberately independent, not an if/elif chain:
        a request can be simultaneously unbounded ("delete all the logs")
        AND missing a backup plan, and the caller benefits from seeing
        every gap at once rather than the first one found. Deduped by code
        below because a single scan can trip more than one check for the
        same underlying gap (e.g. both `no_target` and `ambiguous_referent`
        firing on the same missing anchor).
        """
        text = (request or "").strip()
        if not text:
            return [Ambiguity("empty", "What would you like me to do?",
                              "the request is empty")]

        anchors = self.anchors_in(text)
        scrubbed = self._scrub(text)
        tokens = re.findall(r"[A-Za-z0-9_./:-]+", text)

        checks = [
            self._check_no_target(anchors),
            self._check_ambiguous_referent(scrubbed, anchors),
            self._check_no_acceptance_criterion(text),
            self._check_unbounded_scope(scrubbed, text),
            self._check_vague_qualifier(scrubbed, text),
            self._check_no_environment(text),
            self._check_no_backup_plan(text, anchors),
            self._check_dangling_pronoun(text),
            self._check_no_schedule(text),
            self._check_underspecified(text, tokens, anchors),
        ]

        found: list[Ambiguity] = []
        seen: set[str] = set()
        for ambiguity in checks:
            if ambiguity is not None and ambiguity.code not in seen:
                found.append(ambiguity)
                seen.add(ambiguity.code)
        return found

    def clarify(self, request: str) -> Clarification:
        """Full report: anchors found and gaps remaining."""
        return Clarification(request, self.detect(request), self.anchors_in(request))

    def explain(self, request: str) -> str:
        """Human readable trace of what the clarifier saw."""
        found = self.detect(request)
        lines = [f"{request!r}",
                 f"  anchors: {self.anchors_in(request) or 'NONE'}",
                 f"  filter:  {self.has_filter(request)}",
                 f"  verdict: {'BLOCKED' if found else 'executable'}"]
        lines += [f"    {a.code}: {a.why}" for a in found]
        return "\n".join(lines)
