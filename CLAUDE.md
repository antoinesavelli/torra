# harness — Agent Instructions

@../system-library/AGENT_DOCTRINE.md

A local model proposes an operating-system change. Five gates decide whether it runs. The
operator approves. This file is instructions for an agent **editing this codebase** — not
instructions the harness itself sends to its planning model (that's `harness/agent.py`'s
`SYSTEM_PROMPT` and `docs/`, a different audience entirely; don't conflate the two).

Stack: Python 3.10+, ruff, mypy, pytest, Textual. Entry points: `harness.cli:main`,
`harness.pipeline:Harness` (the only orchestrator). Tests: **785, must stay 785 passed**
(`python3 -m pytest tests/ -q`) — nothing in `tests/` touches a live Ollama, systemd, or
network; every external dependency is an injected callable (see `tests/conftest.py`).

**This is an operating-system engineering agent first, a chat UI second.** Every design
decision here exists because the thing being gated is real shell commands against a real
Pop!_OS box — package managers, systemd, the filesystem, boot configuration. Optimizing
"agentic engineering" for this repo means optimizing for that domain specifically: command
semantics have to be resolved the way a real shell resolves them, not the way a regex
guesses; service-management assumptions have to match the actual systemd scope in play, not
the convenient default; and the model proposing changes never gets to skip the gates that
exist because a wrong OS-level command is not a bug you can quietly patch after the fact —
it already ran.

---

## Area Maps — read these first (token-efficient navigation)

Read the map for the area you're touching before grepping the source tree.

| Area | Map |
|---|---|
| Package overview (read after the subpackages below) | `harness/README.md` |
| The five gates (`safety`, `state`, `rollback`, `clarifier`, `router`, `decisions`) | `harness/gates/README.md` |
| Task/step primitives, conflict detection, context, Ollama client, machine observation | `harness/core/README.md` |
| Task logging, human labels, drift/loophole audits | `harness/telemetry/README.md` |
| Tests (organised by subsystem) | `tests/README.md` |
| Runnable example task files | `examples/README.md` |
| OS domain knowledge fed to the planning model (measured gaps only) | `docs/README.md` |
| Model-quality benchmark harness | `scripts/dev/model_bench/README.md` |
| **Invariants that must never break, with the story behind each** | `HANDOFF.md` §2 |
| Known live-wire bugs from the last full-codebase audit | `AUDIT_FINDINGS.md` |
| How code here is graded (six dimensions, tiering, floors) | `DEFENSIVE_CODING_STANDARDS.md` |
| Migrating off Claude Code as operator (what's built, what's scoped but not) | `CLAUDE_MIGRATION_ROADMAP.md` |

**`CORE_RULES.yaml` is not a source of truth.** It's a human-readable snapshot of default
values, loaded by nothing (`grep -rn CORE_RULES harness/` returns nothing) — it already
disagrees with the code it describes (`keep_recent: 3` there, `4` in
`ContextCompressor.__init__`) and nothing catches new drift. Never point someone at it to
answer "what does this actually do" — point at the module cited in its own header comment.
If you find yourself tempted to "fix" a behavior by editing this file, stop: you have edited
a description, not the system.

`HANDOFF.md` is this repo's Domain Invariants table and its Task Playbooks section at once —
read it before changing anything under `harness/`, the same way you'd read a nested
`CLAUDE.md` before touching a Paramo area. It is intentionally not duplicated below; this
file condenses it and adds what HANDOFF doesn't cover (Definition of Done, code conventions,
ask-vs-act), rather than restating it.

---

## Execution Architecture

Every task follows this loop — no shortcuts. Notice it's the same shape the harness enforces
on its own planning model (`converse → observe → propose → gate → present → execute →
report`): don't hold the code to a lower discipline than the code holds its model to.

```
OBSERVE → MODEL → DECIDE → ACT → VERIFY → CLOSE
```

1. **OBSERVE** — Read current state before acting: the relevant `README.md`, `HANDOFF.md` §2
   for any invariant the area touches, and the actual code — never edit from memory of a
   prior session or from `CORE_RULES.yaml`'s stale numbers.
2. **MODEL** — Identify blast radius. Is this on the safety-critical path (gates, the
   classifier, `Step.command`/`Step.run` construction, anything between "model proposes" and
   "shell executes") or peripheral (`cli.py`, `tui.py`, a docs file)? That answer decides how
   much rigor and how much asking-first this needs.
3. **DECIDE** — State the plan in one sentence. Ask if it touches the safety-critical path or
   persisted-state format; proceed if it's reversible and outside that path.
4. **ACT** — Smallest edit that satisfies the task. Parallel tool calls when independent.
   **Before defaulting to editing it yourself: is this mechanical/atomic-diff-sized, not a
   judgment call?** If so, check whether the on-disk local model should do it instead — see
   `system-library/AGENT_DOCTRINE.md`'s "Before you ACT: could the on-disk local model do this
   instead?" section and the `local-model-subagent` skill. This repo has no repo-specific coding
   agent of its own in `opencode.json` (just `instructions` pointing here), so that means the
   global `build`/`quick` agent names, not a Paramo-style named one.
5. **VERIFY** — Run the relevant tests; never skip. If the change touches classification,
   routing, or rollback, also re-read the specific `HANDOFF.md` §2 entry it's near and confirm
   the invariant the entry describes still holds, not just that tests are green (a test can be
   accidentally weakened in the same change that breaks what it guards).
6. **CLOSE** — Report what changed and what's next. No trailing narration.

### Definition of Done (applies to every change)

Not complete until all hold, in the *same* change:

1. **Tests run and pass.** `python3 -m pytest tests/ -q` — the count is the fact:
   **785 passed** is the current baseline. A change that adds behavior without adding a test
   for it isn't done; a change that *removes* a test to make the suite pass is very likely
   deleting the thing HANDOFF.md §2 told you not to delete — stop and re-read that section
   before doing it.
2. **Per-file docstring current.** One line + a `Depends on:` line for every touched module
   (see `DEFENSIVE_CODING_STANDARDS.md` §1.3). If you changed what a file does or imports, fix it.
3. **Area map current.** New/moved/renamed file → update that directory's `README.md` in the
   same change. New top-level directory → add a `README.md` and a row to the Area Maps table
   above.
4. **`HANDOFF.md` §2 current** if the change touches an existing invariant, or adds a new one
   worth the same treatment (what breaks, why it's not obvious, which test guards it). A new
   safety-relevant invariant with no entry here is invisible to the next agent who needs it.
5. **Lint and types clean.** `ruff check .` on touched files; `mypy <touched files>` with zero
   new errors vs. the pre-change baseline.
6. **Defensive-coding scorecard held.** `python3 scripts/dev/defensive_coding_report.py` — no
   graded dimension drops a letter grade from the current baseline in `DEFENSIVE_CODING_STANDARDS.md`
   §2.1 (`ENFORCE_FLOORS` in the script is the enforced version of that baseline). If one drops,
   fix the regression in the same change — see that doc §2's extraction pattern for Modularity.
7. **`CORE_RULES.yaml` is not updated as a substitute for step 2.** If a default value changes,
   the docstring/`Depends on` and the actual code are what's authoritative; touching the YAML
   snapshot is optional cleanup, never a stand-in for documenting the real change.
8. **No version bumps, tags, or a `CHANGELOG.md` entry.** This is a solo, private tool with
   no external consumers of the package as an API — `pyproject.toml`'s `version` stays fixed
   at `0.0.0` (present only because PEP 621 requires the field), there is no `harness --version`
   flag, and there are no git tags to keep current. `git log`/GitHub's own commit and compare
   views are the record of what changed and when; `HANDOFF.md` §2 is the record of why. Don't
   reintroduce any of this without the user asking for it back explicitly.

---

## Domain Invariants — Non-Negotiable

Full stories and exact guarding tests are in `HANDOFF.md` §2 — this table is the index, not a
replacement. If any of these is unclear before you act, read the full entry, then ask.

| Invariant | Guarded by |
|---|---|
| **The classified command IS the executed command** — `Step.command`/`Step.run` built from the same string | `test_agent.py::test_a_plan_becomes_a_validated_taskspec` |
| **The model never executes anything** — JSON proposal only, no shell/tool-call loop | `test_pipeline.py::test_a_hand_written_file_still_works_without_a_model` (`ollama.calls == 0`) |
| **Unapproved means refused, never defaulted to True** | `test_pipeline.py::test_a_dangerous_step_does_not_run_unapproved` |
| **Safe steps get no checkbox** — only DANGEROUS/CRITICAL render one in the TUI | `test_tui.py::test_a_safe_plan_gets_no_checkbox` |
| **Telemetry can never break a run** — every `telemetry/` method catches its own exceptions | `test_pipeline.py` |
| **An undo that can't be proven is not "undone"** — three states (`undone`/`unverified`/`failed`), never collapsed to two | `test_gates.py::TestVerifiedRollback` |
| **A declared `rollback:` command is still classified** — a step's own `safe: true` can only make `SafetyClassifier` more cautious, never override it | `test_pipeline.py::test_a_declared_safe_rollback_is_still_classified` |
| **`execute()` cannot replay an already-executed result** — `executed` is set only immediately before a real `step.run()`, never for merely entering the loop | `test_pipeline.py::test_execute_twice_on_the_same_result_does_not_replay_it` + 3 more |
| **Model size/chat-capability come from the daemon (`/api/show`), never guessed from the name** | `test_core.py::TestModelInfo`, `test_gates.py::TestModelParamsAndChatCapable` |
| **Every way to decline a plan goes through `Harness.reject()`** — never `execute()` with empty approvals, never bare UI state | `test_pipeline.py::test_reject_never_runs_an_all_safe_plan` + 4 more |

---

## OS-Engineering Guardrails — what "the domain is real machines" actually requires

These are not generic good-practice items; each exists because a wrong guess here means a
wrong command runs against a real filesystem, package manager, or init system.

- **Command resolution must match how a real shell actually resolves the command**, not how a
  naive pattern-match reads the text on the page. `SafetyClassifier` unwraps `sudo`, `env`,
  `timeout`, `nohup`, `nice`, `xargs`; recurses into `sh -c` payloads; decodes base64 blobs
  piped to a shell; and splits on quotes correctly. Plain regex over raw text measured 5 of 12
  evasions missed in testing — treat that number as the bar a change to this area must clear
  (currently 20/20 caught, 0 false positives; re-run `tests/test_gates.py::TestSafety` and
  don't let it regress). If you're adding a new wrapper or shell construct to recognize, add
  the evasion case to that test corpus in the same change — an unlisted evasion is
  indistinguishable from "handled" until someone hits it live.
- **systemd-scope assumptions must be checked against the real unit, not assumed.**
  `AUDIT_FINDINGS.md` MED-2: service handling assumed `--user` scope everywhere; wrong for a
  system service. On this exact machine, `pop-os`'s `ollama.service` itself is a live
  cautionary example of scope mattering — it runs system-wide, and a `--user`-scoped
  assumption anywhere near it would silently check or act on the wrong unit. When code decides
  how to query or act on a systemd unit, verify scope explicitly; don't default to whichever
  scope was convenient to write first.
- **OS domain knowledge fed to the planning model grows from a measured gap, never a guess.**
  `docs/*.md` exists only for facts a real benchmark run (`scripts/dev/model_bench/`) caught a
  model getting wrong, vague, or hallucinated (see `docs/README.md`) — not from "this seems
  like something a Pop!_OS agent should know." A docs file written from guesswork is the same
  failure mode as an untested gate: it looks like coverage and isn't. Add a themed file only
  behind a benchmark result, and update `docs/README.md`'s table in the same change. Wiring
  any of `docs/` into `harness.agent.SYSTEM_PROMPT` is its own reviewed change with its own
  test — never bundle it into "just adding a docs file."
- **Model routing consults the live daemon before it guesses.** `ModelRouter` and
  `Accelerator.fits` use real `/api/show` data (params, `capabilities`) when available; the
  name-based guess is the fallback for when the daemon can't be reached, never the primary
  path. Found live: `qwen3.5` and `deepseek-r1-heavy` guessed as 0B from the name (no digit),
  and `nomic-embed-text` — an embedding-only model — was routed to as a chat target by the old
  "smallest guessed size wins" downgrade path. If you touch routing or the VRAM-starved
  fallback, keep the daemon-first order; a name-based shortcut that "usually works" is exactly
  how this broke the first time.
- **A destructive/irreversible action never runs without an explicit human tick, on every
  path that can decline or execute a plan** — TUI, CLI, and direct API use all go through the
  same `Harness.execute`/`Harness.reject` choke point. A new UI affordance or entry point that
  bypasses either is a second, untested code path for the exact thing the gates exist to
  cover — see HANDOFF.md §2.8/§2.10 for two real bugs of exactly this shape found in a
  full-file audit.

---

## When a check fails: fix the CAUSE, not the check

The rule is AGENT_DOCTRINE.md's. This repo's guards: the 785-test suite, `ruff`/`mypy`, the
defensive-coding scorecard, the evasion corpus in `test_gates.py::TestSafety`, the clarifier's
ambiguity corpus. The ❌ path has a named precedent here — tuning the clarifier against its own
test corpus instead of a fresh one (`HANDOFF.md` §4 calls this out explicitly as a mistake already
made once). For anything on the safety-critical path (any of the five gates,
`Step.command`/`Step.run`, `execute`/`reject`), the "is this guard actually wrong" call isn't
yours to make unilaterally: surface it to the user.

---

## Code Conventions

(Full rationale: `DEFENSIVE_CODING_STANDARDS.md` §1. Base rules — no comments unless WHY,
one-line docstrings, module `Depends on:` line, CCN<15 ceiling/aim≤10, one test per new module —
are AGENT_DOCTRINE.md's; this section is the harness-specific rest.)

- Line length: 100 (ruff `E501` ignored — don't fight it manually).
- **Named, deliberate CCN exceptions:** `SafetyClassifier._judge` (CCN 46), `Clarifier.detect`
  (CCN 26), and `Harness.execute` (CCN 29) — flat dispatch/guard-clause/orchestration functions
  that read *worse* fragmented. Don't split those to move a number; do split anything that's
  actually tangled.
- Typing: full param + return hints on new/edited code; `Any` for genuinely untyped externals,
  never as a shortcut around a knowable type; `X | None` whenever a real caller can pass `None`.
- Imports: ruff isort, first-party = `harness`.

---

## Source-of-Truth Registry

| Fact class | Canonical owner | Verified by |
|---|---|---|
| Safety/execution invariants | `HANDOFF.md` §2 | the specific test(s) each entry names |
| Default config *values* (informational only) | `CORE_RULES.yaml` | **nothing — known to drift, don't trust it** |
| Default config *behavior* (what actually runs) | the module `CORE_RULES.yaml`'s header points to (`pipeline.py`, `gates/*.py`, `core/*.py`) | the test suite for that module |
| What each package/file does | that directory's `README.md` | *(trust-based — no doc-audit test yet; keep honest by hand)* |
| Code quality grades | `DEFENSIVE_CODING_STANDARDS.md` §2.1 baseline | `scripts/dev/defensive_coding_report.py --enforce` in CI |
| Known live-wire bugs / unfinished work | `AUDIT_FINDINGS.md`, `HANDOFF.md` §6 | *(trust-based; update when fixed, don't leave it stale)* |
| OS domain knowledge for the planning model | `docs/*.md` | `scripts/dev/model_bench/` benchmark results (`REPORT.md`) |
| Model catalog / routing rules | `harness/gates/router.py` (`ROUTING_RULES`), not `CORE_RULES.yaml` | `tests/test_gates.py::TestRouting` |
| Contract summary (entry points, outcomes, stages, exit codes) | `HANDOFF.md` §8 | manual — cross-check against `pipeline.py`/`cli.py` if it looks stale |

---

## Task Playbooks

### Extending command classification (a new evasion, a new wrapper)
1. Add the case to `tests/test_gates.py::TestSafety`'s corpus first — this is benchmark-driven
   work, same discipline as `docs/`: measure the gap before closing it.
2. Implement the resolution logic in `harness/gates/safety.py`.
3. Confirm 20/20 (or the new total) still passes with 0 false positives on the routine-command set.
4. Update `HANDOFF.md` §2.1 only if the change affects the `Step.command`/`Step.run` equivalence.

### Adding a new gate or extending an existing one
1. Read `harness/gates/README.md` and the relevant `HANDOFF.md` §2 entries first.
2. Ask before changing behavior on the safety-critical path (see When to Ask vs. Act).
3. Wire it through `pipeline.py`'s seven stages — a gate that doesn't run inside
   `Harness.execute`'s existing stage sequence is a second code path (see HANDOFF §7, question 2).
4. Add the test, add the `HANDOFF.md` §2 entry, update `gates/README.md`.

### Adding OS domain knowledge to `docs/`
1. Run or point to a `scripts/dev/model_bench/` benchmark showing the actual gap (wrong, vague,
   or hallucinated) — not a guess.
2. New theme → new file; don't grow an existing one past its natural topic boundary.
3. Update `docs/README.md`'s table, including the "Measured gap?" column, in the same change.
4. Do not wire it into `harness.agent.SYSTEM_PROMPT` in this same change — that's a separate,
   reviewed change with its own test.

### Fixing a live-wire bug (`AUDIT_FINDINGS.md` pattern)
1. Reproduce the finding, don't take the writeup on faith — code drifts between when a finding
   is written and when it's picked up.
2. Fix the cause; write the regression test that would have caught it.
3. Remove (don't just leave stale) the finding's entry in `AUDIT_FINDINGS.md` once fixed, or
   mark it resolved with the commit — a findings doc that never shrinks stops being read.
4. If the fix touches an existing `HANDOFF.md` §2 invariant, update that entry.

### Before adding any feature (from `HANDOFF.md` §7 — ask in this order)
1. Does an existing gate already cover this?
2. Does this add a second code path (a way to do something the model path can't, or vice versa)?
3. Can you measure it before you build it, the way the clarifier corpus was built first and
   the routing benchmark exists before routing rules were trusted?
4. Would this add an item to an open-ended "list of things to detect"? (See `HANDOFF.md` §3 for
   what's already been tried and failed at that shape.)

If a change survives all four: add its test under the `tests/` subsystem it belongs to, not a
new top-level phase file — the old six-phase layout is exactly the structure this package was
consolidated out of.

---

## Tool Use Patterns

- **Grep before Read** — if looking for a symbol, grep first to find the file.
- **Test with injected callables, never a live daemon.** Nothing in `tests/` should require a
  running Ollama or network access — that's what keeps CI trustworthy. A handful of tests do
  call real `systemctl`/`dpkg-query`/inotify against whatever unit or package set happens to be
  on the machine; that's fine, since those binaries and kernel facilities are always present on
  any Linux runner, unlike Ollama or the network, which are not. `conftest.py::scripted()` is
  the pattern for a multi-turn conversation without a real backend; extend it rather than
  reaching for a live call in a test.
- **Concurrent sessions share this working tree.** See AGENT_DOCTRINE.md's "Concurrent sessions
  share this working tree" — no repo-specific wrinkle here.
- **Never let `CORE_RULES.yaml` numbers substitute for reading the code** — see the Area Maps
  section above.

---

## When to Ask vs. Act

**Ask first:**
- Anything on the safety-critical path: the five gates, `SafetyClassifier`, `Step.command`/
  `Step.run` construction, `Harness.execute`/`Harness.reject`.
- Schema/format changes to persisted state (`resume/*.json`, `telemetry/tasks.jsonl`,
  `audit/*.audit.json`).
- Deleting files or branches.
- Anything that changes what happens without an explicit human approval step.

**Act freely:**
- Reading any file.
- Editing tests.
- Fixing lint/type errors.
- Adding config that's additive and gated behind a flag/default preserving old behavior.
- Peripheral-tier files (`cli.py`, `tui.py` glue, `__init__.py`) — see
  `DEFENSIVE_CODING_STANDARDS.md` §0 for the full tier list.

---

## Key Entry Points

| Task | Command |
|---|---|
| Tests (all, must stay 785) | `python3 -m pytest tests/ -q` |
| Tests (one file/case) | `python3 -m pytest tests/test_gates.py -k <name>` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Type check | `mypy harness/` |
| Defensive-coding scorecard | `python3 scripts/dev/defensive_coding_report.py` (`--area <pkg>` · `--coverage` · `--verbose`) |
| Environment check | `harness doctor` |
| One-shot request | `harness ask "..."` |
| Plan only, run nothing | `harness plan "..."` |
| Run a hand-written task file (no model) | `harness run task.yaml` |
| Classify a task file without running it | `harness check task.yaml` |
| What the harness has recorded about itself | `harness evidence` |
| Model-quality benchmark | `scripts/dev/model_bench/bench.py` (see its `README.md`) |

---

## Memory

See AGENT_DOCTRINE.md. Project memory lives in
`~/.claude/projects/-mnt-ParamoStorage-harness/memory/` — the same rule this file applies to
`CORE_RULES.yaml` applies to memory: a description can drift from the system it describes.
