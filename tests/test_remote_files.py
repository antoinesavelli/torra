"""Remote pull and push move exact bytes, name what they touch, and judge the real destination.

Depends on: torra.core (TaskSpec), torra.core.remote_edit, torra.gates (RiskLevel, SafetyClassifier).
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from torra.core import TaskSpec
from torra.core.remote_edit import settle_remote_pull
from torra.gates import RiskLevel, SafetyClassifier


def _spec(kind: str, **fields) -> TaskSpec:
    return TaskSpec.validate({"task_id": "t", "request": "r", "steps": [
        {"id": "s", "kind": kind, "run": "ignored", "remote_path": "/etc/app.conf", **fields}]})


def test_a_push_uploads_the_local_file_byte_for_byte(tmp_path: Path) -> None:
    """`read_text` translated CRLF and lone CR to LF and raised on non-UTF-8 content."""
    local = tmp_path / "app.conf"
    original = b"a\r\nb\rc\r\n\xff\xfe tail"
    local.write_bytes(original)
    sent: list[str] = []
    step = _spec("remote_push", reads=[str(local)]).to_steps(
        executor_runner=lambda cmd, cwd, timeout: sent.append(cmd) or "")[0]

    assert step.run is not None
    step.run()

    encoded = sent[0].split("echo ", 1)[1].split(" |", 1)[0].strip("'")
    assert base64.b64decode(encoded) == original


def test_the_push_text_names_the_local_file_that_is_uploaded(tmp_path: Path) -> None:
    secret = tmp_path / "id_rsa"
    step = _spec("remote_push", reads=[str(secret)]).to_steps(executor_runner=None)[0]

    assert str(secret) in step.command and "/etc/app.conf" in step.command


def test_a_pull_refuses_output_that_is_not_pure_base64(tmp_path: Path) -> None:
    """A login banner on stdout used to be decoded into the file."""
    dest = tmp_path / "pulled"
    noisy = "Welcome to the box\n" + base64.b64encode(b"real").decode()
    step = _spec("remote_pull", touches=[str(dest)]).to_steps(
        executor_runner=lambda cmd, cwd, timeout: noisy)[0]

    assert step.run is not None
    with pytest.raises(Exception, match="(?i)base64|padding|non-base64"):
        step.run()
    assert not dest.exists()


@pytest.mark.parametrize("destination", ["/tmp/../etc/app.conf", "//etc/app.conf", "/./etc/app.conf",
                                         "/etc/./app.conf", "~/.bashrc"])
def test_a_pull_is_judged_by_where_its_destination_really_is(destination: str) -> None:
    step = SimpleNamespace(kind="remote_pull", touches=[destination])
    verdict = SimpleNamespace(level=RiskLevel.SAFE, reasons=[])

    settle_remote_pull(verdict, step, SafetyClassifier().protects)

    assert verdict.level is RiskLevel.DANGEROUS, destination


def test_a_pull_into_a_scratch_directory_stays_a_bounded_write() -> None:
    step = SimpleNamespace(kind="remote_pull", touches=["/tmp/work/app.conf"])
    verdict = SimpleNamespace(level=RiskLevel.SAFE, reasons=[])

    settle_remote_pull(verdict, step, SafetyClassifier().protects)

    assert verdict.level is RiskLevel.CAUTION
