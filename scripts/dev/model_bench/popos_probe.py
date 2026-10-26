#!/usr/bin/env python3
"""Probe each model's Pop!_OS-specific factual knowledge, to decide on a docs/ folder.

Depends on: bench.py (transport/metrics), torra.core.OllamaClient. Mixes generic
Ubuntu-derived facts (most models should know these) with System76/Pop!_OS-specific
facts (a plausible gap, since Pop!_OS is a smaller distro with less training data than
Ubuntu proper) to see where knowledge actually breaks down rather than assuming it does.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import OLLAMA_HOST, make_metrics_transport  # noqa: E402

from torra.core import OllamaClient  # noqa: E402

#: Updated 2026-08-28: qwen3:32b/qwen3.5:latest/qwen3-coder:30b were swapped
#: for non-Chinese equivalents in torra/gates/router.py's ROUTING_RULES
#: (operator request); this list follows so a future probe run tests what's
#: actually live rather than retired models. devstral:24b is untouched.
#: muse-glimmer:30b briefly held the debug/plan/analyze slot the same day
#: but was replaced by gpt-oss:20b after a real bench.py run found it tied
#: on accuracy with ~6x the latency and a hard timeout -- dropped from
#: this list too, since it is no longer what the router actually routes to.
MODELS = ["gpt-oss:20b", "devstral-small-2:24b", "devstral:24b"]


@dataclass
class Probe:
    """One factual question with the ground truth to check the answer against."""

    id: str
    specificity: str  # "generic" (Ubuntu-derived) or "pop-specific" (System76/Pop!_OS only)
    question: str
    #: Substrings whose presence indicates a correct/on-target answer. Not exhaustive
    #: NLU -- a human should still skim answers -- but catches clear hits/misses.
    expect_any: tuple[str, ...]


PROBES: list[Probe] = [
    Probe(
        "package-manager",
        "generic",
        "What command-line package manager does Pop!_OS use to install packages?",
        ("apt",),
    ),
    Probe("init-system", "generic", "What init system does Pop!_OS use?", ("systemd",)),
    Probe("developer", "generic", "What company develops Pop!_OS?", ("system76",)),
    Probe(
        "tiling-feature",
        "pop-specific",
        "What is Pop!_OS's built-in tiling window management feature called?",
        ("pop shell", "pop-shell", "popshell"),
    ),
    Probe(
        "upgrade-tool",
        "pop-specific",
        "What is the Pop!_OS-specific command-line tool for checking and applying "
        "OS upgrades and release upgrades (not just `apt`)?",
        ("pop-upgrade",),
    ),
    Probe(
        "graphics-switching",
        "pop-specific",
        "On System76/Pop!_OS hardware with hybrid NVIDIA graphics, what tool or "
        "command manages switching between integrated, NVIDIA, and hybrid graphics modes?",
        ("system76-power", "graphics mode", "switcheroo"),
    ),
    Probe(
        "graphics-switching-scope",
        "pop-specific",
        "Does `system76-power graphics <mode>` work on every System76/Pop!_OS "
        "machine, or does it require specific hardware? What would running "
        "`system76-power graphics integrated` actually do on a desktop tower with "
        "a single discrete GPU bound to the nvidia driver and no driver bound to "
        "any integrated GPU?",
        (
            "requires",
            "hybrid",
            "laptop",
            "no effect",
            "no integrated",
            "won't",
            "wouldn't",
            "nothing to switch",
            "error",
            "fail",
            "not apply",
            "n/a",
            "no-op",
            "no op",
        ),
    ),
    Probe(
        "recovery-partition",
        "pop-specific",
        "How does a user access the Pop!_OS recovery partition to refresh or "
        "reinstall the OS from the boot menu?",
        ("recovery", "esc", "boot menu", "systemd-boot", "space"),
    ),
    Probe(
        "cosmic-desktop",
        "pop-specific",
        "What is COSMIC in the context of Pop!_OS, and is it Rust or another language?",
        ("rust", "desktop environment", "system76"),
    ),
]


def main() -> int:
    metrics: list = []
    for model in MODELS:
        print(f"\n{'=' * 70}\n{model}\n{'=' * 70}")
        client = OllamaClient(
            host=OLLAMA_HOST, timeout=240.0, transport=make_metrics_transport(metrics)
        )
        for probe in PROBES:
            started = time.perf_counter()
            try:
                messages = [{"role": "user", "content": probe.question}]
                reply = client.chat(model, messages, json_mode=False)
            except Exception as exc:  # noqa: BLE001 -- probe script, log and continue
                print(f"  [ERR] {probe.id}: {type(exc).__name__}: {exc}")
                continue
            elapsed = time.perf_counter() - started
            hit = any(term.lower() in reply.lower() for term in probe.expect_any)
            flag = "HIT " if hit else "MISS"
            print(f"  [{flag}] {probe.specificity:12} {probe.id:20} {elapsed:5.1f}s")
            print(f"         {reply.strip()[:220].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
