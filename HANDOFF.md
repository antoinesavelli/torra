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
python3 -m pytest tests/ -q          # must print 721 passed
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

### 2.28 A `tests/test_tui.py` test that dispatches a Textual worker must wait for it before its `async with app.run_test()` block ends

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

### 2.29 `HarnessApp._handle_exception` is the only hook that ever sees a TUI-side crash, and `App.run()` never re-raises past it

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
never fire; overriding `HarnessApp._handle_exception()` is the only correct
integration point. Also confirmed single-threaded: a thread-worker's
failure is caught back on the main event-loop thread (inside `Worker._run`)
before `_handle_exception` ever runs, so there is no concurrency concern for
`CrashReporter.record()` itself.

Also confirmed by reading `tui.py`'s own worker bodies: `_plan_request_body`/
`_execute_plan_body` each have their own internal `try/except Exception`
around `Harness.plan()`/`.execute()` that displays the error inline and
clears `busy` (`tests/test_tui.py::test_plan_request_reports_an_unexpected_exception`,
`test_execute_plan_reports_an_unexpected_exception`) -- those exceptions
never reach `_handle_exception` at all, so `CrashReporter`
(`harness/telemetry/crash.py`) records only genuinely-unhandled TUI bugs
(the same class of thing §2.13/§2.25 found by hand before any hook existed),
never routine plan/execute failures, which `TaskLogger` already records
with `status: failed`/`backend_down`. A crash count in `harness evidence`'s
new health summary should not be read as including ordinary task failures.

`CrashReporter.record()` redacts `message`/`traceback` through the same
`redact_secrets()` `records.py` already uses for `TaskRecord.request`/
`Label.why` -- a traceback's local-variable reprs can contain a secret
exactly the way free-form request text can (Textual's own panic screen
already renders locals via `rich.traceback.Traceback(show_locals=True,
...)`, confirmed by reading `_fatal_error()`; persisting an unredacted copy
of the same information indefinitely would be strictly worse than what's
already shown transiently on screen).

`HarnessApp._handle_exception`'s override calls `self._crash_reporter.record(...)`
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
   unconditionally. Per §2.29, `HarnessApp._handle_exception` is the only
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
   standalone (`HarnessApp._build_sidebar`).
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
of `cli.py` into `harness/telemetry/summary.py` (`health_summary`/
`crash_summary`, no longer CLI-private) so the TUI's new Evidence tab could
reuse them without `tui.py` importing from `cli.py` -- the codebase's two
leaf/entry-point modules had never depended on each other before, and
having one do so would have been the first such coupling. The move alone
regressed the defensive-coding scorecard's Complexity dimension:
`health_summary` had always had a CCN of 20, but `cli.py` is peripheral
tier (not scorecard-enforced) while `harness/telemetry/` is core tier, so
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
> `tests/test_tui.py::test_append_thinking_mounts_and_grows_a_live_reasoning_line`
> + `test_clear_thinking_removes_the_live_reasoning_line` +
> `test_a_completed_plan_request_leaves_no_reasoning_line_behind`

### 2.32 Live step output has to reach `shell()` through a thread-local sink, not a `Step.run` parameter, because `Step` objects are built once at plan time and reused verbatim through execute

The TUI's other blind spot besides §2.31's reasoning tokens: while a real
step runs, the operator saw a `running...` spinner and nothing else --
`shell()` used `subprocess.run()`, fully blocking, so no output exists
anywhere until the whole command finishes. Worse than the reasoning-token
gap: a successful step's captured stdout was never shown *at all*, live or
after the fact -- `_executed()` only ever prints `ok`/`fail` + seconds +
`error`, never `StepResult.output`. This was the only way to close that,
not an optional nicety.

The design constraint that shaped the fix: `TaskSpec.to_steps()` builds
every step's `run` closure exactly once, in `Harness._gate()`, at **plan**
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

What shipped: `harness/core/tasks.py`'s `shell_output_sink(on_output)`, a
context manager wrapping a plain `threading.local()`. `shell()` checks it
on every call; `Harness._run_one_step()` enters it only around the real
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

`ResumePoint.load()` had no caller anywhere in the codebase until this
entry (§6.4's "fixed" pointer) -- `harness/core/resume.py`'s own
`resolve_resume()` is what changed that: `Harness.execute()` calls it
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
   `HarnessError` on that) file.
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

First real-traffic labeling pass this project has ever done (2026-08-26,
57 of 100 logged requests, `harness label`): `drift()` reported a 72%
live false-positive rate against a 10% synthetic baseline -- "DRIFT: the
clarifier is interrupting real work." Investigated before touching
anything, per this file's own §7 discipline (measure before you build):
`drift()`'s `_false_positives`/`_false_negatives` called
`clarifier.detect(request)` fresh on the labeled text and compared that
in isolation to the human's label. But `Agent._messages()`'s own prompt
(`harness/agent.py`) tells the model exactly the opposite of "a flagged
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

Found mining the same real traffic §2.34/§2.35 came from, not a code
audit: a real operator hit the identical failure four times in a row
across escalating attempts -- *"what ar ethe files that are taking up
the most room"* → failed, *"reprint in gb"* → failed, *"failed"*
(literally typed that alone), *"figure out the isse with du home
step"* → failed again, same error every time. Reproduced directly:
`du -ah /home | sort -rh | head -20` -- an entirely ordinary, correct
command -- fails under this harness's `pipefail` wrapper with exit 141.

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

Added so `OPENROUTER_API_KEY` (and anything else) can live in a `.env`
file at the repo root instead of shell config -- `cli.py`'s
`_load_env_file()`, called first thing in `main()`, before argument
parsing even, so it runs ahead of every command that could construct an
`OpenRouterClient()` (`build_harness()`, HANDOFF.md §2.27).

Two choices worth stating so a future edit doesn't quietly undo them:

- **Explicit path (`Path(__file__).resolve().parent.parent / ".env"`),
  not `load_dotenv()`'s bare default call.** The default searches
  upward from the current working directory -- fine for a project you
  always run from inside, wrong for this one: `harness` is meant to be
  run from wherever the operator happens to be (README's own `alias
  harness=...` example), so the default search could just as easily
  load an unrelated `.env` belonging to whatever *other* project's
  directory the operator's shell happens to be sitting in at the time.
- **Never overrides a variable already set in the real environment**
  (`load_dotenv()`'s own default -- deliberately not passed
  `override=True`). An explicit `export OPENROUTER_API_KEY=...` in the
  shell always wins over the file, so the file is safe to leave around
  with a stale or placeholder value without it silently shadowing a
  real one set elsewhere.

`.env` is gitignored; `.env.example` (tracked) documents the expected
keys with empty placeholder values, never a real one.

> Guarded by `tests/test_cli.py::TestLoadEnvFile`: a not-yet-set variable
> gets loaded, an already-set one is never overridden, a missing file is
> a silent no-op, and `main()` is proven to actually call it (not just
> that the function works in isolation)

### 2.38 The launch greeting stopped listing models inline; `f2` opens a picker instead, filtered through the same `router._chat_capable()` the routing path itself trusts

`_greet()`'s healthy branch used to dump `self.ollama.models()` as a raw,
unfiltered comma list -- including embedding-only models like
`nomic-embed-text` (the exact model §"OS-Engineering Guardrails" already
names as a real routing bug once it was mistaken for a chat target,
`harness/gates/router.py`'s `ROUTING_RULES` fix). The new `ModelPicker`
(mirrors `SessionPicker`'s `ModalScreen[str | None]` shape) filters through
`tab.harness.agent.router._chat_capable()` before listing anything, so the
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

Beyond that, `harness/tui.py`'s `VERIFIED_OPENROUTER_MODELS` adds a small,
hand-maintained list of `openrouter/...` slugs to the picker -- not a live
catalog fetch. `OpenRouterClient` has no `models()` call at all (unlike
`OllamaClient.models()`, which the local half of the picker actually
calls), and OpenRouter's real catalog is hundreds of entries deep -- not
something a picker meant for a handful of taps should try to enumerate or
filter live even if it could. Each entry in the list is only added after
a real live slug lookup plus an actual `harness plan`/`ask` call through
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

Fixed by rebinding to `f2` (`harness/tui.py`'s `HarnessApp.BINDINGS`) --
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
checks `HarnessApp.BINDINGS` itself against `textual.keys.KEY_ALIASES`,
via `Binding.make_bindings()` (the same normalizer Textual's own
`BindingsMap` uses, so it handles the full `str | tuple | Binding` union
`BINDINGS` entries are allowed to be) -- so the next binding added to this
class of dead key never needs a live user report to be caught. It does
*not* check the separate `Input`-shadowing class (`ctrl+d`/`ctrl+w`
above) -- that would fail on bindings that already exist today and is a
different, wider fix than this section covers.

> Guarded by `tests/test_tui.py::
> test_no_app_binding_uses_a_key_that_is_really_a_terminal_ansi_alias`

### 2.40 Attempting to close §6.1's 82% live-ask gap: one approach measured worse and was reverted, one measured real and shipped -- rule 6's "Do not plan a task" was quietly forbidding the harness from doing its own job

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
(`git checkout -- harness/agent.py`) before it reached a commit. The
obvious next move -- fire only for some subset of gap *codes* that
"really" mean it -- was rejected without trying: picking which codes
deserve enforcement is the same closed-vocabulary bet §3's table already
measured failing twice elsewhere in this file.

**Attempt 2, shipped: rule 6 was telling the model not to do the one
thing this harness exists for.** Investigating why real traffic showed
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
(§5 "The repair loop is not just for intent 'plan.'", no new failure
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
  at all -- confirmed live across every `harness plan` run earlier in
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

**The one risk this can't engineer away:** the harness has no model of
dependencies *between* steps. Declining step 2 while approving step 3
might make step 3 fail or behave surprisingly, and nothing here can
detect that in general -- it's on the operator to judge when ticking
boxes. `Harness.dependency_warnings(plan, approvals)` (a separate,
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

Both `harness.dependency_warnings` and `PLAN_SCHEMA`/`SYSTEM_PROMPT`
(§2.41 above) were split into their own top-level modules
(`harness/dependency_warnings.py`, `harness/prompts.py`) in the same
change -- not a style preference, a measured one: `pipeline.py` and
`agent.py` both crossed the defensive-coding scorecard's 500-functional-LOC
Modularity threshold once these additions landed (GPA 3.83 -> 3.67),
and `scripts/dev/defensive_coding_report.py`'s Definition of Done
requires fixing that in the same change, not deferring it. Same
precedent as `resolve_resume()` moving into `core/resume.py` (§2.33):
a real, measured reason, not LOC-shuffling for its own sake. Neither
module lives under `harness/gates/` despite being safety-adjacent --
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
> `test_a_completed_plan_request_leaves_no_reasoning_text_behind`

### 2.44 `/auto on`: skip the Run click for a plan that already needed no tick, never for one that does

Direct feedback: even an all-SAFE/CAUTION plan (already auto-approved by
`Verdict.needs_confirmation`, no checkbox rendered at all) still required
a manual Run click in the TUI -- the CLI's `harness ask` never had this
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
> test_auto_run_executes_a_plan_with_nothing_needing_confirmation`,
> `test_auto_run_never_bypasses_confirmation_for_a_dangerous_step`,
> `test_auto_run_defaults_to_off`, `test_auto_command_turns_it_on_and_off`,
> `test_bare_auto_command_reports_state_without_toggling`,
> `test_auto_command_rejects_a_garbage_argument`

### 2.45 Session and Model got their own sidebar tabs alongside Plan (relabeled "Actions") and Thinking -- persistent siblings of the `ctrl+o`/`f2` modals, not replacements

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

Direct feedback, in two parts: "let me see what files it's accessing," and
(once investigating that surfaced a real gap) "let the model drop content
that turned out not to matter." The gap found first: execution output
never re-entered `Agent.context` at all. `Agent._remember()` only ever
stored the operator's request text and the *proposed step commands*
(`turn.spec.steps[i]['run']`) -- real `StepResult.output` from
`Harness.execute()` is a separate call, entirely disconnected from
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
failed, not skipped-as-declined), `Harness._finish_success()` feeds its
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
the free function `remember_step_reads` that `Harness` calls) was split
into its own module, `harness/read_memory.py`, in the same change --
`agent.py` and `pipeline.py` both crossed the 500-functional-LOC
Modularity threshold the moment this landed inline (GPA 3.83 -> 3.67),
same real, measured reason `dependency_warnings.py`/`prompts.py` were
split out earlier (§2.41/§2.42). One real wrinkle in doing that split:
`harness.pipeline_types` already imports `Turn` from `harness.agent`, so
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

`Harness.execute()` already refused to actually replay commands on an
already-executed `RunResult` (`_refuse_replay`, keyed on
`result.executed`, §2.8) -- that part was never broken. What was broken:
`_set_busy` only disables Run/Dry-run/Reject *while* `busy=True`, then
re-enables them the moment a run finishes, so Run stayed clickable
(still green, `variant="success"`) after a completed real run. A second
click dispatched `execute_plan()` again, which called
`tab.harness.execute()` again, which hit `_refuse_replay` and handed
back the *same* `RunResult` with its `results` list untouched --
`_executed()` then iterated that list and re-said every step's
"ok/fail ... Ns" line, the completed/failed message, the agent summary,
and the rollback report a second time. No command actually ran twice,
but the operator watching the chat pane could not tell that from "the
chat repeating."

Fixed in the TUI layer only -- `Harness.execute()`/`_refuse_replay`
itself is untouched, per this file's own "ask first" rule for that
method. Three pieces, all in `harness/tui.py`:

1. `pressed_run` now checks `tab.current.executed` before dispatching at
   all. If a real run already completed on this exact plan, it pushes a
   new `ConfirmDialog` (a small `ModalScreen[bool]`, same shape as
   `SessionPicker`/`ModelPicker`) instead of calling `execute_plan()`
   directly. `result.executed` is False for a REJECTED-with-nothing-
   approved outcome and for a BLOCKED-before-any-step-ran outcome (see
   §2.8's fix) -- both stay retryable with no prompt, unchanged.
2. `_execute_plan_body` now checks `not dry_run and result.executed`
   *before* calling `tab.harness.execute()` at all, and short-circuits
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
> -- spies on `Harness.execute` to assert it is called exactly once
> total (never a second time, not even a refused one, since the TUI now
> short-circuits before reaching it), asserts the confirmation dialog
> appears before anything runs, that cancelling adds zero chat lines,
> and that confirming adds exactly one.

### 2.48 A sixth sidebar tab, Metrics: spend, token use, model/hardware stats, and which files a session's read-only steps have actually fed into the model

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
`HarnessApp.ollama` and `HarnessApp.backends` are **single instances
shared across every tab** (`HarnessApp.__init__` takes both from the
first tab's `Harness` and hands the same objects to every later
`_new_harness()` call, confirmed by reading that constructor, not
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

> Guarded by `tests/test_tui.py::
> test_the_sidebar_metrics_tab_shows_session_stats_and_files_read`
> (drives a real `reads` step through Run, then asserts the model-in-use
> line, a real `accelerator:` line, the per-session turn count, the
> shared client's cumulative-calls line, and the read file path all
> appear) and the updated
> `test_sidebar_tabs_are_in_the_requested_order_with_actions_relabeled`.

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
| Repair-loop check firing on any non-empty `clarifier_gaps` | broke 6 existing tests -- `no_target` fires on nearly any ordinary question | reverted before commit, §2.40 |

The pattern in the clarifier rows is the important one. Three times now, a
closed vocabulary (or a signal that turned out to behave like one) was relied
on to separate "genuinely ambiguous" from "ordinary," and measured worse than
expected. **If you find yourself adding items to a list of things to detect,
ask whether the list is the bug.**

---

## 4. How to verify a change

```bash
python3 -m pytest tests/ -q                       # 721, must stay green
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

**The topic-index scan is non-recursive, but `machines/<hostname>/` is a
named exception, added after finding it missing against the real
library.** `_scan_knowledge_dir` originally only globbed the configured
directory's own top level, on the reasoning that subdirectories are
implementation detail (`fixes/`, `raw/`, `lists/`-style capture data) --
true for the library that motivated the mechanism above, but not for the
one actually in use: `system-library` (this project's own
`$HARNESS_KNOWLEDGE_DIR`, set by the operator's `harness` shell alias)
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

### 6.1 Model quality was completely unmeasured -- **the measurement gap is closed; what it found is a live, still-open question, not a bigger version of the same gap**

Every test still uses a scripted backend, and that still proves the
plumbing and nothing about whether a local model writes good plans on
its own -- that part of this entry hasn't changed. What has changed:
until a full-file audit found it, none of the "what to measure" list
below was actually collectible. `TaskLogger.record()` had no caller
anywhere in `pipeline.py`, `agent.py`, or `tui.py`; `harness label`/
`harness evidence` only ever *read* `tasks.jsonl`, so both could only
ever report zero tasks no matter how much real use the harness saw. This
was measurement-blocked, not just data-poor: the tool to close it did
not just lack data, it structurally could not have collected any.
`Harness._finish()` now calls `self.telemetry.record(...)` for every
turn, and real traffic has been running against it since (§6.2). Each
"what to measure" question below now has a real, if still small-sample,
answer as of this snapshot (`harness evidence`, 143 recorded tasks, 29
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
is **more real post-fix traffic, then re-run `harness evidence` against
it specifically** (`harness label` first, same as always) -- not a
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
and nobody had fed the harness a real week. Since then: 143 real tasks
recorded (`~/.harness/telemetry/tasks.jsonl`, spanning about a month of
actual use across CLI/TUI/OpenRouter), 91 of them hand-labeled
(`harness label`) for the ambiguous/specific measurement §6.1 and §2.34
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
  to say the opposite -- "the harness derives undos for files, services,
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
                 -- a declined step is skipped, not a whole-plan reject,
                 unless *zero* steps end up approved (§2.42)
dep. warnings    Harness.dependency_warnings(plan, approvals) -> list[str]
                 -- best-effort, call before execute(), never blocks (§2.42)
one shot         Harness.run(request, approve: Callable[[Plan], Dict[str,bool]])

outcomes         answered | needs_answer | refused | awaiting_approval |
                 rejected | blocked | completed | failed | backend_down
stages           converse observe propose gate present execute report
risk levels      safe caution dangerous critical
exit codes       0 ok, 1 usage, 2 gate-stopped or unapproved, 3 failed

model contract   JSON only, schema in harness.prompts.PLAN_SCHEMA
                 (re-exported from harness.agent for existing importers)
                 intent: plan | ask | answer | refuse
                 the model NEVER executes; it returns a TaskSpec

audit            <workdir>/audit/<task_id>.audit.json, every decision with reasoning
telemetry        <workdir>/telemetry/tasks.jsonl, append-only, secrets redacted
resume           <workdir>/resume/<task_id>.json, written after each completed step
shadow copies    <workdir>/harness-shadow-<pid>/ (CLI/one-shot) or
                 <workdir>/harness-shadow-<session_id>/ (TUI, one per tab, see §2.23)
sessions (TUI)   <workdir>/sessions/<session_id>/{meta.json,transcript.jsonl}

test command     python3 -m pytest tests/ -q     # 721
```

---

## 9. The one thing to keep

If everything else here is rewritten, keep this: **the model proposes, and
something that is not the model decides.**

Six phases of gate-building were spent on the wrong problem, because nobody
checked that an agent was going to be plugged in. The gates turned out to be
worth keeping anyway, and the reason is that separation. A model that could act
directly would have made every one of them optional.
