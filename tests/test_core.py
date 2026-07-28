"""observe_machine and shell(): the facts the model plans from, and how its
commands actually run.

Depends on: harness.core (observe_machine, OllamaClient), harness.core.tasks (shell).
"""

from __future__ import annotations

import pytest

from harness.core import OllamaClient, observe_machine
from harness.core.machine import OLLAMA_MODELS_DIR_PROBE
from harness.core.tasks import shell


def fake_ollama(models: list[str]) -> OllamaClient:
    """An OllamaClient whose .models() returns a fixed list, no network."""
    return OllamaClient(transport=lambda path, payload, timeout:
                        {"models": [{"name": m} for m in models]})


def test_ollama_models_dir_is_observed() -> None:
    """The running daemon's actual OLLAMA_MODELS, not a guess -- see
    scripts/dev/model_bench/REPORT.md's disk-cleanup scenario, where the
    model hallucinated a plausible-but-wrong path with no grounding."""
    def runner(command: str, cwd: str | None, timeout: float | None) -> str:
        if command == OLLAMA_MODELS_DIR_PROBE:
            return "/mnt/ParamoStorage/ollama-models\n"
        return ""

    state = observe_machine(ollama=fake_ollama(["devstral:latest"]), runner=runner)
    assert state.ollama_models_dir == "/mnt/ParamoStorage/ollama-models"
    assert "local models directory: /mnt/ParamoStorage/ollama-models" in state.render()


def test_a_failed_probe_does_not_abort_observation() -> None:
    """Matches every other probe in observe_machine: a failure here is
    recorded, not raised -- an observation step that can abort a task would
    make the agent less reliable than no observation at all."""
    def runner(command: str, cwd: str | None, timeout: float | None) -> str:
        if command == OLLAMA_MODELS_DIR_PROBE:
            raise RuntimeError("probe failed")
        return "ok"

    state = observe_machine(ollama=fake_ollama([]), runner=runner)
    assert state.ollama_models_dir == ""
    assert any("ollama_models_dir" in e for e in state.errors)
    assert state.hostname == "ok"  # other probes still ran


class TestShellPipefail:
    """`shell()` used /bin/sh (dash on Debian-derived systems) via shell=True,
    whose pipeline exit status is only the *last* command's -- a command that
    fails earlier in a pipe was silently treated as success with whatever
    (often empty) output the tail of the pipe produced on no real input.
    Found live: a real `sudo du -ah / | sort -rh | head -n 20` step reported
    "ok": true with empty output, because sudo had no password and no TTY,
    failed immediately, and `head` on empty stdin exits 0."""

    def test_a_failure_earlier_in_a_pipe_is_not_swallowed(self) -> None:
        with pytest.raises(RuntimeError, match="exit 1"):
            shell("false | cat")

    def test_a_nonexistent_command_earlier_in_a_pipe_is_not_swallowed(self) -> None:
        with pytest.raises(RuntimeError):
            shell("definitely-not-a-real-command-xyz | sort | head -n 5")

    def test_normal_pipes_still_work(self) -> None:
        assert shell("printf '3\\n1\\n2\\n' | sort") == "1\n2\n3"

    def test_the_final_command_alone_failing_still_raises(self) -> None:
        with pytest.raises(RuntimeError, match="exit 1"):
            shell("echo hi | false")
