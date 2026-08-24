# harness

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
git clone https://github.com/antoinesavelli/harness.git ~/harness
cd ~/harness
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements-lock.txt
pip install -e . --no-deps
python3 -m pytest tests/ -q
```

Expect **482 passed**. Then:

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

---

## The interface

Chat on the left, plan on the right. Every proposed step shows its risk level,
why the classifier said so, and whether an undo exists.

Dangerous steps arrive with an **unticked** box. Safe steps get no box at all,
because asking for approval on `df -h` trains people to tick without reading,
which is how the one that mattered gets ticked too.

| Key | Does |
|-----|------|
| `enter` | send |
| `ctrl+d` | dry run: every gate, nothing executes |
| `ctrl+r` | re-plan the last request |
| `ctrl+l` | clear the conversation |
| `ctrl+q` | quit |
| `ctrl+c` | copy selected text (Textual's own built-in behavior; select with click-drag first) |

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

# Only needed for what the undo generator honestly cannot invert:
# migrations, dd, mkfs. Files, services, packages and mkdir are automatic.
rollback:
  - run: docker compose -f /srv/api/docker-compose.yml down
    description: stop the stack
    safe: true
```

Unknown step fields are a hard error. A typo in a document that drives root
shell commands should stop the run.

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

---

## Telemetry

Every request is recorded to `~/.harness/telemetry/tasks.jsonl`: the request
text with secrets redacted, what each gate decided, the model, the outcome.

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
| `harness/tui.py` | the interface |
| `harness/cli.py` | entry points |
| `tests/` | 482 cases, organised by subsystem |
| `examples/` | runnable task files |
| `docs/` | reference material for the planning model, scoped to measured knowledge gaps — see `docs/README.md` |
| `HANDOFF.md` | for whoever or whatever picks this up next |
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

1. **No model quality measurement.** The gates are measured exhaustively. Whether
   a local model writes *good plans* is completely unmeasured. Tests use a
   scripted backend, which proves the plumbing and nothing about judgement.
2. **No real traffic.** Every number above comes from synthetic data.
3. **Undo coverage ceiling.** Migrations, `dd` and `mkfs` need a declared
   `rollback:` entry. The plan pane names which steps those are.
4. **Tree watching misses a same-size same-mtime edit.** The declared-path
   SHA256 layer catches it, so `touches:` still matters.

---

## Running the tests

```bash
python3 -m pytest tests/ -v              # 482 cases
```

Nothing needs a live Ollama, systemd, or network. Every external dependency is
an injected callable, which is also how you wire it to the real thing.
