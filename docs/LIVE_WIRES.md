# torra: live wires

Live wires: places where an obvious-looking change is wrong. The `§5.N` numbers come from the retired
`HANDOFF.md` §5 and never change, so code and tests cite `docs/LIVE_WIRES.md §5.N`. The story behind each
existing entry is in `git log -p -- HANDOFF.md`; a new entry's story goes in its commit message.

Entry shape: **Rule** (what must hold), **Why** (the failure it prevents), **Guarded by** (the tests), **Fix**
(the commit, when one is named). A new entry takes the next unused number, lands in the same change as its
guard test, and stays within 10 non-blank lines under its heading (`tests/test_knowledge_refs.py` enforces it
from §5.26 on). Find one with `grep -n '^### 5\.' docs/LIVE_WIRES.md`.

### 5.1 `SafetyClassifier._decode_base64`'s blob pattern must not require quotes.

**Rule:** `CommandResolver._decode_base64` (`torra/gates/safety_resolve.py`) must match a bounded run of
base64-alphabet characters directly, `(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{8,}(?![A-Za-z0-9+/=])`, with no quote
delimiters required.
**Why:** `echo BLOB | base64 -d | sh` needs no quotes; a quote-requiring regex let the unquoted form evade
decoding, so `rm -rf /home` classified DANGEROUS (generic "piped into a shell" BACKSTOP) instead of the
resolved CRITICAL.
**Guarded by:** `tests/test_gates.py::TestSafety::test_an_unquoted_base64_payload_is_decoded_too`

### 5.2 `_strip` must know which wrapper flags consume a value.

**Rule:** `WRAPPER_VALUE_FLAGS` (`torra/gates/safety_tables.py`) must list every value-taking flag of every
wrapper in `WRAPPERS` (timeout `-s`/`--signal`/`-k`/`--kill-after`, nice `-n`, ionice `-c`/`-n`/`-p`, stdbuf
`-i`/`-o`/`-e`, xargs's several, env `-u`/`-C`/`-S`, sudo `-u`/`-g`/`-h`/`-p`); a new wrapper added to
`WRAPPERS` gets its value flags added in the same change.
**Why:** without it `timeout -s KILL 5 kill -9 1` classified SAFE outright: `_strip` left `KILL` as the
program name, `kill` never reached `_judge_kill`, and no regex BACKSTOP covers kill-of-PID-1 (argv-only
check). An ordinary idiom would slip a PID-1 kill past every gate.
**Guarded by:**
`tests/test_gates.py::TestSafety::test_kill_of_pid_1_is_caught_even_behind_timeouts_value_taking_flag`,
`tests/test_gates.py::TestSafety::test_an_unquoted_base64_payload_is_decoded_too`

### 5.3 `ConflictDetector` reconcile schedule.

**Rule:** `ConflictDetector._detect_tree` (`torra/core/conflict.py`) keeps its full walk every
`reconcile_every` checks; do not remove it for speed.
**Why:** the inotify event fast path is 5,000x cheaper than walking but has three silent failure modes (queue
overflow, directories created after watches attach, moves in from outside); the periodic walk makes them
survivable and disagreements are counted in `stats()`.
**Guarded by:** nothing automated (trust-based)

### 5.4 `_detect_tree()`'s fast path must clear `_known`, not just add to it.

**Rule:** in `ConflictDetector._detect_tree` (`torra/core/conflict.py`), when `_classify` returns `None` for a
touched path the loop must `self._known.pop(path, None)`, not only record conflicts.
**Why:** `_known` persists across `detect()` calls by design; a path edited then reverted to its baseline
bytes still fires an inotify event, and without the pop the stale conflict fed `Torra._run_steps()` a false
"external conflict" and blocked a real run until the next full walk (up to `reconcile_every`, default 20,
calls later).
**Guarded by:** `tests/test_core.py::TestConflictDetectorFastPath`

### 5.5 `ContextCompressor` degradation ladder.

**Rule:** `ContextCompressor.compress` (`torra/core/context.py`) keeps its three fallback tiers and the hard
guard that skips the pass rather than making things worse.
**Why:** with many small entries a per-entry summary line costs more than the content it replaces; it measured
-484% reduction before the guard existed.
**Guarded by:** nothing automated (trust-based)

### 5.6 `Clarifier` anchor scrubbing.

**Rule:** `Clarifier._scrub` (`torra/gates/clarifier.py`) must run the vague-word checks against text with the
anchors blanked out.
**Why:** without it `yeet the /srv/api/old.log file` trips the vague-qualifier rule on the `old` inside a
filename.
**Guarded by:** nothing automated (trust-based)

### 5.7 `_check_no_environment` must recognize a target through the anchor system, not a separate preposition guess.

**Rule:** `Clarifier._check_no_environment` (`torra/gates/clarifier.py`) must reuse the anchor labels from
`anchors_in()` ("named environment", "hostname or tagged identifier"), not a looser `\b(to|into|on)\s+\S+`
fallback or a verb-exclusion word list.
**Why:** the preposition pattern matched "to"/"into"/"on" plus any word, so "deploy the api to fix the bug"
was treated as having named an environment. The file's design already rejected closed-vocabulary approaches on
measurement, so existing anchor infrastructure is reused.
**Guarded by:** `tests/test_gates.py::TestClarifier::test_deploy_to_fix_something_is_still_flagged`

### 5.8 Agent repair loop bound.

**Rule:** `Agent.MAX_REPAIRS` (`torra/agent.py`) is 2, and the last proposal is still returned for real gating
even after the budget is spent.
**Why:** a model that will not improve must not spin.
**Guarded by:** nothing automated (trust-based)

### 5.9 The repair loop is not just for intent "plan."

**Rule:** `Agent._repair_complaint` (`torra/agent.py`) dispatches on intent: `_prescreen` for "plan",
`_answer_dropped_a_task` for "answer" (any `task` object attached to an answer-intent reply, populated or
empty, triggers one repair round); a clean answer with no `task` never does.
**Why:** a model asked to summarize disk usage replied `intent: "answer"` with a `task` attached; `_parse`
reads `task` only for "plan", so the real content was silently discarded and the operator saw only the
announcement.
**Guarded by:** `tests/test_agent.py::test_an_answer_that_silently_drops_a_populated_task_is_repaired`,
`tests/test_agent.py::test_an_answer_with_an_empty_task_scaffold_is_also_repaired`,
`tests/test_agent.py::test_a_clean_answer_with_no_task_never_wastes_a_repair_round`

### 5.10 `_answer_dropped_a_task` doesn't catch a placeholder with no task at all.

**Rule:** `Agent._answer_is_a_placeholder` (`torra/agent.py`) must flag an answer by a structural tell
(`message` opens with a deferral verb like "Checking"/"I'll look" AND closes by naming "your question"/"the
request"), requiring both ends, not a keyword list.
**Why:** a reply "Checking system library documentation ... to answer your question." with no `task` sailed
through both repair rounds and was shown as the final answer; requiring both ends keeps a real answer opening
"Checking the logs, ..." from being flagged.
**Guarded by:** `tests/test_agent.py::test_an_answer_that_only_promises_to_check_is_repaired`,
`tests/test_agent.py::test_a_short_answer_that_happens_to_open_with_checking_is_not_flagged`

### 5.11 `TorraApp.BINDINGS` must never claim `ctrl+c`.

**Rule:** `TorraApp.BINDINGS` (`torra/tui.py`) must not bind `ctrl+c`; quit lives on `ctrl+q`, and `ctrl+c`
keeps Textual's default of `screen.copy_text`.
**Why:** Textual deliberately made ctrl+c copy, not quit; an earlier version bound it to "quit" and copying
selected chat/plan text closed the whole app.
**Guarded by:** `tests/test_tui.py::test_ctrl_c_copies_rather_than_quitting`

### 5.12 `_finish(write_audit=False)`.

**Rule:** `finish` with `write_audit=False` (`torra/pipeline_report.py`, the awaiting-approval case) leaves
the stage list open and writes no audit record.
**Why:** the run has not happened yet; an earlier version appended the report stage during `plan()` and
produced `present -> report -> execute` in the stage list.
**Guarded by:** nothing automated (trust-based)

### 5.13 The TUI status line's spinner.

**Rule:** `TorraApp._set_busy` (`torra/tui.py`) starts a `set_interval(0.1, self._tick_spinner)` timer on
`True` and stops/clears it on `False`, and must only be called on the app's own thread (directly from the main
thread, or via `call_from_thread` from workers).
**Why:** a static "thinking..." label looks the same after one second or sixty (devstral's measured range
spans both); Textual timers are not thread-safe to create, so a new caller invoking `_set_busy` from a
background thread would break silently.
**See:** `scripts/dev/model_bench/REPORT.md`
**Guarded by:** nothing automated (trust-based)

### 5.14 `execute_plan()`'s busy guard is not optional UI polish.

**Rule:** `TorraApp.execute_plan` and `plan_request` (`torra/tui.py`) must check AND flip `busy` synchronously
on the main thread before dispatching the worker (first line, before touching `tab.current`).
**Why:** `@work(thread=True, exclusive=True)` does not preempt a running thread, so a second call (Ctrl+D has
no widget to disable) launched a concurrent `Torra.execute()` on the same mutable `RunResult` 113ms after Run;
flipping `busy` inside the worker left a window for a second dispatch.
**See:** §2.26
**Guarded by:** nothing automated (trust-based)

### 5.15 `detect_accelerator()`'s `ollama ps` column parsing must not assume a fixed column count.

**Rule:** `detect_accelerator` (`torra/core/machine.py`) must match the PROCESSOR cell by value shape,
`\d+%(?:/\d+%)?\s+(CPU/GPU|CPU|GPU)`, not by column position (not `" ".join(line.split()[-2:])`).
**Why:** `ollama ps` here also prints CONTEXT and UNTIL after PROCESSOR, so the positional grab read "from
now", "CPU" never matched, and a CPU-running model fell through to `nvidia-smi` and was reported
GPU-accelerated, which `ModelRouter.fits()` relies on being right.
**Guarded by:** `tests/test_core.py::TestDetectAccelerator`

### 5.16 `shell()`'s explicit `/bin/bash -c 'set -o pipefail; ...'`.

**Rule:** `run_in_group` (`torra/core/process.py`; `shell()` in `torra/core/tasks.py` delegates to it) must
run commands under explicit `/bin/bash -c 'set -o pipefail; ...'`, not `subprocess.run(command, shell=True,
...)`.
**Why:** `shell=True` is `/bin/sh` (dash), whose pipeline status is the last command's only; a failed `sudo du
-ah / | sort -rh | head -n 20` reported `"ok": true, "output": ""`.
**Guarded by:** nothing automated (trust-based)

### 5.17 `ModelRouter._downgrade()`'s `min(self.available, key=self._params_b)` fallback.

**Rule:** `ModelRouter._pick_replacement` (`torra/gates/router.py`, called by `_downgrade`) must pick the
smallest pulled model when nothing in `available` passes `runnable()`; `self.default_model` is only the true
last resort when `available` is empty or unset.
**Why:** `default_model` is "devstral", which can itself be the model that just failed the fit check;
collapsing the branch produced "downgraded from devstral because only 5660 MB VRAM free" that still resolved
to devstral.
**Guarded by:** nothing automated (trust-based)

### 5.18 The "quick" routing rule targets `devstral:24b`, not `qwen3.5:latest`, as of 2026-08-17.

**Rule:** the "quick" rule in `ROUTING_RULES` (`torra/gates/router.py`) must keep targeting `devstral:24b`; do
not revert it to "the smallest pulled model" or reach for `qwen3.5:latest` as a default.
**Why:** live testing gave `qwen3.5:latest` a content-free non-answer on 1 of 5 disk/status checks, while
`devstral:24b` answered all five; not a full fix (both fail on "enumerate/summarize across everything", only
`qwen3:32b` handles it and it rarely fits free VRAM). Full reasoning is in the rule's inline comment.
**Guarded by:** nothing automated (trust-based)

### 5.19 `UndoGenerator.cleanup()` recreates `shadow_dir` after deleting it.

**Rule:** `UndoGenerator.cleanup` (`torra/gates/undo.py`) must recreate `shadow_dir` after removing it, not
just `shutil.rmtree(self.shadow_dir, ignore_errors=True)`.
**Why:** one `UndoGenerator` lives per `Torra` and `_gate()` snapshots at propose time, so deleting without
recreating silently degrades every later `capture_file` to `skipped` for the session. `cleanup()` previously
had zero callers, so 14 shadow dirs accumulated (2026-07-29 full-file audit MED-1, retired
`AUDIT_FINDINGS.md`; `git log --diff-filter=D -- AUDIT_FINDINGS.md`).
**Guarded by:** `tests/test_gates.py::TestUndoGeneration`,
`tests/test_pipeline.py::test_a_successful_run_cleans_up_its_shadow_copies`,
`tests/test_pipeline.py::test_a_dry_run_keeps_its_shadow_copies`

### 5.20 Every `systemctl` call resolves scope first, never hardcodes `--user`.

**Rule:** `systemctl_scope` (`torra/gates/service_state.py`) and its deliberate duplicate
`StateVerifier.systemctl_scope` (`torra/gates/state.py`; `UndoGenerator._systemctl_scope` in
`torra/gates/undo.py` aliases the first) probe `LoadState` to resolve scope before any `systemctl` call; none
may hardcode `--user`.
**Why:** `is-active` alone reports "inactive"/exit-4 for both "not loaded in user scope" and "genuinely
inactive"; `ollama.service` is a system unit on the reference machine, so hardcoded `--user` (recovery advice,
precondition probe, generated rollback) targeted the wrong instance (2026-07-29 audit MED-2, retired
`AUDIT_FINDINGS.md`).
**Guarded by:** `tests/test_gates.py::TestSystemctlScope`,
`tests/test_gates.py::TestUndoGeneration::test_service_undo_uses_the_same_scope_it_was_captured_under`

### 5.21 Site-knowledge library gives the model awareness, never a new capability.

**Rule:** `TORRA_KNOWLEDGE_DIR` (read once via `Torra._resolve_knowledge_dir`, `torra/pipeline.py`) feeds only
a first-heading topic index (`_scan_knowledge_dir`) into `MachineState.render()`'s machine-state message,
never file contents into the prompt or `SYSTEM_PROMPT`; the model must propose a read-only grep/cat step that
`SafetyClassifier` classifies like any other.
**Why:** wiring contents straight into the prompt would bypass the gate a step gets today.
Capability-dependent, not guaranteed: `devstral:24b` reliably proposes the `grep -ril 'KEYWORD' {path}/*.md`
step, the smallest pulled model (`qwen3.5:latest`, now reachable only via the downgrade fallback, see §5.18)
does not after three wording rounds.
**See:** §5.18, `scripts/dev/model_bench/REPORT.md`
**Guarded by:** `tests/test_core.py::TestKnowledgeLibrary`,
`tests/test_agent.py::test_knowledge_dir_reaches_the_default_observer`,
`tests/test_pipeline.py::test_knowledge_dir_defaults_from_the_environment` + 3 more

### 5.22 The topic-index scan is non-recursive, but `machines/<hostname>/` is a named exception, added after finding it missing against the real library.

**Rule:** `_scan_knowledge_dir` (`torra/core/machine.py`) indexes the directory's top level plus
`<dir>/machines/<hostname>/*.md` (hostname from `socket.gethostname()`, not `state.hostname`), keyed
`machines/<hostname>/<file>.md` so same-named root files don't collide; `MachineState.render()`'s grep hint
derives targets from the topic keys (split on `/`). Do not go fully recursive.
**Why:** the real library puts primary content in `machines/<hostname>/{quirks,...}.md`, which the index and
grep hint silently missed; full recursion would surface other machines' docs (`machines/executor/quirks.md` on
`workstation`) as equally-weighted, wrong content.
**See:** §5.21
**Guarded by:**
`tests/test_core.py::TestKnowledgeLibrary::test_a_machines_hostname_subfolder_is_indexed_alongside_the_root`,
`tests/test_core.py::TestKnowledgeLibrary::test_a_same_named_file_at_root_and_in_the_machine_folder_does_not_collide`,
`tests/test_core.py::TestKnowledgeLibrary::test_no_hostname_or_no_matching_folder_stays_root_only`,
`tests/test_core.py::TestKnowledgeLibrary::test_observe_machine_uses_the_real_hostname_to_find_its_machine_folder`,
`tests/test_core.py::TestKnowledgeLibrary::test_render_greps_the_machine_subfolder_too_not_just_the_root`

### 5.23 `_finish()` must snapshot `result.decisions` *after* `_summarize()`, not before.

**Rule:** `finish` (`torra/pipeline_report.py`) must take the `decisions.trail()` snapshot into
`result.decisions` right before the audit write, after `_summarize()` has had its chance to log.
**Why:** a transient summarize failure logged "summary failed" (§2.11) into `self.decisions`, but a snapshot
taken before `_summarize()` left it out of `result` and the audit JSON: an empty summary with no record of
why.
**See:** §2.11
**Guarded by:** `tests/test_pipeline.py::test_a_failed_summarize_call_does_not_break_the_run` (asserts the
failure reason survives into both `result.decisions` and the written audit file)

### 5.24 `approve_at_prompt`'s Ctrl+C must abort the whole loop, not just the current step.

**Rule:** on `EOFError`/`KeyboardInterrupt`, `approve_at_prompt` (`torra/cli.py`) must `break` out of the
loop; the interrupted and remaining steps stay refused by never being added to the approvals map (absent is
treated as False).
**Why:** it used to refuse only the current step and prompt for the next CONFIRM step, inconsistent with
`_collect_answers`' handling of the same exception pair. Not unsafe (a refused step rejects the plan via
`_apply_approvals`), but one keystroke should not mean two things.
**See:** §2.3
**Guarded by:** `tests/test_cli.py::test_approve_at_prompt_aborts_on_interrupt_instead_of_asking_again`

### 5.25 Secret redaction: a multi-word value, and `Label.why`, both used to go to disk in the clear.

**Rule:** `redact_secrets` / `REDACTIONS` (`torra/telemetry/records.py`) must match to end of line (`\S.*$`,
multiline) after `password=`/`token:` etc., and be the one shared redaction used by both `TaskLogger` and
`Label.why` (`torra/telemetry/labels.py`).
**Why:** the `\S+` pattern redacted `"password = super"` and left `"secret phrase"` in the clear, and
`LabelStore.label()`'s free-text `why` went to disk unredacted; extracting a module-level `redact_secrets()`
keeps labels from drifting a second copy of the patterns.
**Guarded by:** `tests/test_telemetry.py::TestRedaction` (4 cases)

### 5.26 A declared path or watch root is CHECKED or UNCHECKED; torra's own unstattable is skipped, not a conflict.

**Rule:** `ConflictDetector`: a stat that raises at baseline makes it UNCHECKED (plan warning, Run log, never compared,
D-018); else CHECKED with a prior hash (None if absent). A CHECKED one whose stat raises is `unreadable` (declared,
root, or inotify event alike), unless torra's own step did it: `refresh` keeps the prior hash, and the mark ends the
first time torra can stat it again. A glob matters only to undo (`capture_file` skips it, and an unstattable or
non-`S_ISREG` path; never open-then-fstat: a FIFO blocks); its literal name is compared. Never let these raise.
**Why:** a glob's undo verified "undone" for nothing; a raising `exists()` crashed `plan()` (live 2026-11-19).
**Not covered** (`docs/STATUS.md` §6.5, xfail tests): a directory chmod'ed under a watch root; a root that stats but
cannot be listed; an outside edit made while torra's own step held the lock. Code: `ConflictDetector`, `capture_file`.
**Guarded by:** `tests/test_core.py::test_declared_path_and_watch_root_matrix`, `tests/test_pipeline.py`
(`test_a_step_locking_its_own_glob_dir_keeps_the_declared_rollback`, `test_a_touches_path_torra_cannot_stat_warns_at_plan_and_runs`)
