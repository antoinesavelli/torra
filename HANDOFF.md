# HANDOFF

For whoever or whatever picks this up next.

This document assumes you are an agent or an engineer with no memory of how this
got built. It tells you what is load-bearing, what will bite you, and what has
already been tried and failed. Read it before changing anything in `harness/`.

---

## 1. Orientation, in ninety seconds

```bash
cd <repo> && python3 -m venv .venv && source .venv/bin/activate
pip install pytest pytest-asyncio pyyaml textual inotify_simple && pip install -e .
python3 -m pytest tests/ -q          # must print 568 passed
harness doctor
```

Eight modules, ~6,900 lines shipped. `core.py`, `gates.py` and `telemetry.py` were later
each split into a same-named package of single-concern submodules (see
`DEFENSIVE_CODING_STANDARDS.md` §1.2 for why); every name still imports from the same
top-level path, e.g. `from harness.gates import SafetyClassifier`. Read in this order:

1. `harness/core/` — `Step`, `TaskSpec` (`tasks.py`), `OllamaClient` (`ollama.py`),
   conflict detection (`conflict.py`) — see `harness/core/README.md`
2. `harness/gates/` — the five guardrails (`rollback.py`+`undo.py` is one
   guardrail, ROLLBACK_ON_FAIL, split across two files past the LOC cap) —
   see `harness/gates/README.md`
3. `harness/agent.py` — the model loop
4. `harness/pipeline.py` — `Harness`, the seven stages, the only orchestrator
   (`harness/pipeline_types.py` holds the `Stage`/`Outcome`/`Plan`/`RunResult`
   data model it produces, re-exported from `pipeline.py` unchanged)

`tui.py`, `cli.py` and `harness/telemetry/` are leaves. Nothing depends on them.

---

## 2. The invariants

Break any of these and the safety story collapses silently. There is a test for
each; if you find yourself deleting one of those tests, stop.

### 2.1 The classified command IS the executed command

`Step.command` is what the safety gate reads. `Step.run` is what executes. In
`TaskSpec.to_steps` they are constructed from the same string, deliberately.

A `Step` whose `run` closure executes something different from its `command`
would pass every gate without any gate being wrong. If you add a new way to
build a `Step`, that equivalence is the thing to preserve.

> Guarded by `tests/test_agent.py::test_a_plan_becomes_a_validated_taskspec`

### 2.2 The model never executes anything

The model returns JSON. It gets no tool-call loop, no shell, no filesystem. It
proposes a `TaskSpec` and the pipeline decides what happens next.

This is not caution for its own sake. Giving the model direct execution would
require every gate reimplemented on a second code path, and the second path is
the one nobody tests.

> Guarded by `tests/test_pipeline.py::test_a_hand_written_file_still_works_without_a_model`,
> which asserts `harness.ollama.calls == 0` on the manual path

### 2.3 Unapproved means refused, never defaulted

`Harness.execute` treats a step that needs confirmation and is absent from the
approvals map as **refused**. Do not "helpfully" default it to True for
convenience, unattended runs, or anything else.

> Guarded by `tests/test_pipeline.py::test_a_dangerous_step_does_not_run_unapproved`

### 2.4 Safe steps get no checkbox

In the TUI, only steps at DANGEROUS or CRITICAL render an approve box. Adding
one to every step feels more careful and is the opposite: it trains the operator
to tick without reading.

> Guarded by `tests/test_tui.py::test_a_safe_plan_gets_no_checkbox`

### 2.5 Telemetry can never break a run

Every method in `telemetry.py` catches its own exceptions and records them.
`TaskLogger.__init__` does not raise even when its directory is unusable. A
logger that raises during a deploy is worse than no logger.

> Guarded by `tests/test_pipeline.py`

### 2.6 An undo that cannot be proven is not "undone"

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

A `rollback:` entry's `safe` flag (from a hand-written task or a model's own
proposal) can only make a command *more* cautious than `SafetyClassifier`
would, never less. `Harness.execute` classifies every rollback command
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

`Harness.execute` refuses immediately if `result.executed` is already
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

`ModelRouter._params_b` and `_chat_capable` consult `model_params_b` /
`model_chat_capable` (real `/api/show` data, `OllamaClient.model_info`)
before falling back to guessing from the model's name. `Accelerator.fits`
(`harness/core/machine.py`) does the same via its own `model_params_b`
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

`Harness.reject(result)` exists specifically because `execute(result,
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
another way to decline a plan, route it through `Harness.reject()`, not
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

`Harness._summarize()` (called once from `_finish()`, after a model-authored
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

### 2.12 Every filename built from `task_id` goes through `_safe_filename`

`task_id` is fully model- or human-controlled: `TaskSpec.validate()` does
`task_id=str(data["task_id"])` with no character restriction at all, and a
model's own JSON `task` object supplies it directly (`Agent._parse` only
defaults it when the model omits the field). Found live: `execute()`'s
resume-file path was built as `self.workdir / "resume" / f"{task_id}.json"`
with no sanitizing, right next to `_finish()`'s audit-file path in the same
class, which already sanitized. A `task_id` of `"../../../../tmp/evil"`
wrote a real file outside `workdir/resume/` on any run where a later step
failed before `_finish_success()`'s cleanup ran (that only fires on a fully
successful run). `_safe_filename()` (a static helper both call sites now
share) replaces every character outside alnum/`-`/`_`/`.` -- `/` is the
only character that can turn one path component into several, and with it
gone a leftover `..` is just a funny filename, not a traversal. If you add
a third place that turns `task_id` (or any other model-supplied string)
into a filename, route it through this helper rather than re-deriving the
character set by hand.

> Guarded by `tests/test_pipeline.py::test_a_malicious_task_id_cannot_write_the_resume_file_outside_the_workdir`

### 2.13 A step's DOM id in the TUI is never built from `step_id` itself

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

Same underlying principle as §2.3 ("unapproved means refused, never
defaulted to True"), recurring in the telemetry read path instead of the
execution path. `TaskLogger._apply_plan_fields()` computed `approved = all(sp.approved
is not False for sp in plan.steps)` -- `StepPlan.approved` is `None` until
`Harness._apply_approvals()` actually runs, which only happens inside
`execute()`. A plan blocked by a failed precondition never reaches
`execute()` at all (`Harness.plan()` calls `_finish()` directly), so every
step's `approved` stays `None` -- and `None is not False` is `True`, so a
`BLOCKED` task nobody ever saw was persisted with `approved: true`.
`_apply_approvals()` always resolves every step to a real `True`/`False`
once it runs (a step needing no confirmation is auto-`True`), so requiring
`is True` for every step is exactly as strict for a normal completed run
and correctly `False` for anything that never reached approval. No
downstream reader consumes this field today, so this was a silent
data-quality bug rather than a live break -- but it is exactly the kind of
thing `harness evidence`/model-quality analysis (§6.1) would eventually
rely on, and it would already have been wrong.

> Guarded by `tests/test_pipeline.py::test_a_precondition_blocked_plan_is_not_recorded_as_approved`,
> `test_a_normally_completed_plan_is_still_recorded_as_approved`

### 2.15 `TaskSpec.validate()` type-checks every field, not just presence

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
   `Harness.plan()`'s narrow `except OllamaError | AgentError` clause,
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

Found by an independent security audit, reproduced live: `ConflictDetector.hash_file()`
opened whatever `Path.open("rb")` was pointed at with no check on what kind
of filesystem entry it actually was. Opening a FIFO for reading with no
writer blocks **forever** -- and `baseline()` runs during `Harness`'s GATE
stage, before any operator ever sees the plan (`pipeline.py`'s `_gate()`
baselines every `touches` path a model's own JSON proposal declares). A
single stray FIFO at a model-declared `touches` path -- no malice required,
any real FIFO already present at that path works -- wedged the entire
harness with no timeout anywhere in the call chain. `_scan_knowledge_dir()`
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
`{"capabilities": 42}` made `Harness.plan()` raise a raw uncaught
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

### 2.19 A dry run never reaches `_summarize()`

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

Found while raising `harness/core/conflict.py`'s test coverage (59% -> 74%,
the single worst-covered file in the tree), not by reading the test in
isolation: its shared `_detector()` fixture built the detector with
`use_inotify=False`. `_detect_tree()`'s own guard --
`if not self.use_inotify or not self._inotify or self._since_walk >=
self.reconcile_every: return self._reconcile()` -- means `not
self.use_inotify` alone was enough to always take the full-walk branch,
regardless of the `detector._inotify = True` truthy stand-in the fixture
set up right after. `_reconcile()` rebuilds `self._known` from scratch on
every call, so the exact stale-conflict bug this test class exists to
guard (§5's "`_known` is an accumulator... never removed a stale entry")
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

### 2.23 Multiple `Harness` instances sharing a `workdir` need session-scoped `undo=` and `task_id_prefix`

Found during design review for multi-tab TUI sessions, before any tab code
existed -- these are latent collisions that only matter once more than one
`Harness` can share a `workdir` in one process, which nothing did before
this feature.

1. **Shadow-dir collision.** `UndoGenerator`'s default shadow_dir
   (`pipeline.py`'s `self.undo = undo or UndoGenerator(shadow_dir=self.workdir
   / f"harness-shadow-{os.getpid()}")`) is keyed only by `(workdir, pid)` --
   two `Harness` instances built with the same `workdir` in the same process
   collide on it. Confirmed live (not just reasoned about): one session's
   `UndoGenerator.cleanup()` really does `shutil.rmtree` the other session's
   shadow copies out from under it, breaking that session's rollback
   guarantee mid-run. Fix: every caller building more than one `Harness`
   against the same `workdir` must pass an explicit, session-derived
   `undo=UndoGenerator(shadow_dir=workdir / f"harness-shadow-{session_id}")`
   -- `Harness.__init__` needed no change, `undo` was already injectable.
2. **`task_id` collision.** `Agent`'s fallback `task_id`
   (`f"agent-{self._turn_counter}"`) restarts at 0 per `Agent` instance, so
   two sessions' first turns both default to `task_id="agent-1"` --
   **deterministically**, not as an edge case -- silently overwriting each
   other's `workdir/audit/agent-1.audit.json`. Fix: `Agent.__init__` gained
   `task_id_prefix: str = ""`, applied only at the one `setdefault` fallback
   line, never to a model-chosen `task_id` (a model choosing the same id
   across two tabs remains a low-probability residual risk, not meaningfully
   worse than today's single-session behavior, and deliberately out of
   scope). Every caller building more than one `Agent`/`Harness` against the
   same `workdir` should pass a distinct `task_id_prefix` per session.

Telemetry and the `audit`/`resume` directories deliberately stay **unscoped**
(shared across every session on one `workdir`) -- that's what keeps `harness
evidence`/`harness label` seeing everything across every tab instead of
fragmenting per-session. Only the two collisions above needed fixing; do not
over-scope by giving every session its own subtree of `workdir`.

> Guarded by `tests/test_pipeline.py::test_two_harnesses_sharing_a_workdir_do_not_collide_on_shadow_files`,
> `tests/test_agent.py::test_task_id_prefix_reaches_the_default_task_id`

### 2.24 A tab's plan/execute worker must capture its `session_id` at dispatch time, not read "active" at completion time

The TUI's tabs each run their own `Harness.plan()`/`.execute()` in a
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

### 2.26 A `Harness`/`Agent` reused across many turns needs per-turn scoping it never needed before

Found by a full-codebase audit run immediately after §2.23-2.25 shipped,
specifically because that feature is the first place in the codebase where
a single `Harness`/`Agent` either lives across many turns in one process
(a TUI tab, previously impossible -- every CLI invocation built a fresh
one) or gets torn down and rebuilt against the *same* persisted session
identity (a reopen). Both are genuinely new capabilities, and both exposed
state that had always been implicitly scoped to "one `Harness` instance =
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
   `Harness` and only ever grows (`DecisionLog.records`, nothing cleared
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

Also touched, extending §5's existing "`execute_plan()`'s busy guard is
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

### 2.27 An alternate model backend must be strictly opt-in, and `harness tui` had its own, separate `Harness` construction path

`Agent.resolve_backend()` (`harness/agent.py`) is the only place a request's
model string can route to something other than `self.ollama` --
`harness/gates/router.py`'s `ModelRouter` has no reference to `self.backends`
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

Found during design review, before shipping: `cli.py`'s `build_harness()` is
not the only place a `Harness` gets constructed. `cmd_tui` called
`run_tui(workdir=...)` with no `harness=` argument, and `HarnessApp.__init__`
built a bare `Harness(workdir=...)` directly -- bypassing `build_harness()`
(and, incidentally, its disk-free precondition too) entirely. Fixed by having
`cmd_tui` call `build_harness()` and pass its result into
`run_tui(harness=...)`, and by threading `self.backends` (captured off the
initial `Harness` the same way `self.ollama` already is) into every
subsequent tab's `Harness(...)` call in `_build_session_harness()` -- a new
tab that doesn't inherit `backends` would silently lose OpenRouter access
after the first tab, the same shape of bug §2.23-§2.26 already catalog for
other per-tab state.

**Found live by manual testing after that fix shipped, not by a test:**
`build_harness()`'s first version registered `"openrouter"` only when
`OPENROUTER_API_KEY` was actually set -- reasoning that constructing a
client doomed to fail every call wasn't worth it. That reasoning was wrong.
`harness ask "..." --model openrouter/anthropic/claude-3.5-sonnet` with no
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

`SessionTab.model_override` (`harness/tui.py`) is runtime-only, deliberately
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
> test_build_harness_registers_openrouter_even_without_the_env_var` /
> `test_cmd_tui_builds_the_harness_through_build_harness`,
> `tests/test_tui.py::` `/model` sets/clears the override, a new tab
> inherits `self.backends`,
> `test_an_unconfigured_openrouter_override_fails_clearly_not_silently`
> (the end-to-end regression test for the manual-testing finding above)

---

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

The pattern in the clarifier rows is the important one. Twice, a closed
vocabulary was replaced with a slightly larger closed vocabulary and measured
worse than expected. **If you find yourself adding items to a list of things to
detect, ask whether the list is the bug.**

---

## 4. How to verify a change

```bash
python3 -m pytest tests/ -q                       # 568, must stay green
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
  task — these had no equivalent in the shipped `harness/` test suite and
  cannot currently be re-measured at all.

Treat those specific numbers as historical claims about the prototype, not as
currently-verified facts. This project's own standard is "a citation you
cannot re-run is not a citation" — this section exists so that standard
applies to itself.

---

## 5. Live wires

Places where an obvious-looking change is wrong.

**`SafetyClassifier._decode_base64`'s blob pattern must not require quotes.**
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

**`_strip` must know which wrapper flags consume a value.** The most
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

**`ConflictDetector` reconcile schedule.** The event fast path is 5,000x cheaper
than walking, but inotify has three silent failure modes: queue overflow,
directories created after watches attach, and moves in from outside. The full
walk every `reconcile_every` checks is what makes those survivable, and
disagreements are counted in `stats()`. Do not remove it for speed.

**`_detect_tree()`'s fast path must clear `_known`, not just add to it.**
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
feeding `Harness._run_steps()` a false "external conflict" and blocking a
real run on nothing, until the next scheduled full walk happened to
rebuild `_known` from scratch (up to `reconcile_every` calls later,
default 20). The loop now does `self._known.pop(path, None)` in the
`else` branch.

> Guarded by `tests/test_core.py::TestConflictDetectorFastPath`

**`ContextCompressor` degradation ladder.** With many small entries, a per-entry
summary line costs more than the content it replaces, so compression becomes a
net loss. There are three fallback tiers and a hard guard that skips the pass
rather than making things worse. This was measured at **-484% reduction** before
the guard existed.

**`Clarifier` anchor scrubbing.** Vague-word checks run against text with the
anchors blanked out. Without it, `yeet the /srv/api/old.log file` trips the
vague-qualifier rule on the `old` inside a filename.

**`_check_no_environment` must recognize a target through the anchor
system, not a separate preposition guess.** It used to fall back to
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

**Agent repair loop bound.** `MAX_REPAIRS = 2`. A model that will not improve
must not spin. The last proposal is still returned for real gating even after
the budget is spent.

**The repair loop is not just for intent "plan."** It used to trigger only
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

**`_answer_dropped_a_task` doesn't catch a placeholder with no task at
all.** Found live, testing the knowledge library: asked whether SSH keys
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

**`HarnessApp.BINDINGS` must never claim `ctrl+c`.** Textual's own default
for that key is deliberately not "quit" -- `App.action_help_quit`'s
docstring says so outright: "Ctrl+C is now bound to copy if an input /
textarea is focused... this makes it possible, even likely, that a user
may do it accidentally -- which would be maddening." `Screen`'s own default
binding sends `ctrl+c` to `screen.copy_text` (selected-text copy) instead.
An earlier version of this file bound `ctrl+c` to `"quit"` directly,
silently re-breaking exactly what Textual's maintainers fixed: found live,
any attempt to copy selected chat/plan text via ctrl+c closed the whole
app instead of copying anything. Quit now lives on `ctrl+q`; do not move
it back onto `ctrl+c`, even though that used to be the harness's own
convention.

> Guarded by `tests/test_tui.py::test_ctrl_c_copies_rather_than_quitting`

**`_finish(write_audit=False)`.** The awaiting-approval case leaves the stage
list open and writes no audit record, because the run has not happened yet. An
earlier version appended the report stage during `plan()` and produced
`present -> report -> execute` in the stage list.

**The TUI status line's spinner.** `HarnessApp._set_busy(True, ...)` starts a
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

**`execute_plan()`'s busy guard is not optional UI polish.** Textual's
`@work(thread=True, exclusive=True)` does not preempt an already-running
thread worker -- it only stops *tracking* the old one once a new one of the
same group starts, so a second call while the first is still mid-run
launches a genuinely concurrent second thread. The Run/Dry-run buttons get
`disabled = busy` from `_set_busy`, and `action_dry_run()` (Ctrl+D) has no
widget to disable at all, so the guard doing the real work is the `busy`
check itself. Found live: pressing Ctrl+D ~50ms after clicking Run, while
the first call's step was still sleeping, dispatched a second
`Harness.execute()` call on the *same* mutable `RunResult` -- two calls
113ms apart, genuinely concurrent, confirmed by instrumenting
`harness.execute`. It happened to land harmlessly that run (one call
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

**`detect_accelerator()`'s `ollama ps` column parsing must not assume a
fixed column count.** This function's own docstring says `ollama ps` wins
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

**`shell()`'s explicit `/bin/bash -c 'set -o pipefail; ...'`.** Looks like it
could be simplified back to `subprocess.run(command, shell=True, ...)` --
don't. `shell=True` runs under `/bin/sh`, which is `dash` on this and most
Debian-derived systems, and dash's pipeline exit status is only the
**last** command's. A `sudo something | sort | head` where `sudo` fails
(no TTY, no password) still exits 0, because `head` on empty stdin exits
0. Found live: a real `sudo du -ah / | sort -rh | head -n 20` step
reported `"ok": true, "output": ""` for exactly this reason. `pipefail`
under bash is what makes a mid-pipe failure actually fail the step.

**`ModelRouter._downgrade()`'s `min(self.available, key=self._params_b)`
fallback.** When nothing in `available` passes the `runnable()` fit check,
this picks the smallest pulled model rather than falling through to
`self.default_model`. Collapsing that branch back to `self.default_model`
looks like a harmless simplification -- it isn't, because `default_model`
is `"devstral"`, which can itself be the model that just failed the fit
check. That produced a real routing_reason of "downgraded from devstral
because only 5660 MB VRAM free" that still resolved to `"model":
"devstral"`. `self.default_model` is only correct as the *true* last
resort, when `available` is empty or unset entirely.

**The "quick" routing rule targets `devstral:24b`, not `qwen3.5:latest`,
as of 2026-08-17.** Recovered from an abandoned worktree and re-verified
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

**`UndoGenerator.cleanup()` recreates `shadow_dir` after deleting it.**
Simplifying it to just `shutil.rmtree(self.shadow_dir, ignore_errors=True)`
looks harmless -- it isn't, because one `UndoGenerator` is built once per
`Harness` and reused across every `plan()` call for the rest of the
session (`_gate()` snapshots at propose time, before `execute()` even
knows whether the run will be dry or real). Deleting the directory without
recreating it would silently degrade every subsequent `capture_file` to a
`skipped` entry for the rest of the session. Found in a full-file audit:
`cleanup()` had zero callers anywhere before this, so 14 shadow
directories had already accumulated on one real machine -- see
`AUDIT_FINDINGS.md` MED-1.

**Every `systemctl` call resolves scope first, never hardcodes `--user`.**
`StateVerifier.systemctl_scope()` / `UndoGenerator._systemctl_scope()`
(duplicated between the two, per the gates' no-cross-reference rule) probe
`LoadState` to tell "not loaded in user scope" apart from "loaded and
genuinely inactive" -- `is-active` alone reports "inactive"/exit-4 for
both, which looks like a real answer but isn't one. Found live +
confirmed in a full-file audit: `ollama.service` is a **system** unit on
this project's own reference machine, and every hardcoded `--user` call
(recovery advice, precondition probe, generated service rollback) was
silently targeting the wrong systemd instance. See `AUDIT_FINDINGS.md`
MED-2.

**Site-knowledge library gives the model awareness, never a new capability.**
`HARNESS_KNOWLEDGE_DIR` (read once in `Harness.__init__`, via
`Harness._resolve_knowledge_dir`) points `observe_machine()` at an optional
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

**`_finish()` must snapshot `result.decisions` *after* `_summarize()`, not
before.** Found live, testing the knowledge library above: a question that
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

**`approve_at_prompt`'s Ctrl+C must abort the whole loop, not just the
current step.** Found by an independent audit: on `EOFError`/
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

**Secret redaction: a multi-word value, and `Label.why`, both used to go
to disk in the clear.** Found by an independent audit, two related gaps in
`harness/telemetry/`: (1) the `password=`/`token:`/etc. redaction pattern
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

### 6.1 Model quality is completely unmeasured (the only P0)

Every test uses a scripted backend. That proves the plumbing and proves **nothing
about whether a local model writes good plans**. The scripted replies were
written by the same process that wrote the code, which means the validation is
once again against imagination.

What to measure, from `~/.harness/telemetry/tasks.jsonl`:

- how often `repairs == 0` on the first proposal
- how often the repair loop rescued a plan that would have been rejected
- how often a human rejected a plan outright
- whether `intent == "ask"` correlates with requests a human agrees were vague

If local models turn out to write mediocre plans, the fix is prompt work and
model choice. **It is not more gates.** The gates are done.

Until a full-file audit found it, none of this was actually collectible:
`TaskLogger.record()` had no caller anywhere in `pipeline.py`, `agent.py`,
or `tui.py`. `harness label`/`harness evidence` (`cli.py`) only ever
*read* `tasks.jsonl` -- nothing wrote to it, so both commands could only
ever report zero tasks, no matter how much real use the harness saw.
`Harness._finish()` now calls `self.telemetry.record(...)` for every
turn, using the same `<workdir>/telemetry/tasks.jsonl` path those
commands already read from. This P0 was measurement-blocked in a way
this document didn't previously say out loud: the tool to close it did
not just lack data, it structurally could not have collected any.

> Guarded by `tests/test_pipeline.py::test_every_finished_turn_is_recorded_to_telemetry`

### 6.2 No real traffic

Every number in `README.md` came from synthetic data. `harness label` and
`harness evidence` can now actually see something (6.1) -- but the log
starts empty from the moment this was fixed, not backfilled from
whatever use came before it. Nobody has fed them a real week yet either
way.

### 6.3 Undo coverage ceiling

Migrations, `dd`, `mkfs` require a declared `rollback:` entry. Automatic
generation for database migrations would need to understand the migration tool,
which is a real project rather than a patch.

### 6.4 `ResumePoint` doesn't actually resume anything

Found in a full-file audit. `Harness.execute` writes a `ResumePoint` to
`<workdir>/resume/<task_id>.json` after each completed step and deletes
it on success -- but `ResumePoint.load()`, which exists and works, has no
caller anywhere in the codebase. Nothing checks for an existing resume
file at the start of `execute()`, so a crash mid-task and a subsequent
retry (even `harness run` on the identical task file) re-runs every step
from scratch, including ones that already had real side effects.

Not fixed here: doing this safely needs real design work, not a
localized patch -- at minimum, verifying the retried plan's step IDs
still match the saved sequence before trusting `completed` at all, and
deciding whether a skipped step's declared undos still get pushed onto
the rollback stack (they should: a later step failing must still be able
to roll back work a resumed run itself didn't just perform). Until
someone builds that, treat the resume file as a crash-forensics artifact
you can read by hand, not a working feature.

### 6.5 Smaller, known

- Tree watching misses a same-size same-mtime edit; the SHA256 layer catches it
- Reflink shadow copies only help on btrfs and xfs, not the ext4 default
- One blind-corpus clarifier false positive (`run nvidia-smi and report the free
  VRAM in MB`), left unfixed deliberately so one unoptimised measurement survives
- `detect_accelerator()`'s ROCm branch (`harness/core/machine.py`) detects
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
entry points     harness.cli:main, harness.tui:run_tui, harness.pipeline:Harness
core object      Harness(workdir, ollama=, agent=, safety=, verifier=, undo=,
                         rollback=, conflicts=, clarifier=, router=, decisions=,
                         telemetry=, recheck_between_steps=True, knowledge_dir=None)
plan             Harness.plan(request, model=None) -> RunResult
execute          Harness.execute(result, approvals: Dict[str, bool], dry_run=False)
one shot         Harness.run(request, approve: Callable[[Plan], Dict[str,bool]])

outcomes         answered | needs_answer | refused | awaiting_approval |
                 rejected | blocked | completed | failed | backend_down
stages           converse observe propose gate present execute report
risk levels      safe caution dangerous critical
exit codes       0 ok, 1 usage, 2 gate-stopped or unapproved, 3 failed

model contract   JSON only, schema in harness.agent.PLAN_SCHEMA
                 intent: plan | ask | answer | refuse
                 the model NEVER executes; it returns a TaskSpec

audit            <workdir>/audit/<task_id>.audit.json, every decision with reasoning
telemetry        <workdir>/telemetry/tasks.jsonl, append-only, secrets redacted
resume           <workdir>/resume/<task_id>.json, written after each completed step
shadow copies    <workdir>/harness-shadow-<pid>/ (CLI/one-shot) or
                 <workdir>/harness-shadow-<session_id>/ (TUI, one per tab, see §2.23)
sessions (TUI)   <workdir>/sessions/<session_id>/{meta.json,transcript.jsonl}

test command     python3 -m pytest tests/ -q     # 568
```

---

## 9. The one thing to keep

If everything else here is rewritten, keep this: **the model proposes, and
something that is not the model decides.**

Six phases of gate-building were spent on the wrong problem, because nobody
checked that an agent was going to be plugged in. The gates turned out to be
worth keeping anyway, and the reason is that separation. A model that could act
directly would have made every one of them optional.
