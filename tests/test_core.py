"""observe_machine and shell(): the facts the model plans from, and how its
commands actually run. Also ConflictDetector's fast (inotify) path.

Depends on: harness.core (observe_machine, OllamaClient, ConflictDetector),
harness.core.tasks (shell).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harness.core import (
    ConflictDetector,
    ConflictKind,
    ContextCompressor,
    MachineState,
    ModelBackend,
    OllamaClient,
    OllamaError,
    OllamaStatus,
    OpenRouterClient,
    observe_machine,
)
from harness.core.conflict import Conflict
from harness.core.machine import (
    OLLAMA_MODELS_DIR_PROBE,
    Accelerator,
    _scan_knowledge_dir,
    detect_accelerator,
)
from harness.core.tasks import (
    ContextBudgetError,
    TaskSpec,
    TaskSpecError,
    shell,
    shell_output_sink,
)


@contextlib.contextmanager
def hang_guard(seconds: float = 3.0):
    """Fail fast with TimeoutError instead of hanging the whole suite if a
    regression on a FIFO/blocking-file-type fix reintroduces a real hang."""
    def handler(signum, frame):
        raise TimeoutError(f"blocked for over {seconds}s -- a real hang, not a slow op")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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


def test_a_failed_core_probe_is_recorded_and_the_rest_still_run() -> None:
    """Same guarantee as the ollama_models_dir case, for the main
    OBSERVE_COMMANDS loop itself -- not previously exercised."""
    def runner(command: str, cwd: str | None, timeout: float | None) -> str:
        if command == "hostname":
            raise RuntimeError("probe failed")
        return "ok"

    state = observe_machine(runner=runner)
    assert state.hostname == ""
    assert any("hostname" in e for e in state.errors)
    assert state.kernel == "ok"  # other probes still ran


def test_an_extra_probe_populates_extras_and_a_failure_does_not_abort_the_rest(
        ) -> None:
    def runner(command: str, cwd: str | None, timeout: float | None) -> str:
        if command == "custom-probe":
            return "custom output"
        if command == "broken-probe":
            raise RuntimeError("boom")
        return "ok"

    state = observe_machine(runner=runner, extra={
        "custom": "custom-probe", "broken": "broken-probe"})

    assert state.extras == {"custom": "custom output"}
    assert any("broken" in e for e in state.errors)
    assert state.hostname == "ok"  # core probes still ran
    assert "custom:" in state.render()
    assert "custom output" in state.render()


def test_an_accelerator_detection_failure_is_recorded_not_raised() -> None:
    def runner(command: str, cwd: str | None, timeout: float | None) -> str:
        if "nvidia-smi" in command or "rocm-smi" in command or "ollama ps" in command:
            raise RuntimeError("no GPU tooling")
        return "ok"

    state = observe_machine(runner=runner)

    assert state.hostname == "ok"  # observation as a whole did not abort
    # detect_accelerator's own try/except already handles missing tooling
    # gracefully (falls back to "cpu"), so this asserts the outer
    # try/except around it is also reachable without breaking anything,
    # not that it necessarily fires for this specific runner.
    assert state.accelerator.kind in ("cpu", "cuda", "rocm")


def test_observe_machine_records_a_genuine_accelerator_bug_not_raises(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """detect_accelerator() guards every branch it controls, so the outer
    try/except in observe_machine can only fire for a bug in
    detect_accelerator itself, not a misbehaving runner -- forced here by
    replacing the function outright, the same way the module's own tests
    scope every other monkeypatch to a specific target."""
    import harness.core.machine as machine_module

    def broken_detect_accelerator(runner=None):
        raise RuntimeError("simulated internal bug")

    monkeypatch.setattr(machine_module, "detect_accelerator", broken_detect_accelerator)

    state = observe_machine(runner=lambda cmd, cwd, timeout: "ok")

    assert any("accelerator: RuntimeError" in e for e in state.errors)


def test_machine_state_to_dict_round_trips_the_basics() -> None:
    state = MachineState(hostname="pop-os", os_release="Pop!_OS 24.04")
    data = state.to_dict()
    assert data["hostname"] == "pop-os"
    assert data["os_release"] == "Pop!_OS 24.04"


def test_scan_knowledge_dir_skips_an_unreadable_file_not_the_whole_scan(
        tmp_path) -> None:
    locked = tmp_path / "locked.md"
    locked.write_text("# Locked\n", encoding="utf-8")
    locked.chmod(0o000)
    (tmp_path / "readable.md").write_text("# Readable\n", encoding="utf-8")
    try:
        _path, headings = _scan_knowledge_dir(tmp_path)
    finally:
        locked.chmod(0o644)

    assert headings == {"readable.md": "Readable"}


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

    def test_a_fifo_named_dot_md_is_skipped_instead_of_hanging(self, tmp_path) -> None:
        """Found by an independent audit, reproduced live: opening a FIFO
        for reading with no writer blocks forever. Agent.observe() runs
        this on essentially every conversation turn, so one stray FIFO
        named *.md in a configured knowledge directory used to wedge every
        turn after it, not just one."""
        os.mkfifo(tmp_path / "quirks.md")
        (tmp_path / "real.md").write_text("# Real Doc\n", encoding="utf-8")

        with hang_guard():
            _path, headings = _scan_knowledge_dir(tmp_path)
        assert headings == {"real.md": "Real Doc"}

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

    def _client(self, show_response: Any) -> OllamaClient:
        """`Any`, not `dict`: some tests deliberately inject a malformed
        (non-dict) response to exercise the "response of the wrong shape"
        defensive path -- see test_a_response_of_the_wrong_shape_..."""
        return OllamaClient(transport=lambda path, payload, timeout: show_response)

    def test_billions_are_parsed(self) -> None:
        info = self._client({"details": {"parameter_size": "14.8B"},
                             "capabilities": ["completion"]}).model_info("x")
        assert info == {"params_b": 14.8, "chat_capable": True, "thinking_capable": False}

    def test_thinking_capability_is_read_from_capabilities(self) -> None:
        info = self._client({"details": {"parameter_size": "8.0B"},
                             "capabilities": ["completion", "thinking"]}).model_info("x")
        assert info["thinking_capable"] is True

    def test_missing_capabilities_key_defaults_to_not_thinking_capable(self) -> None:
        """The opposite default from chat_capable -- not knowing whether a
        model exposes reasoning tokens must mean "don't ask it to stream
        them," not "assume it can."."""
        info = self._client({"details": {"parameter_size": "8.0B"}}).model_info("x")
        assert info["thinking_capable"] is False

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

    def test_a_response_of_the_wrong_shape_returns_an_empty_dict_not_a_crash(
            self) -> None:
        """Found by an independent audit: valid JSON in an unexpected shape
        (a list instead of a mapping, or "capabilities" as something
        non-iterable) used to raise an uncaught TypeError/AttributeError
        here, past this method's own "returns {} on any failure" contract
        and past Harness.plan()'s narrow except clause -- the operator got
        a raw traceback instead of the intended backend-down handling."""
        assert self._client(["not", "a", "mapping"]).model_info("x") == {}
        assert self._client({"details": {"parameter_size": "8B"},
                             "capabilities": 42}).model_info("x") == {}
        assert self._client({"details": "not a mapping either"}).model_info("x") == {}


class TestChatResponseShape:
    """OllamaClient.chat(): found by the same audit -- a response of the
    wrong shape raised an uncaught TypeError/AttributeError instead of the
    OllamaError the docstring promises for "an unusable response"."""

    def test_a_list_response_raises_ollama_error_not_a_crash(self) -> None:
        # Deliberately the wrong shape (list, not dict) -- exercising the
        # defensive path, not a real transport contract violation.
        client = OllamaClient(
            transport=lambda path, payload, timeout: ["not", "a", "mapping"])  # type: ignore[arg-type,return-value]
        with pytest.raises(OllamaError, match="generation failed"):
            client.chat("model", [{"role": "user", "content": "hi"}])

    def test_a_non_mapping_message_field_raises_ollama_error_not_a_crash(self) -> None:
        client = OllamaClient(transport=lambda path, payload, timeout: {"message": "not a dict"})
        with pytest.raises(OllamaError, match="generation failed"):
            client.chat("model", [{"role": "user", "content": "hi"}])

    def test_a_well_formed_response_still_works(self) -> None:
        client = OllamaClient(
            transport=lambda path, payload, timeout: {"message": {"content": "hi there"}})
        assert client.chat("model", [{"role": "user", "content": "hi"}]) == "hi there"

    def test_a_blank_content_raises_ollama_error(self) -> None:
        client = OllamaClient(
            transport=lambda path, payload, timeout: {"message": {"content": "   "}})
        with pytest.raises(OllamaError, match="empty response"):
            client.chat("model", [{"role": "user", "content": "hi"}])


class TestChatThinking:
    """OllamaClient.chat()'s `on_thinking` opt-in: switches to the streaming
    transport and `think: true`, leaves every existing (non-streaming)
    caller's request shape untouched."""

    def test_without_on_thinking_the_plain_transport_is_used(self) -> None:
        """The default path -- every caller that doesn't ask for reasoning
        tokens must see byte-for-byte the same request as before this
        feature existed."""
        seen: dict[str, object] = {}

        def transport(path: str, payload: dict, timeout: float) -> dict:
            seen["payload"] = payload
            return {"message": {"content": "hi there"}}

        def stream_transport(path: str, payload: dict, timeout: float, on_thinking: object) -> dict:
            raise AssertionError("stream_transport must not be used without on_thinking")

        client = OllamaClient(transport=transport, stream_transport=stream_transport)
        assert client.chat("model", [{"role": "user", "content": "hi"}]) == "hi there"
        assert seen["payload"]["stream"] is False
        assert "think" not in seen["payload"]

    def test_on_thinking_switches_to_the_streaming_transport(self) -> None:
        seen: dict[str, object] = {}

        def transport(path: str, payload: dict, timeout: float) -> dict:
            raise AssertionError("the plain transport must not be used with on_thinking")

        def stream_transport(path: str, payload: dict, timeout: float, on_thinking: object) -> dict:
            seen["payload"] = payload
            return {"message": {"content": "the plan"}}

        client = OllamaClient(transport=transport, stream_transport=stream_transport)
        result = client.chat("model", [{"role": "user", "content": "hi"}],
                             on_thinking=lambda chunk: None)
        assert result == "the plan"
        assert seen["payload"]["stream"] is True
        assert seen["payload"]["think"] is True

    def test_reasoning_deltas_reach_the_callback_in_order(self) -> None:
        def stream_transport(path: str, payload: dict, timeout: float, on_thinking) -> dict:
            for chunk in ("Let me ", "think about ", "this."):
                on_thinking(chunk)
            return {"message": {"content": "final answer"}}

        received: list[str] = []
        client = OllamaClient(stream_transport=stream_transport)
        result = client.chat("model", [{"role": "user", "content": "hi"}],
                             on_thinking=received.append)
        assert received == ["Let me ", "think about ", "this."]
        assert result == "final answer"

    def test_http_stream_reassembles_ndjson_and_calls_on_thinking_per_line(self) -> None:
        """The real transport, not an injected fake -- proves the NDJSON
        parsing itself, one JSON object per line, content re-assembled
        from every line's delta the same way `_http` returns a single dict."""

        lines = [
            json.dumps({"message": {"content": "", "thinking": "step one. "}, "done": False}),
            json.dumps({"message": {"content": "", "thinking": "step two."}, "done": False}),
            json.dumps({"message": {"content": "{\"a\": 1}", "thinking": ""}, "done": False}),
            json.dumps({"message": {"content": "", "thinking": ""}, "done": True,
                       "total_duration": 123}),
        ]

        class FakeResponse:
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def __iter__(self) -> Any:
                return iter(line.encode("utf-8") + b"\n" for line in lines)

        received: list[str] = []
        client = OllamaClient()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())
            result = client.chat("model", [{"role": "user", "content": "hi"}],
                                 on_thinking=received.append)
        assert received == ["step one. ", "step two."]
        assert result == '{"a": 1}'


class TestHttpErrorDetail:
    """OllamaClient._http(): every other test in this file injects a fake
    `transport`, which never exercises the real urllib path at all. Found
    live: a bad or unpulled model name reached the operator as the generic
    "HTTP Error 404: Not Found" -- Ollama's own response body
    ({"error": "model '...' not found"}) was never read, discarding exactly
    the detail needed to fix the request."""

    def test_ollamas_own_error_message_surfaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import email.message
        import json as json_module
        import urllib.error
        import urllib.request

        from harness.core.tasks import OllamaError

        error = urllib.error.HTTPError(
            "http://localhost:11434/api/chat", 404, "Not Found",
            email.message.Message(), None)
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
        import email.message
        import urllib.error
        import urllib.request

        from harness.core.tasks import OllamaError

        error = urllib.error.HTTPError(
            "http://localhost:11434/api/chat", 502, "Bad Gateway",
            email.message.Message(), None)
        monkeypatch.setattr(error, "read", lambda: b"<html>not json</html>")

        def fake_urlopen(request: object, timeout: float) -> object:
            raise error

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        client = OllamaClient()
        with pytest.raises(OllamaError, match="Bad Gateway"):
            client.chat("x", [{"role": "user", "content": "hi"}])

    def test_an_empty_payload_request_uses_get_not_post(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_http`'s GET branch (empty payload, as health()/models() send) --
        every other test here either injects a fake `transport` (bypassing
        `_http` entirely) or already goes through the POST branch. This
        deliberately still mocks `urlopen` rather than hitting a real
        daemon: CLAUDE.md's own rule for this test suite is that nothing in
        `tests/` touches a live Ollama, systemd, or network, precisely so
        the suite stays green in CI, which has no Ollama installed."""
        import urllib.request

        seen: dict[str, object] = {}

        class FakeResponse:
            def read(self) -> bytes:
                return b'{"models": []}'

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            seen["method"] = request.get_method()
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        client = OllamaClient()
        status = client.health()

        assert seen["method"] == "GET"
        assert status is OllamaStatus.HEALTHY


class TestOpenRouterClient:
    """OpenRouterClient: mirrors OllamaClient's injectable-transport shape
    (TestChatResponseShape above) but for OpenAI-compatible request/response
    shapes and an env-var-sourced API key."""

    def test_satisfies_the_model_backend_protocol(self) -> None:
        assert isinstance(OpenRouterClient(api_key="k"), ModelBackend)

    def test_chat_success(self) -> None:
        client = OpenRouterClient(
            api_key="k",
            transport=lambda path, payload, timeout: {
                "choices": [{"message": {"content": "hi there"}}]})
        assert client.chat("anthropic/claude-3.5-sonnet",
                           [{"role": "user", "content": "hi"}]) == "hi there"

    def test_json_mode_sets_response_format_not_ollamas_format_field(self) -> None:
        seen: dict[str, Any] = {}

        def transport(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
            seen.update(payload)
            return {"choices": [{"message": {"content": "{}"}}]}

        client = OpenRouterClient(api_key="k", transport=transport)
        client.chat("x", [{"role": "user", "content": "hi"}], json_mode=True)

        assert seen["response_format"] == {"type": "json_object"}
        assert "format" not in seen

    def test_missing_api_key_raises_a_specific_error_before_any_request(self) -> None:
        called = False

        def transport(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
            nonlocal called
            called = True
            return {}

        client = OpenRouterClient(api_key="", transport=transport)
        with pytest.raises(OllamaError, match="OPENROUTER_API_KEY is not set"):
            client.chat("x", [{"role": "user", "content": "hi"}])
        assert not called, "no request should be sent with an empty key"

    def test_api_key_defaults_from_the_environment(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
        client = OpenRouterClient()
        assert client.api_key == "env-key"

    def test_explicit_api_key_overrides_the_environment(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
        client = OpenRouterClient(api_key="explicit-key")
        assert client.api_key == "explicit-key"

    def test_no_choices_in_response_raises_ollama_error_not_a_crash(self) -> None:
        client = OpenRouterClient(
            api_key="k", transport=lambda path, payload, timeout: {"choices": []})
        with pytest.raises(OllamaError, match="generation failed"):
            client.chat("x", [{"role": "user", "content": "hi"}])

    def test_a_list_response_raises_ollama_error_not_a_crash(self) -> None:
        client = OpenRouterClient(
            api_key="k",
            transport=lambda path, payload, timeout: ["not", "a", "mapping"])  # type: ignore[arg-type,return-value]
        with pytest.raises(OllamaError, match="generation failed"):
            client.chat("x", [{"role": "user", "content": "hi"}])

    def test_a_blank_content_raises_ollama_error(self) -> None:
        client = OpenRouterClient(
            api_key="k",
            transport=lambda path, payload, timeout: {
                "choices": [{"message": {"content": "   "}}]})
        with pytest.raises(OllamaError, match="empty response"):
            client.chat("x", [{"role": "user", "content": "hi"}])

    def test_recovery_options_are_openrouter_shaped_not_ollamas(self) -> None:
        titles = " ".join(o.title for o in OpenRouterClient(api_key="k").recovery_options())
        assert "OPENROUTER_API_KEY" in titles
        assert "Ollama" not in titles and "systemctl" not in titles

    def test_stats_reports_calls_and_host(self) -> None:
        client = OpenRouterClient(
            api_key="k",
            transport=lambda path, payload, timeout: {
                "choices": [{"message": {"content": "ok"}}]})
        client.chat("x", [{"role": "user", "content": "hi"}])
        stats = client.stats()
        assert stats["calls"] == 1
        assert stats["host"] == OpenRouterClient.DEFAULT_HOST

    def test_openrouters_nested_error_message_surfaces(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OpenRouter nests error detail under {"error": {"message": ...}},
        not Ollama's flat {"error": "..."} -- a different shape, same lesson
        as TestHttpErrorDetail: the generic HTTP reason isn't the detail an
        operator needs."""
        import email.message
        import json as json_module
        import urllib.error
        import urllib.request

        error = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/chat/completions", 401, "Unauthorized",
            email.message.Message(), None)
        monkeypatch.setattr(error, "read",
                            lambda: json_module.dumps(
                                {"error": {"code": 401, "message": "invalid API key"}}).encode())

        def fake_urlopen(request: object, timeout: float) -> object:
            raise error

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        client = OpenRouterClient(api_key="bad-key")
        with pytest.raises(OllamaError, match="invalid API key"):
            client.chat("x", [{"role": "user", "content": "hi"}])

    def test_a_non_json_error_body_falls_back_to_the_http_reason(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        import email.message
        import urllib.error
        import urllib.request

        error = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/chat/completions", 502, "Bad Gateway",
            email.message.Message(), None)
        monkeypatch.setattr(error, "read", lambda: b"<html>not json</html>")

        def fake_urlopen(request: object, timeout: float) -> object:
            raise error

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        client = OpenRouterClient(api_key="k")
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
            # Deliberately the wrong shape -- exercising validate()'s own
            # rejection of a non-mapping rollback entry.
            TaskSpec.validate(self._base(["just a string"]))  # type: ignore[list-item]

    def test_a_well_formed_rollback_entry_still_validates_cleanly(self) -> None:
        spec = TaskSpec.validate(self._base(
            [{"run": "rm -f /tmp/x", "description": "cleanup", "safe": True}]))
        assert len(spec.rollback_actions()) == 1

    def test_an_absent_rollback_list_is_fine(self) -> None:
        spec = TaskSpec.validate({"task_id": "t", "request": "r",
                                  "steps": [{"id": "s1", "run": "true"}]})
        assert spec.rollback_actions() == []

    def test_a_quoted_string_safe_value_is_rejected(self) -> None:
        """Found by an independent audit: bool("false") is True in Python
        -- a rollback entry declaring `safe: "false"` (a quoted string, an
        easy JSON-formatting slip for a model) used to silently mean the
        opposite of what was declared."""
        with pytest.raises(TaskSpecError, match="'safe' must be true or false"):
            TaskSpec.validate(self._base([{"run": "true", "safe": "false"}]))


class TestTaskSpecStepValidation:
    """Found by an independent audit of tasks.py: `touches`/`watch_roots`/
    `estimated_seconds` had no type validation at all in `_validate_step`,
    unlike every other field. `to_steps()`'s `[str(t) for t in value]` on a
    bare string doesn't raise -- Python iterates a string character by
    character -- so `touches: /etc/hosts` (a plausible missing-brackets
    slip) silently became `['/', 'e', 't', 'c', ...]`, and `to_steps()`'s
    unguarded `float(...)` on `estimated_seconds` raised well past
    Harness.plan()'s narrow except clause instead of here, skipping every
    decision-log/audit/telemetry call that clause's catch normally reaches.
    """

    def _base(self, **step_extra) -> dict:
        return {"task_id": "t", "request": "r",
               "steps": [{"id": "s1", "run": "true", **step_extra}]}

    def test_a_bare_string_touches_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="'touches' must be a list"):
            TaskSpec.validate(self._base(touches="/etc/hosts"))

    def test_a_bare_string_watch_roots_is_rejected(self) -> None:
        spec = self._base()
        spec["watch_roots"] = "/etc"
        with pytest.raises(TaskSpecError, match="'watch_roots' must be a list"):
            TaskSpec.validate(spec)

    def test_a_non_numeric_estimated_seconds_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="'estimated_seconds' must be a number"):
            TaskSpec.validate(self._base(estimated_seconds="soon"))

    def test_a_well_formed_step_still_validates_and_builds_cleanly(self) -> None:
        spec = TaskSpec.validate(self._base(touches=["/etc/hosts"], estimated_seconds=5))
        steps = spec.to_steps()
        assert steps[0].touches == ["/etc/hosts"]
        assert steps[0].estimated_seconds == 5.0

    def test_an_absent_touches_and_estimated_seconds_is_fine(self) -> None:
        spec = TaskSpec.validate(self._base())
        steps = spec.to_steps()
        assert steps[0].touches == []

    def test_a_dry_run_step_describes_instead_of_running(self) -> None:
        """`to_steps(dry_run=True)`'s own closure branch -- distinct from
        Harness._run_one_step's separate runtime dry-run short-circuit,
        which never calls Step.run at all and so never reaches this. The
        only way to exercise it is calling .run() directly on a step
        built this way, the way `harness plan`/`check` never currently do
        (Agent._prescreen builds with dry_run=True but only classifies
        step.command, never calls .run())."""
        spec = TaskSpec.validate(self._base())
        steps = spec.to_steps(dry_run=True)
        assert steps[0].run is not None
        assert steps[0].run() == "[dry-run] true"
        assert steps[0].estimated_seconds is None


class TestTaskSpecBasicValidation:
    """validate()'s own guard-clause cascade (non-mapping top level, missing
    keys, empty steps list, a non-mapping step, a step with no 'run', a
    duplicate step id) had zero direct test coverage -- only the touches/
    watch_roots/estimated_seconds/rollback-safe checks added this session
    were ever exercised. Also TaskSpec.load()'s two failure paths and
    to_yaml(), neither tested anywhere before this."""

    def test_a_non_mapping_top_level_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="must be a mapping"):
            # Deliberately the wrong shape -- exercising validate()'s own
            # top-level mapping check.
            TaskSpec.validate(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_a_missing_required_key_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="missing required key"):
            TaskSpec.validate({"task_id": "t", "steps": [{"id": "s", "run": "true"}]})

    def test_an_empty_steps_list_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="non-empty list"):
            TaskSpec.validate({"task_id": "t", "request": "r", "steps": []})

    def test_a_non_mapping_step_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="must be a mapping"):
            TaskSpec.validate({"task_id": "t", "request": "r", "steps": ["just a string"]})

    def test_a_step_with_no_run_command_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="no 'run' command"):
            TaskSpec.validate({"task_id": "t", "request": "r",
                              "steps": [{"id": "s", "description": "typo, no run"}]})

    def test_a_duplicate_step_id_is_rejected(self) -> None:
        with pytest.raises(TaskSpecError, match="duplicate step id"):
            TaskSpec.validate({"task_id": "t", "request": "r", "steps": [
                {"id": "s", "run": "true"}, {"id": "s", "run": "false"}]})

    def test_load_of_a_missing_file_is_rejected(self, tmp_path) -> None:
        with pytest.raises(TaskSpecError, match="task file not found"):
            TaskSpec.load(tmp_path / "no-such-file.yaml")

    def test_load_of_unreadable_yaml_is_rejected(self, tmp_path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("key: [unterminated", encoding="utf-8")
        with pytest.raises(TaskSpecError, match="unreadable YAML"):
            TaskSpec.load(bad)

    def test_to_yaml_round_trips_through_load(self, tmp_path) -> None:
        spec = TaskSpec.validate({
            "task_id": "t", "request": "r", "watch_roots": ["/etc"],
            "steps": [{"id": "s", "run": "true"}],
            "rollback": [{"run": "echo undo"}]})
        out = tmp_path / "roundtrip.yaml"
        out.write_text(spec.to_yaml(), encoding="utf-8")

        reloaded = TaskSpec.load(out)

        assert reloaded.task_id == "t"
        assert reloaded.watch_roots == ["/etc"]
        assert reloaded.steps == spec.steps
        assert reloaded.rollback == spec.rollback

    def test_to_yaml_omits_empty_watch_roots_and_rollback(self) -> None:
        spec = TaskSpec.validate({"task_id": "t", "request": "r",
                                  "steps": [{"id": "s", "run": "true"}]})
        rendered = spec.to_yaml()
        assert "watch_roots" not in rendered
        assert "rollback" not in rendered

    def test_rollback_actions_defensively_skips_an_invalid_entry(self) -> None:
        """validate() already rejects this at construction time -- this
        guards a TaskSpec built directly, bypassing validate()."""
        spec = TaskSpec(task_id="t", request="r",
                        steps=[{"id": "s", "run": "true"}],
                        # Deliberately the wrong shape in the first entry --
                        # exercising the defensive skip, not a real caller.
                        rollback=["not a mapping",  # type: ignore[list-item]
                                 {"description": "no run key"}])
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

    def test_fits_is_always_false_on_cpu_regardless_of_model_size(self) -> None:
        acc = Accelerator(kind="cpu")
        assert acc.fits("qwen3:8b") is False

    def test_fits_defaults_an_unknown_model_size_to_fitting_anywhere(self) -> None:
        acc = Accelerator(kind="cuda", free_vram_mb=100)
        assert acc.fits("some-totally-unrecognized-model-xyz") is True

    def test_fits_compares_a_known_size_against_free_vram(self) -> None:
        acc = Accelerator(kind="cuda", free_vram_mb=100)
        assert acc.fits("qwen3:32b") is False, "32b needs far more than 100 MB free"
        roomy = Accelerator(kind="cuda", free_vram_mb=100_000)
        assert roomy.fits("qwen3:8b") is True

    def test_to_dict_excludes_model_params_b_but_includes_gpu(self) -> None:
        acc = Accelerator(kind="cuda", name="RTX 4090", vram_mb=24564, free_vram_mb=23800,
                          model_params_b={"qwen3:8b": 8.0})
        d = acc.to_dict()
        assert "model_params_b" not in d
        assert d["gpu"] is True
        assert d["name"] == "RTX 4090"

    def test_render_reports_gpu_name_and_free_vram(self) -> None:
        acc = Accelerator(kind="cuda", name="RTX 4090", vram_mb=24564, free_vram_mb=23800)
        assert "RTX 4090" in acc.render()
        assert "23800 MB free of 24564 MB" in acc.render()


class TestHashFileSkipsNonRegularFiles:
    """ConflictDetector.hash_file(): found by an independent audit,
    reproduced live -- opening a FIFO for reading with no writer blocks
    forever. `baseline()` runs during Harness's GATE stage, before any
    operator ever sees the plan, so a single stray FIFO at a model-declared
    `touches` path used to wedge the whole harness, with no timeout
    anywhere in the call chain."""

    def test_a_fifo_returns_none_instead_of_hanging(self, tmp_path) -> None:
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        with hang_guard():
            assert ConflictDetector().hash_file(fifo) is None

    def test_a_symlink_to_a_fifo_also_returns_none_instead_of_hanging(
            self, tmp_path) -> None:
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        link = tmp_path / "link_to_pipe"
        link.symlink_to(fifo)
        with hang_guard():
            assert ConflictDetector().hash_file(link) is None

    def test_a_regular_file_is_still_hashed_normally(self, tmp_path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("hello", encoding="utf-8")
        assert ConflictDetector().hash_file(target) is not None

    def test_a_symlink_to_a_regular_file_is_still_hashed(self, tmp_path) -> None:
        target = tmp_path / "real.txt"
        target.write_text("hello", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        assert ConflictDetector().hash_file(link) == ConflictDetector().hash_file(target)

    def test_a_missing_file_still_returns_none(self, tmp_path) -> None:
        assert ConflictDetector().hash_file(tmp_path / "nope") is None


class TestConflictDetection:
    """ConflictDetector's core classification logic -- feeds
    Harness._run_steps()'s external-conflict check directly, which decides
    whether a real run gets BLOCKED, but had no dedicated test coverage for
    almost any of its branches beyond the inotify fast path's stale-conflict
    fix (TestConflictDetectorFastPath, below)."""

    # -- _detect_declared(): explicitly tracked paths ------------------- #

    def test_a_declared_path_created_after_baseline_is_reported(self, tmp_path) -> None:
        target = tmp_path / "new.txt"
        detector = ConflictDetector()
        detector.baseline([str(target)])  # doesn't exist yet
        target.write_text("appeared", encoding="utf-8")

        found = detector._detect_declared()

        assert len(found) == 1
        assert found[0].kind is ConflictKind.CREATED
        assert found[0].baseline_hash is None
        assert found[0].current_hash is not None

    def test_a_declared_path_deleted_after_baseline_is_reported(self, tmp_path) -> None:
        target = tmp_path / "gone.txt"
        target.write_text("here", encoding="utf-8")
        detector = ConflictDetector()
        detector.baseline([str(target)])
        target.unlink()

        found = detector._detect_declared()

        assert len(found) == 1
        assert found[0].kind is ConflictKind.DELETED
        assert found[0].current_hash is None

    def test_a_declared_path_that_becomes_unreadable_is_reported(self, tmp_path) -> None:
        target = tmp_path / "locked.txt"
        target.write_text("secret", encoding="utf-8")
        detector = ConflictDetector()
        detector.baseline([str(target)])
        target.chmod(0o000)
        try:
            found = detector._detect_declared()
        finally:
            target.chmod(0o644)  # restore, or tmp_path cleanup can't remove it

        assert len(found) == 1
        assert found[0].kind is ConflictKind.UNREADABLE

    def test_a_declared_path_content_change_is_reported(self, tmp_path) -> None:
        target = tmp_path / "app.conf"
        target.write_text("original", encoding="utf-8")
        detector = ConflictDetector()
        detector.baseline([str(target)])
        target.write_text("modified", encoding="utf-8")

        found = detector._detect_declared()

        assert len(found) == 1
        assert found[0].kind is ConflictKind.MODIFIED

    def test_an_unchanged_declared_path_reports_nothing(self, tmp_path) -> None:
        target = tmp_path / "stable.txt"
        target.write_text("same", encoding="utf-8")
        detector = ConflictDetector()
        detector.baseline([str(target)])

        assert detector._detect_declared() == []

    # -- _reconcile(): the full-walk source of truth --------------------- #

    def test_reconcile_reports_a_new_file_appearing_in_the_tree(self, tmp_path) -> None:
        detector = ConflictDetector(roots=[tmp_path], use_inotify=False)
        detector.baseline([], roots=[tmp_path])
        (tmp_path / "new.txt").write_text("surprise", encoding="utf-8")

        found = detector._reconcile()

        assert len(found) == 1
        assert found[0].kind is ConflictKind.CREATED

    def test_reconcile_reports_a_file_removed_from_the_tree(self, tmp_path) -> None:
        target = tmp_path / "gone.txt"
        target.write_text("here", encoding="utf-8")
        detector = ConflictDetector(roots=[tmp_path], use_inotify=False)
        detector.baseline([], roots=[tmp_path])
        target.unlink()

        found = detector._reconcile()

        assert len(found) == 1
        assert found[0].kind is ConflictKind.DELETED

    def test_reconcile_reports_a_content_change_via_size_or_mtime(self, tmp_path) -> None:
        target = tmp_path / "app.conf"
        target.write_text("original", encoding="utf-8")
        detector = ConflictDetector(roots=[tmp_path], use_inotify=False)
        detector.baseline([str(target)], roots=[tmp_path])
        target.write_text("modified content, a different size", encoding="utf-8")

        found = detector._reconcile()

        assert len(found) == 1
        assert found[0].kind is ConflictKind.MODIFIED

    def test_reconcile_reports_nothing_for_an_unchanged_tree(self, tmp_path) -> None:
        (tmp_path / "stable.txt").write_text("same", encoding="utf-8")
        detector = ConflictDetector(roots=[tmp_path], use_inotify=False)
        detector.baseline([], roots=[tmp_path])

        assert detector._reconcile() == []

    # -- _classify(): the inotify fast path's per-event classifier ------ #

    def test_classify_reports_a_new_path_not_in_fingerprints(self, tmp_path) -> None:
        target = tmp_path / "new.txt"
        target.write_text("surprise", encoding="utf-8")
        detector = ConflictDetector()

        conflict = detector._classify(str(target))

        assert conflict is not None
        assert conflict.kind is ConflictKind.CREATED

    def test_classify_reports_a_known_path_that_disappeared(self, tmp_path) -> None:
        target = tmp_path / "gone.txt"
        detector = ConflictDetector()
        detector.fingerprints[str(target)] = (0, 0)

        conflict = detector._classify(str(target))

        assert conflict is not None
        assert conflict.kind is ConflictKind.DELETED

    def test_classify_returns_none_for_an_untracked_missing_path(self, tmp_path) -> None:
        detector = ConflictDetector()
        assert detector._classify(str(tmp_path / "never-existed.txt")) is None

    def test_classify_returns_none_when_fingerprint_still_matches(self, tmp_path) -> None:
        target = tmp_path / "stable.txt"
        target.write_text("same", encoding="utf-8")
        detector = ConflictDetector()
        st = target.stat()
        detector.fingerprints[str(target)] = (st.st_size, st.st_mtime_ns)

        assert detector._classify(str(target)) is None

    def test_classify_returns_none_when_fingerprint_changed_but_hash_did_not(
            self, tmp_path) -> None:
        """A same-content rewrite (an editor's atomic-save round-trip
        landing on identical bytes) changes mtime but not content -- the
        hash layer exists precisely so this doesn't report as a conflict."""
        target = tmp_path / "app.conf"
        target.write_text("content", encoding="utf-8")
        detector = ConflictDetector()
        detector.hashes[str(target)] = detector.hash_file(target)
        detector.fingerprints[str(target)] = (999999, 0)  # force a fingerprint mismatch
        target.write_text("content", encoding="utf-8")  # rewritten, same bytes

        assert detector._classify(str(target)) is None

    def test_classify_reports_a_real_content_change(self, tmp_path) -> None:
        target = tmp_path / "app.conf"
        target.write_text("original", encoding="utf-8")
        detector = ConflictDetector()
        detector.hashes[str(target)] = detector.hash_file(target)
        st = target.stat()
        detector.fingerprints[str(target)] = (st.st_size, st.st_mtime_ns)
        target.write_text("changed content here", encoding="utf-8")

        conflict = detector._classify(str(target))

        assert conflict is not None
        assert conflict.kind is ConflictKind.MODIFIED

    # -- detect(): declared + tree layers merged ------------------------- #

    def test_detect_dedups_a_path_flagged_by_both_declared_and_tree_layers(
            self, tmp_path) -> None:
        target = tmp_path / "app.conf"
        target.write_text("original", encoding="utf-8")
        detector = ConflictDetector(roots=[tmp_path], use_inotify=False)
        detector.baseline([str(target)], roots=[tmp_path])
        target.write_text("modified", encoding="utf-8")

        found = detector.detect()

        # Both layers would independently notice this same change --
        # detect() must report it once, not twice.
        matching = [c for c in found if c.path == str(target)]
        assert len(matching) == 1

    # -- edges: unreadable files, oversized trees, missing roots -------- #

    def test_hash_file_records_an_error_when_read_fails_mid_file(self, tmp_path) -> None:
        target = tmp_path / "locked.txt"
        target.write_text("secret", encoding="utf-8")
        target.chmod(0o000)
        detector = ConflictDetector()
        try:
            result = detector.hash_file(target)
        finally:
            target.chmod(0o644)

        assert result is None
        assert detector.errors

    def test_walk_stops_and_records_an_error_past_max_files(self, tmp_path) -> None:
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        detector = ConflictDetector(max_files=2)

        fingerprints = detector._walk(tmp_path)

        assert len(fingerprints) <= 2
        assert any("max_files" in e for e in detector.errors)

    def test_walk_on_a_missing_root_returns_empty_not_an_error(self, tmp_path) -> None:
        detector = ConflictDetector()
        assert detector._walk(tmp_path / "does-not-exist") == {}
        assert detector.errors == []

    def test_refresh_re_baselines_so_the_harnesss_own_write_is_not_a_conflict(
            self, tmp_path) -> None:
        target = tmp_path / "app.conf"
        target.write_text("v1", encoding="utf-8")
        detector = ConflictDetector(roots=[tmp_path], use_inotify=False)
        detector.baseline([str(target)], roots=[tmp_path])
        target.write_text("v2, written by the harness itself", encoding="utf-8")

        detector.refresh([str(target)])

        assert detector._detect_declared() == []

    def test_close_is_a_no_op_when_inotify_was_never_started(self) -> None:
        detector = ConflictDetector(use_inotify=False)
        detector.close()  # must not raise
        assert detector._inotify is None

    def test_conflict_to_dict_serializes_the_kind_as_its_value(self) -> None:
        conflict = Conflict("/tmp/x", ConflictKind.MODIFIED, "before", "after", "changed")
        assert conflict.to_dict()["kind"] == "modified"

    def test_walk_records_an_error_on_a_scandir_failure_and_keeps_going(
            self, tmp_path: Path) -> None:
        locked = tmp_path / "locked-dir"
        locked.mkdir()
        (locked / "inside.txt").write_text("x", encoding="utf-8")
        (tmp_path / "readable.txt").write_text("y", encoding="utf-8")
        locked.chmod(0o000)
        detector = ConflictDetector()
        try:
            fingerprints = detector._walk(tmp_path)
        finally:
            locked.chmod(0o755)

        assert any(str(tmp_path / "readable.txt") == p for p in fingerprints)
        assert any(str(locked) in e for e in detector.errors)

    def test_classify_records_a_conflict_when_stat_fails_after_a_known_path_exists(
            self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "locked.txt"
        target.write_text("x", encoding="utf-8")
        detector = ConflictDetector()
        detector.fingerprints[str(target)] = (0, 0)  # known, forces past exists()
        real_stat = Path.stat
        calls = {"n": 0}

        def selective_broken_stat(self, *args, **kwargs):
            # target.exists() (called first, inside _classify) also goes
            # through Path.stat -- only the SECOND call (_classify's own
            # explicit `target.stat()`) is the one under test, so the first
            # call must still succeed or exists() itself raises.
            if self == target:
                calls["n"] += 1
                if calls["n"] > 1:
                    raise OSError("simulated stat failure")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", selective_broken_stat)
        conflict = detector._classify(str(target))

        assert conflict is not None
        assert conflict.kind is ConflictKind.UNREADABLE

    def test_close_swallows_an_error_from_the_real_inotify_object(self) -> None:
        class BrokenInotify:
            def close(self):
                raise OSError("simulated close failure")

        detector = ConflictDetector(use_inotify=False)
        detector._inotify = BrokenInotify()
        detector.close()  # must not raise
        assert detector._inotify is None

    def test_walk_skips_a_non_regular_non_directory_entry(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("x", encoding="utf-8")
        os.mkfifo(tmp_path / "a.fifo")
        detector = ConflictDetector()

        fingerprints = detector._walk(tmp_path)

        assert str(tmp_path / "real.txt") in fingerprints
        assert not any("fifo" in p for p in fingerprints)

    def test_walk_records_no_error_when_a_single_entry_stat_fails(
            self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "real.txt").write_text("x", encoding="utf-8")

        class BrokenEntry:
            name = "broken.txt"
            path = str(tmp_path / "broken.txt")

            def is_dir(self, follow_symlinks=False):
                return False

            def is_file(self, follow_symlinks=False):
                return True

            def stat(self, follow_symlinks=False):
                raise OSError("simulated per-entry stat failure")

        real_scandir = os.scandir

        @contextlib.contextmanager
        def scandir_with_one_broken_entry(path):
            with real_scandir(path) as it:
                yield [*it, BrokenEntry()]

        monkeypatch.setattr(os, "scandir", scandir_with_one_broken_entry)
        detector = ConflictDetector()

        fingerprints = detector._walk(tmp_path)

        assert str(tmp_path / "real.txt") in fingerprints
        assert "broken.txt" not in str(fingerprints)
        assert not detector.errors  # a per-entry OSError is swallowed, not recorded

    def test_start_inotify_degrades_when_add_watch_fails(
            self, tmp_path: Path, monkeypatch) -> None:
        from inotify_simple import INotify

        def broken_add_watch(self, path, mask):
            raise OSError("simulated add_watch failure")

        monkeypatch.setattr(INotify, "add_watch", broken_add_watch)
        detector = ConflictDetector(roots=[tmp_path], use_inotify=True)
        try:
            detector._start_inotify()
            assert detector.use_inotify is False
            assert detector._inotify is None
            assert any("inotify setup failed" in e for e in detector.errors)
        finally:
            detector.close()

    def test_events_records_an_error_and_returns_none_when_read_fails(
            self, tmp_path: Path) -> None:
        detector = ConflictDetector(roots=[tmp_path], use_inotify=True)
        try:
            detector._start_inotify()

            def broken_read(timeout=0):
                raise OSError("simulated read failure")

            detector._inotify.read = broken_read
            assert detector._events() is None
            assert any("inotify read failed" in e for e in detector.errors)
        finally:
            detector.close()

    def test_stats_reports_fast_checks_and_full_walks(self) -> None:
        detector = ConflictDetector(use_inotify=False)
        detector.fast_checks = 3
        detector.full_walks = 1
        detector.errors.append("oops")

        stats = detector.stats()

        assert stats["fast_checks"] == 3
        assert stats["full_walks"] == 1
        assert stats["errors"] == ["oops"]


class TestConflictDetectorRealInotify:
    """_start_inotify()/_events() against the real inotify_simple library --
    every other test in this file injects a truthy stand-in for
    `_inotify` and a fake `_events`, so nothing exercised the actual OS
    integration `_detect_tree()`'s fast path depends on in production."""

    def test_start_inotify_attaches_a_real_watch(self, tmp_path: Path) -> None:
        detector = ConflictDetector(roots=[tmp_path], use_inotify=True)
        try:
            detector._start_inotify()
            assert detector.use_inotify is True
            assert detector._inotify is not None
            assert detector._watch_paths
        finally:
            detector.close()

    def test_events_reports_a_real_file_creation(self, tmp_path: Path) -> None:
        detector = ConflictDetector(roots=[tmp_path], use_inotify=True)
        try:
            detector._start_inotify()
            (tmp_path / "new.txt").write_text("surprise", encoding="utf-8")
            # inotify delivery is asynchronous; a short real wait is the
            # only reliable way to observe it without mocking the library
            # this test exists specifically to exercise.
            import time as time_module
            deadline = time_module.time() + 2.0
            touched: set[str] = set()
            while time_module.time() < deadline and not touched:
                touched = detector._events() or set()
            assert any("new.txt" in p for p in touched)
        finally:
            detector.close()

    def test_events_returns_none_when_inotify_was_never_started(self) -> None:
        detector = ConflictDetector(use_inotify=False)
        assert detector._events() is None

    def test_start_inotify_degrades_when_the_library_is_unavailable(
            self, tmp_path: Path, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "inotify_simple":
                raise ImportError("simulated: inotify_simple not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", broken_import)
        detector = ConflictDetector(roots=[tmp_path], use_inotify=True)
        detector._start_inotify()

        assert detector.use_inotify is False
        assert any("inotify unavailable" in e for e in detector.errors)


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
        detector = ConflictDetector(roots=[tmp_path], use_inotify=True)
        # Found while raising this file's test coverage: `use_inotify=False`
        # here made `_detect_tree()`'s own guard (`if not self.use_inotify
        # or not self._inotify or ...: return self._reconcile()`) always
        # take the full-walk branch instead -- `_reconcile()` rebuilds
        # `_known` from scratch every call, so the stale-conflict bug this
        # class exists to guard against literally cannot reproduce there.
        # Every test below passed, but not because the fix held; they never
        # reached the code the fix is in. `use_inotify=True` plus stubbing
        # `_start_inotify` (so `baseline()` doesn't open a real inotify fd
        # in a test) is what actually routes through the event-based path.
        detector._start_inotify = lambda: None
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


class TestShellOutputSink:
    """`shell_output_sink`/`shell()`'s live-streaming path: real subprocess
    tests throughout, same discipline as TestShellPipefail above -- this is
    the actual mechanism `Harness.execute()` uses to give the TUI live step
    output, so its pipefail/timeout/exit-code contract has to match the
    default path exactly, not just "look similar"."""

    def test_without_a_sink_shell_is_unaffected(self) -> None:
        """The overwhelming default case: no sink active anywhere, `shell()`
        behaves exactly as it always has (this is also implicitly covered
        by TestShellPipefail running with no sink, but stated directly)."""
        assert shell("echo hello") == "hello"

    def test_a_sink_receives_output_as_it_runs_not_only_at_the_end(self) -> None:
        received: list[str] = []
        with shell_output_sink(received.append):
            result = shell("echo one; echo two; echo three")
        assert result == "one\ntwo\nthree"
        assert "".join(received) == "one\ntwo\nthree\n"

    def test_the_sink_is_scoped_to_the_with_block(self) -> None:
        received: list[str] = []
        with shell_output_sink(received.append):
            shell("echo inside")
        shell("echo outside")  # no sink active -- must not append here
        assert received == ["inside\n"]

    def test_a_none_sink_is_a_harmless_no_op(self) -> None:
        with shell_output_sink(None):
            assert shell("echo hello") == "hello"

    def test_success_return_value_matches_the_non_streaming_path(self) -> None:
        assert shell("printf 'a\\nb\\n'") == shell("printf 'a\\nb\\n'")
        received: list[str] = []
        with shell_output_sink(received.append):
            streamed = shell("printf 'a\\nb\\n'")
        assert streamed == "a\nb"

    def test_a_failure_raises_with_the_same_shape_as_the_default_path(self) -> None:
        with pytest.raises(RuntimeError, match="exit 1"):
            with shell_output_sink(lambda chunk: None):
                shell("echo boom >&2; exit 1")

    def test_stderr_is_preferred_in_the_failure_message_same_as_default(self) -> None:
        received: list[str] = []
        with pytest.raises(RuntimeError, match="from stderr"):
            with shell_output_sink(received.append):
                shell("echo from stdout; echo from stderr >&2; exit 3")
        # Both streams still reached the live callback -- only the raised
        # message prefers stderr, matching (stderr or stdout) in the
        # default path.
        assert any("from stdout" in c for c in received)
        assert any("from stderr" in c for c in received)

    def test_stdout_and_stderr_stay_separate_on_success(self) -> None:
        """The streaming path merges both into one queue for live display,
        but must not merge them into the *returned* value -- StepResult.output
        must mean the same thing whether or not a live consumer was watching."""
        received: list[str] = []
        with shell_output_sink(received.append):
            result = shell("echo out-only; echo err-only >&2")
        assert result == "out-only"  # stderr never in the success return value
        assert any("err-only" in c for c in received)  # but still streamed live

    def test_pipefail_is_still_enforced_while_streaming(self) -> None:
        received: list[str] = []
        with pytest.raises(RuntimeError, match="exit 1"):
            with shell_output_sink(received.append):
                shell("false | cat")

    def test_a_hung_process_is_killed_on_timeout(self) -> None:
        received: list[str] = []
        with pytest.raises(subprocess.TimeoutExpired):
            with shell_output_sink(received.append):
                shell("sleep 5", timeout=0.3)

    def test_output_genuinely_streams_instead_of_arriving_all_at_once(self) -> None:
        """The whole point of this feature: the first chunk must land well
        before the command finishes, not just before shell() returns (which
        would be true even of a fully-buffered implementation)."""
        import time

        arrivals: list[float] = []
        start = time.monotonic()
        with shell_output_sink(lambda chunk: arrivals.append(time.monotonic() - start)):
            shell("echo first; sleep 0.5; echo second")
        assert len(arrivals) == 2
        assert arrivals[0] < 0.25, \
            f"first chunk arrived at {arrivals[0]:.2f}s, not live if that's near the 0.5s total"
        assert arrivals[1] >= 0.4

    def test_nested_calls_restore_the_previous_sink(self) -> None:
        """Defensive, not a realistic call pattern today (nothing nests
        shell_output_sink calls) -- proves the save/restore is correct
        rather than a blind overwrite, in case that ever changes."""
        outer: list[str] = []
        inner: list[str] = []
        with shell_output_sink(outer.append):
            with shell_output_sink(inner.append):
                shell("echo nested")
            shell("echo outer-again")
        assert inner == ["nested\n"]
        assert outer == ["outer-again\n"]


class TestContextCompressor:
    """`ContextCompressor` had zero dedicated unit tests before this class --
    the only place it was ever exercised was one incidental assertion in
    test_agent.py (`agent.context.compressions >= 1`). It shows up as
    "covered" in the defensive-coding scorecard's Testing dimension only
    because some test imports the `harness.core` package for something
    else entirely -- that check is file-level, not behavioral. Nothing
    anywhere actually verified the net-saving guarantee, the terser-fallback
    degradation, `keep_recent`, or `ContextBudgetError`, despite
    AUDIT_FINDINGS.md's "Things checked and found correct" list claiming
    the net-saving guard was verified. It wasn't -- not by a test, anyway.
    """

    def test_a_single_entry_over_budget_is_rejected_loudly(self) -> None:
        c = ContextCompressor(max_tokens=10)
        with pytest.raises(ContextBudgetError):
            c.add("u1", "user", "x" * 1000)

    def test_an_out_of_range_trigger_ratio_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="trigger_ratio must be in"):
            ContextCompressor(trigger_ratio=1.5)

    def test_context_step_and_compression_result_to_dict_are_serializable(self) -> None:
        c = ContextCompressor(max_tokens=1000)
        step = c.add("u1", "user", "hello")
        assert step.to_dict() == {
            "entry_id": "u1", "role": "user", "content": "hello",
            "tokens": step.tokens, "compressed": False}

        result = c.compress(force=True)
        assert result.to_dict()["triggered"] is False

    def test_the_cut_search_stops_as_soon_as_the_target_is_met(self) -> None:
        """Compressing more than needed would throw away recoverable
        detail for no benefit -- the search must stop at the smallest
        `cut` that already gets under target, not walk all the way to
        `compressible` every time."""
        c = ContextCompressor(max_tokens=1000, trigger_ratio=0.5, target_ratio=0.5,
                              keep_recent=0)
        for i in range(6):
            c.add(f"e{i}", "user",
                 f"entry number {i} has a decent amount of real content padding it out")

        result = c.compress(force=True)

        assert result.triggered is True
        assert c.entries[0].entry_id == "summary::e0..e0", (
            "the search should have stopped after folding just the first entry"
        )

    def test_force_with_too_few_entries_to_touch_keep_recent_is_a_no_op(self) -> None:
        """`force=True` still must not fold entries the caller asked to
        always keep -- with fewer entries than keep_recent, there is
        nothing left that is even eligible to compress."""
        c = ContextCompressor(max_tokens=1000, keep_recent=5)
        c.add("u1", "user", "hello")

        result = c.compress(force=True)

        assert result.triggered is False
        assert len(c.entries) == 1

    def test_compression_does_not_trigger_below_the_ratio(self) -> None:
        c = ContextCompressor(max_tokens=1000, trigger_ratio=0.9, keep_recent=1)
        c.add("u1", "user", "hi")
        result = c.maybe_compress()
        assert result.triggered is False
        assert len(c.entries) == 1

    def test_keep_recent_entries_are_never_folded_into_the_summary(self) -> None:
        c = ContextCompressor(max_tokens=40, trigger_ratio=0.5, target_ratio=0.1,
                              keep_recent=2)
        for i in range(10):
            c.add(f"e{i}", "user", f"entry number {i} has some real content in it")
        result = c.compress(force=True)

        assert result.triggered is True
        assert [e.entry_id for e in c.entries[-2:]] == ["e8", "e9"]

    def test_compression_never_increases_total_tokens(self) -> None:
        """The one guarantee the class's own docstring makes ("guaranteeing
        a net saving") -- checked across every compress() call in a long,
        realistic-shaped run, not just the first one."""
        c = ContextCompressor(max_tokens=300, trigger_ratio=0.85, keep_recent=3)
        for i in range(500):
            before = c.total_tokens
            c.add(f"u{i}", "user", f"request number {i} about something specific")
            c.add(f"a{i}", "assistant", f"done with request {i}, result was fine")
            result = c.maybe_compress()
            if result.triggered:
                assert result.tokens_after <= result.tokens_before
                assert c.total_tokens == result.tokens_after
            assert c.total_tokens <= max(before, c.max_tokens) + 50

    def test_a_net_loss_summary_is_skipped_not_applied(self) -> None:
        """Many entries too tiny for even the terser fallback to beat --
        compression must decline rather than make the window bigger."""
        c = ContextCompressor(max_tokens=1000, trigger_ratio=0.01, target_ratio=0.01,
                              keep_recent=0)
        c.add("u1", "user", "a")
        before_entries = list(c.entries)

        result = c.compress(force=True)

        assert result.triggered is False
        assert "skipped" in result.summary
        assert c.entries == before_entries
        assert c.compressions == 0

    def test_force_compresses_even_under_the_trigger_ratio(self) -> None:
        c = ContextCompressor(max_tokens=1000, trigger_ratio=0.99, target_ratio=0.02,
                              keep_recent=0)
        for i in range(20):
            c.add(f"e{i}", "user", f"entry {i} with a bit of real content in it")

        assert c.maybe_compress().triggered is False
        assert c.compress(force=True).triggered is True

    def test_as_messages_round_trips_role_and_content(self) -> None:
        c = ContextCompressor(max_tokens=1000)
        c.add("u1", "user", "hello")
        c.add("a1", "assistant", "hi there")
        assert c.as_messages() == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
