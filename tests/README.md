# tests/

Organised by subsystem. Nothing here needs a live Ollama, systemd,
or network -- every external dependency is an injected callable (see
`conftest.py`), which is also how it's wired to the real thing in `torra/`.

| File | Holds |
|---|---|
| `conftest.py` | Shared fixtures: `scripted()` drives a multi-turn conversation, including the repair loop, without a daemon |
| `test_core.py` | `observe_machine()`'s probes (disk/accelerator/knowledge-library), `TaskSpec` validation, conflict detection, `ContextCompressor` |
| `test_gates.py` | The five guardrails: safety evasions, precondition TTL/flips, undo generation, verified rollback, clarifier accuracy, routing |
| `test_agent.py` | The model loop: plan validation, repair (including the placeholder-answer and dropped-task checks), conversation compression, backend errors, post-execution summarize() |
| `test_pipeline.py` | The seven-stage `Torra`: approval flow, dry-run, precondition re-checks, external-edit detection, rollback safety overrides, knowledge-dir wiring, the post-execution summary |
| `test_tui.py` | Terminal rendering: verdict display, checkbox logic, busy-guard races (Ctrl+D/Run double-dispatch), keybindings, backend-down/precondition-failure/exception reporting, rollback summaries, multi-tab sessions (independent state, per-session worker groups, close/reopen), transcript persistence, the session picker |
| `test_cli.py` | `cli.py`'s entry points: `ask`/`plan`/`run`/`check`/`doctor`/`evidence`/`label`, `main` |
| `test_resume.py` | `ResumePoint`'s on-disk round-trip: atomic writes, corrupt-file handling |
| `test_session.py` | `SessionStore`'s persisted-conversation layer: atomic meta writes, append-only transcript, corrupt-file/line tolerance, `ContextCompressor` rehydration |
| `test_telemetry.py` | `CorpusExporter.drift()` and `LoopholeAudit.audit()` at the unit level, plus secret redaction (`TaskLogger.scrub()`/`Label.why`) |
| `test_iterate.py` | `iterate()`'s bounded edit, verify, fix loop: stop conditions, round count, dry-run behaviour |
| `test_read_memory.py` | A completed read-only step's output entering the model's context, tagged by path, and the model dropping a path again |
| `test_known_programs.py` | Fail-closed classification: the read-only, bounded-write, protected-write, asks and CRITICAL command lists, plus `trust()` and the resolver fixes |
| `test_untrusted.py` | Data blocks that cannot be closed or forged, summaries, machine state and old transcripts at the right authority |
| `test_remote_files.py` | Remote push and pull move exact bytes, name what they touch and judge the real destination |
| `test_secrets.py` | Which secret-looking strings `redact_secrets` scrubs, and which credential files need a tick to read |
| `test_instruction_files.py` | `CLAUDE.md` stays a one-line import stub, every read-first trigger target exists and is indexed, `AGENTS.md` stays under its byte budget |

```bash
python3 -m pytest tests/ -q              # every test passes; the count never drops
```
