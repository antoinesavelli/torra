# Code conventions, sources of truth, tool use

The fuller form of the short conventions in `AGENTS.md`, plus the source-of-truth registry.

**`CORE_RULES.yaml` is not a source of truth.** It's a human-readable snapshot of default
values, loaded by nothing (`grep -rn CORE_RULES harness/` returns nothing) — it already
disagrees with the code it describes (`keep_recent: 3` there, `4` in
`ContextCompressor.__init__`) and nothing catches new drift. Never point someone at it to
answer "what does this actually do" — point at the module cited in its own header comment.
If you find yourself tempted to "fix" a behavior by editing this file, stop: you have edited
a description, not the system.

`HANDOFF.md` is this repo's Domain Invariants table and its Task Playbooks section at once —
read it before changing anything under `harness/`, the same way you'd read a nested
`AGENTS.md` before touching a Paramo area. It is intentionally not duplicated below; this
file condenses it and adds what HANDOFF doesn't cover (Definition of Done, code conventions,
ask-vs-act), rather than restating it.

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
- **Never let `CORE_RULES.yaml` numbers substitute for reading the code** — see the Area Maps
  section of `AGENTS.md`.

## Memory

See AGENT_DOCTRINE.md. Project memory lives in
`~/.claude/projects/-mnt-ParamoStorage-harness/memory/` — the same rule this file applies to
`CORE_RULES.yaml` applies to memory: a description can drift from the system it describes.
