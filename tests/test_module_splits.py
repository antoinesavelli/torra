"""The 2026-10-25 module splits kept every public import path and the classes they were cut from.

Depends on: torra.gates.safety (and its parts safety_types, safety_tables, safety_resolve,
safety_judge), torra.gates.known_programs (and its parts program_basics, program_tables),
torra.gates.undo (and its parts undo_types, undo_shadow, undo_remote), torra.pipeline (and its
part pipeline_steps), torra.agent (and its part agent_checks).
"""

from __future__ import annotations

from torra import agent, agent_checks, pipeline, pipeline_steps
from torra.gates import (
    known_programs,
    program_basics,
    program_tables,
    safety,
    safety_judge,
    safety_resolve,
    safety_tables,
    safety_types,
    undo,
    undo_remote,
    undo_shadow,
    undo_types,
)


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


def test_known_programs_keeps_its_public_names() -> None:
    assert known_programs.READ_ONLY is program_basics.READ_ONLY
    assert known_programs.BOUNDED_WRITE is program_basics.BOUNDED_WRITE
    assert known_programs.PROTECTED_WRITE is program_basics.PROTECTED_WRITE
    assert known_programs.reads_credentials is program_basics.reads_credentials
    assert known_programs.PREDICATES is program_tables.PREDICATES
    assert known_programs.SUBCOMMANDS is program_tables.SUBCOMMANDS



def test_undo_keeps_its_public_names_and_composes_its_two_halves() -> None:
    assert undo.Snapshot is undo_types.Snapshot
    assert undo.SnapshotKind is undo_types.SnapshotKind
    assert undo.UndoAction is undo_types.UndoAction
    assert undo.UndoGenerator.__mro__[1:3] == (undo_remote.RemoteCapture, undo_shadow.ShadowStore)
    assert undo.UndoGenerator.STALE_SHADOW_SECONDS is undo_shadow.ShadowStore.STALE_SHADOW_SECONDS


def test_torra_keeps_execute_and_inherits_the_step_loop() -> None:
    assert pipeline.Torra.__mro__[1] is pipeline_steps.StepRunner
    assert "execute" in vars(pipeline.Torra) and "execute" not in vars(pipeline_steps.StepRunner)
    assert {"_run_steps", "_arm_step", "_sweep_rollback"} <= set(vars(pipeline_steps.StepRunner))


def test_agent_inherits_its_plan_checks() -> None:
    assert agent.Agent.__mro__[1] is agent_checks.PlanChecks
    assert agent.Agent._extract_json is agent_checks.PlanChecks._extract_json
    assert {"_prescreen", "_repair_complaint"} <= set(vars(agent_checks.PlanChecks))
