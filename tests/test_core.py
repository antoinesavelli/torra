"""observe_machine: the facts the model plans from, and the probes behind them.

Depends on: harness.core (observe_machine, OllamaClient).
"""

from __future__ import annotations

from harness.core import OllamaClient, observe_machine
from harness.core.machine import OLLAMA_MODELS_DIR_PROBE


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
