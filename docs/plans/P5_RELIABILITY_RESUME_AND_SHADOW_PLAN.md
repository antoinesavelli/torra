# Plan: P5 Systems and Reliability, resume expiry and the shadow-copy regular-file check

Status: done
Owner decisions pending: none (owner's yes of 2026-11-19 covers these two fixes)
Last verified: 3d81c60 on 2026-11-19 (U1 and U2 committed; U3, the load fix and U4 in the next commit)
Executor: an Opus orchestrator session; Sonnet builds each unit, a fresh Opus verifies it

## 1. Goal

Close the two Systems and Reliability deductions of the 2026-11-18 re-grade (`docs/plans/GRADE_TO_A_PLAN.md`
Phase 5): gap 6.6 (a resume point never expires and the operator cannot see it at tick time) and
`UndoGenerator.capture_file` copying a model-declared `touches` path that is not a regular file.

## 2. Decisions

- **D1, 24h expiry.** `resolve_resume` ignores a point whose `at` is more than `RESUME_MAX_AGE_SECONDS`
  (24 * 3600, the value of `ShadowStore.STALE_SHADOW_SECONDS` in `torra/gates/undo_shadow.py`) from `now`, in
  either direction (a far-future `at` is a jumped clock or garbage), notes why and starts fresh. A point whose
  `in_progress` mark names a step of this plan never expires (§2.64 point 2): that step's effect stays unknown
  however old. A mark naming a step the plan lacks does expire, since it would not block (U2 verify finding).
  `now` is injectable. `at` joins `_well_typed` (a finite number, not a bool), so a wrong-typed, NaN, infinite or
  too-large `at` is a corrupt file and starts fresh instead of raising inside `execute()`. No format change:
  every save already writes `at`. Residual: every save re-stamps `at`, so a task that fails daily keeps its
  first day's completed steps; a first-written time would be a new field (format change, owner's call).
- **D2, no sweep.** An expired file is inert and the next run of the same task id overwrites it (`begin_step`).
  Files of task ids never re-run stay, a few hundred bytes each; the shadow reaper is not taught about them.
- **D3, plan-time preview.** `Torra._gate` runs the same `resolve_resume` read-only (it only loads) and appends
  to `plan.warnings`: "resumes N of M steps: a, b (an earlier attempt finished them; delete <path> to run them
  again)", or, for an in-flight mark, the `interrupted_reason` text prefixed "this run will stop: ". Both the CLI
  and the TUI already render `plan.warnings`, so neither display changes. `execute()`'s own resolution stays the
  authority; the preview can only be stale in the safe direction (it said "resumes", execute runs them).
- **D4, regular files only.** `capture_file` calls `stat()` (follows a symlink, never opens, so a FIFO cannot
  block it) and refuses anything not `S_ISREG`: a character or block device, FIFO, socket or directory, and a
  symlink to any of them. It records the reason in `skipped` ("<path> is a character device, not a regular file;
  torra does not read or copy it, so no undo generated, declare one manually") and returns None. The plan shows
  that step as uncovered (CLI "undo: none derivable", TUI "no automatic undo for: <id>"), and the reason is in
  `plan.coverage["skipped"]` and the gate's decision log. A missing path and a dangling symlink keep today's
  FILE_ABSENT path.
- **Residual (accepted):** a regular file swapped for a device between `stat()` and the copy. Closing it means
  copying from a checked fd, which the `cp --reflink` path cannot do; the path is the model's, the swap needs a
  concurrent local writer. Worst case: a file swapped for `/dev/zero` is read by `copy2` without bound, filling the
  shadow dir's disk. Under reflink, `cp --reflink=always` refuses the device at once (measured, coreutils 9.4: "failed
  to clone"; its 60s timeout would cap it otherwise) and `_copy` falls back to that same `copy2`.

## 3. Ask-first boundaries

Stop and ask the owner before: any change to the format of `resume/*.json`, `telemetry/tasks.jsonl` or
`audit/*.audit.json`; any change to how `execute()`/`reject()` decide what runs (beyond D1 deciding a point
expired); an edit outside `torra/gates/undo.py`, `torra/core/resume.py` (where `resolve_resume` lives),
`torra/pipeline.py`, the plan display and tests; a change to an existing invariant's meaning.

## 4. Units (failing tests first in each)

| Unit | Files | Done when | Check |
|---|---|---|---|
| U1 shadow copy | `torra/gates/undo.py`, `tests/test_gates.py` | D4 holds; tests: `/dev/null` and a FIFO and a socket and a symlink to `/dev/null` are skipped with the reason and never opened (a FIFO test must not hang), a regular file and a symlink to one still copy, the step is uncovered | `uv run pytest -q tests/test_gates.py` |
| U2 expiry | `torra/core/resume.py`, `tests/test_resume.py`, `tests/test_pipeline.py::TestResume` | D1 holds; tests: an old point is ignored with a note, an old `in_progress` mark still blocks through `execute()`, a fresh point still resumes, a wrong-typed `at` starts fresh, the constant equals `STALE_SHADOW_SECONDS` | `uv run pytest -q tests/test_resume.py tests/test_pipeline.py` |
| U3 preview (after U2) | `torra/pipeline.py`, `tests/test_pipeline.py`, `tests/test_cli.py`, `tests/test_tui.py` | D3 holds; tests: plan warning for a resumable point, for an interrupted one, none for an expired or absent one, the CLI and TUI show the line | `uv run pytest -q tests/test_pipeline.py tests/test_cli.py tests/test_tui.py` |
| U4 docs (orchestrator) | `docs/INVARIANTS.md` §2.64, `docs/LIVE_WIRES.md`, `docs/STATUS.md`, `docs/DECISIONS.md` | entries within ceilings, §6.6 and the shadow line shrunk to pointers, a D-017 row for 24h | `uv run pytest -q tests/test_knowledge_refs.py` |

U1 runs beside U2; U3 waits for U2 (both edit `tests/test_pipeline.py`). Full Definition of Done after U4.

## 5. Record

| Unit | Built by | Verified by | Result |
|---|---|---|---|
| U1 | Sonnet | fresh Opus: PASS | `e566993`; the orchestrator added `digest` to the test's never-read patch |
| U2 | Sonnet | fresh Opus: FAIL (huge-int `at` raised in `execute()`, NaN never expired, a mark naming no plan step was exempt), fixed, re-verified PASS | `3d81c60`; D1 revised |
| U3 | Sonnet | fresh Opus: PASS | next commit; the verifier found a FIFO resume file now hung `plan()`, so `ResumePoint.load` refuses a non-regular file (orchestrator, with a test) |
| U4 | orchestrator | `tests/test_knowledge_refs.py` | next commit |
| U5 `exists()` crash (owner's yes 2026-11-19, after a live crash) | orchestrator | fresh Opus: FAIL (the raise moved to the conflict check in `execute()`) | `8b719ed`: `capture_file` records a skip when `exists()` raises |
| U6 the same raise in `execute()` | orchestrator | full Definition of Done | `24cd268`: `ConflictDetector` reports an unstattable path `unreadable`, so the run blocks and sweeps |
| U7 a glob in `touches` (owner's yes 2026-11-19) | orchestrator | full Definition of Done | `4b5d4a8`: `capture_file` skips it; the conflict check reported it `unreadable`, so the run blocked |
| U8 warn, don't block (owner's choice 2026-11-19, D-018) | orchestrator | re-verify: FAIL (a real `[`-named file lost its check and undo; watch roots; `refresh`) | `aef8f6c`: a glob or unstattable `touches` path is in `ConflictDetector.unchecked`: plan warning, Run log, no block; a later change still blocks |
| U9 re-verify of U8 | orchestrator | re-verify: FAIL (an absent `app[1].conf` created from outside went unseen; a `refresh`-unchecked path stayed so; a lost root waited for the walk) | `1c7ab1a`: a glob needs no literal file (a present `app[1].conf` keeps its undo and check); an unstattable watch root is unchecked at baseline, `unreadable` later; `refresh` moves a path torra's own step made unstattable to unchecked; LIVE_WIRES §5.26 |
| U10 re-verify of U9 | orchestrator | re-verify: FAIL (torra's own `chmod 0` read as an outside change, blocking and dropping the declared rollback) | `fe1bcde`: a glob's literal name is still compared (created blocks); a `refresh`-unchecked path is skipped only while unstattable; a lost watch root blocks on the inotify path; D-018 and LIVE_WIRES §5.26 match |
| U11 one model, not more patches | orchestrator | re-verify: FAIL (an own lock under a watch root via inotify; an unchecked root restored got no watch; the own mark outlived its lock) | `13a19b8`: each declared path and watch root CHECKED (prior hash) or UNCHECKED (stat raised at baseline); `refresh` keeps the prior hash for torra's own unstattable; a glob is an undo concern only; pinned by a 5 kinds x 8 events matrix (`tests/test_core.py`) |
| U12 re-verify of U11 (branch-introduced only) | orchestrator | re-verify: PASS (code); docs and records left | `7e1846c`: `_classify` follows the skip rule; a root restored by an own step gets its watch; a mark ends once torra can stat the path; watch-root defects already on main recorded in STATUS, xfail |
| U13 records only (no code change) | orchestrator | full Definition of Done | the fail-closed mirror of an own lock and the walk half of the chmod'ed-subdirectory gap pinned by strict xfails; STATUS, LIVE_WIRES §5.26 and D-018 say both sides of the choice; `034b15f` |

Left open (`docs/STATUS.md` §6.5, §6.6): `at` re-stamped on every save; the preview is a snapshot, and a dry run
still shows "will stop"; a dangling symlink
is snapshotted as absent; `remote_edit` reads a `reads` file unchecked after approval.
