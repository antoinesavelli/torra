# torra: decisions

One row per decision; a reversal edits the row's Status and adds a new row. Every row names its source (a commit
or a doc); the why is in the source when it is long. Approaches tried and abandoned are not repeated here:
`HANDOFF.md` §3 owns them.

| ID | Date | Decision | Why | Status |
|---|---|---|---|---|
| D-001 | 2026-08-25 | An alternate model backend (`openrouter/<id>`) is used only when the operator names it; `ModelRouter` can never pick one | Auto-selection is made structurally impossible, and a missing key fails loudly. Source: `HANDOFF.md` §2.27, `0f68566` | Standing |
| D-002 | 2026-08-28 | The routing catalog holds no Qwen-origin models: `gpt-oss:20b` for debug/plan/analyze and the fallback, `devstral-small-2:24b` for code | Owner request, not a measured gap; replacements chosen from public benchmarks and torra's own battery. Source: `HANDOFF.md` §2.49, `a58ea71` | Standing |
| D-003 | 2026-08-28 | A second machine (executor) is observed read-only over SSH; remote file edits exist only as torra-built pull-edit-push steps, never as a model-written `ssh` | Remote execution would need a new evasion class in the classifier and breaks undo. The owner chose read-only observation. Source: `HANDOFF.md` §2.52, §2.53, `0a1376b`, `dd49395` | Standing |
| D-004 | 2026-08-23 | The site-knowledge library gives the model a topic index only; reading a file is an ordinary gated plan step | File contents wired into the prompt would bypass the gate a step gets. Source: `HANDOFF.md` §5.21, `583f99f` | Standing |
| D-005 | 2026-10-24 | The Claude Code hook (`scripts/claude_code_hook.py`) asks on CRITICAL and never answers "allow" | An "allow" would skip Claude Code's own permission rules; asking at DANGEROUS would prompt on 41.6% of 84,078 measured commands. Source: `HANDOFF.md` §2.85, `051d658` | Standing |
| D-006 | 2026-08-25 | No version bumps, tags, `CHANGELOG.md` or `torra --version`; `pyproject.toml` `version` stays `0.0.0` | A solo tool with no consumers of the package as an API; `git log` and `HANDOFF.md` §2 are the record. Source: `docs/agents/LOOP.md` Definition of Done item 7, `0845072` | Standing |
| D-007 | 2026-07-28 | The OS reference files in `docs/` are not wired into `torra.agent.SYSTEM_PROMPT` | Injecting them changes the core loop and needs its own reviewed change and test. Source: `docs/README.md`, `docs/agents/GUARDRAILS.md`, `5bfa622` | Standing; see Parked P-1 |
| D-008 | 2026-10-25 | torra lives at `/mnt/ParamoStorage/corsu/torra` | Corsican AI products share one parent. Source: migration plan decision 1 (below), `302470c` | Done |
| D-009 | 2026-10-25 | `agents/` becomes `docs/agents/`, beside `DESIGN.md`, `DECISIONS.md`, `STATUS.md` | The corsu repo standard. Source: migration plan decision 5, `818f9ff` | Done |
| D-010 | 2026-10-25 | Python 3.12, uv with a committed `uv.lock`, ruff, `mypy --strict`, pytest; `requirements-lock.txt` and Python 3.10 retired | The corsu toolchain baseline. Source: migration plan decision 6 | Done |
| D-011 | 2026-10-25 | MIT license; public on `antoinesavelli/torra`, pushed directly to the existing remote | Source: migration plan decisions 7 and 9 | License done; push pending |
| D-012 | 2026-10-25 | A gitleaks pre-push hook over full history, GitHub secret scanning and push protection; CI runs ruff, ruff format, mypy, pytest plus torra's coverage floors and defensive scorecard | Source: migration plan decision 8 | Hooks and CI done; GitHub settings pending |
| D-013 | 2026-10-25 | The private `paramotrading/torra` repo is deleted | Stale since 2026-09-05. Source: migration plan decision 12 | Pending (owner, Phase C) |

The migration plan is `/mnt/ParamoStorage/system-library/docs/plans/2026-10-25-corsu-migration.md`; its decisions
are the owner's, made in the 2026-10-25 grilling.

## Parked

| ID | Parked item | Revival trigger |
|---|---|---|
| P-1 | Wiring `docs/` reference files into the planning model's prompt | Its own reviewed change with its own test (`docs/README.md`) |
| P-2 | Ask each DANGEROUS/CRITICAL tick when its step runs, not all before step 1 (`GRADE_TO_A_PLAN.md` D8, open since 2026-10-12) | "Wait for real-use evidence"; needs the owner's answers on what a no does mid-run, whether the operator must stay at the terminal, and whether a resume re-asks |
| P-3 | `iterate()` resolving a NEEDS_ANSWER round instead of stopping (`CLAUDE_MIGRATION_ROADMAP.md`, bucket 1) | "Only if it's actually hit in practice, with a real example in hand" |
| P-4 | Session-scale memory/research continuity for the model (roadmap bucket 3) | The four-question gate in `HANDOFF.md` §7 passes; not started |
| P-5 | Claude Code ecosystem conveniences: subagents, skills, connectors, scheduling (roadmap bucket 4) | A specific convenience turns out to be load-bearing for the operator's workflow, not hypothetically useful |
