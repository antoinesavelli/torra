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
python3 -m pytest tests/ -q          # must print 128 passed
harness doctor
```

Seven modules, ~5,400 lines shipped. `core.py`, `gates.py` and `telemetry.py` were later
each split into a same-named package of single-concern submodules (see
`DEFENSIVE_CODING_STANDARDS.md` §1.2 for why); every name still imports from the same
top-level path, e.g. `from harness.gates import SafetyClassifier`. Read in this order:

1. `harness/core/` — `Step`, `TaskSpec` (`tasks.py`), `OllamaClient` (`ollama.py`),
   conflict detection (`conflict.py`) — see `harness/core/README.md`
2. `harness/gates/` — the five guardrails, one submodule each — see `harness/gates/README.md`
3. `harness/agent.py` — the model loop
4. `harness/pipeline.py` — `Harness`, the seven stages, the only orchestrator

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

> Guarded by `tests/test_gates.py::TestVerifiedRollback`

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

### 2.10 Every way to decline a plan goes through the pipeline

`Harness.reject(result)` exists specifically because `execute(result,
{})` is **not** a safe way to implement "decline this plan": a step's
`approved` defaults to `True` whenever it doesn't need confirmation, so
calling `execute` with empty approvals on an all-safe plan does not
refuse it, it *runs* it. `reject()` sets `outcome = REJECTED`
unconditionally and calls `_finish()`, the same choke point every other
terminal outcome goes through -- audit record written, telemetry
recorded (2.9's neighbor gap, §6.1), decision logged.

Found in a full-file audit: the TUI's Reject button used to bypass all
of that -- `self.current = None` and nothing else. It looked harmless
(nothing runs either way) but meant the single most direct expression of
an operator declining a plan left no trace anywhere, including in the
one metric HANDOFF.md §6.1 names explicitly: "how often a human rejected
a plan outright." If you add another way to decline a plan (a CLI flag,
a keybinding), route it through `Harness.reject()`, not through
`execute()` with an empty approvals dict and not through UI state alone.

> Guarded by `tests/test_pipeline.py::test_reject_never_runs_an_all_safe_plan`
> and `tests/test_tui.py::test_reject_records_the_decision`

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
python3 -m pytest tests/ -q                       # 128, must stay green
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

**`ConflictDetector` reconcile schedule.** The event fast path is 5,000x cheaper
than walking, but inotify has three silent failure modes: queue overflow,
directories created after watches attach, and moves in from outside. The full
walk every `reconcile_every` checks is what makes those survivable, and
disagreements are counted in `stats()`. Do not remove it for speed.

**`ContextCompressor` degradation ladder.** With many small entries, a per-entry
summary line costs more than the content it replaces, so compression becomes a
net loss. There are three fallback tiers and a hard guard that skips the pass
rather than making things worse. This was measured at **-484% reduction** before
the guard existed.

**`Clarifier` anchor scrubbing.** Vague-word checks run against text with the
anchors blanked out. Without it, `yeet the /srv/api/old.log file` trips the
vague-qualifier rule on the `old` inside a filename.

**Agent repair loop bound.** `MAX_REPAIRS = 2`. A model that will not improve
must not spin. The last proposal is still returned for real gating even after
the budget is spent.

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
timer must be created on the app's own thread -- `_set_busy` is only ever
invoked via `call_from_thread` from the worker threads that plan/execute, so
this holds today, but a new caller that calls it directly from a background
thread would break silently (Textual timers are not thread-safe to create).

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
                         rollback=, conflicts=, clarifier=, router=, decisions=)
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
shadow copies    <workdir>/harness-shadow-<pid>/, reaped by age, prefix+pid strict

test command     python3 -m pytest tests/ -q     # 128
```

---

## 9. The one thing to keep

If everything else here is rewritten, keep this: **the model proposes, and
something that is not the model decides.**

Six phases of gate-building were spent on the wrong problem, because nobody
checked that an agent was going to be plugged in. The gates turned out to be
worth keeping anyway, and the reason is that separation. A model that could act
directly would have made every one of them optional.
