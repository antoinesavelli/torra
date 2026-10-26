"""SAFETY_FIRST: resolves what a command will actually run, then classifies it.

Depends on: torra.gates.safety_resolve (resolution), torra.gates.safety_judge (per-program rules),
torra.gates.safety_types (the value types, re-exported here), torra.gates.safety_tables,
torra.gates.known_programs (what torra can vouch for as read-only).
"""

from __future__ import annotations

import codecs
import re
import shlex

from .known_programs import reads_credentials
from .safety_judge import CommandJudge
from .safety_resolve import CommandResolver
from .safety_tables import ANSI_C_QUOTED, EXPANSION, SAFE_REDIRECT_TARGETS, WORD_ENDS
from .safety_types import ORDER, ResolvedCommand, RiskLevel, Verdict

__all__ = ["RiskLevel", "ResolvedCommand", "Verdict", "SafetyClassifier"]


class SafetyClassifier(CommandResolver, CommandJudge):
    """Classifies what a command will actually run, not what it looks like.

    Regex over raw text caught five evasions out of twelve on the first pass
    in testing. This resolves first (quotes, `VAR=value`, wrappers, `sh -c`
    and `$(...)` recursion, base64) then classifies resolved argv, keeping
    the regex table only as a backstop -- what stands between an agent's
    hallucinated `rm -rf /var` and the disk.
    """

    @staticmethod
    def _redirect_word(rest: str) -> str | None:
        """The word bash reads at the start of `rest`: quotes removed, `$'..'` escapes decoded, ended
        by a blank or an unquoted `;|&()<>`. A `$(` becomes a backtick, so the substitution stays in the
        word instead of ending it. None when it cannot be read (an unclosed quote, a bad escape)."""
        try:
            rest = ANSI_C_QUOTED.sub(
                lambda m: shlex.quote(codecs.decode(m.group(1), "unicode_escape")), rest
            )
            rest = rest.replace('$"', '"').replace("$(", "`")
            lexer = shlex.shlex(rest, posix=True, punctuation_chars=WORD_ENDS)
            lexer.whitespace_split, lexer.commenters = True, ""
            return lexer.get_token() or ""
        except ValueError:
            return None

    @staticmethod
    def _masked_word_end(masked: str, at: int) -> int:
        """Where the word after `at` ends in quote-masked text, whose quoted characters are already blank."""
        while at < len(masked) and masked[at] in " \t":
            at += 1
        while at < len(masked) and not masked[at].isspace() and masked[at] not in WORD_ENDS:
            at += 1
        return at

    def _check_redirects(self) -> list[tuple[RiskLevel, str]]:
        """`_check_redirect` over every shell text resolve() parsed, each finding once: a payload is
        quoted in the line that carries it, so the line's own scan never sees its `>`."""
        return list(
            dict.fromkeys(
                f for text, env in self.shell_texts for f in self._check_redirect(text, env)
            )
        )

    def _check_redirect(self, text: str, env: dict[str, str]) -> list[tuple[RiskLevel, str]]:
        """Redirect-target risk: a concern neither _judge nor BACKSTOP looks
        at. Captures the operator (`>`/`>>`) alongside the target -- found
        in a full-file audit: classification was already correct for `>>`,
        but the reason text always said "overwrites" even for an append.

        Matches against `_mask_quoted(text)`, not the raw text: a bare `>`
        has no meaning inside a shell quote, and this used to fire on any
        `>` character anywhere, including a Python comparison operator
        inside a `-c` payload's own quotes. The target is then read from
        the original `text` after that operator, as bash reads the word
        (quotes removed) -- masking only blanks in-quote characters, so
        positions are identical in both strings."""
        masked = self._mask_quoted(text)
        writes_a_file = False
        # Only an unquoted `>`, `>>`, `>|` or `>&` redirects, but its target is read from the original
        # text the way bash reads the word: `> "/etc/passwd"` writes /etc/passwd (HANDOFF.md §2.86-§2.88).
        for redirect in re.finditer(r">[>|&]?", masked):
            op, target = redirect.group(), self._redirect_word(text[redirect.end() :])
            if target is None:
                return [(RiskLevel.DANGEROUS, "redirect target torra cannot read")]
            target = self._expand(target, env)
            if op == ">&" and re.fullmatch(r"\d*-?", target):
                continue  # `2>&1`, `>&2`, `>&-`: a descriptor, not a file
            if target in SAFE_REDIRECT_TARGETS:
                # A stream sink or documented no-op; nothing is overwritten. Blanked so the
                # generic check below ignores it too, while a later redirect is still judged.
                end = self._masked_word_end(masked, redirect.end())
                masked = masked[: redirect.start()] + " " * (end - redirect.start()) + masked[end:]
                continue
            found = self._judge_redirect_target(op, target)
            if found:
                return [found]
            writes_a_file = writes_a_file or op == ">&"
        if writes_a_file or re.search(r">(?!&)\s*[^>\s]", masked):
            return [(RiskLevel.CAUTION, "output redirect that overwrites")]
        return []

    @staticmethod
    def _expand(word: str, env: dict[str, str]) -> str:
        """`$NAME` and `${NAME}` replaced by a value the line assigned; any other expansion is left as it is."""
        return re.sub(
            r"\$(?:\{(\w+)\}|(\w+))", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), word
        )

    def _judge_redirect_target(self, op: str, target: str) -> tuple[RiskLevel, str] | None:
        """CRITICAL for a system path, DANGEROUS for a target only known at run time, else None."""
        if target in ("", "/"):
            return None  # a bare `/` cannot be written ("Is a directory"); markdown `<dir>/<name>` reads as one
        if self._protected(target):
            return (
                RiskLevel.CRITICAL,
                f"redirect {'appends to' if op == '>>' else 'overwrites'} {target}",
            )
        if EXPANSION.search(target) and target.startswith(("$", "`", "/")):
            return RiskLevel.DANGEROUS, f"redirect target {target} is only known at run time"
        return None

    def classify(self, command: str) -> Verdict:
        """Classify a command by resolving it, with regex as the backstop."""
        # Three independent signal sources feed `levels`, and the final
        # verdict is the max across all of them, never just the first or the
        # most specific: argv-based _judge (resolved, evasion-resistant),
        # the regex BACKSTOP (catches whatever resolve() failed to parse),
        # and the redirect-target check (a concern neither of the other two
        # looks at). A command only needs one of the three to flag it; this
        # is deliberately not an either/or so a gap in one layer doesn't
        # silently downgrade what another layer already caught.
        text = (command or "").strip()
        self.parse_failures = []
        self.shell_texts = []
        self.env_risks = []
        self.unwrap_limit_hit = False
        reasons: list[str] = []
        levels: list[RiskLevel] = []

        resolved = self.resolve(text)
        for rc in resolved:
            for level, why in self._judge(rc):
                origin = f" (via {rc.unwrapped_from})" if rc.unwrapped_from else ""
                reasons.append(
                    f"{level.value.upper()}: {why}{origin} [argv: {' '.join(rc.argv[:6])}]"
                )
                levels.append(level)

        unparsed = bool(self.parse_failures) or self.unwrap_limit_hit
        for level, why in self._check_backstop(text, unparsed) + self._check_redirects():
            levels.append(level)
            reasons.append(f"{level.value.upper()}: {why}")

        if self.parse_failures:
            reasons.append(
                f"NOTE: {len(self.parse_failures)} parse issue(s), "
                "the regex backstop carried the classification"
            )

        for level_, why in self._context_findings(resolved, levels, text):
            levels.append(level_)
            reasons.append(f"{level_.value.upper()}: {why}")
        level = max(levels, key=ORDER.index) if levels else RiskLevel.SAFE
        if not reasons:
            reasons.append(f"no destructive pattern in {len(resolved)} resolved command(s)")
        return Verdict(text, level, reasons, resolved)

    def _context_findings(
        self, resolved: list[ResolvedCommand], levels: list[RiskLevel], text: str
    ) -> list[tuple[RiskLevel, str]]:
        """Risks that come from the line as a whole: a changed search path, nesting too deep to
        see into, or a `cd` into a system directory before a write, after which relative paths
        cannot be judged."""
        found = [
            (RiskLevel.DANGEROUS, f"sets {n}, which changes what later commands run")
            for n in self.env_risks
        ]
        if self.unwrap_limit_hit:
            found.append(
                (
                    RiskLevel.DANGEROUS,
                    f"nests commands more than {self.max_depth} "
                    "levels deep, past what torra unwraps, so what runs is unseen",
                )
            )
        entered = any(
            rc.program == "cd" and any(self._protected(o) for o in rc.argv[1:]) for rc in resolved
        )
        if entered and any(lv in (RiskLevel.CAUTION, RiskLevel.DANGEROUS) for lv in levels):
            found.append((RiskLevel.DANGEROUS, "changes into a system directory and then writes"))
        fed = re.findall(r"(?<![<\d&])<\s*(?![<(])([^\s;|&<>()]+)", self._mask_quoted(text))
        if any(reads_credentials(f) for f in fed):
            found.append((RiskLevel.DANGEROUS, "feeds a file that holds credentials to a command"))
        return found

    def explain(self, command: str) -> str:
        """Human readable trace of how a command was resolved and scored."""
        verdict = self.classify(command)
        lines = [
            f"command: {command}",
            f"verdict: {verdict.level.value}",
            f"resolved into {len(verdict.resolved)} concrete command(s):",
        ]
        for rc in verdict.resolved:
            origin = f"  <- {rc.unwrapped_from}" if rc.unwrapped_from else ""
            lines.append(f"  {' '.join(rc.argv)}{origin}")
        lines.append("reasons:")
        lines += [f"  {r}" for r in verdict.reasons]
        return "\n".join(lines)
