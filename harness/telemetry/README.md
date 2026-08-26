# harness/telemetry/

What the harness records about itself. `__init__.py` re-exports every public
name, so `from harness.telemetry import X` works exactly as it did when this
was one file.

| File | Holds |
|---|---|
| `records.py` | `TaskRecord`/`TaskLogger`, append-only JSONL of every task with what each gate decided |
| `labels.py` | `Label`/`LabelStore`, human verdicts attached to logged requests (depends on `records.py` for `TaskRecord`) |
| `audit.py` | `CorpusExporter` (drift against the synthetic clarifier corpus, scored from real logged outcomes via `TaskRecord.clarifier_blocked`, not a fresh clarifier call -- `HANDOFF.md` §2.34) and `LoopholeAudit` (the anchored-but-goalless gap), both depend on `records.py`/`labels.py` |
| `crash.py` | `CrashRecord`/`CrashReporter`, append-only JSONL of unhandled exceptions that escaped every other guard -- not ordinary gated-task failures, which `records.py` already covers (depends on `records.py` for `redact_secrets`) |
| `summary.py` | `health_summary`/`crash_summary`, human-readable aggregation over `records.py`/`crash.py` data -- shared by `cli.py`'s `harness evidence` and `tui.py`'s sidebar Evidence tab, so neither entry point depends on the other |

Covered by `tests/test_telemetry.py`: redaction, `CorpusExporter.drift()`,
`LoopholeAudit.audit()`, and each file's own failure-handling paths (a logger
degrades to a no-op rather than raising, per invariant §2.5).
