# torra: decisions

One row per decision; a reversal edits the row's Status and adds a new row. Every row names its source (a commit
or a doc); the why is in the source when it is long. Approaches tried and abandoned are in `## Rejected`
below.

| ID | Date | Decision | Why | Status |
|---|---|---|---|---|
| D-001 | 2026-08-25 | An alternate model backend (`openrouter/<id>`) is used only when the operator names it; `ModelRouter` can never pick one | Auto-selection is made structurally impossible, and a missing key fails loudly. Source: `docs/INVARIANTS.md` §2.27, `0f68566` | Standing |
| D-002 | 2026-08-28 | The routing catalog holds no Qwen-origin models: `gpt-oss:20b` for debug/plan/analyze and the fallback, `devstral-small-2:24b` for code | Owner request, not a measured gap; replacements chosen from public benchmarks and torra's own battery. Source: `docs/INVARIANTS.md` §2.49, `a58ea71` | Standing |
| D-003 | 2026-08-28 | A second machine (executor) is observed read-only over SSH; remote file edits exist only as torra-built pull-edit-push steps, never as a model-written `ssh` | Remote execution would need a new evasion class in the classifier and breaks undo. The owner chose read-only observation. Source: `docs/INVARIANTS.md` §2.52, §2.53, `0a1376b`, `dd49395` | Standing |
| D-004 | 2026-08-23 | The site-knowledge library gives the model a topic index only; reading a file is an ordinary gated plan step | File contents wired into the prompt would bypass the gate a step gets. Source: `docs/LIVE_WIRES.md` §5.21, `583f99f` | Standing |
| D-005 | 2026-10-24 | The Claude Code hook (`scripts/claude_code_hook.py`) asks on CRITICAL and never answers "allow" | An "allow" would skip Claude Code's own permission rules; asking at DANGEROUS would prompt on 41.6% of 84,078 measured commands. Source: `docs/INVARIANTS.md` §2.85, `051d658` | Standing |
| D-006 | 2026-08-25 | No version bumps, tags, `CHANGELOG.md` or `torra --version`; `pyproject.toml` `version` stays `0.0.0` | A solo tool with no consumers of the package as an API; `git log` and `docs/INVARIANTS.md` are the record. Source: `docs/agents/LOOP.md` Definition of Done item 7, `0845072` | Standing |
| D-007 | 2026-07-28 | The OS reference files in `docs/os/` are not wired into `torra.agent.SYSTEM_PROMPT` | Injecting them changes the core loop and needs its own reviewed change and test. Source: `docs/os/README.md`, `docs/agents/GUARDRAILS.md`, `5bfa622` | Standing; see Parked P-1 |
| D-008 | 2026-10-25 | torra lives in the shared `corsu/` folder | Corsican AI products share one parent. Source: migration plan decision 1 (below), `302470c` | Done |
| D-009 | 2026-10-25 | `agents/` becomes `docs/agents/`, beside `DESIGN.md`, `DECISIONS.md`, `STATUS.md` | The corsu repo standard. Source: migration plan decision 5, `818f9ff` | Done |
| D-010 | 2026-10-25 | Python 3.12, uv with a committed `uv.lock`, ruff, `mypy --strict`, pytest; `requirements-lock.txt` and Python 3.10 retired | The corsu toolchain baseline. Source: migration plan decision 6 | Done |
| D-011 | 2026-10-25 | MIT license; public on `antoinesavelli/torra`, pushed directly to the existing remote | Source: migration plan decisions 7 and 9 | License done; push pending |
| D-012 | 2026-10-25 | A gitleaks pre-push hook over full history, GitHub secret scanning and push protection; CI runs ruff, ruff format, mypy, pytest plus torra's coverage floors and defensive scorecard | Source: migration plan decision 8 | Hooks and CI done; GitHub settings pending |
| D-013 | 2026-10-25 | The private `paramotrading/torra` repo is deleted | Stale since 2026-09-05. Source: migration plan decision 12 | Pending (owner, Phase C) |
| D-014 | 2026-10-27 | `HANDOFF.md` and the other root docs retire onto the corsu standard: §2 → `docs/INVARIANTS.md`, §5 → `docs/LIVE_WIRES.md` (numbers kept, entries condensed to rule, why, guard test), §3 → "Rejected" here, §6 → `docs/STATUS.md` "Open gaps"; OS reference → `docs/os/`, plans → `docs/plans/`, routine logs → `docs/reports/`, commands → `docs/USAGE.md`. The byte-ceiling guard moves to the two new files (size + 10 %) | Two doc systems stacked on each other; a 305 KB append-only file nobody could read whole. The incident stories stay in `git log -p -- HANDOFF.md`. Source: `docs/plans/2026-10-26-doc-architecture.md` | Done |
| D-015 | 2026-10-27 | The defensive-coding scorecard prints word bands (A = strong, B = ok, C = weak, D = poor, F = failing) and a band mean, not letters and a GPA; thresholds, floors and `--enforce` strictness unchanged | Its A–F was confused with the `/letter-grade` skill's A–F grade. Same spec as Paramo's copy. Source: `docs/plans/2026-10-27-scorecard-word-bands.md` | Done |
| D-016 | 2026-11-18 | The options of a program judged by an `OptionSpec` (the `allowing` entries in `PREDICATES`, curl, tar, sed, awk, every `SUBCOMMANDS` entry) are judged by an allow list (a spec of its non-writing options, parsed like getopt), not by a list of flags to catch; long names match in full only, never as a prefix | Patching the P5.5 evidence (`curl -w '%output{F}'`, `awk -W exec`, `pip list --log=F`) would be the open-ended detect-list shape (R-2, R-3, R-11): the next red-team finds the next option. A prefix rule (the first O1 wording) was rejected in Phase 1: the real program matches its whole option table first, so `curl --output F` would read as the allowed `--output-dir`. Source: `docs/plans/P5_5_OPTION_ALLOWLIST_PLAN.md` O1, `docs/INVARIANTS.md` §2.93 | Done (`37b87f0`, `d370da1`) |
| D-017 | 2026-11-19 | A resume point dated more than 24h from now, either way, is ignored and the run starts fresh, unless its `in_progress` mark names a step of the plan; the plan shows what a resume will skip or stop on. No sweep of expired files | 24h is `ShadowStore.STALE_SHADOW_SECONDS`, the age after which torra already treats its own shadow copies as abandoned; a day-old "completed" is not evidence for a find-then-act plan (gap 6.6). A step's unknown effect stays unknown however old, so an in-flight mark never expires. An expired file is inert and overwritten by the next run of its task id. Source: `docs/INVARIANTS.md` §2.64, `docs/plans/P5_RELIABILITY_RESUME_AND_SHADOW_PLAN.md` | Standing |

The migration plan is `docs/plans/2026-10-25-corsu-migration.md` in the private system-library repo; its decisions
are the owner's, made in the 2026-10-25 grilling.

## Parked

| ID | Parked item | Revival trigger |
|---|---|---|
| P-1 | Wiring `docs/os/` reference files into the planning model's prompt | Its own reviewed change with its own test (`docs/os/README.md`) |
| P-2 | Ask each DANGEROUS/CRITICAL tick when its step runs, not all before step 1 (`docs/plans/GRADE_TO_A_PLAN.md` D8, open since 2026-10-12) | "Wait for real-use evidence"; needs the owner's answers on what a no does mid-run, whether the operator must stay at the terminal, and whether a resume re-asks |
| P-3 | `iterate()` resolving a NEEDS_ANSWER round instead of stopping (`docs/plans/CLAUDE_MIGRATION_ROADMAP.md`, bucket 1) | "Only if it's actually hit in practice, with a real example in hand" |
| P-4 | Session-scale memory/research continuity for the model (roadmap bucket 3) | The four-question gate in `docs/agents/PLAYBOOKS.md` ('Before adding any feature') passes; not started |
| P-5 | Claude Code ecosystem conveniences: subagents, skills, connectors, scheduling (roadmap bucket 4) | A specific convenience turns out to be load-bearing for the operator's workflow, not hypothetically useful |

## Rejected

Approaches tried or considered and rejected; do not re-derive them. They cost measurement to discover.

| ID | Approach | Why it failed | Source |
|---|---|---|---|
| R-1 | Regex over raw command text for safety | Missed 5 of 12 evasions | Replaced by argv resolution in `SafetyClassifier.resolve` |
| R-2 | Closed list of vague **phrasings** for the clarifier | 83% false negatives on a blind corpus | Replaced by the anchor-presence test |
| R-3 | Closed list of mutating **verbs** for the clarifier | 50% false negatives, the same failure one level up | Replaced by the verb-agnostic anchor test |
| R-4 | First-match model routing | Mis-routed multi-intent requests | Replaced by weighted scoring plus a lead multiplier |
| R-5 | Term-frequency scoring for routing | "review the deploy script" routed to a code model | Replaced by "the earliest-matching rule owns the intent" |
| R-6 | 24-character head bonus for routing | The second word also caught the bonus | Replaced by the exclusive lead multiplier |
| R-7 | Monkeypatching `shutil.copy2` for reflink copies | Infinite recursion, and the patch is process-global | Replaced by `UndoGenerator._copy` |
| R-8 | Three-tier adaptive checkpoint policy | About 300 lines to protect a 40-second interactive turn | Replaced by one `ResumePoint` file |
| R-9 | Repair-loop check firing on any non-empty `clarifier_gaps` | Broke 6 existing tests: `no_target` fires on nearly any ordinary question | Reverted before commit; `docs/INVARIANTS.md §2.40` |
| R-10 | Hard-linking instead of copying for undo shadow copies (found while closing the reflink question, 2026-08-26) | A hard link protects only against delete or replace; a step that truncates and rewrites the same inode in place (`> file`, some editors) corrupts the "snapshot" too, so undo would look fast and restore garbage. ext4 has no reflink (`_probe_reflink()` falls back to a byte copy bounded by `MAX_SHADOW_BYTES`, 64 MB), and `/tmp` is the same ext4 here (and where `/tmp` is tmpfs, undo snapshots would spend RAM that model routing needs, `CPU_PARAM_CEILING_B`). Reopen only if a measured performance problem shows up | `git log -p -- HANDOFF.md` (old §6.5) |
| R-11 | Auto-generating undo for database migrations (alembic, Django, Rails, Flyway, Prisma, ...) | An open-ended list of tools to detect (the R-2/R-3 shape), and a wrong guess at a tool's "down" runs against a live database; some migrations (a lossy `DROP COLUMN`) have no down at all. A hand-written, human-reviewed `rollback:` entry is the design, not a stopgap | `git log -p -- HANDOFF.md` (old §6.3) |

The pattern in R-2, R-3 and R-9 is the important one: three times now, a closed vocabulary (or a signal that behaved like
one) was relied on to separate "genuinely ambiguous" from "ordinary" and measured worse than expected. If you
find yourself adding items to a list of things to detect, ask whether the list is the bug.
