#!/usr/bin/env python3
"""Compute the six-dimension defensive-coding scorecard for torra/.

Depends on: radon (complexity), ruff (lint, invoked as a subprocess), stdlib
ast (docstrings/type hints/test-import scanning). Read-only: never edits code.
See DEFENSIVE_CODING_STANDARDS.md for what each dimension means and why.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TORRA_ROOT = REPO_ROOT / "torra"
TESTS_ROOT = REPO_ROOT / "tests"

#: Last-known-good grades, recorded from the first clean run against this
#: codebase. A commit that drops any of these, or the overall GPA below
#: MIN_GPA, fails `--enforce`. Raise a floor when you clear it; never lower
#: one to turn a red gate green (see standards doc §2/§6).
#:
#: Complexity raised to A: `_judge`, `detect`, `execute`, and every other
#: CCN>=15 offender were decomposed into focused helpers (each still one
#: rule/check/phase per function, not a lookup table or a class hierarchy)
#: on 2026-08-11, so 0 functions now sit at or above the CCN 15 ceiling.
#: Modularity's file-size signal is also clean (0 files over 500 LOC --
#: `gates/rollback.py` split into `rollback.py` + `undo.py`), but the
#: dimension's separate nested-complexity signal (depth >= 3 AND CCN >= 10)
#: still has real offenders -- several are legitimate linear fallback
#: cascades (`detect_accelerator`, `split_segments`) the standards doc's
#: own §1.1 says not to fragment just to lower a number, so the floor
#: stays B rather than being pushed to A by fixing those too.
ENFORCE_FLOORS: dict[str, str] = {
    "Complexity": "A", "Documentation": "A", "Typing": "A",
    "Testing": "A", "Modularity": "B", "Lint": "A",
}
MIN_GPA = 3.80

GRADE_GPA = {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "F": 0.0}

#: Peripheral files are listed for visibility but excluded from the grade --
#: they are not on the path between "model proposes" and "shell executes".
PERIPHERAL_SUFFIXES = ("cli.py", "tui.py", "__init__.py")


def tier_of(path: Path) -> str:
    """Classify one file as critical, core, or peripheral (see standards doc §0)."""
    rel = path.relative_to(TORRA_ROOT).as_posix()
    if path.name in PERIPHERAL_SUFFIXES:
        return "peripheral"
    if rel.startswith("gates/") or rel in ("core/tasks.py", "pipeline.py"):
        return "critical"
    return "core"


def source_files() -> list[Path]:
    """Every torra/*.py file, caches excluded."""
    return sorted(p for p in TORRA_ROOT.rglob("*.py")
                  if "__pycache__" not in p.parts)


def functional_loc(path: Path) -> int:
    """Line count excluding blanks and comment-only lines."""
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


@dataclass
class DimensionResult:
    """One graded dimension: its letter, GPA, headline, and worst offenders."""

    name: str
    grade: str
    headline: str
    offenders: list[str]


def band(value: float, thresholds: tuple[float, float, float, float],
         lower_is_better: bool) -> str:
    """A/B/C/D/F from a value against four ascending thresholds."""
    a, b, c, d = thresholds
    if lower_is_better:
        if value <= a:
            return "A"
        if value <= b:
            return "B"
        if value <= c:
            return "C"
        if value <= d:
            return "D"
        return "F"
    if value >= a:
        return "A"
    if value >= b:
        return "B"
    if value >= c:
        return "C"
    if value >= d:
        return "D"
    return "F"


# --------------------------------------------------------------------------- #
# Complexity + Modularity (radon cc)
# --------------------------------------------------------------------------- #


def radon_complexity(files: list[Path]) -> dict[str, list[dict]]:
    """Per-function cyclomatic complexity, keyed by file path, via `radon cc -j`."""
    proc = subprocess.run(
        [sys.executable, "-m", "radon", "cc", "-j", *[str(f) for f in files]],
        capture_output=True, text=True, cwd=REPO_ROOT)
    if proc.returncode != 0:
        raise RuntimeError(f"radon cc failed: {proc.stderr}")
    return json.loads(proc.stdout)


def max_nesting_depth(node: ast.AST, depth: int = 0) -> int:
    """Deepest nesting of if/for/while/try blocks inside one function."""
    nesting_types = (ast.If, ast.For, ast.While, ast.Try)
    best = depth
    for child in ast.iter_child_nodes(node):
        child_depth = depth + 1 if isinstance(child, nesting_types) else depth
        best = max(best, max_nesting_depth(child, child_depth))
    return best


def function_nesting(files: list[Path]) -> dict[str, dict[str, int]]:
    """Nesting depth of every top-level-in-class-or-module function, by qualified name."""
    out: dict[str, dict[str, int]] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        per_file: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                per_file[node.name] = max_nesting_depth(node)
        out[str(path)] = per_file
    return out


def grade_complexity(files: list[Path], tiers: dict[Path, str]) -> DimensionResult:
    """% of decision-path functions at/above CCN 15 (hard ceiling, standards §1.1)."""
    decision_files = [f for f in files if tiers[f] != "peripheral"]
    if not decision_files:
        return DimensionResult("Complexity", "A", "no decision-path files", [])

    cc = radon_complexity(decision_files)
    total = over = 0
    offenders: list[tuple[int, str]] = []
    for filepath, blocks in cc.items():
        for block in blocks:
            total += 1
            if block["complexity"] >= 15:
                over += 1
                offenders.append(
                    (block["complexity"],
                     f"{Path(filepath).relative_to(REPO_ROOT)}:{block['lineno']} "
                     f"{block['name']} (CCN {block['complexity']})"))
    pct = (over / total * 100) if total else 0.0
    grade = band(pct, (1.5, 4.0, 8.0, 15.0), lower_is_better=True)
    offenders.sort(reverse=True)
    headline = f"{over} decision-path fn(s) >= CCN 15 ({pct:.1f}%), {total} checked"
    return DimensionResult("Complexity", grade, headline, [o for _, o in offenders])


def grade_modularity(files: list[Path], tiers: dict[Path, str]) -> DimensionResult:
    """File size (>500 functional LOC) and nested-complexity smells (standards §1.2)."""
    decision_files = [f for f in files if tiers[f] != "peripheral"]
    if not decision_files:
        return DimensionResult("Modularity", "A", "no decision-path files", [])

    oversized = [(functional_loc(f), f) for f in decision_files
                 if functional_loc(f) > 500]
    size_pct = len(oversized) / len(decision_files) * 100
    size_grade = band(size_pct, (2.0, 5.0, 10.0, 18.0), lower_is_better=True)

    cc = radon_complexity(decision_files)
    nesting = function_nesting(decision_files)
    tangled: list[str] = []
    total_fns = 0
    for filepath, blocks in cc.items():
        depths = nesting.get(filepath, {})
        for block in blocks:
            total_fns += 1
            if block["complexity"] >= 10 and depths.get(block["name"], 0) >= 3:
                tangled.append(
                    f"{Path(filepath).relative_to(REPO_ROOT)}:{block['lineno']} "
                    f"{block['name']} (CCN {block['complexity']}, depth "
                    f"{depths.get(block['name'], 0)})")
    tangled_pct = (len(tangled) / total_fns * 100) if total_fns else 0.0
    tangled_grade = band(tangled_pct, (1.5, 4.0, 8.0, 15.0), lower_is_better=True)

    grade = min(size_grade, tangled_grade, key=lambda g: GRADE_GPA[g])
    headline = (f"{len(tangled)} fn(s) nested >= depth 3 & CCN >= 10, "
                f"{len(oversized)} decision-path file(s) > 500 LOC")
    offenders = [f"{loc} LOC: {f.relative_to(REPO_ROOT)}"
                 for loc, f in sorted(oversized, reverse=True)] + tangled
    return DimensionResult("Modularity", grade, headline, offenders)


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #


def grade_documentation(files: list[Path], tiers: dict[Path, str]) -> DimensionResult:
    """% of decision-path modules carrying a 'Depends on:' line (standards §1.3)."""
    decision_files = [f for f in files if tiers[f] != "peripheral"]
    if not decision_files:
        return DimensionResult("Documentation", "A", "no decision-path files", [])

    missing: list[str] = []
    has_docstring = 0
    for path in decision_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(tree) or ""
        if doc:
            has_docstring += 1
        if "Depends on:" not in doc:
            missing.append(str(path.relative_to(REPO_ROOT)))

    covered = len(decision_files) - len(missing)
    pct = covered / len(decision_files) * 100
    grade = band(pct, (90.0, 75.0, 55.0, 35.0), lower_is_better=False)
    headline = (f"{covered}/{len(decision_files)} carry 'Depends on:' "
                f"({pct:.0f}%), {has_docstring}/{len(decision_files)} have a "
                "module docstring")
    return DimensionResult("Documentation", grade, headline, missing)


# --------------------------------------------------------------------------- #
# Typing
# --------------------------------------------------------------------------- #


def grade_typing(files: list[Path], tiers: dict[Path, str]) -> DimensionResult:
    """Average % of params and returns annotated (standards §1.4)."""
    decision_files = [f for f in files if tiers[f] != "peripheral"]
    if not decision_files:
        return DimensionResult("Typing", "A", "no decision-path files", [])

    params_total = params_typed = returns_total = returns_typed = 0
    offenders: list[str] = []
    for path in decision_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            returns_total += 1
            if node.returns is not None:
                returns_typed += 1
            else:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                                 f"{node.name} missing return annotation")
            args = [a for a in node.args.args + node.args.kwonlyargs
                    if a.arg not in ("self", "cls")]
            for arg in args:
                params_total += 1
                if arg.annotation is not None:
                    params_typed += 1
                else:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                                     f"{node.name}({arg.arg}) missing type hint")

    return_pct = (returns_typed / returns_total * 100) if returns_total else 100.0
    param_pct = (params_typed / params_total * 100) if params_total else 100.0
    avg = (return_pct + param_pct) / 2
    grade = band(avg, (90.0, 75.0, 60.0, 45.0), lower_is_better=False)
    headline = f"returns {return_pct:.0f}%, params {param_pct:.0f}%"
    return DimensionResult("Typing", grade, headline, offenders)


# --------------------------------------------------------------------------- #
# Testing
# --------------------------------------------------------------------------- #


def module_name_of(path: Path) -> str:
    """Dotted module name relative to the repo root, e.g. torra.gates.safety."""
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def test_imported_modules() -> set[str]:
    """Every torra.* dotted path referenced by an import/from-import in tests/."""
    imported: set[str] = set()
    for path in TESTS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    return imported


def grade_testing(files: list[Path], tiers: dict[Path, str]) -> DimensionResult:
    """% of decision-path modules with at least one test-side import (standards §1.5)."""
    decision_files = [f for f in files if tiers[f] != "peripheral"]
    if not decision_files:
        return DimensionResult("Testing", "A", "no decision-path files", [])

    imported = test_imported_modules()
    uncovered: list[str] = []
    for path in decision_files:
        name = module_name_of(path)
        # A module is covered if tests import it directly, or import its
        # parent package (which re-exports it) -- see each __init__.py.
        covered = any(name == m or name.startswith(m + ".") or m.startswith(name)
                      for m in imported)
        if not covered:
            uncovered.append(str(path.relative_to(REPO_ROOT)))

    covered_count = len(decision_files) - len(uncovered)
    pct = covered_count / len(decision_files) * 100
    grade = band(pct, (80.0, 65.0, 50.0, 35.0), lower_is_better=False)
    headline = f"{covered_count}/{len(decision_files)} import-covered by tests ({pct:.0f}%)"
    return DimensionResult("Testing", grade, headline, uncovered)


# --------------------------------------------------------------------------- #
# Lint
# --------------------------------------------------------------------------- #


def grade_lint(files: list[Path]) -> DimensionResult:
    """Raw ruff issue count across all graded files (standards §1.6)."""
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format=json",
         *[str(f) for f in files]],
        capture_output=True, text=True, cwd=REPO_ROOT)
    issues = json.loads(proc.stdout or "[]")
    grade = band(len(issues), (0, 5, 20, 50), lower_is_better=True)
    offenders = [f"{Path(i['filename']).relative_to(REPO_ROOT)}:{i['location']['row']} "
                f"{i['code']} {i['message']}" for i in issues]
    return DimensionResult("Lint", grade, f"{len(issues)} ruff issue(s)", offenders)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


#: Verbose offender lists are capped here, at display time, so headline
#: counts (over the full set) and the printed list never disagree -- found
#: live: each grade_*() truncated its own offenders to 10 before this
#: existed, so Modularity's headline said "11 fn(s)" while only 10 were
#: ever listed, with no "+more" to say the list wasn't complete.
MAX_OFFENDERS_SHOWN = 10


def render(results: list[DimensionResult], overall_gpa: float,
           overall_grade: str, verbose: bool) -> str:
    """The scorecard table, plus offenders when verbose."""
    lines = ["=" * 68, "DEFENSIVE CODING SCORECARD".center(68), "=" * 68]
    for r in results:
        lines.append(f"  {r.name:<14} [{r.grade}]  {r.headline}")
        if verbose:
            shown = r.offenders[:MAX_OFFENDERS_SHOWN]
            lines.extend(f"      - {o}" for o in shown)
            hidden = len(r.offenders) - len(shown)
            if hidden > 0:
                lines.append(f"      ... (+{hidden} more)")
    lines.append("-" * 68)
    lines.append(f"  {'OVERALL':<14} [{overall_grade}]  GPA {overall_gpa:.2f} "
                 "(decision-path weighted)")
    lines.append("=" * 68)
    return "\n".join(lines)


def gpa_to_grade(gpa: float) -> str:
    """Nearest letter grade for a GPA value."""
    for letter, points in sorted(GRADE_GPA.items(), key=lambda kv: -kv[1]):
        if gpa >= points - 0.5:
            return letter
    return "F"


def main() -> int:
    """Compute the scorecard, print it, and enforce floors if asked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="torra",
                        help="Subpackage to grade, relative to the repo root "
                             "(default: torra, the whole package)")
    parser.add_argument("--enforce", action="store_true",
                        help="Exit non-zero if any dimension or the overall "
                             "GPA drops below its recorded floor")
    parser.add_argument("--verbose", action="store_true",
                        help="List the worst offenders per dimension")
    args = parser.parse_args()

    area_root = (REPO_ROOT / args.area).resolve()
    files = [f for f in source_files() if area_root in f.parents or f.parent == area_root]
    if not files:
        print(f"no Python files found under {args.area}", file=sys.stderr)
        return 1
    tiers = {f: tier_of(f) for f in files}

    results = [
        grade_complexity(files, tiers),
        grade_documentation(files, tiers),
        grade_typing(files, tiers),
        grade_testing(files, tiers),
        grade_modularity(files, tiers),
        grade_lint(files),
    ]
    overall_gpa = sum(GRADE_GPA[r.grade] for r in results) / len(results)
    overall_grade = gpa_to_grade(overall_gpa)

    print(render(results, overall_gpa, overall_grade, args.verbose))

    if not args.enforce:
        return 0

    failures = [f"{r.name}: {r.grade} below floor {ENFORCE_FLOORS[r.name]}"
                for r in results
                if GRADE_GPA[r.grade] < GRADE_GPA[ENFORCE_FLOORS[r.name]]]
    if overall_gpa < MIN_GPA:
        failures.append(f"overall GPA {overall_gpa:.2f} below floor {MIN_GPA:.2f}")
    if failures:
        print("\nENFORCE FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
