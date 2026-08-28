# harness/

The package. Model proposes, five gates decide, the operator approves.

| File / dir | Holds |
|---|---|
| [`gates/`](gates/README.md) | The five safety guardrails, one submodule each |
| [`core/`](core/README.md) | Task/step primitives, conflict detection, context compression, the Ollama client |
| [`telemetry/`](telemetry/README.md) | Task logging, human labels, drift and loophole audits |
| `agent.py` | The model loop: conversation to validated `TaskSpec`, never a shell |
| `prompts.py` | `PLAN_SCHEMA`/`SYSTEM_PROMPT` -- the literal text handed to the model, split out of `agent.py` |
| `pipeline.py` | `Harness`, the seven-stage orchestrator, the only thing that decides what runs |
| `pipeline_types.py` | The data model `Harness` produces: `Stage`, `Outcome`, `StepPlan`, `Plan`, `RunResult` |
| `dependency_warnings.py` | Best-effort, pre-`execute()` check for an approved step depending on a declined one -- never blocks, so it isn't under `gates/` |
| `read_memory.py` | A completed read-only step's real output entering the model's own context, tagged by path, and the model later saying a path no longer helps |
| `cli.py` | Entry points: `harness`, `ask`, `plan`, `run`, `check`, `doctor`, `evidence`, `label` |
| `tui.py` | The terminal interface: one tab per session, chat pane plus the gated plan pane, pickers to reopen a past session or choose a model |
| `__init__.py` | Package-level re-exports |

Read `agent.py` and `pipeline.py` after the three subpackages above — see
[`HANDOFF.md`](../HANDOFF.md) for the full reading order and the invariants
that must not be broken. [`DEFENSIVE_CODING_STANDARDS.md`](../DEFENSIVE_CODING_STANDARDS.md)
covers how code here is written and graded; run
`python3 scripts/dev/defensive_coding_report.py` for the current scorecard.
