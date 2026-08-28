"""What the box looks like right now, and what the model will actually run on.

Depends on: harness.core.tasks (shell), harness.core.ollama (OllamaClient, for
the model listing observe_machine attaches), stdlib subprocess (lazily
imported, build_ssh_runner only) and os.environ (resolve_executor_config).
"""

from __future__ import annotations

import os
import re
import socket
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .ollama import OllamaClient
from .tasks import shell

__all__ = ["Accelerator", "detect_accelerator", "MachineState", "observe_machine",
          "build_ssh_runner", "observe_executor", "resolve_executor_config"]

#: Parameter counts for models whose name does not carry one.
KNOWN_MODEL_PARAMS_B: dict[str, float] = {
    "devstral": 24.0, "codestral": 22.0, "mistral": 7.0, "mixtral": 47.0,
    "llama3": 8.0, "phi3": 3.8, "gemma2": 9.0, "deepseek-coder": 6.7,
    "devstral-small-2": 24.0, "muse-glimmer": 30.0, "gpt-oss": 20.0,
}


@dataclass
class Accelerator:
    """What the model will actually run on.

    Ollama silently falls back to CPU when the driver is missing, the model does
    not fit in VRAM, or it was built without GPU support. It reports this only in
    `ollama ps`, which nobody reads before typing a request. Detecting it here
    means the router can stop sending a 32b model to a CPU that will take three
    minutes to answer.
    """

    kind: str = "cpu"           # cuda | rocm | cpu
    name: str = ""
    vram_mb: int = 0
    free_vram_mb: int = 0
    running_on: str = ""        # what `ollama ps` reports for a loaded model
    detail: str = ""
    #: Real parameter counts from `/api/show` (Agent.observe fills this in,
    #: keyed like `available` -- `tag:latest` normalized), consulted before
    #: the name guess below. Not part of the audit-facing dict; see to_dict.
    model_params_b: dict[str, float] = field(default_factory=dict)
    #: What `ollama ps` reports currently GPU-resident, and its size --
    #: `fits()` uses these only when `max_loaded_models == 1` confirms
    #: loading anything else evicts it first (HANDOFF.md §2.35).
    loaded_model: str = ""
    loaded_model_mb: int = 0
    #: The daemon's own `OLLAMA_MAX_LOADED_MODELS`, read from its real
    #: environment (`OLLAMA_MAX_LOADED_MODELS_PROBE`) -- 0 means unset/
    #: unknown, never guessed, so `fits()` only assumes an eviction when
    #: this is confirmed exactly 1.
    max_loaded_models: int = 0

    @property
    def gpu(self) -> bool:
        """True when inference will actually be accelerated."""
        return self.kind != "cpu" and self.free_vram_mb > 0

    def fits(self, model: str) -> bool:
        """Whether a model will fit in free VRAM.

        Free VRAM alone underestimates what a candidate model could use
        when something else is already loaded and the daemon is confirmed
        single-model (`max_loaded_models == 1`): Ollama evicts the
        resident model to make room rather than refusing, so that VRAM is
        available to the candidate too, not permanently spoken for. Found
        live: a small model that happened to load first (because it fit)
        stayed resident and starved every later request of the headroom a
        larger, more capable model needed -- self-reinforcing, and the
        exact mechanism behind HANDOFF.md §2.34's 84% false-negative rate
        on ambiguous requests (the model measured better at asking never
        got picked because the router thought it didn't fit).
        """
        if not self.gpu:
            return False
        params = self.model_params_b.get(model if ":" in model else f"{model}:latest", 0.0)
        if not params:
            # Parameter count from the name, two bytes per parameter for a
            # typical q4 quant plus context overhead. Deliberately crude:
            # the cost of being wrong is a slow answer, and the cost of not
            # guessing at all is routing a 32b model onto a CPU every
            # single time. Only reached when the real size above is
            # missing -- found live, this guess alone read "deepseek-r1-
            # heavy" and "qwen3.5" (no digit in either name) as 0B, which
            # this same "unknown -> assume it fits" fallback then let
            # through onto a card with less than half the VRAM they
            # actually needed.
            match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model, re.IGNORECASE)
            params = float(match.group(1)) if match else KNOWN_MODEL_PARAMS_B.get(
                model.split(":")[0].lower(), 0.0)
        if not params:
            return True
        effective_free = self.free_vram_mb
        loaded_tag = self.loaded_model if ":" in self.loaded_model else f"{self.loaded_model}:latest"
        candidate_tag = model if ":" in model else f"{model}:latest"
        if self.max_loaded_models == 1 and self.loaded_model and loaded_tag != candidate_tag:
            effective_free += self.loaded_model_mb
        return effective_free >= params * 1024 * 0.7

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, for the audit trail and the model prompt.

        `model_params_b` is excluded on purpose -- it can hold one entry per
        pulled model, and neither the audit reader nor the model prompt
        needs a table of every other model's size to explain this decision.
        """
        data = asdict(self)
        data.pop("model_params_b", None)
        return {**data, "gpu": self.gpu}

    def render(self) -> str:
        """One line for the model's context and the status bar."""
        if not self.gpu:
            return f"accelerator: CPU only ({self.detail or 'no usable GPU'})"
        return (f"accelerator: {self.kind} {self.name}, "
                f"{self.free_vram_mb} MB free of {self.vram_mb} MB"
                + (f", currently running on {self.running_on}"
                   if self.running_on else ""))


def _parse_size_mb(text: str) -> int:
    """Parse `ollama ps`'s human-readable SIZE column ("9.5 GB", "512 MB")
    into MB. Returns 0 on anything unparseable rather than raising -- a
    size this function can't read just means `fits()`'s eviction credit
    stays 0, the same conservative behavior as before this existed."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)", text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    return int(value * 1024) if match.group(2).upper() == "GB" else int(value)


def _max_loaded_models(execute: Callable[[str, str | None, float | None], str]) -> int:
    """The daemon's real `OLLAMA_MAX_LOADED_MODELS`, read from its own
    process environment the same way `OLLAMA_MODELS_DIR_PROBE` reads
    `OLLAMA_MODELS` -- 0 (never guessed) when unset or unreadable, since
    Ollama's own unset-default is a dynamic, undocumented computation this
    project has no live daemon to verify a guess against."""
    try:
        raw = execute(OLLAMA_MAX_LOADED_MODELS_PROBE, None, 5.0).strip()
        return int(raw) if raw.isdigit() else 0
    except Exception:
        return 0


def detect_accelerator(
    runner: Callable[[str, str | None, float | None], str] | None = None,
) -> Accelerator:
    """Work out whether Ollama is actually using a GPU.

    Checks three things in order of authority: what `ollama ps` says a loaded
    model is running on, what nvidia-smi reports, then rocm. `ollama ps` wins
    because it is the only one that reflects what the daemon actually decided
    rather than what the hardware could in principle do.
    """
    execute = runner or shell
    loaded_model = ""
    loaded_model_mb = 0

    try:
        listing = execute("ollama ps", None, 10.0)
        for line in listing.splitlines()[1:]:
            # The last-two-tokens trick this used to use only worked for the
            # ollama version it was written against. Found live: current
            # `ollama ps` also has CONTEXT and UNTIL columns after PROCESSOR
            # ("100% CPU     4096       5 minutes from now"), so
            # line.split()[-2:] grabbed trailing UNTIL words ("from now")
            # instead of the processor split -- "CPU" never matched, so a
            # model ollama itself was running on pure CPU silently fell
            # through to nvidia-smi's hardware-capability check and got
            # reported as GPU-accelerated, exactly backwards from what this
            # function's own docstring says ollama ps is here to guarantee.
            match = re.search(r"\d+%(?:/\d+%)?\s+(CPU/GPU|CPU|GPU)", line)
            if match:
                processor = match.group(0)
                if "CPU" in processor.upper() and "GPU" not in processor.upper():
                    return Accelerator(
                        kind="cpu", running_on=processor,
                        detail="ollama ps reports a loaded model on CPU")
                # Confirmed GPU (or partial CPU/GPU offload), but ollama ps
                # carries no VRAM figures -- fall through to nvidia-smi/rocm
                # below for those. Name/size are this line's first two
                # columns -- for fits()'s eviction credit (HANDOFF.md §2.35).
                fields = line.split()
                if fields:
                    loaded_model = fields[0]
                    loaded_model_mb = _parse_size_mb(line)
                break
    except Exception:
        pass

    try:
        out = execute(
            "nvidia-smi --query-gpu=name,memory.total,memory.free "
            "--format=csv,noheader,nounits", None, 10.0)
        name, total, free = [p.strip() for p in out.splitlines()[0].split(",")]
        return Accelerator("cuda", name, int(float(total)), int(float(free)),
                           loaded_model=loaded_model, loaded_model_mb=loaded_model_mb,
                           max_loaded_models=_max_loaded_models(execute))
    except Exception as exc:
        nvidia_error = f"{type(exc).__name__}"

    try:
        out = execute("rocm-smi --showmeminfo vram --csv", None, 10.0)
        if out.strip():
            return Accelerator("rocm", "amd", detail="rocm-smi responded")
    except Exception:
        pass

    return Accelerator(kind="cpu",
                       detail=f"no nvidia-smi or rocm-smi ({nvidia_error})")


@dataclass
class MachineState:
    """What the box actually looks like right now.

    The agent gets this before it proposes anything. A model asked to "clean up
    docker" with no idea what is installed will invent plausible commands; the
    same model given real `docker system df` output will work from what is
    there. Observation is the cheapest accuracy improvement available.
    """

    hostname: str = ""
    os_release: str = ""
    kernel: str = ""
    uptime: str = ""
    disk: str = ""
    memory: str = ""
    cwd: str = ""
    accelerator: Accelerator = field(default_factory=Accelerator)
    #: A second, real machine ("executor"), observed read-only over SSH when
    #: configured -- see observe_executor()/resolve_executor_config(). None
    #: (the default) means either no such machine is configured or its own
    #: observation failed; either way, no executor block is rendered and
    #: nothing about local behavior changes (HANDOFF.md §2.52). Never
    #: populate this field's own .executor -- render() has no cycle guard.
    executor: MachineState | None = None
    ollama_models: list[str] = field(default_factory=list)
    ollama_models_dir: str = ""
    #: A configured, optional local directory of site-specific documentation
    #: (hardware/services/quirks/etc. for *this* machine) -- keyed by
    #: filename, valued by that file's first heading, not its full content
    #: (some of these run past 100KB; the model gets an index, then reads
    #: the relevant file itself via an ordinary read-only step, the same
    #: way it reads anything else). Empty when unconfigured or absent.
    knowledge_library_path: str = ""
    knowledge_library: dict[str, str] = field(default_factory=dict)
    extras: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    def render(self) -> str:
        """Compact text block for the model's context."""
        lines = [
            f"hostname: {self.hostname}",
            f"os: {self.os_release}",
            f"kernel: {self.kernel}",
            f"uptime: {self.uptime}",
            f"cwd: {self.cwd}",
            self.accelerator.render(),
            "",
            "disk:", self.disk,
            "",
            "memory:", self.memory,
        ]
        if self.ollama_models:
            lines += ["", f"local models: {', '.join(self.ollama_models)}"]
        if self.ollama_models_dir:
            lines += [f"local models directory: {self.ollama_models_dir}"]
        if self.knowledge_library:
            # A topic key with a "/" (only "machines/<hostname>/<file>.md"
            # today) names its own subdirectory -- fold those into the grep
            # target list too, or the suggested command would silently miss
            # exactly the per-machine files that prompted indexing them at
            # all (see _scan_knowledge_dir's docstring).
            dirs = {self.knowledge_library_path}
            dirs.update(f"{self.knowledge_library_path}/{name.rsplit('/', 1)[0]}"
                       for name in self.knowledge_library if "/" in name)
            targets = " ".join(f"{d}/*.md" for d in sorted(dirs))
            lines += [
                "", f"site knowledge library at {self.knowledge_library_path} "
                "documents this exact machine: hardware, services, storage layout, "
                "past fixes, known quirks. The relevant fact is often buried in "
                "one file's prose, not named in any file's own topic below -- so "
                "when the request needs a specific fact about this machine "
                "(a path, a repository location, a config value, whether "
                "something is set up at all), do not guess and do not ask the "
                "operator first. Propose a read-only step that greps every file "
                "for the relevant keyword before anything else, for example:\n"
                f"    grep -ril 'KEYWORD' {targets}\n"
                "then read whichever file(s) that finds. Topics, for a narrower "
                "first guess when one obviously fits:",
            ]
            lines += [f"  {name}: {heading}"
                     for name, heading in self.knowledge_library.items()]
        for name, value in self.extras.items():
            lines += ["", f"{name}:", value]
        if self.executor is not None:
            lines += [
                "",
                "--- executor (a second, real machine, reached over SSH for "
                "read-only observation only -- these facts are informational; "
                "you cannot execute anything there, every step you propose "
                "still runs on THIS machine) ---",
                self.executor.render(),
            ]
        return "\n".join(lines)


#: Read-only probes. Everything here is safe to run without asking, which is
#: what makes observation free: nothing in this list can change the system.
OBSERVE_COMMANDS: list[tuple[str, str]] = [
    ("hostname", "hostname"),
    ("os_release", "grep PRETTY_NAME /etc/os-release | cut -d'\"' -f2"),
    ("kernel", "uname -r"),
    ("uptime", "uptime -p"),
    # A hardcoded path list (was "df -h / /home") goes stale the moment a
    # mount is added or removed -- found live: asked about /mnt/ParamoStorage,
    # the model correctly refused to guess rather than answer for an unlisted
    # mount, but the real gap was that mount never being observed at all.
    # Filtering by fs type instead of naming paths keeps every real mount
    # (including ones added later) visible without hand-maintaining a list.
    ("disk", "df -h -x tmpfs -x devtmpfs -x overlay -x squashfs -x proc "
             "-x sysfs -x cgroup -x cgroup2 -x tracefs -x fuse.gvfsd-fuse "
             "2>/dev/null"),
    ("memory", "free -h"),
]

#: Reads the *running* ollama daemon's actual OLLAMA_MODELS, not the calling
#: process's environment -- a model asked "where do the local models live"
#: with no grounding otherwise guesses (measured: it guessed wrong in
#: scripts/dev/model_bench/REPORT.md's disk-cleanup scenario), and the two
#: environments commonly differ, e.g. when ollama runs as a systemd service
#: with its own Environment= and the harness runs from an unrelated shell.
#: Falls back to the caller's own OLLAMA_MODELS or Ollama's documented
#: default when no such process is found or its environ isn't readable.
OLLAMA_MODELS_DIR_PROBE = (
    "pid=$(pgrep -x ollama | head -1); "
    "if [ -n \"$pid\" ] && [ -r \"/proc/$pid/environ\" ]; then "
    "val=$(tr '\\0' '\\n' < /proc/$pid/environ | grep '^OLLAMA_MODELS=' | cut -d= -f2-); "
    "fi; "
    "echo \"${val:-${OLLAMA_MODELS:-$HOME/.ollama/models}}\""
)

#: Same real-daemon-environment technique as OLLAMA_MODELS_DIR_PROBE, but
#: no fallback baked into the echo -- an empty result must mean "unknown",
#: not a guessed default, since Ollama's own unset behavior is a dynamic,
#: undocumented computation (`Accelerator.fits`'s eviction credit only
#: ever applies when this comes back exactly "1", confirmed live, never
#: assumed). See HANDOFF.md §2.35.
OLLAMA_MAX_LOADED_MODELS_PROBE = (
    "pid=$(pgrep -x ollama | head -1); "
    "if [ -n \"$pid\" ] && [ -r \"/proc/$pid/environ\" ]; then "
    "tr '\\0' '\\n' < /proc/$pid/environ | grep '^OLLAMA_MAX_LOADED_MODELS=' | cut -d= -f2-; "
    "fi"
)


#: Standardized meta-filenames (agent instructions, changelog, license --
#: about the library's own upkeep, not a fact about the machine) excluded
#: from the topic index. Deliberately a small, well-known, industry-wide
#: set rather than anything specific to one library's own naming, so this
#: stays generalizable to any configured knowledge directory.
KNOWLEDGE_META_FILENAMES: frozenset[str] = frozenset({
    "claude.md", "changelog.md", "readme.md", "agents.md",
    "license.md", "contributing.md",
})


def _scan_knowledge_topics(root: Path) -> dict[str, str]:
    """First heading of every regular `.md` file directly inside `root`,
    keyed by filename, meta-filenames excluded.

    Only the heading, not the content: some of these files run past
    100KB, and the point is an index cheap enough to include on every
    turn, not the library itself -- the model reads a specific file via
    an ordinary step once it decides it needs one.
    """
    headings: dict[str, str] = {}
    for md in sorted(root.glob("*.md")):
        lower = md.name.lower()
        if lower in KNOWLEDGE_META_FILENAMES or lower.endswith("-claude.md"):
            continue
        try:
            # Same fix as ConflictDetector.hash_file(): a FIFO opened for
            # reading with no writer blocks forever. observe() runs this
            # on essentially every conversation turn, so a single stray
            # FIFO named *.md in a configured knowledge directory would
            # wedge every turn after it, not just one.
            if not stat.S_ISREG(md.stat().st_mode):
                continue
            with md.open("r", encoding="utf-8", errors="replace") as fh:
                heading = next((s for line in fh if (s := line.strip())), "")
        except OSError:
            continue
        headings[md.name] = heading.lstrip("#").strip()[:80]
    return headings


def _scan_knowledge_dir(
        path: os.PathLike[str] | str, hostname: str = "") -> tuple[str, dict[str, str]]:
    """Top-level `.md` topics in a configured site-knowledge directory, plus
    (when present) this machine's own subfolder: `machines/<hostname>/*.md`.

    Not recursive beyond that one specific, well-known layout: `fixes/`,
    `raw/`, `lists/`-style subdirectories are implementation detail for
    whatever maintains the library, not something to advertise as a topic.
    `machines/<hostname>/` is different -- found live against a real
    library (`system-library`'s own README: "Each machine gets a folder"):
    the per-machine survey (`quirks.md`, `hardware.md`, `services.md`, ...)
    is the library's actual primary content, one level below the root docs
    this function used to stop at, so a hostname match without one silently
    left the single most relevant file invisible to the model. Machine
    entries are keyed `machines/<hostname>/<file>.md` so a same-named root
    file (e.g. both `system-library/network.md` and
    `system-library/machines/workstation/network.md` exist) can't collide.
    """
    root = Path(path)
    if not root.is_dir():
        return "", {}
    headings = _scan_knowledge_topics(root)
    if hostname:
        machine_dir = root / "machines" / hostname
        if machine_dir.is_dir():
            headings.update({
                f"machines/{hostname}/{name}": heading
                for name, heading in _scan_knowledge_topics(machine_dir).items()})
    return str(root), headings


def observe_machine(
    extra: dict[str, str] | None = None,
    ollama: OllamaClient | None = None,
    runner: Callable[[str, str | None, float | None], str] | None = None,
    knowledge_dir: os.PathLike[str] | str | None = None,
) -> MachineState:
    """Gather read-only facts about the machine."""
    # A probe that fails is recorded on state.errors rather than raised: an
    # observation step that can abort a task would make the agent less
    # reliable than no observation at all.
    execute = runner or shell
    state = MachineState(cwd=os.getcwd())

    if knowledge_dir is not None:
        try:
            # socket.gethostname(), not state.hostname -- the `hostname`
            # OBSERVE_COMMANDS probe below hasn't run yet at this point,
            # and this value is only ever used as a `machines/<hostname>/`
            # lookup key, never reported to the operator as an observed
            # fact, so it doesn't need to come from the same real-command
            # path everything in `state` otherwise does.
            state.knowledge_library_path, state.knowledge_library = \
                _scan_knowledge_dir(knowledge_dir, hostname=socket.gethostname())
        except Exception as exc:
            state.errors.append(f"knowledge_library: {type(exc).__name__}: {exc}")

    for name, command in OBSERVE_COMMANDS:
        try:
            setattr(state, name, execute(command, None, 15.0))
        except Exception as exc:
            state.errors.append(f"{name}: {type(exc).__name__}: {exc}")

    for name, command in (extra or {}).items():
        try:
            state.extras[name] = execute(command, None, 20.0)
        except Exception as exc:
            state.errors.append(f"{name}: {type(exc).__name__}: {exc}")

    try:
        state.accelerator = detect_accelerator(execute)
    except Exception as exc:
        state.errors.append(f"accelerator: {type(exc).__name__}: {exc}")

    if ollama is not None:
        state.ollama_models = ollama.models()
        try:
            state.ollama_models_dir = execute(OLLAMA_MODELS_DIR_PROBE, None, 5.0).strip()
        except Exception as exc:
            state.errors.append(f"ollama_models_dir: {type(exc).__name__}: {exc}")

    return state


def resolve_executor_config(
    host: str | None = None, user: str | None = None, key_path: str | None = None,
) -> tuple[str, str, str] | None:
    """(host, user, key_path) for an optional second, SSH-reachable machine
    ("executor") -- explicit args win, else HARNESS_EXECUTOR_HOST/_USER/
    _SSH_KEY from the environment, same explicit-overrides-env precedent as
    Harness._resolve_knowledge_dir. Gated on `host` alone: `user`/`key_path`
    may resolve empty and fall through to ssh's own config/agent/default-
    identity resolution. None (feature entirely absent, no SSH ever
    attempted) when no host resolves either way -- read by
    Agent.__init__ once at construction, not something a caller ordinarily
    calls directly."""
    resolved_host = host if host is not None else os.environ.get("HARNESS_EXECUTOR_HOST", "")
    if not resolved_host:
        return None
    resolved_user = user if user is not None else os.environ.get("HARNESS_EXECUTOR_USER", "")
    resolved_key = key_path if key_path is not None else os.environ.get(
        "HARNESS_EXECUTOR_SSH_KEY", "")
    return (resolved_host, resolved_user, resolved_key)


def build_ssh_runner(
    host: str, user: str = "", key_path: str = "", connect_timeout: float = 5.0,
) -> Callable[[str, str | None, float | None], str]:
    """A runner matching shell()'s exact contract (same signature, same
    raise-on-nonzero/return-stripped-stdout behavior), wrapping each command
    in a non-interactive SSH call to `host` instead of running it locally --
    drop-in for observe_machine()'s/detect_accelerator()'s existing `runner`
    injection point, never a Step's own execution path (HANDOFF.md §2.52:
    nothing calls this but Agent's own default observer).

    BatchMode=yes plus a closed stdin guarantees ssh can never sit waiting on
    a passphrase/host-key prompt nobody is watching -- that would otherwise
    hang an unrelated, local-only turn. StrictHostKeyChecking=accept-new
    (not =no) trusts a new host key on first contact but still fails on a
    *changed* key later.
    """
    def run(command: str, cwd: str | None, timeout: float | None) -> str:
        import shlex
        import subprocess

        remote_command = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
               "-o", f"ConnectTimeout={int(connect_timeout)}",
               "-o", "StrictHostKeyChecking=accept-new"]
        if key_path:
            argv += ["-i", os.path.expanduser(key_path)]
        argv.append(f"{user}@{host}" if user else host)
        argv.append(remote_command)
        result = subprocess.run(argv, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=timeout or 3600)
        if result.returncode != 0:
            raise RuntimeError(
                f"exit {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:400]}")
        return (result.stdout or "").strip()
    return run


def observe_executor(
    runner: Callable[[str, str | None, float | None], str],
    extra: dict[str, str] | None = None,
) -> MachineState:
    """Read-only facts about a second machine, reusing observe_machine()/
    OBSERVE_COMMANDS/detect_accelerator() completely unchanged -- just
    pointed at `runner` instead of local shell(). Never raises: a
    genuinely unreachable or misconfigured executor degrades to a mostly-
    empty MachineState with the failure in .errors, the same guarantee
    observe_machine() already gives each individual local probe. This
    function's own try/except is defense-in-depth on top of that -- it
    makes "never raises" an absolute guarantee here specifically, not just
    an inherited property. `ollama=None` deliberately: getting executor's
    model list would need a second real OllamaClient pointed at its
    daemon, out of scope for a first version (HANDOFF.md §2.52)."""
    try:
        return observe_machine(runner=runner, extra=extra)
    except Exception as exc:
        return MachineState(errors=[f"executor: {type(exc).__name__}: {exc}"])
