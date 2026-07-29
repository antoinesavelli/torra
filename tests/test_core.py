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


def test_fallback_model_default_is_actually_pulled() -> None:
    """Found in a full-file audit: the default was "qwen3:8b", deleted from
    the catalog along with the rest of the exploratory pull (HANDOFF.md
    §2.9). Both Agent.converse's degraded-status fallback and the "Fall
    back to {model}" recovery-option text were recommending a model that
    no longer exists. Pin this to the current curated four so a future
    catalog change has to touch this constant deliberately, not by
    accident."""
    assert OllamaClient().fallback_model == "qwen3.5:latest"


class TestModelInfo:
    """OllamaClient.model_info(): real per-model size and chat capability
    from `/api/show`, so routing stops guessing from the name alone. Found
    live: a freshly-pulled catalog with names like "qwen3.5" and
    "deepseek-r1-heavy" (no digit in either) and an actual embedding model
    ("nomic-embed-text") all guessed as "0B, fits anywhere" from the name,
    which let the embedding model win a VRAM-starved downgrade fallback
    over every real chat candidate."""

    def _client(self, show_response: dict) -> OllamaClient:
        return OllamaClient(transport=lambda path, payload, timeout: show_response)

    def test_billions_are_parsed(self) -> None:
        info = self._client({"details": {"parameter_size": "14.8B"},
                             "capabilities": ["completion"]}).model_info("x")
        assert info == {"params_b": 14.8, "chat_capable": True}

    def test_millions_are_converted_to_billions(self) -> None:
        info = self._client({"details": {"parameter_size": "137M"},
                             "capabilities": ["embedding"]}).model_info("x")
        assert info["params_b"] == pytest.approx(0.137)

    def test_embedding_only_is_not_chat_capable(self) -> None:
        info = self._client({"details": {"parameter_size": "137M"},
                             "capabilities": ["embedding"]}).model_info("x")
        assert info["chat_capable"] is False

    def test_completion_capability_is_chat_capable(self) -> None:
        info = self._client({"details": {"parameter_size": "8.0B"},
                             "capabilities": ["completion", "tools"]}).model_info("x")
        assert info["chat_capable"] is True

    def test_missing_capabilities_key_defaults_to_chat_capable(self) -> None:
        """An older Ollama that doesn't report capabilities at all must not
        be read as "cannot chat" -- that would block every model on it."""
        info = self._client({"details": {"parameter_size": "8.0B"}}).model_info("x")
        assert info["chat_capable"] is True

    def test_an_unparseable_size_reports_zero_not_a_crash(self) -> None:
        info = self._client({"details": {"parameter_size": "unknown"}}).model_info("x")
        assert info["params_b"] == 0.0

    def test_a_transport_failure_returns_an_empty_dict(self) -> None:
        def transport(path: str, payload: dict, timeout: float) -> dict:
            raise ConnectionRefusedError("no daemon")

        assert OllamaClient(transport=transport).model_info("x") == {}


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
