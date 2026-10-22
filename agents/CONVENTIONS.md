# Code conventions, sources of truth, tool use

The fuller form of the short conventions in `AGENTS.md`, plus the source-of-truth registry and where new
information goes.

Default values are read from the module that uses them, never from a doc: a doc that quotes a
default describes the system, it is not the system.

`HANDOFF.md` holds this repo's invariants (§2), dead ends (§3), live wires (§5) and open work (§6).
It is too large to read whole: look up the § your change touches before editing
`torra/`. `grep -n '^### 2\.' HANDOFF.md` lists every invariant, `grep -n '^### 5\.' HANDOFF.md` every
live wire. It is intentionally not duplicated below; this file condenses it and adds what HANDOFF
doesn't cover (Definition of Done, code conventions, ask-vs-act), rather than restating it.

## Code Conventions

(Full rationale: `DEFENSIVE_CODING_STANDARDS.md` §1. Base rules — no comments unless WHY,
one-line docstrings, module `Depends on:` line, CCN<15 ceiling/aim≤10, one test per new module —
are AGENT_DOCTRINE.md's; this section is torra-specific rest.)

- Line length: 100 (ruff `E501` ignored — don't fight it manually).
- **No CCN exceptions remain.** `SafetyClassifier._judge`, `Clarifier.detect` and `Torra.execute`
  were split on 2026-08-11 (`DEFENSIVE_CODING_STANDARDS.md` §2.1 update); `Torra.execute` is 14,
  one below the ceiling, so extract a helper before adding a branch. Don't fragment flat code
  just to move a number; do split anything that's actually tangled.
- Typing: full param + return hints on new/edited code; `Any` for genuinely untyped externals,
  never as a shortcut around a knowable type; `X | None` whenever a real caller can pass `None`.
- Imports: ruff isort, first-party = `torra`.

## Source-of-Truth Registry

For each class of fact, exactly one place owns it; everything else links there rather than restating
(a second copy is how facts rot). A blank or `trust-based` in **Verified by** means nothing catches
drift, so treat that fact with extra suspicion. A new kind of durable fact gets a row here and a row
in "Where new information goes".

| Fact class | Canonical owner | Verified by |
|---|---|---|
| Instruction-file shape, root byte budget (14,400 bytes) | `AGENTS.md`, with `CLAUDE.md` as a one-line import | `tests/test_instruction_files.py` |
| Safety/execution invariants | `HANDOFF.md` §2 | the specific test(s) each entry names; `tests/test_knowledge_refs.py` (every `file.py::name` citation resolves to a defined test) |
| The critical invariants, loaded at start | `AGENTS.md` Domain Invariants table | `tests/test_knowledge_refs.py` checks each `file.py::name` a row cites exists; a bare file name or a "+ N more" is not checked |
| Files Claude Code must ask before editing (the five gates, `Torra.execute`/`reject`, `Step` construction) | `.claude/settings.json` `permissions.ask` | *(trust-based: a new gate file under `torra/gates/` is covered by the glob; a renamed `pipeline.py`/`core/tasks.py` is not)* |
| Default config values and behavior (what actually runs) | the module that reads them (`pipeline.py`, `gates/*.py`, `core/*.py`) | that module's tests |
| What each package/file does | its one-line module docstring, indexed by its directory's `README.md` row | `defensive_coding_report.py --enforce` (Documentation floor A: decision-path modules carry a module docstring with a `Depends on:` line); `tests/test_knowledge_refs.py` (the `AGENTS.md` Area Maps paths exist); README rows and docstring text are *(trust-based)* |
| Code-quality floors (what CI fails on) | `scripts/dev/defensive_coding_report.py` (`ENFORCE_FLOORS`, `MIN_GPA`); the method is `DEFENSIVE_CODING_STANDARDS.md` | `defensive_coding_report.py --enforce` in `.github/workflows/ci.yml` |
| Live wires ("an obvious-looking change here is wrong") | `HANDOFF.md` §5.N, plus a `# Live wire: HANDOFF.md §5.N` comment at the code site | `tests/test_knowledge_refs.py` (every cited §N.M header exists) |
| Dead ends (tried, failed, don't reopen) | `HANDOFF.md` §3 | *(trust-based)* |
| Open work | one owner per item: grade findings (P-/D-numbered) in `GRADE_TO_A_PLAN.md` until that plan closes (its leftovers then move to §6), everything else in `HANDOFF.md` §6, or in `TODO.md` (the only mutable checklist) while it is in-flight work not yet worth a §6 item; an item leaves `TODO.md` by moving to §6 or being deleted; where an item appears in two places, the other keeps only a link | *(trust-based; a fixed item shrinks to its pointer)* |
| OS domain knowledge for the planning model | `docs/*.md` | `scripts/dev/model_bench/` benchmark results (`REPORT.md`) |
| Model catalog / routing rules | `torra/gates/router.py` (`ROUTING_RULES`) | `tests/test_gates.py::TestRouting` |
| Contract summary (entry points, outcomes, stages, exit codes) | `HANDOFF.md` §8 | *(trust-based: cross-check against `pipeline.py`/`cli.py` if it looks stale)* |
| Cross-session context only Claude needs | the Claude memory directory (see Memory) | *(trust-based)* |
| Whether a fresh agent answers correctly | `agents/knowledge_questions.yaml` | `tests/test_knowledge_questions.py` (every expected fact is in its source docs); the fresh-agent grade is manual |

## Where new information goes

The registry is read-side; this is write-side. You just learned something: route it by kind, and
check the last column, which is the miswrite that causes the rot.

| You just learned… | It goes in… | NOT in… |
|---|---|---|
| A rule that must always hold | a `HANDOFF.md` §2 entry (opens with `**Rule:**`, names its guard test) and the guard test; a critical one also gets an `AGENTS.md` invariant row | a comment alone, or only `AGENTS.md` (no room for the story) |
| Why a line or value is non-obvious | a WHY-comment at that line; if it needs a story or more than a few lines, it is a live wire (next row) | `HANDOFF.md` or memory: too far from the code, so it rots |
| An obvious-looking change here would be wrong | `HANDOFF.md` §5.N (next unused number) and `# Live wire: HANDOFF.md §5.N` at the code site | an unnumbered paragraph, or the comment alone |
| A dead end: tried X, it failed | `HANDOFF.md` §3 | memory or a commit message: nobody reads either before trying X again |
| Open work | its one owner (see the registry); the other place gets a link | both places in full |
| What a file does | its one-line module docstring, indexed by its directory's `README.md` row | restated in several docs |
| OS knowledge for the planning model | `docs/*.md`, only from a measured `scripts/dev/model_bench/` gap | a guess, or `agents/` (that is for agents editing torra) |
| Cross-session context only Claude needs (accounts, the owner's preferences, gotchas of tools outside this repo) | the Claude memory directory | a repo doc; but anything OpenCode or the local model also needs goes in the repo, because Claude memory is Claude-only |
| A rule for agents editing torra | `AGENTS.md` if it must be loaded at start (under its 14,400-byte budget), else an `agents/<TOPIC>.md` behind a read-first trigger and a row in `agents/README.md` | `CLAUDE.md`: it only imports `AGENTS.md` |
| A multi-step plan | a root `*_PLAN.md` or `*_ROADMAP.md` whose first lines are `Status: active` (or `done`), `Owner decisions pending: <ids or none>` and `Last verified: <sha> on <date>`, plus an `AGENTS.md` Area Maps row. A plan holds design and decisions, never checkboxes. Update `Last verified` only when you re-checked the plan against the code at that sha. The commit that completes a plan retires it with `git rm` (ask first) and updates every reference; `tests/test_knowledge_refs.py` checks the header | a checklist inside a plan, or a finished plan left in place or copied into `HANDOFF.md` or `AGENTS.md`: it would read as live |
| A fact a fresh agent must get right | a question in `agents/knowledge_questions.yaml`, changed in the same commit as the fact | only the doc: then nothing notices when it drifts |

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
- **Never print, log, or paste a secret value** (`OPENROUTER_API_KEY`, anything in `.env`, a
  secret-bearing `os.environ` entry) — not in a debug `print`, a log line, a suggested command
  (`RecoveryOption.action` is shown to operators and lands in the decisions log), a test fixture,
  or a doc. To confirm a var is set, show presence/length, never the value:
  `test -n "$VAR" && echo "VAR is set (${#VAR} chars)" || echo "VAR is MISSING"`. Precedent:
  `OpenRouterClient.recovery_options()` once suggested `echo $OPENROUTER_API_KEY`; guarded by
  `tests/test_core.py::TestOpenRouterClient::test_check_key_action_*`. Same rule as Paramo's
  `SECURITY.md` ("Secrets must never be printed, logged, or pasted").

## Memory

See AGENT_DOCTRINE.md. Project memory lives in
`~/.claude/projects/-mnt-ParamoStorage-AIModels-torra/memory/`. A memory can drift from the system
it describes, like any description: check it against the code before acting on it.
