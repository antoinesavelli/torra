"""The 2026-10-25 module splits kept every public import path and the classes they were cut from.

Depends on: torra.gates.safety (and its parts safety_types, safety_tables, safety_resolve,
safety_judge).
"""

from __future__ import annotations

from torra.gates import safety, safety_judge, safety_resolve, safety_tables, safety_types


def test_safety_keeps_its_public_names_and_composes_its_two_halves() -> None:
    assert safety.RiskLevel is safety_types.RiskLevel
    assert safety.ResolvedCommand is safety_types.ResolvedCommand
    assert safety.Verdict is safety_types.Verdict
    assert safety.SafetyClassifier.__mro__[1:3] == (
        safety_resolve.CommandResolver, safety_judge.CommandJudge)
    assert safety.SafetyClassifier._protected is safety_judge.CommandJudge._protected


def test_the_safety_tables_use_only_the_risk_levels_the_classifier_orders() -> None:
    assert {level for level, _ in safety_tables.PROGRAM_RISK.values()} <= set(safety_types.ORDER)
    assert all(level in safety_types.ORDER for _, level, _ in safety_tables.BACKSTOP)
