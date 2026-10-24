"""The scorecard's Testing rule and Modularity nesting join grade what they claim to.

Depends on: scripts/dev/defensive_coding_report.py (loaded by path: scripts/ is not a package).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The scorecard module, pointed at a throwaway repo under tmp_path."""
    spec = importlib.util.spec_from_file_location(
        "defensive_coding_report", ROOT / "scripts" / "dev" / "defensive_coding_report.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # @dataclass looks its module up here
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "TORRA_ROOT", tmp_path / "torra")
    monkeypatch.setattr(module, "TESTS_ROOT", tmp_path / "tests")
    return module


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def test_testing_credits_a_reexport_but_not_the_bare_package(
        report: ModuleType, tmp_path: Path) -> None:
    """`from torra import cli` once covered every module in torra/, so the grade could not drop."""
    _write(tmp_path, {
        "torra/__init__.py": "",
        "torra/cli.py": "def main() -> None:\n    pass\n",
        "torra/gates/__init__.py": "from .safety import Classifier\n",
        "torra/gates/safety.py": "class Classifier:\n    pass\n",
        "torra/gates/state.py": "def check() -> None:\n    pass\n",
        "torra/gates/hidden.py": "def helper() -> None:\n    pass\n",
        "tests/test_x.py": ("from torra import cli\nfrom torra.gates import Classifier\n"
                            "from torra.gates import state\n"),
    })
    files = report.source_files()
    result = report.grade_testing(files, {f: report.tier_of(f) for f in files})
    assert result.offenders == ["torra/gates/hidden.py"]


def test_nesting_is_joined_by_def_line_not_by_name(report: ModuleType, tmp_path: Path) -> None:
    """A flat `run` defined after a tangled one once overwrote its depth, hiding the offender."""
    branches = "".join(f"                    if x == {i}:\n                        total += {i}\n"
                       for i in range(8))
    _write(tmp_path, {
        "torra/__init__.py": "",
        "torra/worker.py": (
            "class A:\n"
            "    def run(self, xs: list[int]) -> int:\n"
            "        total = 0\n"
            "        for x in xs:\n"
            "            if x:\n"
            "                while x > 0:\n"
            f"{branches}"
            "                    x -= 1\n"
            "        return total\n"
            "\n\n"
            "class B:\n"
            "    def run(self) -> int:\n"
            "        return 0\n"),
    })
    files = report.source_files()
    result = report.grade_modularity(files, {f: report.tier_of(f) for f in files})
    assert result.offenders == ["torra/worker.py:2 run (CCN 12, depth 4)"]
