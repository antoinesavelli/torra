# torra: status

Single owner of dynamic status. Edit in place; nothing else restates it. Last updated 2026-10-25.

**Phase: built, in use on the owner's workstation, in progress.** 209 commits on `main` since 2026-07-28.
Moved to the corsu layout and standard on 2026-10-25 (`docs/DECISIONS.md` D-008 to D-013); those commits are
local and not pushed (the push is the owner's call, Phase C of the migration plan).

## Where it stands

A program the command classifier does not recognise
needs a tick. Three shell shapes that still ran such a program with no tick
(deeply nested `sh -c` or `$(...)`, `hash -p`, and an exported `PATH`,
`LD_PRELOAD` or `BASH_ENV`) were found on 2026-10-08 and closed on 2026-10-09,
each with a test (`HANDOFF.md` §2.75). The Phase 5 source read (`GRADE_TO_A_PLAN.md`)
found more of that shape (`read PATH`, `printf -v PATH`, namerefs) and `hostname newbox`
running unticked; closed on 2026-10-23 (§2.83, §2.84). Its blind red-team has not
run; until it does, treat the classifier as unfinished.
The plan checks added on 2026-10-11 and 2026-10-12 (`HANDOFF.md` §2.78-2.81) have
met real models only in plan-only runs. All of it was pushed on 2026-10-13.

## Code health

Measured 2026-10-25 at commit `57a4f04`, from the repo root with `uv run`.

| Result | Measured how |
|---|---|
| 1921 tests, all passing | `uv run pytest -q` |
| `ruff check`, `ruff format --check` and `mypy --strict` clean | `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` |
| Branch coverage 98% overall (floor 96) and 99% on the safety path (floor 97) | `.github/workflows/ci.yml`'s two `coverage report` lines |
| Defensive-coding scorecard A (GPA 3.83), with 0 decision-path files over 500 functional LOC | `uv run python scripts/dev/defensive_coding_report.py --verbose --enforce` |

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
reproducible; see `HANDOFF.md` §4 ("What was here before") for the honest
accounting. This project's own standard is that an unreproducible citation
isn't one, so those numbers are no longer claimed here at all.

## Known gaps

1. **Model judgement is measured now, and the first real finding is still
   open.** The gates are tested against fixed corpora; tests still use a scripted
   backend and prove nothing about judgement on their own. But real traffic
   now flows through telemetry and gets hand-labeled (`torra label`), and
   that measurement found the model stops and asks for only ~18% of requests
   a human calls genuinely ambiguous (82% live false-negative rate against a
   0% synthetic baseline). Whether a routing fix already found along the way
   closes that gap isn't confirmed yet -- too little traffic has run since.
   One related failure shape is fixed: a question torra could answer
   itself with a read-only command (`lsblk`, `systemctl --failed`, ...)
   used to get deflected back to "please run that yourself" instead of the
   torra just running it -- live A/B tested against the real model before
   shipping. See `HANDOFF.md` §6.1 and §2.40.
2. **Real traffic exists, and it already found two real bugs.** Recorded
   tasks are hand-labeled (`torra label`), and that
   loop has already surfaced a pipefail/SIGPIPE bug and a VRAM-routing bug
   that reading the code alone hadn't caught. See `HANDOFF.md` §6.2.
3. **Undo coverage ceiling is narrower than it looks.** `dd`/`mkfs` writing
   to a regular file is already covered automatically, the same as any other
   command, as long as the file is declared in `touches:`. Only `dd`/`mkfs`
   against a **raw device** and database migrations need a hand-written
   `rollback:` entry -- there's no prior on-disk state to snapshot for
   either. The plan pane names which steps those are. See `HANDOFF.md` §6.3.
4. **Tree watching misses a same-size same-mtime edit.** The declared-path
   SHA256 layer catches it, so `touches:` still matters.
5. **You tick before the plan's own checks run.** Every tick is collected
   before step 1, so in a "list it, then delete it" plan the listing cannot
   inform the tick. The prompt now says what each target is; asking at the
   moment each step runs is an open decision (`GRADE_TO_A_PLAN.md` D8).
6. **A read-only probe can need a tick.** `cat /var/run/reboot-required`
   rates DANGEROUS because the regex backstop matches "reboot" in the file
   name. It errs toward asking. See `HANDOFF.md` §6.5.

## Open work

One owner per item, as `docs/agents/CONVENTIONS.md` registers it; this page only points.

| Where | What |
|---|---|
| `GRADE_TO_A_PLAN.md` | Phase 5 findings P5.1 to P5.3, owner decision D8, the blind red-team |
| `TODO.md` | the planner/verifier prototype checklist (`scripts/dev/model_bench/planner_verifier.py`) |
| `HANDOFF.md` §6 | unfinished work with its story |
| `CLAUDE_MIGRATION_ROADMAP.md` | buckets 2 to 4 of moving the operator role off Claude Code (parked: `docs/DECISIONS.md`) |

## Repo metadata (drafts for the owner's approval, applied in Phase C)

GitHub About (= `pyproject.toml` `description`): see that file.
GitHub topics: `ollama`, `local-llm`, `linux`, `textual`, `ai-agents`, `human-in-the-loop`.
