# torra

[![CI](https://github.com/antoinesavelli/torra/actions/workflows/ci.yml/badge.svg)](https://github.com/antoinesavelli/torra/actions/workflows/ci.yml)

A local model proposes. Five gates decide. You approve. Then it runs.

torra is a terminal agent for operating-system work on a Linux machine (built and used on Pop!_OS). A
model running locally through Ollama reads the real machine state and proposes a plan in the same
structure as a hand-written task file. Five code-based guardrails resolve every command to argv,
classify it, and snapshot the files it touches; they hold every step rated DANGEROUS or CRITICAL, and any
program they do not recognise, until the operator ticks it. The model never gets a shell. It is in
progress and one person's tool, with no outside users: the classifier is unfinished (`docs/STATUS.md`).

**Status: in progress.** What is built, what was measured and what is still open: `docs/STATUS.md`.

```bash
uv sync
uv run torra doctor
uv run torra
```

---

## What it is

You describe what you want in plain language. A model on your own machine reads
the actual state of the box, writes a plan, and hands it to five guardrails that
resolve every command down to argv, classify it, snapshot the files it touches,
and hold every step classified DANGEROUS or CRITICAL until you tick it.

The model never gets a shell. It writes a proposal into the same structure a
hand-written task file uses, and that proposal travels the exact code path the
test suite covers. There is no second path.

```
converse   the model reads your request, consults the clarifier for what is
           structurally missing, and answers, asks, or proposes
observe    real machine state, read-only, before any proposal exists
propose    a task spec, every field type-checked
gate       safety classification, preconditions, undo generation, conflict baseline
present    you see the plan AND every gate verdict, and approve per step
execute    with rollback armed and preconditions rechecked between steps
report     back into the conversation, and into telemetry
```

Before a plan reaches you, the model gets up to two revision rounds for
anything torra can check without running it: a task that fails validation,
a command bash cannot parse, a template slot such as `<found-path>` left in
a command, a destructive or system-path step, and a step needing a tick that
names a path which does not exist. The first three end the request if they
are still there afterwards. A missing path is only a warning, printed above
the steps, because a plan may create the path itself.

---

## Install

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), and Ollama running locally with at least one model pulled.

To use an Ollama on another machine (say through an SSH tunnel), set
`TORRA_OLLAMA_HOST=http://127.0.0.1:11435`. Only the model calls follow it; the
`ollama ps`/GPU check and the "restart ollama" suggestion still act on the
machine torra runs on.

```bash
git clone https://github.com/antoinesavelli/torra.git ~/torra
cd ~/torra
uv sync --locked --extra fast
uv run pytest -q
```

Expect every test to pass. Then:

```bash
torra doctor     # environment check
torra            # the interface
```

Put it on PATH permanently:

```bash
echo "alias torra='~/torra/.venv/bin/torra'" >> ~/.bashrc && source ~/.bashrc
```

`inotify_simple` is optional (the `fast` extra, `uv sync --extra fast`). With it, conflict detection checks only the paths
the kernel reports changed, plus a periodic full walk; without it, every check walks the tree.
`textual` is required for the interface but not for `run` or `check`.

To update an existing checkout later: `scripts/update.sh` -- pulls latest
`main`, reinstalls, and reruns the test suite as the final gate. Refuses to
run over uncommitted local changes.

---

## The interface

Chat on the left, a sidebar on the right. The sidebar has six tabs, in
this order:

- **Actions** -- the current proposal: every step's risk level, why the
  classifier said so, whether an undo exists, and the Run/Reject/Dry run
  buttons. (Still `PlanPane` internally -- the label changed, not the concept.)
- **Thinking** -- the live reasoning stream, for a model that exposes it
  (e.g. `deepseek-r1` or `qwen3:32b`) -- grows while it plans and resets
  once the plan or answer arrives; not part of the saved conversation,
  just a window into that one turn.
- **Session** -- browse and reopen a past session without leaving the
  tab, the persistent sibling of `ctrl+o`'s picker; recomputed each time
  you switch to it.
- **Model** -- pick this tab's model override without a modal, the
  persistent sibling of `f2`'s picker; same list, same behavior.
- **Evidence** -- a live health/crash summary, the same content
  `torra evidence` prints, recomputed each time you switch to it.
- **Metrics** -- which model this tab actually used and its real size/
  context ceiling, a live `ollama ps`/GPU line, this session's own
  context/turn figures, cumulative token and OpenRouter spend counters
  (labelled "since app start" -- those clients are shared across every
  tab, not tracked per session), and which files a `reads` step's real
  output has fed into the model's own context this session.

A live spinner and elapsed time appear directly above the prompt input while
the model is planning or a run is in progress -- not just in the header's
status line, which is easy to miss when you're looking at what you just
typed. While planning, it also says `Esc to stop`: the model call itself
can't be killed mid-request, but Escape frees the input immediately and
discards whatever answer eventually arrives. An approved run in progress is
never interruptible this way -- stopping real command execution partway
through is a safety hazard, not a convenience.

Once you approve a plan, each step's real stdout/stderr streams live
into the chat as it runs, tagged with the step id -- the only place a
successful step's output is ever shown, so unlike the reasoning stream
it stays in the transcript after the run finishes.

Steps classified DANGEROUS or CRITICAL arrive with an **unticked** box, each
with a description of *why* it's there -- what it confirms, what it depends on
from an earlier step, what it sets up for a later one -- not just the bare
command. SAFE and CAUTION steps get no box at all, because asking for
approval on `df -h` trains people to tick without reading, which is how the
one that mattered gets ticked too.

Each step that needs a tick also says what it will act on, looked up
read-only when you first see it, for example `on disk: /srv/app/old-logs:
directory, 41 files in 3 folders, 1.2 GiB, newest change 2026-09-07` or
`on disk: /srv/app/old-logs: does not exist`. It never follows a symlink or reads a file, and it stops
counting a large tree after a second. It describes the paths written in the
plan, not what an earlier step will find.

Ticking some dangerous steps and leaving others declined runs the ticked
ones and skips the rest -- it no longer rejects the whole plan over one
declined step. If an approved step's command looks like it depends on a
declined one (references a path the declined step would have produced),
you'll see a warning before anything runs; it's a best-effort hint; not a
guarantee, since torra has no real model of dependencies between
steps.

Every conversation is a tab. Each tab is a fully independent session -- its
own model context, its own gate history -- so running a plan in one never
blocks or interrupts another. Every message is persisted as it happens
(`workdir/sessions/<id>/`), so a crash or restart never loses a
conversation: reopen it with `ctrl+o` and both the visible chat and the
model's own memory of it come back exactly as they were.

| Key | Does |
|-----|------|
| `enter` | send |
| `escape` | stop an in-flight plan request (never an approved run in progress -- see the interface section below) |
| `ctrl+d` | dry run: every gate, nothing executes |
| `ctrl+r` | re-plan the last request |
| `ctrl+l` | clear the conversation (keeps the persisted history -- leaves a marker, not a gap) |
| `ctrl+t` | new tab: an independent session |
| `ctrl+w` | close the active tab (refuses on the last tab, or a busy one) |
| `ctrl+tab` / `ctrl+shift+tab` | next / previous tab |
| `ctrl+o` | reopen a past session in a new tab |
| `f2` | choose this tab's model from a picker |
| `ctrl+q` | quit |
| `ctrl+c` | copy selected text (Textual's own built-in behavior; select with click-drag first) |

`f2` opens a picker listing "(automatic routing)", every locally
available chat-capable model, and a small hand-verified set of `openrouter/...`
models (tagged `remote, paid` so a tap doesn't trigger a paid call by
accident) -- pick one to pin the tab's next requests to it. Typing
`/model <name>` in the prompt does the same thing and is still the only
way to reach an `openrouter/...` model beyond that verified set, since
OpenRouter's full catalog isn't enumerated. Bare `/model` (or picking
"(automatic routing)") clears the override back to routing's own choice.
See **OpenRouter** below for the one built-in alternative to a local model.

`/auto on` skips the manual Run click for a plan with zero steps needing
confirmation -- a plan made entirely of SAFE/CAUTION steps runs the moment
it's proposed instead of waiting for a click. It never changes what needs
a tick in the first place: a DANGEROUS/CRITICAL step always waits for an
explicit approval regardless of this setting. `/auto off` turns it back
off (the default); bare `/auto` reports the current state without
toggling it. Per tab, and not persisted across a restart, same as
`/model`.

**`ctrl+c` copy not landing in your real clipboard?** Textual copies via an OSC 52
terminal escape sequence, which some terminals don't honor -- confirmed live on
GNOME Terminal (VTE), which drops it silently: the in-app selection highlights
fine, but nothing reaches the system clipboard. Workaround, terminal-native and
independent of Textual entirely: hold `Shift` while you click-drag to select
(bypasses the app's own mouse capture the same way it would in `vim` or `htop`),
then use your terminal's own copy shortcut -- on GNOME Terminal that's
`ctrl+shift+c` (plain `ctrl+c` there is reserved for the running program), or
middle-click to paste the X11 primary selection with no explicit copy step at all.

---

## The five gates

- **SAFETY_FIRST**: resolves what a command will actually run before classifying it.
- **TRANSPARENCY**: every branch torra takes is recorded with its reasoning in a JSON audit file.
- **VERIFY_STATE**: preconditions are rechecked between steps, not once before execution.
- **ROLLBACK_ON_FAIL**: undos are derived from what a step declares, armed just before that step runs.
- **ASK_CLARIFYING_QUESTIONS**: detects ambiguity by what a request is missing, not by how it is phrased.

Commands, task files, the full five gates, hardware-aware routing, OpenRouter, the second-machine executor, the site-knowledge library and telemetry are in [`docs/USAGE.md`](docs/USAGE.md).

---

## Layout

| Path | Holds |
|------|-------|
| `torra/core/` | steps, task specs, conflict detection, context, the Ollama client |
| `torra/gates/` | the five guardrails, one submodule each |
| `torra/agent.py` | the model loop |
| `torra/pipeline.py` | the seven stages |
| `torra/pipeline_types.py` | the data model `Torra` produces (`Stage`, `Outcome`, `Plan`, `RunResult`) |
| `torra/telemetry/` | what it records about itself |
| `torra/tui.py` | the interface: tabs, one session per tab |
| `torra/core/session.py` | `SessionStore` -- one persisted transcript per conversation |
| `torra/cli.py` | entry points |
| `torra/telemetry/crash.py` | `CrashReporter` -- unhandled exceptions, distinct from ordinary gated-task failures |
| `scripts/update.sh` | pull latest, reinstall, rerun the tests -- keeping this checkout current on your own machine (there's no server to deploy to) |
| `scripts/claude_code_hook.py` | a Claude Code `PreToolUse` hook: torra's classifier judges each Bash command Claude runs; CRITICAL asks in Claude Code's prompt, it never answers "allow", and verdicts needing a tick go to `~/.torra/claude_code/verdicts.jsonl` (`docs/INVARIANTS.md` §2.85). Wired in `~/.claude/settings.json` as a `Bash` matcher running `.venv/bin/python -I scripts/claude_code_hook.py` by absolute path |
| `tests/` | the test suite, organised by subsystem |
| `examples/` | runnable task files |
| `docs/os/` | reference material for the planning model, scoped to measured knowledge gaps — see `docs/os/README.md` |
| `docs/DESIGN.md` · `DECISIONS.md` · `STATUS.md` | the design as built · decisions with their why and parked work · the single owner of current status, measured numbers and known gaps |
| `docs/agents/` | topic files for agents editing this repo: `LOOP.md` (loop, Definition of Done), `GUARDRAILS.md`, `PLAYBOOKS.md`, `CONVENTIONS.md`, `knowledge_questions.yaml` |
| `AGENTS.md` · `CLAUDE.md` | agent instructions · `@AGENTS.md` only |
| `pyproject.toml` · `uv.lock` | package and tool config (ruff, mypy `--strict`, pytest) · locked dependencies |
| `.githooks/` · `LICENSE` | `pre-commit` refuses credentials and torra's own state files, `pre-push` scans the whole history with gitleaks (enable with `git config core.hooksPath .githooks`) · MIT |
| `.github/workflows/ci.yml` | ruff, ruff format, mypy, the tests under branch coverage with floors, and the defensive-coding scorecard |
| `docs/INVARIANTS.md` · `docs/LIVE_WIRES.md` | for whoever or whatever picks this up next -- what changed and why lives in each invariant's and live wire's own entry and in `git log`, not a separate changelog |
| `docs/agents/DEFENSIVE_CODING_STANDARDS.md` | how code here is graded; `scripts/dev/defensive_coding_report.py` computes it |
| `docs/reports/AUDIT_LOG.md` | weekly append-only log from the `torra-doc-audit` opencode routine (scheduled weekly) — greps doc citations, and a local-model agent judges any that don't resolve real-vs-false-positive. |
| `docs/reports/DEFENSIVE_SCORECARD_LOG.md` | monthly append-only log from the `torra-defensive-scorecard` opencode routine (scheduled monthly) — runs `scripts/dev/defensive_coding_report.py --enforce`, and only on a real regression has a local-model agent diagnose the likely commit from recent git history. |

---

## Status and measurements

The measured results (with the date and commit each was run at) and the known gaps live in
`docs/STATUS.md`, the single place that states what is true now.

---

## How it is built

torra is designed and directed by its owner. AI coding agents write the code,
and the test suite verifies their work. Up to commit `f9c0cd6` (2026-10-12),
158 of the 168 commits on `main` carry a `Co-Authored-By:` trailer naming a Claude model.
The other 10 carry no trailer, which says nothing either way about who wrote them.

---

## Running the tests

```bash
uv run pytest -q
```

The last recorded run is in `docs/STATUS.md`.
Nothing needs a live Ollama, systemd, or network. Every external dependency is
an injected callable, which is also how you wire it to the real thing.
