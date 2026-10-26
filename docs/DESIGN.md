# torra: design

Status: describes the built system as of 2026-10-25. It is **not** a pre-build approved spec: torra was built
before the corsu standard, and no owner spec predates the code. Compiled from `README.md`, `HANDOFF.md` and
`AGENTS.md`; where this page and the code disagree, the code wins. The owner has not approved this page as a spec.

## 1. Purpose and scope

As `AGENTS.md` has always stated it: a local model proposes an operating-system change, five gates decide
whether it runs, the operator approves. "This is an operating-system engineering agent first, a chat UI second."

The gates hold back real shell commands against a real Linux box (package managers, systemd, the filesystem,
boot configuration). A wrong OS-level command is not a bug to patch afterwards: it already ran.

Not in scope, each with its source:

- Remote execution on a second machine. Executor is observed read-only over SSH; remote file edits exist only as
  torra-built pull-edit-push steps (`HANDOFF.md` §2.52, §2.53).
- A model with a shell or a tool-call loop (§2 of this page; `HANDOFF.md` §2.2).
- A version number, tags or a changelog (`docs/agents/LOOP.md`, Definition of Done item 7).
- Claude Code's ecosystem conveniences (subagents, skills, connectors, scheduling): lowest priority, not built
  (`CLAUDE_MIGRATION_ROADMAP.md`, bucket 4).

## 2. The principle

Moved here from `HANDOFF.md` §9. **The model proposes, and something that is not the model decides.**

Six phases of gate-building were spent on the wrong problem, because nobody checked that an agent was going to
be plugged in. The gates turned out to be worth keeping anyway, and the reason is that separation. A model that
could act directly would have made every one of them optional.

Consequences the code enforces, each pinned by a test in `AGENTS.md`'s invariants table: the classified command
is the executed command; the model returns JSON only; unapproved means refused; every way to decline goes through
`Torra.reject()`.

## 3. Module split

The dependency direction, moved here from `HANDOFF.md` §1. Read in this order:

1. `torra/core/`: `Step`, `TaskSpec` (`tasks.py`), `OllamaClient` (`ollama.py`), conflict detection
   (`conflict.py`); see `torra/core/README.md`.
2. `torra/gates/`: the five guardrails (`rollback.py` and `undo.py` are one guardrail, ROLLBACK_ON_FAIL, split across two files past
   the LOC cap; see `torra/gates/README.md`).
3. `torra/agent.py`: the model loop.
4. `torra/pipeline.py`: `Torra`, the seven stages, the only orchestrator. `torra/pipeline_types.py` holds the
   `Stage`/`Outcome`/`Plan`/`RunResult` data model it produces, re-exported from `pipeline.py` unchanged.

`tui.py`, `cli.py` and `torra/telemetry/` are leaves: nothing depends on them. The per-path map is `README.md`'s
Layout table; the contract (entry points, outcomes, stages, exit codes, state files) is `HANDOFF.md` §8.

## 4. Where the rest of the design lives

| Topic | Owner |
|---|---|
| The seven stages and the five gates, as a user meets them | `README.md` |
| Each invariant with its story and guard test | `HANDOFF.md` §2 |
| Approaches tried and abandoned | `HANDOFF.md` §3 |
| Why a decision was made | `docs/DECISIONS.md` |
