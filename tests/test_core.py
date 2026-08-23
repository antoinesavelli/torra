"""observe_machine and shell(): the facts the model plans from, and how its
commands actually run. Also ConflictDetector's fast (inotify) path.

Depends on: harness.core (observe_machine, OllamaClient, ConflictDetector),
harness.core.tasks (shell).
"""

from __future__ import annotations

import pytest

from harness.core import ConflictDetector, OllamaClient, observe_machine
from harness.core.machine import OLLAMA_MODELS_DIR_PROBE, _scan_knowledge_dir, detect_accelerator
from harness.core.tasks import TaskSpec, TaskSpecError, shell


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


class TestKnowledgeLibrary:
    """observe_machine(knowledge_dir=...): a configured, optional local
    directory of site-specific documentation the model can propose a
    read-only step against, instead of guessing or asking the operator for
    something already documented. See scripts/dev/model_bench/REPORT.md's
    2026-08-23 entry for the measured, model-capability-dependent result
    this was actually built to close a gap on."""

    def test_top_level_md_files_are_indexed_by_first_heading(self, tmp_path) -> None:
        (tmp_path / "services.md").write_text(
            "# Services and processes\n\nbody text\n", encoding="utf-8")
        (tmp_path / "quirks.md").write_text(
            "\n\n# Known quirks and gotchas\nbody\n", encoding="utf-8")

        path, headings = _scan_knowledge_dir(tmp_path)
        assert path == str(tmp_path)
        assert headings == {
            "quirks.md": "Known quirks and gotchas",
            "services.md": "Services and processes",
        }

    def test_meta_filenames_are_excluded(self, tmp_path) -> None:
        """CLAUDE.md/CHANGELOG.md document the library's own upkeep, not a
        fact about the machine -- including them risks the model trying
        to "consult" agent instructions for hardware facts."""
        (tmp_path / "CLAUDE.md").write_text("# instructions\n", encoding="utf-8")
        (tmp_path / "home-CLAUDE.md").write_text("# more instructions\n", encoding="utf-8")
        (tmp_path / "CHANGELOG.md").write_text("# changes\n", encoding="utf-8")
        (tmp_path / "hardware.md").write_text("# Hardware\n", encoding="utf-8")

        _path, headings = _scan_knowledge_dir(tmp_path)
        assert headings == {"hardware.md": "Hardware"}

    def test_a_missing_directory_degrades_to_empty_not_a_crash(self) -> None:
        path, headings = _scan_knowledge_dir("/no/such/directory/at/all")
        assert path == ""
        assert headings == {}

    def test_observe_machine_populates_the_new_fields(self, tmp_path) -> None:
        (tmp_path / "storage.md").write_text("# Storage layout\n", encoding="utf-8")

        def runner(command: str, cwd: str | None, timeout: float | None) -> str:
            return ""

        state = observe_machine(runner=runner, knowledge_dir=tmp_path)
        assert state.knowledge_library_path == str(tmp_path)
        assert state.knowledge_library == {"storage.md": "Storage layout"}

    def test_render_includes_the_section_only_when_configured(self, tmp_path) -> None:
        (tmp_path / "storage.md").write_text("# Storage layout\n", encoding="utf-8")

        def runner(command: str, cwd: str | None, timeout: float | None) -> str:
            return ""

        with_library = observe_machine(runner=runner, knowledge_dir=tmp_path)
        without_library = observe_machine(runner=runner)

        assert "storage.md: Storage layout" in with_library.render()
        assert "knowledge library" in with_library.render()
        assert "knowledge library" not in without_library.render()
        assert "storage.md" not in without_library.render()

    def test_a_bad_knowledge_dir_probe_does_not_abort_observation(self, monkeypatch) -> None:
        """Matches every other probe in observe_machine: a failure here is
        recorded, not raised."""
        import harness.core.machine as machine_module

        def broken_scan(path):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(machine_module, "_scan_knowledge_dir", broken_scan)

        def runner(command: str, cwd: str | None, timeout: float | None) -> str:
            return "ok"

        state = observe_machine(runner=runner, knowledge_dir="/whatever")
        assert state.knowledge_library == {}
        assert any("knowledge_library" in e for e in state.errors)
        assert state.hostname == "ok"


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


class TestHttpErrorDetail:
    """OllamaClient._http(): every other test in this file injects a fake
    `transport`, which never exercises the real urllib path at all. Found
    live: a bad or unpulled model name reached the operator as the generic
    "HTTP Error 404: Not Found" -- Ollama's own response body
    ({"error": "model '...' not found"}) was never read, discarding exactly
    the detail needed to fix the request."""

    def test_ollamas_own_error_message_surfaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json as json_module
        import urllib.error
        import urllib.request

        from harness.core.tasks import OllamaError

        error = urllib.error.HTTPError(
            "http://localhost:11434/api/chat", 404, "Not Found", None, None)
        monkeypatch.setattr(error, "read",
                            lambda: json_module.dumps(
                                {"error": "model 'ghost:latest' not found"}).encode())

        def fake_urlopen(request: object, timeout: float) -> object:
            raise error

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        client = OllamaClient()
        with pytest.raises(OllamaError, match="model 'ghost:latest' not found"):
            client.chat("ghost:latest", [{"role": "user", "content": "hi"}])

    def test_a_non_json_error_body_falls_back_to_the_http_reason(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.request

        from harness.core.tasks import OllamaError

        error = urllib.error.HTTPError(
            "http://localhost:11434/api/chat", 502, "Bad Gateway", None, None)
        monkeypatch.setattr(error, "read", lambda: b"<html>not json</html>")

        def fake_urlopen(request: object, timeout: float) -> object:
            raise error

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        client = OllamaClient()
        with pytest.raises(OllamaError, match="Bad Gateway"):
            client.chat("x", [{"role": "user", "content": "hi"}])


class TestTaskSpecRollbackValidation:
    """TaskSpec.validate() had zero direct test coverage of its own rejection
    paths before this -- `_validate_step` (steps) and rollback entries alike.
    Found live: a rollback entry with a `run` typo (e.g. `cmd` instead of
    `run`) passed validate() silently, and rollback_actions() then just
    `continue`d past it with no error -- the operator believed they had a
    declared rollback and actually had none, discoverable only by reading
    the entry back by hand or watching it not fire during a real failure.
    Rollback entries now go through the same strict validation steps
    already got."""

    def _base(self, rollback: list[dict]) -> dict:
        return {"task_id": "t", "request": "r",
               "steps": [{"id": "s1", "run": "true"}], "rollback": rollback}

    def test_a_missing_run_is_rejected_at_validate_time_not_silently_dropped(
            self) -> None:
        with pytest.raises(TaskSpecError, match="no 'run' command"):
            TaskSpec.validate(self._base([{"description": "typo, no run key"}]))

    def test_a_run_typo_is_rejected_as_an_unknown_field(self) -> None:
        """The typo that actually motivated this: `cmd` instead of `run`.
        Caught as "unknown field" rather than "no run command" -- either
        message is honest about the same underlying defect, but this is
        the literal case that used to validate cleanly and silently drop."""
        with pytest.raises(TaskSpecError, match="unknown field"):
            TaskSpec.validate(self._base(
                [{"cmd": "rm -rf /tmp/x", "description": "typo"}]))

    def test_an_unknown_rollback_field_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="unknown field"):
            TaskSpec.validate(self._base(
                [{"run": "true", "retries": 3}]))

    def test_a_non_mapping_rollback_entry_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="must be a mapping"):
            TaskSpec.validate(self._base(["just a string"]))

    def test_a_well_formed_rollback_entry_still_validates_cleanly(self) -> None:
        spec = TaskSpec.validate(self._base(
            [{"run": "rm -f /tmp/x", "description": "cleanup", "safe": True}]))
        assert len(spec.rollback_actions()) == 1

    def test_an_absent_rollback_list_is_fine(self) -> None:
        spec = TaskSpec.validate({"task_id": "t", "request": "r",
                                  "steps": [{"id": "s1", "run": "true"}]})
        assert spec.rollback_actions() == []


class TestDetectAccelerator:
    """detect_accelerator(): no test anywhere exercised this before -- the
    module-level "imported by tests" metric passed (observe_machine pulls
    it in) while the function itself had zero cases. Found live: current
    `ollama ps` output carries CONTEXT and UNTIL columns after PROCESSOR
    ("100% CPU     4096       5 minutes from now"), so the old
    line.split()[-2:] grabbed trailing UNTIL words instead of the
    processor split -- a model ollama itself was running on pure CPU was
    silently reported as GPU-accelerated, backwards from what this
    function's own docstring promises ("ollama ps wins because it is the
    only one that reflects what the daemon actually decided")."""

    NVIDIA_SMI = "NVIDIA GeForce RTX 4090, 24564, 23800"

    def _runner(self, ps_output: str):
        def runner(cmd: str, cwd: str | None, timeout: float | None) -> str:
            if cmd == "ollama ps":
                return ps_output
            if cmd.startswith("nvidia-smi"):
                return self.NVIDIA_SMI
            raise RuntimeError("not available")
        return runner

    def test_a_cpu_loaded_model_is_reported_as_cpu_even_with_a_real_gpu_present(
            self) -> None:
        ps = ("NAME              ID              SIZE      PROCESSOR    "
             "CONTEXT    UNTIL              \n"
             "somemodel:latest  abc123def456    5.0 GB    100% CPU     "
             "4096       5 minutes from now    \n")
        acc = detect_accelerator(self._runner(ps))
        assert acc.kind == "cpu"
        assert "CPU" in acc.running_on

    def test_a_gpu_loaded_model_falls_through_to_nvidia_smi_for_vram_figures(
            self) -> None:
        ps = ("NAME              ID              SIZE      PROCESSOR    "
             "CONTEXT    UNTIL              \n"
             "qwen3.5:latest    6488c96fa5fa    9.5 GB    100% GPU     "
             "32768      3 minutes from now    \n")
        acc = detect_accelerator(self._runner(ps))
        assert acc.kind == "cuda"
        assert acc.free_vram_mb == 23800

    def test_partial_cpu_gpu_offload_is_not_misread_as_pure_cpu(self) -> None:
        ps = ("NAME              ID              SIZE      PROCESSOR      "
             "CONTEXT    UNTIL              \n"
             "qwen3-coder:30b   06c1097efce0    18 GB     43%/57% CPU/GPU  "
             "8192       10 minutes from now    \n")
        acc = detect_accelerator(self._runner(ps))
        assert acc.kind == "cuda"

    def test_no_model_loaded_falls_through_to_nvidia_smi(self) -> None:
        ps = "NAME    ID    SIZE    PROCESSOR    CONTEXT    UNTIL\n"
        acc = detect_accelerator(self._runner(ps))
        assert acc.kind == "cuda"
        assert acc.name == "NVIDIA GeForce RTX 4090"

    def test_nothing_available_degrades_to_cpu_with_a_reason(self) -> None:
        def runner(cmd: str, cwd: str | None, timeout: float | None) -> str:
            raise RuntimeError("not available")
        acc = detect_accelerator(runner)
        assert acc.kind == "cpu"
        assert "nvidia-smi" in acc.detail


class TestConflictDetectorFastPath:
    """_detect_tree()'s inotify fast path. Found live: `_known` is an
    accumulator across calls, and the per-event loop only ever added to it
    on a real conflict -- never removed a stale entry when a touched path
    turned out to no longer differ from baseline (edited then edited back
    to the same bytes, a plausible editor-autosave or no-op-write pattern).
    A conflict recorded on one fast check stuck around across every later
    detect() call until the next scheduled full walk rebuilt `_known` from
    scratch -- long enough to feed Harness._run_steps() a false "external
    conflict" and block a real run on nothing."""

    def _detector(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("original content", encoding="utf-8")
        detector = ConflictDetector(roots=[tmp_path], use_inotify=False)
        detector.baseline([str(target)], roots=[tmp_path])
        detector._inotify = True  # truthy stand-in; only .read() is ever called on it
        return detector, target

    def test_a_reverted_file_is_cleared_from_known_conflicts(self, tmp_path) -> None:
        detector, target = self._detector(tmp_path)

        target.write_text("MODIFIED", encoding="utf-8")
        detector._events = lambda: {str(target)}
        found = detector._detect_tree()
        assert [c.path for c in found] == [str(target)], "the real edit must be reported"

        target.write_text("original content", encoding="utf-8")
        detector._events = lambda: {str(target)}
        found = detector._detect_tree()
        assert found == [], "reverted to baseline -- must not still be reported"

    def test_an_unrelated_conflict_survives_a_different_paths_revert(self, tmp_path) -> None:
        """The fix must remove only the path that actually reverted, not
        every known conflict."""
        detector, target = self._detector(tmp_path)
        other = tmp_path / "other.txt"
        other.write_text("original other", encoding="utf-8")
        detector.hashes[str(other)] = detector.hash_file(other)
        detector.fingerprints[str(other)] = (
            other.stat().st_size, other.stat().st_mtime_ns)

        target.write_text("MODIFIED", encoding="utf-8")
        other.write_text("ALSO MODIFIED", encoding="utf-8")
        detector._events = lambda: {str(target), str(other)}
        detector._detect_tree()

        target.write_text("original content", encoding="utf-8")
        detector._events = lambda: {str(target)}
        found = detector._detect_tree()
        assert [c.path for c in found] == [str(other)], \
            "target reverted and must be gone; other's real conflict must remain"


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
