# torra

[![CI](https://github.com/antoinesavelli/torra/actions/workflows/ci.yml/badge.svg)](https://github.com/antoinesavelli/torra/actions/workflows/ci.yml)

A local model proposes. Five gates decide. You approve. Then it runs.

**Status: in progress.** A program the command classifier does not recognise
needs a tick. Three shell shapes that still ran such a program with no tick
(deeply nested `sh -c` or `$(...)`, `hash -p`, and an exported `PATH`,
`LD_PRELOAD` or `BASH_ENV`) were found on 2026-10-08 and closed on 2026-10-09,
each with a test (`HANDOFF.md` §2.75). No independent re-check has run since
(`GRADE_TO_A_PLAN.md` Phase 5); until one does, treat the classifier as unfinished.
The plan checks added on 2026-10-11 and 2026-10-12 (`HANDOFF.md` §2.78-2.81) have
met real models only in plan-only runs. All of it was pushed on 2026-10-13.

```bash
pip install -e .
torra doctor
torra
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

Needs Python 3.10+, Ollama running locally with at least one model pulled.

```bash
git clone https://github.com/antoinesavelli/torra.git ~/torra
cd ~/torra
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements-lock.txt
pip install -e . --no-deps
python3 -m pytest tests/ -q
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

`inotify_simple` is optional. With it, conflict detection checks only the paths
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

## Commands

```bash
torra                       # the interface
torra ask "..."             # one request: shows the plan, asks once, then each tick
torra plan "..."            # plan only, print it, run nothing
torra run task.yaml         # a hand-written task file, no model involved
torra check task.yaml       # classify a task file without running it
torra doctor                # environment check
torra evidence              # what torra has recorded about itself
torra label                 # label recorded traffic
```

Exit codes: **0** ok, **1** usage, **2** stopped by a gate or unapproved, **3** failed.

```bash
torra ask "restart ollama" || echo "stopped with $?"
```

`run` and `check` deliberately never touch the model. When it is down, or wrong,
or you already know exactly what you want, the gates stay available without it.

---

## `ask --verify`: a bounded coding/refactor loop

```bash
torra ask "add input validation to the login handler" \
    --verify "run pytest tests/test_login.py" \
    --max-iterations 3
```

After the request completes, the same check is repeated; if it fails, the
model is asked to fix the underlying issue and the check runs again, up to
`--max-iterations` fix attempts (default 3). This is not a second way to run
a command -- every round is an ordinary `plan()`/`gate()`/`execute()` turn on
the same conversation, exactly what a human retyping "that failed, fix it"
after reading real output would produce. It stops the moment any round isn't
COMPLETED (rejected, blocked, backend down, or the model asks a clarifying
question) -- an automated loop cannot safely answer for you, only a human
turn can. Each round prints its plan and asks "run this plan?" before it
runs; `--yes` skips that question for the first change and the checks,
never for a fix round, and never ticks a step. See `torra/iterate.py` for the implementation and
`CLAUDE_MIGRATION_ROADMAP.md` for why this is scoped the way it is.

---

## Task files

A task is a YAML document. `run` is both what executes and what the safety gate
classifies, so what gets checked is exactly what gets run.

```yaml
task_id: deploy-api-2026-07-28
request: deploy commit a1b2c3d to staging
watch_roots: [/srv/api]

steps:
  - id: pull
    kind: shell
    run: git -C /srv/api pull --ff-only
    estimated_seconds: 20
    touches: [/srv/api/docker-compose.yml]

  - id: up
    kind: deploy
    run: docker compose -f /srv/api/docker-compose.yml up -d
    estimated_seconds: 240

# Only needed for what the undo generator honestly cannot invert: a
# migration's own "down" semantics, or dd/mkfs against a raw device.
# Files (including dd/mkfs writing to one, via `touches` above), services,
# packages, and mkdir are automatic.
rollback:
  - run: docker compose -f /srv/api/docker-compose.yml down
    description: stop the stack
    safe: true
```

Unknown step fields are a hard error. A typo in a document that drives root
shell commands should stop the run.

`reads: [/path]` (the read counterpart to `touches`) declares a path a
step reads without modifying. Meaningful mainly for a model-generated
plan, not a hand-written one: once such a step completes for real, its
output enters the *model's own memory* of the conversation, tagged by
that path -- `torra run`/`torra check` never touch the model at all,
so declaring it on a hand-written step passes the schema check but has nothing
to feed.

If a run crashes partway through, re-running the identical task file skips
whatever steps already completed for real instead of redoing them --
checked against `task_id` and the step sequence, so an edited or
unrelated task file is never trusted as a continuation of a different one.

---

## The five gates

**SAFETY_FIRST.** Resolves what a command will actually run before classifying
it: quote-aware segment splitting, `VAR=value` substitution, wrapper unwrapping
(`sudo`, `env`, `timeout`, `nohup`, `nice`, `xargs`), recursion into `sh -c`/
`su -c` payloads and command substitutions (`$(...)`/backticks), base64
decode of blobs piped to a shell, and `$IFS` field-splitting obfuscation.
A known read-only program runs with no tick, a known writer runs flagged
CAUTION, and a program it does not recognise is DANGEROUS and needs a tick.
On 2026-10-09 all 31 cases in the evasion corpus (`tests/test_gates.py::TestSafety`)
classified DANGEROUS or CRITICAL, and none of its 31 routine commands needed a
tick. A fixed corpus is not proof of coverage: see the status note at the top.

**TRANSPARENCY.** Every branch torra takes is recorded with its reasoning
and lands in a JSON audit file. Reasoning is not optional; a decision logged
without one is flagged as a violation.

**VERIFY_STATE.** Preconditions are rechecked between steps, not once before
execution, with a TTL per check so an expensive probe is not re-run 200 times. A
service that is up when you approve and dead at step two stops the run.

**ROLLBACK_ON_FAIL.** Undos are derived from what a step declares: files in
`touches` are snapshotted, `systemctl` state is captured, `apt install` records
the package set, `mkdir` gets a removal that refuses to delete a directory
someone filled. A step's undo is armed just before that step runs, so a stop
reverses only steps that ran. Before any rollback, torra checks for files
changed outside it and skips their undo instead of restoring a snapshot over
the edit (`HANDOFF.md` §2.59, §2.71). After an undo runs, torra checks the
system and reports one of three states: `undone` (the check passed),
`unverified` (it ran, but the system is not back), `failed` (it raised).

**ASK_CLARIFYING_QUESTIONS.** Detects ambiguity by what a request is missing (a
path, unit, host, version, file or quantity), not by how it is phrased. On
2026-10-09 it flagged all 18 ambiguous requests in its test corpus and 1 of 10
specific ones. On real traffic the model asks far less often: see Known gaps.

---

## Hardware-aware routing

`ModelRouter` scores the request against a small set of intent rules (weighted, not
first-match) to pick a model, then checks it actually fits in whatever VRAM is free
right now -- Ollama silently falls back to CPU when a model doesn't fit, and reports
it only in `ollama ps`, which nobody reads before typing a request.

| Request mentions | Routed model | Why |
|---|---|---|
| debug, crash, traceback, root cause | `gpt-oss:20b` | deepest local reasoning budget (replaces qwen3:32b, then muse-glimmer:30b; measured best of 4 models 2026-08-28) |
| deploy, release, provision, systemd unit | `devstral:24b` | benchmarked best at tool/file-editing work |
| refactor, implement, patch, migrate code | `devstral-small-2:24b` | 65.8% SWE-bench Verified (replaces qwen3-coder:30b, 2026-08-28; still not benchmarked against this torra) |
| plan, architect, design, trade-off | `gpt-oss:20b` | long-context reasoning (replaces qwen3:32b, then muse-glimmer:30b; measured best of 4 models 2026-08-28) |
| analyze, summarize, review, audit | `gpt-oss:20b` | reasoning-bound, not tool-bound (replaces qwen3:32b, then muse-glimmer:30b; measured best of 4 models 2026-08-28) |
| list, show, status, "what is" | `devstral:24b` | measured live: qwen3.5:latest gave a content-free non-answer on 1 of 5 real disk/status checks |
| ambiguous (no clear target) | `gpt-oss:20b` | measured better at asking a clarifying question than guessing (replaces qwen3:32b, then muse-glimmer:30b; measured best of 4 models 2026-08-28) |
| none of the above | `devstral:24b` | the measured-reliable general default |

Every Qwen-origin model in the catalog was swapped for a non-Chinese equivalent on
2026-08-28 (operator request): `qwen3-coder:30b` → `devstral-small-2:24b` (Mistral AI),
`qwen3:32b` → `muse-glimmer:30b` (Meta) → `gpt-oss:20b` (OpenAI) after a real benchmark
run showed muse-glimmer tied on accuracy but ~6x the latency with a hard timeout,
`qwen3.5:latest` → `gpt-oss:20b` (the `OllamaClient` fallback default). `devstral:24b`
was already non-Chinese and is untouched.
See `torra/gates/router.py`'s `ROUTING_RULES` comment for the full reasoning and the
"not yet benchmarked against this torra" caveat.

Whatever gets picked still has to fit: if the free VRAM can't hold it, the router
downgrades to the next model that does, and says so in the routing reason -- never
a silent swap. Check what you are on:

```bash
ollama ps                                    # PROCESSOR column is the authority
nvidia-smi --query-gpu=memory.free --format=csv
journalctl --user -u ollama | grep -i vram   # why it fell back
```

A 32b model at q4 wants roughly 20 GB. Under that, it offloads.

---

## OpenRouter

Everything above is local by default -- nothing leaves the box unless you
explicitly ask it to. [OpenRouter](https://openrouter.ai) is the one built-in
exception: a paid, remote, OpenAI-compatible API proxying many hosted models.
`ModelRouter` never selects one on its own; it only activates when you name
one directly, because sending request text, clarifier gaps, and observed
machine state to a third party is a real trust-boundary change from
everything else here.

```bash
export OPENROUTER_API_KEY=...          # never a CLI arg, never persisted to disk
torra ask "..." --model openrouter/anthropic/claude-3.5-sonnet
```

Or put `OPENROUTER_API_KEY=...` in a `.env` file at the repo root --
`torra` loads it automatically on every command (`cli.py`'s
`_load_env_file`), no shell config needed. `.env` is already in
`.gitignore`; an explicit `export` in your shell always wins over
whatever's in the file.

In the TUI, `/model openrouter/anthropic/claude-3.5-sonnet` pins a tab to it;
bare `/model` clears the override back to local routing. Without
`OPENROUTER_API_KEY` set, neither path fails silently -- both report clearly
that OpenRouter isn't configured, not a confusing "model not found" the way
handing an unrecognized name straight to Ollama would.

---

## A second machine (executor)

If you have a second, SSH-reachable machine, the model can see real facts
about it -- hostname, kernel, uptime, disk, memory, GPU -- to reason about in
its answers, and can edit a file there through a narrow, controlled
mechanism. In both cases the model never authors an `ssh`/`scp` command
itself; torra constructs the real action.

```bash
export TORRA_EXECUTOR_HOST=10.0.0.44
export TORRA_EXECUTOR_USER=antoine
export TORRA_EXECUTOR_SSH_KEY=~/.ssh/id_ed25519_executor_link
```

Same `.env`/shell-export precedence as `OPENROUTER_API_KEY` above. Absent
`TORRA_EXECUTOR_HOST`, this is entirely off -- no SSH connection is ever
attempted, and nothing about local-only behavior changes (`HANDOFF.md`
§2.52).

**Editing a file there is pull-edit-push, never a raw remote command.** The
model proposes exactly three steps: pull the file to a local path it
chooses, an ordinary local edit step against that path (full undo/snapshot
coverage, unchanged), then push it back. The push step always needs an
explicit tick, regardless of what it looks like -- writing to a second,
less-guarded machine is always treated as torra's highest risk level,
unconditionally. Its undo snapshots the file's prior content over SSH
before the push runs; a failure later in the same run restores that
snapshot and checks the result, as local file undo does
(`HANDOFF.md` §2.53).

---

## Site-knowledge library

Point torra at a directory of Markdown docs about this specific machine
(hardware, services, storage layout, past fixes, quirks) and the model gets a
topic index of it on every turn -- so "where is the backup repo" gets answered
from what you have already written down instead of a guess or a clarifying
question:

```bash
export TORRA_KNOWLEDGE_DIR=/path/to/your/docs
```

This only gives the model *awareness* that the library exists, never a new
capability: reading a file still goes through an ordinary, gated plan step
(a `grep`/`cat` of an ordinary file, rated SAFE, so it runs without a tick)
-- nothing bypasses the five gates.
It also only reliably works with a capable-enough model behind it; the
smallest routed-to model does not consistently use it (measured in
`scripts/dev/model_bench/REPORT.md`).

The topic index is the directory's own top-level `.md` files, plus --
specifically -- `machines/<this machine's hostname>/*.md` if that folder
exists, since a library organized one machine per folder keeps its most
relevant content there rather than at the root. No other subdirectory is
indexed.

---

## Telemetry

Every request is recorded to `~/.torra/telemetry/tasks.jsonl`: the request
text with secrets redacted, what each gate decided, the model, the outcome.
`torra evidence` also reports a health summary aggregated from that same
file -- the full outcome breakdown (not just completions), local vs.
OpenRouter split, mean turn time, rollback verification rate -- and a crash
history from `~/.torra/telemetry/crashes.jsonl`: unhandled exceptions that
escaped every other guard, redacted the same way request text is, distinct
from ordinary gated-task failures (those are outcomes, not crashes).

```bash
torra label       # two minutes a week
torra evidence    # what torra has learned about itself
```

`drift()` and the loophole audit both refuse to report a rate below **30 labels**,
enforced in code. A false negative rate computed on a handful of samples is noise
wearing a percentage sign.

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
| `tests/` | the test suite, organised by subsystem |
| `examples/` | runnable task files |
| `docs/` | reference material for the planning model, scoped to measured knowledge gaps — see `docs/README.md` |
| `HANDOFF.md` | for whoever or whatever picks this up next -- what changed and why lives in its own §2 entries and in `git log`, not a separate changelog |
| `DEFENSIVE_CODING_STANDARDS.md` | how code here is graded; `scripts/dev/defensive_coding_report.py` computes it |
| `AUDIT_LOG.md` | weekly append-only log from the `torra-doc-audit` opencode routine (systemd user timer, Mon 06:50) — greps doc citations, and a local-model agent judges any that don't resolve real-vs-false-positive. Runner: `~/.local/share/platform-routines/` (a sibling to Paramo's own `paramo-routines/`). |
| `DEFENSIVE_SCORECARD_LOG.md` | monthly append-only log from the `torra-defensive-scorecard` opencode routine (systemd user timer, 1st, 07:35) — runs `scripts/dev/defensive_coding_report.py --enforce`, and only on a real regression has a local-model agent diagnose the likely commit from recent git history. |

---

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

---

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

---

## How it is built

torra is designed and directed by its owner. AI coding agents write the code,
and the test suite verifies their work. Up to commit `f9c0cd6` (2026-10-12),
158 of the 168 commits on `main` carry a `Co-Authored-By:` trailer naming a Claude model.
The other 10 carry no trailer, which says nothing either way about who wrote them.

---

## Running the tests

```bash
python3 -m pytest tests/ -q
```

On 2026-10-12 (commit `f9c0cd6`) this ran 1650 tests, all passing.
Nothing needs a live Ollama, systemd, or network. Every external dependency is
an injected callable, which is also how you wire it to the real thing.
