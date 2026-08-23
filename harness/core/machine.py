"""What the box looks like right now, and what the model will actually run on.

Depends on: harness.core.tasks (shell), harness.core.ollama (OllamaClient, for
the model listing observe_machine attaches).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .ollama import OllamaClient
from .tasks import shell

__all__ = ["Accelerator", "detect_accelerator", "MachineState", "observe_machine"]

#: Parameter counts for models whose name does not carry one.
KNOWN_MODEL_PARAMS_B: dict[str, float] = {
    "devstral": 24.0, "codestral": 22.0, "mistral": 7.0, "mixtral": 47.0,
    "llama3": 8.0, "phi3": 3.8, "gemma2": 9.0, "deepseek-coder": 6.7,
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

    @property
    def gpu(self) -> bool:
        """True when inference will actually be accelerated."""
        return self.kind != "cpu" and self.free_vram_mb > 0

    def fits(self, model: str) -> bool:
        """Whether a model will fit in free VRAM."""
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
        return self.free_vram_mb >= params * 1024 * 0.7

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
                # below for those.
                break
    except Exception:
        pass

    try:
        out = execute(
            "nvidia-smi --query-gpu=name,memory.total,memory.free "
            "--format=csv,noheader,nounits", None, 10.0)
        name, total, free = [p.strip() for p in out.splitlines()[0].split(",")]
        return Accelerator("cuda", name, int(float(total)), int(float(free)))
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
                f"    grep -ril 'KEYWORD' {self.knowledge_library_path}/*.md\n"
                "then read whichever file(s) that finds. Topics, for a narrower "
                "first guess when one obviously fits:",
            ]
            lines += [f"  {name}: {heading}"
                     for name, heading in self.knowledge_library.items()]
        for name, value in self.extras.items():
            lines += ["", f"{name}:", value]
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


#: Standardized meta-filenames (agent instructions, changelog, license --
#: about the library's own upkeep, not a fact about the machine) excluded
#: from the topic index. Deliberately a small, well-known, industry-wide
#: set rather than anything specific to one library's own naming, so this
#: stays generalizable to any configured knowledge directory.
KNOWLEDGE_META_FILENAMES: frozenset[str] = frozenset({
    "claude.md", "changelog.md", "readme.md", "agents.md",
    "license.md", "contributing.md",
})


def _scan_knowledge_dir(path: os.PathLike[str] | str) -> tuple[str, dict[str, str]]:
    """First heading of every top-level `.md` file in a configured
    site-knowledge directory.

    Only the heading, not the content: some of these files run past
    100KB, and the point is an index cheap enough to include on every
    turn, not the library itself -- the model reads a specific file via
    an ordinary step once it decides it needs one. Not recursive: `fixes/`,
    `raw/`, `lists/`-style subdirectories are implementation detail for
    whatever maintains the library, not something to advertise as a
    top-level topic.
    """
    root = Path(path)
    if not root.is_dir():
        return "", {}
    headings: dict[str, str] = {}
    for md in sorted(root.glob("*.md")):
        lower = md.name.lower()
        if lower in KNOWLEDGE_META_FILENAMES or lower.endswith("-claude.md"):
            continue
        try:
            with md.open("r", encoding="utf-8", errors="replace") as fh:
                heading = next((s for line in fh if (s := line.strip())), "")
        except OSError:
            continue
        headings[md.name] = heading.lstrip("#").strip()[:80]
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
            state.knowledge_library_path, state.knowledge_library = \
                _scan_knowledge_dir(knowledge_dir)
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
