"""
core_behaviors_v4.py
Phase 4 behaviors.

  P0-1  StructuralClarifier      ASK_CLARIFYING_QUESTIONS rebuilt around missing
                                 structure rather than a list of vague phrasings
  P1-1  ReflinkUndoGenerator     copy-on-write shadow snapshots where the
                                 filesystem supports them
  P1-2  ShadowReaper             ages out orphaned shadow directories

The clarifier is the point of this phase. Measured against a 66 entry labeled
corpus, the Phase 1 regex table had a **36% false negative rate**: it waved
through "reset the postgres volume", "roll back the deployment", and
"free up some disk", among twelve others. That is the expensive direction,
because an underspecified task that clears this gate then gets correctly
approved by every gate below it. A well formed command aimed at the wrong
target is still a well formed command.

Phases 1 through 3 are unmodified.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core_behaviors import Ambiguity, AskClarifyingQuestionsBehavior
from core_behaviors_v3 import Snapshot, SnapshotKind, UndoGenerator, VerifiedAction

__all__ = [
    "StructuralClarifier",
    "ReflinkUndoGenerator",
    "ReapReport",
    "ShadowReaper",
]


# --------------------------------------------------------------------------- #
# P0-1: structural clarifier
# --------------------------------------------------------------------------- #


#: Something concrete enough to act on without asking. Any one of these anchors
#: a request: an absolute path, a quantity with a unit, a version, a commit sha,
#: a systemd unit, a hostname, a port, a quoted literal, a flag.
ANCHOR_PATTERNS: List[Tuple[str, str]] = [
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
     r"(systemd\s+)?(unit|service|daemon|container|process)\b",
     "named service or unit"),
]

#: Anchors strong enough to justify a destructive operation without asking about
#: recoverability. An environment word like "staging" is an anchor for routing
#: but not for deletion: "wipe the staging data" still needs to know which data.
STRONG_ANCHOR_LABELS: Tuple[str, ...] = (
    "absolute path", "named file", "systemd unit", "commit sha",
    "hostname or tagged identifier", "quoted literal", "named service or unit",
    "date",
)

#: A predicate that bounds an otherwise unbounded scope.
FILTER_PATTERNS: List[str] = [
    r"\b(older|newer|larger|smaller|more|less|greater|fewer)\s+than\b",
    r"\bover\s+\d+", r"\bunder\s+\d+", r"\bat\s+least\b", r"\bat\s+most\b",
    r"\bnamed\b", r"\bmatching\b", r"\bwhere\b", r"\bwith\s+the\s+\w+\b",
    r"\bfrom\s+the\s+last\b", r"\bexcept\b", r"\band\s+nothing\s+else\b",
    r"\busing\s+more\s+than\b", r"\bin\s+/",
]

#: Verbs that change system state.
MUTATING_VERBS = (
    r"\b(delete|remove|purge|wipe|drop|clear|reset|restart|stop|start|deploy|"
    r"release|ship|roll\s*out|roll\s*back|rollback|migrate|archive|kill|bump|"
    r"install|uninstall|upgrade|downgrade|harden|tune|optimi[sz]e|improve|"
    r"speed\s*up|free\s*up|point|update|fix|clean\s*up|sort\s*out|handle|"
    r"deal\s+with|take\s+care\s+of|set\s*up|scale|rotate|prune|truncate)\b"
)

#: Verbs whose success is a measurement, so they need a number.
IMPROVEMENT_VERBS = (
    r"\b(faster|optimi[sz]e|improve|speed\s*up|tune|less\s+\w+|better|harden|"
    r"ready|reduce|increase|scale)\b"
)

#: Destructive verbs. These need a target AND a recoverability answer.
DESTRUCTIVE_VERBS = (
    r"\b(delete|remove|purge|wipe|drop|reset|erase|truncate|prune|clear)\b"
)

#: Nouns that sound like a target but name a category, not an instance.
GENERIC_REFERENTS = (
    r"\b(the\s+)?(server|box|machine|host|database|db|api|service|app|"
    r"deployment|cache|thing|issue|problem|setup|endpoint|volume|build|"
    r"cluster|node|instance|environment|pipeline|system|stack|site|data)\b"
)

#: Quantifiers that name a set without defining it.
VAGUE_QUANTIFIERS = (
    r"\b(old|stale|stuck|unused|orphaned|some|several|a\s+few|various|"
    r"latest|newest|recent|leftover|junk|extra)\b"
)

#: Unbounded scope words.
UNBOUNDED = r"\b(all|every|everything|everywhere|entire|whole)\b"

#: Pronouns with no antecedent inside the request.
DANGLING = r"\b(it|this|that|these|those|them|they)\b"

#: Deploy family verbs, which need a named environment.
DEPLOY_VERBS = r"\b(deploy|release|ship|roll\s*out|rollout|push\s+.*\s+out)\b"

#: Open ended timing.
OPEN_TIMING = r"\b(later|soon|sometime|some\s+time|asap|when\s+you\s+can|eventually)\b"


class StructuralClarifier(AskClarifyingQuestionsBehavior):
    """Detects ambiguity by what a request is missing, not by how it is phrased.

    The Phase 1 clarifier matched a list of vague phrasings. That approach can
    only catch phrasings someone thought to write down, and the measured cost
    was a 36% false negative rate on a labeled corpus. "make the api faster" and
    "reset the postgres volume" both sailed through, because neither matches
    "fix it" or "make it better".

    This inverts the test. Instead of asking "does this look vague", it asks
    "does this contain the structure an executable request needs":

      1. a concrete **anchor**: a path, a quantity with a unit, a version, a
         commit, a unit name, a host, a port, a named file, or an environment
      2. a **bounded scope** when the request says all/every/everything
      3. a **measurable outcome** when the verb is an improvement verb
      4. a **named environment** when the verb is a deploy verb
      5. a **recoverability answer** when the verb is destructive

    A request that has the structure passes even if it is phrased casually. A
    request that lacks it is stopped even if it sounds precise. That is what
    moves the false negative rate, and it also fixes the Phase 1 false positives
    where "remove /tmp/build-cache-2024 and nothing else" got blocked for
    containing the word "remove": it has a path anchor and an explicit filter,
    so there is nothing left to ask.
    """

    #: Requests shorter than this with no anchor are underspecified outright.
    MIN_TOKENS: int = 4

    def __init__(
        self,
        ask: Optional[Callable[[Ambiguity], Optional[str]]] = None,
        extra_anchors: Optional[Sequence[Tuple[str, str]]] = None,
        strict_destructive: bool = False,
    ) -> None:
        """
        Args:
            ask: Callable that puts one question to the operator.
            extra_anchors: Additional (regex, label) pairs treated as concrete.
                Use this for site specific things like an internal hostname
                scheme the default patterns do not know about.
            strict_destructive: Ask about recoverability for every destructive
                request, even a well anchored one. Off by default, because that
                rule alone produced two of the three Phase 1 false positives.
        """
        super().__init__(ask=ask)
        self.anchors: List[Tuple[str, str]] = list(ANCHOR_PATTERNS) + list(
            extra_anchors or [])
        self.strict_destructive = bool(strict_destructive)

    # -- structural probes -------------------------------------------------- #

    def anchors_in(self, task: str) -> List[str]:
        """Return the labels of every concrete anchor found in a request."""
        found: List[str] = []
        for pattern, label in self.anchors:
            if re.search(pattern, task, re.IGNORECASE):
                found.append(label)
        return found

    @staticmethod
    def has_filter(task: str) -> bool:
        """True when the request bounds its own scope with a predicate."""
        return any(re.search(p, task, re.IGNORECASE) for p in FILTER_PATTERNS)

    @staticmethod
    def has_measure(task: str) -> bool:
        """True when the request states a number the outcome can be checked against."""
        return bool(re.search(r"\b\d+(\.\d+)?\s*\w*", task))

    @staticmethod
    def _has_antecedent(task: str) -> bool:
        """True when a pronoun has a concrete noun before it in the request."""
        match = re.search(DANGLING, task, re.IGNORECASE)
        if not match:
            return True
        before = task[:match.start()]
        return bool(re.search(r"[\w./-]{3,}", before)) and len(before.split()) >= 3

    # -- detection ---------------------------------------------------------- #

    def detect(self, task: str) -> List[Ambiguity]:
        """Return every structural gap found in a request.

        Returns:
            A list of Ambiguity records, each naming what is missing and why it
            matters. Empty means the request is executable as written.
        """
        text = (task or "").strip()
        if not text:
            return [Ambiguity("empty", "What would you like me to do?",
                              "the request is empty")]

        found: List[Ambiguity] = []
        seen: set[str] = set()

        def add(code: str, question: str, why: str) -> None:
            """Record one ambiguity, ignoring duplicates of the same code."""
            if code not in seen:
                found.append(Ambiguity(code=code, question=question, why=why))
                seen.add(code)

        anchors = self.anchors_in(text)

        # Vague-word checks run against text with the anchors blanked out.
        # Without this, "yeet the /srv/api/old.log file" trips the vague
        # qualifier rule on the "old" inside a filename, which is a word the
        # operator never used as a judgement.
        scrubbed = text
        for pattern, _label in self.anchors:
            scrubbed = re.sub(pattern, " \u2588 ", scrubbed, flags=re.IGNORECASE)

        tokens = re.findall(r"[A-Za-z0-9_./:-]+", text)
        mutating = re.search(MUTATING_VERBS, text, re.IGNORECASE)
        destructive = re.search(DESTRUCTIVE_VERBS, text, re.IGNORECASE)

        # 1. Nothing concrete to act on, whatever the verb.
        #
        #    This rule replaced a closed list of mutating verbs, and that swap is
        #    the single most important line in this file. Holdout scoring showed
        #    the verb list missing "tidy", "trim", "drain", "quiesce", "throttle",
        #    "consolidate", "retire", "sync", and "stand up", which is the exact
        #    failure mode of the Phase 1 phrasing list one level up: a closed
        #    vocabulary can only catch what someone thought to write down.
        #
        #    Asking "is there an anchor" instead of "is the verb in my list" is
        #    verb-agnostic, so an operator inventing new jargon cannot slip past.
        if not anchors:
            verb = mutating.group(0) if mutating else "this request"
            add("no_target",
                "Which exact path, unit, host, container, or version should I "
                "act on?",
                f"'{verb}' has nothing concrete to act on: the request names no "
                "path, unit, host, version, file, or quantity")

        # 2. A generic category noun standing in for an instance.
        generic = re.search(GENERIC_REFERENTS, scrubbed, re.IGNORECASE)
        if generic and not anchors:
            add("ambiguous_referent",
                f"Which specific {generic.group(0).replace('the ', '').strip()} "
                "do you mean? Name the host, unit, or path.",
                f"'{generic.group(0).strip()}' names a category, not an instance")

        # 3. An improvement verb with no number to check against.
        improvement = re.search(IMPROVEMENT_VERBS, text, re.IGNORECASE)
        if improvement and not self.has_measure(text):
            add("no_acceptance_criterion",
                "What measurable outcome counts as done? Give a number or a "
                "threshold.",
                f"'{improvement.group(0)}' is a direction, not a target, so "
                "there is no way to tell when the task succeeded")

        # 4. Unbounded scope with no filter to bound it.
        unbounded = re.search(UNBOUNDED, scrubbed, re.IGNORECASE)
        if unbounded and not self.has_filter(text):
            add("unbounded_scope",
                "What exact path or filter defines the set you mean?",
                f"'{unbounded.group(0)}' can match far more than intended and "
                "the request states no predicate")

        # 5. A vague quantifier standing in for a selection rule.
        vague = re.search(VAGUE_QUANTIFIERS, scrubbed, re.IGNORECASE)
        if vague and not self.has_filter(text):
            add("vague_qualifier",
                f"What defines '{vague.group(0)}' here? Give an age, a size, "
                "or a name pattern.",
                f"'{vague.group(0)}' is a judgement, not a selection rule")

        # 6. A deploy with no environment named.
        deploying = re.search(DEPLOY_VERBS, text, re.IGNORECASE)
        if deploying and not re.search(
                r"\b(to|into|on)\s+\S+|\b(staging|production|prod|local|dev)\b",
                text, re.IGNORECASE):
            add("no_environment",
                "Which environment is the target: local, staging, or production?",
                "a deploy without a named environment is a coin flip")

        # 7. A destructive verb with no recoverability answer. Keyed on a
        #    STRONG anchor: an environment word is enough to route a task but
        #    not enough to delete with, which is what let "wipe the staging
        #    data" through on the first pass.
        #    An explicit filter counts too: "remove docker images older than
        #    30 days" already states what it will and will not touch, so asking
        #    about backups is an interruption rather than a safeguard.
        strong = [a for a in anchors if a in STRONG_ANCHOR_LABELS]
        if destructive and (self.strict_destructive
                            or not (strong or self.has_filter(text))):
            add("no_backup_plan",
                "Should I back up or stage the targets before removing them?",
                "the request destroys state and states no backup plan")

        # 8. A pronoun with nothing to bind to.
        if re.search(DANGLING, text, re.IGNORECASE) and not self._has_antecedent(text):
            add("dangling_pronoun",
                "What does that refer to? Name the file, service, or host.",
                "a pronoun with no antecedent inside the request")

        # 9. Open ended timing.
        if re.search(OPEN_TIMING, text, re.IGNORECASE):
            add("no_schedule",
                "Do you want this run now, or scheduled for a specific time?",
                "timing was left open")

        # 10. Too short to contain a request.
        if len(tokens) < self.MIN_TOKENS and not anchors:
            add("underspecified",
                "Can you describe the task with a concrete target and outcome?",
                f"the request is {len(tokens)} tokens and names nothing concrete")

        return found

    def explain(self, task: str) -> str:
        """Human readable trace of what the clarifier saw in a request."""
        anchors = self.anchors_in(task)
        found = self.detect(task)
        lines = [f"{task!r}",
                 f"  anchors: {anchors or 'NONE'}",
                 f"  filter:  {self.has_filter(task)}",
                 f"  verdict: {'BLOCKED' if found else 'executable'}"]
        for amb in found:
            lines.append(f"    {amb.code}: {amb.why}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# P1-1: copy on write shadow snapshots
# --------------------------------------------------------------------------- #


class ReflinkUndoGenerator(UndoGenerator):
    """UndoGenerator that uses copy-on-write clones where it can.

    Phase 3 measured 9.5 MB of shadow copies for 50 files at 200 KB, byte for
    byte, with no dedup. Pop-OS installs on ext4 by default but a lot of people
    run btrfs on /home, and both btrfs and xfs support reflinks, where a clone
    shares extents until one side is written. On those filesystems a snapshot
    costs metadata rather than data.

    `cp --reflink=auto` does exactly the right thing: clone when the filesystem
    supports it, fall back to a full copy when it does not. So the strategy is
    to try it once per shadow directory, remember whether it worked, and stop
    paying the subprocess cost after that.

    Falls back to the Phase 3 behavior on any failure, and records which mode it
    ended up in so the audit does not have to guess.
    """

    def __init__(
        self,
        shadow_dir: Optional[os.PathLike[str] | str] = None,
        service_probe: Optional[Callable[[str], str]] = None,
        package_probe: Optional[Callable[[], set[str]]] = None,
        max_shadow_bytes: Optional[int] = None,
        prefer_reflink: bool = True,
    ) -> None:
        """
        Args:
            prefer_reflink: Try `cp --reflink=auto` before a byte copy.
            (Other arguments as UndoGenerator.)
        """
        super().__init__(shadow_dir, service_probe, package_probe, max_shadow_bytes)
        self.prefer_reflink = bool(prefer_reflink)
        self.reflink_supported: Optional[bool] = None
        self.reflink_copies: int = 0
        self.byte_copies: int = 0

    def _probe_reflink(self) -> bool:
        """Test once whether this filesystem clones instead of copying."""
        if self.reflink_supported is not None:
            return self.reflink_supported
        if not self.prefer_reflink or not shutil.which("cp"):
            self.reflink_supported = False
            return False

        probe = self.shadow_dir / ".reflink-probe"
        clone = self.shadow_dir / ".reflink-probe-clone"
        try:
            probe.write_bytes(b"reflink probe")
            result = subprocess.run(
                ["cp", "--reflink=always", str(probe), str(clone)],
                capture_output=True, text=True, timeout=10,
            )
            self.reflink_supported = result.returncode == 0
            if not self.reflink_supported:
                self.skipped.append(
                    "reflink unsupported on this filesystem, using byte copies: "
                    + (result.stderr.strip() or "cp --reflink=always failed")
                )
        except Exception as exc:
            self.reflink_supported = False
            self.skipped.append(f"reflink probe failed: {exc!r}")
        finally:
            for path in (probe, clone):
                try:
                    path.unlink()
                except OSError:
                    pass
        return bool(self.reflink_supported)

    def _copy(self, source: Path, destination: Path) -> str:
        """Clone or copy one file. Returns the mode actually used."""
        if self._probe_reflink():
            try:
                result = subprocess.run(
                    ["cp", "--reflink=always", "--preserve=all",
                     str(source), str(destination)],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    self.reflink_copies += 1
                    return "reflink"
            except Exception as exc:
                self.skipped.append(f"reflink copy of {source} failed: {exc!r}")
        shutil.copy2(source, destination)
        self.byte_copies += 1
        return "copy"

    def capture_file(self, path: os.PathLike[str] | str) -> Optional[VerifiedAction]:
        """Snapshot one file, cloning instead of copying where supported.

        Reimplements the Phase 3 body rather than intercepting `shutil.copy2`.
        The first draft monkeypatched that function to route copies through the
        reflink path, which recursed infinitely the moment the reflink path fell
        back to `shutil.copy2` and found itself. Patching a stdlib function is
        also process-global and not thread safe, so a harness that runs a
        background health monitor has no business doing it.
        """
        target = Path(path)

        if not target.exists():
            return super().capture_file(path)

        try:
            stat = target.stat()
        except OSError as exc:
            self.skipped.append(f"{target}: stat failed {exc!r}")
            return None

        size, mode = stat.st_size, stat.st_mode
        if size > self.max_shadow_bytes:
            note = (f"{target} is {size} bytes, over max_shadow_bytes="
                    f"{self.max_shadow_bytes}; no undo generated, push one manually")
            self.skipped.append(note)
            return None

        self._counter += 1
        shadow = self.shadow_dir / f"{self._counter:05d}_{target.name}"
        try:
            mode_used = self._copy(target, shadow)
        except OSError as exc:
            self.skipped.append(f"{target}: shadow copy failed {exc!r}")
            return None

        digest = self._digest(target)
        snap = Snapshot(SnapshotKind.FILE_CONTENT, str(target),
                        f"{size} bytes shadowed via {mode_used}", str(shadow),
                        digest, mode=mode)
        self.snapshots.append(snap)

        def undo_restore() -> None:
            """Copy the shadowed bytes back and restore the original mode."""
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shadow, target)
            if mode is not None:
                os.chmod(target, mode & 0o7777)

        return VerifiedAction(
            description=f"restore {target} from snapshot ({mode_used})",
            undo=undo_restore,
            safe=True,
            verify=lambda: self._digest(target) == digest,
            generated=True,
            snapshot=snap,
        )

    def stats(self) -> Dict[str, Any]:
        """Copy mode counters, for the audit trail."""
        return {
            "reflink_supported": self.reflink_supported,
            "reflink_copies": self.reflink_copies,
            "byte_copies": self.byte_copies,
            "snapshots": len(self.snapshots),
            "skipped": list(self.skipped),
        }


# --------------------------------------------------------------------------- #
# P1-2: shadow reaper
# --------------------------------------------------------------------------- #


@dataclass
class ReapReport:
    """What a reap pass removed and what it left alone."""

    scanned: int = 0
    removed: int = 0
    bytes_freed: int = 0
    kept_live: int = 0
    errors: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Default the error list without sharing it between instances."""
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


class ShadowReaper:
    """Ages out shadow directories a crashed run left behind.

    Phase 3 cleaned shadow copies only on an explicit `cleanup()` call, which a
    crashed run never reaches. On a busy box that is a slow disk leak with
    nothing watching it.

    Two safeguards keep this from deleting live state. It only touches
    directories matching `prefix`, so it cannot wander into real data. And it
    skips any directory whose mtime is inside `min_age_seconds`, so a run that
    is currently using its shadow directory is never reaped out from under
    itself.
    """

    DEFAULT_PREFIX: str = "harness-shadow-"
    DEFAULT_MAX_AGE: float = 24 * 3600.0
    DEFAULT_MIN_AGE: float = 300.0

    def __init__(
        self,
        roots: Sequence[os.PathLike[str] | str],
        prefix: str = DEFAULT_PREFIX,
        max_age_seconds: float = DEFAULT_MAX_AGE,
        min_age_seconds: float = DEFAULT_MIN_AGE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            roots: Directories to scan for orphaned shadow directories.
            prefix: Only directories whose name starts with this are candidates.
            max_age_seconds: Remove candidates older than this.
            min_age_seconds: Never remove anything younger than this, so a live
                run is not reaped mid-flight.
            clock: Injectable time source.
        """
        self.roots = [Path(r) for r in roots]
        self.prefix = prefix
        self.max_age_seconds = float(max_age_seconds)
        self.min_age_seconds = float(min_age_seconds)
        self._clock = clock

    @staticmethod
    def _size(path: Path) -> int:
        """Total bytes under a directory, best effort."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def candidates(self) -> List[Path]:
        """Directories eligible for removal right now."""
        now = self._clock()
        out: List[Path] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir() or not entry.name.startswith(self.prefix):
                    continue
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if age < self.min_age_seconds:
                    continue
                if age >= self.max_age_seconds:
                    out.append(entry)
        return out

    def reap(self, dry_run: bool = False) -> ReapReport:
        """Remove aged out shadow directories.

        Args:
            dry_run: Report what would be removed without removing it.

        Returns:
            A ReapReport. Failures are recorded rather than raised, because a
            reaper that crashes the run it is cleaning up after is worse than
            one that leaves a directory behind.
        """
        report = ReapReport()
        now = self._clock()

        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError as exc:
                report.errors.append(f"{root}: {exc!r}")
                continue

            for entry in entries:
                if not entry.is_dir() or not entry.name.startswith(self.prefix):
                    continue
                report.scanned += 1
                try:
                    age = now - entry.stat().st_mtime
                except OSError as exc:
                    report.errors.append(f"{entry}: {exc!r}")
                    continue

                if age < self.min_age_seconds:
                    report.kept_live += 1
                    continue
                if age < self.max_age_seconds:
                    continue

                size = self._size(entry)
                if dry_run:
                    report.removed += 1
                    report.bytes_freed += size
                    continue
                try:
                    shutil.rmtree(entry)
                    report.removed += 1
                    report.bytes_freed += size
                except OSError as exc:
                    report.errors.append(f"{entry}: rmtree failed {exc!r}")

        return report
