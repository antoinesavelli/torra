# torra: status

Single owner of dynamic status. Edit in place; nothing else restates it. Last updated 2026-11-19.

**Phase: built, in use on the owner's workstation, in progress.** 209 commits on `main` since 2026-07-28.
Moved to the corsu layout and standard on 2026-10-25 (`docs/DECISIONS.md` D-008 to D-013); those commits are
local and not pushed (the push is the owner's call, Phase C of the migration plan). On 2026-10-27 the old doc
system retired onto that standard (D-014): `HANDOFF.md` is gone, its invariants and live wires are
`docs/INVARIANTS.md` and `docs/LIVE_WIRES.md`, and the repo root holds only `README.md`, `AGENTS.md`,
`CLAUDE.md` and `LICENSE`.

## Where it stands

A program the command classifier does not recognise
needs a tick. Three shell shapes that still ran such a program with no tick
(deeply nested `sh -c` or `$(...)`, `hash -p`, and an exported `PATH`,
`LD_PRELOAD` or `BASH_ENV`) were found on 2026-10-08 and closed on 2026-10-09,
each with a test (`docs/INVARIANTS.md` §2.75). The Phase 5 source read (`docs/plans/GRADE_TO_A_PLAN.md`)
found more of that shape (`read PATH`, `printf -v PATH`, namerefs) and `hostname newbox`
running unticked; closed on 2026-10-23 (`docs/INVARIANTS.md` §2.83, §2.84). Its blind red-team ran on
2026-11-17: 30 commands written by a model that read no repo code, all 30 need a tick
(14 CRITICAL, 16 DANGEROUS, `docs/plans/GRADE_TO_A_PLAN.md` Phase 5). The re-grade ran on 2026-11-18 at `386f2b8`: A- (90, up from B+ 87), exit not met; one new major (P5.5, write and run options of listed programs such as `curl -w '%output{...}'`, `awk -W exec` and `tar --index-file` ran unticked). P5.5 is fixed (`docs/INVARIANTS.md` §2.93, `docs/plans/P5_5_OPTION_ALLOWLIST_PLAN.md`) and awaits the next re-grade.
The plan checks added on 2026-10-11 and 2026-10-12 (`docs/INVARIANTS.md` §2.78-2.81) have
met real models only in plan-only runs. All of it was pushed on 2026-10-13.

## Code health

Measured 2026-10-25 at commit `57a4f04`, from the repo root with `uv run`.

| Result | Measured how |
|---|---|
| 1921 tests, all passing | `uv run pytest -q` |
| `ruff check`, `ruff format --check` and `mypy --strict` clean | `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` |
| Branch coverage 98% overall (floor 96) and 99% on the safety path (floor 97) | `.github/workflows/ci.yml`'s two `coverage report` lines |
| Defensive-coding scorecard strong (band mean 3.83), with 0 decision-path files over 500 functional LOC | `uv run python scripts/dev/defensive_coding_report.py --verbose --enforce` |

The tree was formatted on 2026-10-25 (`ruff format`, `*.md` excluded) after five modules that formatting
would have pushed past 500 functional LOC were split by cohesion with no change to their code:
`gates/safety.py`, `gates/known_programs.py`, `gates/undo.py`, `pipeline.py` and `agent.py`. Each public
import path is kept (`tests/test_module_splits.py`), and each class now inherits the part it was cut from.
Largest decision-path file after formatting: `core/machine.py` at 486.

## Where the numbers come from

All run on 2026-10-12 at commit `f9c0cd6`.

| Result | Measured how |
|---|---|
| 1650 tests, all passing | `python3 -m pytest tests/ -q` |
| 31 of 31 evasion cases classified DANGEROUS or CRITICAL; 0 of 31 routine commands needed a tick | `tests/test_gates.py::TestSafety` |
| 150 commands that must ask (55 unknown or unproven, 9 critical, 70 from a source-reading review, 16 from the 2026-10-08 probe) all ask; 138 read-only or routine commands run unticked | `tests/test_known_programs.py` |
| Clarifier flagged 18 of 18 ambiguous requests and 1 of 10 specific ones | `tests/test_gates.py::TestClarifier` |
| On the 183 steps of the 121 past plans in the owner's `~/.torra/audit`: 11 need a tick (6%; 7 did when they ran); the syntax check flags 2 steps, both broken; the missing-path check flags 5 plans (4%); the placeholder check flags 1 step, a real slot | Replaying those plans through the current checks, no model runs, nothing executed |
| Eight plan-only runs on `devstral:24b` and `gpt-oss:20b`: the schema repair, the syntax check and the missing-path check each fired correctly at least once; the placeholder refusal was not triggered | Live, against the real models, in a scratch workdir, nothing executed (6 runs at `3aae981`, 2 at `f9c0cd6`) |

The first four rows came from data generated in testing, not from real use.
The last two touch real traffic, but it is one person's machine and a few
hundred tasks. That is the single largest caveat on this whole project and it
is not resolved.

The six-phase prototype this package was consolidated from (`legacy/`) has
been removed from the repository. A handful of numbers previously cited here
— a 5,143x tree-watching speedup, checkpoint-size reduction, bytes-per-logged-task
— were measured only against that removed code and are no longer independently
reproducible. This project's own standard is that an unreproducible citation
isn't one, so those numbers are no longer claimed here at all.
The clarifier's 0% false-negative figure was likewise measured against a genuinely blind third corpus that was
removed with `legacy/`; `tests/test_gates.py::TestClarifier` asserts 0% only against its own corpus, so treat the
blind-corpus claim as historical.

## Open gaps

1. **Model judgement on real traffic:** gap 6.1.
2. **Real traffic and the label-and-measure loop:** gap 6.2.
3. **Undo coverage ceiling:** gap 6.3.
4. **Tree watching misses a same-size same-mtime edit.** The declared-path
   SHA256 layer catches it, so `touches:` still matters.
5. **You tick before the plan's own checks run.** Every tick is collected
   before step 1, so in a "list it, then delete it" plan the listing cannot
   inform the tick. The prompt now says what each target is; asking at the
   moment each step runs is an open decision (`docs/plans/GRADE_TO_A_PLAN.md` D8).

### 6.1 The clarifier misses most genuinely ambiguous requests

Open: on real, hand-labeled traffic the model stops and asks for only 18% of requests a human calls ambiguous
(82% live false negatives, 39 occurrences) against a 0% synthetic baseline; whether the VRAM-eviction-credit routing
fix (`docs/INVARIANTS.md §2.35`) closes it is unanswered, because 38 of the 39 were logged before that fix landed
(2026-08-27 10:30) and one has been recorded since (correctly blocked; n=1 proves nothing).

Why it matters: the gates are tested on fixed corpora and tests use a scripted backend, so only real traffic
measures model judgement. As of that snapshot (143 recorded tasks, 29 `plan`-intent): the first proposal needed no
repair in 12 of 29 plan turns, the repair loop rescued 11 of 29, and a human rejected 2 plans outright. Next step:
more post-fix traffic, `torra label`, then `torra evidence`. If the gap persists the fix is prompt and
model-judgment work, not more gates, and not a guess at new clarifier anchors (R-2, R-3 in `docs/DECISIONS.md`).
Two attempts are recorded in `docs/INVARIANTS.md §2.40` (the repair-loop check reverted, R-9; the shipped rule-6
wording fix, live A/B tested, for a read-only diagnostic such as `lsblk` deflected back to the operator): read them
before a third.

Pointers: `tests/test_pipeline.py::test_every_finished_turn_is_recorded_to_telemetry`,
`tests/test_telemetry.py::TestDrift` (`docs/INVARIANTS.md §2.34`); the synthetic-corpus baseline is
`tests/test_gates.py::TestClarifier::test_zero_false_negatives` and `::test_low_false_positives`.

### 6.2 Keep collecting real traffic after each fix

Open: the label-and-measure loop runs, but each fix needs its own post-fix traffic.

Why it matters: 143 real tasks (about a month, CLI/TUI/OpenRouter), 91 hand-labeled, already found the
pipefail/SIGPIPE-forgiveness gap (`docs/INVARIANTS.md §2.36`) and the VRAM-eviction-credit routing bug (`§2.35`).
`drift()` averages fixed and still-broken traffic into one number (gap 6.1) until enough post-fix tasks exist to
separate them. The 2026-10-11/12 replay and live plan-only runs are under "Where the numbers come from".

### 6.3 Undo has no automatic coverage for raw devices and migrations

Open: `dd`/`mkfs` against a raw device and database migrations need a hand-written `rollback:`.

Why it matters: a regular file written by `dd`/`mkfs` is already covered (`touches:` is snapshotted whichever command
writes it; verified live, and `SYSTEM_PROMPT` rule 7 now says so). A raw device has no prior on-disk state a userspace
snapshot could restore, and the plan pane names which steps those are. Migrations are deliberately left to a
human-reviewed `rollback:` (R-11 in `docs/DECISIONS.md`): this is a boundary, not a TODO.

Pointers: `tests/test_gates.py::TestUndoGeneration::test_dd_against_a_regular_declared_file_is_already_covered`,
`test_it_invents_nothing_it_cannot_invert`.

### 6.5 Smaller known items

Open, each small.

- **Rename backups.** `harness-pre-torra-rename.bundle` and `dot-harness-pre-torra.tgz` were in `/mnt/data/archive/`;
  that directory was not found on 2026-10-27, so they may already be gone. Delete only on the owner's say-so.
- **Corsican spelling.** Have a native speaker confirm the spelling of *torra* (the Genoese coastal watchtower; the
  meaning is certain, the spelling is not).
- **Prescreen cost.** Found 2026-10-12, left as is by the owner: the "`request` line is underspecified" prescreen
  complaint spent 2 of 3 model calls on a correct read-only plan (`ps aux --sort=-%mem`).
- **Missing-path check is a heuristic** (`docs/INVARIANTS.md §2.80`): it misses paths built at run time and could
  flag a plan that creates a path in a way it does not recognise (one wasted repair round and a warning).
- **Blind-corpus clarifier false positive** (`run nvidia-smi and report the free VRAM in MB`): left unfixed
  deliberately so one unoptimised measurement survives.
- **ROCm VRAM is never read.** `detect_accelerator()` (`torra/core/machine.py`) detects presence only, so
  `Accelerator.gpu` is always `False` on ROCm and the router caps at `CPU_PARAM_CEILING_B` (9B). Fix only against
  real `rocm-smi` output (its columns changed across ROCm versions and no AMD hardware is in the target
  environment); a guessed parser would report a fabricated VRAM figure.
- **Undo states, doc vs code (found 2026-10-13, doc or code).** `AGENTS.md` says an undo has three states and
  `undone` needs the verifier to return True; the code also has `cancelled` and returns `undone` with
  `verified=None` when no verifier was supplied, which `RollbackReport.ok` counts as success
  (`torra/gates/rollback.py`). `docs/INVARIANTS.md §2.6` states what the code does.
- **Model size falls back to a name guess (found 2026-10-13).** `AGENTS.md` says size is never guessed from the
  name; `ModelRouter._params_b` does when `/api/show` has nothing (chat capability never does).
  `docs/INVARIANTS.md §2.9` states what the code does.
- **Shadow copy of a non-regular file, and a `touches` path torra cannot stat:** closed 2026-11-19
  (`docs/LIVE_WIRES.md` §5.26). Found while closing it, still open: a dangling symlink or a loop is snapshotted as
  absent, so its undo deletes the link; `remote_edit` reads a `remote_push` step's `reads` file with no regular-file
  or size check (after approval).
- **`cat /secret.env` is SAFE (found 2026-10-14, security call).** Only a file named exactly `.env` counts as a
  credential read; re-checked 2026-10-27, still SAFE.
- **`unzip` false positive (found 2026-10-14).** As a `known_programs` writer, `unzip /home/user/archive.zip -d /tmp`
  is DANGEROUS because every operand counts as a destination (`docs/INVARIANTS.md §2.54`); re-checked 2026-10-27.
- **Stale comment.** `torra/gates/router.py:92` says `muse-glimmer:30b` is still pulled; it is not (`ollama list`).
  Comment only, on the gates path, so left for the owner.
- **An unusable model reply gets no repair round** (`docs/INVARIANTS.md §2.67`, `§2.73`, `§2.74`, `§2.78`). A
  task that fails validation gets one (`§2.78`); a reply with no JSON or no task object is reported, not repaired.
  A feature, not a defect: `agent.py` has no room for it without a split.
- **A user `trap ... ERR` or `set -e` inside a step defeats the error wrapper** (`docs/INVARIANTS.md §2.61`,
  `§2.71`, `§2.74`).
- **`begin_step` swallows a failed save of the resume file** (`docs/INVARIANTS.md §2.71`, `§2.74`;
  `_save_quietly` in `torra/core/resume.py`), so a crash may leave no in-progress mark. The digest-covers-only-the-
  command-text part stays open (`docs/INVARIANTS.md §2.64`).
- **An unredirected `cmd &` holds the output pipes open until the timeout** (`docs/INVARIANTS.md §2.71`, `§2.74`).
- **Ctrl-C between steps is not caught** (`docs/INVARIANTS.md §2.74`); Ctrl-C inside a step is (`§2.61`).
- **Redaction is pattern-based** (`docs/INVARIANTS.md §2.74`): a secret in a shape no pattern knows still leaves.
- **Classifier gaps left open** (`docs/INVARIANTS.md §2.72`):
  - `pytest` and `pip install` run arbitrary code and stay CAUTION. By design.
  - A relative path after a `cd` into a non-system directory is not tracked.
  - Program-specific format strings are not parsed in general. tmux's `#(...)` is closed (`docs/INVARIANTS.md
    §2.92`, 2026-10-27); `#{E:...}`/`#{T:...}`, which re-expand an option or a pane title as a format, stay SAFE.
  - Case and symlink spellings of a protected path rely on `_protected`'s own normalisation.
- **Option-judge gaps left open** (`docs/INVARIANTS.md` §2.93; found in P5.5, all but the `sed -i` item pre-existing):
  - argv keeps an unquoted redirect word and loses its quoting, so `date >> f` and `date '>>' f` look alike; the fix
    belongs in the resolver.
  - An unresolved `$VAR` or `$(...)` word in argv can expand to an option (`curl ${O:--o} F URL` writes `F`).
  - `awk_program_is_safe` misses a redirect after a regex holding `;` and gawk `@include`.
  - An over-tick P5.5 introduced: `sed -i` with `a`/`i` text or `{}` blocks went from CAUTION to asking (10 real
    commands, a few with `r FILE`, which asks by design); widening `script_rules` would free the rest.
  - `HOME=` and `XDG_CONFIG_HOME=` choose curl's `.curlrc`; chained relative tar `-C`; curl to non-HTTP schemes and local data in a URL;
    `RIPGREP_CONFIG_PATH` and `SYSTEMD_PAGER` are not in `RISKY_ENV`.
  - `python3 -c` with a payload of about 7,000 chained unary minus signs raises `MemoryError` in `_pure_python`
    (`program_checks.py`); only `SyntaxError` and `ValueError` are caught.
- **Heredoc bodies are scanned for redirects** (`docs/INVARIANTS.md §2.87`): an over-tick. Re-checked 2026-10-27:
  `cat <<EOF` with `echo a > /etc/x` in the body is CRITICAL.
- **Remote edits** (`docs/INVARIANTS.md §2.73`): `torra check` classifies a remote step's ignored `run` text instead
  of its real verdict (`cmd_check` in `torra/cli.py` still calls `classify(command)` on it); the pull-edit-push
  pairing the prompt asks for is not enforced.
- **`router.available` is not re-read after a step that pulls or removes a model** (`docs/INVARIANTS.md §2.73`;
  `Agent.observe` refreshes it only on request or when empty).
- **The tick preview is a snapshot** (`docs/INVARIANTS.md §2.81`): it describes the paths written in the plan, not
  what an earlier step will find, and is not a live view once first shown.
- **Model-bench injection scenario** (`docs/INVARIANTS.md §2.66`): not done, needs a live model run; owned by the
  P3.3 row under Open work.

### 6.6 A stopped run's resume point: closed 2026-11-19

A point more than 24h from now is ignored, and the plan says what a resume will skip or stop on
(`docs/INVARIANTS.md §2.64`, `docs/DECISIONS.md` D-017). Left open: every save re-stamps `at`, so a task that fails
at least once a day keeps trusting its first day's completed steps (a first-written time is a new field, a format
change); expired files are not swept (each is inert, and the next run of its task id overwrites it); the plan's
line is a snapshot, so a file left between plan and Run, or a clock skew at the 24h edge, is not announced.

## Open work

One owner per item, as `docs/agents/CONVENTIONS.md` registers it: the table points at other owners; the checklists below are owned here.

| Where | What |
|---|---|
| `docs/plans/GRADE_TO_A_PLAN.md` | owner decision D8, the P3.3 model-bench scenario (the Phase 5 blind red-team ran 2026-11-17: 30 of 30 asked) |
| `docs/plans/CLAUDE_MIGRATION_ROADMAP.md` | buckets 2 to 4 of moving the operator role off Claude Code (parked: `docs/DECISIONS.md`) |

### Planner/verifier prototype (`scripts/dev/model_bench/planner_verifier.py`)

State as of 2026-10-14: 9b planner (`qwen3.5:9b`) with a 24b verifier (`devstral:24b`); structured
findings, veto decided in code. One run: fixed set 7/7 good passed and 2/2 bad blocked, live clean
plans 1/8 vetoed, planted path caught 5/8. The git-recoverable exemption was added after the fixed
set showed a wrong veto, so that 7/7 is partly tuned. Committed 2026-10-21 (`61b6886`).

- [ ] Repeat `--fixed-only` 3 to 5 times and report the spread of the 7/7 and 2/2.
- [ ] The `invented_specific` fix for description-only quotes (`executable_text`, pinned by
      `--self-check`) is not yet re-measured on a live run.
- [ ] Look at the 3 missed planted-path controls (docker-prune, install-and-enable, timer); two
      reviews timed out or named other things.
- [ ] Add a short note on the swapped roles, structured findings and fixed set to
      `scripts/dev/model_bench/REPORT.md`, replacing "Torra has no LLM-based verifier today".

### Both projects on the workstation

- [ ] Optional: set `OLLAMA_MAX_LOADED_MODELS=2` (`sudo systemctl edit ollama`, then restart) so a
      24b and an 8b stay resident together (about 20 GB of the 4090's 24 GB). Not needed for the
      prototype, which plans every task first and verifies afterwards. Restart only when no
      pruvera trial is running.

### pruvera (after the live trial finishes)

- [ ] A per-role model through the sandbox's inference filter, so a 24b can supervise an 8b in a
      trial. This is a harness change: pre-register it through `bench.experiment`, land it per the
      bench-edits memory (git-archive copy, one guarded commit), and check whether it widens what a
      local model may do unsupervised (the rule-change gate).

### Executor (8 GB) as the 8b planner host

- 2026-10-15: `bench.make_metrics_transport` hardcoded the workstation host, so an earlier "executor"
  run never reached executor (the 404s were `llama3.1:8b` missing on the workstation); fixed with a
  `host` argument. First real run, `llama3.1:8b` on executor with `devstral:24b` on the workstation:
  10 plans, 5 vetoed (most on names the 8b planner invented, e.g. `postgresql-13`, `/srv/api/.env`),
  planted path caught as a blocker in 2 of 10 (the verifier usually filed it as `style`, minor).
  One run, and `planner_verifier.py` was being edited by the other chips during it.
- [ ] Re-run once the chips land, and check the vetoed plans by hand: are the invented names real
      planner hallucinations (then the vetoes are right) or defaults the check should allow.
- [ ] Decide what to do about the verifier calling a planted path `style`: promote by quote check
      regardless of kind, or leave it.
- [ ] Executor has only `hermes3:8b` and `llama3.1:8b`; try `hermes3:8b` as the planner too.
- [ ] `bin/executor-ollama-tunnel down` when finished; it is currently up.

## Repo metadata (drafts for the owner's approval, applied in Phase C)

GitHub About (= `pyproject.toml` `description`): see that file.
GitHub topics: `ollama`, `local-llm`, `linux`, `textual`, `ai-agents`, `human-in-the-loop`.
