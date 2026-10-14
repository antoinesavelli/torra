# Plan: a knowledge layer an agent can use cheaply and trust

Written 2026-10-13, after a `~/.torra` state cleanup prompted the question "is `HANDOFF.md` the most
efficient way to hold what agents need to know?" **Status (2026-10-13): executing.** The owner accepted
K1 to K5 as recommended. The work runs on Claude subagents routed as the `subagent-fanout` skill now says:
readers on Sonnet at medium effort, builders on Sonnet at xhigh, verifiers on Opus at high. The model is Paramo's knowledge layer
(`/mnt/ParamoStorage/Paramo/docs/agents/KNOWLEDGE_ROUTING.md`), cut down to torra's size. Its own
tooling is about 2,300 lines; this plan budgets about 150.

Scope: the files an agent *editing torra* reads (`AGENTS.md`, `agents/`, `HANDOFF.md`, the root plan
and audit docs, the area `README.md`s). Out of scope: `docs/*.md`, which is OS knowledge for torra's
planning model (a different reader with its own measured-gap rule), the public `README.md`, and torra's
runtime behavior.

## 1. Baseline and target

Measured on 2026-10-13 at `309d59f`.

| What | Today | Target |
|---|---|---|
| Cost of reading `HANDOFF.md` whole, as its line 7 asks | 261 KB, about 65k tokens, more than the whole window of the 64k local model Paramo's eval used | Nobody reads it whole. Line 7 says to look up the § being touched. |
| Cost of looking up one rule | The §2 entry: median 36 lines (82 entries, 3,470 lines), with the rule mixed into its story | `grep -A3 '^### 2.NN'` returns the rule and its guard in 4 lines or fewer |
| `HANDOFF.md §x.y` references from code, tests and agent docs | 31 distinct, all resolve, nothing enforces it | All resolve, enforced by a test |
| Guard tests cited in `HANDOFF.md`, `AGENTS.md`, `agents/` | 110 distinct; 109 resolve (5 are deliberate `*`/`...` wildcards); **1 stale**: `HANDOFF.md:1113` cites `test_append_thinking_mounts_and_grows_a_live_reasoning_line`, now `test_append_thinking_grows_the_thinking_tab` | All resolve, enforced by a test |
| §5 live wires ("an obvious-looking change here is wrong") | About 20, 406 lines, unnumbered, cited from code 0 times | Numbered §5.n, and the code site points at its entry |
| Open work | Lives in both §6.5 and `GRADE_TO_A_PLAN.md` (at least 3 items: `reboot-required`, D8, the missing-path heuristic) | One owner per item; the other place links to it |
| Docs known to be wrong or closed but presented as live | `CORE_RULES.yaml` (loaded by nothing, drifts, still 6 references); `AGENTS.md` describes `AUDIT_FINDINGS.md` as "known live-wire bugs" though it is a closed 2026-07-29 audit; `RENAME_TO_TORRA.md` is done | Retired or described correctly |
| "Where does a new fact go?" | No answer anywhere; each session guesses | `agents/KNOWLEDGE.md`, one trigger row in `AGENTS.md` |
| Does a fresh agent actually find the right answer? | Unmeasured | A question set checked in CI (no model) and one recorded fresh-agent baseline |

Already right, and kept: every one of the 82 §2 entries names a guarding test (Paramo's `AGENTS.md`
machine-checks 4 of its invariants). The `AGENTS.md` invariant table puts the critical rules in the starting
context. `tests/test_instruction_files.py` already holds the root `AGENTS.md` to a 14,400-byte budget. It
fails right now on a peer session's uncommitted `AGENTS.md` edit (14,410 bytes; 14,131 committed), so the
guard works.

## 2. Decisions the owner takes first

| # | Decision | Recommendation | Blocks |
|---|---|---|---|
| K1 | Restructure `HANDOFF.md` in place, or split it into one file per entry. | **In place.** About 65 code citations read `HANDOFF.md §x.y`, and a split breaks every one for no gain in lookup cost once each entry starts with its rule. **No § number is ever reused or renumbered;** a retired entry keeps its number as a one-line stub. | P2.1, P2.2 |
| K2 | Which file owns open work: `HANDOFF.md` §6 or `GRADE_TO_A_PLAN.md`. | **Grade findings stay in `GRADE_TO_A_PLAN.md` until that plan closes; everything else is owned by §6.** Where an item is in both, one place states it and the other links to it by number. When the grade plan closes, its leftovers move to §6. | P2.3 |
| K3 | How a closed doc is retired: `git rm` (git history keeps it), or move it to an `archive/` folder in the repo. | **`git rm`**, with every reference updated in the same commit. Deleting files is ask-first; each file is listed for approval in P3.1. | P3.1 |
| K4 | §5 live-wire pointers mean comment-only edits in `torra/gates/` and `pipeline.py`, which are on the ask-first path. | **Yes**, as comment-only commits with no code change, reviewed as a diff. | P2.2 |
| K5 | How the fresh-agent eval runs. | **Manually, once, as a baseline**, with a capable model, at most 5 questions per batch. No schedule: Paramo's weekly unattended run scores 8 to 14 of 39 (`docs/eval/GRADE_LOG.md`), and 3 of its runs were flagged for opening the answer key or never running the grader, which is noise, not signal. | P4.3 |

## 3. Phases

Each item is one commit. Start each with `git status --short` and `git log --oneline -5`: peer sessions edit
`HANDOFF.md` and `AGENTS.md` often. Stage explicit paths only. Finish each with the full suite (count never
drops), `ruff check .` and `mypy` on any touched Python.

### Phase 0: guards before moves (tests only; act-freely)

These come first so that every later move is checked.

- **P0.1 References resolve.** One new test module, `tests/test_knowledge_refs.py`, about 80 lines:
  - every `HANDOFF.md §N.M` cited anywhere in `torra/`, `tests/`, `agents/`, `AGENTS.md` and the root
    `*.md` names a header that exists;
  - every `tests/<file>.py::<name>` cited in `HANDOFF.md`, `AGENTS.md` and `agents/` names a function or
    class that exists, where a name ending in `*` or `...` matches as a prefix;
  - every backticked repo path in `AGENTS.md`'s area-map table exists. The trigger-table targets are
    already checked by `test_instruction_files.py`.

  Executor: a Sonnet builder. Watch it fail on `HANDOFF.md:1113` first.
- **P0.2 Fix what P0.1 finds.** Today that is the one stale guard name and nothing else. Doc edits only.

Exit: suite green, test count up.

### Phase 1: routing (docs only)

- **P1.1 `agents/KNOWLEDGE.md`**, about 40 lines. It holds two tables:
  - **Registry (read side):** each kind of fact, the one place that owns it, and what verifies it. A
    blank in the last column means the fact is trust-based.
  - **Routing (write side):** what you just learned, where it goes, and where it must not go.

  First draft of the registry rows:

  | Fact | Owner | Verified by |
  |---|---|---|
  | Instruction-file shape, root byte budget | `AGENTS.md` | `test_instruction_files.py` |
  | The critical invariants, loaded at start | `AGENTS.md` invariant table | each row's test exists (P0.1) |
  | Full rule, its why, its story | `HANDOFF.md` §2 | refs and guards resolve (P0.1) |
  | "An obvious change here is wrong" | `HANDOFF.md` §5.n, pointed at from the code site | refs resolve (P0.1) |
  | Dead ends | `HANDOFF.md` §3 | trust-based |
  | Open work | per K2 | trust-based |
  | What a file does | module docstring and its area `README.md` | trust-based |
  | How code is graded | `DEFENSIVE_CODING_STANDARDS.md` | `defensive_coding_report.py` |
  | Cross-session context not in the code | the Claude memory directory | trust-based |
  | Whether a fresh agent answers correctly | `agents/knowledge_questions.yaml` | P4.2 |

  Index it in `agents/README.md`.
- **P1.2 `AGENTS.md`, budget-neutral.** Add a trigger row: "write down something you learned → read
  `agents/KNOWLEDGE.md`". In the same commit:
  - add an area-map row for this plan;
  - correct the `AUDIT_FINDINGS.md` description;
  - pay for the bytes by trimming elsewhere.

  Blocked until the peer's `AGENTS.md` edit lands back under 14,400 bytes.
- **P1.3 `HANDOFF.md` line 7.** "Read it before changing anything" becomes "look up the § your change
  touches; `grep -n '^### 2\.' HANDOFF.md` lists every rule". Do the same for any other instruction that
  sends an agent through the whole file.

Exit: `test_instruction_files.py` and P0.1 green; the new trigger resolves.

### Phase 2: `HANDOFF.md` in place (K1)

- **P2.1 Every §2 entry starts with its rule.** The line under each header becomes
  `**Rule:** <one sentence>. **Guard:** <test ids>.`, and the story stays below it unchanged.
  - Some headers are stories, not rules (for example §2.34, §2.40, §2.42 and §2.47). Those entries need
    the Rule line most.
  - Executor: a Sonnet builder, all 82 entries in order, then an Opus verifier over every Rule line.
  - Check per batch: each Guard line names only tests the entry's own "Guarded by" already names, which
    P0.1 verifies.
  - Measure afterwards: `grep -A1` output size for all 82 entries.
- **P2.2 Number §5 and point the code at it (K4).**
  - Number the live wires §5.1 to §5.n in their current order. Each later addition gets the next number;
    `GRADE_TO_A_PLAN.md` §3 already asks every fix to add one.
  - At each code site, add a comment-only line: `# Live wire: HANDOFF.md §5.n`.
  - Executor: a Sonnet builder working from a reader's site list (the owner's routing replaced the
    local model here). One commit for the numbering and one for the comments.
- **P2.3 One owner for open work (K2).** Dedupe §6.5 against `GRADE_TO_A_PLAN.md`; the 3 known overlaps
  are listed in section 1. A fixed §6 item shrinks to its pointer, which this file's own rule already
  requires.

Exit: P0.1 green, and no § number changed: the set of header numbers after is a superset of the set before.

### Phase 3: retire what misleads (ask-first, K3)

- **P3.1** List each candidate with its references for approval, then `git rm` it and update its
  references in the same commit. Candidates:
  - `CORE_RULES.yaml`: loaded by nothing, drifts, 6 references;
  - `RENAME_TO_TORRA.md`: marked done, 1 reference;
  - `AUDIT_FINDINGS.md`: a closed audit, 9 references. It could instead shrink to a §6 pointer.

  This plan joins the list when its own status reads done.

Exit: P0.1 green (no dangling references).

### Phase 4: measure

- **P4.1 `agents/knowledge_questions.yaml`**, 12 to 15 questions in Paramo's format (`id`, `question`,
  `must_contain`, `must_not_contain`, `sources`). Cover the facts most likely to rot, for example:
  - the three undo states;
  - what must happen before persisted state is deleted;
  - which file owns open work;
  - where a new OS fact goes;
  - the systemd scope of `ollama.service`.
- **P4.2 Check sources in CI.** About 40 lines of test, no model: every `must_contain` pattern appears in
  that question's `sources`. This catches a doc and its answer drifting apart. Port the format, not
  Paramo's 219-line script.
- **P4.3 One fresh-agent baseline (K5).** A capable model answers the questions without access to the
  answer key, at most 5 per batch. The score and the date are recorded under this section.

Exit: P4.2 green; the baseline is recorded.

## 4. Risks

- **Concurrent edits.** Peer sessions commit to `HANDOFF.md` and `AGENTS.md` daily (`309d59f` landed while
  this plan was being written). Keep each item small, re-read the file just before each edit, and stage
  explicit paths.
- **Renumbering breaks citations.** Prevented by K1's rule and checked by P0.1 and P2's exit check.
- **The `AGENTS.md` budget.** P1.2 must not grow the file; if trimming cannot pay for the row, the row
  waits.
- **Over-building.** The budget is about 150 lines of new test code in total. If an item needs more,
  stop and ask; Paramo's 979-line `audit_docs.py` is the cautionary example.
- **A question set that rots.** It is under the same rule it tests: change a fact, change its question in
  the same commit (P4.2 fails otherwise).
