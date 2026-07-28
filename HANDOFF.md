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
python3 -m pytest tests/ -q          # must print 76 passed
harness doctor
```

Seven modules, 6,600 lines shipped. Read them in this order:

1. `harness/core.py` — `Step`, `TaskSpec`, `OllamaClient`, conflict detection
2. `harness/gates.py` — the five guardrails, one class each
3. `harness/agent.py` — the model loop
4. `harness/pipeline.py` — `Harness`, the seven stages, the only orchestrator

`tui.py`, `cli.py` and `telemetry.py` are leaves. Nothing depends on them.

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

> Guarded by `tests/test_pipeline.py` and the phase 5 cases in `legacy/`

### 2.6 An undo that cannot be proven is not "undone"

`VerifiedRollback` has three terminal states. `undone` requires the verifier to
return True. An action with no verifier reports `verified=None`, which is honest
about the fact that nothing was checked. Do not collapse these back to two.

> Guarded by `tests/test_gates.py::TestVerifiedRollback`

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
python3 -m pytest tests/ -q                       # 76, must stay green
cd legacy && python3 -m pytest tests/ -q          # 121, the original assertions
```

If you touch the clarifier:

```bash
cd legacy && python3 clarifier_corpus.py
```

Three corpora. Only the **blind** number is meaningful; the other two were used
for tuning. Current: 0% false negatives, 10% false positives. **Do not tune
against the blind set.** If you use it to diagnose something, write a fresh one
afterwards and report that number instead.

If you touch safety, conflict detection, or rollback, the phase probes in
`legacy/break_it*.py` still run and print severity-ranked findings.

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

### 6.2 No real traffic

Every number in `README.md` came from synthetic data. `harness label` plus
`harness evidence` are built and waiting; nobody has fed them a real week.

### 6.3 Undo coverage ceiling

Migrations, `dd`, `mkfs` require a declared `rollback:` entry. Automatic
generation for database migrations would need to understand the migration tool,
which is a real project rather than a patch.

### 6.4 Smaller, known

- Tree watching misses a same-size same-mtime edit; the SHA256 layer catches it
- Reflink shadow copies only help on btrfs and xfs, not the ext4 default
- One blind-corpus clarifier false positive (`run nvidia-smi and report the free
  VRAM in MB`), left unfixed deliberately so one unoptimised measurement survives

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

test command     python3 -m pytest tests/ -q     # 76
legacy tests     cd legacy && pytest tests/ -q   # 121
clarifier score  cd legacy && python3 clarifier_corpus.py
```

---

## 9. The one thing to keep

If everything else here is rewritten, keep this: **the model proposes, and
something that is not the model decides.**

Six phases of gate-building were spent on the wrong problem, because nobody
checked that an agent was going to be plugged in. The gates turned out to be
worth keeping anyway, and the reason is that separation. A model that could act
directly would have made every one of them optional.
