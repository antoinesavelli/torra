# harness

[![CI](https://github.com/paramotrading/harness/actions/workflows/ci.yml/badge.svg)](https://github.com/paramotrading/harness/actions/workflows/ci.yml)

A local model proposes. Five gates decide. You approve. Then it runs.

```bash
pip install -e .
harness doctor
harness
```

---

## What it is

You describe what you want in plain language. A model on your own machine reads
the actual state of the box, writes a plan, and hands it to five guardrails that
resolve every command down to argv, classify it, snapshot the files it touches,
and refuse to run anything dangerous without an explicit tick from you.

The model never gets a shell. It writes a proposal into the same structure a
hand-written task file uses, and that proposal travels the exact code path the
test suite covers. There is no second path.

```
converse   the model reads your request, consults the clarifier for what is
           structurally missing, and answers, asks, or proposes
observe    real machine state, read-only, before any proposal exists
propose    a validated task spec
gate       safety classification, preconditions, undo generation, conflict baseline
present    you see the plan AND every gate verdict, and approve per step
execute    with rollback armed and preconditions rechecked between steps
report     back into the conversation, and into telemetry
```

---

## Install

Needs Python 3.10+, Ollama running locally with at least one model pulled.

```bash
git clone https://github.com/paramotrading/harness.git ~/harness
cd ~/harness
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements-lock.txt
pip install -e . --no-deps
python3 -m pytest tests/ -q
```

Expect **712 passed**. Then:

```bash
harness doctor     # environment check
harness            # the interface
```

Put it on PATH permanently:

```bash
echo "alias harness='~/harness/.venv/bin/harness'" >> ~/.bashrc && source ~/.bashrc
```

`inotify_simple` is optional and makes tree watching about 5,000x cheaper.
`textual` is required for the interface but not for `run` or `check`.

To update an existing checkout later: `scripts/update.sh` -- pulls latest
`main`, reinstalls, and reruns the test suite as the final gate. Refuses to
run over uncommitted local changes.

---

## The interface

Chat on the left, a sidebar on the right. The sidebar has five tabs, in
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
  `harness evidence` prints, recomputed each time you switch to it.

Once you approve a plan, each step's real stdout/stderr streams live
into the chat as it runs, tagged with the step id -- the only place a
successful step's output is ever shown, so unlike the reasoning stream
it stays in the transcript after the run finishes.

Dangerous steps arrive with an **unticked** box, each with a description of
*why* it's there -- what it confirms, what it depends on from an earlier
step, what it sets up for a later one -- not just the bare command. Safe
steps get no box at all, because asking for approval on `df -h` trains
people to tick without reading, which is how the one that mattered gets
ticked too.

Ticking some dangerous steps and leaving others declined runs the ticked
ones and skips the rest -- it no longer rejects the whole plan over one
declined step. If an approved step's command looks like it depends on a
declined one (references a path the declined step would have produced),
you'll see a warning before anything runs; it's a best-effort hint; not a
guarantee, since the harness has no real model of dependencies between
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
harness                       # the interface
harness ask "..."             # one request, approve at the prompt
harness plan "..."            # plan only, print it, run nothing
harness run task.yaml         # a hand-written task file, no model involved
harness check task.yaml       # classify a task file without running it
harness doctor                # environment check
harness evidence              # what the harness has recorded about itself
harness label                 # label recorded traffic
```

Exit codes: **0** ok, **1** usage, **2** stopped by a gate or unapproved, **3** failed.

```bash
harness ask "restart ollama" || echo "stopped with $?"
```

`run` and `check` deliberately never touch the model. When it is down, or wrong,
or you already know exactly what you want, the gates stay available without it.

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
that path -- `harness run`/`harness check` never touch the model at all,
so declaring it on a hand-written step validates cleanly but has nothing
to feed.

If a run crashes partway through, re-running the identical task file skips
whatever steps already completed for real instead of redoing them --
validated against `task_id` and the step sequence, so an edited or
unrelated task file is never trusted as a continuation of a different one.

---

## The five gates

**SAFETY_FIRST.** Resolves what a command will actually run before classifying
it: quote-aware segment splitting, `VAR=value` substitution, wrapper unwrapping
(`sudo`, `env`, `timeout`, `nohup`, `nice`, `xargs`), recursion into `sh -c`/
`su -c` payloads and command substitutions (`$(...)`/backticks), base64
decode of blobs piped to a shell, and `$IFS` field-splitting obfuscation.
Regex over raw text missed five of twelve evasions in testing. This catches
26 out of 26 with no false positives on 27 routine commands.

**TRANSPARENCY.** Every branch the harness takes is recorded with its reasoning
and lands in a JSON audit file. Reasoning is not optional; a decision logged
without one is flagged as a violation.

**VERIFY_STATE.** Preconditions are rechecked between steps, not once before
execution, with a TTL per check so an expensive probe is not re-run 200 times. A
service that is up when you approve and dead at step two stops the run.

**ROLLBACK_ON_FAIL.** Undos are derived from what a step declares, not hoped for:
files in `touches` are snapshotted, `systemctl` state is captured, `apt install`
records the package set, `mkdir` gets a removal that refuses to delete a
directory someone filled. Each undo then has to **prove** it worked. Three
states, not two: `undone` (verified), `unverified` (ran, but the system is not
back), `failed` (raised).

**ASK_CLARIFYING_QUESTIONS.** Detects ambiguity by what a request is missing, not
by how it is phrased. Two closed-vocabulary approaches failed measurement first:
a list of vague phrasings scored 83% false negatives on a blind corpus, a list of
mutating verbs scored 50%. Asking "is there a concrete anchor" is verb-agnostic
and measured 0% false negatives on a corpus it was never tuned against.

---

## Hardware-aware routing

`ModelRouter` scores the request against a small set of intent rules (weighted, not
first-match) to pick a model, then checks it actually fits in whatever VRAM is free
right now -- Ollama silently falls back to CPU when a model doesn't fit, and reports
it only in `ollama ps`, which nobody reads before typing a request.

| Request mentions | Routed model | Why |
|---|---|---|
| debug, crash, traceback, root cause | `qwen3:32b` | deepest local reasoning budget |
| deploy, release, provision, systemd unit | `devstral:24b` | benchmarked best at tool/file-editing work |
| refactor, implement, patch, migrate code | `qwen3-coder:30b` | code-generation specialist (not yet benchmarked here) |
| plan, architect, design, trade-off | `qwen3:32b` | long-context reasoning |
| analyze, summarize, review, audit | `qwen3:32b` | reasoning-bound, not tool-bound |
| list, show, status, "what is" | `devstral:24b` | measured live: qwen3.5:latest gave a content-free non-answer on 1 of 5 real disk/status checks |
| ambiguous (no clear target) | `qwen3:32b` | measured better at asking a clarifying question than guessing |
| none of the above | `devstral:24b` | the measured-reliable general default |

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
harness ask "..." --model openrouter/anthropic/claude-3.5-sonnet
```

Or copy `.env.example` to `.env` at the repo root and set it there --
`harness` loads it automatically on every command (`cli.py`'s
`_load_env_file`), no shell config needed. `.env` is already in
`.gitignore`; an explicit `export` in your shell always wins over
whatever's in the file.

In the TUI, `/model openrouter/anthropic/claude-3.5-sonnet` pins a tab to it;
bare `/model` clears the override back to local routing. Without
`OPENROUTER_API_KEY` set, neither path fails silently -- both report clearly
that OpenRouter isn't configured, not a confusing "model not found" the way
handing an unrecognized name straight to Ollama would.

---

## Site-knowledge library

Point the harness at a directory of Markdown docs about this specific machine
(hardware, services, storage layout, past fixes, quirks) and the model gets a
topic index of it on every turn -- so "where is the backup repo" gets answered
from what you have already written down instead of a guess or a clarifying
question:

```bash
export HARNESS_KNOWLEDGE_DIR=/path/to/your/docs
```

This only gives the model *awareness* that the library exists, never a new
capability: reading a file still goes through an ordinary, gated plan step
(a `grep`/`cat`, always SAFE, auto-run) -- nothing bypasses the five gates.
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

Every request is recorded to `~/.harness/telemetry/tasks.jsonl`: the request
text with secrets redacted, what each gate decided, the model, the outcome.
`harness evidence` also reports a health summary aggregated from that same
file -- the full outcome breakdown (not just completions), local vs.
OpenRouter split, mean turn time, rollback verification rate -- and a crash
history from `~/.harness/telemetry/crashes.jsonl`: unhandled exceptions that
escaped every other guard, redacted the same way request text is, distinct
from ordinary gated-task failures (those are outcomes, not crashes).

```bash
harness label       # two minutes a week
harness evidence    # what the harness has learned about itself
```

`drift()` and the loophole audit both refuse to report a rate below **30 labels**,
enforced in code. A false negative rate computed on a handful of samples is noise
wearing a percentage sign.

---

## Layout

| Path | Holds |
|------|-------|
| `harness/core/` | steps, task specs, conflict detection, context, the Ollama client |
| `harness/gates/` | the five guardrails, one submodule each |
| `harness/agent.py` | the model loop |
| `harness/pipeline.py` | the seven stages |
| `harness/pipeline_types.py` | the data model `Harness` produces (`Stage`, `Outcome`, `Plan`, `RunResult`) |
| `harness/telemetry/` | what it records about itself |
| `harness/tui.py` | the interface: tabs, one session per tab |
| `harness/core/session.py` | `SessionStore` -- one persisted transcript per conversation |
| `harness/cli.py` | entry points |
| `harness/telemetry/crash.py` | `CrashReporter` -- unhandled exceptions, distinct from ordinary gated-task failures |
| `scripts/update.sh` | pull latest, reinstall, rerun the tests -- keeping this checkout current on your own machine (there's no server to deploy to) |
| `tests/` | 712 cases, organised by subsystem |
| `examples/` | runnable task files |
| `docs/` | reference material for the planning model, scoped to measured knowledge gaps — see `docs/README.md` |
| `HANDOFF.md` | for whoever or whatever picks this up next -- what changed and why lives in its own §2 entries and in `git log`, not a separate changelog |
| `DEFENSIVE_CODING_STANDARDS.md` | how code here is graded; `scripts/dev/defensive_coding_report.py` computes it |

---

## Where the numbers come from

| Claim | Measured how |
|---|---|
| 26/26 evasions caught, 0 false positives | `tests/test_gates.py::TestSafety` |
| Clarifier 0% false negatives | `tests/test_gates.py::TestClarifier` |

Every one of those came from data generated in testing, not from real use. That
is the single largest caveat on this whole project and it is not resolved.

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
   open.** The gates are measured exhaustively; tests still use a scripted
   backend and prove nothing about judgement on their own. But real traffic
   now flows through telemetry and gets hand-labeled (`harness label`), and
   that measurement found the model stops and asks for only ~18% of requests
   a human calls genuinely ambiguous (82% live false-negative rate against a
   0% synthetic baseline). Whether a routing fix already found along the way
   closes that gap isn't confirmed yet -- too little traffic has run since.
   One related failure shape is fixed: a question the harness could answer
   itself with a safe read-only command (`lsblk`, `systemctl --failed`, ...)
   used to get deflected back to "please run that yourself" instead of the
   harness just running it -- live A/B tested against the real model before
   shipping. See `HANDOFF.md` §6.1 and §2.40.
2. **Real traffic exists, and it already found two real bugs.** No longer a
   gap to close -- 143 recorded tasks and counting, 91 hand-labeled, and that
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

---

## Running the tests

```bash
python3 -m pytest tests/ -v              # 712 cases
```

Nothing needs a live Ollama, systemd, or network. Every external dependency is
an injected callable, which is also how you wire it to the real thing.
