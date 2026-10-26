"""SAFETY_FIRST, half two: judging one resolved command's argv against the per-program rules.

Depends on: torra.gates.safety_types (RiskLevel, ResolvedCommand), torra.gates.safety_tables
(risk table, protected prefixes, install and source markers), torra.gates.known_programs (what torra
can vouch for). `torra.gates.safety.SafetyClassifier` inherits `CommandJudge`.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable

from .known_programs import BOUNDED_WRITE, PROTECTED_WRITE, READ_ONLY, reads_credentials, trust
from .safety_tables import (
    BACKSTOP,
    CRITICAL_UNITS,
    DOCUMENT_CONTAINER_EXTENSIONS,
    INSTALL_VERBS,
    LISTING_TOOLS,
    METADATA_ONLY,
    PACKAGE_INSTALLERS,
    PIP_SOURCE_LONG,
    PIP_VALUE_SHORT,
    PROGRAM_RISK,
    PROTECTED_PREFIXES,
    UNPARSED_ONLY,
    UNTRUSTED_SOURCE_FLAGS,
    UNTRUSTED_SOURCE_OPERAND,
)
from .safety_types import ResolvedCommand, RiskLevel

__all__ = ["CommandJudge"]


class CommandJudge:
    """Risk findings for resolved commands: protected paths, per-program rules, the regex backstop."""

    @staticmethod
    def _protected(path: str) -> bool:
        """True when a path targets a system location worth protecting."""
        if path.startswith(("~", "$HOME", "${HOME}")):
            return True  # the home directory, which /home already protects by its absolute path
        cleaned = posixpath.normpath(re.sub(r"/{2,}", "/", path.rstrip("/*") or "/"))
        return cleaned in PROTECTED_PREFIXES or any(
            cleaned.startswith(p + "/") for p in PROTECTED_PREFIXES if p != "/")

    def protects(self, path: str) -> bool:
        """Public form of `_protected`, for steps torra builds itself (a remote pull's local file)."""
        return self._protected(path)

    @staticmethod
    def _looks_like_document_container(path: str) -> bool:
        """True when a path's extension is a zip-based office document
        format, regardless of whether it's absolute or relative -- see
        DOCUMENT_CONTAINER_EXTENSIONS for why this exists alongside, not
        instead of, `_protected`."""
        return path.rsplit("/", 1)[-1].lower().endswith(DOCUMENT_CONTAINER_EXTENSIONS)

    @staticmethod
    def _argv_shape(argv: list[str]) -> tuple[list[str], list[str], set[str]]:
        """Split argv into flags, operands, and single-char short-flag letters."""
        flags = [a for a in argv[1:] if a.startswith("-")]
        operands = [a for a in argv[1:] if not a.startswith("-")]
        chars = set("".join(f.lstrip("-") for f in flags if not f.startswith("--")))
        return flags, operands, chars

    def _judge(self, rc: ResolvedCommand) -> list[tuple[RiskLevel, str]]:
        """Risk findings for one resolved command, dispatched per program.

        One recognized program, one rule, one `_judge_*` helper holding
        that program's real logic -- not a lookup table encoding risk
        levels or a class per program. classify() takes the max of this
        and the regex BACKSTOP, so a program missing here is still caught.
        """
        found: list[tuple[RiskLevel, str]] = []
        program, argv = rc.program, rc.argv
        flags, operands, chars = self._argv_shape(argv)
        protected = any(self._protected(o) for o in operands)
        overwrites_a_document = any(self._looks_like_document_container(o) for o in operands)

        if program not in METADATA_ONLY and any(reads_credentials(o) for o in operands):
            found.append((RiskLevel.DANGEROUS, "touches a file that holds credentials"))

        if program in PROGRAM_RISK and not (
                program in LISTING_TOOLS and trust(rc.argv, self._protected) == READ_ONLY):
            level, why = PROGRAM_RISK[program]
            found.append((level, f"{why} via {program}"))

        rules: list[tuple[bool, Callable[[], list[tuple[RiskLevel, str]]]]] = [
            (program == "rm", lambda: self._judge_rm(chars, flags, protected)),
            (program == "dd", lambda: self._judge_dd(argv)),
            (program == "find", lambda: self._judge_find(argv, protected)),
            (program == "systemctl", lambda: self._judge_systemctl(argv)),
            (program == "git", lambda: self._judge_git(argv)),
            (program in ("apt", "apt-get", "dnf", "pacman", "snap"),
             lambda: self._judge_package_manager(argv)),
            (program in PACKAGE_INSTALLERS,
             lambda: self._judge_package_install(program, argv)),
            (program in ("python", "python3", "perl", "ruby", "node"),
             lambda: self._judge_script_source(program, argv)),
            (program == "kill", lambda: self._judge_kill(argv)),
            (program == "docker", lambda: self._judge_docker(argv)),
            (program in ("chmod", "chown"),
             lambda: self._judge_chmod_chown(program, protected)),
            (program in ("tee", "cp", "mv", "zip"),
             # `zip`'s output archive is a write target exactly like cp/mv's
             # destination -- xlsx/odt are both zip containers, and repacking
             # one over an existing path (extract, edit the inner XML,
             # `zip -r original.file ...`) is as much an overwrite as `cp`/
             # `mv` onto the same path, which this same protected-prefix
             # check already catches. `unzip` deliberately excluded: its
             # archive argument is a read source, not a write target, so
             # reusing this generic operand-protected check for it would
             # misclassify an ordinary extraction of an existing file as
             # "writes to a system path".
             lambda: self._judge_tee_cp_mv_zip(program, protected, overwrites_a_document)),
        ]
        for matched, call in rules:
            if matched:
                found += call()
        return found or self._judge_by_trust(rc)

    def _judge_by_trust(self, rc: ResolvedCommand) -> list[tuple[RiskLevel, str]]:
        """No rule objected, so only a program torra can vouch for runs without a tick."""
        verdict = trust(rc.argv, self._protected)
        if verdict == READ_ONLY:
            return []
        if verdict == BOUNDED_WRITE:
            return [(RiskLevel.CAUTION, f"{rc.program} writes files or state")]
        if verdict == PROTECTED_WRITE:
            return [(RiskLevel.DANGEROUS, f"{rc.program} writes to a system path")]
        return [(RiskLevel.DANGEROUS,
                 f"unrecognised program {rc.program!r}: not known to be read-only")]

    @staticmethod
    def _judge_rm(chars: set[str], flags: list[str],
                  protected: bool) -> list[tuple[RiskLevel, str]]:
        recursive = "r" in chars or "R" in chars or "--recursive" in flags
        forced = "f" in chars or "--force" in flags
        if recursive and forced:
            return [(RiskLevel.CRITICAL, "recursive force delete")]
        if recursive or protected:
            return [(RiskLevel.CRITICAL if protected else RiskLevel.DANGEROUS,
                     "recursive or system-path delete")]
        return [(RiskLevel.DANGEROUS, "file deletion")]

    @staticmethod
    def _judge_dd(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if any(a.startswith("of=/dev/") for a in argv):
            return [(RiskLevel.CRITICAL, "raw write to a block device")]
        return []

    @staticmethod
    def _judge_find(argv: list[str], protected: bool) -> list[tuple[RiskLevel, str]]:
        if "-delete" in argv or "-exec" in argv:
            return [(RiskLevel.CRITICAL if protected else RiskLevel.DANGEROUS,
                     "find with -delete or -exec")]
        return []

    @staticmethod
    def _judge_systemctl(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if any(a in ("stop", "disable", "mask") for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "service interruption")]
        if any(a in ("restart", "reload", "isolate") for a in argv[1:]) and any(
                a.lower().removesuffix(".service") in CRITICAL_UNITS for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "restarting a unit the current session depends on")]
        if any(a in ("start", "restart", "enable", "daemon-reload") for a in argv[1:]):
            return [(RiskLevel.CAUTION, "service state change")]
        return []

    @staticmethod
    def _judge_git(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if re.search(
                r"\breset\s+--hard\b|\bclean\s+-[a-z]*f|\bpush\s+--force\b"
                r"|\bcheckout\s+--\s|\bstash\s+(drop|clear)\b", " ".join(argv[1:])):
            return [(RiskLevel.DANGEROUS, "unrecoverable git operation")]
        return []

    @staticmethod
    def _judge_package_manager(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        if any(a in ("remove", "purge", "autoremove", "uninstall") for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "package removal")]
        if "install" in argv[1:] and any(a.endswith("-") and not a.startswith("-") for a in argv[1:]):
            return [(RiskLevel.DANGEROUS, "an install that also removes a package (name-)")]
        if any(a in ("install", "upgrade", "update", "full-upgrade") for a in argv[1:]):
            return [(RiskLevel.CAUTION, "package change")]
        return []

    @staticmethod
    def _pip_source_option(rest: list[str]) -> bool:
        """A short cluster whose first value-taking letter is `-i`/`-f`, or a prefix of a source long option."""
        clusters = (a[1:] for a in rest if a.startswith("-") and not a.startswith("--"))
        longs = (a.split("=", 1)[0] for a in rest if a.startswith("--") and len(a.split("=", 1)[0]) > 2)
        return any(next((ch for ch in c if ch in PIP_VALUE_SHORT), "") in ("i", "f") for c in clusters) or any(
            s.startswith(f) for f in longs for s in PIP_SOURCE_LONG)

    @staticmethod
    def _judge_package_install(program: str, argv: list[str]) -> list[tuple[RiskLevel, str]]:
        """A package install from a non-default source runs code torra cannot vouch for.

        `install` runs the fetched package's build/setup code as the invoking user, so pointing
        it at a custom index, a URL, a VCS ref, or a sideloaded archive is supply-chain / RCE
        territory and must ask for a tick -- not run at the CAUTION a plain default-index install
        gets. A plain `pip install <name>` (or a local `pip install .` / `-e .`) stays CAUTION:
        it is the ordinary workflow, and over-blocking it would train the operator to wave every
        install through. Returning `[]` here lets that normal path fall through to its usual
        CAUTION; only a non-default source is escalated.
        """
        rest = argv[1:]
        operands = [a for a in rest if not a.startswith("-")]
        if not INSTALL_VERBS & set(operands):
            return []
        flag_names = {a.split("=", 1)[0] for a in rest if a.startswith("-")}
        flag_values = [a.split("=", 1)[1] for a in rest if a.startswith("-") and "=" in a]
        untrusted = bool(flag_names & UNTRUSTED_SOURCE_FLAGS) or any(
            UNTRUSTED_SOURCE_OPERAND.match(a) for a in operands + flag_values) or (
            program in ("pip", "pip3") and CommandJudge._pip_source_option(rest))
        if untrusted:
            return [(RiskLevel.DANGEROUS,
                     f"{program} install from a non-default source (custom index, URL, or VCS) "
                     "-- runs fetched code, a supply-chain / RCE risk")]
        return []

    @staticmethod
    def _call_signature(source: str, start: int, end: int) -> str | None:
        """The call a quoted literal at source[start:end] is an argument to
        -- "load_workbook" for `openpyxl.load_workbook('x.xlsx')`, or
        "open:write"/"open:read" for Python's own `open()`, whose read and
        write paths share one name and are told apart only by the mode
        argument that follows. None when the literal isn't immediately
        `name(` -- e.g. a bare comparison operand -- rather than a call
        argument."""
        before = re.search(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(\s*$", source[:start])
        if not before:
            return None
        name = before.group(1).rsplit(".", 1)[-1]
        if name != "open":
            return name
        mode = re.match(r"""^\s*,\s*(?:mode\s*=\s*)?['"]([rwaxb+]{1,4})['"]""", source[end:])
        return "open:write" if mode and any(c in mode.group(1) for c in "wax+") else "open:read"

    @staticmethod
    def _repeated_path_literal(source: str) -> str | None:
        """A quoted, path-shaped literal appearing 2+ times in interpreter
        source is the shape of a read-then-write-back on the same file --
        `openpyxl.load_workbook('x.xlsx') ... wb.save('x.xlsx')`,
        `Document('x.docx') ... doc.save('x.docx')` -- regardless of which
        library is doing it. Matching the repeated literal itself, not a
        per-library method name, is deliberate: enumerating "save" spellings
        library by library is exactly the open-ended list HANDOFF.md §3
        already warns is a losing shape. Found via
        scripts/dev/model_bench/domain_exploration.py's xlsx-edit scenario
        (2026-08-28, devstral:24b): a `python3 -c` step loaded an existing
        spreadsheet and saved back over the same path with no backup or
        declared rollback, and classified SAFE -- neither the regex BACKSTOP
        nor this function (before this check existed) has any notion of an
        interpreter payload overwriting a path it also read.

        Counting bare occurrences over-fired on ordinary reads of the same
        path twice (comparing a file to itself, validating then processing
        it) -- found testing this for real against actual xlsx files, zero
        writes anywhere, still classified DANGEROUS. A read-then-write has a
        real, checkable signature instead: two *differently*-named calls
        touching the same literal (`load_workbook`/`save`,
        `read_excel`/`to_excel`) versus the same call repeated
        (`load_workbook`/`load_workbook` -- just read twice). `open()` is
        the one call whose read and write paths share a name, so it gets
        its own mode-aware signature via `_call_signature` rather than
        falling through this same-name-means-safe rule."""
        occurrences: dict[str, list[str | None]] = {}
        for m in re.finditer(r"'([^']*)'|\"([^\"]*)\"", source):
            literal = m.group(1) or m.group(2)
            if not literal or " " in literal:
                continue
            if "/" in literal or re.search(r"\.[A-Za-z0-9]{1,5}$", literal):
                occurrences.setdefault(literal, []).append(
                    CommandJudge._call_signature(source, m.start(), m.end()))
        for literal, sigs in occurrences.items():
            if len(sigs) < 2:
                continue
            if "open:write" in sigs or len(set(sigs) - {"open:read"}) >= 2:
                return literal
        return None

    @staticmethod
    def _judge_script_source(program: str, argv: list[str]) -> list[tuple[RiskLevel, str]]:
        # `\bunlink\b`, not `\.unlink\(` -- Perl's bareword `unlink(...)` /
        # `unlink glob(...)` has no leading dot, unlike Python's os.unlink,
        # and the old pattern required the dot so it never matched.
        found: list[tuple[RiskLevel, str]] = []
        source = " ".join(argv)
        if re.search(r"rmtree|os\.remove|\bunlink\b|subprocess|os\.system"
                     r"|unlinkSync|rmSync", source):
            found.append((RiskLevel.DANGEROUS,
                         f"{program} source performs filesystem or process operations"))
        reopened = CommandJudge._repeated_path_literal(source)
        if reopened:
            found.append((RiskLevel.DANGEROUS,
                         f"{program} source reopens {reopened!r} -- looks like an "
                         "in-place overwrite with no declared backup or rollback"))
        return found

    @staticmethod
    def _kill_targets(argv: list[str]) -> tuple[list[str], bool]:
        """The pid operands of a `kill` line, and whether it only lists signals."""
        skip_next, list_mode, signalled, pids = False, False, False, []
        for tok in argv[1:]:
            if skip_next:
                skip_next = False
            elif tok in ("-s", "-n", "--signal"):
                skip_next = signalled = True
            elif tok in ("-l", "-L", "--list", "--table"):
                list_mode = True
            elif signalled and re.fullmatch(r"-\d+", tok):
                pids.append(tok)  # `kill -9 -1`: the signal is chosen, so this is a pid
            elif tok.startswith("-"):
                signalled = True
            else:
                pids.append(tok)
        return pids, list_mode

    @staticmethod
    def _judge_kill(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        # `-9` alone used to be the only signal spec recognized here --
        # `kill -s KILL 1`, `kill -SIGKILL 1`, `kill -s 9 1`, and plain
        # `kill 1` (default SIGTERM) all classified SAFE. Found in a
        # full-file audit. Walking argv properly (rather than reusing the
        # generic `operands` split) matters here specifically: `-s`/`-n`/
        # `--signal` consume the *next* token as their argument, not a
        # PID -- `kill -s 1 12345` sends SIGHUP to PID 12345 and never
        # touches PID 1, and naively treating "1" as an operand there
        # would flag a command that is not dangerous.
        pids, list_mode = CommandJudge._kill_targets(argv)
        if list_mode:
            return []
        if "1" in pids:
            return [(RiskLevel.CRITICAL, "kill of PID 1")]
        if any(p == "0" or p.startswith("-") for p in pids):
            return [(RiskLevel.CRITICAL, "kill of a whole process group or every process")]
        if not pids or not all(p.isdigit() for p in pids):
            return [(RiskLevel.DANGEROUS, "kill of a process torra cannot name (computed pid)")]
        return []

    @staticmethod
    def _judge_docker(argv: list[str]) -> list[tuple[RiskLevel, str]]:
        words = set(argv[1:])
        if words & {"rm", "rmi", "kill", "prune"} or (
                "down" in words and words & {"-v", "--volumes"}):
            return [(RiskLevel.DANGEROUS, "removes containers, images or volumes")]
        if "--privileged" in words or words & {"--pid=host", "--cap-add=ALL"}:
            return [(RiskLevel.DANGEROUS, "runs a container with host-level privileges")]
        mounts = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-v", "--volume")]
        mounts += [a.split("=", 1)[1] for a in argv if a.startswith("--volume=")]
        if any(CommandJudge._protected(m.split(":", 1)[0]) for m in mounts):
            return [(RiskLevel.DANGEROUS, "mounts a system or home directory into a container")]
        return []

    @staticmethod
    def _judge_chmod_chown(program: str, protected: bool) -> list[tuple[RiskLevel, str]]:
        if protected:
            return [(RiskLevel.CRITICAL, f"{program} on a system path")]
        return []

    @staticmethod
    def _judge_tee_cp_mv_zip(program: str, protected: bool,
                              overwrites_a_document: bool) -> list[tuple[RiskLevel, str]]:
        if protected:
            return [(RiskLevel.DANGEROUS, f"{program} writes to a system path")]
        if overwrites_a_document:
            return [(RiskLevel.DANGEROUS,
                     f"{program} writes to a path that looks like an existing office "
                     "document (xlsx/docx/pptx/odt/ods/odp are zip containers -- a "
                     "relative path here still isn't a protected system prefix)")]
        return []

    @staticmethod
    def _check_backstop(text: str, unparsed: bool) -> list[tuple[RiskLevel, str]]:
        """Regex backstop: catches whatever resolve() failed to parse."""
        return [(level, f"{why} [regex backstop]") for pattern, level, why in BACKSTOP
                if (unparsed or why not in UNPARSED_ONLY) and re.search(pattern, text, re.IGNORECASE)]
