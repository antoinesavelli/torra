# harness — condensed context for local coding agents

Full spec lives in `CLAUDE.md` (too long for most local models' context budget — this is the
~2k-token subset that matters for most single-file edits). If something you need isn't here,
say so instead of guessing. Auto-loaded by OpenCode via the `AGENTS.md` convention.

**If you're a paid/capable model reading this file rather than the local model about to make the
edit, check this before you touch anything**: is the change mechanical/atomic-diff-sized, not a
judgment call? If so, drive the local model (global `build`/`quick` agent — this repo has no
repo-specific one) through it instead of editing yourself — see `CLAUDE.md`'s ACT step and the
`local-model-subagent` skill. Not applicable if you're the local model already doing the edit.

A local model proposes an operating-system change. Five gates decide whether it runs. The
operator approves. This is an OS-engineering agent first, a chat UI second — the thing being
gated is real shell commands against a real Pop!_OS box (package managers, systemd, the
filesystem, boot configuration), not a sandbox.

Stack: Python 3.10+, ruff, mypy, pytest, Textual. Entry points: `harness.cli:main`,
`harness.pipeline:Harness` (the only orchestrator). Tests: **785, must stay 785 passed**
(`python3 -m pytest tests/ -q`) — nothing in `tests/` touches a live Ollama, systemd, or
network; every external dependency is an injected callable.

## Area Maps — read the relevant one before editing

| Area | Map |
|---|---|
| Package overview | `harness/README.md` |
| The five gates (`safety`, `state`, `rollback`, `clarifier`, `router`, `decisions`) | `harness/gates/README.md` |
| Task/step primitives, conflict detection, context, Ollama client, machine observation | `harness/core/README.md` |
| Task logging, human labels, drift/loophole audits | `harness/telemetry/README.md` |
| Tests (organised by subsystem) | `tests/README.md` |
| Runnable example task files | `examples/README.md` |
| OS domain knowledge fed to the planning model | `docs/README.md` |
| **Invariants that must never break, with the story behind each** | `HANDOFF.md` §2 |
| Known live-wire bugs from the last full-codebase audit | `AUDIT_FINDINGS.md` |
| How code here is graded | `DEFENSIVE_CODING_STANDARDS.md` |

`CORE_RULES.yaml` is loaded by nothing and is known to drift from the code it describes —
never treat it as ground truth for "what does this actually do."

## Domain Invariants — non-negotiable

Full stories and exact guarding tests are in `HANDOFF.md` §2 — read the full entry before
touching any of these.

- **The classified command IS the executed command** — `Step.command`/`Step.run` built from
  the same string.
- **The model never executes anything** — JSON proposal only, no shell/tool-call loop.
- **Unapproved means refused, never defaulted to True.**
- **Safe steps get no checkbox** — only DANGEROUS/CRITICAL render one in the TUI.
- **Telemetry can never break a run** — every `telemetry/` method catches its own exceptions.
- **An undo that can't be proven is not "undone"** — three states (`undone`/`unverified`/
  `failed`), never collapsed to two.
- **A declared `rollback:` command is still classified** — a step's own `safe: true` can only
  make `SafetyClassifier` more cautious, never override it.
- **`execute()` cannot replay an already-executed result.**
- **Model size/chat-capability come from the daemon (`/api/show`), never guessed from the
  name.**
- **Every way to decline a plan goes through `Harness.reject()`** — never `execute()` with
  empty approvals, never bare UI state.

## OS-engineering guardrails

- **Command resolution must match how a real shell resolves it**, not a regex guess.
  `SafetyClassifier` unwraps `sudo`/`env`/`timeout`/`nohup`/`nice`/`xargs`, recurses into
  `sh -c`, decodes base64-to-shell, splits quotes correctly. Bar: 20/20 evasions caught, 0
  false positives (`tests/test_gates.py::TestSafety`) — don't regress it.
- **systemd-scope assumptions must be checked against the real unit**, never assumed
  `--user`. `ollama.service` on this exact machine runs system-wide.
- **OS domain knowledge in `docs/*.md` grows only from a measured `scripts/dev/model_bench/`
  gap**, never a guess.
- **Model routing consults the live daemon (`/api/show`) before it guesses from the name.**
- **A destructive/irreversible action never runs without an explicit human tick**, on every
  path (TUI, CLI, direct API) that can decline or execute a plan.

## Secrets

Never print, log, or paste a secret value (`OPENROUTER_API_KEY`, `.env` contents) — including in
a suggested command like `RecoveryOption.action`. To check a var is set, show presence/length:
`test -n "$VAR" && echo "VAR is set (${#VAR} chars)" || echo "VAR is MISSING"`.

## When to ask vs. act

**Ask first:** anything on the safety-critical path (the five gates, `SafetyClassifier`,
`Step.command`/`Step.run` construction, `Harness.execute`/`Harness.reject`); schema/format
changes to persisted state (`resume/*.json`, `telemetry/tasks.jsonl`, `audit/*.audit.json`);
deleting files or branches.

**Act freely:** reading any file; editing tests; fixing lint/type errors; peripheral-tier
files (`cli.py`, `tui.py` glue, `__init__.py`).

## Code conventions

Line length 100 (ruff `E501` ignored). No comments unless WHY. One-line docstrings + a
`Depends on:` line per touched module. CCN<15 ceiling (named exceptions: `SafetyClassifier
._judge`, `Clarifier.detect`, `Harness.execute` — don't split those to move a number).
Full param + return type hints on new/edited code.

## Definition of done

1. `python3 -m pytest tests/ -q` — must stay 785 passed.
2. Per-file docstring current on every touched module.
3. That directory's `README.md` updated if a file was added/moved/renamed.
4. `HANDOFF.md` §2 updated if the change touches an existing invariant.
5. `ruff check .` and `mypy <touched files>` clean, zero new errors.
6. No version bumps, tags, or `CHANGELOG.md` entries — this repo doesn't keep them.

## Key commands

| Task | Command |
|---|---|
| Tests (all, must stay 785) | `python3 -m pytest tests/ -q` |
| Tests (one file/case) | `python3 -m pytest tests/test_gates.py -k <name>` |
| Lint | `ruff check .` |
| Type check | `mypy harness/` |
| Defensive-coding scorecard | `python3 scripts/dev/defensive_coding_report.py` |
| One-shot request | `harness ask "..."` |
| Plan only, run nothing | `harness plan "..."` |
| Classify a task file without running it | `harness check task.yaml` |
