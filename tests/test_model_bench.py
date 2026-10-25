"""scripts/dev/model_bench as a fucone job: endpoint from TORRA_OLLAMA_HOST, an Ollama outage aborts, results go to FUCONE_RUN_DIR."""

from __future__ import annotations

import importlib
import json
import sys
import urllib.error
from collections.abc import Iterator
from email.message import Message
from pathlib import Path

import pytest

from torra.agent import AgentError
from torra.core import OllamaError

BENCH_DIR = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "model_bench"
sys.path.insert(0, str(BENCH_DIR))
bench = importlib.import_module("bench")
read_results = importlib.import_module("read_results")


def wrapped(cause: BaseException) -> OllamaError:
    """An OllamaError chained the way OllamaClient.chat raises it."""
    try:
        raise OllamaError("generation failed") from cause
    except OllamaError as exc:
        return exc


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/api/chat", code, "err", Message(), None)


@pytest.fixture
def restore_bench(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Re-import bench under the original env after a test reloads it under another."""
    yield
    monkeypatch.undo()
    importlib.reload(bench)


def test_ollama_host_defaults_to_local_daemon(
    monkeypatch: pytest.MonkeyPatch, restore_bench: None
) -> None:
    monkeypatch.delenv("TORRA_OLLAMA_HOST", raising=False)
    assert importlib.reload(bench).OLLAMA_HOST == "http://127.0.0.1:11434"


def test_ollama_host_follows_torra_ollama_host(
    monkeypatch: pytest.MonkeyPatch, restore_bench: None
) -> None:
    monkeypatch.setenv("TORRA_OLLAMA_HOST", "http://127.0.0.1:9")
    assert importlib.reload(bench).OLLAMA_HOST == "http://127.0.0.1:9"


@pytest.mark.parametrize(
    "cause",
    [
        http_error(503),
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        TimeoutError("timed out"),
    ],
)
def test_an_ollama_outage_is_an_infra_error(cause: BaseException) -> None:
    assert bench.is_infra_error(wrapped(cause))


@pytest.mark.parametrize(
    "exc",
    [
        wrapped(http_error(404)),
        OllamaError("devstral:24b returned an empty response"),
        AgentError("no JSON object in reply"),
    ],
)
def test_a_bad_model_answer_is_not_an_infra_error(exc: BaseException) -> None:
    assert not bench.is_infra_error(exc)


class RaisingAgent:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def converse(self, request: str, model: str | None = None) -> None:
        raise self.exc


def test_grade_task_aborts_on_an_outage() -> None:
    with pytest.raises(OllamaError):
        bench.grade_task(bench.TASKS[0], RaisingAgent(wrapped(http_error(503))), [], "m")


def test_grade_task_records_a_bad_answer_as_data() -> None:
    result = bench.grade_task(bench.TASKS[0], RaisingAgent(AgentError("bad")), [], "m")
    assert not result.ok and result.error == "AgentError: bad"


def test_results_go_to_fucone_run_dir_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / "repo" / "results.json"
    monkeypatch.setattr(bench, "RESULTS_PATH", default)
    monkeypatch.setenv("FUCONE_RUN_DIR", str(tmp_path))
    assert bench.write_results([{"a": 1}]) == tmp_path / "results.json"
    assert json.loads((tmp_path / "results.json").read_text()) == [{"a": 1}]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["results.json"]
    assert not default.exists()


def test_results_go_to_the_tracked_file_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench, "RESULTS_PATH", tmp_path / "results.json")
    monkeypatch.delenv("FUCONE_RUN_DIR", raising=False)
    assert bench.write_results([]) == tmp_path / "results.json"


def rows(models: list[str]) -> list[dict[str, object]]:
    return [
        {"model": m, "task_id": t.id, "ok": True, "intent_correct": True}
        for m in models
        for t in bench.TASKS
    ]


def test_reader_accepts_a_complete_battery() -> None:
    assert read_results.problem(rows(["a", "b"])) == ""


@pytest.mark.parametrize("bad", [[], {"a": 1}, rows(["a"])[:-1], [{"model": "a"}]])
def test_reader_refuses_an_unusable_result(bad: object) -> None:
    assert read_results.problem(bad)


def test_reader_exits_non_zero_without_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUCONE_RUN_DIR", str(tmp_path))
    assert read_results.main() == 1
    (tmp_path / "results.json").write_text(json.dumps(rows(["a"])))
    assert read_results.main() == 0
