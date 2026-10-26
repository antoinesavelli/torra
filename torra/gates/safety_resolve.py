"""SAFETY_FIRST, half one: resolving a shell line into the concrete commands it will run.

Depends on: torra.gates.safety_types (ResolvedCommand), torra.gates.safety_tables (wrappers,
interpreters, risky variables), torra.gates.assignments (which variables a call assigns).
`torra.gates.safety.SafetyClassifier` inherits `CommandResolver`.
"""

from __future__ import annotations

import base64
import re
import shlex

from .assignments import UNSEEN, assigned_names
from .safety_tables import (
    CONTROL_PREFIXES,
    HEREDOC,
    INTERPRETERS,
    RISKY_ENV,
    STRUCTURE_ONLY,
    WRAPPER_VALUE_FLAGS,
    WRAPPERS,
)
from .safety_types import ResolvedCommand

__all__ = ["CommandResolver"]


def _part_of_a_redirect(command: str, i: int) -> bool:
    """`&` in `2>&1` or `&>file`, `|` in `>|` (overriding noclobber): an operator's, not a separator."""
    ch, before, after = command[i], command[i - 1 : i], command[i + 1 : i + 2]
    return (ch == "&" and (before in (">", "<") or after == ">")) or (ch == "|" and before == ">")


class CommandResolver:
    """Quotes, `VAR=value`, wrappers, `sh -c`, `$(...)` and base64 resolved to concrete argv lists."""

    def __init__(self, max_depth: int = 4) -> None:
        """Set the recursion limit for unwrapping nested interpreters."""
        self.max_depth = int(max_depth)
        self.parse_failures: list[str] = []
        self.env_risks: list[str] = []
        self.unwrap_limit_hit = False
        #: Every shell text resolve() parsed (the line, each `sh -c`/`eval`/`$(...)`/base64 payload) with
        #: its own `NAME=value` assignments, so classify() checks the redirects of each (HANDOFF.md §2.89).
        self.shell_texts: list[tuple[str, dict[str, str]]] = []

    # -- resolution -------------------------------------------------------- #

    @staticmethod
    def split_segments(command: str) -> list[str]:
        """Split a shell line into segments, respecting quotes."""
        # A naive re.split on ";" tears apart the payload of
        # `python3 -c "import shutil;shutil.rmtree('/x')"`, which is exactly
        # how a destructive command escapes classification.
        segments: list[str] = []
        buf: list[str] = []
        quote: str | None = None
        escaped = False
        i, n = 0, len(command)

        # Character-by-character state machine; the branch order below is
        # the precedence it depends on: a backslash-escaped char is never a
        # separator even inside a quote, a quoted char is never a separator
        # even if it's a quote of the other kind, and only once neither of
        # those applies does an operator or separator actually split.
        while i < n:
            ch = command[i]
            if escaped:
                buf.append(ch)
                escaped = False
            elif ch == "\\":
                buf.append(ch)
                escaped = True
            elif quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif command[i : i + 2] in ("&&", "||"):
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            elif _part_of_a_redirect(command, i):
                buf.append(ch)
            elif ch in (";", "|", "&", "\n"):
                segments.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1

        segments.append("".join(buf))
        return [s.strip() for s in segments if s.strip()]

    @staticmethod
    def _mask_quoted(text: str) -> str:
        """Blank every character inside a quoted span (space, same length
        and position) so `_check_redirect`'s raw-text `>` scan can't mistake
        an ordinary in-string character for a real shell redirect, which has
        no meaning inside quotes. Found testing real xlsx-editing scripts:
        `print(wb.active.max_row > 2)` -- an ordinary Python comparison
        inside a `-c` payload -- read as an output redirect and escalated to
        CAUTION. Same quote/escape state machine as split_segments above,
        repurposed to mask instead of split, rather than a second,
        potentially-diverging implementation of the same quoting rules."""
        out: list[str] = []
        quote: str | None = None
        escaped = False
        for ch in text:
            in_quote = quote is not None
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif quote:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            out.append(" " if in_quote else ch)
        return "".join(out)

    def _tokenize(self, segment: str) -> list[str]:
        """Tokenize one segment, recording rather than swallowing failures."""
        try:
            return shlex.split(segment, comments=True)
        except ValueError as exc:
            self.parse_failures.append(f"{segment!r}: {exc}")
            return segment.split()

    @staticmethod
    def _env_runs_a_string(out: list[str]) -> bool:
        """`env -S 'cmd args'` and `env --split-string=...` run a command string."""
        return out[0].rsplit("/", 1)[-1] == "env" and any(
            a.startswith(("-S", "--split-string")) for a in out[1:4]
        )

    @staticmethod
    def _consume_wrapper(out: list[str]) -> bool:
        """Pop a leading wrapper program with its flags and duration, if
        present. Mutates `out` in place; returns whether anything was consumed."""
        if not out or out[0].rsplit("/", 1)[-1] not in WRAPPERS:
            return False
        if CommandResolver._env_runs_a_string(out):
            return False  # leave `env` as the program, which asks
        wrapper = out.pop(0).rsplit("/", 1)[-1]
        value_flags = WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
        while out and (out[0] == "--" or out[0].startswith("-")):
            flag = out.pop(0)
            if flag == "--":
                break
            # A flag known to take a value consumes the next token too --
            # unless it was given as "--flag=value", which already carries
            # its value in the one token just popped.
            if flag in value_flags and "=" not in flag and out:
                out.pop(0)
        if out and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", out[0]):
            out.pop(0)
        return True

    def _strip(self, argv: list[str], env: dict[str, str]) -> list[str]:
        """Remove leading VAR=value assignments and wrapper programs."""
        # Repeats until nothing changes because wrappers nest: `env FOO=bar
        # sudo timeout 5 rm -rf /x` has an assignment, then a wrapper, then
        # another assignment-shaped token could reappear after a wrapper is
        # peeled (some wrappers accept their own VAR=value-looking flags). A
        # single pass would stop after the first layer and misclassify by
        # whatever program was still hidden underneath.
        out = list(argv)
        changed = True
        while out and changed:
            changed = False
            while out and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", out[0]):
                name, _, value = out[0].partition("=")
                env[name] = value
                self._note_assignment(name)
                out.pop(0)
                changed = True
            if out and out[0] in CONTROL_PREFIXES:
                out.pop(0)  # `do rm -rf /x`: the keyword is not the command
                changed = True
            if self._consume_wrapper(out):
                changed = True
        return out

    def _note_assignment(self, name: str) -> None:
        """Record a variable whose value changes what a later command runs."""
        if name in RISKY_ENV or name.startswith("GIT_") or name == UNSEEN:
            self.env_risks.append(name)

    def _note_builtin_assignments(self, argv: list[str]) -> None:
        """`export PATH=/x`, `read PATH`, `for PATH in /x`: every assignment besides a leading `NAME=`."""
        for name in assigned_names(argv):
            self._note_assignment(name)

    @staticmethod
    def _unparen(segment: str) -> str:
        """Drop the grouping parentheses `(cd /x && ls)` leaves glued to its first and last word."""
        segment = segment.lstrip("( \t")
        while segment.endswith(")") and segment.count(")") > segment.count("("):
            segment = segment[:-1].rstrip()
        return segment

    @staticmethod
    def _substitute(argv: list[str], env: dict[str, str]) -> list[str]:
        """Expand $VAR and ${VAR} from assignments seen earlier in the line."""
        out = []
        for token in argv:
            for name, value in env.items():
                token = token.replace(f"${{{name}}}", value).replace(f"${name}", value)
            out.append(token)
        return out

    def _decode_base64(self, command: str, require_pipe_to_shell: bool = True) -> str | None:
        """Decode a literal base64 blob piped into a shell.

        Found live: base64's own alphabet has no whitespace, so unquoted
        `echo BLOB | base64 -d | sh` used to evade the quote-requiring blob
        pattern. Matching a bounded charset run instead (no quotes needed)
        catches both forms -- the charset itself is the boundary.
        `require_pipe_to_shell=False` (for `_resolve_substitutions`) skips
        the `| sh` check: `$(echo BLOB | base64 -d)` proves it will run
        just by being inside a substitution span.
        """
        if not re.search(r"base64\s+(-d|--decode)", command):
            return None
        if require_pipe_to_shell and not re.search(r"\|\s*(sudo\s+)?(ba|z|k|da)?sh\b", command):
            return None
        # Live wire: HANDOFF.md §5.1
        for blob in re.findall(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{8,}(?![A-Za-z0-9+/=])", command):
            try:
                decoded = base64.b64decode(blob, validate=True).decode("utf-8", "replace")
                if decoded.strip():
                    return decoded
            except Exception as exc:
                self.parse_failures.append(f"base64 decode failed: {exc}")
        return None

    @staticmethod
    def _matching_paren(command: str, open_at: int) -> int | None:
        """Index just past the `)` balancing the `(` at `open_at`, or None."""
        depth, j, n = 1, open_at, len(command)
        while j < n and depth > 0:
            depth += 1 if command[j] == "(" else -1 if command[j] == ")" else 0
            j += 1
        return j if depth == 0 else None

    def _find_command_substitutions(self, command: str) -> list[str]:
        """Every `$(...)`/backtick span's inner text -- captured stdout
        becomes the text that runs next, like a `sh -c` payload.

        Found live: `$(echo BLOB|base64 -d)` classified SAFE outright --
        `split_segments` split on the `|` *inside* the substitution, and
        `_decode_base64` never saw a pipe to `sh`, only to `base64`.
        Ignores quote context deliberately (a false positive here costs
        one prompt; a false negative is the bypass this closes)."""
        spans: list[str] = []
        i, n = 0, len(command)
        while i < n:
            if command[i : i + 2] in ("$(", "<(", ">("):
                j = self._matching_paren(command, i + 2)
                if j is None:
                    break
                spans.append(command[i + 2 : j - 1])
                i = j
            elif command[i] == "`":
                j = command.find("`", i + 1)
                if j == -1:
                    break
                spans.append(command[i + 1 : j])
                i = j + 1
            else:
                i += 1
        return spans

    def _blank_substitutions(self, command: str) -> str:
        """Swap each `$(...)`/backtick span for a placeholder word. The span is judged on its own
        (`_resolve_substitutions`); its inner spaces and `|` must not split or garble the outer
        command, as in `VERSION=$(git rev-parse HEAD)`."""
        for span in self._find_command_substitutions(command):
            for opener in ("$(", "<(", ">(", "`"):
                closer = "`" if opener == "`" else ")"
                command = command.replace(f"{opener}{span}{closer}", "SUBST", 1)
        return command

    @staticmethod
    def _strip_heredocs(command: str) -> str:
        """Drop a heredoc body fed to a program that only reads it (`cat <<EOF`): it is data. One
        fed to an interpreter stays, because the interpreter runs it."""

        def keep_or_drop(m: re.Match[str]) -> str:
            lead = command[command.rfind("\n", 0, m.start()) + 1 : m.start()].split()[:1]
            runner = lead[0].rsplit("/", 1)[-1] if lead else ""
            if runner in INTERPRETERS or runner in WRAPPERS:
                return m.group(0)
            return f"<<{m.group(2)}{m.group(3)}"

        return HEREDOC.sub(keep_or_drop, command)

    def _resolve_substitutions(self, command: str, depth: int) -> list[ResolvedCommand]:
        """Recurse into every command-substitution span -- captured stdout is what runs next."""
        out: list[ResolvedCommand] = []
        for sub in self._find_command_substitutions(command):
            decoded = self._decode_base64(sub, require_pipe_to_shell=False)
            if decoded:
                out += [
                    ResolvedCommand(r.argv, r.source, "base64 via substitution")
                    for r in self.resolve(decoded, depth + 1)
                ]
            out += [
                ResolvedCommand(r.argv, r.source, "command substitution")
                for r in self.resolve(sub, depth + 1)
            ]
        return out

    @staticmethod
    def _expand_ifs(segment: str) -> str:
        """`${IFS}`/`$IFS` between tokens is a well-known obfuscation: bash
        expands IFS (default: space/tab/newline) and field-splits on it, so
        `rm${IFS}-rf${IFS}/` runs like `rm -rf /`. Found live: a whitespace
        tokenizer saw one token instead, and the command downgraded from
        CRITICAL to DANGEROUS -- still gated, but the wrong severity."""
        return re.sub(r"\$\{?IFS\}?", " ", segment)

    def resolve(self, command: str, depth: int = 0) -> list[ResolvedCommand]:
        """Expand a shell line into the concrete commands it will run."""
        # `depth` bounds two different recursions that could otherwise not
        # terminate on adversarial input: a base64 blob that decodes to
        # another base64-piped-to-sh command, and a `sh -c` payload that
        # itself contains another `sh -c`. Hitting the limit is recorded, and
        # `_context_findings` makes the line DANGEROUS: what runs past this
        # point is unseen, and unseen used to read as SAFE (GRADE_TO_A_PLAN P1.3).
        if depth > self.max_depth:
            self.parse_failures.append(f"max unwrap depth hit on {command!r}")
            self.unwrap_limit_hit = True
            return []

        resolved: list[ResolvedCommand] = []
        decoded = self._decode_base64(command)
        if decoded:
            resolved += [
                ResolvedCommand(r.argv, r.source, "base64 payload")
                for r in self.resolve(decoded, depth + 1)
            ]
        resolved += self._resolve_substitutions(command, depth)

        env: dict[str, str] = {}
        # env is filled below. A payload's data-only heredoc bodies are dropped first, so the message
        # in `git commit -m "$(cat <<'EOF' ... EOF)"` is not read as shell; the line itself is kept whole.
        self.shell_texts.append((command if depth == 0 else self._strip_heredocs(command), env))
        for segment in self.split_segments(
            self._blank_substitutions(self._strip_heredocs(command))
        ):
            segment = self._unparen(self._expand_ifs(segment))
            argv = self._substitute(self._strip(self._tokenize(segment), env), env)
            if not argv:
                continue
            self._note_builtin_assignments(argv)  # before the skip: `for PATH in /x` assigns
            if argv[0] in STRUCTURE_ONLY:
                continue
            resolved += self._unwrap(argv, segment, depth)
        return resolved

    def _unwrap(self, argv: list[str], segment: str, depth: int) -> list[ResolvedCommand]:
        """The commands one segment runs: its `eval` or `sh -c` payload, or itself."""
        program = ResolvedCommand(argv, segment).program
        out: list[ResolvedCommand] = []
        if program == "eval" and len(argv) > 1:
            out += [
                ResolvedCommand(r.argv, r.source, "eval")
                for r in self.resolve(" ".join(argv[1:]), depth + 1)
            ]
        at = next((i for i, a in enumerate(argv[:-1]) if re.fullmatch(r"-[A-Za-z]*c", a)), None)
        payload = argv[at + 1] if program in INTERPRETERS and at is not None else ""
        if not payload:
            return out + [ResolvedCommand(argv, segment)]
        # Only a *shell* interpreter's -c payload is worth recursing into as more shell
        # syntax. Python/Perl/Ruby/Node source is not shell grammar, so it is kept as one
        # opaque ResolvedCommand and left to _judge's checks of the source text.
        if program.startswith("python") or program in ("perl", "ruby", "node"):
            return out + [ResolvedCommand(argv, segment, f"{program} -c source")]
        return out + [
            ResolvedCommand(r.argv, r.source, f"{program} -c")
            for r in self.resolve(payload, depth + 1)
        ]
