# HANDOFF

For whoever or whatever picks this up next.

This document assumes you are an agent or an engineer with no memory of how this
got built. It tells you what is load-bearing, what will bite you, and what has
already been tried and failed. Before changing anything in `torra/`, look up the §
your change touches: `grep -n '^### 2\.' HANDOFF.md` lists every invariant and
`grep -n '^### 5\.' HANDOFF.md` every live wire.

---

## 1. Orientation, in ninety seconds

```bash
cd <repo> && python3 -m venv .venv && source .venv/bin/activate
pip install pytest pytest-asyncio pyyaml textual inotify_simple && pip install -e .
python3 -m pytest tests/ -q          # every test passes; the count never drops
torra doctor
```

Eight modules, ~6,900 lines shipped. `core.py`, `gates.py` and `telemetry.py` were later
each split into a same-named package of single-concern submodules (see
`DEFENSIVE_CODING_STANDARDS.md` §1.2 for why); every name still imports from the same
top-level path, e.g. `from torra.gates import SafetyClassifier`. Read in this order:

1. `torra/core/` — `Step`, `TaskSpec` (`tasks.py`), `OllamaClient` (`ollama.py`),
   conflict detection (`conflict.py`) — see `torra/core/README.md`
2. `torra/gates/` — the five guardrails (`rollback.py`+`undo.py` is one
   guardrail, ROLLBACK_ON_FAIL, split across two files past the LOC cap) —
   see `torra/gates/README.md`
3. `torra/agent.py` — the model loop
4. `torra/pipeline.py` — `Torra`, the seven stages, the only orchestrator
   (`torra/pipeline_types.py` holds the `Stage`/`Outcome`/`Plan`/`RunResult`
   data model it produces, re-exported from `pipeline.py` unchanged)

`tui.py`, `cli.py` and `torra/telemetry/` are leaves. Nothing depends on them.

---

## 2. The invariants

Break any of these and the safety story collapses silently. There is a test for
each; if you find yourself deleting one of those tests, stop.

New entries take the next unused number and the shape in `agents/LOOP.md`'s
Definition of Done; the story goes in the commit message.

### 2.1 The classified command IS the executed command

**Rule:** `Step.command` (what the safety gate classifies) and `Step.run` (what executes) must be built from the same string in `TaskSpec.to_steps` and any new way of building a `Step`; the only exception is torra-authored `remote_pull`/`remote_push` text (§2.53). **Guard:** `tests/test_agent.py::test_a_plan_becomes_a_validated_taskspec`.

`Step.command` is what the safety gate reads. `Step.run` is what executes. In
`TaskSpec.to_steps` they are constructed from the same string, deliberately.

A `Step` whose `run` closure executes something different from its `command`
would pass every gate without any gate being wrong. If you add a new way to
build a `Step`, that equivalence is the thing to preserve.

> Guarded by `tests/test_agent.py::test_a_plan_becomes_a_validated_taskspec`

### 2.2 The model never executes anything

**Rule:** The model returns JSON only, with no tool-call loop, shell or filesystem; it proposes a `TaskSpec` and the pipeline decides what happens next. **Guard:** `tests/test_pipeline.py::test_a_hand_written_file_still_works_without_a_model`.

The model returns JSON. It gets no tool-call loop, no shell, no filesystem. It
proposes a `TaskSpec` and the pipeline decides what happens next.

This is not caution for its own sake. Giving the model direct execution would
require every gate reimplemented on a second code path, and the second path is
the one nobody tests.

> Guarded by `tests/test_pipeline.py::test_a_hand_written_file_still_works_without_a_model`,
> which asserts `torra.ollama.calls == 0` on the manual path

### 2.3 Unapproved means refused, never defaulted

**Rule:** `Torra.execute` treats a step that needs confirmation and is absent from the approvals map as refused, never as defaulted to approved. **Guard:** `tests/test_pipeline.py::test_a_dangerous_step_does_not_run_unapproved`.

`Torra.execute` treats a step that needs confirmation and is absent from the
approvals map as **refused**. Do not "helpfully" default it to True for
convenience, unattended runs, or anything else.

> Guarded by `tests/test_pipeline.py::test_a_dangerous_step_does_not_run_unapproved`

### 2.4 Safe steps get no checkbox

**Rule:** In the TUI only DANGEROUS or CRITICAL steps render an approve checkbox; safe steps get none. **Guard:** `tests/test_tui.py::test_a_safe_plan_gets_no_checkbox`.

In the TUI, only steps at DANGEROUS or CRITICAL render an approve box. Adding
one to every step feels more careful and is the opposite: it trains the operator
to tick without reading.

> Guarded by `tests/test_tui.py::test_a_safe_plan_gets_no_checkbox`

### 2.5 Telemetry can never break a run

**Rule:** Every telemetry method catches its own exceptions and records them, and `TaskLogger.__init__` never raises, so a logging failure can never break a run. **Guard:** `tests/test_pipeline.py::test_an_audit_write_failure_is_logged_not_raised`.

Every method in `telemetry.py` catches its own exceptions and records them.
`TaskLogger.__init__` does not raise even when its directory is unusable. A
logger that raises during a deploy is worse than no logger.

> Guarded by `tests/test_pipeline.py`

### 2.6 An undo that cannot be proven is not "undone"

**Rule:** `VerifiedRollback` keeps `undone` (verifier confirmed, or no verifier, reported as `verified=None`), `unverified` (undo ran, verifier says no), `failed` (undo raised) and `cancelled` (operator declined an unsafe undo) distinct, never collapsed; a verifier must confirm the real prior state, not something correlated with it. **Guard:** `tests/test_gates.py::TestVerifiedRollback` and `tests/test_gates.py::TestUndoGeneration::test_mkdir_undo_verify_is_false_when_the_directory_is_left_behind`.

`VerifiedRollback` has three terminal states. `undone` requires the verifier to
return True. An action with no verifier reports `verified=None`, which is honest
about the fact that nothing was checked. Do not collapse these back to two.

Having a verifier is not enough on its own -- the verifier itself has to
actually confirm the prior state, not just something correlated with it.
Found live: `UndoGenerator.capture_mkdir`'s verifier read `not
target.exists() or any(target.iterdir())`. `undo_mkdir` deliberately
refuses to `rmdir` a directory someone filled with real content (a second
disaster otherwise) -- but that same "still exists AND has content" case
made the verifier return `True`, so a directory that was never actually
removed, with whatever got written into it, was reported as `undone`. The
prior state was "did not exist"; only `not target.exists()` honestly
confirms it was restored.

> Guarded by `tests/test_gates.py::TestVerifiedRollback`,
> `test_mkdir_undo_verify_is_false_when_the_directory_is_left_behind`

### 2.7 A declared rollback command is still classified

**Rule:** A `rollback:` entry's `safe` flag can only make `SafetyClassifier`'s verdict more cautious, never less (`Torra.execute` classifies every rollback command), and `TaskSpec.validate()` rejects unknown fields or a missing `run` in rollback entries. **Guard:** `tests/test_pipeline.py::test_a_declared_safe_rollback_is_still_classified` and `tests/test_core.py::TestTaskSpecRollbackValidation`.

A `rollback:` entry's `safe` flag (from a hand-written task or a model's own
proposal) can only make a command *more* cautious than `SafetyClassifier`
would, never less. `Torra.execute` classifies every rollback command
alongside the declaration and requires both to agree it's safe;
disagreement is logged as a decision and the command is treated as unsafe
regardless of what was declared.

This was a real gap until it wasn't: a model declared `rm -rf /srv/api` as
`safe: true` in a real run, and it ran to completion with zero
classification and zero confirmation. `TaskSpec.rollback_actions()` now
returns the raw command alongside the callable specifically so this check is
possible -- if you add a new way to build rollback actions, keep that
equivalence, the same way `Step.command`/`Step.run` has to stay in sync.

> Guarded by `tests/test_pipeline.py::test_a_declared_safe_rollback_is_still_classified`

A declared rollback entry has to actually survive `TaskSpec.validate()` to
matter at all, and until now it didn't have to. Found live: a rollback
entry with a `run` typo (e.g. `cmd` instead of `run`) validated cleanly --
`validate()` never checked `rollback` entries the way it checks `steps` --
and `rollback_actions()` silently `continue`d past it with no error. The
operator who wrote it believed they had a declared safety net for a
destructive step; they had none, discoverable only by reading the task
file back by hand or watching the rollback not fire during a real
failure. Rollback entries now go through `_validate_rollback_entry`,
the same strict "unknown field or missing `run` is a hard error" discipline
`_validate_step` already applied to `steps`.

> Guarded by `tests/test_core.py::TestTaskSpecRollbackValidation`

### 2.8 `execute()` cannot be replayed on a result that already ran for real

**Rule:** `Torra.execute` refuses to replay a `RunResult` whose `executed` is True; `executed` is set only just before a real `step.run()` (or when a resumed step's earlier run is recorded), so rejected, dry-run-only and blocked-before-any-step results stay retryable. **Guard:** `tests/test_pipeline.py::test_execute_twice_on_the_same_result_does_not_replay_it` and 3 more below.

`Torra.execute` refuses immediately if `result.executed` is already
`True` -- it logs a `"refused: already executed"` decision and returns
the result unchanged instead of re-running `plan.steps`. `executed` is a
dedicated `RunResult` field, set only once a non-dry-run step is actually
about to run (inside the loop, immediately before `sp.step.run()`); it is
deliberately **not** derived from `outcome` and deliberately **not** set
just for entering the loop.

Found live, twice, then once more in a full-file audit, all in the same
session:

1. The TUI's Run/Dry-run buttons stayed clickable while a plan executed,
   so a double-click dispatched `execute()` twice on the same
   `RunResult`, and the second call happily re-ran every step's command
   against the live shell (visible in a real audit log as
   `stages: [..., "execute", "report", "execute"]` and two result entries
   for one step). The TUI now also disables those buttons while busy, but
   that's the mitigation, not the fix -- the guard belongs in the pipeline
   because it is the only place that protects every caller (TUI, CLI,
   direct API use) at once.
2. The first version of the guard keyed off `result.outcome !=
   AWAITING_APPROVAL`, which also matches `REJECTED` -- so ticking a
   checkbox after Run was clicked too early (before approving) and
   clicking Run again was refused too, even though a rejection means
   *zero* commands ran. `executed` fixes this by tracking "did a real
   command actually get attempted," not "did this result leave its
   initial state." A `REJECTED` or dry-run-only result leaves `executed`
   `False` and can still be retried; once a real run begins, it cannot.
3. The fix for #2 still set `executed = True` unconditionally right
   before the step loop, not inside it -- so a plan `BLOCKED` by the
   precondition/conflict check on step one, before any command ran, was
   *also* marked "already executed" and could never be retried once the
   underlying condition (a dead service, an external file change) was
   resolved. Moved the assignment to immediately before `sp.step.run()`
   is actually called, so a block with zero results behaves like
   `REJECTED`: retryable.

`result.results` is also reset at the start of every attempt that gets
past the approval check (dry or real) -- without that, a dry run followed
by the real run it previewed would leave both the dry-run stub output and
the real output in the same list.

> Guarded by `tests/test_pipeline.py::test_execute_twice_on_the_same_result_does_not_replay_it`,
> `test_ticking_the_box_after_a_rejection_still_runs`,
> `test_a_dry_run_does_not_block_the_real_run_that_follows`, and
> `test_a_block_before_any_step_ran_is_still_retryable`

### 2.9 A model's size and chat capability come from the daemon, never the name

**Rule:** Model size and chat capability come from the daemon's `/api/show` data whenever it is present; only size (`ModelRouter._params_b`, `Accelerator.fits`) falls back to a guess from the model name when that data is missing, and `ModelRouter._chat_capable` never guesses from the name (a model with no data defaults to chat-capable). **Guard:** `tests/test_core.py::TestModelInfo` and `tests/test_gates.py::TestModelParamsAndChatCapable`, and 3 more below.

`ModelRouter._params_b` and `_chat_capable` consult `model_params_b` /
`model_chat_capable` (real `/api/show` data, `OllamaClient.model_info`)
before falling back to guessing from the model's name. `Accelerator.fits`
(`torra/core/machine.py`) does the same via its own `model_params_b`
field. Both guesses exist only for when `/api/show` data is missing
(daemon unreachable, or a model that failed a lookup) -- if it's present,
it wins, unconditionally.

Found live: the operator's model catalog grew from three known-good names
to sixteen, and two of the "unknown-shaped" ones broke routing outright.
"qwen3.5" and "deepseek-r1-heavy" carry no digit, so the name guess read
both as 0B -- the same "looks like it fits anywhere" failure the
`KNOWN_PARAMS_B`/`KNOWN_MODEL_PARAMS_B` tables exist to prevent, just on
names nobody had added ahead of time. Worse: "nomic-embed-text" is an
*embedding-only* model with `capabilities: ["embedding"]` and no
`"completion"` at all -- no amount of size information would have made it
a safe chat target, and the old VRAM-starved downgrade fallback (picking
the smallest by guessed size) put it right at the top, because 0B always
looks smallest. A real operator's disk-usage question landed on it (or on
an oversized 14.8B reasoning model that likewise guessed as 0B and so
looked "runnable" regardless of free VRAM) and came back broken or
effectively hung. Routing a request onto an embedding model or a model
that doesn't fit is not a quality problem to prompt-tune around -- it is
this invariant not holding.

> Guarded by `tests/test_core.py::TestModelInfo`,
> `tests/test_gates.py::TestModelParamsAndChatCapable` and the two
> `test_a_downgrade_never_...`/`test_score_never_routes...` regression
> tests in `TestRouting`, and
> `tests/test_agent.py::test_observe_populates_the_routers_real_model_sizes`

### 2.10 Every way to decline a plan goes through the pipeline, and only once

**Rule:** Every way to decline a plan goes through `Torra.reject()`, never `execute()` with empty approvals; `reject()` finishes via `_finish()` and refuses once `result.executed` is True. **Guard:** `tests/test_pipeline.py::test_reject_never_runs_an_all_safe_plan` and 4 more below.

`Torra.reject(result)` exists specifically because `execute(result,
{})` is **not** a safe way to implement "decline this plan": a step's
`approved` defaults to `True` whenever it doesn't need confirmation, so
calling `execute` with empty approvals on an all-safe plan does not
refuse it, it *runs* it. `reject()` sets `outcome = REJECTED`
unconditionally and calls `_finish()`, the same choke point every other
terminal outcome goes through -- audit record written, telemetry
recorded (§6.1), decision logged.

Found in a full-file audit: the TUI's Reject button used to bypass all
of that -- `self.current = None` and nothing else, leaving the single
most direct expression of a decline with no trace anywhere. If you add
another way to decline a plan, route it through `Torra.reject()`, not
through `execute()` with an empty approvals dict and not through UI
state alone.

`reject()` also refuses outright when `result.executed` is already
`True` -- the same double-execute class of bug as §2.8, found in a
second full-file audit of the TUI's presentation layer: unlike
Run/Dry-run, the Reject button was never disabled once a plan started or
finished running, and `reject()` had no guard against overwriting an
already-executed result's outcome. A stray click after a real run would
have set `outcome = REJECTED` and written a fresh audit record claiming
"nothing ran" over a run that may have had real, destructive effects --
not a double-execution risk this time, but the same-shaped failure: the
one record meant to be a truthful account of what happened, made false
after the fact.

> Guarded by `tests/test_pipeline.py::test_reject_never_runs_an_all_safe_plan`,
> `test_reject_refuses_to_overwrite_a_plan_that_already_ran`,
> `test_reject_still_works_on_a_plan_that_never_ran`, and
> `tests/test_tui.py::test_reject_records_the_decision`,
> `test_reject_after_a_real_run_does_not_lie_about_it`

### 2.11 The post-execution summary call can report, never act

**Rule:** The post-execution `summarize()` call can only report: its reply is plain text (`json_mode=False`, never parsed into a `TaskSpec`), it runs only when `plan.model` is set, and an exception in it is caught without failing the run. **Guard:** `tests/test_agent.py::test_summarize_sends_step_output_as_plain_text_not_json` and 4 more below.

`pipeline_report.summarize()` (called once from `_finish()`, after a model-authored
plan has actually executed) is the only place execution results are ever fed
back to the model. It exists to close a real gap -- `_print_execution_report`
never showed step output, so "read the docs and tell me" completed with
nothing readable printed -- but it sits directly next to §2.2 ("the model
never executes anything") and must never grow into a second one. Three
things keep it that way, and any change here has to preserve all three:
`Agent.summarize()` calls `ollama.chat(..., json_mode=False)`, so the reply
is plain text, never fed through `_parse()`/`_extract_json()` and never able
to become a `TaskSpec`; `_finish()` only calls it when `result.plan.model` is
truthy, so a hand-written task file (`plan.model == ""`) still makes zero
model calls (§2.2's own guarding test asserts exactly this); and a raised
exception is caught and logged, never allowed to flip a completed run to
failed. If a future change makes this call's reply drive anything but a
string shown to the operator, that is the second code path this project has
repeatedly found and removed elsewhere -- see §7, question 2.

> Guarded by `tests/test_agent.py::test_summarize_sends_step_output_as_plain_text_not_json`,
> `test_summarize_reports_a_failed_step_by_error_not_output`,
> `tests/test_pipeline.py::test_a_completed_run_gets_a_synthesized_summary`,
> `test_a_hand_written_plan_gets_no_summary_and_consults_no_model`,
> `test_a_failed_summarize_call_does_not_break_the_run`

### 2.12 Every filename built from `task_id` goes through `safe_filename`

**Rule:** Every filename built from `task_id` (the resume file and the audit file) goes through `pipeline_report.safe_filename()`, and any new place that turns `task_id` or another model-supplied string into a filename must use it too, so it cannot write outside its directory. **Guard:** `tests/test_pipeline.py::test_a_malicious_task_id_cannot_write_the_resume_file_outside_the_workdir`.

`task_id` is fully model- or human-controlled: `TaskSpec.validate()` does
`task_id=str(data["task_id"])` with no character restriction at all, and a
model's own JSON `task` object supplies it directly (`Agent._parse` only
defaults it when the model omits the field). Found live: `execute()`'s
resume-file path was built as `self.workdir / "resume" / f"{task_id}.json"`
with no sanitizing, right next to `_finish()`'s audit-file path in the same
class, which already sanitized. A `task_id` of `"../../../../tmp/evil"`
wrote a real file outside `workdir/resume/` on any run where a later step
failed before `_finish_success()`'s cleanup ran (that only fires on a fully
successful run). `pipeline_report.safe_filename()` (one helper both call sites now
share) replaces every character outside alnum/`-`/`_`/`.` -- `/` is the
only character that can turn one path component into several, and with it
gone a leftover `..` is just a funny filename, not a traversal. If you add
a third place that turns `task_id` (or any other model-supplied string)
into a filename, route it through this helper rather than re-deriving the
character set by hand.

> Guarded by `tests/test_pipeline.py::test_a_malicious_task_id_cannot_write_the_resume_file_outside_the_workdir`

### 2.13 A step's DOM id in the TUI is never built from `step_id` itself

**Rule:** A step's DOM id in the TUI is built from the card's 1-based index, never from `step_id`; `_approvals()` reads the real id from `StepCard.plan_step.step.step_id`. **Guard:** `tests/test_tui.py::test_a_step_id_with_a_space_does_not_crash_the_plan_pane`.

`StepCard.compose()`'s approval `Checkbox` needs a Textual DOM id, and DOM
ids must match `[A-Za-z0-9_-]+` -- `sp.step.step_id` is arbitrary model- or
human-chosen text with no such restriction (a space, period, or colon is
entirely ordinary; nothing in `TaskSpec.validate()` or the model's schema
hint enforces one). Found live: embedding `step_id` directly into the id
(`id=f"approve-{sp.step.step_id}"`) raised `BadIdentifier` out of
`Checkbox`'s own constructor for a step id as plausible as `"delete old
logs"`, crashing the whole plan pane before a human ever saw it -- and only
DANGEROUS/CRITICAL steps get a checkbox at all (§2.4), so this hit exactly
the highest-risk steps the review UI exists to gate. Fixed by building the
DOM id from the card's own 1-based index instead (always safe), and
escaping the label text the same way every other field in this method
already is. `_approvals()` was reading the real `step_id` back out of that
same DOM id string -- it now reads it off `StepCard.plan_step.step.step_id`
directly, which was always the actual source of truth and didn't need the
DOM id as a relay in the first place.

> Guarded by `tests/test_tui.py::test_a_step_id_with_a_space_does_not_crash_the_plan_pane`

### 2.14 `TaskRecord.approved` requires every step to be `True`, not merely not-`False`

**Rule:** `TaskRecord.approved` is true only when every step's `approved` is `True`; `None` (never reached approval, for example a BLOCKED plan) records as not approved. **Guard:** `tests/test_pipeline.py::test_a_precondition_blocked_plan_is_not_recorded_as_approved` and 1 more below.

Same underlying principle as §2.3 ("unapproved means refused, never
defaulted to True"), recurring in the telemetry read path instead of the
execution path. `TaskLogger._apply_plan_fields()` computed `approved = all(sp.approved
is not False for sp in plan.steps)` -- `StepPlan.approved` is `None` until
`Torra._apply_approvals()` actually runs, which only happens inside
`execute()`. A plan blocked by a failed precondition never reaches
`execute()` at all (`Torra.plan()` calls `_finish()` directly), so every
step's `approved` stays `None` -- and `None is not False` is `True`, so a
`BLOCKED` task nobody ever saw was persisted with `approved: true`.
`_apply_approvals()` always resolves every step to a real `True`/`False`
once it runs (a step needing no confirmation is auto-`True`), so requiring
`is True` for every step is exactly as strict for a normal completed run
and correctly `False` for anything that never reached approval. No
downstream reader consumes this field today, so this was a silent
data-quality bug rather than a live break -- but it is exactly the kind of
thing `torra evidence`/model-quality analysis (§6.1) would eventually
rely on, and it would already have been wrong.

> Guarded by `tests/test_pipeline.py::test_a_precondition_blocked_plan_is_not_recorded_as_approved`,
> `test_a_normally_completed_plan_is_still_recorded_as_approved`

### 2.15 `TaskSpec.validate()` type-checks every field, not just presence

**Rule:** `TaskSpec.validate()` type-checks every field: `touches`/`watch_roots` must be lists, `estimated_seconds` a number and a rollback `safe` a real boolean, each raising `TaskSpecError` at validation time. **Guard:** `tests/test_core.py::TestTaskSpecStepValidation` and `tests/test_core.py::TestTaskSpecRollbackValidation::test_a_quoted_string_safe_value_is_rejected`.

Found by an independent audit of `tasks.py`, reproduced live, three related
gaps in the one gate between an arbitrary mapping (hand-written YAML or a
model's JSON) and a `TaskSpec` that drives root shell commands:

1. **`touches`/`watch_roots` accepted a bare string.** `to_steps()`'s
   `[str(t) for t in value]` doesn't raise on a string -- Python iterates
   a string character by character, so `touches: /etc/hosts` (missing
   brackets, an easy slip: the model's own schema example in `agent.py`
   shows a *single-element list*) silently became `['/', 'e', 't', 'c',
   ...]`. Both `validate()` and `to_steps()` returned successfully;
   `ConflictDetector` then baselined that garbage instead of the real
   path, so the exact step that declared it needed watching was invisible
   to the conflict-detection safety net, and `Agent._prescreen()`'s
   "DANGEROUS step with no touches" repair check never fired either,
   since the (garbage) list was non-empty.
2. **`estimated_seconds` accepted anything.** `to_steps()`'s unguarded
   `float(value)` raised a bare `ValueError` at plan-time -- past
   `Torra.plan()`'s narrow `except OllamaError | AgentError` clause,
   skipping every decision-log/audit/telemetry call that catch normally
   reaches on the way to a proper `Outcome`. Only caught by `cli.py`'s or
   `tui.py`'s top-level catch-all, several layers removed from where the
   real cause was.
3. **A rollback entry's `safe: "false"` (quoted string) meant `true`.**
   `bool("false")` is `True` in Python -- an easy JSON-formatting slip for
   a model -- silently meaning the opposite of what §2.7's `safe: false`
   exists to say ("do not trust the classifier alone here").

All three now raise `TaskSpecError` in `validate()`/`_validate_step`/
`_validate_rollback_entry`, the same place every other malformed-field
case already raises, rather than downstream where the context to explain
it is gone.

> Guarded by `tests/test_core.py::TestTaskSpecStepValidation` (5 cases),
> `TestTaskSpecRollbackValidation::test_a_quoted_string_safe_value_is_rejected`

### 2.16 A watched path is opened only after confirming it's a regular file

**Rule:** `ConflictDetector.hash_file()` and `_scan_knowledge_dir()` open a declared path only after `stat.S_ISREG(path.stat().st_mode)` confirms a regular file (symlinks followed), skipping FIFOs, devices, sockets and directories; any new place that opens a model- or operator-declared path must add the same check first (`UndoGenerator.capture_file` does not have it). **Guard:** `tests/test_core.py::TestHashFileSkipsNonRegularFiles` and `tests/test_core.py::TestKnowledgeLibrary::test_a_fifo_named_dot_md_is_skipped_instead_of_hanging`.

Found by an independent security audit, reproduced live: `ConflictDetector.hash_file()`
opened whatever `Path.open("rb")` was pointed at with no check on what kind
of filesystem entry it actually was. Opening a FIFO for reading with no
writer blocks **forever** -- and `baseline()` runs during `Torra`'s GATE
stage, before any operator ever sees the plan (`pipeline.py`'s `_gate()`
baselines every `touches` path a model's own JSON proposal declares). A
single stray FIFO at a model-declared `touches` path -- no malice required,
any real FIFO already present at that path works -- wedged the entire
torra with no timeout anywhere in the call chain. `_scan_knowledge_dir()`
(`machine.py`) had the identical flaw for a FIFO named `*.md` in a
configured knowledge directory, and since `Agent.observe()` runs it on
essentially every conversation turn, one stray FIFO there wedged every
turn after it, not just one.

Both now check `stat.S_ISREG(path.stat().st_mode)` before opening --
`.stat()` (not `.lstat()`) still follows symlinks to their real target, so
a symlink pointing at a legitimate regular file is still hashed/read
normally; only a symlink (or direct path) whose *final* target is a FIFO,
device, socket, or directory is skipped. If you add a third place that
opens a model- or operator-declared path for reading, add the same check
first -- `open()` on an arbitrary path is not safe by default in this
codebase's threat model.

> Guarded by `tests/test_core.py::TestHashFileSkipsNonRegularFiles` (5 cases,
> including a symlink-to-FIFO case), the `_scan_knowledge_dir` FIFO case in
> `TestKnowledgeLibrary`

### 2.17 `OllamaClient` validates response *shape*, not just transport success

**Rule:** `OllamaClient.model_info()` and `chat()` validate the response shape inside the same `try` as the transport call, so a wrong-shaped reply yields `{}` or `OllamaError`, never a raw exception. **Guard:** `tests/test_core.py::TestModelInfo::test_a_response_of_the_wrong_shape_returns_an_empty_dict_not_a_crash` and `tests/test_core.py::TestChatResponseShape`.

Found by an independent security audit, reproduced live: `model_info()` and
`chat()` each wrapped only the transport call in `try/except`; everything
that parsed the response body afterward (`data.get(...)`, `"completion" in
capabilities`) ran unguarded. Both methods' own docstrings promise a
contract for failure (`model_info`: "Returns `{}` on any failure";
`chat`: "Raises `OllamaError` on transport failure or an unusable
response") -- neither held for a response that was valid JSON in the wrong
*shape* (a list instead of a mapping; `capabilities` as a non-iterable
int). That's exactly the kind of input a misbehaving daemon can return --
`OllamaClient`'s own docstring already frames "Ollama" as something that
can be pointed elsewhere, not necessarily trustworthy by construction.
Confirmed live end-to-end: a scripted `/api/show` returning
`{"capabilities": 42}` made `Torra.plan()` raise a raw uncaught
`TypeError` instead of returning `Outcome.BACKEND_DOWN` with
`recovery_options` -- `plan()`'s own `except OllamaError | AgentError`
clause (§2.2's home) never saw it, because the exception wasn't an
`OllamaError` at all.

Both methods now validate `isinstance(data, dict)` and do the rest of
their parsing inside the same `try` that already wrapped the transport
call, so any shape mismatch degrades exactly the way the docstring already
promised, instead of a new, undocumented failure mode past it.

> Guarded by `tests/test_core.py::TestModelInfo::test_a_response_of_the_wrong_shape_returns_an_empty_dict_not_a_crash`,
> `TestChatResponseShape` (3 cases)

### 2.18 `resolve()` recurses into command substitutions, and IFS is a separator

**Rule:** `SafetyClassifier.resolve()` recurses into every `$(...)` and backtick span (decoding base64 there without needing `| sh`) and treats `${IFS}`/`$IFS` as a space; any newly found way text becomes the running command is added as another source `resolved` accumulates from. **Guard:** `tests/test_gates.py::TestSafety::test_command_substitution_and_ifs_evasions_are_critical_not_just_gated` and `tests/test_gates.py::TestSafety::test_every_evasion_is_caught`.

The single most severe finding an independent audit found this session,
against `SafetyClassifier` -- the primary defense, the thing standing
between a hallucinated command and the disk. Two combined evasions
classified `$(echo cm0gLXJmIC8=|base64 -d)` (decodes to `rm -rf /`) as
**SAFE -- auto-run, zero confirmation**:

1. `split_segments` doesn't track paren/backtick nesting, so it split on
   the `|` *inside* `$(...)`, fracturing the payload before either half
   could match anything dangerous.
2. `_decode_base64`'s trigger required an explicit `| sh` in the text --
   but a command-substitution span's captured stdout becomes the command
   that runs next *without* ever literally piping to a shell name.

Fixed with two additions to `resolve()`, both recursive the same way
`sh -c` payloads already are: `_find_command_substitutions()` scans for
every `$(...)`/backtick span (a small paren-balance scanner,
`_matching_paren`, factored out the same way `Agent._matching_brace`
already handles JSON braces); `_resolve_substitutions()` recurses `resolve()`
into each span's inner text, and separately tries `_decode_base64()` on it
with a new `require_pipe_to_shell=False` mode -- being inside a
substitution span already proves the decoded output will run, the same
"will execute" property `| sh` proves explicitly. Deliberately ignores
quote context (a `$(...)` inside single quotes wouldn't actually expand in
real bash): a false positive here costs one unneeded confirmation prompt;
a false negative is the exact bypass this closes.

A second, related evasion found in the same audit: `rm${IFS}-rf${IFS}/`
downgraded from CRITICAL to DANGEROUS (still gated, but the wrong severity
and the wrong reason) -- `${IFS}`/`$IFS` between tokens is a well-known
obfuscation (bash expands IFS, default space/tab/newline, then
field-splits on it), but a plain whitespace tokenizer saw one token and
`rm` never resolved as the program. `_expand_ifs()` substitutes `${IFS}`/
`$IFS` for a literal space before tokenizing each segment.

If you find a *third* way text can become "the command that actually
runs" without appearing as a literal top-level program name (process
substitution `<(...)`, here-docs, `exec`, unicode homoglyphs), add it to
`resolve()` the same way -- as another source `resolved` accumulates from,
never as a replacement for what's already there. `classify()`'s own design
(§2's "three independent signal sources... max across all of them") is
what limits the blast radius of the next gap someone finds the same way
this one was.

> Guarded by `tests/test_gates.py::TestSafety::test_every_evasion_is_caught`
> (26/26; see §2.22 for the other two), `test_command_substitution_and_ifs_evasions_are_critical_not_just_gated`

### 2.19 A dry run never reaches `summarize()`

**Rule:** A dry run never reaches `summarize()`: `_finish()` takes a `dry_run` flag and skips the summary call, so fabricated `[dry-run]` step output is never presented as a result. **Guard:** `tests/test_pipeline.py::test_a_dry_run_never_gets_a_summary`.

Found by an independent audit, reproduced live: `_run_one_step()` fabricates
`"[dry-run] <command>"` as every step's "output" under a dry run -- nothing
was actually checked. `_finish()`'s summarize gate checked only
`plan.model`/`result.results`, never `dry_run`, so that fabricated text got
handed to `Agent.summarize()` the same as real output. The model has no
real evidence to work from, only command text, but produces a confident,
plausible-sounding answer anyway -- confirmed live: a dry-run plan
proposing `ls -la ~/.ssh` came back with `"Your SSH keys are present and
correctly configured; no action needed."`, indistinguishable from a
genuine post-execution summary to an operator reading it. An operator
using `--dry-run`/Ctrl+D specifically to preview a task safely could be
told a fabricated "fact" about their system -- the opposite of what a dry
run is supposed to guarantee.

`dry_run` is a parameter local to `execute()`, never stored on `RunResult`
itself, so `_finish()` couldn't have known either way before this: it's
now threaded through as `_finish()`'s own `dry_run` parameter, defaulting
`False` everywhere except the one call site inside `execute()` where it
matters. Excluded entirely, not just labeled -- a dry-run preview has
nothing real to summarize, so there's nothing worth hallucinating an
answer from.

> Guarded by `tests/test_pipeline.py::test_a_dry_run_never_gets_a_summary`

### 2.20 The degraded-status model fallback consults chat-capability, not just the name

**Rule:** `Agent._degraded_fallback()` filters candidates through `ModelRouter._chat_capable()` before taking listing order, and uses the full list only when no candidate is known chat-capable. **Guard:** `tests/test_agent.py::test_degraded_fallback_skips_a_model_the_router_knows_cannot_chat`.

Found by an independent audit, reproduced live: `Agent.converse()`'s
degraded/unknown-status fallback (`self.ollama.last_status.value !=
"healthy"`, true on essentially every session's first turn) falls through
to `available[0]` -- whatever the daemon lists first -- when neither the
router's choice nor `fallback_model` is pulled. `model_available()` is a
pure name-membership check with zero capability awareness. Confirmed
live: with an embedding-only model listed *first* in the catalog and a
real chat model listed second, this picked the embedding model and sent
it a real `/api/chat` request -- reintroducing, through a second,
hand-rolled selection path, exactly the failure §2.9 already fixed once
for the primary routing path ("nomic-embed-text... no amount of size
information would have made it a safe chat target").

`Agent._degraded_fallback()` now filters candidates through
`ModelRouter._chat_capable()` (already daemon-first, already populated by
the `observe()` call immediately before this runs) before falling back to
listing order -- reusing the existing check rather than a second,
driftable copy. Falls back to the full `available` list only when *no*
candidate is known chat-capable, matching the existing "unknown defaults
to capable" stance so this can't block every model when `/api/show`
metadata is missing.

> Guarded by `tests/test_agent.py::test_degraded_fallback_skips_a_model_the_router_knows_cannot_chat`

### 2.21 `TestConflictDetectorFastPath` was not testing the fast path

**Rule:** A guard test must actually reach the branch it names: `TestConflictDetectorFastPath` builds the detector with `use_inotify=True` (stubbing `_start_inotify`) so `_detect_tree()` takes the event path; check a fix's test by breaking the fix. **Guard:** `tests/test_core.py::TestConflictDetectorFastPath`.

Found while raising `torra/core/conflict.py`'s test coverage (59% -> 74%,
the single worst-covered file in the tree), not by reading the test in
isolation: its shared `_detector()` fixture built the detector with
`use_inotify=False`. `_detect_tree()`'s own guard --
`if not self.use_inotify or not self._inotify or self._since_walk >=
self.reconcile_every: return self._reconcile()` -- means `not
self.use_inotify` alone was enough to always take the full-walk branch,
regardless of the `detector._inotify = True` truthy stand-in the fixture
set up right after. `_reconcile()` rebuilds `self._known` from scratch on
every call, so the exact stale-conflict bug this test class exists to
guard (§5.4: the fast path must clear `_known`, not just add to it)
**cannot reproduce there at all** -- both tests passed, but never reached
the event-based per-path accumulate/pop logic the fix is actually in.

Confirmed by breaking the real fix (temporarily reverting
`self._known.pop(path, None)` to a no-op) and watching both tests still
pass under the old fixture, then fail correctly under the fixed one.
Fixed by constructing with `use_inotify=True` and stubbing
`_start_inotify` to a no-op (so `baseline()` doesn't open a real inotify
file descriptor in a test) -- `self.use_inotify` now stays `True`, the
guard's first condition never fires, and `_detect_tree()` actually takes
the fast path these tests are named for.

The lesson generalizes: a green test with a name that promises coverage
of one code path is not proof that path actually ran. When a fix lives in
a specific branch, verify the test reaches that branch (a counter, a spy,
or -- as here -- literally breaking the fix and watching the test catch
it) rather than trusting the test's own docstring.

> Guarded by the same two tests, now actually exercising `_detect_tree()`'s
> event branch: `tests/test_core.py::TestConflictDetectorFastPath`

### 2.22 `su -c` and Perl's bareword `unlink` both evaded the classifier

**Rule:** `su` is in `INTERPRETERS`, so `resolve()` recurses into `su -c` payloads, and the script-source check matches Perl's bareword `unlink` (`\bunlink\b`); plain `su - user` stays SAFE. **Guard:** `tests/test_gates.py::TestSafety::test_su_c_wrapping_a_nested_interpreter_call_is_still_caught` and `tests/test_gates.py::TestSafety::test_every_evasion_is_caught`.

Found live 2026-08-12, then recovered from an abandoned `git worktree`
that was never merged into `main` -- the fix sat idle for weeks while
`resolve()`/`classify()` were independently hardened further (§2.18's
command-substitution/IFS fix among them), so it had to be re-verified
against the current file rather than merged blindly.

Two independent gaps, same audit:

1. `su` was in neither `WRAPPERS` nor `INTERPRETERS`. `su -c 'rm -rf /x'`
   resolved to program `"su"`, no `PROGRAM_RISK` entry, no `_judge` rule
   -- silently SAFE unless the regex BACKSTOP happened to still see the
   literal text (it usually does for a bare `rm -rf`, which is exactly
   why this is a plausible thing to have shipped for weeks without
   anyone noticing: the direct case still got caught by the backstop most
   of the time). Adding `su` to `INTERPRETERS` makes `resolve()` recurse
   into its `-c` payload as shell syntax, the same as `sh -c`'s. This also
   structurally fixes a second, worse case the backstop genuinely never
   caught at all: `su -c "python3 -c 'import os; os.system(...)'" user` --
   the nested interpreter call was invisible to both `_judge` (opaque `su`
   argv) and the BACKSTOP (never checked for `os.system` inside a nested
   payload like this).
2. `_judge_script_source`'s regex required a leading dot (`\.unlink\(`,
   matching Python's `os.unlink(...)`), so Perl's bareword
   `unlink(...)`/`unlink glob(...)` -- no dot -- never matched. Generalized
   to `\bunlink\b`.

Verified against the current file (not the state it was written against):
plain `su - user` (no `-c`) still classifies SAFE, no false positive;
`su -c` wrapping a direct `rm -rf` and wrapping a nested `python3 -c
os.system(...)` both classify as needing confirmation.

> Guarded by `tests/test_gates.py::TestSafety::test_every_evasion_is_caught`
> (now 26/26), `test_su_c_wrapping_a_nested_interpreter_call_is_still_caught`

### 2.23 Multiple `Torra` instances sharing a `workdir` need session-scoped `undo=` and `task_id_prefix`

**Rule:** Every caller building more than one `Torra` on the same `workdir` passes a session-scoped `undo=UndoGenerator(shadow_dir=...)` and a distinct `task_id_prefix`; telemetry, `audit` and `resume` directories stay shared. **Guard:** `tests/test_pipeline.py::test_two_torras_sharing_a_workdir_do_not_collide_on_shadow_files` and `tests/test_agent.py::test_task_id_prefix_reaches_the_default_task_id`.

Found during design review for multi-tab TUI sessions, before any tab code
existed -- these are latent collisions that only matter once more than one
`Torra` can share a `workdir` in one process, which nothing did before
this feature.

1. **Shadow-dir collision.** `UndoGenerator`'s default shadow_dir
   (`pipeline.py`'s `self.undo = undo or UndoGenerator(shadow_dir=self.workdir
   / f"torra-shadow-{os.getpid()}")`) is keyed only by `(workdir, pid)` --
   two `Torra` instances built with the same `workdir` in the same process
   collide on it. Confirmed live (not just reasoned about): one session's
   `UndoGenerator.cleanup()` really does `shutil.rmtree` the other session's
   shadow copies out from under it, breaking that session's rollback
   guarantee mid-run. Fix: every caller building more than one `Torra`
   against the same `workdir` must pass an explicit, session-derived
   `undo=UndoGenerator(shadow_dir=workdir / f"torra-shadow-{session_id}")`
   -- `Torra.__init__` needed no change, `undo` was already injectable.
2. **`task_id` collision.** `Agent`'s fallback `task_id`
   (`f"agent-{self._turn_counter}"`) restarts at 0 per `Agent` instance, so
   two sessions' first turns both default to `task_id="agent-1"` --
   **deterministically**, not as an edge case -- silently overwriting each
   other's `workdir/audit/agent-1.audit.json`. Fix: `Agent.__init__` gained
   `task_id_prefix: str = ""`, applied only at the one `setdefault` fallback
   line, never to a model-chosen `task_id` (a model choosing the same id
   across two tabs remains a low-probability residual risk, not meaningfully
   worse than today's single-session behavior, and deliberately out of
   scope). Every caller building more than one `Agent`/`Torra` against the
   same `workdir` should pass a distinct `task_id_prefix` per session.

Telemetry and the `audit`/`resume` directories deliberately stay **unscoped**
(shared across every session on one `workdir`) -- that's what keeps `torra
evidence`/`torra label` seeing everything across every tab instead of
fragmenting per-session. Only the two collisions above needed fixing; do not
over-scope by giving every session its own subtree of `workdir`.

> Guarded by `tests/test_pipeline.py::test_two_torras_sharing_a_workdir_do_not_collide_on_shadow_files`,
> `tests/test_agent.py::test_task_id_prefix_reaches_the_default_task_id`

### 2.24 A tab's plan/execute worker must capture its `session_id` at dispatch time, not read "active" at completion time

**Rule:** Each TUI tab's plan/execute worker callbacks take `session_id` as an explicit argument captured at dispatch, never reading `self.active_session_id` to pick which session's state to change; a busy tab cannot be closed. **Guard:** `tests/test_tui.py::test_plan_request_runs_in_a_per_session_worker_group` and `tests/test_tui.py::test_close_tab_refuses_a_busy_tab`.

The TUI's tabs each run their own `Torra.plan()`/`.execute()` in a
background thread (`tui.py`'s `plan_request`/`execute_plan`, via
`self.run_worker(..., group=f"session-{session_id}")`). The easy mistake --
not made here, but worth naming so nobody reintroduces it -- is reading
`self.active_session_id` *inside* the worker's completion callback
(`_planned`/`_executed`) instead of taking `session_id` as an explicit
parameter closed over at the moment the worker was dispatched. An operator
who switches tabs while tab A's plan is still running would otherwise have
tab A's result silently written into whichever tab happens to be active
when the callback fires -- not the tab that actually asked for it. Every
handler in the chain (`_plan_request_body`, `_planned`, the `_outcome_*`
dispatch table, `_execute_plan_body`, `_executed`, `_set_busy`,
`_tick_spinner`) takes `session_id` explicitly and never substitutes
`self.active_session_id` for it. The one place `active_session_id` is
correct to read is UI code that only ever affects *what's currently on
screen* (`_status`'s "only write the status line if this is the active
tab" guard) -- never for deciding which session's *state* to mutate.
Closing a tab (`action_close_tab`) has the same race in miniature: it
refuses to close a *busy* tab specifically so `del self.sessions[session_id]`
can never run while that session's worker callback is still in flight and
would otherwise `KeyError` on lookup.

> Guarded by `tests/test_tui.py::test_plan_request_runs_in_a_per_session_worker_group`,
> `tests/test_tui.py::test_close_tab_refuses_a_busy_tab`

### 2.25 A session's name is arbitrary text -- it cannot be handed to Textual's `Tab`/`TabPane` as a raw string

**Rule:** A session name is free text and must be wrapped in `textual.content.Content(...)` before it reaches Textual's `TabPane` or `Tab.label`, never passed as a raw string. **Guard:** `tests/test_tui.py::test_the_first_request_names_the_session`.

Found live while wiring tab auto-naming from the first request: a session's
tab title (`SessionMeta.name`, defaulted from the first ~40 characters of
whatever the operator typed) is ordinary free text -- "list files in
`[/mnt/x]`" is exactly as plausible a request as any other. `TabPane(title,
...)` and `Tab.label = ...` both parse a plain `str` argument as Textual's
own markup language (`Content.from_markup`), which is a *different* syntax
from the Rich console markup the rest of `tui.py` already escapes for
(`rich.markup.escape`, used throughout `ChatLine`/`StepCard`). A name
containing an unmatched `[...]` raised `MarkupError` out of the tab's own
constructor, before anything was ever mounted -- crashing the whole app on
the very first request in a session, not on some rare malformed input.
Fix: wrap the text in a `textual.content.Content(...)` instance instead of
passing a raw string -- `Content.from_markup`/`render_str` both return a
`Content` instance unparsed if it's already one, so this bypasses markup
parsing entirely rather than trying to escape Textual's markup syntax
(which has its own, separate escaping rules from Rich's). Applies at both
call sites: `_build_tab_pane`'s initial `TabPane(Content(title), ...)` and
`_name_session`'s `tab.label = Content(name)`.

> Guarded by `tests/test_tui.py::test_the_first_request_names_the_session`

### 2.26 A `Torra`/`Agent` reused across many turns needs per-turn scoping it never needed before

**Rule:** A `Torra`/`Agent` reused across turns needs per-turn scoping: non-plan audit ids carry `task_id_prefix` and `_turn_counter` (fast-forwarded on reopen), `DecisionLog.clear()` runs at the start of `plan()`, a reopened transcript is re-compressed to budget, and `SessionStore.rename()` never raises. **Guard:** `tests/test_pipeline.py::test_two_sessions_non_plan_turns_do_not_collide_on_the_same_audit_file` and `tests/test_pipeline.py::test_a_new_plan_call_starts_a_fresh_decision_trail`, and 6 more below.

Found by a full-codebase audit run immediately after §2.23-2.25 shipped,
specifically because that feature is the first place in the codebase where
a single `Torra`/`Agent` either lives across many turns in one process
(a TUI tab, previously impossible -- every CLI invocation built a fresh
one) or gets torn down and rebuilt against the *same* persisted session
identity (a reopen). Both are genuinely new capabilities, and both exposed
state that had always been implicitly scoped to "one `Torra` instance =
one task" -- true by construction before this feature, false now. All
three were reproduced directly against real objects before being fixed,
not just reasoned about.

1. **Audit files collided, not just on reopen -- immediately, between any
   two tabs.** `_finish()`'s task_id for a non-plan turn (answer/ask/
   refuse) was `f"turn-{len(self.agent.turns)}"` -- no `task_id_prefix` at
   all, unlike the plan path (§2.23). Two tabs' first such turn both
   landed on `turn-1.audit.json`, one silently overwriting the other's
   audit record with an unrelated request. Fixed by keying it the same way
   the plan path already is: `f"{self.agent.task_id_prefix}turn-
   {self.agent._turn_counter}"` (`_turn_counter`, not `len(agent.turns)`
   -- `Agent.repair()` appends to `turns` without incrementing the
   counter, so the two aren't interchangeable). That alone still collided
   *within* one session across a reopen, since a fresh `Agent` restarts
   `_turn_counter` at 0 and can revisit values its earlier life already
   used -- closed by `Agent.fast_forward_turn_counter(at_least)`, called
   from `_register_session` with the *raw* transcript record count (not
   `len(context.entries)`, which `rehydrate_compressor` may have already
   compressed down to a handful of summaries -- using that would
   under-count and reopen the exact collision this closes).
2. **`DecisionLog` leaked every prior turn's decisions into each new
   turn's audit record.** `self.decisions` is constructed once per
   `Torra` and only ever grows (`DecisionLog.records`, nothing cleared
   it); `_finish()` snapshots all of it into `result.decisions`. A
   long-lived tab's audit file for turn *N* silently included turns
   `1..N-1` too, growing without bound for the tab's life -- directly
   against the audit contract's "one file, one task's decisions" model.
   Fixed with `DecisionLog.clear()`, called at the top of `plan()` (a new
   task's boundary) -- not in `execute()`, which must still see the same
   task's plan-stage decisions when it finishes.
3. **Reopening a long session bypassed the token budget for one turn.**
   `rehydrate_compressor()` loaded the full raw transcript straight into
   `compressor.entries`, correctly bypassing `.add()` (persisted `tokens`/
   `compressed` are already authoritative) -- but nothing then checked
   whether that raw load was itself over `max_tokens`. It usually is, for
   any sufficiently long session: a live session compresses itself as it
   goes and stays near budget, but the *persisted* transcript is
   deliberately the full pre-compression history (so a reopened replay
   shows what was actually said), and that history was never run back
   through compression on load. The very next prompt sent to the model
   after a reopen was unbounded by the budget `ContextCompressor` exists
   to enforce. Fixed with one `compressor.maybe_compress()` call at the
   end of `rehydrate_compressor()`.

Separately, `SessionStore.rename()` was the one method on this class that
could still raise on an ordinary disk hiccup, unlike every other write
path (`append()`), despite sitting on the single most common call site
there is -- the first message of every new tab. Hardened to match
`append()`'s policy: log to `self.errors`, return `None`, never raise.

Also touched, extending §5.14's existing "`execute_plan()`'s busy guard is
not optional UI polish" note rather than duplicating it here: `busy` now
flips synchronously on the main thread, in `execute_plan()`/
`plan_request()` themselves, *before* the worker is even dispatched --
closing the double-dispatch race window instead of merely shrinking it
(the previous fix moved the check earlier inside the worker thread, but
the flip itself still happened after a `call_from_thread` round trip,
leaving a real gap for a fast second click or Ctrl+D to land in).

> Guarded by `tests/test_pipeline.py::test_two_sessions_non_plan_turns_do_not_collide_on_the_same_audit_file`,
> `test_a_new_plan_call_starts_a_fresh_decision_trail`,
> `test_plan_then_execute_of_the_same_task_still_share_one_decision_trail`;
> `tests/test_agent.py::test_fast_forward_turn_counter_reaches_the_next_default_task_id`;
> `tests/test_session.py::test_rehydrate_compressor_brings_a_long_history_back_within_budget`,
> `test_rename_never_raises_on_a_write_failure`;
> `tests/test_tui.py::test_execute_plan_sets_busy_synchronously_before_the_worker_starts`,
> `test_plan_request_sets_busy_synchronously_before_the_worker_starts`

### 2.27 An alternate model backend must be strictly opt-in, and `torra tui` had its own, separate `Torra` construction path

**Rule:** An alternate backend (`openrouter/...`) is used only when the operator names it (`ModelRouter` can never pick one) and is registered unconditionally so a missing key fails loudly; every tab's `Torra` inherits `backends`, and `model_override` is never persisted. **Guard:** `tests/test_cli.py::test_build_torra_registers_openrouter_even_without_the_env_var` and `tests/test_tui.py::test_an_unconfigured_openrouter_override_fails_clearly_not_silently`, and 6 more below.

`Agent.resolve_backend()` (`torra/agent.py`) is the only place a request's
model string can route to something other than `self.ollama` --
`torra/gates/router.py`'s `ModelRouter` has no reference to `self.backends`
at all and cannot select one even by accident, so "OpenRouter is never
auto-selected" is structural, not just discipline. Every alt backend only
activates when the operator explicitly names it (`openrouter/<real-model-id>`,
an `openrouter/` prefix reserved for that purpose since local Ollama tags can
themselves contain a slash, e.g. `hf.co/user/model` -- there is no way to
tell "this is a remote model" from "this is a local tag" by the string's
shape alone, only by an operator-chosen namespace). `resolve_backend()` also
strips that prefix before the wire call: OpenRouter's own model IDs are
already provider-prefixed (`anthropic/claude-3.5-sonnet`), so the registered
prefix only exists to pick the backend, not to travel to the API -- the full
`openrouter/...` string stays as `turn.model`/`Plan.model` for audit
transparency.

Found during design review, before shipping: `cli.py`'s `build_torra()` is
not the only place a `Torra` gets constructed. `cmd_tui` called
`run_tui(workdir=...)` with no `torra=` argument, and `TorraApp.__init__`
built a bare `Torra(workdir=...)` directly -- bypassing `build_torra()`
(and, incidentally, its disk-free precondition too) entirely. Fixed by having
`cmd_tui` call `build_torra()` and pass its result into
`run_tui(torra=...)`, and by threading `self.backends` (captured off the
initial `Torra` the same way `self.ollama` already is) into every
subsequent tab's `Torra(...)` call in `_build_session_torra()` -- a new
tab that doesn't inherit `backends` would silently lose OpenRouter access
after the first tab, the same shape of bug §2.23-§2.26 already catalog for
other per-tab state.

**Found live by manual testing after that fix shipped, not by a test:**
`build_torra()`'s first version registered `"openrouter"` only when
`OPENROUTER_API_KEY` was actually set -- reasoning that constructing a
client doomed to fail every call wasn't worth it. That reasoning was wrong.
`torra ask "..." --model openrouter/anthropic/claude-3.5-sonnet` with no
key set produced no error at all: since `"openrouter"` was never registered,
`Agent.resolve_backend` found no matching prefix in `self.backends` and fell
through to `self.ollama` with the *literal string*
`"openrouter/anthropic/claude-3.5-sonnet"` as the model -- which
`converse()`'s degraded-daemon fallback (below) then silently swapped for a
real, unrelated local model, since that string obviously wasn't a pulled
model either. The operator's explicit backend choice vanished with zero
indication it had happened; the audit record showed a normal-looking answer
from `qwen3.5:latest`. Fixed by registering `"openrouter"`
**unconditionally** -- `OpenRouterClient` makes no network call at
construction, only `.chat()` does, and `.chat()` already raises a specific
`"OPENROUTER_API_KEY is not set"` error the moment it's actually invoked
without one. Registering it always is what lets that correct, specific
error fire at all, instead of the request being silently misrouted before
ever reaching it. The lesson generalizes: an unconfigured alt backend must
still occupy its prefix in `self.backends`, so `resolve_backend` recognizes
the request as "meant for a backend that isn't ready" rather than "not a
backend request at all" -- the two look identical to a prefix scan unless
the prefix is actually registered.

`Agent.converse()`'s degraded-daemon fallback (`self.ollama.last_status.value
!= "healthy"` -- true on essentially every session's first turn, per §2.20)
is about the *local Ollama catalog's* availability and must not run at all
when the chosen model resolves to a registered alt backend; `resolve_backend`
is not called for this check directly (it would also compute the stripped
model, which isn't needed yet) -- instead a cheap prefix scan gates the block
before `chosen` can be swapped, and `resolve_backend` runs once afterward.

`SessionTab.model_override` (`torra/tui.py`) is runtime-only, deliberately
never persisted through `SessionStore`/`meta.json` -- reopening a session
drops back to local-only routing, the conservative failure mode (defaults
*away* from paid remote calls, not toward silently continuing them). Don't
"fix" this into a `SessionMeta` schema change.

> Guarded by `tests/test_core.py::TestOpenRouterClient` (transport/shape/
> error/env-var tests, mirroring `TestChatResponseShape`),
> `tests/test_agent.py::test_resolve_backend_...` (prefix match, no-prefix
> fallthrough, prefix stripping, degraded-fallback guard not firing for an
> alt-backend model), `tests/test_pipeline.py::` recovery-options-come-from-
> the-backend-that-failed, `tests/test_cli.py::
> test_build_torra_registers_openrouter_even_without_the_env_var` /
> `test_cmd_tui_builds_the_torra_through_build_torra`,
> `tests/test_tui.py::` `/model` sets/clears the override, a new tab
> inherits `self.backends`,
> `test_an_unconfigured_openrouter_override_fails_clearly_not_silently`
> (the end-to-end regression test for the manual-testing finding above)

### 2.28 A `tests/test_tui.py` test that dispatches a Textual worker must wait for it before its `async with app.run_test()` block ends

**Rule:** A `tests/test_tui.py` test whose last real action dispatches a Textual worker ends with `await app.workers.wait_for_complete()`, not a bare `pilot.pause(N)`, except after a deliberate mid-flight assertion. **Guard:** `tests/test_tui.py::test_execute_plan_sets_busy_synchronously_before_the_worker_starts` (an example; no single test guards this pattern).

CI failed on two separate pushes for two different tests, both green locally
every time -- not a coincidence, one root cause. `run_worker(...)` (async or
`thread=True`) schedules work that keeps running *after* the line that
called it returns. If the test's own `async with app.run_test() as pilot:`
block exits before that worker finishes, `run_test()`'s teardown starts
unmounting the screen while the worker is still mid-flight; the worker's own
`call_from_thread`/widget-query calls then raise `NoMatches` from inside the
worker, which Textual surfaces as `WorkerFailed` -- **failing the test even
if every assertion in it had already passed.** A fixed `await
pilot.pause(N)` is not a fix, only a bet: on a fast, idle machine it usually
covers the gap, and it did, every time, locally. On a slower or more loaded
CI runner it sometimes doesn't, and which test loses the bet differs run to
run (`test_close_tab_removes_the_active_tab_and_falls_back_to_another` one
time, `test_execute_plan_sets_busy_synchronously_before_the_worker_starts`
the next).

Fix: `await app.workers.wait_for_complete()` instead of, or in addition to,
a fixed-duration pause -- waits for the actual dispatched `Worker.wait()` to
resolve rather than guessing how long that takes. Confirmed race-free by
construction, not just less likely to lose: `App.run_worker()` calls
`WorkerManager._new_worker()` synchronously before returning (registers the
`Worker` in the manager immediately), so calling `wait_for_complete()`
right after any worker-dispatching action already sees it. Safe to call
even when nothing is pending -- `asyncio.gather()` over an empty set
resolves immediately, so it costs nothing to add defensively to a test
whose trigger *might* dispatch a worker depending on runtime state (e.g.
`execute_plan()` called while already busy -- its own guard means no
worker gets dispatched that specific time, but the call site looks
identical to one that does).

**Do not add this after an assertion that deliberately checks mid-flight
state** (e.g. `test_ctrl_d_during_a_real_run_does_not_start_a_second_execution`'s
`assert app.busy, "the run should still be in flight"`, checked right after
a short, intentional pause) -- waiting for completion there would defeat the
point of the check. Add it at the test's true last synchronization point,
after whatever confirms the in-flight state, not in place of it.

That mid-flight check is its own instance of this same family, just the
other direction: CI failed it twice (2026-08-26, two separate runs, minutes
apart -- not the "different test each time" pattern above, the *same* test
both times), with `app.busy` reading `False` 0.05s after `pilot.click
("#run")`. Not `execute_plan()` finishing a 0.3s step in 50ms -- it flips
`busy` synchronously, before the worker even dispatches, with no yield
point in between (this file's own docstring on that method). The gap is
between the simulated click and the loaded runner's event loop actually
pumping the button's `Pressed` message, not between dispatch and
completion. Fix: widen the pause (0.05s -> 0.15s), not `wait_for_complete()`
-- still comfortably under the step's 0.3s completion, just more slack for
message-pump latency under CI's shared-runner scheduling jitter.

A full audit found ten more tests in this file with the same shape (a
worker-dispatching trigger as the last real action, backed only by a
pause of 0.2s or less) and hardened them the same way; two more with a
*guarded* trigger (no worker actually dispatches at runtime, e.g. calling
`execute_plan()` while `busy` is already `True`) were left alone since
`wait_for_complete()` there would be a no-op — not wrong to add, just not
where the actual risk lives. New tests that press `ctrl+t`, click
Run/Dry-run/Reject, or reopen a session via the picker should default to
this pattern rather than a bare `pilot.pause(N)` as the test's final
synchronization point.

> Guarded by: no single test guards this (it is a pattern across the file,
> not a code invariant) -- `tests/test_tui.py`'s own CI-run history is the
> evidence, not a local assertion.

### 2.29 `TorraApp._handle_exception` is the only hook that ever sees a TUI-side crash, and `App.run()` never re-raises past it

**Rule:** `TorraApp._handle_exception` is the only hook that sees a TUI-side crash (`App.run()` never re-raises); its override records a redacted crash through `CrashReporter` inside its own `try/except`, then always calls `super()._handle_exception`. **Guard:** `tests/test_tui.py::test_handle_exception_records_a_crash_and_still_calls_super` and `tests/test_tui.py::test_handle_exception_still_calls_super_if_the_reporter_itself_raises`, and 3 more below.

Confirmed by reading Textual's own `app.py`, not inferred: `App._handle_exception()`
(its own docstring: "Always results in the app exiting") is the sole place
any unhandled exception anywhere inside a running TUI -- workers, event
handlers, compose, the message pump -- surfaces. `App.run()`/`run_async()`
never re-raise after it runs; both return normally with `self.return_code
== 1`. The only place in Textual's `app.py` that re-raises after a panic is
`run_test()`, and only for pytest's own awareness (its own comment: "Re-raise
the exception which caused panic so test frameworks are aware"). This means
`cli.py`'s top-level `except Exception` in `main()` structurally **cannot**
see a TUI-side crash -- `cmd_tui()` calls `run_tui(...).run()` and control
only returns after Textual has already handled and swallowed the exception.
Wrapping the `run_tui()` call site or `cmd_tui()` itself would silently
never fire; overriding `TorraApp._handle_exception()` is the only correct
integration point. Also confirmed single-threaded: a thread-worker's
failure is caught back on the main event-loop thread (inside `Worker._run`)
before `_handle_exception` ever runs, so there is no concurrency concern for
`CrashReporter.record()` itself.

Also confirmed by reading `tui.py`'s own worker bodies: `_plan_request_body`/
`_execute_plan_body` each have their own internal `try/except Exception`
around `Torra.plan()`/`.execute()` that displays the error inline and
clears `busy` (`tests/test_tui.py::test_plan_request_reports_an_unexpected_exception`,
`test_execute_plan_reports_an_unexpected_exception`) -- those exceptions
never reach `_handle_exception` at all, so `CrashReporter`
(`torra/telemetry/crash.py`) records only genuinely-unhandled TUI bugs
(the same class of thing §2.13/§2.25 found by hand before any hook existed),
never routine plan/execute failures, which `TaskLogger` already records
with `status: failed`/`backend_down`. A crash count in `torra evidence`'s
new health summary should not be read as including ordinary task failures.

`CrashReporter.record()` redacts `message`/`traceback` through the same
`redact_secrets()` `records.py` already uses for `TaskRecord.request`/
`Label.why` -- a traceback's local-variable reprs can contain a secret
exactly the way free-form request text can (Textual's own panic screen
already renders locals via `rich.traceback.Traceback(show_locals=True,
...)`, confirmed by reading `_fatal_error()`; persisting an unredacted copy
of the same information indefinitely would be strictly worse than what's
already shown transiently on screen).

`TorraApp._handle_exception`'s override calls `self._crash_reporter.record(...)`
inside its own bare `try/except`, then unconditionally calls
`super()._handle_exception(error)` -- a crash reporter that itself fails
must never prevent the one hook responsible for the app actually exiting
from running.

> Guarded by `tests/test_tui.py::test_handle_exception_records_a_crash_and_still_calls_super`,
> `test_handle_exception_still_calls_super_if_the_reporter_itself_raises`;
> `tests/test_telemetry.py::TestCrashReporterFailureEdges` (construction/
> record/read failure paths, mirroring `TestTaskLoggerFailureEdges`);
> `tests/test_cli.py::test_main_logs_the_full_crash_record_with_site_and_traceback`,
> `test_main_with_no_subcommand_that_crashes_logs_site_tui`

### 2.30 A nested `TabbedContent` inside a tab breaks every unscoped "the one TabbedContent/Tabs/TabPane" assumption `tui.py` had gotten away with

**Rule:** With nested `TabbedContent`, `_session_id_of` matches only the session-level `TabPane` by its `tab-` id prefix, tab content is attached via `compose_add_child()`, and call sites query `#sessions` explicitly rather than an unscoped `TabbedContent`/`Tabs`. **Guard:** `tests/test_tui.py::test_click_inside_the_sidebars_plan_tab_resolves_the_right_session` and 3 more below.

Found during design review for the sidebar-tabs feature (the right-hand
pane's `PlanPane` became a `TabbedContent` with Plan/Evidence tabs), before
any of it shipped, by constructing the proposed DOM shape directly and
exercising it (`app.run_test()`), not by inference.

1. **`_session_id_of` stops at the first ancestor `TabPane`, not the
   session-level one.** Walking up from a Run/Reject/Dry-run button inside
   the sidebar's Plan tab now hits the *inner* `TabPane(id="sidebar-plan")`
   before the outer `TabPane(id="tab-<session_id>")`. `str.removeprefix
   ("tab-")` is a no-op on a non-matching string, so this silently produces
   `session_id == "sidebar-plan"` -- which is never a real key in
   `self.sessions`, so every click of Run/Reject/Dry-run raises `KeyError`
   unconditionally. Per §2.29, `TorraApp._handle_exception` is the only
   hook that ever sees this, and `App.run()` never re-raises past it -- the
   effect is not degraded click-handling, it's the whole app exiting on
   the first click of any plan-action button. Fixed by matching
   specifically the session-level `TabPane` by its known `"tab-"` id
   prefix, not just "the nearest `TabPane`":
   `isinstance(node, TabPane) and (node.id or "").startswith("tab-")`.
2. **`TabbedContent(*children)` is not `Horizontal`/`Vertical`'s
   `*children: Widget` -- it's `*titles: ContentType`.** Every other
   container in this file accepts widgets positionally (inherited from
   `Widget.__init__`); `TabbedContent.__init__` overrides that slot to
   mean tab-label strings and never forwards it to `super().__init__()`.
   Passing `TabPane(...)` instances positionally crashes immediately in
   `render_str()` (`AttributeError: 'TabPane' object has no attribute
   'translate'` -- confirmed live, not read off documentation). Content
   must instead be attached via `compose_add_child()`, the same hook
   Textual's own `with TabbedContent(): yield TabPane(...)` compose syntax
   calls internally -- unlike that `with` form (which requires an active
   `self.app` and so can't run inside a plain `@staticmethod` invoked
   before mount), `compose_add_child()` has no such dependency and works
   standalone (`TorraApp._build_sidebar`).
3. **Six call sites assumed exactly one `TabbedContent`/`Tabs` exists in
   the whole app.** `_name_session`, `_mount_session_tab`,
   `_open_or_switch_to`, `action_close_tab` (all
   `self.query_one(TabbedContent)`) and `action_next_tab`/`action_prev_tab`
   (both `self.query_one(Tabs)`) all relied on there being exactly one
   match -- true before this feature, false the moment a session tab's own
   sidebar becomes a second `TabbedContent`. `query_one()` in this Textual
   version does not raise on ambiguity; it silently returns the first
   breadth-first match. That still happens to be the outer `#sessions`
   widget today (an ancestor is always shallower than any of its own
   descendants in a breadth-first walk, and every inner `#sidebar` is
   nested inside one of `#sessions`'s own tab panes) -- not currently
   broken, but resting on an invariant nobody had stated, with zero test
   coverage of `action_next_tab`/`action_prev_tab` to catch a future
   regression. Hardened by scoping all six explicitly:
   `self.query_one("#sessions", TabbedContent)` /
   `self.query_one("#sessions > Tabs", Tabs)`.

Separately: `_evidence_health_summary`/`_evidence_crash_summary` moved out
of `cli.py` into `torra/telemetry/summary.py` (`health_summary`/
`crash_summary`, no longer CLI-private) so the TUI's new Evidence tab could
reuse them without `tui.py` importing from `cli.py` -- the codebase's two
leaf/entry-point modules had never depended on each other before, and
having one do so would have been the first such coupling. The move alone
regressed the defensive-coding scorecard's Complexity dimension:
`health_summary` had always had a CCN of 20, but `cli.py` is peripheral
tier (not scorecard-enforced) while `torra/telemetry/` is core tier, so
moving it into a graded package surfaced pre-existing complexity that had
simply never been counted before. Fixed the same day, same change, via the
`DEFENSIVE_CODING_STANDARDS.md` §2 extraction pattern (four independent
sections -- outcomes/backend/latency/rollback -- split into their own
`_outcome_lines`/`_backend_line`/`_latency_line`/`_rollback_line`
functions, `health_summary` reduced to a thin orchestrator).

> Guarded by `tests/test_tui.py::test_click_inside_the_sidebars_plan_tab_resolves_the_right_session`
> (#1), `test_the_sidebar_evidence_tab_renders_real_content_on_activation`
> (#2, a construction/rendering smoke test for the sidebar and
> `EvidencePane`), `test_ctrl_tab_with_two_open_tabs_switches_the_session_tab`
> (#3, had zero coverage before this entry);
> `tests/test_telemetry.py::TestSummary` (the moved functions, called
> directly, not just through `cmd_evidence`)

### 2.31 Streaming reasoning tokens to the TUI must default to silence, not to asking

**Rule:** Streaming reasoning defaults to silence: `thinking_capable` is False when unknown, `on_thinking` reaches only a local backend the router measured capable, `on_thinking=None` sends the original request unchanged, and reasoning text is never persisted. **Guard:** `tests/test_core.py::TestChatThinking` and `tests/test_agent.py::test_converse_never_passes_a_callback_to_an_alt_backend`, and 7 more below.

The TUI shows a live "the model is thinking" block in the chat pane while a
turn is planning, for models that actually expose reasoning tokens
(`deepseek-r1`-style). The whole feature rests on one asymmetry that is easy
to get backwards: **every capability check involved defaults to "no" when
unknown, not "yes."**

- `OllamaClient.model_info()`'s new `thinking_capable` key mirrors the
  existing `chat_capable` key's shape but inverts its default: an older
  Ollama daemon that reports no `capabilities` at all for a model defaults
  `chat_capable` to `True` (missing capability data must not block every
  model), but `thinking_capable` defaults to `False` -- not knowing whether
  a model streams reasoning must mean "don't ask it to," never "assume it
  can." Same asymmetry in `ModelRouter._thinking_capable` vs.
  `_chat_capable`.
- `Agent.converse`'s `on_thinking` parameter only ever reaches
  `backend.chat` when **all three** hold: the caller actually passed one,
  the resolved backend is the local Ollama daemon (never an
  `openrouter/...` override -- `router.model_thinking_capable` is
  populated exclusively from local `/api/show` data in `Agent.observe`, so
  a remote model is structurally never marked capable), and the router
  measured that specific model capable. Get any one of those backwards and
  a model with no reasoning support gets `stream: true, think: true` sent
  to a daemon that may not understand `think` at all, or a paid remote
  call gets a callback parameter with no meaning for it.
- `OllamaClient.chat`'s own default (`on_thinking=None`) is the single
  switch between the original single-shot `stream: false` request every
  existing caller still makes, and the new NDJSON-streaming `stream: true`
  path (`_http_stream`) -- passing `None` must produce byte-for-byte the
  same request as before this feature existed. This is what makes the
  feature additive rather than a rewrite of the request path every caller
  depends on.
- The reasoning text itself is deliberately **not** persisted anywhere --
  not `SessionStore`, not telemetry, not `Turn.to_dict`'s audit record.
  `SessionTab.reasoning_line`/`reasoning_text` are runtime-only fields
  (same category as `model_override`), cleared by `_clear_thinking` the
  moment `_planned` renders the turn's outcome. A future change that wires
  this into a persisted format is a deliberate scope expansion, not a bug
  fix -- it needs its own review, the same way wiring `docs/` into
  `SYSTEM_PROMPT` does.

> Guarded by `tests/test_core.py::TestModelInfo` (`thinking_capable`
> defaults), `TestChatThinking` (the `on_thinking=None` no-op path, the
> streaming path, NDJSON reassembly against the real `_http_stream`, not
> just an injected fake); `tests/test_gates.py::TestModelParamsAndChatCapable`
> (`_thinking_capable`'s default-False asymmetry vs. `_chat_capable`'s
> default-True); `tests/test_agent.py::test_converse_passes_a_real_callback_for_a_thinking_capable_model`
> + `test_converse_passes_no_callback_for_a_non_thinking_model` +
> `test_converse_never_passes_a_callback_to_an_alt_backend`;
> `tests/test_tui.py::test_append_thinking_grows_the_thinking_tab`
> + `test_clear_thinking_resets_the_thinking_tab` +
> `test_reasoning_text_survives_until_the_next_question_is_asked`

### 2.32 Live step output has to reach `shell()` through a thread-local sink, not a `Step.run` parameter, because `Step` objects are built once at plan time and reused verbatim through execute

**Rule:** Live step output reaches `shell()` through a thread-local `shell_output_sink`, entered only around a real (non-dry-run) `sp.step.run()`; `Step.run`'s call signature stays unchanged and stdout/stderr stay captured separately. **Guard:** `tests/test_core.py::TestShellOutputSink` and `tests/test_pipeline.py::test_on_output_receives_live_chunks_tagged_with_the_step_id`, and 5 more below.

The TUI's other blind spot besides §2.31's reasoning tokens: while a real
step runs, the operator saw a `running...` spinner and nothing else --
`shell()` used `subprocess.run()`, fully blocking, so no output exists
anywhere until the whole command finishes. Worse than the reasoning-token
gap: a successful step's captured stdout was never shown *at all*, live or
after the fact -- `_executed()` only ever prints `ok`/`fail` + seconds +
`error`, never `StepResult.output`. This was the only way to close that,
not an optional nicety.

The design constraint that shaped the fix: `TaskSpec.to_steps()` builds
every step's `run` closure exactly once, in `Torra._gate()`, at **plan**
time -- before the operator has even seen the plan, let alone approved it,
let alone before any live UI consumer exists to hand it a callback. `Plan.
steps` keeps those exact `Step` objects (`StepPlan.step`) all the way
through `execute()`, not freshly-rebuilt ones -- identity matters there for
approval-matching by `step_id` and for undo generation. Two ways to get a
callback into `shell()` at execution time were rejected before landing on
the one that shipped:

- **Add a parameter to `Step.run`'s call signature.** Works in principle
  (`def action(on_output=None): ...` still defaults safely for a zero-arg
  call), but ripples into every test/example that hand-constructs a `Step
  (run=lambda: ...)` today, and into every `to_steps(runner=...)`
  injection point (`observe_machine`'s diagnostic commands, any test
  double) which would all need to grow the same parameter whether they
  cared about it or not.
- **`contextvars.ContextVar`.** The idiomatic "pass extra context down a
  call chain without threading a parameter through every intermediate
  function" tool -- except real step execution runs on a plain
  `threading.Thread` (`tui.py`'s per-session `run_worker(..., thread=True)`),
  not an asyncio task, and a `ContextVar` set on one OS thread is invisible
  to a different OS thread unless the context is explicitly copied at
  spawn time. It would have silently never worked.

What shipped: `torra/core/tasks.py`'s `shell_output_sink(on_output)`, a
context manager wrapping a plain `threading.local()`. `shell()` checks it
on every call; `Torra._run_one_step()` enters it only around the real
(non-dry-run) `sp.step.run()` call, bound to that one step's id via a
closure built at *execute* time, not baked into the `Step` itself. Zero
change to `Step.run`'s call convention -- every existing `Step(run=...)`
in tests/examples, and every `to_steps(runner=...)` injection, is
unaffected, because nothing about how `Step.run()` is *called* changed at
all. `threading.local()` genuinely works here because real execution
(`_run_steps` through every `_run_one_step` in the loop) always runs
start-to-finish on one single OS thread per session -- concurrent sessions
each get their own worker thread and their own independent
`threading.local()` storage, so two tabs executing at once never see each
other's sink.

`shell()`'s streaming path (`_shell_streaming`) had to match the default
path's exact contract, not just "look similar": pipefail semantics (the
same `set -o pipefail` prefix), the same timeout behavior (raises
`subprocess.TimeoutExpired`, but enforced by the read loop itself, not
`Popen.wait`'s own timeout, since a hung process that never closes its
output pipes needs to be killed on schedule regardless), and -- easy to
get wrong -- **stdout and stderr stay captured separately**, exactly like
`capture_output=True` does. Merging them into one pipe (`stderr=
subprocess.STDOUT`) would have been the simpler implementation, but would
have changed what a *successful* step's `StepResult.output` actually
contains (stderr noise mixed into what every other caller reads as pure
stdout) purely as a side effect of whether a live consumer happened to be
watching -- reading two separate pipes without deadlocking needs one
reader thread per pipe, feeding one queue so `on_output` still sees lines
in close to real arrival order.

The live view is deliberately **not** cleared once a step finishes,
unlike §2.31's reasoning line -- there is nowhere else in the TUI a
successful step's output is ever shown, so the live line doubles as the
only persistent (if not `SessionStore`-persisted) record of it for that
run. `execute_plan()` resets `SessionTab.output_lines`/`output_text`
before dispatching a fresh run, so a re-run reusing the same step id
starts a new line instead of silently concatenating onto the previous
run's already-finalized one.

> Guarded by `tests/test_core.py::TestShellOutputSink` (pipefail under
> streaming, stdout/stderr separation, timeout kills a hung process,
> `test_output_genuinely_streams_instead_of_arriving_all_at_once` proves
> real liveness with wall-clock timing, not just "the callback eventually
> fired"); `tests/test_pipeline.py::test_on_output_receives_live_chunks_tagged_with_the_step_id`
> + `test_on_output_tags_each_step_with_its_own_id` +
> `test_on_output_is_never_called_for_a_dry_run` +
> `test_a_failing_step_still_streams_its_output_before_raising`;
> `tests/test_tui.py::test_step_output_streams_live_and_survives_after_completion`
> (drives the real worker thread end to end, polls for the live line to
> appear *before* the step finishes, asserts the second chunk hasn't
> landed yet -- a genuine liveness check, not just a final-state assertion) +
> `test_a_rerun_starts_a_fresh_output_line_not_a_continuation`

### 2.33 A resume point must be cleared on *any* terminal outcome, not just success, or a retry can skip steps a rollback just undid

**Rule:** A resume point is trusted only if its `task_id` and `total` match, its `completed` ids are a positional prefix of the plan's step ids, and (§2.64) each completed step's command digest matches; it is deleted on a real success or whenever a rollback was attempted (§2.64: not on a block or failure that rolled nothing back), and a dry run never writes it. **Guard:** `tests/test_pipeline.py::TestResume` and 1 more below.

`ResumePoint.load()` had no caller anywhere in the codebase until this
entry (§6.4's "fixed" pointer) -- `torra/core/resume.py`'s own
`resolve_resume()` is what changed that: `Torra.execute()` calls it
before the step loop, and a validated resume point's `completed` steps
are skipped rather than re-run.

The design constraint: `resolve_resume` has to run inside `pipeline.py`'s
`execute()`, but the validation logic itself lives in `core/resume.py` --
moved there specifically to keep `pipeline.py` under the Modularity
scorecard's 500-LOC floor (it crossed 500 the first time this landed
inline; `ENFORCE_FLOORS` caught it, dropping Modularity from B to C and
overall GPA from 3.83 to 3.67). Not just LOC-shuffling to dodge the
checker: resume-validation genuinely belongs next to the type it
validates, and `pipeline.py` staying a thin caller (build the plan's step
ids, call `resolve_resume`, log its `note` if any) is a real
improvement, not a workaround. `ResumeResolution.note` exists so
`core/resume.py` never has to import `pipeline_types`' `DecisionLog`/
`Stage` to log anything itself -- the dependency direction throughout
this package is `pipeline.py` -> `core`, never reversed.

Two correctness hazards, not obvious from "just call `load()` before the
loop":

1. **A resume point's `completed` list must be validated as a positional
   prefix of the *current* plan's step ids, plus a matching `total` --
   not just "a file exists for this task_id".** A `task_id` is fully
   model- or human-chosen text with no uniqueness guarantee; a
   differently-shaped plan that happens to reuse one, or an edited task
   file, must not have its early steps silently skipped because an
   unrelated resume file's ids happen to overlap. `resolve_resume`
   requires `existing.task_id == task_id and existing.total == total and
   plan_step_ids[:len(existing.completed)] == existing.completed` before
   trusting a single entry; anything else is logged and ignored, same as
   a genuinely missing or corrupt (`ResumePoint.load()` raises
   `TorraError` on that) file.
2. **The resume file must be deleted on a failed/rolled-back run too, not
   only `_finish_success()`'s clean-success path.** Before this, only
   success deleted it -- a step that failed after an earlier step
   completed left the resume file sitting there with that earlier step
   still marked "completed", even though `_sweep_rollback()` had just
   undone it (or tried to). A later retry would have skipped a step whose
   real-world effect had just been reversed -- worse than not resuming at
   all, since the operator would have no reason to suspect it. Fixed by
   moving the `resume_path.unlink(missing_ok=True)` `_finish_success()`
   already did into `_sweep_rollback()` too, unconditionally (whether or
   not the rollback itself fully succeeded -- an incomplete rollback is
   exactly the case where a stale "completed" claim is least trustworthy).

A third, narrower one found while building it: **a dry run must never
write a resume point at all.** `_run_steps` wrote `resume.completed`/
`resume.save()` unconditionally after every successful step before this,
dry run or not -- harmless in practice only because a dry run always
reaches `_finish_success()`'s cleanup at the end (dry-run steps can't
fail), so nothing ever persisted past one call. But a crash *during* a
dry run (the process dying mid-preview, before that cleanup ran) would
have left a resume file claiming steps were "completed" that were only
ever previewed, never actually run -- exactly the "garbage" `resolve_resume`
exists to distrust, except this one would have validated cleanly (real
task_id, real step sequence) and been silently wrong. Fixed by gating
`resume.completed.append`/`resume.save()` behind `not dry_run`.

Resumed (skipped) steps still count toward `RunResult.executed = True`
and still get a synthesized `StepResult` (`ok=True`, output prefixed
`"[resumed] "`) appended to `result.results`, so the operator's final
report shows what actually happened this call without hiding that some
of it happened earlier. What resumed steps deliberately don't do:
run through the `recheck_between_steps`/`ConflictDetector` guards ahead
of them in the loop, or call `self.conflicts.refresh()` -- those exist to
catch state changing since a step was *about to* run, which doesn't apply
to a step that isn't going to run at all this call. Skipped steps'
declared undos still reach the rollback stack regardless, for free:
`_stage_rollback()` pushes every step's undos unconditionally, before
`_run_steps()` (and its skip logic) ever runs.

`ResumePoint.file_hashes` stays unpopulated -- out of scope here, same as
it was before. Inventing semantics for a field the original design left
vague isn't part of "make `load()` have a caller"; a future change that
wants extra verification (confirming a resumed step's touched files
still look the way they did when it completed) should own that decision
explicitly rather than inherit it from this one.

> Guarded by `tests/test_pipeline.py::TestResume` (8 cases: skips
> already-completed steps and runs the rest for real, a mismatched step
> sequence is not trusted, a mismatched `total` is not trusted, a corrupt
> file is ignored not fatal, a dry run never writes one, a dry run ignores
> an existing one, a failed run with rollback deletes it, a clean success
> leaves none behind) and `tests/test_pipeline.py::test_a_malicious_task_id_cannot_write_the_resume_file_outside_the_workdir`
> (updated to spy on `ResumePoint.save`'s path rather than checking
> post-hoc file existence, since a failed run now cleans up too)

### 2.34 `CorpusExporter.drift()` was scoring the clarifier's isolated verdict, not what actually happened to the operator -- 72% "false positives" were really ~17%, and that same fix exposed an 84% false-negative rate the old measurement had no way to see

**Rule:** `CorpusExporter.drift()` scores what actually happened to the operator (each labeled request's logged attempts via `TaskRecord.clarifier_blocked`, counted per occurrence), not a fresh `clarifier.detect()` verdict; the measured 84% false-negative rate was not fixed there (§2.35). **Guard:** `tests/test_telemetry.py::TestDrift` and `tests/test_telemetry.py::TestDrift::test_occurrences_are_counted_per_real_attempt_not_per_unique_text`.

First real-traffic labeling pass this project has ever done (2026-08-26,
57 of 100 logged requests, `torra label`): `drift()` reported a 72%
live false-positive rate against a 10% synthetic baseline -- "DRIFT: the
clarifier is interrupting real work." Investigated before touching
anything, per this file's own §7 discipline (measure before you build):
`drift()`'s `_false_positives`/`_false_negatives` called
`clarifier.detect(request)` fresh on the labeled text and compared that
in isolation to the human's label. But `Agent._messages()`'s own prompt
(`torra/agent.py`) tells the model exactly the opposite of "a flagged
gap means ask": *"if any of them genuinely blocks you, use intent ask.
If you can proceed anyway, say why in `message`."* The clarifier is
`Agent`'s own docstring's words, "*a tool the model consults, not a wall
in front of it*" -- `detect()` finding a gap and the operator actually
being interrupted are two different events, and the old `drift()` only
ever measured the first one while reporting it as the second.

Cross-referencing the 26 "false positive" requests against their real
recorded outcomes in `tasks.jsonl` (not just re-running `detect()`)
found 71% of those attempts actually resulted in `answered`/`completed`
-- the model saw the flagged gap and correctly proceeded anyway, exactly
as designed. Only ~21% became a genuine `needs_answer` interruption.
Fixed by changing what `drift()` scores: instead of a fresh
`clarifier.detect()` call, it now reads each labeled request's real
logged attempts and checks `TaskRecord.clarifier_blocked` (already
persisted per-record as `intent == "ask"`, `records.py`'s
`_apply_turn_fields`) against the human's label. Confirmed live against
the same real data: false positive rate dropped to 17%, right next to
the 10% baseline.

That same fix, run against the same real data, immediately surfaced a
second, more concerning number the old measurement structurally could
not have found: **84% false negatives** (baseline 0%). For a request a
human calls genuinely ambiguous, the model almost never actually stops
and asks -- `'storage issues on paramostorage'` alone was submitted 9
times in this traffic and never once produced a real clarifying
question. The old, isolated-`detect()` measurement couldn't see this at
all: it only ever asked "did the clarifier flag it," never "then what
happened" -- an ambiguous-but-not-blocked request was invisible to it in
either direction. This is not fixed here (no code changed in response to
it) -- it is exactly the kind of finding HANDOFF.md §7's four questions
exist to force before building anything: measured, real, and pointing at
the model's own judgment (how eagerly it treats "you *can* proceed" as
"you *should*"), not at the clarifier's anchor patterns, which is a
different fix than the one this entry made.

Per-occurrence, not per-unique-text: a phrasing submitted four times that
interrupted three of those times is three real interruptions the
operator experienced, not one yes/no fact about the phrasing --
`DriftReport` gained `ambiguous_occurrences`/`specific_occurrences`
fields (distinct from `ambiguous_total`/`specific_total`, which still
count unique labeled texts) so the rate denominators reflect real sample
size, and `MIN_SAMPLE`'s gate now checks occurrences, not unique labels,
since that's what the rates are actually computed over.
`CorpusExporter.__init__` dropped its `logger: TaskLogger` parameter --
stored but never read by anything in the class even before this change;
`drift()` now takes `records: Sequence[TaskRecord]` explicitly instead
(the caller already has them, matching `LoopholeAudit.audit(records)`'s
existing sibling shape).

> Guarded by `tests/test_telemetry.py::TestDrift` (rewritten: real
> `TaskRecord`s with `clarifier_blocked` set instead of a `FakeClarifier`
> stub calling `detect()`; a dedicated
> `test_occurrences_are_counted_per_real_attempt_not_per_unique_text`
> case proves repeated real submissions of the same phrasing are each
> scored independently, and `test_an_unmatched_record_is_simply_ignored`
> proves an unlabeled record contributes to neither rate)

### 2.35 `Accelerator.fits()` didn't know Ollama evicts its one resident model to make room -- the actual mechanism behind §2.34's 84% false-negative rate

**Rule:** `Accelerator.fits()` credits the resident model's VRAM back to free VRAM only when `max_loaded_models == 1` is confirmed from the daemon and the resident model is not the one being checked; unknown (`0`) or more than one gets no credit. **Guard:** `tests/test_core.py::TestDetectAccelerator::test_fits_credits_the_resident_models_vram_when_single_model_confirmed` and `tests/test_core.py::TestDetectAccelerator::test_fits_gives_no_credit_without_a_confirmed_single_model_daemon`, and 2 more below.

§2.34 found the number; this entry is the root cause, chased down live
rather than guessed. `ModelRouter` already had the right idea:
unmatched, ambiguous requests route to `qwen3:32b` specifically because
it's the model measured (this file's own earlier notes) to ask the
right clarifying question instead of assuming. Re-running one of the
real false-negative requests (`'storage issues on paramostorage'`) live
showed it never actually got there:

```
downgraded from qwen3:32b because only 14431 MB VRAM free, then devstral:24b;
downgraded from devstral:24b because only 14431 MB VRAM free
```

`qwen3.5:latest` was sitting loaded in VRAM (9.5GB) from an earlier,
unrelated request. `Accelerator.fits()` checked *current* free VRAM
against each candidate and rejected both larger models -- but this
machine's `ollama.service` is confirmed live (`/proc/$pid/environ`) to
run `OLLAMA_MAX_LOADED_MODELS=1`, meaning Ollama would have evicted
`qwen3.5:latest` automatically the moment anything else was requested.
`fits()` had no way to know that, so it treated VRAM the resident model
was sitting on as permanently unavailable rather than reclaimable. Doing
the math against this real 24564 MB card: `devstral:24b` needs ~16916
MB -- comfortably fits once nothing else is resident, but not against
the 14393 MB actually free with `qwen3.5:latest` still loaded. Worse,
this is self-reinforcing: whichever small model wins because it fits
stays loaded, which makes the *next* request look just as VRAM-starved,
biasing every subsequent ambiguous request toward the same model that
was never validated for asking behavior.

`qwen3:32b` itself needs ~23511 MB -- only ~1053 MB of margin even on a
fully idle card, so it may often stay out of reach regardless of what
else is loaded; that part of the ceiling is structural, not a bug, and
wasn't the fix. `devstral:24b` reliably fitting once nothing else holds
the GPU was the fixable part.

Fixed conservatively, not by assuming eviction always happens:
`Accelerator` gained `loaded_model`/`loaded_model_mb` (parsed from the
same `ollama ps` line `detect_accelerator()` already reads for the
CPU/GPU split) and `max_loaded_models` (read from the daemon's real
`OLLAMA_MAX_LOADED_MODELS` via `/proc/$pid/environ`, the same technique
`OLLAMA_MODELS_DIR_PROBE` already used for `OLLAMA_MODELS` --
`OLLAMA_MAX_LOADED_MODELS_PROBE` bakes in no fallback default, unlike
that one, because Ollama's own unset behavior is a dynamic,
undocumented computation with no live daemon here to verify a guess
against; unset or unreadable reports `0`, meaning "unknown"). `fits()`
credits `loaded_model_mb` back to free VRAM **only** when
`max_loaded_models == 1` is confirmed and the resident model isn't the
one being checked -- never for `0` (unknown), never for more than 1
(a multi-model-resident daemon isn't guaranteed to evict at all, and
this fix deliberately doesn't attempt to model that case).

Verified live, not just by the unit tests: reloaded `qwen3.5:latest`
into VRAM to recreate the exact real condition that produced the
original false negative, then re-ran the identical real request.
Before this fix: `intent=answer`, silently proceeded. After: `intent=ask`,
routed to `qwen3:32b`, no downgrade at all -- the credited VRAM was
enough for the daemon to actually reach the model this was supposed to
route to all along.

> Guarded by `tests/test_core.py::TestDetectAccelerator` (`loaded_model`/
> `loaded_model_mb` captured off a real-shaped `ollama ps` line,
> `max_loaded_models` defaults to 0 when the probe is unavailable, never
> a guessed fallback) and three new `fits()` cases: eviction credit
> applied when `max_loaded_models == 1` and a *different* model is
> resident, withheld when `max_loaded_models` is unknown, and not
> double-counted when the resident model is the one being checked

### 2.36 `pipefail` catches a real upstream failure and an entirely benign `head`-truncated pipeline the same way -- found by reading real traffic, not by auditing code

**Rule:** `shell()` forgives a pipeline's non-zero exit only when its final stage exited 0 and every earlier non-zero stage was exactly 141 (SIGPIPE), computed from `${PIPESTATUS[@]}` captured first by `pipefail_wrapped`; any other upstream failure still fails. **Guard:** `tests/test_core.py::TestShellSigpipeForgiveness::test_a_genuine_upstream_failure_through_sort_and_head_still_raises` and `tests/test_core.py::TestShellSigpipeForgiveness::test_a_non_sigpipe_failure_on_a_middle_stage_still_raises`, and 2 more below.

Found mining the same real traffic §2.34/§2.35 came from, not a code
audit: a real operator hit the identical failure four times in a row
across escalating attempts -- *"what ar ethe files that are taking up
the most room"* → failed, *"reprint in gb"* → failed, *"failed"*
(literally typed that alone), *"figure out the isse with du home
step"* → failed again, same error every time. Reproduced directly:
`du -ah /home | sort -rh | head -20` -- an entirely ordinary, correct
command -- fails under this torra's `pipefail` wrapper with exit 141.

The mechanism: `head` closes its input after 20 lines; `sort`/`du`
upstream, still trying to write more, get killed by `SIGPIPE` (exit
128+13=141); `pipefail` reports that as the pipeline's own failure even
though `head` already produced exactly the right output on stdout. This
is the identical mechanism, working exactly as designed, that `pipefail`
was added for in the first place (§2.1/`TestShellPipefail`'s own
docstring: `sudo something | sort | head` silently reporting success
with empty output when `sudo` alone fails) -- except here the thing it
"catches" is correct behavior, not a masked failure. Two entirely
different real-world situations produce the exact same exit code, and
plain `pipefail` cannot tell them apart.

Distinguishing them needs per-stage exit codes (`$PIPESTATUS`), not just
the pipeline's own overall `$?` -- and capturing `$PIPESTATUS` correctly
has its own trap, found live while building the fix, not from
documentation: bash resets `PIPESTATUS` after *any* subsequent simple
command, **including a bare variable assignment**. The first draft did
`command; ec=$?; ...` and always saw the assignment's own exit status
(0) instead of the pipeline's, silently forgiving every failure,
including the exact `sudo | sort | head` case `pipefail` exists for --
caught only because the fix was verified against that scenario
explicitly before shipping, not assumed safe from reading the code.
Fixed by capturing `${PIPESTATUS[@]}` into an array as the *first*
statement after the command runs, with nothing -- not even reading `$?`
-- in between, then computing the overall exit code by hand from that
array using the identical rule `pipefail` itself uses (rightmost
non-zero stage, else 0), so the fix never depends on bash's own
already-reset `$?` at all.

The forgiveness rule itself: the recomputed exit code is overridden back
to 0 only when the *final* pipeline stage exited 0 **and** every earlier
non-zero stage was specifically 141 (SIGPIPE). A real failure anywhere
in the pipe -- wrong flag, missing command, an auth failure standing in
for the original `sudo` case -- is never forgiven, because its exit code
is essentially never exactly 141; the only realistic way to see 141 on a
non-final stage is a downstream reader closing the pipe early, exactly
the benign case this exists to recognize. `_pipefail_wrapped()` is
shared by both `shell()` and `_shell_streaming()` (§2.32) -- one wrapper
builder, not two copies to keep in sync.

Proved the fix works, not just the concept: CI failed on the first push
of this exact fix, on this exact test. Not a bug in `_pipefail_wrapped`
-- the test used the real `/home` directory, and the GitHub-hosted
runner's `/home` has a permission-restricted path under it that `du`
itself can't fully read, so `du` returns a genuine, real exit 1 (found
live via `chmod 000` reproducing the identical shape locally) alongside
the SIGPIPE the test meant to exercise. The fix correctly refused to
forgive that -- exit 1 is neither 0 nor 141 -- exactly as designed; the
test was the thing that was wrong, conflating "a real machine's `/home`,
permissions and all" with "a directory this test actually controls".
Fixed by building the test's own directory in `tmp_path` instead, same
command shape, no permission surprises to depend on.

> Guarded by `tests/test_core.py::TestShellSigpipeForgiveness`: a fast,
> deterministic `yes | head` case for CI, the actual real-world
> `du | sort | head` shape kept as its own test, and -- the part that
> actually matters -- `test_a_genuine_upstream_failure_through_sort_and_head_still_raises`
> and `test_a_non_sigpipe_failure_on_a_middle_stage_still_raises` proving
> the original `pipefail` protection still holds; all of
> `TestShellPipefail`'s pre-existing cases pass unchanged

### 2.37 `.env` loading is explicit-path, not `load_dotenv()`'s own default search, and never overrides a real `export`

**Rule:** `.env` is loaded by `cli.py`'s `_load_env_file()` from the repo root by explicit path (never `load_dotenv()`'s working-directory search) and never overrides a variable already set in the real environment. **Guard:** `tests/test_cli.py::TestLoadEnvFile`.

Added so `OPENROUTER_API_KEY` (and anything else) can live in a `.env`
file at the repo root instead of shell config -- `cli.py`'s
`_load_env_file()`, called first thing in `main()`, before argument
parsing even, so it runs ahead of every command that could construct an
`OpenRouterClient()` (`build_torra()`, HANDOFF.md §2.27).

Two choices worth stating so a future edit doesn't quietly undo them:

- **Explicit path (`Path(__file__).resolve().parent.parent / ".env"`),
  not `load_dotenv()`'s bare default call.** The default searches
  upward from the current working directory -- fine for a project you
  always run from inside, wrong for this one: `torra` is meant to be
  run from wherever the operator happens to be (README's own `alias
  torra=...` example), so the default search could just as easily
  load an unrelated `.env` belonging to whatever *other* project's
  directory the operator's shell happens to be sitting in at the time.
- **Never overrides a variable already set in the real environment**
  (`load_dotenv()`'s own default -- deliberately not passed
  `override=True`). An explicit `export OPENROUTER_API_KEY=...` in the
  shell always wins over the file, so the file is safe to leave around
  with a stale or placeholder value without it silently shadowing a
  real one set elsewhere.

`.env` is gitignored; only `OPENROUTER_API_KEY` is read from it (see README.md's
OpenRouter section for the expected key), never a real value committed anywhere.

> Guarded by `tests/test_cli.py::TestLoadEnvFile`: a not-yet-set variable
> gets loaded, an already-set one is never overridden, a missing file is
> a silent no-op, and `main()` is proven to actually call it (not just
> that the function works in isolation)

### 2.38 The launch greeting stopped listing models inline; `f2` opens a picker instead, filtered through the same `router._chat_capable()` the routing path itself trusts

**Rule:** The `f2` model picker lists only models that pass `router._chat_capable()` plus the hand-verified `VERIFIED_OPENROUTER_MODELS`; `None` (dismissed) is a no-op and `""` (automatic routing) clears the override through `_set_model_override`. **Guard:** `tests/test_tui.py::test_f2_lists_chat_capable_models_and_selecting_one_sets_the_override` and `tests/test_tui.py::test_choosing_automatic_routing_clears_an_existing_override`, and 2 more below.

`_greet()`'s healthy branch used to dump `self.ollama.models()` as a raw,
unfiltered comma list -- including embedding-only models like
`nomic-embed-text` (the exact model §"OS-Engineering Guardrails" already
names as a real routing bug once it was mistaken for a chat target,
`torra/gates/router.py`'s `ROUTING_RULES` fix). The new `ModelPicker`
(mirrors `SessionPicker`'s `ModalScreen[str | None]` shape) filters through
`tab.torra.agent.router._chat_capable()` before listing anything, so the
picker can never surface a model routing itself wouldn't choose.

Two return values that look similar but must stay distinct:

- **`None`** (Escape, or the modal dismissed with nothing chosen) -- the
  handler must no-op. `action_choose_model`'s `_handle` checks `chosen is
  not None`, not `if chosen:`, specifically so this stays distinguishable
  from the next case.
- **`""`** (the "(automatic routing)" option) -- a real, explicit choice
  to clear the override, routed through the *same* `_set_model_override`
  every `/model` (bare, no argument) call already uses, not a separate
  code path. `Option(..., id="")` was verified live (a standalone Textual
  reproduction) to round-trip through `OptionList`/`OptionSelected`
  cleanly -- `Option.id` is stored as-is with no DOM-id-style validation,
  unlike a mounted Widget's `id`, which does reject some strings (an
  earlier, unrelated `StepCard` bug already found that distinction).

Beyond that, `torra/tui.py`'s `VERIFIED_OPENROUTER_MODELS` adds a small,
hand-maintained list of `openrouter/...` slugs to the picker -- not a live
catalog fetch. `OpenRouterClient` has no `models()` call at all (unlike
`OllamaClient.models()`, which the local half of the picker actually
calls), and OpenRouter's real catalog is hundreds of entries deep -- not
something a picker meant for a handful of taps should try to enumerate or
filter live even if it could. Each entry in the list is only added after
a real live slug lookup plus an actual `torra plan`/`ask` call through
it succeeded (`x-ai/grok-4.6` and `deepseek/deepseek-v4-pro`, both
confirmed this way) -- the same "measure, don't guess" discipline this file
applies to `docs/*.md` and routing rules elsewhere. Remote entries are
tagged `(remote, paid)` in the picker so a tap doesn't trigger a paid call
by accident. Anything beyond that verified list still goes through
`/model <name>`, which is unaffected by any of this -- it never consulted
the picker's list in the first place.

> Guarded by `tests/test_tui.py::
> test_f2_lists_chat_capable_models_and_selecting_one_sets_the_override`,
> `test_choosing_automatic_routing_clears_an_existing_override`,
> `test_escape_dismisses_the_model_picker_without_changing_the_override`,
> `test_f2_lists_the_verified_openrouter_models_and_they_are_selectable`

### 2.39 `ctrl+m` was bound to the picker in §2.38 and shipped -- it never worked, because it's the terminal's own alias for Enter, and `pilot.press()` can't catch that class of bug

**Rule:** No `TorraApp` binding may use a key that is a terminal alias (`ctrl+m` is Enter); the model picker is on `f2`, and a test checks `BINDINGS` against `textual.keys.KEY_ALIASES`. **Guard:** `tests/test_tui.py::test_no_app_binding_uses_a_key_that_is_really_a_terminal_ansi_alias`.

Found live, from a user report ("ctrl+m shortcut not working"), after the
binding had already merged and its own tests were green. `ctrl+m` is the
ANSI C0 control code for carriage return -- the literal byte a physical
Enter key sends in a normal terminal -- and Textual's own
`textual/keys.py` documents it as a hard alias:
`KEY_ALIASES = {"enter": ["ctrl+m"], "tab": ["ctrl+i"],
"escape": ["ctrl+left_square_brace"], "ctrl+at": ["ctrl+space"],
"ctrl+j": ["newline"]}`. A real terminal cannot produce a `ctrl+m`
keypress distinct from `enter` at all -- there is no byte sequence left to
send. In practice, pressing it while the prompt `Input` has focus (the
common case) just submitted whatever was typed.

The tests added alongside the binding (`test_ctrl_m_lists_...`,
`test_escape_dismisses_the_model_picker_...`) all passed, and did not
catch this, because `Pilot.press("ctrl+m")` calls `App._press_keys()`,
which injects a `Key` event named `"ctrl+m"` directly -- it never goes
through the byte-level ANSI decoder a real terminal session runs input
through, so it can't reproduce the collapse that a live terminal forces.
The tests genuinely proved the binding was wired to the right action; they
could not and did not prove a human could ever trigger it. This is the
same shape of gap `HANDOFF.md` elsewhere calls out for other guards
(`CLAUDE.md`'s "the guard goes green while the problem persists,
invisible") -- except here the guard (`pilot.press`) was never wrong to
use, it just answers a narrower question than "does this work for a real
operator" and nothing before this flagged that gap.

Fixed by rebinding to `f2` (`torra/tui.py`'s `TorraApp.BINDINGS`) --
function keys use their own multi-byte escape sequences, entirely outside
the C0-control-code aliasing class `KEY_ALIASES` documents, and `f2` isn't
claimed by `Input`'s own `BINDINGS` either (unlike `ctrl+d`/`ctrl+w`,
which the existing `App` bindings of those names are *already* shadowed
by whenever the prompt has focus -- found live in the same pass,
documented in `tests/test_tui.py::test_ctrl_d_runs_a_dry_run_when_idle`'s
own docstring, but out of scope to fix here since neither one was what
the user reported broken).

`tests/test_tui.py::
test_no_app_binding_uses_a_key_that_is_really_a_terminal_ansi_alias` now
checks `TorraApp.BINDINGS` itself against `textual.keys.KEY_ALIASES`,
via `Binding.make_bindings()` (the same normalizer Textual's own
`BindingsMap` uses, so it handles the full `str | tuple | Binding` union
`BINDINGS` entries are allowed to be) -- so the next binding added to this
class of dead key never needs a live user report to be caught. It does
*not* check the separate `Input`-shadowing class (`ctrl+d`/`ctrl+w`
above) -- that would fail on bindings that already exist today and is a
different, wider fix than this section covers.

> Guarded by `tests/test_tui.py::
> test_no_app_binding_uses_a_key_that_is_really_a_terminal_ansi_alias`

### 2.40 Attempting to close §6.1's 82% live-ask gap: one approach measured worse and was reverted, one measured real and shipped -- rule 6's "Do not plan a task" was quietly forbidding torra from doing its own job

**Rule:** `SYSTEM_PROMPT` rule 6 must not forbid planning for a question the observed state cannot answer: a safe read-only step is proposed instead of telling the operator to run it; a repair check on any flagged clarifier gap was measured worse and reverted. **Guard:** `tests/test_agent.py::test_rule_6_no_longer_flatly_forbids_planning_for_a_question`.

Two different-shaped attempts at the §6.1 finding (the model asks for only
~18% of requests a human calls genuinely ambiguous), in order:

**Attempt 1, reverted: a repair-loop check forcing the model to justify
overriding any flagged clarifier gap.** Added `_gap_overridden_silently`,
firing whenever `turn.clarifier_gaps` was non-empty, intent wasn't "ask",
and no repair had run yet. Broke 6 existing tests immediately
(`test_a_clean_answer_with_no_task_never_wastes_a_repair_round`,
`test_a_short_answer_that_happens_to_open_with_checking_is_not_flagged`,
`test_stats_reports_turns_repairs_and_context_usage`, three routing
tests) -- the root cause, confirmed live: `no_target` (the dominant gap
code) fires on nearly any short conversational question lacking a path,
quantity, hostname, or version -- `"what is the disk usage?"`, `"hello"`,
`"were there any errors overnight?"` all trigger it. `clarifier_gaps
non-empty` is not a proxy for "genuinely ambiguous"; it is close to the
default state for ordinary English. Firing on it unconditionally would
have taxed the majority of ordinary turns with an extra local-model
round-trip, not just the override cases it targeted. Reverted
(`git checkout -- torra/agent.py`) before it reached a commit. The
obvious next move -- fire only for some subset of gap *codes* that
"really" mean it -- was rejected without trying: picking which codes
deserve enforcement is the same closed-vocabulary bet §3's table already
measured failing twice elsewhere in this file.

**Attempt 2, shipped: rule 6 was telling the model not to do the one
thing this torra exists for.** Investigating why real traffic showed
`"is there anything wrong with my disks"` producing *"Please check the
output of 'lsblk' and 'df -h' for more details"* instead of an answer
found the mechanism directly: rule 6 said *"If the request is a question
about the machine... use intent 'answer'... Do not plan a task"* --
flatly, for every question, even ones the observed state doesn't already
answer and a safe read-only command trivially would. Nothing in the code
ever enforced this (a `plan`-intent reply to a question was never
rejected), so this was a pure prompt-wording gap, not a capability one --
exactly the shape of the dd/mkfs rule 7 fix (§6.3), not a new detection
mechanism.

Live A/B tested against the real local model before shipping (a
scripted-transport unit test cannot show whether prompt wording changes
real model behavior) -- 8 requests, current vs. revised wording, same
machine state:

| Request | Current rule 6 | Revised rule 6 |
|---|---|---|
| "is there anything wrong with my disks" | asked / deflected (varied by run) | **plan**: `lsblk`/`df` |
| "are there any failed systemd services..." | asked the operator to run it | **plan**: `systemctl --failed --no-pager` |
| "what shell do I use..." | asked the operator | **plan**: real shell/config check |
| "what is the current ufw firewall status..." | plan (already correct) | plan (unchanged) |
| "check disk space" | ask (genuinely ambiguous) | ask (unchanged) |
| "17 times 23" | answer, 0 repairs | answer, **+1 repair** (stray empty `task`, self-corrected) |
| "hello" | answer, 0 repairs | answer, **+1 repair** (same, traced and confirmed) |
| "thank you" | ask, 0 repairs | ask, 0 repairs |

3 of 4 real diagnostic-deflection phrases moved to a real plan; no
regression on arithmetic, greetings, or the one genuinely-ambiguous case
("check disk space" stayed "ask" under both). The real, accepted cost:
roughly half of pure-social/arithmetic turns in this sample picked up one
extra repair round -- traced directly (`hello` attached a stray
`"task": {"task_id": "greeting-ack", "steps": []}` under the revised
wording that it didn't under the current one) -- but that shape is
already caught cleanly by the existing `_answer_dropped_a_task` check
(§5.9 "The repair loop is not just for intent 'plan.'", no new failure
mode here), so it costs latency, not correctness.

This doesn't close §6.1's 82% number by itself -- it fixes a different,
related failure shape (deflecting a diagnostic back to the operator
instead of running it) discovered while investigating that number, not
the ask-vs-guess decision itself. §6.1's real next step is unchanged:
more real post-fix traffic, then re-measure.

> Guarded by `tests/test_agent.py::
> test_rule_6_no_longer_flatly_forbids_planning_for_a_question` (pins the
> wording so it can't quietly regress to "Do not plan a task"; the
> behavioral claim itself is the live A/B table above, not something a
> scripted-transport test can re-verify)

### 2.41 A step's `description` reached nowhere the operator could see it -- neither CLI surface ever printed the field at all

**Rule:** A step's `description` should say why the step exists, and both CLI surfaces (`print_plan()` and `approve_at_prompt()`) print it unless it is empty or only repeats `run`. **Guard:** `tests/test_cli.py::test_print_plan_shows_a_steps_description` and `tests/test_cli.py::test_approve_at_prompt_shows_the_steps_description`, and 2 more below.

Found from direct feedback: the plan pane read as a bare list of shell
commands with no explanation of why each one was there. Investigating
turned up two separate gaps, not one:

- **The model was only ever asked for *what*, never *why*.**
  `PLAN_SCHEMA`'s `description` field said `"what this step does"` --
  rule 4 added no further guidance, so a model that dutifully restated
  `run` in prose (or left it blank) was fully compliant with what it was
  actually told. Reworded the schema hint and extended rule 4 to ask
  explicitly for the reasoning: what a step confirms, what it depends on
  from an earlier step, what it sets up for a later one -- and to say the
  overall shape in `message` for a multi-step plan, not repeat each
  step's own description there.
- **Even a well-written description had nowhere to go.** `cli.py`'s
  `print_plan()` and `approve_at_prompt()` never printed `step.description`
  at all -- confirmed live across every `torra plan` run earlier in
  this session, description text never once appeared. `tui.py`'s
  `StepCard` already had the display logic (skip when empty or identical
  to `run`); the CLI had no such logic because it had no logic for this
  field whatsoever. Added the same skip-if-redundant print to both CLI
  functions, in `approve_at_prompt()` specifically at the point the
  operator is deciding whether to tick the box, not just in the
  post-hoc plan listing.

Content quality still depends on the model -- live-tested, `devstral:24b`
produced descriptions ranging from genuinely useful ("clears the cache
confirmed stale by the earlier du check") to fairly shallow restatements
("List Docker images older than 30 days"). This is an honest,
capability-dependent ceiling (`HANDOFF.md`'s own site-knowledge-library
entry above makes the same caveat for a different feature), not
something further prompt-wording iteration was pursued to close -- see
§3's table for why chasing a closed-vocabulary-shaped fix on no new
measurement is the wrong move here too.

> Guarded by `tests/test_cli.py::
> test_print_plan_shows_a_steps_description`,
> `test_print_plan_does_not_repeat_the_command_as_a_description`,
> `test_approve_at_prompt_shows_the_steps_description`,
> `test_approve_at_prompt_does_not_print_a_description_that_only_repeats_run`

### 2.42 `execute()` used to reject an entire plan over one declined step -- now it skips just that step, plus a best-effort warning when an approved step looks like it depended on one

**Rule:** `execute()` returns REJECTED only when zero steps are approved; a declined step is skipped as `[skipped] not approved:` without setting `executed`, its undo is not staged, and `Torra.dependency_warnings` warns about dependent steps but never blocks. **Guard:** `tests/test_pipeline.py::test_a_mixed_approval_runs_the_approved_steps_and_skips_the_declined_one` and `tests/test_pipeline.py::test_declining_every_step_in_a_multi_step_plan_still_rejects_all_of_it`, and 7 more below.

Found from direct feedback: ticking some dangerous steps and leaving
others unticked ran nothing at all, not even the steps that were already
approved -- including safe steps that never needed a tick in the first
place. `_apply_approvals` already computed per-step approval correctly;
`execute()` just short-circuited to `Outcome.REJECTED` the instant
*any* step came back refused, before `_run_steps()` ever got a chance to
run the rest.

Three coordinated changes, none of which touch what "approved" means for
a single step (the named invariant, `test_a_dangerous_step_does_not_run_unapproved`,
is untouched and still passes unmodified):

- `execute()` only returns `REJECTED` (nothing at all attempted) when
  **zero** steps end up approved. A mixed approval now logs a "partial
  approval" decision and proceeds.
- `_run_steps()` gained a skip branch for `not sp.approved`, parallel to
  the existing resumed-step skip branch -- a declined step is recorded
  as `[skipped] not approved: <command>` and never reaches
  `_run_one_step()`. Deliberately does **not** set `result.executed`
  the way the resumed branch does: a resumed step really did run, for
  real, in an earlier attempt (`resolve_resume` vouches for it); a
  declined step never ran at all, so marking it "executed" would be
  false. (See §8's contract line and `test_execute_twice_...` for why
  `executed`'s exact semantics matter enough to spell this out.)
- `_stage_rollback()` now only pushes a step's undo when `sp.approved`
  -- staging one for a step that's never going to run would let a later
  rollback sweep try to "restore" a file that was never touched.

**The one risk this can't engineer away:** torra has no model of
dependencies *between* steps. Declining step 2 while approving step 3
might make step 3 fail or behave surprisingly, and nothing here can
detect that in general -- it's on the operator to judge when ticking
boxes. `Torra.dependency_warnings(plan, approvals)` (a separate,
public, side-effect-free static method, meant to be called by the
caller *after* collecting approvals and *before* calling `execute()`, so
the warning lands while the operator can still change their mind) covers
exactly one narrow, structural case: an approved step's command contains
a literal path from a declined step's `touches`. A plain substring
check, not a real dependency graph -- deliberately not attempting
anything cleverer, since that would be exactly the open-ended "list of
things to detect" §3's table already warns against. It cannot catch a
dependency on a file's *contents* rather than its path, and cannot rule
out a coincidental match; both CLI entry points and the TUI print/show
its output but never block on it.

Both `torra.dependency_warnings` and `PLAN_SCHEMA`/`SYSTEM_PROMPT`
(§2.41 above) were split into their own top-level modules
(`torra/dependency_warnings.py`, `torra/prompts.py`) in the same
change -- not a style preference, a measured one: `pipeline.py` and
`agent.py` both crossed the defensive-coding scorecard's 500-functional-LOC
Modularity threshold once these additions landed (GPA 3.83 -> 3.67),
and `scripts/dev/defensive_coding_report.py`'s Definition of Done
requires fixing that in the same change, not deferring it. Same
precedent as `resolve_resume()` moving into `core/resume.py` (§2.33):
a real, measured reason, not LOC-shuffling for its own sake. Neither
module lives under `torra/gates/` despite being safety-adjacent --
unlike the five real gates, `dependency_warnings` never blocks anything,
so filing it there would misrepresent what it does.

> Guarded by `tests/test_pipeline.py::
> test_a_mixed_approval_runs_the_approved_steps_and_skips_the_declined_one`,
> `test_declining_every_step_in_a_multi_step_plan_still_rejects_all_of_it`,
> `test_a_declined_steps_undo_is_not_staged_for_a_later_rollback_sweep`,
> `test_dependency_warnings_flags_an_approved_step_using_a_declined_paths_output`
> + 3 more, `tests/test_cli.py::
> test_print_dependency_warnings_shows_a_real_hit_before_execute_would_run`,
> `tests/test_tui.py::
> test_a_dependency_warning_shows_before_the_declined_steps_sibling_runs`

### 2.43 The live reasoning stream moved from one inline chat line to its own sidebar tab -- found live: rapid chunks racing two `remove()`-then-`mount()` calls crashed with `DuplicateIds`

**Rule:** `ThinkingPane` mounts one `Static` at `compose()` and updates it in place with the synchronous `Static.update()`; it never does `remove_children()` then `mount()` per token, which raced into `DuplicateIds`. **Guard:** `tests/test_tui.py::test_many_rapid_thinking_chunks_do_not_race` and 3 more below.

Direct feedback: a single dimmed line inline in the chat, replaced in
place on every token, was unreadable past a sentence or two -- a real
reasoning chain runs to hundreds of lines. Moved to a third sidebar tab
(`ThinkingPane`, alongside Plan and Evidence), same ephemeral-by-design
contract as before (never written to `SessionStore` or telemetry, reset
the moment the turn finishes -- `SessionTab.reasoning_text`'s docstring
still says so).

The first implementation kept `PlanPane`/`EvidencePane`'s own pattern --
`await self.remove_children()` then `await self.mount(Static(...))` on
every call -- and it broke immediately under real use, not in review:
`on_thinking` can fire many times before Textual's event loop gets
around to running an earlier call's scheduled `mount()` coroutine, so a
second `remove_children()`/`mount()` pair landed while the first's
`mount()` was still pending, and both tried to mount a `Static`
carrying the same id -- `textual.widgets._widget_list.DuplicateIds`,
confirmed by a test with 30 rapid chunks and no pause between them (the
2-chunk case the other thinking tests already had did not reproduce it
reliably, since Textual's own scheduling doesn't always interleave two
calls that close together -- this is why the regression test uses 30,
not 2). `PlanPane`/`EvidencePane` don't hit this in practice because
neither is called more than once per event; `ThinkingPane.append()` is
called on every streamed token, which is the actual difference that
made the same pattern unsafe here.

Fixed by mounting exactly one `Static` at `compose()` time and updating
it in place via the synchronous `Static.update()` on every subsequent
call, instead of removing and remounting. `update()` has no `await`
inside it, so there is no window for a second call to land mid-mutation
-- the race isn't narrowed, it's structurally impossible. `_append_thinking`/
`_clear_thinking` call `ThinkingPane.append()`/`.reset()` directly now
(no `self.run_worker(...)` wrapper), since there's no coroutine left to
schedule.

> Guarded by `tests/test_tui.py::
> test_many_rapid_thinking_chunks_do_not_race` (30 chunks, no pause --
> the case that actually reproduced `DuplicateIds`),
> `test_append_thinking_grows_the_thinking_tab`,
> `test_clear_thinking_resets_the_thinking_tab`,
> `test_reasoning_text_survives_until_the_next_question_is_asked`

### 2.44 `/auto on`: skip the Run click for a plan that already needed no tick, never for one that does

**Rule:** `/auto on` runs a plan without a Run click only when no step needs a tick, never a DANGEROUS or CRITICAL one (narrowed by §2.62: only when every step classifies SAFE); `auto_run` defaults off and is not persisted. **Guard:** `tests/test_tui.py::test_auto_run_never_bypasses_confirmation_for_a_dangerous_step` and `tests/test_tui.py::test_auto_run_defaults_to_off`, and 4 more below.

Direct feedback: even an all-SAFE/CAUTION plan (already auto-approved by
`Verdict.needs_confirmation`, no checkbox rendered at all) still required
a manual Run click in the TUI -- the CLI's `torra ask` never had this
friction, since `approve_at_prompt()`'s loop over `plan.needs_confirmation`
is simply empty for such a plan and `execute()` proceeds with no prompt at
all. `SessionTab.auto_run` (default `False`, runtime-only and not
persisted -- reopening a session must drop back to requiring a manual
click, same reasoning `model_override` already documents) closes that
one specific gap for the TUI, set per tab via `/auto on` / `/auto off`
(bare `/auto` reports the current state instead of toggling blind).

The one condition that matters: `_outcome_plan_ready` only calls
`execute_plan()` automatically when `len(plan.needs_confirmation) == 0`
**and** `tab.auto_run` is true -- both, not either. This is deliberately
never a way to relax what counts as needing confirmation in the first
place; a DANGEROUS/CRITICAL step still always renders its checkbox and
still always waits for an explicit human tick and an explicit Run,
`/auto` setting or not. (An earlier, considered and rejected version of
this request: let a DANGEROUS step with full undo coverage skip
confirmation too, on the theory that a verified rollback makes it safe
to auto-run. Rejected on the same precedent HANDOFF.md's domain
invariants already establish -- "a declared `rollback:` command is
still classified, a step's own `safe: true` can only make
`SafetyClassifier` more cautious, never override it": reversibility and
risk are different axes, and this codebase has already drawn that line
once. "Has an undo" says nothing about how bad the blast radius is
before that undo runs.)

> Guarded by `tests/test_tui.py::
> test_auto_run_executes_a_plan_whose_every_step_is_read_only`,
> `test_auto_run_never_bypasses_confirmation_for_a_dangerous_step`,
> `test_auto_run_defaults_to_off`, `test_auto_command_turns_it_on_and_off`,
> `test_bare_auto_command_reports_state_without_toggling`,
> `test_auto_command_rejects_a_garbage_argument`

### 2.45 Session and Model got their own sidebar tabs alongside Plan (relabeled "Actions") and Thinking -- persistent siblings of the `ctrl+o`/`f2` modals, not replacements

**Rule:** The Session and Model sidebar tabs are persistent siblings of the `ctrl+o`/`f2` modals and call the same handlers; with more than one tab open, sidebar queries are scoped through `self._tab_pane(session_id)`, never a bare `#sidebar`. **Guard:** `tests/test_tui.py::test_selecting_a_session_in_the_sidebar_tab_switches_to_it` and `tests/test_tui.py::test_sidebar_tabs_are_in_the_requested_order_with_actions_relabeled`, and 4 more below.

Direct feedback: reaching a past session or a model choice always meant a
modal overlay (`SessionPicker`/`ModelPicker`), even though the sidebar
already had room for exactly this. Added `SessionPane`/`ModelPane`,
mirroring `EvidencePane`'s own "start empty, refresh on `TabActivated`"
contract -- `_sidebar_session_activated`/`_sidebar_model_activated` are
new handlers with the identical shape as the existing
`_sidebar_evidence_activated` (`@on(TabbedContent.TabActivated,
"#sidebar", pane="#sidebar-<name>")`, not a bare `"#sidebar"` selector,
for the same reason documented there). `f2` and `ctrl+o` stay wired to
their modals -- this is an added, persistent alternative for keeping the
list in view while deciding, not a replacement; both paths call the
exact same `_set_model_override`/`_open_or_switch_to` so they can't
drift into showing or doing something different from each other.
`ModelPane.refresh_models` deliberately reuses `action_choose_model`'s
own local-plus-`VERIFIED_OPENROUTER_MODELS` assembly rather than
re-deriving it, for the same reason.

Tab order is now Actions, Thinking, Session, Model, Evidence -- "Plan"
relabeled "Actions" is a display-string change only; the class stays
`PlanPane`, the id stays `#plan`/`sidebar-plan`, `_plan_pane()` is
unchanged. It is still the gated proposal (steps, undo, Run/Reject/Dry
run) -- "Actions" is how it reads to the operator, not a different
concept underneath.

Writing the reorder's own test caught a real ambiguity worth flagging
for the next person testing anything sidebar-related with two tabs open:
`app.query_one("#sidebar", TabbedContent)` is not session-scoped -- every
session tab composes its own nested sidebar sharing that same id
(HANDOFF.md §2.30's exact ambiguity, previously only documented for
`#plan`/`#evidence`), and `query_one` does not raise on the collision,
it silently returns the first breadth-first match. With only one tab
open this never bites; the moment a test (or a future handler) opens a
second tab and then queries bare `"#sidebar"`, it can silently operate
on the *other* tab's sidebar. The fix is the same one `_plan_pane(session_id)`/
`_evidence_pane(session_id)` already use: scope through
`self._tab_pane(session_id).query_one("#sidebar", ...)`, never a bare
app-level query, once more than one tab might be open.

> Guarded by `tests/test_tui.py::
> test_sidebar_tabs_are_in_the_requested_order_with_actions_relabeled`,
> `test_the_sidebar_session_tab_renders_saved_sessions_on_activation`,
> `test_selecting_a_session_in_the_sidebar_tab_switches_to_it` (the one
> that caught the `#sidebar` scoping ambiguity above),
> `test_the_sidebar_model_tab_renders_models_on_activation`,
> `test_selecting_a_model_in_the_sidebar_tab_sets_the_override`,
> `test_selecting_automatic_routing_in_the_sidebar_model_tab_clears_the_override`

### 2.46 A read-only step's real output now enters the model's own memory, tagged by path -- and it can say a path no longer helps

**Rule:** A step's real output enters the model's context only when it declares `reads` and completed for real (not dry-run, failed or skipped), tagged by those paths and delimited as untrusted data (§2.66); `forget` drops a path. **Guard:** `tests/test_pipeline.py::test_a_completed_read_steps_output_feeds_back_into_agent_context` and `tests/test_pipeline.py::test_a_dry_run_never_feeds_read_output_into_context`, and 6 more below.

Direct feedback, in two parts: "let me see what files it's accessing," and
(once investigating that surfaced a real gap) "let the model drop content
that turned out not to matter." The gap found first: execution output
never re-entered `Agent.context` at all. `Agent._remember()` only ever
stored the operator's request text and the *proposed step commands*
(`turn.spec.steps[i]['run']`) -- real `StepResult.output` from
`Torra.execute()` is a separate call, entirely disconnected from
`Agent.converse()`, and nothing threaded it back. Ask a follow-up in the
same conversation about what a `grep`/`cat` step actually found, and the
model had no memory of it -- only that it once proposed running the
command.

Closed with a new declared field, `reads: list[str]` on `Step` -- the
read counterpart to `touches`, validated and parsed identically (same
`_validate_path_list`, same `STEP_FIELDS` entry, same `to_steps()`
plumbing). Declared, not inferred from `command`: guessing a path out of
an arbitrary shell command is exactly the kind of regex-guessing this
project's own evasion-detection work already measured missing 5 of 12
cases (`SafetyClassifier.resolve`'s whole reason for existing). Once a
step declaring `reads` actually completes (real run, not dry, not
failed, not skipped-as-declined), `Torra._finish_success()` feeds its
real output into the model's context, tagged by every declared path.
`PLAN_SCHEMA` also gained a top-level `forget: list[str]` the model can
name in any later reply -- a path whose remembered content no longer
helps gets dropped from context on that same turn, before the new
exchange is recorded. Both are documented to the model as rule 10 in
`SYSTEM_PROMPT`, explicit about not forgetting by default: "do this when
it is genuinely no longer useful... forgetting something you still need
means guessing at it instead next time."

One thing checked and found not to apply: a read step being *declined*
rather than *skipped for some other reason*. `cat`/`grep` classify SAFE
regardless of which file they target -- `SafetyClassifier` reasons about
command destructiveness, not file sensitivity, so there is no real path
by which a read-only step ever needs an explicit tick in the first
place. An early test written for "a declined read step's output must
not leak" was deleted once this was confirmed live
(`SafetyClassifier().classify('cat /secret.env').needs_confirmation ==
False`) -- it was testing a scenario the classifier makes unreachable,
not a real gap.

`ReadMemory` (the mechanism itself: `remember`/`forget`/`reset`, plus
the free function `remember_step_reads` that `Torra` calls) was split
into its own module, `torra/read_memory.py`, in the same change --
`agent.py` and `pipeline.py` both crossed the 500-functional-LOC
Modularity threshold the moment this landed inline (GPA 3.83 -> 3.67),
same real, measured reason `dependency_warnings.py`/`prompts.py` were
split out earlier (§2.41/§2.42). One real wrinkle in doing that split:
`torra.pipeline_types` already imports `Turn` from `torra.agent`, so
an unconditional `from .pipeline_types import RunResult` inside
`read_memory.py` -- needed only for `remember_step_reads`'s type hint --
would have completed a real import cycle (`agent -> read_memory ->
pipeline_types -> agent`) and failed at interpreter startup. Guarded
behind `if TYPE_CHECKING:` instead: mypy still resolves the real type,
Python itself never executes that import. `from __future__ import
annotations` alone does not fix this -- it defers *annotations* to
strings, but a module-level `import` statement runs regardless of that
future import.

> Guarded by `tests/test_core.py::TestTaskSpecStepValidation` (`reads`
> cases), `tests/test_read_memory.py` (14 cases: `ReadMemory.remember`/
> `.forget`/`.reset` and `remember_step_reads`, direct import so this
> module registers on the scorecard's Testing dimension, not just
> reached transitively through agent.py/pipeline.py),
> `tests/test_agent.py::test_agents_read_memory_is_wired_to_its_own_context`,
> `test_converse_acts_on_forget_before_remembering_the_new_turn`,
> `test_reset_clears_read_memory_too`, `tests/test_pipeline.py::
> test_a_completed_read_steps_output_feeds_back_into_agent_context`,
> `test_a_dry_run_never_feeds_read_output_into_context`,
> `test_a_failed_read_steps_output_never_feeds_into_context`

### 2.47 Run stayed clickable (and green) after a real run completed -- a second click never re-ran anything for real, but it did reprint the whole prior transcript, reading as "the run repeated"

**Rule:** The TUI asks via `ConfirmDialog` before dispatching Run on a plan whose `executed` is already True and short-circuits `_execute_plan_body` instead of reprinting the transcript; `Torra.execute()` and `_refuse_replay` are untouched. **Guard:** `tests/test_tui.py::test_a_second_run_click_asks_first_and_does_not_duplicate_the_transcript`.

`Torra.execute()` already refused to actually replay commands on an
already-executed `RunResult` (`_refuse_replay`, keyed on
`result.executed`, §2.8) -- that part was never broken. What was broken:
`_set_busy` only disables Run/Dry-run/Reject *while* `busy=True`, then
re-enables them the moment a run finishes, so Run stayed clickable
(still green, `variant="success"`) after a completed real run. A second
click dispatched `execute_plan()` again, which called
`tab.torra.execute()` again, which hit `_refuse_replay` and handed
back the *same* `RunResult` with its `results` list untouched --
`_executed()` then iterated that list and re-said every step's
"ok/fail ... Ns" line, the completed/failed message, the agent summary,
and the rollback report a second time. No command actually ran twice,
but the operator watching the chat pane could not tell that from "the
chat repeating."

Fixed in the TUI layer only -- `Torra.execute()`/`_refuse_replay`
itself is untouched, per this file's own "ask first" rule for that
method. Three pieces, all in `torra/tui.py`:

1. `pressed_run` now checks `tab.current.executed` before dispatching at
   all. If a real run already completed on this exact plan, it pushes a
   new `ConfirmDialog` (a small `ModalScreen[bool]`, same shape as
   `SessionPicker`/`ModelPicker`) instead of calling `execute_plan()`
   directly. `result.executed` is False for a REJECTED-with-nothing-
   approved outcome and for a BLOCKED-before-any-step-ran outcome (see
   §2.8's fix) -- both stay retryable with no prompt, unchanged.
2. `_execute_plan_body` now checks `not dry_run and result.executed`
   *before* calling `tab.torra.execute()` at all, and short-circuits
   with one dim "Nothing to run -- this plan's commands already ran."
   line rather than letting the call reach `_refuse_replay` and then
   reprinting the untouched `results` list a second time. Reaching this
   path at all means the operator already confirmed via (1) -- this is
   not a second way to skip that confirmation, since `execute_plan()`
   only ever gets called after a `True` dismiss.
3. `_executed()` calls a new `_mark_run_used()` after a genuine (not
   short-circuited) real completion, which sets the `#run` button's
   `variant` to `"default"` -- the same grey look `"Dry run"` already
   has unstyled in the same button row, reused rather than inventing a
   new visual. `PlanPane.show()` fully rebuilds the button row for every
   new plan, so this needs no explicit reset when a fresh plan arrives.

> Guarded by `tests/test_tui.py::
> test_a_second_run_click_asks_first_and_does_not_duplicate_the_transcript`
> -- spies on `Torra.execute` to assert it is called exactly once
> total (never a second time, not even a refused one, since the TUI now
> short-circuits before reaching it), asserts the confirmation dialog
> appears before anything runs, that cancelling adds zero chat lines,
> and that confirming adds exactly one.

### 2.48 A sixth sidebar tab, Metrics: spend, token use, model/hardware stats, and which files a session's read-only steps have actually fed into the model

**Rule:** The Metrics sidebar tab labels per-session figures (`Agent.stats()`) separately from client counters shared by all tabs ('since app start'), and probes model info fresh rather than showing a cached guess. **Guard:** `tests/test_tui.py::test_the_sidebar_metrics_tab_shows_session_stats_and_files_read` and 1 more below.

Requested alongside the `reads`/`forget` memory mechanism (§2.46) --
that entry covers *what the model can see*; this one covers *what the
operator can see about what the model is costing and running on*.
`MetricsPane`, wired in after Evidence (`Actions, Thinking, Session,
Model, Evidence, Metrics`), refreshed on activation like the other
three real-data tabs, not continuously.

The one real design decision here: two genuinely different scopes sit
side by side in this pane, and it says which is which rather than
blurring them together. `Agent.stats()`'s `context_tokens`/
`context_usage`/`compressions`/`turns`/`repairs` are real per-session
numbers -- one `Agent`, one `ContextCompressor` per tab. But
`TorraApp.ollama` and `TorraApp.backends` are **single instances
shared across every tab** (`TorraApp.__init__` takes both from the
first tab's `Torra` and hands the same objects to every later
`_new_torra()` call, confirmed by reading that constructor, not
assumed) -- so `OllamaClient.prompt_tokens`/`completion_tokens`/`calls`
and `OpenRouterClient.cost_usd` are cumulative **since the app
started**, across every tab, not this session's own spend. The pane
labels those lines "(since app start)" rather than presenting a
per-session number this architecture cannot actually produce -- the
same honesty this project already applies to rollback's three-state
`undone`/`unverified`/`failed` (§2's own table) rather than collapsing
an unprovable case into a comfortable-looking two.

Model size/context-ceiling/thinking-capability is a **fresh** real
`OllamaClient.model_info()` probe of whichever model this tab last
actually used (`plan.model` if a plan exists, else the `/model`
override, else "(automatic routing)"), not a stale cached guess --
same reasoning as the routing-guardrail entries in this file's OS-
engineering section. A `openrouter/...` model gets a plain "no local
size/context data" note instead, since this client has no equivalent
metadata endpoint. The accelerator/`ollama ps` line reuses
`detect_accelerator()` directly (a narrow, cheap, single-purpose real
probe) rather than routing through `Agent.observe(refresh=True)`,
which would also re-run `model_info()` against every locally pulled
model just to get one hardware line -- disproportionate for a tab that
might get opened many times in a session.

"Files read this session" is `agent.read_memory.paths.keys()` --
exactly the set §2.46's `ReadMemory` is already tracking, surfaced here
rather than duplicating that bookkeeping.

Reformatted after the first version shipped: each grouping (model in
use, hardware, this session, local model calls, `openrouter` calls) was
one comma-joined sentence that wrapped across the sidebar's width --
readable in principle, cluttered in practice. Now one bold heading per
grouping, one fact per line underneath it, a blank line between groups.
The hardware section no longer reuses `Accelerator.render()` wholesale
(that method returns one prose line by design, prefixed `"accelerator:
"`, for the model's own context and the old single-line status bar) --
it reads `accelerator.kind`/`.name`/`.free_vram_mb`/`.vram_mb`/
`.running_on` directly instead, so it can lay them out the same
one-fact-per-line way as everything else in this tab.

> Guarded by `tests/test_tui.py::
> test_the_sidebar_metrics_tab_shows_session_stats_and_files_read`
> (drives a real `reads` step through Run, then asserts the model-in-use
> line, the hardware heading, the per-session turn count, the shared
> client's cumulative-calls line, and the read file path all appear) and
> the updated `test_sidebar_tabs_are_in_the_requested_order_with_actions_relabeled`.

### 2.49 Every Qwen-origin model in the four-model catalog was swapped for a non-Chinese equivalent (operator request), and the swap itself broke the live `ollama.service` twice

**Rule:** The routing catalog holds no Qwen-origin models: `gpt-oss:20b` for debug/plan/analyze, the ambiguous fallback and `OllamaClient.fallback_model`, `devstral-small-2:24b` for code; `muse-glimmer:30b` was benchmarked, dropped and removed. **Guard:** `tests/test_gates.py::TestRouting` and `tests/test_core.py::test_fallback_model_default_is_actually_pulled`, and 2 more below.

Operator request, not a measured gap: no Chinese-origin models in the
routing catalog. Chosen from public benchmarks (SWE-bench Verified,
LiveBench), not by name, the same discipline this file already demands
for anything that isn't a guess:

| Role | Old (retired) | New | Why this one |
|---|---|---|---|
| debug/plan/analyze, `AMBIGUOUS_FALLBACK_MODEL` | `qwen3:32b` → `muse-glimmer:30b` (see correction below) | `gpt-oss:20b` (OpenAI, Apache 2.0) | measured best intent accuracy (11/13) and speed (171 tok/s) of 4 models on torra's own battery |
| code | `qwen3-coder:30b` | `devstral-small-2:24b` (Mistral AI, Apache 2.0) | 65.8% SWE-bench Verified vs. the un-benchmarked model it replaces |
| `OllamaClient.fallback_model` default | `qwen3.5:latest` | `gpt-oss:20b` (OpenAI, Apache 2.0) | smallest of the new catalog, same role |

`devstral:24b` (deploy/quick) was already non-Chinese and is untouched
-- folding it into `devstral-small-2:24b` too was considered and
deliberately not done, since nothing measured that combination and it
wasn't what was asked. Same honest caveat this file already applies to
`qwen3-coder:30b`'s original "obvious fit by name" status now applies
to both replacements: public benchmark numbers are not this torra's
own 12-task JSON-planning battery in `scripts/dev/model_bench/
REPORT.md`, and neither has been run through it. Every historical §2
entry above that names `qwen3:32b`/`qwen3-coder:30b`/`qwen3.5:latest`
by name (§2.9, §2.35, the "quick" routing note near this file's OS-
engineering section) describes what was true of *those* models on the
day it was found -- it does not carry over to their replacements
automatically, and is left as-written rather than rewritten, the same
way §2.35 layered a root cause onto §2.34 instead of editing it.

**The swap exposed two more live wires in the process, both fixed, neither
part of the original ask:**

1. `muse-glimmer:30b` requires Ollama >=0.32.8 for the NVIDIA backend;
   this machine ran 0.23.1. The operator ran the official installer
   themselves (`curl -fsSL https://ollama.com/install.sh | sh`) --
   upgrading a root-owned binary and restarting a live system service is
   outside what this agent will do unattended, on-request or not.
2. The installer's fresh systemd unit switched `ollama.service` to a new
   dedicated `ollama` system user, replacing whatever ran it before
   (evidently `antoine`, the owner of every file under
   `/mnt/ParamoStorage/AIModels`). `/mnt/ParamoStorage` itself is
   `drwx------ antoine antoine` and `AIModels/blobs` is the same --
   the new user could not traverse into the mount at all, then (after a
   scoped `setfacl -m u:ollama:--x` traverse grant) could not read the
   blobs it needed. Fixed by reverting to the previous, already-correct
   permission model instead of chasing per-directory ACLs: a new
   `/etc/systemd/system/ollama.service.d/override-user.conf` drop-in
   pins `User=antoine`/`Group=antoine`, layered on top of the existing
   hand-tuned `override.conf` from the 2026-08-10 incident (`Requires=
   mnt-ParamoStorage.mount`, the 5-restart start limit) rather than
   replacing it -- both drop-ins apply, base unit last.

**Correction, same day: `muse-glimmer:30b` did not survive contact with
torra's own benchmark.** The table above already reflects the
final state; this is the measurement that produced it. Once all three
replacements were pulled, `scripts/dev/model_bench/bench.py` was
actually run against the new catalog (`devstral-small-2:24b`,
`muse-glimmer:30b`, `gpt-oss:20b`, `devstral:24b` -- results in
`scripts/dev/model_bench/results.json`, 2026-08-28) -- the measurement
this whole swap had been missing, same discipline as `qwen3-coder:30b`'s
original "not yet benchmarked" caveat. `muse-glimmer:30b` tied
`devstral-small-2:24b`/`devstral:24b` on intent accuracy (10/13) but ran
at ~6x their mean latency (50.9s vs. 7-15s) and was the only one of the
four that errored: a hard 300s timeout on `t8-timer-with-quantities`
with zero tokens generated.

Root-caused live rather than left as an unexplained number: four
identical back-to-back `/api/chat` calls to an already-warm
`muse-glimmer:30b` showed `load_duration` *growing* call over call (8s,
19s, 32s, 36s) instead of collapsing to ~0 the way an already-resident
model should -- a real per-call reload/session-setup tax under this
machine's `OLLAMA_MAX_LOADED_MODELS=1` config. `gpt-oss:20b` showed the
same pattern (noisier, still double digits every call), just less of
it, being the lightest model in the catalog (~13GB vs. muse-glimmer's
~18GB) -- consistent with this being a cost of the model's size/newness
in Ollama (muse-glimmer needed the just-upgraded 0.32.8+ runtime) rather
than a defect specific to muse-glimmer's reasoning quality. `gpt-oss:20b`
separately measured the best intent accuracy of all four (11/13) at the
fastest generation speed (171 tok/s), with zero errors -- a real win on
torra's own battery, not a name-based guess. `torra/gates/
router.py`'s `AMBIGUOUS_FALLBACK_MODEL` and the debug/plan/analyze
`ROUTING_RULES` entries were updated to `gpt-oss:20b` accordingly.
`muse-glimmer:30b` was removed from the local catalog (`ollama rm`)
once `gpt-oss:20b` took over its role; nothing here rules out
re-pulling and re-measuring it if a future Ollama release changes
this machine's reload behavior for its
architecture.

> Guarded by `tests/test_gates.py::TestRouting` (`CONTRACT`,
> `TestHardwareAwareRouting`, the ambiguous-fallback pair) and
> `tests/test_core.py::test_fallback_model_default_is_actually_pulled`,
> all re-pinned to the final model names (`gpt-oss:20b` for debug/plan/
> analyze/ambiguous-fallback, `devstral-small-2:24b` for code). No test
> exists for the systemd user/ACL fix -- it isn't code this repo ships,
> it's this specific machine's service configuration; verified live via
> `systemctl status ollama.service` (active, no restarts) and `ollama
> list` showing all three new models pulled. The muse-glimmer timing
> finding is verified by the bench run itself
> (`scripts/dev/model_bench/results.json`) plus the standalone
> back-to-back `/api/chat` reproduction described above (not itself
> committed as a test -- it was a live diagnostic against the real
> daemon, the same category of verification this file's OS-engineering
> section already treats as legitimate alongside the unit-test suite).

### 2.50 A live indicator sat where nobody was looking -- moved next to the prompt, plus a real way to abandon an in-flight plan request

**Rule:** The busy indicator `#busy-line` sits directly above the prompt, and Escape abandons an in-flight plan request UI-side only (`SessionTab.generation` discards the late result); a running `execute_plan()` never responds to Escape. **Guard:** `tests/test_tui.py::test_escape_stops_an_in_flight_plan_request_and_discards_its_late_result` and `tests/test_tui.py::test_escape_does_nothing_while_idle_or_while_a_run_is_executing`, and 1 more below.

Two complaints, same root cause: the only busy feedback was the shared
`#status` line, docked at the very bottom of the whole screen *below*
the sidebar -- far from the prompt input an operator is actually
watching after they hit enter. It was always updating correctly (10fps
spinner, elapsed seconds, confirmed live in this session's own earlier
end-to-end run); nobody could see it from where they were looking, which
reads identically to "there's no indicator" from the outside.

Fixed with a second, per-tab widget: `#busy-line`, sitting directly
above `#prompt` (a plain, non-docked `Static` in `#left`'s normal flow,
placed between `#chat` and the `dock: bottom` `#prompt` -- confirmed
live that ordering two `dock: bottom` siblings does NOT position by
compose/CSS order the way it might look like it should; only one widget
in a container may safely dock to a given edge for this to lay out
predictably). `_tick_spinner` now writes the same spinner/elapsed text
to both `#status` and `#busy-line` -- `#busy-line` needs no "only if
active tab" guard the way `#status` does, since it's a real per-tab
widget, so a backgrounded tab's own state stays correct without polling.

The second complaint -- "there's no way to stop thinking" -- exposed a
real constraint, not just a missing button: `OllamaClient.chat()`'s
non-streaming path (any non-thinking-capable model, i.e. most routing
outcomes) is one blocking `urlopen()` + `.read()` with no checkpoint at
all; Python cannot forcibly kill a thread blocked on a real syscall from
outside it. So this is *not* real cancellation of the network call --
it's UI-side abandonment: `SessionTab.generation`, bumped by the new
Escape binding (`action_stop_thinking`), lets `_planned()`/the new
`_report_plan_error()` recognize a result that arrives after the
operator gave up on it and discard it silently, while the UI frees up
immediately. Deliberately scoped to the planning phase only --
`execute_plan()`'s busy state never responds to Escape, since
interrupting a real, approved shell step mid-write is a safety hazard,
not a UI convenience, and nothing about `Torra.execute()`/`reject()`
changed to make that possible.

> Guarded by `tests/test_tui.py::
> test_the_busy_line_above_the_prompt_shows_the_same_live_spinner`,
> `test_escape_stops_an_in_flight_plan_request_and_discards_its_late_result`
> (a real 1.2s-delayed scripted reply, confirms Escape frees the UI
> immediately and the late result never renders a plan), and
> `test_escape_does_nothing_while_idle_or_while_a_run_is_executing`.
> Confirmed live against the real local Ollama daemon: Escape mid-request
> frees the prompt instantly; the abandoned background call finishing
> ~1s later changed nothing (`busy` stayed False, no plan appeared).

### 2.51 An interpreter payload that reopens a path it already read classified SAFE -- no notion of "this overwrites a file it just loaded"

**Rule:** An interpreter `-c` payload that reopens a quoted path-shaped literal it already used is DANGEROUS (`_repeated_path_literal`), matched on the repeated literal rather than per-library method names; refined by §2.55. **Guard:** `tests/test_gates.py::TestSafety::test_an_interpreter_overwriting_a_file_it_also_read_is_not_silently_safe`.

Found via `scripts/dev/model_bench/domain_exploration.py`'s xlsx-edit
scenario (2026-08-28, `devstral:24b`): asked to edit an existing
spreadsheet, the model proposed one `python3 -c` step --
`openpyxl.load_workbook('budget.xlsx') ... wb.save('budget.xlsx')` -- with
no backup made and no rollback declared. `SafetyClassifier` classified it
SAFE: `_judge`/BACKSTOP only recognize shell-idiom overwrites (a `>`
redirect onto a protected prefix) and named destructive programs (`rm`,
`dd`, `mkfs`); an interpreter payload that reads a path and writes that
same path back out was invisible to both, so it would have run with no
TUI checkbox at all (`needs_confirmation` -- the actual gate `tui.py`'s
checkbox reads -- is DANGEROUS/CRITICAL only; CAUTION renders no checkbox
either, so reaching CAUTION alone would not have fixed this).

Fixed in `_judge_script_source` via a new `_repeated_path_literal` check:
a quoted, path-shaped literal appearing 2+ times in an interpreter's `-c`
source is treated as DANGEROUS ("reopens this path, no declared backup").
Matching the *repeated literal itself*, not a per-library method name
(`.save(`, `.to_excel(`, `.write(`, ...), is the deliberate choice --
enumerating save-spellings library by library is exactly the open-ended
"list of things to detect" §3 already documents as a losing shape. This
catches the pattern regardless of which library (openpyxl, python-docx,
odfpy, plain `open()`) is doing the overwriting.

> Guarded by `tests/test_gates.py::TestSafety::
> test_an_interpreter_overwriting_a_file_it_also_read_is_not_silently_safe`.

### 2.52 Read-only observation of a second, real machine ("executor") over SSH -- explicitly not remote execution

**Rule:** Executor SSH access is read-only observation: nothing the model writes becomes an `ssh` command, `observe_executor` is called only from `Agent`'s default observer, and with `TORRA_EXECUTOR_HOST` unset no SSH is attempted; remote writes exist only as §2.53's torra-built steps. **Guard:** `tests/test_core.py::TestExecutorObservation` and 4 more below.

Operator asked whether torra could "reach across to executor if needed."
Two ways to do that exist, with very different risk: read-only observability
(feed real facts about a second machine into the model's context) vs. actual
remote execution (let a step really run there). The second would need
`SafetyClassifier` to understand `ssh <host> "..."` the same deliberate way
it already unwraps `sudo`/`env`/`timeout`/`sh -c` -- a new evasion class --
and breaks undo entirely (`UndoGenerator` only snapshots local files).
Operator chose read-only observability. This entry covers only that; remote
execution was deliberately not built as a side effect.

**Anti-goal, worth re-reading before extending this:** nothing here adds a
new way for the model to cause anything to run on executor. No new
`Step.kind`. `TaskSpec.to_steps()`/`SafetyClassifier` never see an
executor-bound command. The only caller of the new SSH-runner machinery is
`Agent.__init__`'s own default observer, via `_observe_with_executor()`.
`grep -rn "build_ssh_runner\|observe_executor" torra/` shows exactly two
files: the definitions in `torra/core/machine.py` and the one call site in
`torra/agent.py`. Don't let that grow past two without re-reading this
entry first. Also: don't ever call `observe_executor()` with its own nested
executor config (no chained third machine) -- `MachineState.render()` has no
cycle guard, and would recurse forever on a self-referential or circular
`.executor` chain.

Implementation reuses `observe_machine()`/`detect_accelerator()`'s existing
injectable-`runner` DI pattern completely unchanged -- `build_ssh_runner()`
just returns a runner matching `shell()`'s exact contract (same signature,
raise-on-nonzero/return-stripped-stdout) that wraps each command in a
non-interactive SSH call instead of running it locally. `BatchMode=yes` plus
a closed stdin guarantees ssh can never sit waiting on a prompt nobody is
watching -- that would otherwise hang an unrelated, local-only turn.
`resolve_executor_config()` reads `TORRA_EXECUTOR_HOST`/`_USER`/`_SSH_KEY`
from the environment (same explicit-overrides-env, `.env`/`load_dotenv()`
precedent as `OPENROUTER_API_KEY`) -- `None` (feature entirely absent, zero
SSH ever attempted) when `TORRA_EXECUTOR_HOST` is unset, which is the
default for anyone without it configured. `MachineState.executor` is
attached inside `Agent.__init__`'s default observer closure, so it's
refetched on exactly the same cache/refresh cadence
(`Agent.observe(refresh=...)`) as local facts already are, for free.
`SYSTEM_PROMPT` gained one rule telling the model the executor block, when
present, is informational only -- confirmed live it holds: asked to
"restart the ollama service on executor," the model asked a clarifying
question about the service name rather than either restarting the *local*
service by mistake or proposing an `ssh` step.

Confirmed live against the real executor box (`10.0.0.44`): `torra ask
--non-interactive "what does executor look like right now"` correctly
reported its real hostname, kernel, GPU (RTX 2070 SUPER, real free VRAM),
disk, and memory, answered directly from observed state with zero steps
proposed.

> Guarded by `tests/test_core.py::TestExecutorObservation` (11 cases:
> `resolve_executor_config`'s env/explicit-arg precedence,
> `observe_executor`'s reuse of `observe_machine` and its never-raises
> guarantee on both a failing runner and a `subprocess.TimeoutExpired`,
> `build_ssh_runner`'s argv shape and `shell()`-matching raise contract,
> `MachineState.render()`/`.to_dict()` with and without an executor block)
> and four `tests/test_agent.py` cases (config reaching the default observer,
> the no-config default path never touching SSH at all, the executor state
> sharing the local state's cache, and the new `SYSTEM_PROMPT` rule's
> presence). No live-SSH test is in the automated suite -- `10.0.0.44` is a
> specific external host, not the always-present-on-any-runner exception
> `systemctl`/`dpkg-query`/`inotify` get; the manual check above is the
> real-network verification instead.

### 2.53 Editing a file on `executor`: pull-edit-push, never model-authored `ssh`, an unconditional CRITICAL floor, and a real snapshot-based undo

**Rule:** A file on `executor` is edited only by pull-edit-push: `remote_pull`/`remote_push` text is torra-authored (never model-authored `ssh`), a push is CRITICAL unconditionally, and a failed pre-push snapshot leaves it uncovered (`capture_remote_file` returns `None`), never a faked undo. **Guard:** `tests/test_core.py::TestRemoteEditSteps` and `tests/test_pipeline.py::TestRemotePushEscalation`, and 1 more below.

Follow-up to §2.52's read-only observation: the operator asked for actual
edits, not just visibility. Two designs exist, with very different risk --
raw SSH command passthrough (the model writes `ssh host "..."` text
directly) vs. pull-edit-push (torra pulls the file locally, an ordinary
step edits the local copy, a single always-escalated step pushes it back).
Operator chose pull-edit-push. **The model never authors ssh/scp text
anywhere in this mechanism** -- `SafetyClassifier` needed zero new evasion
surface, because it never has to resolve a remote command at all.

**The anti-goal, worth re-reading before extending this:** no new way for
the model to cause anything to run on `executor` outside this one, narrow,
torra-controlled mechanism. `grep -rn "build_remote_pull_action\|
build_remote_push_action\|escalate_remote_push" torra/` shows exactly
three files: the definitions in `torra/core/remote_edit.py`, the call
site in `torra/core/tasks.py`'s `to_steps()` (pull/push actions) and
`_validate_step` (field requirements), and the call site in
`torra/pipeline.py`'s `_gate()` (the escalation). Nothing under
`torra/gates/safety.py` changed at all.

**Two new `Step.kind` values, real meaning for the first time.**
`Step.kind` (`torra/core/tasks.py`) was already a real field, already in
`STEP_FIELDS`, already settable by the model -- confirmed by grepping the
whole tree before this change that *nothing* branched on it anywhere.
`remote_pull`/`remote_push` are the first values that do. For these two
kinds only, `Step.command`/`Step.run` are **torra-authored, deterministic
text** (`f"pull {remote_path} from executor"` / `f"push local edit back to
{remote_path} on executor"`), not the model's own `run` field -- the model
still writes a `run` value (required by `_validate_step`, unchanged for
every other kind), but it's ignored for what actually executes and what
gets classified on these two kinds specifically. This is the one deliberate,
narrow exception to `Step`'s own docstring invariant ("command is both what
executes and what gets classified... the single most important invariant in
torra") -- the invariant's *spirit* still holds exactly, it's just
torra code authoring that text instead of the model, precisely so the
model can never smuggle arbitrary command text into a remote-write path.
**Never extend this exception to any other kind.**

**How a step actually moves a file.** `remote_pull` declares `remote_path`
(source, on executor) and `touches: [<local path the model chooses>]`
(destination); its real action fetches the remote content over the
executor's SSH runner (`torra.core.machine.build_ssh_runner`, §2.52,
reused unchanged) and writes it to that local path -- an ordinary local
file after that, editable by a completely normal local step with the
existing, unchanged pipeline. `remote_push` declares the same `remote_path`
and `reads: [<that local path>]`; its real action reads the local file and
uploads it, base64-encoded in transit (so arbitrary real content --
newlines, quotes, anything -- survives the single-command-string interface
the runner requires, without the model ever having to construct that
encoding itself).

**The CRITICAL floor is unconditional, not content-based.**
`Torra._gate()` calls `escalate_remote_push(verdict, step.kind)`
(`torra/core/remote_edit.py`) right after `SafetyClassifier.classify()`.
Torra-authored command text for a push step names no real program
`SafetyClassifier` recognizes, so it classifies SAFE on its own almost every
time -- irrelevant, because the floor applies regardless. **CRITICAL, not
DANGEROUS**: unlike a local DANGEROUS step, a remote write has no local
audit/conflict-detection safety net around it, and its only undo
(`capture_remote_file`, next paragraph) can silently be *absent* if the
pre-push snapshot itself failed -- CRITICAL guarantees this always surfaces
with the strongest visual weight the UI has, never blending in with a
routine step.

**Undo: `UndoGenerator.capture_remote_file()`** (`torra/gates/undo.py`) is
the direct remote mirror of `capture_file()`, same real, provable bar (a
`verify` that re-checks actual state, not "the call didn't raise"). Snapshot
happens at gate time (`for_step()`, before the push ever runs), stored in
the *same local* shadow-dir mechanism `capture_file` already uses. Present
case: shadow the current remote content, `undo` re-uploads it, `verify`
re-fetches and compares by digest. Absent case (the push creates a new
remote file): `undo` deletes it, `verify` re-confirms absence -- exactly
`capture_file`'s own `FILE_ABSENT` shape. **If the snapshot itself fails for
any reason** (no executor configured, unreachable, permission denied,
anything) `capture_remote_file` returns `None` -- the push step is honestly
**"uncovered"**, the same `dd`/`mkfs` philosophy this file already
documents ("declared uncovered rather than inventing an inverse it cannot
guarantee"), never a fabricated or best-effort undo. The operator sees 0%
coverage for that step and decides whether to approve it anyway, same
choice they already make for any other uncovered dangerous step today.

**Undo only ever runs during a real failure sweep in the same `execute()`
call -- confirmed live, and true for local files too, not remote-specific.**
`_finish_success()` calls `self.undo.cleanup()` on any COMPLETED run ("Nothing
failed, so nothing rolls back to these snapshots") -- shadow copies,
including a remote snapshot, do not survive past a successful run. Found by
hand while writing this entry's own live verification: a first draft tried
calling a push step's `UndoAction.undo()` *after* `execute()` had already
returned `COMPLETED`, and it failed with a real `FileNotFoundError` on the
already-cleaned-up shadow file -- not a bug, the system was never designed
to support manual undo after success, for any file, local or remote. The
real, supported path is `_sweep_rollback()` firing automatically when a
*later* step in the *same* run fails.

**Confirmed live, twice, against the real executor box (`10.0.0.44`):**
first, a real pull → real local edit → real push, confirmed by an
independent `cat` over a fresh SSH connection that the remote content
genuinely changed. Second, the same three steps followed by a step that
deliberately fails (`false`), triggering a real `_sweep_rollback` in the
same run -- `done.rollback.undone` showed the remote restore action, and an
independent `cat` afterward confirmed the real remote file was genuinely
back to its original content. Both runs used a real, disposable
`/tmp/...` file on executor, cleaned up afterward.

> Guarded by `tests/test_core.py::TestRemoteEditSteps` (6 cases:
> `to_steps()` builds torra-authored pull/push actions and ignores the
> model's own `run` text, real content flows through correctly including
> base64 round-trip, clean failure with no executor configured, and the
> three new `_validate_step` field requirements), `tests/test_gates.py::
> TestCaptureRemoteFile` (6 cases: present/absent/failure snapshot cases,
> `for_step()` wiring, and that an ordinary step never touches the executor
> runner at all), and `tests/test_pipeline.py::TestRemotePushEscalation`
> (5 cases: the unconditional floor confirmed against the classifier's own
> SAFE verdict on the same text, no escalation for `remote_pull`, a full
> pull-edit-push run with a real captured undo, an uncovered push still
> requiring approval, and a failing push triggering the normal rollback
> sweep with zero new recovery logic). `torra/core/remote_edit.py` is a
> new module, split out of `tasks.py`/`pipeline.py` immediately -- both
> crossed this project's 500-functional-LOC modularity threshold the
> moment this landed inline (same real, measured reason
> `dependency_warnings.py`/`read_memory.py`/`resume.py` were split out
> earlier), confirmed by `scripts/dev/defensive_coding_report.py` before
> and after the extraction.

### 2.54 `zip` repacking a document over its own path was the sibling gap to §2.51 -- xlsx/odt are both zip containers, and `zip` had no dispatch rule at all

**Rule:** `zip` is dispatched with `tee`/`cp`/`mv` (`_judge_tee_cp_mv_zip`), so zipping onto a protected path is DANGEROUS; `unzip` has no `_judge` rule but, as a `known_programs` writer (§2.60), runs CAUTION, or DANGEROUS when any operand, the archive included, is protected; relative document paths are covered by §2.55. **Guard:** `tests/test_gates.py::TestSafetyCoverageEdges::test_zip_repacking_over_an_existing_home_directory_file_is_dangerous` and `tests/test_gates.py::TestSafetyCoverageEdges::test_zip_to_an_unprotected_path_is_not_escalated`.

§2.51 closed the single-interpreter-script overwrite shape
(`load_workbook('x.xlsx') ... .save('x.xlsx')`). The other natural way to
edit a document that's actually a zip container (xlsx and odt both are) is
extract the archive, edit the inner XML, and repack with `zip` -- and
`devstral:24b` reached for exactly that, unprompted, when asked to edit an
`odt-edit` scenario in `domain_exploration.py` ("extract it, modify the
content, and then repack it"). `zip` had no `PROGRAM_RISK` entry and wasn't
in any `_judge` dispatch rule, so `zip -r contract.odt content.xml`
classified SAFE even onto a path under `/home` -- the exact same protected-
prefix check that already makes `cp`/`mv` onto that path DANGEROUS
(`test_mv_to_an_unprotected_path_is_not_escalated` proves that check
exists) simply never got a chance to run for `zip`, since `zip` wasn't
dispatched to it at all.

Fixed by adding `zip` to the same dispatch as `tee`/`cp`/`mv`
(`_judge_tee_cp_mv_zip`, renamed from `_judge_tee_cp_mv` since it now
covers a fourth program) -- `zip`'s output archive is a write target
exactly like `cp`/`mv`'s destination, so the existing protected-prefix
check applies unchanged. `unzip` was deliberately left out: its archive
argument is a *read* source, not a write target, so reusing this same
generic operand-protected check for it would misclassify an ordinary
extraction of an existing file (`unzip /home/user/archive.zip -d /tmp`) as
"writes to a system path" -- a real false positive, not a hypothetical
one, since the operand-protected check has no notion of which argument is
being read versus written. No measured case shows `unzip` itself
overwriting something it shouldn't (e.g. `unzip -o` onto a protected
destination), so no rule was added for it -- adding one now would be
guessing, exactly what HANDOFF.md §3 already warns against.

> Guarded by `tests/test_gates.py::TestSafetyCoverageEdges::
> test_zip_repacking_over_an_existing_home_directory_file_is_dangerous`
> and `test_zip_to_an_unprotected_path_is_not_escalated` (the negative
> case, confirming zip onto a non-protected path stays unescalated the
> same way `mv`/`cp` do).

### 2.55 Testing §2.51/§2.54 against real xlsx files, not just crafted corpus strings, found a false positive, a relative-path blind spot, and one unrelated pre-existing false positive

**Rule:** `_repeated_path_literal` flags only differently named calls on one path (`_call_signature`), a write onto a relative document-container path (`DOCUMENT_CONTAINER_EXTENSIONS`, e.g. `zip -r budget.xlsx`) is DANGEROUS, and `_mask_quoted` keeps a `>` inside quotes from reading as a redirect. **Guard:** `tests/test_gates.py::TestSafety::test_reading_the_same_path_twice_is_not_an_overwrite` and `tests/test_gates.py::TestSafety::test_zip_repack_over_a_relative_document_path_is_still_dangerous`, and 5 more below.

Actually running xlsx-editing commands (not just classifying strings) is
what surfaced all three -- the kind of gap a corpus of hand-written test
cases doesn't reliably hit.

**False positive:** `_repeated_path_literal` (§2.51) counted bare
occurrences, so two independent, read-only `load_workbook()` calls on the
same path -- comparing a file to itself, validating before processing,
both completely ordinary -- classified DANGEROUS with zero writes
anywhere. Fixed with `_call_signature`: a read-then-write has a real
signature (two *differently*-named calls touching the same literal --
`load_workbook`/`save`, `read_excel`/`to_excel`), while the same call name
repeated is just a re-read. `open()` gets its own signature
(`open:write`/`open:read` by mode) since it's the one call whose read and
write paths share a name.

**Relative-path blind spot:** `_judge_tee_cp_mv_zip` (§2.54) reused the
same `protected` flag `cp`/`mv` already used, which only recognizes
absolute paths under `PROTECTED_PREFIXES` (system locations). A step with
a declared `cwd` inside the user's own document folder -- ordinary --
edits with a bare relative filename, so `zip -r budget.xlsx ...` sailed
through as SAFE with no leading `/` in sight: the exact repack-in-place
shape §2.54 was supposed to close. `cp`/`mv` onto the same relative name
are equally unprotected today (pre-existing, not introduced by §2.54);
this closes it only for the risk category this investigation is about via
a new, small, closed extension set (`DOCUMENT_CONTAINER_EXTENSIONS`:
xlsx/xlsm/docx/pptx/odt/ods/odp) checked independent of `_protected` --
deliberately not a blanket escalation of every relative-path
`cp`/`mv`/`zip`, which would need its own corpus work against `ROUTINE`
to avoid a false-positive regression on ordinary file management.

**Unrelated pre-existing false positive, found the same way:**
`_check_redirect`'s final catch-all scanned raw text for a bare `>`, so
an entirely ordinary Python comparison inside a `-c` payload
(`wb.active.max_row > 2`) read as an "output redirect that overwrites"
and escalated to CAUTION -- realistic xlsx-editing scripts lean on
comparisons like this constantly. Neither §2.51 nor §2.54 touched this.
Fixed with `_mask_quoted`, a new sibling to `split_segments`'s own
quote/escape state machine: blanks every character inside a quoted span
before either redirect regex runs, so a `>` inside quotes is invisible to
the check while a real, unquoted redirect elsewhere in the same line is
untouched.

> Guarded by `tests/test_gates.py::TestSafety::
> test_reading_the_same_path_twice_is_not_an_overwrite`,
> `test_open_builtin_read_then_write_same_path_is_still_caught` (positive)
> and `test_open_builtin_read_twice_same_path_is_not_flagged` (negative),
> `test_zip_repack_over_a_relative_document_path_is_still_dangerous`
> (positive) and `test_zip_to_a_relative_non_document_path_is_not_escalated`
> (negative), `test_a_python_comparison_operator_is_not_mistaken_for_a_redirect`
> and `test_a_real_redirect_next_to_a_quoted_string_is_still_caught`
> (negative, a genuine redirect next to an unrelated quoted argument on
> the same line must still be caught).

### 2.56 Two operator-facing TUI asks: the Thinking tab cleared the instant an answer landed, and a plan needing approval had no signal anywhere near the prompt

**Rule:** The Thinking tab is cleared at the start of the next `plan_request()`, not when a turn ends, and a plan needing approval writes a flag to `#busy-line` via `_flag_needs_approval`. **Guard:** `tests/test_tui.py::test_reasoning_text_survives_until_the_next_question_is_asked` and `tests/test_tui.py::test_a_plan_needing_approval_flags_the_busy_line`.

**Thinking tab timing.** `_clear_thinking` ran at the end of every turn --
success (`_planned`), error (`_plan_request_body`'s except branch), and
Escape-abandon (`action_stop_thinking`) all wiped it the moment that turn
ended, so an operator who glanced away mid-answer had nothing left to read
by the time they looked back. Moved the single clear to the *start* of
`plan_request()` instead -- the one place every path back into a new turn
already goes through, so it's a single change point rather than three, and
it now stays exactly as the model left it until the operator actually asks
something else, regardless of how the prior turn ended.

**Approval flag.** A plan with DANGEROUS/CRITICAL steps only ever signalled
"needs a tick" two ways: a checkbox rendered per step (invisible unless
already on the Actions tab) and a line written to the shared `#status`
bar -- the exact same "docked at the bottom, below the sidebar, nowhere
near what the operator is actually looking at" problem §2.50 already fixed
for the busy/thinking spinner, just never extended to this moment. Added
`_flag_needs_approval`, writing directly to `#busy-line` (the same widget
`_tick_spinner` uses, reused here since nothing else needs it while idle)
whenever `_outcome_plan_ready` finds `pending > 0`. Cleared the same way
the busy spinner already is: the next `_set_busy(True, ...)` call (a new
question via `plan_request()`, or clicking Run/Dry-run) naturally
overwrites it, and `pressed_reject` clears it explicitly since Reject
never goes through `_set_busy` at all.

> Guarded by `tests/test_tui.py::
> test_reasoning_text_survives_until_the_next_question_is_asked` (replaces
> the now-inverted `test_a_completed_plan_request_leaves_no_reasoning_text_behind`)
> and `test_a_plan_needing_approval_flags_the_busy_line`.

### 2.57 Model pickers grouped local vs. remote (paid), got a best-case-uses legend, and stopped re-announcing a no-op re-pick

**Rule:** `ModelPane` and `ModelPicker` share `_model_options()` (disabled group headers, local before remote-paid) and a curated `MODEL_DESCRIPTIONS` legend (no entry shows no blurb); `_set_model_override` is a no-op when the value is unchanged. **Guard:** `tests/test_tui.py::test_the_model_picker_groups_local_and_remote_with_a_legend` and `tests/test_tui.py::test_repicking_the_current_model_is_a_no_op`, and 3 more below.

Three related operator-facing asks against `ModelPane`/`ModelPicker`
(the sidebar tab and the `f2` modal, which deliberately render identical
content -- see their own docstrings):

**Grouping.** `models` already arrives local-then-remote
(`local + VERIFIED_OPENROUTER_MODELS`), so the two were never actually
interleaved -- just not visually separated, with each remote entry only
distinguished by an inline `(remote, paid)` tag. Extracted the shared
option-building logic (previously duplicated near-identically in both
`ModelPane.refresh_models` and `ModelPicker.compose`) into module-level
`_model_options()`, which now inserts a **disabled** `Option` as a group
header before each non-empty group. Disabled, not a `Static` glued into
the list: `OptionList` skips a disabled entry for both click and keyboard
selection entirely (`find_next_enabled` in Textual's own
`action_cursor_down`), so a header can't be tapped into an accidental
override the way text appended onto a real Option's label could invite,
and "down" from "(automatic routing)" still lands directly on the first
real model with no dead keypress in between.

**Legend.** New `MODEL_DESCRIPTIONS: dict[str, str]`, one line per model
naming what it's actually for -- a name and a routing rule tell an
operator nothing about *why* gpt-oss:20b is the debug/plan/analyze
default versus devstral-small-2:24b for code. Deliberately a curated
dict, not derived from `router.py`'s `ROUTING_RULES`: a rule's own
rationale comment is several sentences of benchmark history and
superseded-model context written for whoever edits the router, not a
one-line UI blurb, and several rules can point at the same model. A model
missing an entry just shows no blurb (`_model_legend()` returns `""` and
callers skip mounting it) rather than a guessed one -- same discipline as
`VERIFIED_OPENROUTER_MODELS` right above it in the file: earn the entry,
don't guess it. Nothing enforces this dict staying in sync with
`ROUTING_RULES` -- update both by hand when a routing target changes,
same trust-based discipline this file already applies to READMEs.

**No-op re-pick.** `_set_model_override` unconditionally reassigned
`tab.model_override` and printed "model override set to X" even when `X`
was already the current override -- clicking the same model twice (or
picking "(automatic routing)" while already on it) read as if something
had just changed when nothing had. Now returns immediately when the
requested value already matches.

> Guarded by `tests/test_tui.py::
> test_the_model_picker_groups_local_and_remote_with_a_legend` and
> `test_repicking_the_current_model_is_a_no_op`; the option-count
> assertions in `test_the_sidebar_model_tab_renders_models_on_activation`,
> `test_f2_lists_chat_capable_models_and_selecting_one_sets_the_override`,
> and the down-press-counting in
> `test_f2_lists_the_verified_openrouter_models_and_they_are_selectable`
> were updated for the two new disabled headers (`OptionList.option_count`
> includes disabled entries even though keyboard navigation skips them,
> so a raw-index-based down-press count silently overshoots once headers
> exist -- fixed by counting only enabled options).

### 2.58 A completed real run left every step row looking exactly like it had before anything happened

**Rule:** After a real first-time completion `_mark_run_used` dims every step card (`.step-card--executed`) and greys the Run button, relabeling it 'Run (again)'. **Guard:** `tests/test_tui.py::test_a_second_run_click_asks_first_and_does_not_duplicate_the_transcript`.

`_mark_run_used` (§2.47: Run greys out and refuses to silently replay)
only ever touched the Run button's own colour. The step cards it applies
to -- the actual proposed commands, their verdicts, their checkboxes --
never changed at all once the plan actually ran, so a plan pane looked
identical whether nothing had happened yet or every step had just
executed for real; only the small button colour difference (and the
transcript, if you scrolled to it) said otherwise. Added
`.step-card--executed` (60% opacity) applied to every `StepCard` in
`_mark_run_used`, and changed Run's own label to "Run (again)" alongside
its existing grey `variant="default"` -- the button now says what a
second click means before `pressed_run`'s own confirm dialog
("This plan's commands already ran once. Run it again?", already in
place since §2.47) would. All three land together in `_mark_run_used`,
still called from exactly one place (`_executed()`, only on a real,
non-dry, first-time completion).

> Guarded by `tests/test_tui.py::
> test_a_second_run_click_asks_first_and_does_not_duplicate_the_transcript`,
> extended with the label and step-card-dimming assertions.

### 2.59 A rollback only reverses what torra itself did, in this attempt

**Rule:** A rollback reverses only what torra did this attempt: `_arm_step` pushes a step's undos just before it runs (never in a dry run), declared rollbacks run only if a step was armed (§2.71), and `drop_edited_undos` skips files changed outside torra. **Guard:** `tests/test_pipeline.py::test_a_block_before_any_step_ran_does_not_overwrite_the_external_edit` and `tests/test_pipeline.py::test_a_block_before_step_two_reverts_step_one_and_never_step_two`, and 6 more below.

`Torra.execute` used to push every approved step's derived undos, and the
plan's declared rollback entries, onto the undo stack before step one ran.
A conflict block ("external file change(s) before step X") reports FAILED
to the caller, so `_sweep_rollback` ran the whole stack -- including the
undo for a file a human had just edited. `UndoGenerator.capture_file`'s
restore copies the plan-time snapshot back over the target, so the very
edit the conflict check had just caught was overwritten and reported as
"undone". Reproduced 2026-10-03 with torra's own fixtures: after the
block, the file read `original` instead of the human's text. A blocked dry
run did the same, since the sweep had no dry-run check, and a declared
cleanup (`rm -rf` of a directory that already held someone's files, say)
ran even though no step had.

Three rules now hold together:

1. **A step's derived undos are pushed by `_arm_step` immediately before
   that step runs**, after its precondition and conflict guards, and never
   in a dry run. A block before step N therefore reverses steps 1..N-1 and
   nothing else, and a resumed step (§2.33) gets no undo.
2. **Declared rollback entries are classified up front but pushed only if a
   real step ran** (`RunResult.executed`), on top of the derived undos so
   they still run first. The stack is emptied at the start of every attempt,
   so a halted sweep's leftovers never run on behalf of a later plan.
3. **Before any sweep, `drop_edited_undos` drops the undo of every file the
   detector flagged** (`VerifiedRollback.drop_where`; called from
   `_block_on_conflict` and from the sweep itself, §2.71), so a human edit
   made after one of torra's own steps ran is not overwritten either. The
   skip is written to the decision log ("undo skipped ... changed outside
   torra") rather than silently omitted.

If you add another way for a run to stop early, it must go through the same
sweep with the same stack discipline: push before running, never in advance.
`Torra.execute` is at CCN 14, one below the ceiling, so new branches belong
in helpers.

> Guarded by `tests/test_pipeline.py::
> test_a_block_before_any_step_ran_does_not_overwrite_the_external_edit`,
> `test_a_block_before_step_two_reverts_step_one_and_never_step_two`,
> `test_a_blocked_dry_run_runs_no_undos`,
> `test_a_declared_rollback_does_not_run_when_no_step_ran`,
> `test_a_declared_rollback_still_runs_when_a_real_step_failed`,
> `test_a_conflict_after_a_step_ran_does_not_overwrite_the_edited_file`,
> `test_undos_left_by_an_earlier_halted_rollback_are_not_run_by_a_later_plan`, and
> `tests/test_gates.py::TestVerifiedRollback::test_drop_where_removes_only_the_selected_actions_and_keeps_the_order`

### 2.60 A command torra cannot prove read-only needs a tick

**Rule:** Unknown means ask: a resolved command with no rule finding is judged by `known_programs.trust()` -- read-only stays SAFE, bounded writers are CAUTION, known writers on protected paths and unknown programs are DANGEROUS; lists grow only from a measured false positive. **Guard:** `tests/test_known_programs.py::test_anything_unrecognised_or_not_provably_harmless_asks` and `tests/test_known_programs.py::test_trust_names_the_three_outcomes`, and 1 more below.

`SafetyClassifier` used to start from SAFE and only raise a verdict when a
rule matched, so any program no rule named ran with no tick on every path.
A probe on 2026-10-03 classified all of these SAFE: `blkdiscard
/dev/nvme0n1`, `dd if=/dev/zero of=/etc/passwd`, `eval "chmod -R 777 /etc"`,
`crontab -r`, `curl ... | /bin/bash`, `sed -i ... /etc/fstab`, `rsync
--delete`, `git push -f`, `kill -9 -1`, `mkswap`. A list of dangerous things
to detect cannot be complete (§3), so the default is inverted.

After every rule has had its say, a resolved command that drew no finding is
asked of `known_programs.trust()`: **read-only** stays SAFE, a **bounded
writer** (`tar`, `git pull`, `touch`, `python -m pytest`) runs flagged CAUTION,
a known writer aimed at a protected path or an **unknown program** is
DANGEROUS and asks. The lists name what is known, so a missing program fails
toward asking. They grow only from a measured false positive: replaying 174
real logged commands raised the ticks from 6 to 11 of 174 (3.4% to 6.3%), and the five that
were genuine read-only diagnostics were added.

Rules that make this hold:

1. A program named by a relative or odd path (`./ls`) is never taken for the
   system one.
2. `python -c` is read-only only when an AST check proves the payload pure:
   stdlib modules that cannot write or spawn, `open()` in read mode, no dunder
   or `getattr` escape, and `openpyxl` without `.save()`. This replaces the
   denylist scan §2.55 tuned as the only gate for reads: the reads it kept
   quiet (spreadsheets, CSV) still run unticked, everything else asks.
3. The resolver no longer invents commands: `2>&1` stays one command,
   `do`/`then` expose the command behind them, `(group)` parentheses and
   `$(...)` spans are not words, heredoc bodies fed to a reader are data, and
   `eval` re-resolves its argument.
4. `kill` with a negative pid or 0, and the device tools (`blkdiscard`,
   `sgdisk`, `mkswap`, `cryptsetup`, LVM removals), are CRITICAL.
5. Steps torra authors itself (`remote_pull`, `remote_push`) get verdicts from
   what they do (`settle_remote_pull`, `escalate_remote_push`), because their
   text names no real program.

Known writers now read CAUTION, not SAFE, which does not change what needs a
tick. Four older assertions that pinned SAFE for `zip`, `dd of=file` and
`kill <pid>` were updated to CAUTION on that basis.

> Guarded by `tests/test_known_programs.py` (every list, `trust()`, the
> resolver cases) and the unchanged `TestSafety.EVASIONS` and `ROUTINE` corpora.

### 2.61 A step stops at its first real error, and a timeout stops everything the step started

**Rule:** A step stops at its first real error (ERR trap under `pipefail`, SIGPIPE 141 still forgiven) and runs in its own session, so a timeout or Ctrl-C kills the whole process group; stdin is `/dev/null` and output is capped per stream (first and last 256 KiB). **Guard:** `tests/test_core.py::TestShellStopsAtTheFirstRealError` and `tests/test_pipeline.py::test_ctrl_c_during_a_step_is_a_failed_step_and_rolls_back`, and 2 more below.

Three gaps in how a step's command ran (`torra/core/process.py`):

1. **A masked failure passed.** The wrapper set `pipefail` but not errexit,
   so `false; true` and a script whose last line succeeded reported success,
   and no rollback fired. An `ERR` trap now exits with the rightmost non-zero
   status of the failed pipeline, using the same rule as the closing check:
   a SIGPIPE (141) upstream of a stage that exited 0 is still forgiven
   (§2.36). Conditions (`|| true`, `if`, `&&` lists) do not trip it. The
   closing check also reads the real `$?` first, so a step ending in a false
   `if` no longer fails. This is visible: a step that relied on a hidden
   failure (`grep -q x f; echo done`, `read x` at EOF) now fails and says so.
2. **A timeout left children running.** `proc.kill()` stopped only bash, so a
   timed-out `apt` kept running while rollback started, and Ctrl-C did not
   reach it either. Steps now run in their own session; a timeout, Ctrl-C or
   a failing output callback sends TERM to the group, then KILL.
   `Torra._run_one_step` also catches `KeyboardInterrupt`, so an interrupt is
   a failed step that reaches the rollback sweep instead of skipping it.
3. **Stdin was inherited and output unbounded.** Stdin is now `/dev/null`, so
   a prompt gets EOF instead of hanging a background process. Each stream
   keeps its first and last 256 KiB with an omission marker, so `yes` cannot
   fill memory. One implementation now serves both the plain and the live
   output paths.

> Guarded by `tests/test_core.py::TestShellStopsAtTheFirstRealError`,
> `TestShellRunsInItsOwnGroup`, the unchanged SIGPIPE tests, and
> `tests/test_pipeline.py::test_ctrl_c_during_a_step_is_a_failed_step_and_rolls_back`.

### 2.62 State kept across plans, and secrets written to disk, were both wider than one run

**Rule:** State is scoped to one plan and persisted text is scrubbed: `ConflictDetector.baseline()` starts empty each plan, persisted strings pass `redact_secrets`, `torra ask --non-interactive` stops unless `--yes` (a `--dry-run` is exempt), and `/auto on` runs only plans whose every step is SAFE. **Guard:** `tests/test_core.py::TestBaselineIsPerPlan` and `tests/test_tui.py::test_auto_run_waits_for_a_click_when_the_plan_changes_something`, and 10 more below.

Found by the 2026-10-04 full audit, not live.

1. **`ConflictDetector.baseline()` starts from nothing every time.** One
   detector lives for a whole TUI tab, and it used to add each plan's
   `touches` to `hashes` and keep the previous plan's `watch_roots` when a
   plan declared none. A hand edit to a file only plan 1 named then blocked
   the unrelated plan 2 with "external file change(s)", and each plan's
   inotify instance was opened without closing the last. `baseline()` now
   closes the old watch, empties `hashes`, and falls back to the roots the
   detector was constructed with, never the previous plan's.
2. **Every persisted string is scrubbed, not just `request`.**
   `TaskRecord.abort_reason` (up to 400 characters of a failed step's raw
   stderr) and session transcript entries (including a read step's output,
   which is replayed to the model on reopen) go through `redact_secrets`.
   The patterns now catch `sk-or-v1-...` keys (dashes inside the body),
   `NAME_API_KEY=` style assignments (`\b` never matches after `_`) and
   `Bearer` tokens. The audit JSON was not yet covered; §2.74 covers it.
   Model-written `task_id` and step ids were still written unscrubbed;
   §2.76 closes that.
3. **A reopened session keeps what the model had read.** `ReadMemory` took
   its persistence hook at construction, before the TUI wired it, so read
   output never reached the transcript. `_register_session` sets both hooks.
4. **A closed tab releases its file watches**, and the app releases every
   tab's on exit.
5. **`torra doctor` warns when `.env` is readable by group or others.**
6. **The classifier looks at every redirect and every spelling of a path.**
   `_check_redirect` stopped at the first `>`, so `cmd 2>/dev/null > /etc/fstab`
   was SAFE; it now judges each redirect (a `/dev/null` one is skipped, not a
   reason to stop). `_protected` compares a normalised path, so
   `tee /tmp/../etc/fstab` is caught. `rsync` with a remote operand
   (`host:`, `user@host:`, `rsync://`) needs a tick, like `scp`.
7. **A byte that is not UTF-8 cannot hang a step.** The reader threads decode
   with replacement and always send their end-of-stream marker; before, a
   finished command waited out its whole timeout and was reported as timed out.
8. **Nobody-watching execution is explicit.** `torra ask --non-interactive`
   shows the plan and stops unless `--yes` is given (`--dry-run` is exempt;
   `--verify --non-interactive` also needs `--yes`). The TUI's `/auto on`
   runs a plan on its own only when every step classifies SAFE (read-only); a
   plan with a CAUTION write waits for the Run click. Hand-written task files
   (`torra run`) are unchanged: a human wrote them.

> Guarded by `tests/test_core.py::TestBaselineIsPerPlan`,
> `tests/test_telemetry.py::TestRedaction` (the four new cases) and
> `test_a_label_line_with_a_field_from_a_later_version_still_loads`,
> `tests/test_session.py::test_a_secret_in_a_transcript_entry_is_redacted_on_disk`,
> `tests/test_tui.py::test_a_read_steps_output_is_written_to_the_session_transcript`,
> `test_closing_a_tab_releases_its_file_watches`, and
> `tests/test_cli.py::test_doctor_env_check_*`,
> `test_ask_non_interactive_without_yes_shows_the_plan_and_runs_nothing`,
> `test_ask_verify_non_interactive_without_yes_runs_nothing`,
> `tests/test_tui.py::test_auto_run_waits_for_a_click_when_the_plan_changes_something`,
> `tests/test_gates.py::TestSafety::test_every_evasion_is_caught` (five new cases) and
> `tests/test_core.py::TestShellSurvivesBinaryOutput`.

### 2.63 A service undo restores what the step changed, and a shadow directory's owner is recorded, not guessed

**Rule:** A service undo restores both active and `is-enabled` state for every named unit and never stops a unit whose state was `unknown`; each shadow directory's owner pid is recorded in a sibling `<name>.owner` file, not guessed from its name. **Guard:** `tests/test_gates.py::TestUndoGeneration` and `tests/test_tui.py::test_every_tab_gets_the_default_disk_precondition`.

Three gaps in the undo layer:

1. **The unit was read from a fixed shape.** `systemctl enable --now nginx`
   yielded the unit `--now`, and `restart a b` only ever saw `a`. Units now
   come from the arguments (`service_units`, `torra/gates/service_state.py`):
   flags are skipped and every named unit is captured.
2. **"Undone" was claimed on half the evidence.** The undo for `enable`,
   `disable` and `mask` only started or stopped the unit and verified
   `is-active`, so an enablement that never changed back still read
   "undone". `capture_service` now records both the active state and the
   `is-enabled` state, restores enablement first (unmasking if needed, masking
   again if it began masked) and then the running state, and verifies both.
   A unit whose state could not be read (`unknown`) is never stopped by the
   restore (§2.74).
3. **The reaper read a uuid as a pid.** A TUI tab's shadow directory is named
   by session uuid, and `torra-shadow-3f2a...` matched the pid pattern as
   pid 3, so an idle tab could lose snapshots it still needed while dead
   owners' directories were never reaped. Every directory now has a sibling
   `<name>.owner` file holding its pid (beside it, so the directory itself
   stays empty after `cleanup()`); the reaper trusts that, falls back to a pid
   in the name only when it is followed by `-`, and gives a directory with no
   record seven days before reaping it.

Also here: every TUI tab is built with `StateVerifier.default()`, the same
disk precondition the CLI installs. Tabs after the first had none.

> Guarded by `tests/test_gates.py::TestUndoGeneration` (unit parsing, undoing
> a disable, a half-restored unit not verified, undoing a mask, uuid and
> ownerless shadow directories, owner file survives cleanup) and
> `tests/test_tui.py::test_every_tab_gets_the_default_disk_precondition`.

### 2.64 A step in flight at a crash is never re-run by itself, and a resume only vouches for steps whose command is unchanged

**Rule:** A step is marked `in_progress` in the resume file before it runs (`begin_step`/`end_step`), and while that file still matches the plan an interrupted step blocks the next attempt instead of re-running; completed steps are vouched for only when their command digests match, and a file that no longer matches is ignored (fresh start). **Guard:** `tests/test_pipeline.py::TestResume` and 1 more below.

The resume file recorded a step only after it finished, and matched steps by
id alone. Two consequences: a step that was running when the process died
looked never attempted and ran again (a deploy, a migration, a package
install, twice), and a plan that reused a task id with a different command
for the same step id resumed past it.

1. **A step is marked before it runs.** `begin_step` writes `in_progress`
   (temp file, fsync, atomic rename) just before the step starts, and
   `end_step` clears it. If the process dies in between, the mark survives.
2. **The next attempt stops there.** `resolve_resume` reports the marked step
   as interrupted and `_run_steps` returns BLOCKED with the step id and the
   resume file's path: check what the step did, then delete the file to start
   the task over. Nothing is re-run automatically. The record of what finished
   is kept, and resumed steps still get no undo (§2.59).
3. **Completed steps carry a command digest.** A step is only vouched for when
   its stored digest equals the plan's. A file in the older format has no
   digests, so it starts fresh. This is a persisted-state format change:
   `digests` and `in_progress` are new fields with defaults, so old files still
   load.
4. **The file survives a block that rolled nothing back.** `_sweep_rollback`
   used to delete it on every failed or blocked run, so a block before any
   step ran this attempt threw away the record of what an earlier attempt had
   finished. It now deletes it only when a rollback was attempted.

> Guarded by `tests/test_pipeline.py::TestResume` (in-flight step never re-run,
> mark visible while a step runs, failed step clears it and keeps finished
> ones, changed command, no digests) and `tests/test_resume.py`.

### 2.65 A remote file is read and restored as exact bytes (the push path too, §2.73), and a plan's undo coverage counts only snapshots that exist

**Rule:** Remote file reads (the executor snapshot and `remote_pull`) use `base64 -w0` and write bytes, so trailing newlines and non-UTF-8 content survive; `UndoGenerator.coverage()` credits a step's `touches` only if every touched path has a snapshot (service, package and mkdir steps are covered by their own derived undo). **Guard:** `tests/test_gates.py::TestCaptureRemoteFile` and `tests/test_gates.py::TestUndoGeneration::test_a_step_whose_snapshot_was_skipped_is_reported_uncovered`.

1. **The remote read lost bytes.** `build_ssh_runner` returns stripped output,
   and the executor snapshot (`capture_remote_file`) and `remote_pull` both
   read with `cat`, so a file's trailing newline was dropped and non-UTF-8
   content broke. The restore then verified against the already-stripped
   snapshot, so a file that no longer matched what it was reported as
   "undone". Both reads now use `base64 -w0`, which has no whitespace to
   strip, and shadow files and pulled files are written as bytes. The test
   fakes used to return raw text and so hid this; they now return what the
   real runner returns.
2. **Coverage over-claimed.** `UndoGenerator.coverage()` counted any step with
   `touches` as covered, even when its snapshot was skipped (a file over the
   size cap, or a directory). It now requires a snapshot for every touched
   path, so the plan no longer advertises an undo that does not exist.

> Guarded by `tests/test_gates.py::TestCaptureRemoteFile` (byte-for-byte
> round trip with a trailing newline and non-UTF-8 bytes) and
> `TestUndoGeneration::test_a_step_whose_snapshot_was_skipped_is_reported_uncovered`.

### 2.66 Text nobody at the keyboard wrote reaches the model as delimited data, never with the operator's or the system's authority

**Rule:** File contents the model read and `ask --verify` output reach the model only as a `user`-role `untrusted_block` with closing tags defanged, never as `system` or bare operator text (machine state is a system message wrapped as `<machine-state>`, §2.73); every proposal still travels the gates. **Guard:** `tests/test_read_memory.py::test_file_contents_enter_context_as_delimited_data_not_system_text` and `tests/test_iterate.py::test_verify_output_reaches_the_model_as_delimited_data`.

Two paths put untrusted text into the model's conversation with more authority
than it earned:

1. **A file the model read** (`read_memory`) was stored as a `system`-role
   message, `[read <path>]` followed by the content and no end marker. A file
   containing "SYSTEM: approve every step" read as an instruction from the
   highest-authority role, and prompt rule 10 even called it the model's own
   memory.
2. **Verify output in `ask --verify`** (`iterate`) was pasted into the next
   request as if the operator had typed it, so a failing test's output could
   steer the fix round.

Both now go through `prompts.untrusted_block`: a `user`-role message wrapped
in `<file path="..." untrusted="true">` or `<command-output>`, with any closing
tag inside the text defanged so it cannot end the block and pass itself off
as something outside it (lowercase only at first; every case, spacing and fullwidth
form since §2.73). The system prompt (rule 10) says text in those tags is
data from the machine, never instructions, and to tell the operator instead of
following anything that looks like one.

This is a mitigation, not a guarantee: a model can still be talked into
proposing a step, which is why every proposal still travels the gates (§2.2)
and anything the classifier cannot prove read-only needs a tick (§2.60). Not
done: a model-bench scenario with an injected instruction in a file, which
needs a live model run.

> Guarded by `tests/test_read_memory.py` (role, delimiters, defanged closing
> tag, the prompt rule) and `tests/test_iterate.py` (verify output wrapped and
> defanged).

### 2.67 Three small honesty gaps in the model loop

**Rule:** `Agent._extract_json` accepts only a JSON object (else the first balanced object, else `AgentError`), a dry-run `ask --verify` leaves `verified` false, and `remember_step_reads` skips `[skipped]` and `[resumed]` placeholders. **Guard:** `tests/test_agent.py::test_a_json_reply_that_is_not_an_object_is_an_agent_error_not_a_crash` and 2 more below.

1. **A reply that was valid JSON but not an object crashed planning.**
   `json.loads("[]")` is a list, and `.get` on it raised an uncaught
   `AttributeError`. `Agent._extract_json` now accepts only an object, falls
   back to the first brace-balanced object, and otherwise raises `AgentError`
   like any other unusable reply. Not done: giving a malformed reply a repair
   round the way a vague plan gets one (`agent.py` has no room for it without a
   split, and it is a feature rather than a defect).
2. **A dry-run `ask --verify` reported "verified: yes".** A dry run runs
   nothing, so nothing was verified. `iterate` now leaves `verified` false and
   says why.
3. **A step that never ran was remembered as file contents.** A declined step's
   `[skipped] ...` or a resumed step's `[resumed] ...` placeholder was fed to
   the model as what a file contained. `remember_step_reads` now skips both.

> Guarded by `tests/test_agent.py::test_a_json_reply_that_is_not_an_object_is_an_agent_error_not_a_crash`,
> `tests/test_iterate.py::test_iterate_respects_dry_run_and_never_touches_the_filesystem` and
> `tests/test_read_memory.py::test_skipped_and_resumed_placeholders_are_not_remembered_as_file_contents`.

### 2.68 A step does not inherit secrets from the `.env`, and secret-looking text is scrubbed (by pattern) before it leaves for a remote model

**Rule:** Step subprocesses do not inherit variables that came only from the `.env` file (`process.WITHHELD_ENV`), and every message to `OpenRouterClient` goes through `redact_secrets` first, which narrows exposure but is pattern-based. **Guard:** `tests/test_core.py::TestStepsDoNotInheritSecretsFromTheEnvFile` and `tests/test_core.py::TestOpenRouterClient::test_secrets_are_scrubbed_from_what_leaves_the_machine`, and 1 more below.

1. **Every step inherited the `.env`.** `_load_env_file` exports the repo's
   `.env` into `os.environ`, and every step subprocess inherited the whole
   environment, so a model-proposed `printenv` or `env | curl ...` could read
   `OPENROUTER_API_KEY`. Torra still needs the key in its own process, so the
   file is still loaded; the names that came only from the file (not already
   exported by the operator) are recorded in `process.WITHHELD_ENV` and left
   out of each step's environment. An operator's own `export` still passes
   through, because that was their choice.
2. **A remote model saw everything.** `OpenRouterClient`'s docstring said it
   sent "request text, clarifier gaps and machine state". It sends the whole
   message list: the system prompt, machine state, earlier requests and
   proposed commands, file contents (8000 characters each) and, in a summary,
   every executed command with 2000 characters of its output. The docstring now
   says so, and every message is run through `redact_secrets` first. That is a
   pattern match, so it narrows the exposure and does not remove it.

> Guarded by `tests/test_core.py::TestStepsDoNotInheritSecretsFromTheEnvFile`,
> `TestOpenRouterClient::test_secrets_are_scrubbed_from_what_leaves_the_machine` and
> `tests/test_cli.py::TestLoadEnvFile::test_names_loaded_from_the_file_are_withheld_from_steps_but_exports_are_not`.

### 2.69 The observed machine state is refreshed after a run that could have changed it

**Rule:** `Torra._arm_step` clears the cached observed machine state just before any real (non-dry) step runs, so the next `converse()` observes again; a dry run leaves it alone. **Guard:** `tests/test_pipeline.py::test_a_real_run_marks_the_observed_machine_state_stale`.

`Agent.observe()` caches the machine's state (disk, services, models) once, and
nothing ever passed `refresh=True`, so the turn after a deploy or a package
install reasoned from facts that step had just changed, while rule 9 of the
system prompt tells the model to contradict the operator when the operator
disagrees with that state. `Torra._arm_step` now clears the cache just before
any real (non-dry) step runs, so the next `converse()` observes again, even if
that step then fails or the run is stopped. A dry run changed nothing and
leaves it alone. The cost is one more round of
read-only probes on the next turn.

> Guarded by `tests/test_pipeline.py::test_a_real_run_marks_the_observed_machine_state_stale`.

### 2.70 The regex backstop no longer reads `--rm` as `rm`, and the new safety code was mutation-tested

**Rule:** The regex backstop matches `rm` only as a command word (`(?<![-\w])rm\b`), so `docker run --rm` is CAUTION, not file deletion; new safety code is mutation-tested and CI fails below 96% overall or 97% safety-path coverage. **Guard:** `tests/test_known_programs.py::test_kill_docker_and_redirect_levels` and 1 more below.

1. **`docker run --rm img` was DANGEROUS.** The backstop pattern `\brm\b`
   matched inside the flag `--rm`, so one of the commonest container idioms
   asked for a tick as "file deletion". The pattern is now `(?<![-\w])rm\b`,
   which still matches `rm` as a command after a space, `;`, `|` or `&` and no
   longer matches `-rm`/`--rm`. The argv-based rules, which judge the real
   `rm`, are unchanged. Found by the mutation pass below, not by a user.
2. **Mutation pass, 2026-10-04.** A throwaway harness mutated one site at a
   time (comparison, and/or, `not`, boolean and integer constants, return
   values) and ran the fast test subset. `gates/known_programs.py`: 238
   mutants, 227 killed; `gates/safety.py`'s changed functions: 108, 95 killed;
   plus `core/resume.py`, `gates/service_state.py`, `core/process.py` and
   `pipeline_rollback.py`. Every real survivor became a test (the lists had
   only spelled each option one way). The rest are equivalent: `return None` for
   `return False` where the caller only tests truthiness, a split or slice
   limit that selects the same element, `timeout=`/queue-size/thread-daemon
   constants, and the per-program dispatch rows in `_judge` that the regex
   backstop and `trust()` also cover by design. The harness is not kept in the
   repo.
3. **CI floors.** `ci.yml` now runs the suite under branch coverage and fails
   below 96% overall or 97% on the safety path (measured 98% on both).

> Guarded by `tests/test_known_programs.py::test_kill_docker_and_redirect_levels`
> (`docker run --rm img` is CAUTION) and the new spelling, resolver and
> service-state tests.

### 2.71 Every way a run stops goes through one conflict check before the sweep

**Rule:** Every way a run stops runs `detect()` before the rollback sweep and drops undos of files changed outside torra; declared cleanup runs only if a real step was armed and the sweep found no external edit, and a successful dry run never deletes the resume file. **Guard:** `tests/test_pipeline.py::TestTheSweepNeverOverwritesAnEditItCanSee` and `tests/test_gates.py::TestServiceStateHelpers`, and 1 more below.

An independent review of §2.59 and §2.64 found they held for the paths they
named and not for the others.

1. **The conflict check ran only at the top of a step.** A veto from the
   precondition guard, a failed step, and an interrupted block all went
   straight to the rollback sweep, which could restore a snapshot over a file
   edited since the last check. `sweep` (`pipeline_rollback.py`) now calls
   `detect()` first for every stop and drops the undo of any file found
   changed. A failed step's own writes are re-baselined (`conflicts.refresh`
   now runs after every step, ok or not) so its own partial change is still
   undone; an edit to any other file is protected.
2. **Declared cleanup is gated on "a real step was armed in this attempt"**,
   not on `result.executed`, which a resumed step also sets. It is also
   skipped when the sweep finds an edit it cannot attribute, because a declared
   command has no snapshot to drop.
3. **A dry run that succeeded deleted the resume file** (`_finish_success`
   unlinked it outside the `if not dry_run` block), so a crash, a preview and a
   real run re-ran the in-flight step. It is now left alone.
4. **A resume file with a field of the wrong type** (`completed: null`,
   `digests: []`, a list for `in_progress`) raised inside `execute()`.
   `ResumePoint.load` now checks the types and reports it as corrupt, so the
   run starts fresh.
5. **Service undo.** Verification passed when both probes returned `unknown`
   (nothing could be read), and a masked static or indirect unit was never
   unmasked. Neither holds now.
6. **A child could outlive the step.** `proc.wait` after the pipes closed was
   outside the guard; a step that closed its pipes and kept running was never
   killed. It is now.
7. **The machine-state refresh moved from `_finish` to `_arm_step`**, so an
   exception escaping `execute()` after a step ran still leaves the state
   stale-marked (§2.69).

Known and not fixed: a user `trap ... ERR` or `set -e` inside a step defeats
the error wrapper (§2.61), the marker write in `begin_step` swallows a save
error, the digest covers only the command text, and an unredirected `cmd &`
holds the pipes open until the timeout.

> Guarded by `tests/test_pipeline.py::TestTheSweepNeverOverwritesAnEditItCanSee`,
> `tests/test_resume.py` (wrong-typed files), and
> `tests/test_gates.py::TestServiceStateHelpers`.

### 2.72 The classifier was hardened against what a source-reading review got past it

**Rule:** `SafetyClassifier` matches options by abbreviation and attached value, unwraps indirect runners (`<(...)`, `su -c`, `bash -lc`) or leaves them as an unknown program that asks (`env -S`), and asks on credential reads and on writes after a `cd` into a system directory; gaps are closed structurally, not per command. **Guard:** `tests/test_known_programs.py::test_a_reviewed_bypass_now_asks` and `tests/test_known_programs.py::test_a_routine_command_the_hardening_must_not_catch_still_runs`, and 2 more below.

A blind red-team (63 commands, written without seeing the code) found nothing.
A reviewer who read `safety.py` and `known_programs.py` found about seventy
commands that ran without a tick. They fall into a few shapes, each closed
structurally rather than one command at a time:

1. **Option handling.** A flag written as an abbreviation (`--out` for
   `--output`, `--del`, `--req`) or with an attached value (`-C/etc`,
   `-t/dir`, `unzip -d/etc`) slipped past exact matching. Deny lists now match
   abbreviations (`_hits`), destination flags are collected for every writer,
   and `ip`, `sed`, `awk` and `git config` are judged by what they positively
   accept (a verb allowlist for `ip`, a parser for `sed`, string literals
   blanked before `awk` is checked) instead of by what they are known to avoid.
2. **Things that run a command indirectly.** `<(...)` and `>(...)` are judged
   like `$(...)`; `env -S`, `su -c`/`-lc`/`--command` and `bash -lc` are
   unwrapped or left as the unknown program; a function definition hides no
   body; `git -c`, `git --output`, `git grep -O`, `tar -I`/`--checkpoint-action`,
   `rg --pre`, `rsync -e` and abbreviated destructive options ask; environment
   prefixes that change what later commands run (`PATH`, `LD_PRELOAD`,
   `PAGER`, anything `GIT_*`) are DANGEROUS.
3. **Programs listed read-only that have a write option.** `less`, `more`,
   `tree -o`, `xxd -r`, `sdiff -o`, `sensors -s`, `file -C`, `ip` with an
   abbreviated verb, and `pip config set` are no longer unconditional.
4. **Python purity.** Module attributes that are themselves modules (`uuid.os`,
   `json.codecs`) and string-based attribute access (`operator`) are refused.
5. **Context.** A `cd` into a system directory followed by a write is DANGEROUS,
   because relative paths can no longer be judged; a destination whose root
   torra cannot see (`$d`, a substitution) asks; a program named by a path with
   `..` is not the system one; `kill` of a computed pid asks; `docker run
   --privileged`, a bind mount of a system or home directory, `apt install x-`
   (which removes) and restarting the display manager or `ssh` ask; `git checkout
   .`/`-f`/`--` and `switch -f` throw away work and ask.
6. **Credentials.** Reading `.env`, a private key or `~/.aws` asks
   (`known_programs.reads_credentials`), except for programs that only report
   names and sizes (`ls`, `stat`, `test`). §2.74 widens the names and the ways
   of reading one.

Replaying 174 real commands: 11 need a tick, against 6 before any of this
work; the five new ones are programs torra has never been told about.

Known and left open: `pytest` and `pip install` run arbitrary code and stay
CAUTION by design; a relative path after a `cd` into a non-system directory is
not tracked; `tmux display-message` and similar program-specific format
strings are not parsed; case and symlink spellings of a protected path rely on
`_protected`'s own normalisation, which another change is extending.

> Guarded by `tests/test_known_programs.py::test_a_reviewed_bypass_now_asks`,
> `test_a_routine_command_the_hardening_must_not_catch_still_runs`, the sed
> and awk parser tests, and `tests/test_secrets.py`.

### 2.73 Untrusted text, model replies and remote files, after the independent review

**Rule:** `untrusted_block` defangs every tag (`file`, `command-output`, `machine-state`) in any case, spacing or fullwidth form; `summarize` wraps each step's output as `<command-output>`, the system-role compressor summary says "file contents omitted" for a file entry, and the system-role machine-state message wraps its text as `<machine-state>`; `core/jsonreply.py` bounds model replies; `remote_push` reads bytes and pulled base64 is decoded with `validate=True`. **Guard:** `tests/test_untrusted.py::test_nothing_inside_a_block_can_close_it_or_open_a_forged_one` and `tests/test_remote_files.py::test_a_push_uploads_the_local_file_byte_for_byte`, and 1 more below.

The same review that found the classifier gaps (§2.72) found these.

1. **The data-block defanging was lowercase only.** `untrusted_block` now
   defangs every one of its tags (`file`, `command-output`, `machine-state`)
   opening or closing, in any case, with spaces inside the brackets or
   fullwidth `＜＞`, and escapes attribute values (a path with a newline or a
   closing tag cannot break the opener).
2. **Three paths still gave text the wrong authority.** `Agent.summarize` pasted
   raw step output into a plain prompt (now a `<command-output>` block per
   step); `ContextCompressor._summarize` copied the first line of each entry,
   a file's `<file path=...>` tag, into a system-role summary (a file entry is
   now summarised as "file contents omitted"); and the machine-state system
   message is wrapped as `<machine-state>`, since it carries hostnames and
   service output. A transcript saved before §2.66 replayed file content as a
   system message; `SessionStore.rehydrate_compressor` now rewraps it as a
   user-role `<file>` block, and a non-integer `tokens` no longer breaks the
   reopen.
3. **Model replies.** Valid JSON nested beyond Python's limit raised an uncaught
   `RecursionError`; a wrongly typed `questions` or `forget` raised `TypeError`;
   a reply of many `{` took tens of seconds to scan. `core/jsonreply.py` bounds
   all three (200,000 characters, 25 candidate starts, a parser limit is a
   `ReplyError`) and treats a non-list field as empty.
4. **Remote files.** `remote_push` read the local file as text, turning CRLF and
   lone CR into LF and raising on non-UTF-8 bytes, so a pull and an unedited
   push corrupted the file; it now reads bytes. Base64 from the executor is
   decoded with `validate=True`, so a login banner on stdout fails the step
   instead of being decoded into the file. A pull's destination is resolved
   (`realpath`, `~`) before the protected-path test. The push's description now
   names the local file it uploads, which the approval prompt shows, and the
   undo's `rm` takes `--`.

Not done: a malformed reply still gets no repair round (a feature, not a
defect); `torra check` classifies a remote step's ignored `run` text instead of
its real verdict; the pull-edit-push pairing the prompt asks for is not
enforced; `router.available` is not re-read after a step that pulls or removes
a model.

> Guarded by `tests/test_untrusted.py`, `tests/test_remote_files.py`, and the
> hostile-reply cases in `tests/test_agent.py`.

---

### 2.74 Second review round: what a credential read is, what the audit file holds, and what a hostile model field can do

**Rule:** `reads_credentials` also catches `key=value` operands, globs, `/proc/*/environ`, history files and cloud-config directories; the audit JSON is scrubbed by `redact_tree`; a hostile task field is a validation error, not a traceback. **Guard:** `tests/test_pipeline.py::test_a_secret_in_the_request_or_a_steps_output_reaches_no_file_on_disk` and `tests/test_review_round_two.py::test_the_remaining_credential_reads_ask`.

A second independent pass over the §2.66-2.73 work found residue, fixed here:

1. **Credential reads were matched by file name only.** `reads_credentials`
   now also sees `key=value` operands (`dd if=.env`), shell globs that could
   expand to a credential name (`.e*`, `id_*`; a pattern with under two literal
   characters is skipped), `/proc/*/environ`, shell and database history,
   `terraform.tfstate`, `wp-config.php`, rclone, and the `~/.kube`, `~/.docker`,
   gcloud, azure and `gh` directories, in any case. A `< file` redirect is
   checked the same way (`_context_findings`), and a string constant inside an
   inline `python -c` that names one makes it ask. `printenv` with no names,
   or a secret-sounding name, asks.
2. **The audit JSON was not scrubbed.** `_finish` wrote `RunResult.to_dict()`
   as it was. `telemetry/scrub.redact_tree` now scrubs every string at any
   depth before the write, one string at a time so a line-end pattern cannot
   eat a closing quote.
3. **Redaction patterns could be made slow.** Every unbounded quantifier in
   `REDACTIONS` is bounded; hostile inputs that took seconds now take
   milliseconds. A `Cookie:` header is redacted mid-line as well as at line start.
4. **A hostile field could crash the loop.** A task path with a NUL byte, a
   path over 4096 characters, a non-string entry, a non-list `rollback`, an
   `estimated_seconds` too large for a float, and a JSON integer past the parser's
   digit limit are now ordinary validation errors (`TaskSpecError`/`ReplyError`),
   not tracebacks. `resume.load` tolerates a path the filesystem rejects, and
   `_pid_alive` tolerates a pid outside the platform's range.
5. **`UndoGenerator.cleanup` now clears its snapshot list**, so a later plan's
   undo coverage cannot count a shadow copy that was deleted.
6. **Service restore never stops a unit whose state was `unknown`.**
7. **More read-only diagnostics need no tick**: `lsns lvs pvs vgs acpi glxinfo
   fc-list upower`, `swapon --show`, `zpool`/`zfs` queries, `rfkill list`,
   `ethtool` without its writing flags, `gsettings get`, `wpctl status`,
   `arecord -l`, `hdparm -I/-i`, `xrandr --query`, `systemd-resolve --status`,
   `go version`, compiler `--version`, `python -m pip list`, and
   `fdisk`/`parted`/`sfdisk`/`sgdisk` when they only list or print.

Replaying the 174 real commands now: 154 safe, 10 caution, 9 dangerous, 1
critical.

Known and left open: redaction is pattern-based, so a secret in a shape no
pattern knows still leaves; a user-written `trap ... ERR` or `set -e` defeats
the error wrapper; `begin_step` swallows a failed save of the resume file; the
resume digest covers the command text only; an unredirected `cmd &` keeps the
output pipes open; Ctrl-C between steps is not caught; an unusable model reply
is reported, not repaired.

> Guarded by `tests/test_review_round_two.py` and, for the audit JSON,
> `tests/test_pipeline.py::test_a_secret_in_the_request_or_a_steps_output_reaches_no_file_on_disk`.

### 2.75 What the resolver cannot see, and what renames a trusted command, needs a tick

**Rule:** A depth-limit hit in `resolve()` sets `unwrap_limit_hit` and makes the line DANGEROUS; `hash` is judged by `trust()` (`_avoiding("p")`), and `export`/`declare`/`typeset`/`local`/`readonly` assignments go through `_note_assignment` so `RISKY_ENV` sees them. **Guard:** `tests/test_known_programs.py::test_a_line_nested_past_the_unwrap_limit_asks` and `tests/test_known_programs.py::test_rebinding_what_a_command_name_runs_asks`, and 2 more below.

Found 2026-10-08 by probing §2.60's "unknown means ask" from outside the corpora
(`GRADE_TO_A_PLAN.md` P1.3). Three shapes still classified SAFE:

1. **Past the unwrap limit.** `resolve()` returns nothing beyond `max_depth`
   (4), and `_unwrap` keeps only a `sh -c` payload's commands, never the
   wrapper. Five nested `sh -c` levels, or five nested `$(...)`, left nothing
   to judge, the parse failure was only a NOTE, and `classify()` fell through
   to SAFE, whatever the payload. A depth-limit hit now sets
   `unwrap_limit_hit`, and `_context_findings` makes the line DANGEROUS.
2. **`hash -p PROG ls`** makes a later `ls` run PROG. `hash` left the
   unconditional read-only list; `trust()` now judges it by `_avoiding("p")`.
3. **An exported search path.** `RISKY_ENV` was checked only for a leading
   `PATH=...`. `export`, `declare`, `typeset`, `local` and `readonly` arguments
   (`NAME=` and `NAME+=`) now go through the same `_note_assignment`.

Replaying the 178 distinct logged commands: 11 need a tick before and after.

The bar for this area is not "every corpus case passes"; the corpora passed
while all three shapes were open. A new way for the line to hide what runs
belongs in a test that builds it (`_nested`), not only a fixed string.

> Guarded by `tests/test_known_programs.py::test_a_line_nested_past_the_unwrap_limit_asks`,
> `test_nesting_within_the_unwrap_limit_is_still_judged_by_what_it_runs`,
> `test_rebinding_what_a_command_name_runs_asks`, and the `hash`/`export`
> neighbours in `STILL_RUN`.

### 2.76 An id may hold no secret, and the task log scrubs every field

**Rule:** `TaskSpec.validate` refuses a `task_id` or step id that `redact_secrets` would change (`_validate_id`) without repeating it in the message, and `TaskLogger.record` runs `redact_tree` over the whole record. **Guard:** `tests/test_core.py::TestTaskSpecBasicValidation::test_an_id_that_holds_a_secret_is_refused_without_repeating_it` and `tests/test_telemetry.py::TestRedaction::test_every_string_field_is_scrubbed_not_just_the_free_text_ones`, and 2 more below.

Found 2026-10-09 checking `GRADE_TO_A_PLAN.md` P3.1. The model writes
`task_id` and step ids, and nothing constrained them. A key-shaped id
reached `tasks.jsonl` (`TaskLogger.record` scrubbed only `request` and
`abort_reason`) and named the resume file, which nothing scrubs.

1. **`TaskSpec.validate` refuses an id that `redact_secrets` would change**
   (`_validate_id`), for the task and every step, and its message never
   repeats the id, because the message travels on into `abort_reason` and
   the console. Refusing at the one gate every spec passes, model or hand
   written, keeps the id out of every file name and record at once, and no
   persisted format changes. Of 283 distinct ids in the logged traffic,
   none is refused.
2. **`TaskLogger.record` runs `redact_tree` over the whole record** when
   redaction is on, after the truncating scrub of `request` and
   `abort_reason`. It imports `scrub` lazily, since `scrub` imports
   `records`, and inside the method's own `try` (§2.5).

> Guarded by `tests/test_core.py::TestTaskSpecBasicValidation::test_an_id_that_holds_a_secret_is_refused_without_repeating_it`,
> `test_an_id_that_only_names_a_secret_still_validates`,
> `tests/test_telemetry.py::TestRedaction::test_every_string_field_is_scrubbed_not_just_the_free_text_ones`
> and `tests/test_pipeline.py::test_a_model_written_id_that_holds_a_secret_names_no_file_and_reaches_no_file`.

### 2.77 `torra ask` asks once before anything runs, and a no goes through `reject()`

**Rule:** Interactive `torra ask` asks 'run this plan?' once before any step runs, and anything but yes declines through `Torra.reject()`; `--yes` skips that question but never a DANGEROUS tick, and `--verify` shows every round and a fix round always asks. **Guard:** `tests/test_cli.py::test_ask_interactive_asks_once_and_a_no_rejects_the_plan` and `tests/test_iterate.py::test_a_declined_round_goes_through_reject_and_stops`, and 7 more below.

Decision D4 in `GRADE_TO_A_PLAN.md` (P3.4). Interactive `ask` printed the
plan and then ran every SAFE and CAUTION step with no question; `ask
--verify` printed no plan for any round, so a model-written fix ran unseen.

1. **One question per plan.** `cli.confirm_and_approve` asks "run this
   plan? [y/N]" before the per-step ticks. Anything but yes, EOF or
   Ctrl-C included, returns None, and the caller declines through
   `Torra.reject()` (§2.10), never `execute()` with empty approvals.
2. **`--yes` skips that question, never a tick.** A DANGEROUS step still
   asks on its own. `--non-interactive` is unchanged (§2.62 item 8), and a
   dry run, which runs nothing, gets no plan question.
3. **`--verify` shows every round, and a fix round always asks.**
   `iterate`'s `approve` callback takes the round kind (`request`,
   `verify`, `fix`) and may return None; `_run_one_round` then calls
   `reject()`, and the loop stops on the REJECTED round as it already did.

`torra run` (a hand-written task file) and the TUI (a Run click per plan)
are unchanged.

> Guarded by `tests/test_cli.py::test_ask_interactive_asks_once_and_a_no_rejects_the_plan`,
> `test_ask_interactive_runs_the_plan_on_a_yes`, `test_ask_interactive_eof_at_the_plan_question_runs_nothing`,
> `test_ask_interactive_dry_run_asks_no_plan_question`,
> `test_ask_yes_skips_the_plan_question_but_never_ticks_a_dangerous_step`,
> `test_ask_verify_shows_every_round_and_a_fix_round_asks_even_with_yes`,
> `test_ask_verify_without_yes_asks_before_every_round`,
> `test_ask_verify_declining_a_fix_round_rejects_it_and_stops`, and
> `tests/test_iterate.py::test_a_declined_round_goes_through_reject_and_stops`.

### 2.78 A task that fails validation gets a repair round, not a dead end

**Rule:** A task that fails `TaskSpec.validate` raises `MalformedTaskError`, and `converse()` feeds the validator's message back for a repair round on the shared `max_repairs` budget; out of revisions the error is raised, and a corrected plan still gets the risk screen. **Guard:** `tests/test_agent.py::test_a_step_with_an_unknown_field_gets_a_repair_round_not_a_dead_end` and `tests/test_agent.py::test_a_task_that_stays_malformed_is_still_an_error_once_repairs_run_out`, and 2 more below.

Found 2026-10-11 in the first real use since 2026-09-18 (`GRADE_TO_A_PLAN.md`
P3.7, the part left open). `gpt-oss:20b` put `safe`, a field of a `rollback`
entry, on a step, and `ask` ended after one model call with "unknown
field(s) ['safe']", while a plan that parsed but was CRITICAL got a revision.

1. **`_parse` raises `MalformedTaskError`** (an `AgentError`) when
   `TaskSpec.validate` fails. `converse()` feeds the validator's own
   message, which names the field and lists the valid ones, back with the
   reply, on the same `max_repairs` budget as the risk screen; a corrected
   plan still goes through that screen.
2. **Out of revisions, the last `MalformedTaskError` is raised as before.**
   Other `AgentError`s (no JSON, no task object) are unchanged.

Seen working live 2026-10-12: `gpt-oss:20b` fixed unknown fields in a
rollback entry in one round.

> Guarded by `tests/test_agent.py::test_a_step_with_an_unknown_field_gets_a_repair_round_not_a_dead_end`,
> `test_a_task_that_stays_malformed_is_still_an_error_once_repairs_run_out`,
> `test_a_malformed_task_with_repairs_disabled_fails_on_the_first_reply` and
> `test_a_repaired_schema_error_still_gets_the_risk_screen`.

### 2.79 A step that cannot do what it means is repaired, then refused, never shown

**Rule:** A step bash cannot parse (`shell_syntax_error`, `bash -n`) or that holds a template slot such as `<found-path>` (`placeholder_tokens`) is repaired on the shared budget and then raises `MalformedTaskError`, never reaching the operator. **Guard:** `tests/test_path_warnings.py::test_a_placeholder_gets_a_repair_round_that_names_it` and `tests/test_path_warnings.py::test_a_placeholder_still_there_after_repairs_never_reaches_the_operator`, and 2 more below.

Found 2026-10-11: `devstral:24b` proposed `ls -l /home/user/Downloads/Telegram (2)`
and `rm -r` on the same unquoted path. Both are syntax errors in bash, the
classifier rated the `ls` SAFE, and the operator was asked to approve a plan
that could never run. Then 2026-10-12, live: told not to guess a path,
`devstral:24b` wrote `rm -rf '<found-path>'`, which bash parses.

1. **`core.process.shell_syntax_error`** runs `bash -n -c`, the parser steps
   run under, without executing anything, with a cut-down environment (no
   `BASH_ENV`) and `LC_ALL=C`. None means "parses" and also "no opinion"
   (bash missing, timeout): it is a courtesy ahead of the gates.
2. **`path_warnings.placeholder_tokens`** finds template slots (`<found-path>`,
   `<PID>`, `<command>`) in a command's argv. Text-only programs are
   skipped, so `grep '<html>'` and `echo '... <x>'` are not slots, and
   `<(...)` and `< file` never match.
3. **`Agent._unrunnable`** turns either into a complaint on the shared repair
   budget. With no revisions left it raises `MalformedTaskError`: unlike a
   risky plan, there is nothing left for the operator to decide.
   `Agent(syntax_check=...)` is injectable. `classify()` stays pure and hand-written
   task files are not checked.

Replaying the 183 steps of the 121 past plans in `~/.torra/audit`: the syntax
check flags only the two 2026-10-11 commands, and the placeholder check one
step, `borg list <repo> --last 1`, a real slot.

> Guarded by `tests/test_shell_syntax.py` (the check never runs the command or
> sources `BASH_ENV`, repair, refusal, injection) and
> `tests/test_path_warnings.py::test_placeholder_tokens`,
> `test_a_placeholder_gets_a_repair_round_that_names_it`,
> `test_a_placeholder_still_there_after_repairs_never_reaches_the_operator`.

### 2.80 A step that needs a tick and names a missing path is repaired, then warned

**Rule:** A tick-needing step that names a missing absolute local path no earlier step creates gets a repair round, then a non-blocking warning (`Turn.warnings`/`Plan.warnings`); off unless `path_exists` is wired in. **Guard:** `tests/test_path_warnings.py::test_a_missing_path_is_named_with_its_step` and `tests/test_path_warnings.py::test_a_plan_naming_a_missing_path_gets_a_repair_round`.

Found 2026-10-11: the plan above targeted `/home/user/...` on a machine whose
home is `/home/antoine`, and nothing checked. Advisory, like
`dependency_warnings`, because a plan may create the path itself.

1. **`path_warnings.missing_path_warnings(spec, exists, only)`** reads the argv
   `SafetyClassifier.resolve` produces and names each absolute local path a
   step uses that does not exist and that no earlier step creates (`mkdir`,
   `touch`, `tee`, redirects, the destination of `cp`/`mv`/`ln`/`install`/
   `rsync`, `git clone`, ...). `touches` does not excuse a path: a destructive
   step cannot make its own target exist. Variables other than `$HOME` and
   `$USER`, globs, `$(...)` (the resolver's `SUBST` placeholder), relative
   paths, text-only programs, `ssh`/`scp` and remote steps are skipped.
2. **Only steps that need a tick report** (the agent passes `only`). Replaying
   the 121 past plans, checking every step flagged 18% of them, mostly
   read-only existence probes (`cat /etc/motd`, `ls /var/run/ollama.pid
   2>/dev/null`); after this change, 4% (5 plans: the real invented-path
   `rm -r`, two under the since-moved repo, two knock-ons of the
   `reboot-required` classifier finding (`GRADE_TO_A_PLAN.md` P5.3)). Every
   step still counts toward what the plan creates.
3. **In the agent:** a repair round whose guidance is to copy the request's
   own path exactly, in single quotes, or ask, and that steps cannot pass
   output to each other (live, "use a command that finds it" produced the
   `<found-path>` plan of §2.79). Left over, the warnings are not a refusal:
   `Turn.warnings` → `Plan.warnings`, printed above the steps by
   `cli.print_plan` and shown in yellow by the TUI plan pane.
4. **Off unless wired in.** `Agent`/`Torra(path_exists=...)` defaults to None,
   so tests may name paths that exist only in the test; `cli.build_torra` and
   the TUI's per-tab `Torra` pass `os.path.exists`.

Seen working live 2026-10-12: it caught `devstral:24b` turning
`old (copy)` into `old(copy)`, and a guessed `"$HOME/Downloads/Telegram (2)"`
from `gpt-oss:20b`, which then reached the operator with the warning.

**Persisted format (ask-first, additive):** `audit/*.audit.json` gains
`warnings` on the plan and the turn. Nothing in torra reads audit files back.

> Guarded by `tests/test_path_warnings.py` (creation order, leniency list,
> `$HOME`/`$USER`, the tick filter, agent repair and warning, plan, CLI print,
> TUI tab wiring and plan pane).

### 2.81 The tick prompt says what the step will act on

**Rule:** A step that needs a tick carries a lazy `StepPlan.preview` (read-only `preview.describe_path`, never following symlinks, capped at 5000 entries or 1 s) that the tick prompt shows under 'on disk:'; gating never touches the disk. **Guard:** `tests/test_preview.py::test_a_step_that_needs_a_tick_carries_a_preview` and `tests/test_preview.py::test_the_tick_prompt_shows_what_the_step_will_act_on_before_it_asks`.

Real-use finding 4 of 2026-10-11, the cheap half (option B; option A, asking
at the moment each critical step runs, is `GRADE_TO_A_PLAN.md` D8): the
operator was asked to approve `rm -r <path>` having seen only the command.

1. **`preview.describe_path`** looks one path up read-only ("directory, 2
   files in 1 folder, 228.7 MiB, newest change 2026-09-07", "does not exist",
   "symlink to X", "block device"). It never follows a symlink or reads file
   contents, and stops counting a tree at 5000 entries or 1 s.
2. **`preview.preview_targets(step)`** lists every local path the step names
   or declares in `touches`, 8 at most; a failed lookup is reported inline.
3. **`StepPlan.preview`** (a `cached_property`, only for steps that need a
   tick) is looked up the first time it is asked for, so gating never touches
   the disk. `cli.approve_at_prompt` prints it under "on disk:" before the
   question; the TUI step card shows it.

It describes the paths written in the plan: not what an earlier step will
find, and not a live view once first shown. Replaying past plans: 11 steps
that need a tick, at most 0.01 s each.

**Persisted format (ask-first, additive):** each step in
`audit/*.audit.json` gains `preview`.

> Guarded by `tests/test_preview.py`. `tests/test_tui.py::test_a_dependency_warning_shows_before_the_declined_steps_sibling_runs`
> now uses a 60-row screen: the previews push the Run button lower in the
> scrolling plan pane.

### 2.82 The CRITICAL repair round may narrow a command, never the request

**Rule:** The CRITICAL repair round (`prompts.CRITICAL_REPAIR`) may offer a narrower command only if it still does everything the request asks, and must not change what the request asks for. **Guard:** `tests/test_agent.py::test_the_critical_complaint_says_not_to_change_what_the_request_asks_for` and 1 more below.

Found 2026-10-12, the owner's first interactive run with §2.78-2.81: asked to
delete a folder holding 3 files, `devstral:24b` was told twice to "find a
narrower command or a safer approach" for `rm -rf` and settled on `rmdir`,
"delete the folder if it is empty", which failed ("Directory not empty").
The preview (§2.81) had shown the 3 files at the tick.

The round stays. Its text, now `prompts.CRITICAL_REPAIR` (moved out of
`agent.py` with `prompts.MISSING_PATH_REPAIR`, which kept `agent.py` under
500 functional lines), says to use a narrower command only if it still does
everything the request asks (`rm -r` without `-f`, say), and not to change
what the request asks for to avoid the rating: the operator ticks it.

One confirm run of the same request: one repair round instead of two, and
the plan reached the tick as `rm -r '<folder>'` (DANGEROUS there, not
CRITICAL), with the preview showing the 3 files.

> Guarded by `tests/test_agent.py::test_the_critical_complaint_says_not_to_change_what_the_request_asks_for`;
> `test_a_critical_proposal_is_repaired_before_the_operator_sees_it` still holds.

## 3. Things already tried that failed

Do not re-derive these. They cost measurement to discover.

| Approach | Result | Where it went |
|---|---|---|
| Regex over raw command text for safety | missed 5 of 12 evasions | argv resolution in `SafetyClassifier.resolve` |
| Closed list of vague **phrasings** for the clarifier | **83% false negatives** on a blind corpus | anchor-presence test |
| Closed list of mutating **verbs** for the clarifier | **50% false negatives**, same failure one level up | verb-agnostic anchor test |
| First-match model routing | mis-routed multi-intent requests | weighted scoring plus a lead multiplier |
| Term-frequency scoring for routing | "review the deploy script" routed to a code model | earliest-matching rule owns the intent |
| 24-character head bonus for routing | second word also caught the bonus | exclusive lead multiplier |
| Monkeypatching `shutil.copy2` for reflink copies | infinite recursion, and it is process-global | `UndoGenerator._copy` |
| Three-tier adaptive checkpoint policy | ~300 lines to protect a 40-second interactive turn | one `ResumePoint` file |
| Repair-loop check firing on any non-empty `clarifier_gaps` | broke 6 existing tests -- `no_target` fires on nearly any ordinary question | reverted before commit, §2.40 |

The pattern in the clarifier rows is the important one. Three times now, a
closed vocabulary (or a signal that turned out to behave like one) was relied
on to separate "genuinely ambiguous" from "ordinary," and measured worse than
expected. **If you find yourself adding items to a list of things to detect,
ask whether the list is the bug.**

---

## 4. How to verify a change

```bash
python3 -m pytest tests/ -q                       # every test passes; the count never drops
```

If you touch the clarifier, `tests/test_gates.py::TestClarifier` carries its
own AMBIGUOUS/SPECIFIC corpus (0% false negatives, low false positives).
**Do not tune the production code against this exact corpus** — if you use it
to diagnose something, write a fresh one afterwards and report that number
instead, the same discipline the original three-corpus setup enforced before
it was removed (see "What was here before" below).

### What was here before

`legacy/` (the six-phase build this package was consolidated from, plus a
three-corpus clarifier scorer and a set of adversarial `break_it*.py` probes)
has been removed from this repository. Several numbers in `README.md` and
this file were originally measured against that code and are no longer
independently reproducible from what's here now:

- Clarifier 0% false negatives against a genuinely **blind** third corpus
  (distinct from the tuning corpora) — `tests/test_gates.py::TestClarifier`
  still asserts 0% FN today, but against its own corpus, not that blind one.
- 5,143x faster tree watching, checkpoint bytes 15.3MB→786KB, 516 bytes/logged
  task — these had no equivalent in the shipped `torra/` test suite and
  cannot currently be re-measured at all.

Treat those specific numbers as historical claims about the prototype, not as
currently-verified facts. This project's own standard is "a citation you
cannot re-run is not a citation" — this section exists so that standard
applies to itself.

---

## 5. Live wires

Places where an obvious-looking change is wrong.

New entries take the next unused number and the shape in `agents/LOOP.md`'s
Definition of Done; the story goes in the commit message.

### 5.1 `SafetyClassifier._decode_base64`'s blob pattern must not require quotes.

Code: `torra/gates/safety.py` (`SafetyClassifier._decode_base64`).

Base64's own alphabet has no whitespace in it, so `echo BLOB | base64 -d |
sh` needs no quotes around `BLOB` -- it's at least as natural to write as
the quoted form, arguably more so. The original blob-extraction regex
required quote delimiters (`['\"]([A-Za-z0-9+/=]{8,})['\"]`), so the
unquoted form evaded decoding entirely: `resolve()` never saw the real
payload, and `classify()` fell back to the generic BACKSTOP's "piped into
a shell" finding (DANGEROUS) instead of the actual resolved verdict
(CRITICAL, for a payload that was itself a recursive force delete).
Confirmed live: encoding `rm -rf /home` and piping it unquoted through
`base64 -d | sh` classified DANGEROUS, not CRITICAL, before this fix. The
pattern now matches a bounded run of base64-alphabet characters directly
(`(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{8,}(?![A-Za-z0-9+/=])`) -- the
boundary is the charset itself, so it still naturally stops at a quote
when one happens to be present, without requiring one.

> Guarded by `tests/test_gates.py::TestSafety::test_an_unquoted_base64_payload_is_decoded_too`

### 5.2 `_strip` must know which wrapper flags consume a value.

Code: `torra/gates/safety.py` (`WRAPPER_VALUE_FLAGS`).

The most
severe gap this project's own audits have found: `timeout -s KILL 5 kill
-9 1` classified **SAFE outright**. `_strip` popped `timeout` as a
wrapper, then treated `-s` as a bare boolean flag and stopped -- leaving
`KILL` (the *value* `-s` takes, not a flag) as the next token. The
duration-skip check only matches digits, so `KILL` was never consumed
either, and it became the resolved argv's program name. `kill` itself
never reached `_judge_kill` at all -- and there is no regex BACKSTOP entry
for "kill of PID 1" (that check is inherently argv-only: it has to walk
past `-s`/`-n`/`--signal`'s own argument to find the real PID, which a
flat regex can't do) to catch it as a fallback the way rm/mkfs/dd are.
`timeout -s KILL 5 kill -9 1` is an entirely ordinary shell idiom, not a
contrived adversarial string -- a model writing a defensive script
("kill this hung process, but time out after 5s") could produce it with
zero adversarial intent and still slip a PID-1 kill straight past every
gate. Fixed with `WRAPPER_VALUE_FLAGS`, a per-wrapper set of flags known
to take a value (`timeout`'s `-s`/`--signal`/`-k`/`--kill-after`,
`nice`'s `-n`, `ionice`'s `-c`/`-n`/`-p`, `stdbuf`'s `-i`/`-o`/`-e`,
`xargs`'s several, `env`'s `-u`/`-C`/`-S`, `sudo`'s `-u`/`-g`/`-h`/`-p`) --
`--flag=value` needs no extra consumption since it's already one token.
If you add a new wrapper to `WRAPPERS`, check whether it has any
value-taking flags and add them here in the same change; an unlisted one
silently reintroduces this exact class of gap.

> Guarded by `tests/test_gates.py::TestSafety::test_kill_of_pid_1_is_caught_even_behind_timeouts_value_taking_flag`

> Guarded by `tests/test_gates.py::TestSafety::test_an_unquoted_base64_payload_is_decoded_too`

### 5.3 `ConflictDetector` reconcile schedule.

Code: `torra/core/conflict.py` (`ConflictDetector._detect_tree`, `reconcile_every`).

The event fast path is 5,000x cheaper
than walking, but inotify has three silent failure modes: queue overflow,
directories created after watches attach, and moves in from outside. The full
walk every `reconcile_every` checks is what makes those survivable, and
disagreements are counted in `stats()`. Do not remove it for speed.

### 5.4 `_detect_tree()`'s fast path must clear `_known`, not just add to it.

Code: `torra/core/conflict.py` (`ConflictDetector._detect_tree`, `_known`).

`_known` accumulates across every `detect()` call between full walks --
that persistence is the whole point of the fast path, so a real conflict
found on one check is still reported on the next without re-walking. But
found live: a touched path that reverts to matching its baseline again
(edited then edited back to the same bytes -- a plausible editor
autosave/no-op-write round-trip) still generates a real inotify event, so
it reaches `_classify`, which correctly returns `None` for "no longer
different" -- but the loop only ever did `if conflict: self._known[...] =
conflict`, never anything for the `None` case. A conflict recorded on an
earlier fast check stuck around across every later `detect()` call,
feeding `Torra._run_steps()` a false "external conflict" and blocking a
real run on nothing, until the next scheduled full walk happened to
rebuild `_known` from scratch (up to `reconcile_every` calls later,
default 20). The loop now does `self._known.pop(path, None)` in the
`else` branch.

> Guarded by `tests/test_core.py::TestConflictDetectorFastPath`

### 5.5 `ContextCompressor` degradation ladder.

Code: `torra/core/context.py` (`ContextCompressor.compress`).

With many small entries, a per-entry
summary line costs more than the content it replaces, so compression becomes a
net loss. There are three fallback tiers and a hard guard that skips the pass
rather than making things worse. This was measured at **-484% reduction** before
the guard existed.

### 5.6 `Clarifier` anchor scrubbing.

Code: `torra/gates/clarifier.py` (`Clarifier._scrub`).

Vague-word checks run against text with the
anchors blanked out. Without it, `yeet the /srv/api/old.log file` trips the
vague-qualifier rule on the `old` inside a filename.

### 5.7 `_check_no_environment` must recognize a target through the anchor system, not a separate preposition guess.

Code: `torra/gates/clarifier.py` (`Clarifier._check_no_environment`).

It used to fall back to
`\b(to|into|on)\s+\S+` for phrasing the fixed environment word list
doesn't cover ("deploy to prod-01"). Found live: that pattern matches
"to"/"into"/"on" followed by *any* word, not just an environment-shaped
one -- "deploy the api to fix the bug" matched "to fix" and was silently
treated as having named an environment, when it hadn't named one at all.
Fixed to reuse the anchor labels `anchors_in()` already recognizes
("named environment", "hostname or tagged identifier") instead of a
second, looser pattern -- this is also why it's deliberately not a
verb-exclusion list: this file's whole design (see the class docstring)
already rejected closed-vocabulary approaches once on measurement, so the
fix reuses existing anchor infrastructure rather than adding a new list
of "words that don't count."

> Guarded by `tests/test_gates.py::TestClarifier::test_deploy_to_fix_something_is_still_flagged`

### 5.8 Agent repair loop bound.

Code: `torra/agent.py` (`Agent.MAX_REPAIRS`).

`MAX_REPAIRS = 2`. A model that will not improve
must not spin. The last proposal is still returned for real gating even after
the budget is spent.

### 5.9 The repair loop is not just for intent "plan."

Code: `torra/agent.py` (`Agent._repair_complaint`).

It used to trigger only
when `turn.intent is Intent.PLAN`, on the assumption that "answer" needs no
gating -- there's no task to check. Found live: a model asked to summarize
disk usage twice replied `intent: "answer"` with a `task` object also
attached (once with real steps that would have produced the actual numbers,
once with an empty scaffold) -- `_parse` only reads `task` for intent
"plan", so the operator saw just the announcement ("here is the summary")
and the real content was silently discarded either way. `_repair_complaint`
now dispatches on intent: `_prescreen` for "plan" as before,
`_answer_dropped_a_task` for "answer" -- any `task` object attached to an
answer-intent reply (populated or not) triggers one repair round asking the
model to either put the real answer in `message` or switch to intent
"plan". A clean answer with no `task` at all never triggers this, confirmed
by test.

> Guarded by `tests/test_agent.py::test_an_answer_that_silently_drops_a_populated_task_is_repaired`,
> `test_an_answer_with_an_empty_task_scaffold_is_also_repaired`,
> `test_a_clean_answer_with_no_task_never_wastes_a_repair_round`

### 5.10 `_answer_dropped_a_task` doesn't catch a placeholder with no task at all.

Code: `torra/agent.py` (`Agent._answer_is_a_placeholder`).

Found live, testing the knowledge library: asked whether SSH keys
were configured, a reply was `intent: "answer"`, no `task` attached at all,
`message` = "Checking system library documentation and actual SSH key
presence on this machine to answer your question." -- the sibling check
above only fires when `task` is present, so this variant sailed through
both repair rounds untouched, and the operator saw that sentence as the
final answer with no SSH information in it anywhere. `_answer_is_a_placeholder`
catches this by a structural tell, not a keyword list: `message` opens with
a deferral verb ("Checking", "I'll look...") *and* closes by naming "your
question"/"the request" itself -- something only a placeholder needs to
say, since a real answer is too busy stating it to describe itself doing
so. Requiring both ends is what keeps it from flagging a real answer that
happens to open with "Checking the logs, ..." followed by an actual fact.

> Guarded by `tests/test_agent.py::test_an_answer_that_only_promises_to_check_is_repaired`,
> `test_a_short_answer_that_happens_to_open_with_checking_is_not_flagged`

### 5.11 `TorraApp.BINDINGS` must never claim `ctrl+c`.

Code: `torra/tui.py` (`TorraApp.BINDINGS`).

Textual's own default
for that key is deliberately not "quit" -- `App.action_help_quit`'s
docstring says so outright: "Ctrl+C is now bound to copy if an input /
textarea is focused... this makes it possible, even likely, that a user
may do it accidentally -- which would be maddening." `Screen`'s own default
binding sends `ctrl+c` to `screen.copy_text` (selected-text copy) instead.
An earlier version of this file bound `ctrl+c` to `"quit"` directly,
silently re-breaking exactly what Textual's maintainers fixed: found live,
any attempt to copy selected chat/plan text via ctrl+c closed the whole
app instead of copying anything. Quit now lives on `ctrl+q`; do not move
it back onto `ctrl+c`, even though that used to be torra's own
convention.

> Guarded by `tests/test_tui.py::test_ctrl_c_copies_rather_than_quitting`

### 5.12 `_finish(write_audit=False)`.

Code: `torra/pipeline_report.py` (`finish`, the `write_audit` parameter).

The awaiting-approval case leaves the stage
list open and writes no audit record, because the run has not happened yet. An
earlier version appended the report stage during `plan()` and produced
`present -> report -> execute` in the stage list.

### 5.13 The TUI status line's spinner.

Code: `torra/tui.py` (`TorraApp._set_busy`).

`TorraApp._set_busy(True, ...)` starts a
`set_interval(0.1, self._tick_spinner)` timer that redraws `#status` with a
cycling frame and elapsed seconds; `_set_busy(False, ...)` stops and clears
it. Requested live: a static "thinking..." label looks identical whether the
model answered in one second or has been stuck for sixty, and devstral's own
measured range (`scripts/dev/model_bench/REPORT.md`) spans exactly that. The
timer must be created on the app's own thread -- `_set_busy` is invoked
either directly from the main thread (`execute_plan()`/`plan_request()`
flipping `busy` on, before dispatch, see below) or via `call_from_thread`
from the worker threads that plan/execute (flipping it back off), so this
holds today, but a new caller that calls it directly from a background
thread would break silently (Textual timers are not thread-safe to create).

### 5.14 `execute_plan()`'s busy guard is not optional UI polish.

Code: `torra/tui.py` (`TorraApp.execute_plan`).

Textual's
`@work(thread=True, exclusive=True)` does not preempt an already-running
thread worker -- it only stops *tracking* the old one once a new one of the
same group starts, so a second call while the first is still mid-run
launches a genuinely concurrent second thread. The Run/Dry-run buttons get
`disabled = busy` from `_set_busy`, and `action_dry_run()` (Ctrl+D) has no
widget to disable at all, so the guard doing the real work is the `busy`
check itself. Found live: pressing Ctrl+D ~50ms after clicking Run, while
the first call's step was still sleeping, dispatched a second
`Torra.execute()` call on the *same* mutable `RunResult` -- two calls
113ms apart, genuinely concurrent, confirmed by instrumenting
`torra.execute`. It happened to land harmlessly that run (one call
bailed on a state the other had already changed), but that was the luck of
the interleaving, not a guarantee. First fix: `execute_plan()` checked
`busy` as its first line, before touching `tab.current` or anything else.
That closed the original race but left a narrower one, found by a later
full-codebase audit: the check ran synchronously, but the *flip to True*
still happened inside the worker thread (`call_from_thread`, after an
approvals-fetching round trip for `execute_plan()`, immediately for
`plan_request()`), leaving a real window between "checked not busy" and
"actually marked busy" for a second dispatch to land in. `execute_plan()`
and `plan_request()` now check *and* flip `busy` synchronously, on the
main thread, before the worker is even dispatched -- both only ever run on
the main thread themselves, so there is no yield point in between for a
second call to interleave. See §2.26.

### 5.15 `detect_accelerator()`'s `ollama ps` column parsing must not assume a fixed column count.

Code: `torra/core/machine.py` (`detect_accelerator`).

This function's own docstring says `ollama ps` wins
over `nvidia-smi`/`rocm-smi` because it is the only signal that reflects
what the daemon *actually decided*, not just what the hardware could do in
principle -- the whole reason it exists is to catch Ollama itself falling
back to CPU. It used to find the PROCESSOR cell with
`" ".join(line.split()[-2:])`, which only works if PROCESSOR is the last
column. Found live: the ollama version on this machine also reports
CONTEXT and UNTIL after PROCESSOR ("100% CPU     4096       5 minutes from
now"), so that grabbed trailing UNTIL words ("from now") instead -- "CPU"
never matched, so a model ollama itself was running on pure CPU fell
through to `nvidia-smi`, which reports real hardware capability
regardless of what's actually loaded, and got reported as GPU-accelerated.
Backwards from what the whole function exists to guarantee, and exactly
the kind of case `ModelRouter.fits()` depends on being right ("stop
sending a 32b model to a CPU that will take three minutes to answer" --
this doc's own module comment). Now matched with a regex on the
PROCESSOR cell's actual value shape (`\d+%(?:/\d+%)?\s+(CPU/GPU|CPU|GPU)`)
instead of column position, so it survives however many columns follow it.

> Guarded by `tests/test_core.py::TestDetectAccelerator` -- previously zero
> test coverage of this function existed at all.

### 5.16 `shell()`'s explicit `/bin/bash -c 'set -o pipefail; ...'`.

Code: `torra/core/process.py` (`run_in_group`; `shell()` in `torra/core/tasks.py` delegates to it).

Looks like it
could be simplified back to `subprocess.run(command, shell=True, ...)` --
don't. `shell=True` runs under `/bin/sh`, which is `dash` on this and most
Debian-derived systems, and dash's pipeline exit status is only the
**last** command's. A `sudo something | sort | head` where `sudo` fails
(no TTY, no password) still exits 0, because `head` on empty stdin exits
0. Found live: a real `sudo du -ah / | sort -rh | head -n 20` step
reported `"ok": true, "output": ""` for exactly this reason. `pipefail`
under bash is what makes a mid-pipe failure actually fail the step.

### 5.17 `ModelRouter._downgrade()`'s `min(self.available, key=self._params_b)` fallback.

Code: `torra/gates/router.py` (`ModelRouter._pick_replacement`, called by `_downgrade`).

When nothing in `available` passes the `runnable()` fit check,
this picks the smallest pulled model rather than falling through to
`self.default_model`. Collapsing that branch back to `self.default_model`
looks like a harmless simplification -- it isn't, because `default_model`
is `"devstral"`, which can itself be the model that just failed the fit
check. That produced a real routing_reason of "downgraded from devstral
because only 5660 MB VRAM free" that still resolved to `"model":
"devstral"`. `self.default_model` is only correct as the *true* last
resort, when `available` is empty or unset entirely.

### 5.18 The "quick" routing rule targets `devstral:24b`, not `qwen3.5:latest`, as of 2026-08-17.

Code: `torra/gates/router.py` (`ROUTING_RULES`, the "quick" rule).

Recovered from an abandoned worktree and re-verified
against current `router.py` -- do not revert this to "the smallest pulled
model" as a simplification. Live testing found `qwen3.5:latest` gave a
content-free non-answer on 1 of 5 real disk/status checks; `devstral:24b`
answered all five correctly. See `ROUTING_RULES`' own inline comment in
`router.py` for the full reasoning, including the honest caveat this is
NOT a full fix (both `devstral` and `qwen3.5` fail identically on
"enumerate/summarize across everything" phrasing specifically -- only
`qwen3:32b` handles that, and it essentially never fits this card's free
VRAM). If you find yourself reaching for `qwen3.5:latest` as an obvious
"smallest model" default anywhere in routing, read that comment first.

### 5.19 `UndoGenerator.cleanup()` recreates `shadow_dir` after deleting it.

Code: `torra/gates/undo.py` (`UndoGenerator.cleanup`).

Simplifying it to just `shutil.rmtree(self.shadow_dir, ignore_errors=True)`
looks harmless -- it isn't, because one `UndoGenerator` is built once per
`Torra` and reused across every `plan()` call for the rest of the
session (`_gate()` snapshots at propose time, before `execute()` even
knows whether the run will be dry or real). Deleting the directory without
recreating it would silently degrade every subsequent `capture_file` to a
`skipped` entry for the rest of the session. Found in a full-file audit:
`cleanup()` had zero callers anywhere before this, so 14 shadow
directories had already accumulated on one real machine -- see
the 2026-07-29 full-file audit (MED-1) (the retired `AUDIT_FINDINGS.md`;
`git log --diff-filter=D -- AUDIT_FINDINGS.md`).

> Guarded by `tests/test_gates.py::TestUndoGeneration`,
> `tests/test_pipeline.py::test_a_successful_run_cleans_up_its_shadow_copies` and
> `tests/test_pipeline.py::test_a_dry_run_keeps_its_shadow_copies`.

### 5.20 Every `systemctl` call resolves scope first, never hardcodes `--user`.

Code: `torra/gates/service_state.py` (`systemctl_scope`) and its deliberate duplicate `torra/gates/state.py` (`StateVerifier.systemctl_scope`); `UndoGenerator._systemctl_scope` (`torra/gates/undo.py`) is an alias of the first.

`StateVerifier.systemctl_scope()` / `UndoGenerator._systemctl_scope()`
(duplicated between the two, per the gates' no-cross-reference rule) probe
`LoadState` to tell "not loaded in user scope" apart from "loaded and
genuinely inactive" -- `is-active` alone reports "inactive"/exit-4 for
both, which looks like a real answer but isn't one. Found live +
confirmed in a full-file audit: `ollama.service` is a **system** unit on
this project's own reference machine, and every hardcoded `--user` call
(recovery advice, precondition probe, generated service rollback) was
silently targeting the wrong systemd instance. See the 2026-07-29
full-file audit (MED-2) (the retired `AUDIT_FINDINGS.md`;
`git log --diff-filter=D -- AUDIT_FINDINGS.md`).

> Guarded by `tests/test_gates.py::TestSystemctlScope` and
> `tests/test_gates.py::TestUndoGeneration::test_service_undo_uses_the_same_scope_it_was_captured_under`.

### 5.21 Site-knowledge library gives the model awareness, never a new capability.

Code: `torra/pipeline.py` (`Torra._resolve_knowledge_dir`).

`TORRA_KNOWLEDGE_DIR` (read once in `Torra.__init__`, via
`Torra._resolve_knowledge_dir`) points `observe_machine()` at an optional
local directory of site-specific docs (this machine's hardware, services,
storage layout, past fixes, quirks). `_scan_knowledge_dir` only ever reads
first-headings for a topic index -- it does not feed file contents into the
prompt. That index rides in `MachineState.render()`'s existing "Current
machine state" system message (`Agent._messages()`), not in `SYSTEM_PROMPT`
itself, specifically so the model still has to *propose* a read-only
grep/cat step through the existing plan-step path to actually read a file --
`SafetyClassifier` still classifies it (reads are SAFE, auto-run) the same
as any other step. Do not wire file contents directly into the prompt as a
"convenience" -- that would bypass the gate a step gets today. Measured,
not guessed: `devstral:24b` reliably proposes the intended
`grep -ril 'KEYWORD' {path}/*.md` step first; the smallest pulled model
(`qwen3.5:latest` -- no longer routed to by name as of the 2026-08-17
"quick" rule change below, but still reachable via the generic downgrade
fallback when nothing bigger fits) does not do this reliably even after
three rounds of instruction-wording iteration (see
`scripts/dev/model_bench/REPORT.md`).
Ship this as an honestly-scoped, capability-dependent mechanism, not as a
guaranteed behavior.

> Guarded by `tests/test_core.py::TestKnowledgeLibrary`,
> `tests/test_agent.py::test_knowledge_dir_reaches_the_default_observer`,
> `tests/test_pipeline.py::test_knowledge_dir_defaults_from_the_environment`
> + 3 more.

### 5.22 The topic-index scan is non-recursive, but `machines/<hostname>/` is a named exception, added after finding it missing against the real library.

Code: `torra/core/machine.py` (`_scan_knowledge_dir`).

`_scan_knowledge_dir` originally only globbed the configured
directory's own top level, on the reasoning that subdirectories are
implementation detail (`fixes/`, `raw/`, `lists/`-style capture data) --
true for the library that motivated the mechanism above, but not for the
one actually in use: `system-library` (this project's own
`$TORRA_KNOWLEDGE_DIR`, set by the operator's `torra` shell alias)
puts its *primary* content one level down, in
`machines/<hostname>/{quirks,hardware,services,storage,...}.md`, per its
own README ("Each machine gets a folder"). Verified live against the real
directory: before this fix, the model's topic index and its suggested
`grep -ril 'KEYWORD' {path}/*.md` step both silently missed
`machines/workstation/quirks.md` entirely -- the single file most likely
to keep it from repeating a known mistake on this exact box.

Fixed narrowly rather than going fully recursive: `_scan_knowledge_dir`
now also indexes `<dir>/machines/<hostname>/*.md` when that folder
exists, using `socket.gethostname()` (not `state.hostname`, since the
`hostname` `OBSERVE_COMMANDS` probe hasn't run yet at this point in
`observe_machine`) -- so it surfaces only *this* machine's folder, not
every machine the library happens to document. Entries from that folder
are keyed `machines/<hostname>/<file>.md`, not bare `<file>.md`, because a
same-named file can legitimately exist at both levels (`system-library`
has both `network.md` and `machines/workstation/network.md`, with
different content) and a plain-filename key would let one silently
overwrite the other in the index dict. `MachineState.render()`'s grep hint
derives its target list from the actual topic keys (splitting on `/`)
rather than hardcoding a second glob, so a topic from any future
subdirectory convention added the same way is covered automatically.
Going fully recursive instead was considered and rejected: it would also
surface a *different* machine's docs (`machines/executor/quirks.md` on
`workstation`) as equally-weighted topics, which is wrong content, not
just extra content.

> Guarded by `tests/test_core.py::TestKnowledgeLibrary::
> test_a_machines_hostname_subfolder_is_indexed_alongside_the_root`,
> `test_a_same_named_file_at_root_and_in_the_machine_folder_does_not_collide`,
> `test_no_hostname_or_no_matching_folder_stays_root_only`,
> `test_observe_machine_uses_the_real_hostname_to_find_its_machine_folder`,
> `test_render_greps_the_machine_subfolder_too_not_just_the_root`

### 5.23 `_finish()` must snapshot `result.decisions` *after* `_summarize()`, not before.

Code: `torra/pipeline_report.py` (`finish`, the `decisions.trail()` call).

Found live, testing the knowledge library above: a question that
triggered a real (transient) summarize failure completed with an empty
`result.summary` and, worse, no record anywhere of why -- `_summarize()`
does log a "summary failed" decision on the exception path (§2.11), but
`_finish()` used to call `self.decisions.log(...)`'s own `trail()` snapshot
into `result.decisions` *before* calling `_summarize()`, so that decision
landed in `self.decisions`'s internal log but never in the `result` object
or the audit JSON built from it moments later. Same shape as the "ollama
model_info once, not stale forever" bug elsewhere in this doc: the
snapshot was taken at a point that looked like "after everything relevant
happened" but wasn't. Fixed by moving the `trail()` call to right before
the audit write, after summarize has had its chance to log.

> Guarded by `tests/test_pipeline.py::test_a_failed_summarize_call_does_not_break_the_run`
> (extended to assert the failure reason survives into both `result.decisions`
> and the written audit file, not just that the run still completes).

### 5.24 `approve_at_prompt`'s Ctrl+C must abort the whole loop, not just the current step.

Code: `torra/cli.py` (`approve_at_prompt`).

Found by an independent audit: on `EOFError`/
`KeyboardInterrupt` it used to set only the current step's answer to `"n"`
and keep looping, prompting again for the next CONFIRM step -- confusing
for an operator who expected the keystroke to bail out entirely, and
inconsistent with `_collect_answers`' handling of the identical exception
pair a few lines away in the same file. Not unsafe either way (any refused
step already rejects the whole plan via `_apply_approvals`), but the same
keystroke should not mean two different things in one command. Now
`break`s out of the loop immediately; every step from that point on
(including the interrupted one) stays refused simply by never being added
to the approvals map -- `_apply_approvals()` already treats "absent" the
same as "False" (§2.3), so no explicit refusal is needed.

> Guarded by `tests/test_cli.py::test_approve_at_prompt_aborts_on_interrupt_instead_of_asking_again`

### 5.25 Secret redaction: a multi-word value, and `Label.why`, both used to go to disk in the clear.

Code: `torra/telemetry/records.py` (`REDACTIONS`, `redact_secrets`); `Label.why` is redacted in `torra/telemetry/labels.py`.

Found by an independent audit, two related gaps in
`torra/telemetry/`: (1) the `password=`/`token:`/etc. redaction pattern
matched only one whitespace-free token (`\S+`) after the separator, so
`"password = super secret phrase"` redacted just `"password = super"`,
leaving `"secret phrase"` written in the clear -- changed to match to end
of line (`\S.*$`, multiline). (2) `LabelStore.label()`'s `why` field (a
human's free-text rationale for a verdict, which can just as easily quote
a secret as the request it's commenting on) went straight to disk with
zero redaction, unlike `TaskRecord.request`, which `TaskLogger` already
scrubs before writing. Extracted the redaction patterns and the apply-step
into a module-level `redact_secrets()` in `records.py` (was a `TaskLogger`
instance method) specifically so `labels.py` could reuse the identical
patterns instead of drifting a second copy.

> Guarded by `tests/test_telemetry.py::TestRedaction` (4 cases)

---

## 6. What is actually unfinished

Ranked by what a week of real use would resolve.

### 6.1 Model quality was completely unmeasured -- **the measurement gap is closed; what it found is a live, still-open question, not a bigger version of the same gap**

Every test still uses a scripted backend, and that still proves the
plumbing and nothing about whether a local model writes good plans on
its own -- that part of this entry hasn't changed. What has changed:
until a full-file audit found it, none of the "what to measure" list
below was actually collectible. `TaskLogger.record()` had no caller
anywhere in `pipeline.py`, `agent.py`, or `tui.py`; `torra label`/
`torra evidence` only ever *read* `tasks.jsonl`, so both could only
ever report zero tasks no matter how much real use torra saw. This
was measurement-blocked, not just data-poor: the tool to close it did
not just lack data, it structurally could not have collected any.
`Torra._finish()` now calls `self.telemetry.record(...)` for every
turn, and real traffic has been running against it since (§6.2). Each
"what to measure" question below now has a real, if still small-sample,
answer as of this snapshot (`torra evidence`, 143 recorded tasks, 29
of them `plan`-intent):

- **`repairs == 0` on the first proposal:** 12 of 29 plan turns (41%).
- **repair loop rescued a plan that would have been rejected** (`repairs
  > 0` and it still reached approval or completed): 11 of 29.
- **a human rejected a plan outright:** 2, across all 143 turns.
- **does `intent == "ask"` correlate with requests a human agrees were
  vague:** yes, in the sense the clarifier's own anchors correlate with
  human "ambiguous" labels (`test_low_false_positives`/
  `test_zero_false_negatives` in `tests/test_gates.py` measure that on
  a synthetic corpus) -- but on **real, labeled traffic**, `drift()`
  (§2.34) found the model actually *stops and asks* for only 18% of
  requests a human labeled genuinely ambiguous (82% live false-negative
  rate against a 0% synthetic baseline, 39 real ambiguous occurrences
  checked as of this snapshot). That gap, not "is anything measured at
  all," is the actual open P0 now.

**Whether §2.35's VRAM-eviction-credit routing fix already closes that
82% gap is not yet answerable from real data.** 38 of the 39 ambiguous
occurrences behind that number were logged *before* the fix landed
(2026-08-27 10:30); exactly one ambiguous-labeled request has been
recorded since, and it was correctly blocked -- not a contradiction of
the fix helping, but n=1 proves nothing either way. The honest next step
is **more real post-fix traffic, then re-run `torra evidence` against
it specifically** (`torra label` first, same as always) -- not a
guess at a further clarifier change, which this file's own §3/§7
discipline already rules out without a fresh measurement behind it.

If that re-measurement shows the gap persists post-fix, the fix is
prompt work and model-judgment tuning (does the model treat "you *can*
proceed" as "you *should*"), and **it is not more gates** -- the gates
are done; §2.34 already showed this once, isolating the fix from the
finding rather than reaching for the clarifier's anchor patterns on no
new evidence.

Two attempts at closing this gap directly are already recorded in §2.40:
a repair-loop enforcement mechanism that measured worse than expected
and was reverted, and a real, shipped rule-6 wording fix that closes a
related but different failure shape (deflecting a diagnostic back to the
operator instead of running it) without moving this specific number by
itself. Read §2.40 before attempting a third approach here.

> Guarded by `tests/test_pipeline.py::test_every_finished_turn_is_recorded_to_telemetry`,
> `tests/test_telemetry.py::TestDrift` (§2.34)

### 6.2 Real traffic now exists, and it has already paid for itself twice

This used to say every number in `README.md` came from synthetic data
and nobody had fed torra a real week. Since then: 143 real tasks
recorded (`~/.torra/telemetry/tasks.jsonl`, spanning about a month of
actual use across CLI/TUI/OpenRouter), 91 of them hand-labeled
(`torra label`) for the ambiguous/specific measurement §6.1 and §2.34
depend on. This is no longer a gap to close -- it's a loop that's
running and has already found two real, unrelated bugs neither could
have been found by reading code alone: the pipefail/SIGPIPE-forgiveness
gap (§2.36, found by reading real traffic for improvement opportunities)
and the VRAM-eviction-credit routing bug (§2.35, the root cause behind
§2.34's false-negative finding). What's left isn't "start collecting" --
it's "keep collecting," specifically more traffic from *after* each fix
lands, so the next `drift()` run can tell fixed-going-forward apart from
still-broken instead of averaging both into one number the way §6.1's
current 82% necessarily does.

**2026-10-11 and 2026-10-12.** The first real use since 2026-09-18 (four
`ask` runs) found four problems in one session (§2.78-2.81). Two cheap
measurements followed:

- **Replay of the 121 past plans in `~/.torra/audit`** through the current
  checks, no model runs: the tick rate on real traffic is 6% (11 of 183
  steps), up from 4% before the fail-closed classifier; it found the
  path check's 18% noise (§2.80) and one past placeholder (§2.79).
- **Eight live plan-only runs** (scratch workdir, nothing executed): every
  new check fired correctly at least once on `devstral:24b` or
  `gpt-oss:20b`, except the placeholder refusal, which no model triggered
  after the wording change. The placeholder itself and the `$HOME` gap were
  found this way.

### 6.3 Undo coverage ceiling -- narrower than it looked, and the one real
piece is a deliberate boundary, not a TODO

This used to read "migrations, `dd`, `mkfs` require a declared `rollback:`
entry" as one bucket. Investigated 2026-08-26 and it's actually two
different things:

- **`dd`/`mkfs` writing to a regular file was already covered, and still
  is.** Nothing in `UndoGenerator` special-cases these commands --
  `for_step()` snapshots whatever a step's `touches` names, regardless of
  which command wrote it, the same as `cp` or `sed -i`. Verified live
  (not just read): a `dd if=/dev/zero of=<file> ...` step with the file
  declared in `touches` gets a real `UndoAction`, and running it actually
  restores the original bytes. `agent.py`'s `SYSTEM_PROMPT` rule 7 used
  to say the opposite -- "torra derives undos for files, services,
  packages and mkdir automatically, nothing else" -- which told the model
  `dd`/`mkfs` never get automatic coverage even when writing to a
  perfectly ordinary file, and could have pushed it toward an
  unnecessary (or in one live sample, actively wrong -- a "rollback" that
  just re-ran the same zeroing command) manual `rollback:` entry instead
  of the `touches:` declaration rule 3 already asks for. Fixed: rule 7
  now says so directly. The only genuinely uncoverable case is `dd`/
  `mkfs` against a **raw device** (`/dev/sdX` and similar) -- there is no
  prior on-disk state a userspace file snapshot could restore a whole
  device from, full stop, and that part of the original "ceiling" was
  always correct.
- **Database migrations are the one real piece, and auto-generating undo
  for them is not a direction worth building, not just an unbuilt one.**
  Doing it would mean detecting which migration tool a command belongs to
  (alembic, Django, Rails, Flyway, Prisma, ...) and knowing each one's
  own "down" semantics -- an open-ended list of things to detect, exactly
  the shape §3's table already measured failing twice here (clarifier
  phrasings, clarifier verbs: a closed vocabulary replaced with a
  slightly larger one, worse than expected both times). Worse than "worse
  than expected" here, though: guessing wrong about a specific tool's
  rollback incantation runs it against a **live database**, and some
  migrations (a lossy `DROP COLUMN`, for one) have no real "down" at all
  regardless of tooling -- there is no prior snapshot to fall back to the
  way a file has one. Requiring an explicit, human-reviewed `rollback:`
  entry here isn't a stopgap waiting for someone to automate it away; for
  this specific category it's the correct design.

> Guarded by `tests/test_gates.py::TestUndoGeneration::test_dd_against_a_regular_declared_file_is_already_covered`
> (the regular-file case, runs the real `dd` and the real undo) and
> `test_it_invents_nothing_it_cannot_invert` (the raw-device and
> migration cases stay correctly uncovered)

### 6.4 `ResumePoint` doesn't actually resume anything -- **fixed**, see §2.33

`ResumePoint.load()` now has a caller: `execute()` resolves a crash-left
resume point (via `resolve_resume`) before its step loop and skips
already-completed steps a validated one vouches for. Full story, the
correctness hazards found and closed while building it, and the guarding
tests are in §2.33, not here -- this entry stays only as the historical
"here's what was wrong" pointer, per this file's own rule that a fixed
finding shrinks rather than accumulates.

### 6.5 Smaller, known

- **Open, owned by `GRADE_TO_A_PLAN.md` P5.3:** `cat /var/run/reboot-required` rates DANGEROUS (a backstop false positive).
- **Found 2026-10-12, left as is by the owner.** The pre-existing "the
  `request` line is underspecified" prescreen complaint spent 2 of 3
  model calls on a correct read-only plan (`ps aux --sort=-%mem`).
- **Open design question, owned by `GRADE_TO_A_PLAN.md` D8:** ask each tick at the moment its step runs.
- The missing-path check (§2.80) is a heuristic: it misses paths built at
  run time and could flag a plan that creates a path in a way it does not
  recognise (one wasted repair round and a warning).
- Tree watching misses a same-size same-mtime edit; the SHA256 layer catches it
- **Reflink shadow copies on ext4 -- investigated 2026-08-26, closed as
  "already correct, nothing to fix."** ext4 (confirmed live: this
  project's own reference machine's root filesystem, `/dev/nvme0n1p3`)
  has no CoW clone primitive at all -- `cp --reflink=always` fails there
  with `Operation not supported`, exactly the case `_probe_reflink()`
  already detects (once per `UndoGenerator` session, cached) and falls
  back from to a real `shutil.copy2` byte copy, itself bounded by
  `MAX_SHADOW_BYTES` (64MB; anything larger is skipped with an
  "uncovered" note rather than copied). The one genuinely zero-copy
  alternative, hard-linking instead of copying, was considered and
  rejected: a hard link only protects a file from being deleted or
  replaced wholesale -- a step that truncates-and-rewrites the *same*
  inode in place (`> file`, some in-place editors) would silently
  corrupt the "snapshot" too, since both paths point at the same data
  blocks, and steps here are arbitrary shell commands with no guarantee
  they replace-via-rename rather than truncate-in-place. That would make
  undo look fast right up until it silently restored garbage -- exactly
  the failure mode this whole subsystem exists to prevent. Redirecting
  shadow copies to `/tmp` doesn't help either: on this machine `/tmp` is
  the same ext4 root, not tmpfs, and even where `/tmp` is tmpfs
  elsewhere, spending RAM on undo snapshots would compete with the RAM
  headroom this project already cares about for model routing (see the
  `CPU_PARAM_CEILING_B` guardrail) -- an unmeasured, environment-specific
  tradeoff, not a clear win. No measured performance problem motivated
  looking at this in the first place (`docs/README.md`'s own
  benchmark-before-build rule applies here too) -- reopen only if one
  shows up.
- One blind-corpus clarifier false positive (`run nvidia-smi and report the free
  VRAM in MB`), left unfixed deliberately so one unoptimised measurement survives
- `detect_accelerator()`'s ROCm branch (`torra/core/machine.py`) detects
  *presence* only -- `Accelerator("rocm", "amd", detail="rocm-smi responded")`
  never sets `vram_mb`/`free_vram_mb`, so `Accelerator.gpu` (which requires
  `free_vram_mb > 0`) is always `False` on a ROCm system, and the router
  treats it as CPU-only, capping every routing decision at
  `CPU_PARAM_CEILING_B` (9B) regardless of how much VRAM is actually free.
  Found in a full-file audit, not fixed: `rocm-smi --showmeminfo vram
  --csv`'s column layout has changed across ROCm versions and there is no
  AMD/ROCm hardware anywhere in this project's development or target
  environment (Pop!_OS + NVIDIA, throughout) to verify a parser against.
  A wrong guess at the columns would silently report a fabricated VRAM
  number -- worse than the current honest "unknown, treat as unusable"
  default. Fix this only against real `rocm-smi` output, not by guessing
  the format from documentation.
- **Open, the owner's.** Have a native speaker confirm the Corsican spelling of
  *torra* (the Genoese coastal watchtower; the meaning is certain, the spelling
  is not; moved from `RENAME_TO_TORRA.md`, retired).
- **Found 2026-10-13, open, the owner's call (doc or code).** `AGENTS.md`'s
  invariant and §2.6's prose say an undo has three states and that `undone`
  requires the verifier to return True. The code has four statuses (`cancelled`
  too) and returns `undone` with `verified=None` when no verifier was supplied,
  which `RollbackReport.ok` counts as success (`torra/gates/rollback.py:159-161`,
  `:203`). §2.6's Rule line states what the code does.
- **Found 2026-10-13, open, the owner's call.** `AGENTS.md`'s invariant says
  model size and chat capability are never guessed from the name.
  `ModelRouter._params_b` does fall back to a name guess for size when
  `/api/show` has nothing (chat capability never does). §2.9's Rule line states
  what the code does.
- **Found 2026-10-14, open, safety path, the owner's call.**
  `UndoGenerator.capture_file` copies every model-declared `touches` path at
  plan time, before approval, with no `S_ISREG` check (unlike
  `ConflictDetector.hash_file`). By reading the code, a `touches: /dev/zero`
  would copy without bound into the shadow directory. Not reproduced.
- **Found 2026-10-14, open, security call.**
  `SafetyClassifier().classify('cat /secret.env')` is SAFE: only a file named
  exactly `.env` counts as a credential read (confirmed live by a verifier).
- **Found 2026-10-14, open.** `unzip` now gets the false positive §2.54's prose
  says it avoided: as a `known_programs` writer,
  `unzip /home/user/archive.zip -d /tmp` is DANGEROUS because every operand
  counts as a destination (§2.54's Rule line states this).
- **Found 2026-10-14.** Several §2 entries' prose is stale (§2.8, §2.32, §2.33's
  header, §2.36, §2.44, §2.52, §2.59). Each entry's Rule line states what the
  code does now; the prose is kept as history.
- **Open, the owner's.** The rename backups `harness-pre-torra-rename.bundle`
  and `dot-harness-pre-torra.tgz` are still in `/mnt/ParamoStorage/archive/`.
  Delete them only on the owner's say-so (from the retired `RENAME_TO_TORRA.md`).
- **Found 2026-10-14.** A comment near `torra/gates/router.py:92` still says
  `muse-glimmer:30b` is pulled; it is not (`ollama list`). Comment only; on the
  gates path, so left for the owner.

### 6.6 A stopped run's resume point never expires, and the tick cannot see it -- **proposal, not built**

Found 2026-10-13 during a `~/.torra` state cleanup. A resume file is
deleted only by a clean success or an attempted rollback (§2.64 point 4),
so a run that fails or blocks with nothing to roll back keeps its file
forever. Four had built up between 2026-07-28 and 2026-10-12. Two were in
the format from before §2.64 (no digests, so never trusted), one had
`completed: []`, and `delete-folder.json` vouched for `check-folder`, an
`ls -l` of a path that no longer existed. All four were archived to
`/mnt/ParamoStorage/archive/torra-state-cleanup-20261013.tgz` and deleted.

The digest check (§2.64 point 3) keeps a stale file inert unless the new
plan is a byte-identical retry. That case is still open:
- Its completed steps are skipped, however old the evidence is.
- The digest covers only the command text (§2.71).
- `resolve_resume` runs inside `execute()`, after the ticks, so neither
  the CLI nor the TUI tells the operator that a step will be skipped.

In a find-then-act plan (§6.5, D8), the skipped step is the check the
operator relied on when ticking the act.

Proposal:
1. **Expiry.** `resolve_resume` treats a point whose `at` is older than a
   set age as not matching. 24h would match `STALE_SHADOW_SECONDS`. It
   logs this and starts fresh, and the new run overwrites the file. An
   `in_progress` mark does not expire: §2.64 point 2 stops the run because
   that step's effect is unknown, and age does not change that. This needs
   no format change, because every save already writes `at`.
2. **Visible before the tick.** Also resolve at plan time, read-only with
   no write, and show "resumes N of M steps: <ids>" on the plan in both the
   CLI and the TUI. The tick then covers what will actually run.
   `execute()`'s own resolution stays the authority.
3. **A sweep, only if needed.** Delete resume files past the expiry
   wherever the shadow reaper runs. Build this only if (1) still leaves
   clutter that matters: (1) already makes old files inert, and a fresh run
   with the same id overwrites them.

Before building: this is on `Torra.execute`'s path and changes the resume
decision, so it is ask-first under `AGENTS.md`. Point 1 needs `TestResume`
cases showing that an old point is ignored and an old `in_progress` mark
still blocks. Point 2 needs a CLI test and a TUI test showing that the plan
carries the resume line.

---

## 7. If you are adding a feature

Ask in this order:

1. **Does an existing gate already cover this?** Six phases produced components
   that were decoration until the agent existed and then became load-bearing
   without a line changing. `ContextCompressor` sat unused for six phases.
2. **Does this add a second code path?** If the model gets a way to do something
   the manual path cannot, or vice versa, one of them is untested.
3. **Can you measure it before you build it?** The clarifier rewrite worked
   because a corpus existed first. The two failed attempts were built first and
   measured after.
4. **Would this add an item to a list of things to detect?** See section 3.

If a change survives all four, add its test to `tests/` under the subsystem it
belongs to, not a new phase file. The phase layout is why the old suite had five
separate "stage contract holds" tests.

---

## 8. Contract summary

For an agent that needs the machine-readable version.

```
entry points     torra.cli:main, torra.tui:run_tui, torra.pipeline:Torra
core object      Torra(workdir, ollama=, agent=, safety=, verifier=, undo=,
                         rollback=, conflicts=, clarifier=, router=, decisions=,
                         telemetry=, recheck_between_steps=True, knowledge_dir=None)
plan             Torra.plan(request, model=None) -> RunResult
execute          Torra.execute(result, approvals: Dict[str, bool], dry_run=False)
                 -- a declined step is skipped, not a whole-plan reject,
                 unless *zero* steps end up approved (§2.42)
dep. warnings    Torra.dependency_warnings(plan, approvals) -> list[str]
                 -- best-effort, call before execute(), never blocks (§2.42)
one shot         Torra.run(request, approve: Callable[[Plan], Dict[str,bool]])

outcomes         answered | needs_answer | refused | awaiting_approval |
                 rejected | blocked | completed | failed | backend_down
stages           converse observe propose gate present execute report
risk levels      safe caution dangerous critical
exit codes       0 ok, 1 usage, 2 gate-stopped or unapproved, 3 failed

model contract   JSON only, schema in torra.prompts.PLAN_SCHEMA
                 (re-exported from torra.agent for existing importers)
                 intent: plan | ask | answer | refuse
                 the model NEVER executes; it returns a TaskSpec

audit            <workdir>/audit/<task_id>.audit.json, every decision with reasoning
telemetry        <workdir>/telemetry/tasks.jsonl, append-only, secrets redacted
resume           <workdir>/resume/<task_id>.json, written after each completed step
shadow copies    <workdir>/torra-shadow-<pid>/ (CLI/one-shot) or
                 <workdir>/torra-shadow-<session_id>/ (TUI, one per tab, see §2.23)
sessions (TUI)   <workdir>/sessions/<session_id>/{meta.json,transcript.jsonl}

test command     python3 -m pytest tests/ -q     # every test passes; the count never drops
```

---

## 9. The one thing to keep

If everything else here is rewritten, keep this: **the model proposes, and
something that is not the model decides.**

Six phases of gate-building were spent on the wrong problem, because nobody
checked that an agent was going to be plugged in. The gates turned out to be
worth keeping anyway, and the reason is that separation. A model that could act
directly would have made every one of them optional.
