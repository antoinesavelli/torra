# tests/

609 cases, organised by subsystem. Nothing here needs a live Ollama, systemd,
or network -- every external dependency is an injected callable (see
`conftest.py`), which is also how it's wired to the real thing in `harness/`.

| File | Holds |
|---|---|
| `conftest.py` | Shared fixtures: `scripted()` drives a multi-turn conversation, including the repair loop, without a daemon |
| `test_core.py` | `observe_machine()`'s probes (disk/accelerator/knowledge-library), `TaskSpec` validation, conflict detection, `ContextCompressor` |
| `test_gates.py` | The five guardrails: safety evasions, precondition TTL/flips, undo generation, verified rollback, clarifier accuracy, routing |
| `test_agent.py` | The model loop: plan validation, repair (including the placeholder-answer and dropped-task checks), conversation compression, backend errors, post-execution summarize() |
| `test_pipeline.py` | The seven-stage `Harness`: approval flow, dry-run, precondition re-checks, external-edit detection, rollback safety overrides, knowledge-dir wiring, the post-execution summary |
| `test_tui.py` | Terminal rendering: verdict display, checkbox logic, busy-guard races (Ctrl+D/Run double-dispatch), keybindings, backend-down/precondition-failure/exception reporting, rollback summaries, multi-tab sessions (independent state, per-session worker groups, close/reopen), transcript persistence, the session picker |
| `test_cli.py` | `cli.py`'s entry points: `ask`/`plan`/`run`/`check`/`doctor`/`evidence`/`label`, `main` |
| `test_resume.py` | `ResumePoint`'s on-disk round-trip: atomic writes, corrupt-file handling |
| `test_session.py` | `SessionStore`'s persisted-conversation layer: atomic meta writes, append-only transcript, corrupt-file/line tolerance, `ContextCompressor` rehydration |
| `test_telemetry.py` | `CorpusExporter.drift()` and `LoopholeAudit.audit()` at the unit level, plus secret redaction (`TaskLogger.scrub()`/`Label.why`) |

```bash
python3 -m pytest tests/ -q              # must stay 609 passed
```
