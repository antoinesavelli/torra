#!/usr/bin/env python3
"""Probe each model's Pop!_OS-specific factual knowledge, to decide on a docs/ folder.

Depends on: bench.py (transport/metrics), harness.core.OllamaClient. Mixes generic
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

from harness.core import OllamaClient  # noqa: E402

MODELS = ["qwen3:8b", "qwen3:32b", "devstral"]


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
    Probe("package-manager", "generic",
         "What command-line package manager does Pop!_OS use to install packages?",
         ("apt",)),
    Probe("init-system", "generic",
         "What init system does Pop!_OS use?",
         ("systemd",)),
    Probe("developer", "generic",
         "What company develops Pop!_OS?",
         ("system76",)),
    Probe("tiling-feature", "pop-specific",
         "What is Pop!_OS's built-in tiling window management feature called?",
         ("pop shell", "pop-shell", "popshell")),
    Probe("upgrade-tool", "pop-specific",
         "What is the Pop!_OS-specific command-line tool for checking and applying "
         "OS upgrades and release upgrades (not just `apt`)?",
         ("pop-upgrade",)),
    Probe("graphics-switching", "pop-specific",
         "On System76/Pop!_OS hardware with hybrid NVIDIA graphics, what tool or "
         "command manages switching between integrated, NVIDIA, and hybrid graphics modes?",
         ("system76-power", "graphics mode", "switcheroo")),
    Probe("recovery-partition", "pop-specific",
         "How does a user access the Pop!_OS recovery partition to refresh or "
         "reinstall the OS from the boot menu?",
         ("recovery", "esc", "boot menu", "grub")),
    Probe("cosmic-desktop", "pop-specific",
         "What is COSMIC in the context of Pop!_OS, and is it Rust or another "
         "language?",
         ("rust", "desktop environment", "system76")),
]


def main() -> int:
    metrics: list = []
    for model in MODELS:
        print(f"\n{'=' * 70}\n{model}\n{'=' * 70}")
        client = OllamaClient(host=OLLAMA_HOST, timeout=120.0,
                              transport=make_metrics_transport(metrics))
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
