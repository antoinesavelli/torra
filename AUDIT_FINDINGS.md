# Audit findings — read-only full-codebase pass

**Date:** 2026-07-29
**Scope:** every module under `harness/` (23 files), plus `cli.py`, `tui.py`,
`pipeline.py`, `agent.py`, `pyproject.toml`, `.github/workflows/ci.yml`.
**Method:** read every file end to end, verified each suspected issue against the
live system before recording it, then re-read the tree once more without finding
anything new.
**Baseline at time of audit:** 128 tests pass, ruff clean, mypy clean.

**Status: all 7 findings fixed.** 143 tests pass (was 128), ruff clean, mypy
clean, `defensive_coding_report.py --enforce` exits 0. Each section below is
kept as the historical record of what was found and how it was fixed, with a
**Fixed** line added at the top.

This document originally **recorded** issues and proposed a fix for each,
written before any code was changed. An agent picking this up later should
treat each finding independently: confirm the fix still holds, and if this
document is ever extended with new findings, follow the same discipline
(confirm live, fix, add a regression test, run the full suite + scorecard)
before moving to the next.

Nothing here is a safety-gate defect. The classifier, rollback verification,
approval flow, and double-execute guards all held up under the read. The findings
are a disk leak, a service-scope assumption that is wrong for the machine this
runs on, and a handful of dead fields / stale docs.

---

## MED-1 — Shadow snapshot directories leak forever; docs claim they're reaped

**Fixed.** `UndoGenerator.cleanup()` now runs after every successful
(non-dry-run) execute, and recreates an empty `shadow_dir` so the same
generator stays usable for the rest of the session. A new `_reap_stale_shadows()`
runs on construction and removes sibling `harness-shadow-*` directories that
are both older than `STALE_SHADOW_SECONDS` (24h) and whose owning pid is dead
-- never a run still in progress. `HANDOFF.md:475`'s "reaped by age,
prefix+pid strict" claim is now true rather than aspirational.
Tests: `tests/test_gates.py::TestUndoGeneration` (`test_cleanup_empties_...`,
`test_a_dead_old_sibling_shadow_dir_is_reaped`,
`test_a_fresh_sibling_shadow_dir_is_not_reaped`,
`test_a_live_pids_shadow_dir_is_never_reaped`),
`tests/test_pipeline.py::test_a_successful_run_cleans_up_its_shadow_copies`,
`test_a_dry_run_keeps_its_shadow_copies`.


**Files:** `harness/gates/rollback.py` (`UndoGenerator.cleanup`, line ~430),
`harness/pipeline.py` (`Harness.close`, line ~645; `_finish`, line ~605),
`HANDOFF.md` line 475.

**What's wrong.** `UndoGenerator.cleanup()` (`shutil.rmtree(self.shadow_dir)`)
has **zero callers** anywhere in the codebase. `Harness.close()` calls
`self.conflicts.close()` but never `self.undo.cleanup()`. On a *successful* run
`self.rollback.clear()` empties the undo *stack*, but that does not touch the
shadow *directory* on disk. So every run that snapshots any file leaves its
`~/.harness/harness-shadow-<pid>/` directory behind permanently.

**Evidence.** 14 leaked shadow directories are already sitting under
`/home/antoine/.harness/`:
```
ls -d /home/antoine/.harness/harness-shadow-* | wc -l   # -> 14
```

**Doc contradiction.** `HANDOFF.md:475` states:
```
shadow copies    <workdir>/harness-shadow-<pid>/, reaped by age, prefix+pid strict
```
There is no age-based reaper and no prefix+pid reaping logic anywhere in the
tree. The doc describes behavior that was never implemented.

**Proposed fix.**
1. In `Harness.close()`, call `self.undo.cleanup()` **only after** confirming the
   run is in a terminal, non-recoverable state — but note the existing intent
   (`Harness.close` docstring: *"Shadow copies are kept for post-mortem"*) is
   deliberate. So the safer fix is a **bounded reaper**, not eager deletion:
   on `UndoGenerator.__init__`, before creating the new shadow dir, delete
   sibling `harness-shadow-*` directories whose mtime is older than a TTL
   (e.g. 24h) **and** whose pid is no longer alive (`os.kill(pid, 0)` /
   `/proc/<pid>` check). This matches what `HANDOFF.md:475` already claims
   ("reaped by age, prefix+pid strict") — implement the doc rather than rewrite
   it.
2. On a **successful** run specifically (the `else` branch in
   `Harness.execute`, right after `self.rollback.clear()`), the shadow dir for
   *that* run is safe to remove immediately — nothing will roll back a completed
   run. Add `self.undo.cleanup()` there.
3. If instead you decide reaping is not worth building, then **fix the doc**:
   change `HANDOFF.md:475` to say shadow copies are retained indefinitely for
   post-mortem and must be cleaned manually, so the claim matches reality.

**Regression test.** After a successful `Harness.execute`, assert the shadow
directory no longer exists on disk. For the reaper, create a stale
`harness-shadow-<dead-pid>` dir with an old mtime, construct an `UndoGenerator`,
and assert the stale one was removed while a fresh-pid one was not.

**Severity rationale:** unbounded disk growth in the user's home dir, plus a
documentation claim that is simply false. Not data-loss, hence MED not HIGH.

---

## MED-2 — Service handling assumes `--user` systemd scope; wrong for system services

**Fixed.** Added `systemctl_scope(unit)` (in both `state.py` and, duplicated
per the gates' no-cross-reference rule, `rollback.py`), which distinguishes
"not loaded in user scope" from "loaded and inactive" via `LoadState`
(`is-active` alone can't tell them apart). `StateVerifier.service_active`'s
`user_scope` default changed from `True` to `None` (autodetect).
`UndoGenerator` gained an injectable `scope_probe`, and `_systemctl_state` /
`capture_service`'s generated undo both resolve and use the real scope
instead of hardcoding `--user`. `OllamaClient.recovery_options()`'s restart
advice is now scope-neutral (tries `--user` first, falls through to `sudo
systemctl`) rather than a subprocess probe, since `OllamaClient` is
deliberately network-only with no subprocess dependency.
Tests: `tests/test_gates.py::TestSystemctlScope` (5 cases),
`test_service_undo_uses_the_same_scope_it_was_captured_under`.


**Files:** `harness/core/ollama.py` (`recovery_options`, line ~169),
`harness/gates/state.py` (`StateVerifier.service_active`, line ~94),
`harness/gates/rollback.py` (`_systemctl_state` line ~197, `capture_service`
undo line ~333).

**What's wrong.** Every systemd interaction in the harness hardcodes or defaults
to `--user` scope:
- `recovery_options` advises `systemctl --user restart ollama`
- `StateVerifier.service_active(unit, user_scope=True)` defaults to user scope
- `UndoGenerator._systemctl_state` runs `systemctl --user is-active <unit>`
- `capture_service`'s generated undo runs `systemctl --user start|stop <unit>`

But the Ollama service on this machine — the single service the harness cares
about most — is a **system** service (`/etc/systemd/system/ollama.service`), not
a user service.

**Evidence (live).**
```
systemctl --user is-active ollama   # -> "inactive", exit 4
systemctl is-active ollama          # -> the real state (system scope)
```
So the recovery advice ("run `systemctl --user restart ollama`") is wrong and
would not start the service, and any auto-generated service rollback would probe
and act on the wrong scope — silently reading "unknown"/"inactive" and
restoring the wrong state (or nothing).

**Proposed fix.** Detect scope instead of assuming it. A unit is a system unit
if `systemctl --user is-active <unit>` errors with "no such unit" / exit 4 while
`systemctl is-active <unit>` resolves. Concretely:
1. Add a helper (e.g. in `state.py`) `systemctl_scope(unit) -> "user" | "system"`
   that probes `--user` first and falls back to system scope when the user probe
   reports the unit is not loaded.
2. Thread the resolved scope through `service_active`, `_systemctl_state`, and
   `capture_service` so probe, state-capture, and undo all use the *same* scope
   for a given unit.
3. `recovery_options` for Ollama should reflect the detected scope, or list both
   forms. Given we know Ollama is a system service here, the default recovery
   string is currently actively misleading.

**Regression test.** With an injected `service_probe`/runner that simulates a
unit present only in system scope, assert `service_active` returns the correct
state and `capture_service`'s undo issues a **system-scope** `systemctl` command,
not `--user`.

**Severity rationale:** produces wrong recovery guidance and wrong service
rollback for the harness's own primary dependency. Confirmed live. MED because
service rollback is not the most-travelled path and files/packages are unaffected.

---

## LOW-3 — `Agent.observe(refresh=True)` never refreshes the model catalog or its sizes

**Fixed.** The model-metadata block's guard changed from `if not
self.router.available:` to `if refresh or not self.router.available:`, so an
explicit refresh re-fetches `ollama.models()` and re-populates
`router.model_params_b`/`model_chat_capable` instead of keeping whatever was
fetched on the first `observe()` call.
Test: `tests/test_agent.py::test_observe_refresh_repopulates_stale_model_metadata`.


**File:** `harness/agent.py`, `observe()`, lines ~193–231.

**What's wrong.** The model metadata population is guarded by
`if not self.router.available:`. On the first `observe()` this runs and fills
`router.available`, `router.model_params_b`, `router.model_chat_capable`, and
`accelerator.model_params_b`. But on a subsequent `observe(refresh=True)` — whose
entire purpose is to pick up changes — `self.state` is re-read and the
accelerator is re-assigned, yet because `router.available` is already truthy the
whole model-metadata block is skipped. If the model catalog changed during the
session (models pulled or deleted — which happened in this very session), a
refresh silently keeps stale sizes/capabilities.

**Why it hasn't bitten yet.** `observe(refresh=True)` currently has no callers
(`grep` confirms). This is latent, not live.

**Proposed fix.** Split "have I ever populated this" from "should I re-read on an
explicit refresh." On `refresh=True`, re-run `ollama.models()` +
`model_info(...)` and reassign the router/accelerator dicts even when
`router.available` is already set. Guard the network cost so an ordinary cached
`observe()` (refresh=False) still short-circuits.

**Regression test.** Call `observe()`, change the fake transport's model list,
call `observe(refresh=True)`, assert `router.available` / `model_params_b` now
reflect the new list.

---

## LOW-4 — `TaskRecord.clarifier_anchors` is declared but never persisted

**Fixed.** Added `anchors: list[str]` to `Turn` (agent.py), threaded through
`_parse()` and `repair()` alongside the existing `clarifier_gaps` plumbing,
and populated in `Turn.to_dict()`. `TaskLogger.from_result()` now sets
`record.clarifier_anchors = list(getattr(turn, "anchors", []))`.
Test: `tests/test_pipeline.py::test_the_telemetry_record_carries_the_anchors_the_clarifier_found`.


**File:** `harness/telemetry/records.py` (`TaskRecord.clarifier_anchors` field
line ~34; `from_result` never assigns it).

**What's wrong.** `from_result()` sets `clarifier_codes` (from
`turn.clarifier_gaps`) but never sets `clarifier_anchors`, so every persisted
record carries an empty anchor list. `LoopholeAudit.audit()`
(`harness/telemetry/audit.py:259`) reads `record.clarifier_anchors` first and
only works because it falls back to **recomputing** anchors via
`self.clarifier.anchors_in(record.request)`. The stored field is dead data.

**Proposed fix.** Either populate it in `from_result` (the `turn`/`clarification`
already computed the anchors upstream — thread them through, or recompute once at
record time), or delete the field and the `LoopholeAudit` branch that reads it,
keeping only the recompute path. Populating is preferable: it makes the JSONL log
self-contained rather than dependent on re-running the clarifier at audit time.

**Regression test.** Build a record via `from_result` for a request with a known
anchor (e.g. an absolute path) and assert `record.clarifier_anchors` is non-empty.

---

## LOW-5 — `Plan.conflicts` is never populated; the TUI's "changed outside the harness" banner is dead

**Fixed via option (a).** Removed `Plan.conflicts` (and its `to_dict()`
entry, and the now-unused `Conflict` import in pipeline.py) and the dead
`if plan.conflicts:` banner in `tui.py`. `Plan`'s docstring now explains why
there's no conflicts field, pointing at `Harness.execute()`'s per-step check
(`Outcome.BLOCKED`) as where real conflict detection actually happens.
No new test needed beyond confirming the full suite stays green with the
dead code gone.


**Files:** `harness/pipeline.py` (`_gate`, lines ~350–363 — the removed
pre-approval `detect()`), `harness/tui.py:145–148`.

**What's wrong.** A prior fix (correctly) removed the redundant pre-approval
`self.conflicts.detect()` call from `_gate`, because it ran immediately after
`baseline()` and always returned `[]`. As a result `Plan.conflicts` now always
keeps its default empty list. But `tui.py:145` still does
`if plan.conflicts:` to render a red "N file(s) changed outside the harness since
baseline" banner — which can therefore never appear. External-change detection
now happens only at `execute()` time (surfacing as a `BLOCKED` outcome), never in
the pre-approval plan view.

**Proposed fix.** Two valid directions — pick one and make it intentional:
- **(a) Remove the dead UI.** Delete the `plan.conflicts` branch in `tui.py`
  (and consider dropping the `Plan.conflicts` field) since conflict detection is
  now an execute-time concern surfaced via `BLOCKED`.
- **(b) Repopulate it meaningfully.** If a pre-approval "these files already
  changed since you last baselined" signal is wanted, compute it at a point where
  it can be non-empty (i.e. detect against a *prior* baseline before re-baselining
  in `_gate`), and populate `plan.conflicts` from that.

Option (a) is simpler and matches the current architecture; (b) only if the
product wants the pre-approval warning back.

**Regression test.** For (a): none needed beyond confirming the field/branch is
gone. For (b): a test that a file changed since a prior baseline shows up in
`plan.conflicts`.

---

## LOW-6 — Stale counts in docs/comments

**Fixed.** `.github/workflows/ci.yml`'s header comment and
`harness/core/tasks.py`'s `TaskSpec` docstring no longer hardcode a test
count (76 and 121 respectively, both stale against the actual 143). Worded
to not need updating as the suite grows.


**Files:** `.github/workflows/ci.yml:1` ("the 76-case test suite"),
`harness/core/tasks.py:92` (`TaskSpec` docstring: "121 tests already cover").

**What's wrong.** The suite currently has **128** passing tests. The CI header
comment says 76; the `TaskSpec` docstring says 121. Both are stale. These are
comments/docs only (CI actually runs `pytest tests/ -q` with no hardcoded count,
so the build is unaffected), but they misinform a reader.

**Proposed fix.** Prefer wording that does not need updating on every test added:
e.g. CI comment → "the full test suite, mypy, and the defensive-coding
scorecard"; `tasks.py:92` → "the same code path the existing test suite already
covers" (drop the number). If a number is kept anywhere, add a note that it must
be bumped, or better, don't hardcode it.

---

## LOW-7 — Append redirect (`>> /path`) is reported as "overwrites"

**Fixed.** The redirect regex now captures the operator itself
(`(>{1,2})\s*(/[^\s;|&]+)`), and the reason text says "appends to" for `>>`
and "overwrites" for `>`. Classification (CRITICAL for a protected path) is
unchanged in both cases -- only the wording was wrong.
Test: `tests/test_gates.py::TestSafety::test_an_append_redirect_is_worded_as_an_append_not_an_overwrite`.


**File:** `harness/gates/safety.py`, `classify()`, redirect block lines ~439–447.

**What's wrong.** The redirect regex `>\s*(/[^\s;|&]+)` matches the second `>`
of a `>>` append (e.g. `echo x >> /etc/hosts` → captures `/etc/hosts`). The
classification is **correct** (writing to a protected path is CRITICAL), but the
reason text reads `CRITICAL: redirect overwrites /etc/hosts` when the operation
is an append, not an overwrite. Cosmetic — the verdict level is right, only the
human-facing wording is slightly inaccurate.

**Proposed fix.** Detect `>>` vs `>` and word the reason accordingly
("append to" vs "overwrite"), or use scope-neutral wording ("redirect writes to
`<path>`"). Keep the level unchanged.

**Regression test.** Assert `echo x >> /etc/hosts` classifies CRITICAL (already
true) and, if you word-split, that the reason says "append" not "overwrite".

---

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
  dry-run, and BLOCKED-before-any-step all stay retryable (the last one was fixed
  during this session's earlier audit round).
- **Router model grounding** — `/api/show`-based sizes and chat-capability now
  feed both `ModelRouter` and `Accelerator.fits`; embedding-only models are
  excluded from chat routing.
- **Telemetry** — every method swallows its own exceptions;
  `TaskLogger.__init__` degrades to a no-op on an unwritable path; redaction runs
  before write.
- **ConflictDetector** — explicit-stack walk is symlink-loop-safe; the periodic
  full walk covers inotify's three blind spots; declared-vs-tree dedup is correct.
- **Context compression** — the net-saving guard and terser-fallback ladder
  prevent the measured "-484% reduction" pathology.

---

## Suggested order for the next agent

1. **MED-1** (disk leak) — highest concrete impact, self-contained fix.
2. **MED-2** (systemd scope) — correctness for the harness's own dependency;
   slightly larger because it threads scope through three call sites.
3. **LOW-4 / LOW-5 / LOW-6 / LOW-7** — small, independent cleanups.
4. **LOW-3** — latent; fix when/if `observe(refresh=True)` gains a caller, or
   fix now to close it pre-emptively.

Run `python3 -m pytest tests/ -q`, `ruff check harness/ tests/`, `mypy harness/`,
and `python3 scripts/dev/defensive_coding_report.py` after each fix.
