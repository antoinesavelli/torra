# Audit findings — closed

**Date:** 2026-07-29. **Scope:** every module under `harness/` (23 files), plus
`cli.py`, `tui.py`, `pipeline.py`, `agent.py`, `pyproject.toml`,
`.github/workflows/ci.yml`. **Method:** read every file end to end, verified
each suspected issue against the live system before recording it, then
re-read the tree once more without finding anything new.

**All 7 findings were fixed** shortly after this audit (128 → 143 tests, ruff
clean, mypy clean, `defensive_coding_report.py --enforce` exit 0 at the time).
Kept as a one-line-each historical index, not the original write-up — the
original "What's wrong / proposed fix / regression test" prose is what a
findings doc collects *before* a fix lands; once fixed, that prose is dead
weight repeating what git history and the guarding test already say, and per
`CLAUDE.md`'s own rule this file must shrink, not just accumulate a growing
pile of "Fixed" stamps. If this file is ever extended with a **new**, still-open
finding, give it the full write-up (what's wrong, evidence, proposed fix,
regression test) the way these originally had it — trim it to one line only
once it's actually fixed.

| Finding | Fix | Guarded by |
|---|---|---|
| **MED-1** — shadow snapshot dirs leaked forever; `HANDOFF.md` claimed a reaper that didn't exist | `UndoGenerator.cleanup()` now runs after every successful execute; `_reap_stale_shadows()` removes dead-pid siblings older than 24h on construction | `tests/test_gates.py::TestUndoGeneration` (4 cases), `tests/test_pipeline.py::test_a_successful_run_cleans_up_its_shadow_copies`, `test_a_dry_run_keeps_its_shadow_copies` |
| **MED-2** — service handling hardcoded `--user` systemd scope; wrong for `ollama.service`, a system unit on this machine | Added `systemctl_scope()` (state.py + rollback.py), autodetects via `LoadState` instead of assuming; `capture_service`'s undo and `recovery_options()` now scope-aware | `tests/test_gates.py::TestSystemctlScope` (5 cases), `test_service_undo_uses_the_same_scope_it_was_captured_under` |
| **LOW-3** — `Agent.observe(refresh=True)` never actually refreshed the model catalog or sizes | Guard changed from `if not router.available` to `if refresh or not router.available` | `tests/test_agent.py::test_observe_refresh_repopulates_stale_model_metadata` |
| **LOW-4** — `TaskRecord.clarifier_anchors` declared but never persisted | `Turn.anchors` threaded through `_parse()`/`repair()`, `TaskLogger.from_result()` now sets it | `tests/test_pipeline.py::test_the_telemetry_record_carries_the_anchors_the_clarifier_found` |
| **LOW-5** — `Plan.conflicts` never populated; the TUI's "changed outside the harness" banner was dead | Removed the field and the dead banner branch; real conflict detection is `execute()`'s per-step `Outcome.BLOCKED`, documented on `Plan`'s own docstring | full suite green with the dead code gone |
| **LOW-6** — stale hardcoded test counts in CI comment and a docstring | Reworded to not need updating as the suite grows | — |
| **LOW-7** — append redirect (`>>`) worded as "overwrites" in the safety reason text | Regex now captures the operator; wording says "appends to" vs "overwrites"; classification (CRITICAL) was already correct in both cases | `tests/test_gates.py::TestSafety::test_an_append_redirect_is_worded_as_an_append_not_an_overwrite` |

## Things checked and found correct (no action)

Recording these so the next auditor doesn't re-derive them:

- **Safety classifier** — argv resolution, wrapper/interpreter unwrapping, base64
  decode, `kill -s`/signal-spec PID-1 handling, `/dev/null` exemption, and the
  three-source `max()` merge (judge + backstop + redirect) all hold up.
- **VerifiedRollback** — three-state (undone/unverified/failed) semantics,
  LIFO order, push-back-on-halt, and crashing-confirm-callback handling are
  correct.
- **Double-execute / retry guards** — `RunResult.executed` correctly distinguishes
  "a real command was attempted" from "reached a terminal outcome"; REJECTED,
  dry-run, and BLOCKED-before-any-step all stay retryable.
- **Router model grounding** — `/api/show`-based sizes and chat-capability now
  feed both `ModelRouter` and `Accelerator.fits`; embedding-only models are
  excluded from chat routing.
- **Telemetry** — every method swallows its own exceptions;
  `TaskLogger.__init__` degrades to a no-op on an unwritable path; redaction runs
  before write.
- **ConflictDetector** — explicit-stack walk is symlink-loop-safe; the periodic
  full walk covers inotify's three blind spots; declared-vs-tree dedup is correct.
- **Context compression** — the net-saving guard and terser-fallback ladder
  prevent the measured "-484% reduction" pathology. *(Caveat added 2026-08-23:
  this was checked by reading, not by a test, at the time of this audit — see
  `tests/test_core.py::TestContextCompressor`, added later, for the test that
  now actually proves it.)*

---

Newer live-wire findings from later work are tracked in `HANDOFF.md` §5
("Live wires"), not appended here — that section has the same discipline
(what broke, why it's not obvious, which test guards it) and is read
alongside the invariants it sits next to.
