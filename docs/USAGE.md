# torra usage reference

The command, task-file and routing reference, moved out of `README.md`.

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
`docs/plans/CLAUDE_MIGRATION_ROADMAP.md` for why this is scoped the way it is.

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
tick. A fixed corpus is not proof of coverage: see the status note at the top of README.md.

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
the edit (`docs/INVARIANTS.md` §2.59, §2.71). After an undo runs, torra checks the
system and reports one of three states: `undone` (the check passed),
`unverified` (it ran, but the system is not back), `failed` (it raised).

**ASK_CLARIFYING_QUESTIONS.** Detects ambiguity by what a request is missing (a
path, unit, host, version, file or quantity), not by how it is phrased. On
2026-10-09 it flagged all 18 ambiguous requests in its test corpus and 1 of 10
specific ones. On real traffic the model asks far less often: see the known gaps in `docs/STATUS.md`.

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
export TORRA_EXECUTOR_HOST=192.0.2.10
export TORRA_EXECUTOR_USER=user
export TORRA_EXECUTOR_SSH_KEY=~/.ssh/id_ed25519_torra
```

Same `.env`/shell-export precedence as `OPENROUTER_API_KEY` above. Absent
`TORRA_EXECUTOR_HOST`, this is entirely off -- no SSH connection is ever
attempted, and nothing about local-only behavior changes (`docs/INVARIANTS.md`
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
(`docs/INVARIANTS.md` §2.53).

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
