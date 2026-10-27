"""The scorecard's Testing rule and Modularity nesting join score what they claim to.

Depends on: scripts/dev/defensive_coding_report.py (loaded by path: scripts/ is not a package).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The scorecard module, pointed at a throwaway repo under tmp_path."""
    spec = importlib.util.spec_from_file_location(
        "defensive_coding_report", ROOT / "scripts" / "dev" / "defensive_coding_report.py"
    )
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
    report: ModuleType, tmp_path: Path
) -> None:
    """`from torra import cli` once covered every module in torra/, so the band could not drop."""
    _write(
        tmp_path,
        {
            "torra/__init__.py": "",
            "torra/cli.py": "def main() -> None:\n    pass\n",
            "torra/gates/__init__.py": "from .safety import Classifier\n",
            "torra/gates/safety.py": "class Classifier:\n    pass\n",
            "torra/gates/state.py": "def check() -> None:\n    pass\n",
            "torra/gates/hidden.py": "def helper() -> None:\n    pass\n",
            "tests/test_x.py": (
                "from torra import cli\nfrom torra.gates import Classifier\n"
                "from torra.gates import state\n"
            ),
        },
    )
    files = report.source_files()
    result = report.score_testing(files, {f: report.tier_of(f) for f in files})
    assert result.offenders == ["torra/gates/hidden.py"]


def test_nesting_is_joined_by_def_line_not_by_name(report: ModuleType, tmp_path: Path) -> None:
    """A flat `run` defined after a tangled one once overwrote its depth, hiding the offender."""
    branches = "".join(
        f"                    if x == {i}:\n                        total += {i}\n"
        for i in range(8)
    )
    _write(
        tmp_path,
        {
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
                "        return 0\n"
            ),
        },
    )
    files = report.source_files()
    result = report.score_modularity(files, {f: report.tier_of(f) for f in files})
    assert result.offenders == ["torra/worker.py:2 run (CCN 12, depth 4)"]


def _results(report: ModuleType, **bands: str) -> list[object]:
    """One DimensionResult per floor dimension, at its floor band unless `bands` overrides it."""
    return [
        report.DimensionResult(name, bands.get(name, floor), "headline", [])
        for name, floor in report.ENFORCE_FLOORS.items()
    ]


def test_the_current_floors_themselves_pass(report: ModuleType) -> None:
    """Every dimension exactly at its floor, with a mean at the floor, is not a failure."""
    assert report.enforce_failures(_results(report), report.MIN_BAND_MEAN) == []


def test_the_floors_are_the_letter_floors_they_replaced(report: ModuleType) -> None:
    """A → strong, B → ok, one for one: the move to bands lowered no floor."""
    assert report.ENFORCE_FLOORS == {
        "Complexity": "strong",
        "Documentation": "strong",
        "Typing": "strong",
        "Testing": "strong",
        "Modularity": "ok",
        "Lint": "strong",
    }


def test_a_dimension_one_band_under_its_floor_fails(report: ModuleType) -> None:
    """Modularity weak against floor ok, and Complexity ok against floor strong, both fail."""
    failures = report.enforce_failures(
        _results(report, Modularity="weak", Complexity="ok"), report.MIN_BAND_MEAN
    )
    assert failures == [
        "Complexity: ok below floor strong",
        "Modularity: weak below floor ok",
    ]


def test_a_dimension_above_its_floor_passes(report: ModuleType) -> None:
    """Modularity strong against floor ok is fine."""
    assert report.enforce_failures(_results(report, Modularity="strong"), 4.0) == []


def test_a_band_mean_under_the_minimum_fails_and_one_at_it_does_not(report: ModuleType) -> None:
    """3.79 is below MIN_BAND_MEAN (3.80); exactly 3.80 is not."""
    assert report.MIN_BAND_MEAN == 3.80
    assert report.enforce_failures(_results(report), 3.79) == ["band mean 3.79 below floor 3.80"]
    assert report.enforce_failures(_results(report), 3.80) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, "strong"),
        (1.6, "ok"),
        (4.0, "ok"),
        (4.1, "weak"),
        (8.0, "weak"),
        (8.1, "poor"),
        (15.0, "poor"),
        (15.1, "failing"),
    ],
)
def test_band_lower_is_better_edges(report: ModuleType, value: float, expected: str) -> None:
    """A value exactly on a threshold keeps the better band; just past it drops one."""
    assert report.band(value, (1.5, 4.0, 8.0, 15.0), lower_is_better=True) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (90.0, "strong"),
        (89.9, "ok"),
        (75.0, "ok"),
        (74.9, "weak"),
        (55.0, "weak"),
        (54.9, "poor"),
        (35.0, "poor"),
        (34.9, "failing"),
    ],
)
def test_band_higher_is_better_edges(report: ModuleType, value: float, expected: str) -> None:
    """A value exactly on a threshold keeps the better band; just under it drops one."""
    assert report.band(value, (90.0, 75.0, 55.0, 35.0), lower_is_better=False) == expected


@pytest.mark.parametrize(
    ("mean", "expected"),
    [(3.83, "strong"), (3.5, "strong"), (3.49, "ok"), (2.5, "ok"), (0.4, "failing")],
)
def test_nearest_band(report: ModuleType, mean: float, expected: str) -> None:
    """The band whose value is nearest the mean, ties going to the better band."""
    assert report.nearest_band(mean) == expected


def test_the_overall_line_counts_bands_and_omits_zero_counts(report: ModuleType) -> None:
    """Five strong and one ok: no weak/poor/failing count, and no letter or GPA anywhere."""
    results = _results(
        report,
        Complexity="strong",
        Documentation="strong",
        Typing="strong",
        Testing="strong",
        Modularity="ok",
        Lint="strong",
    )
    text = report.render(results, 23 / 6, "strong", verbose=True)
    overall = next(line for line in text.splitlines() if "OVERALL" in line)
    assert "(5 strong, 1 ok)  band mean 3.83 (floor 3.80)" in overall
    assert overall.startswith("  OVERALL        strong   (")
    assert not any(word in overall for word in ("weak", "poor", "failing"))
    assert "  Modularity     ok       headline" in text
    assert re.search(r"\[[A-F]\]", text) is None
    assert "GPA" not in text
