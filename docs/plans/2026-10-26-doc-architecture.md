# Plan: torra's docs onto the corsu standard

Status: done
Owner decisions pending: none
Last verified: a29ec97 on 2026-10-27 (Phase 1 in 630f633; Phase 2 in the commit after a29ec97)
Owner approved the plan as a whole on 2026-10-26 · Executor: an Opus orchestrator session

## Goal

torra keeps two doc systems at once: the corsu standard (`README.md`, `AGENTS.md`, `docs/DESIGN.md`,
`docs/DECISIONS.md`, `docs/STATUS.md`, `docs/agents/`, per
`/mnt/ParamoStorage/system-library/docs/agents/NEW_REPO.md`) and the older system it was meant to replace: a
305 KB append-only `HANDOFF.md` plus ten other markdown files at the repo root. Done means the root holds only
`README.md`, `AGENTS.md`, `CLAUDE.md` and `LICENSE`. Every fact then has one owner under `docs/`, `HANDOFF.md`
is gone, and no guard is weaker than it is today.

## Target layout

```
README.md  AGENTS.md  CLAUDE.md  LICENSE
docs/
  DESIGN.md  DECISIONS.md  STATUS.md      corsu standard (STATUS absorbs TODO and HANDOFF §6)
  INVARIANTS.md                           HANDOFF §2, condensed
  LIVE_WIRES.md                           HANDOFF §5, condensed
  USAGE.md                                the command/task-file/routing reference from README
  agents/                                 + DEFENSIVE_CODING_STANDARDS.md
  plans/                                  GRADE_TO_A_PLAN.md, CLAUDE_MIGRATION_ROADMAP.md, this file
  reports/                                AUDIT_LOG.md, DEFENSIVE_SCORECARD_LOG.md (routine-written)
  os/                                     boot_and_recovery, package_management, desktop_environment,
                                          graphics, README (the planning-model audience, D-007)
```

## Decisions (made; do not re-decide)

1. **Section numbers survive.** `INVARIANTS.md` keeps headings `### 2.N <title>` and `LIVE_WIRES.md` keeps
   `### 5.N <title>`, so the 49 code/test files citing `HANDOFF.md §2.N` / `§5.N` change only the filename.
   A header line in each file says the numbers come from the retired `HANDOFF.md`.
2. **Condensed entry format** (INVARIANTS and LIVE_WIRES): the rule in one sentence; why it matters in
   2–4 lines; the guarding test(s) as `tests/<file>::<name>`; the fixing commit hash if the old entry names
   one. The long incident stories are dropped. They stay in `git log -p -- HANDOFF.md`, and each file's header
   says so. Never drop a guarding-test reference or a rule.
3. **HANDOFF §3** (tried and failed) becomes rows in a new `## Rejected` table in `docs/DECISIONS.md`
   (ID `R-N`, approach, why it failed, source). The DECISIONS header line that points at §3 is updated.
4. **HANDOFF §6** (unfinished) merges into `docs/STATUS.md` under `## Open gaps`. Items still open keep their
   `6.N` id. Drop items STATUS already shows as done. A citation of a dropped item is rewritten to the
   invariant or test that closed it.
5. **HANDOFF §1, §4, §7, §8, §9** are dropped where `AGENTS.md`, `docs/agents/LOOP.md`, `PLAYBOOKS.md` or
   `DESIGN.md` already say it. A fact found nowhere else moves to the file that owns its topic.
6. **The size guard moves, it is not removed.** `tests/test_knowledge_refs.py` `_HANDOFF_MAX_BYTES` becomes
   per-file ceilings on `docs/INVARIANTS.md` and `docs/LIVE_WIRES.md`, set to measured size + 10 %. Its
   citation checks re-point to the new files. Raising those ceilings becomes the ask-first item in `AGENTS.md`.
   The test count must not drop.
7. **`TODO.md`** folds into `docs/STATUS.md` (open work) and is deleted.
8. **`AIGP_STUDY_GUIDE.md`** leaves the repo for `/mnt/ParamoStorage/career/AIGP_STUDY_GUIDE.md`, because it
   is exam prep, not torra documentation. Its one reference in the repo is removed.
9. **Plans keep their filenames** under `docs/plans/`. No renaming to dated names.
10. **`README.md`** keeps what it is, install, a short interface tour, the five gates in brief and the area
    map. Everything from "Commands" through "Telemetry" moves verbatim to `docs/USAGE.md`, linked from README.

## Ask-first boundaries (ask the owner, wait)

- **Uncommitted edits you didn't make.** `AUDIT_LOG.md`, `DEFENSIVE_SCORECARD_LOG.md`, `GRADE_TO_A_PLAN.md`
  and `scripts/dev/model_bench/REPORT.md` had uncommitted edits before this work started (routine output and
  a peer session). Before moving any of them, ask the owner to commit or OK them. Never stage hunks you didn't
  write.
- **Routine config.** The opencode routines `torra-doc-audit` and `torra-defensive-scorecard` append to the
  root log paths. The paths live in `/mnt/ParamoStorage/system-library/machines/workstation/opencode/opencode.json`
  and `~/.config/opencode/opencode.json`, plus any prompt file they point to (grep both trees). Changing those
  is outside this repo and runs on timers: ask before editing.
- **Comment-only edits under the guarded paths.** Comments in `torra/gates/**`, `torra/pipeline.py` and
  `torra/core/tasks.py` cite these docs. `.claude/settings.json` will prompt for each edit. Batch these files
  into one unit so the owner approves once. Comment and docstring text only, no code.
- **Deleting a file.** `git rm HANDOFF.md` and `git rm TODO.md` are covered by this plan's approval. Any other
  deletion is not.
- No push. No `.venv` recreation. No sudo.

## Units

The orchestrator runs each unit on a `model: "sonnet"` subagent. A fresh `model: "opus"` subagent then
verifies it from the unit text below and `git diff`, never from the Sonnet report. Units in one phase that
list disjoint files may run in parallel; everything else runs in order. Every unit ends with a
`git grep` showing no stale reference to the paths it moved.

### Phase 1: moves (U1–U6 can run in parallel; they touch disjoint files except `AGENTS.md`, which only the orchestrator edits, after the phase)

- **U1 `docs/os/`**: `git mv` the four OS files and `docs/README.md` to `docs/os/`. Fix every reference
  outside `AGENTS.md`. Check: `git grep -n 'docs/\(boot_and_recovery\|package_management\|desktop_environment\|graphics\|README\)\.md'`.
- **U2 `docs/plans/`**: `git mv GRADE_TO_A_PLAN.md CLAUDE_MIGRATION_ROADMAP.md docs/plans/` (only after the
  boundary above is cleared for GRADE_TO_A_PLAN). Fold `TODO.md` into `docs/STATUS.md` and `git rm` it. Fix
  references, excluding comments under guarded paths, which go to U7.
- **U3 standards**: `git mv DEFENSIVE_CODING_STANDARDS.md docs/agents/`. Update
  `scripts/dev/defensive_coding_report.py` if it opens the file by path. Fix references, excluding guarded
  paths.
- **U4 `docs/reports/`**: `git mv` the two logs. The routine-config edit happens only on the owner's yes.
  Without it, stop the unit and report.
- **U5 study guide**: move `AIGP_STUDY_GUIDE.md` out (decision 8) and remove its reference.
- **U6 README split**: decision 10. Check: every heading moved exists in `docs/USAGE.md`, and README links it.
- **U7 guarded comments**: one unit for every citation rewrite in `torra/gates/**`, `torra/pipeline.py` and
  `torra/core/tasks.py`, for the files moved in this phase. The orchestrator runs it after U1–U6.

After the phase: the orchestrator updates the `AGENTS.md` area map and read-first table, runs the full checks
below, and commits Phase 1 by explicit path.

### Phase 2: HANDOFF.md

- **U8a–U8d `docs/INVARIANTS.md`**: §2 has 64 entries (`grep -n '^### 2\.' HANDOFF.md`). Four Sonnet units,
  2.1–2.16, 2.17–2.32, 2.33–2.48 and 2.49–the last, each condensing its range per decision 2 into a fragment
  file in the scratchpad. The orchestrator concatenates them in order with the header. The verifier checks
  that every `### 2.N` heading in HANDOFF has one in INVARIANTS and that every `tests/...::name` in the old
  range survives (script it: extract both sets and diff).
- **U9 `docs/LIVE_WIRES.md`**: §5, same rules, same verifier check.
- **U10 §3 and §6**: decisions 3 and 4, into `docs/DECISIONS.md` and `docs/STATUS.md`.
- **U11 §1, §4, §7, §8, §9**: decision 5. The output is a short list of facts moved, with destinations. The
  verifier spot-checks that nothing unique was lost.
- **U12 citation rewrite** (after U8–U11): `HANDOFF.md §2.` → `docs/INVARIANTS.md §2.`, `§5.` →
  `docs/LIVE_WIRES.md §5.`, `§6.N` → `docs/STATUS.md gap 6.N`, §3/§7 cites → their new homes, across
  `torra/`, `tests/`, `scripts/`, `.github/` and docs. Comments and docstrings only. Guarded paths are
  batched for one approval, as in U7. Check: `git grep -n HANDOFF` lists only this plan and `docs/DECISIONS.md`
  history.
- **U13 guard move**: decision 6, in `tests/test_knowledge_refs.py` and `tests/README.md`. Then
  `AGENTS.md` (invariants table "Full stories" line, area map, ask-first line) and `docs/agents/LOOP.md`
  line about the byte ceiling. Then `git rm HANDOFF.md`.

After the phase: full checks, a `docs/DECISIONS.md` row recording this restructure (D-014, source this plan),
`docs/STATUS.md` updated, commit by explicit path, and set this plan's Status to done.

## Checks (Definition of Done, per phase)

```bash
uv run pytest -q          # count >= the count before the work started; record both
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python scripts/dev/defensive_coding_report.py --enforce
git grep -n 'HANDOFF\.md\|AUDIT_LOG\.md\|SCORECARD_LOG\.md\|GRADE_TO_A_PLAN\.md\|CLAUDE_MIGRATION_ROADMAP\.md\|DEFENSIVE_CODING_STANDARDS\.md\|TODO\.md\|AIGP_STUDY_GUIDE'
```

The last command must list only new-path references and this plan. `ls *.md` at the root must print
`AGENTS.md CLAUDE.md README.md`.

## Report back

One line per unit: done by, verified by, result. Then the before/after test counts, the root `ls`, the size of
`docs/INVARIANTS.md`, and the commit hashes. List any ask-first item still waiting on the owner.
