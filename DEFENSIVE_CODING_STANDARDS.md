# Defensive Coding Standards — a portable playbook

This document is a handoff from a sibling project (Paramo, a Python trading system) that
was brought from an ungraded state to a measured **Overall A, GPA 3.83** and has held that
line under continuous development since. It is not a style guide — it is the architecture
that makes a codebase *stout*: hard to silently regress, cheap to navigate, and safe for an
agent with no memory of the codebase to work in without an experienced human re-reviewing
every line. The goal here is to bring that same defensive posture to this torra project.

The core idea: **quality is not a vibe, it's a number, and the number is enforced.** Every
dimension below is measured by a script, given a letter grade, rolled into one GPA, and
gated in CI so a regression fails the build instead of surviving code review by accident.

---

## 0. The single most important idea: grade only what matters

A flat `radon cc` sweep or `ruff check` treats a 40-line one-shot CLI script the same as
the function that sizes a live order. That's wrong, and it also actively backfires: it
either (a) drowns real risk in noise from throwaway code, or (b) forces you to over-engineer
harmless glue code to keep a global number green.

Fix: **tier every file by actual blast radius** before grading anything.

| Tier | What lives here | Graded? |
|---|---|---|
| **critical** | the decision path — where a wrong value causes real-world harm (for a trading system: sizing, execution, screening; for this torra: the gates, the classifier, anything between "model proposes" and "shell executes") | Yes — heaviest weight |
| **core** | everything else load-bearing (orchestration, config, persistence, the model-facing loop) | Yes |
| **peripheral** | one-shot scripts, CLI/TUI glue, dashboards — not on the safety-relevant path | Listed for visibility, but **excluded from the grade** |

Classify with a simple prefix/suffix rule and encode it once in the scoring script. This one
design choice is what lets the rest of this document hold a *consistent* A instead of
oscillating with every new script.

**Implemented for this torra** in `scripts/dev/defensive_coding_report.py::tier_of`: a file
under `torra/gates/`, or `torra/core/tasks.py` (the `Step`/`TaskSpec` primitives the safety
gate classifies), or `torra/pipeline.py` (`Torra.execute`, the actual invocation point) is
**critical**. Everything else under `torra/` except `cli.py`/`tui.py`/`__init__.py` — the rest
of `torra/core/`, all of `torra/telemetry/`, `torra/agent.py` — is **core**. The three
named files are **peripheral**.

---

## 1. The six graded dimensions

Each dimension below is graded independently (A–F, mapped to a 4.0–0.0 GPA), then averaged
into one **Overall GPA** — the single number that tells you at a glance whether a change made
things worse. Thresholds are Paramo's actual, battle-tested numbers; treat them as a strong
starting point, not gospel — tune once, then don't touch them except to raise the bar.

### 1.1 Complexity (cyclomatic complexity / McCabe, via `radon cc`)

- **Hard ceiling: no function ships at CCN ≥ 15.** Below that, everything is allowed to exist,
  but is graded.
- **New code target: CCN ≤ 10** (McCabe's own original guidance — a habit, not a gate).
- **Existing 10–14 code: leave it alone** unless you're already editing that function *and*
  its complexity is genuinely tangled (see Modularity below) — a flat guard-clause chain, a
  dispatch table, or a ternary-heavy config builder can legitimately score 12–14 and be more
  readable *un-split* than split. Don't fragment code just to lower a number.
- Grade = % of decision-path functions at/above the CCN ceiling, banded at 1.5% / 4% / 8% / 15%
  (A/B/C/D; above 15% is F).

### 1.2 Modularity — two independent smells, not one

This is the dimension most people get wrong by conflating "long" with "bad." Split it into
two signals:

1. **File size**: a source file over **500 functional LOC** (blank lines/comments excluded)
   is a smell. Grade = count of decision-path (critical+core) files over that line, banded
   2% / 5% / 10% / 18% of the graded population.
   - A small number of files are allowed to be **deliberately** kept over 500 LOC (the true
     god-objects of the system — for a trading engine, both order executors and the strategy
     engine). Name them explicitly in this doc so nobody "fixes" them by accident. New
     functionality that isn't intrinsically theirs goes in a new collaborator module instead —
     never grown into an existing big file just because it's already there.
   - **For this torra, no file is currently named as an exception.** The original guess here
     was `pipeline.py`/`agent.py`; measurement showed the real offenders were the flat
     `gates.py` (1651 lines), `core.py` (1229), and `telemetry.py` (698) — each split into a
     same-named package (`gates/`, `core/`, `telemetry/`) of single-concern submodules, none
     over ~330 functional LOC. `pipeline.py` (`Torra`, the seven-stage orchestrator) stayed a
     single file at 437 functional LOC, under the cap without needing an exception. If a future
     change pushes any file over 500, extract before naming an exception — see the next bullet.
   - When a file *does* cross the line because of a real feature addition, the fix is
     extraction: pull a self-contained helper (a distinct phase, a parsing routine, a shared
     primitive) into its own module. This is cheap — usually a 10-minute mechanical move — and
     should be done in the same change that caused the regression, not deferred.
2. **Nested complexity**: a function with control-nesting depth ≥ 3 (an `if` inside a `for`
   inside an `if`) **that is also** CCN ≥ 10. This is deliberately narrower than "any deeply
   nested function" — depth alone is common and often fine (a validation cascade); depth
   *combined with* real branching complexity is what's actually hard to reason about. Grade
   the combination, not either signal alone, or you'll waste effort flattening harmless code.

**One-implementation rule** (applies whenever there's more than one "runtime" — live vs
backtest, CLI vs daemon, sync vs async): when choosing whether to split a file or duplicate
a chunk of logic into a second runtime's copy, the priority order is (1) one shared
implementation so the runtimes can't silently diverge, then (2) cohesion, then (3) file size.
**Never duplicate logic to keep a file small** — extract a shared module instead, even if
that creates a new small file for a modest amount of code.

### 1.3 Documentation — cheap convention, not verbose prose

- Every module gets **exactly one line** at the top describing what it does, plus a
  **`Depends on:`** list — the non-obvious collaborators (other modules, external services,
  config) a reader needs to know about before touching the file. This is the single highest-
  leverage documentation habit in the whole list: it turns "grep the whole repo to find what
  touches this" into "read four lines."
- Function docstrings: **one line, max.** No `Args:`/`Returns:` blocks — the signature and
  type hints already carry that. If a function needs more than one line to explain, that's a
  sign it needs a better name or a WHY-comment at the specific non-obvious point, not a
  docstring essay.
- **Comments explain WHY, never WHAT.** Default to zero comments; add one only for a hidden
  constraint, a workaround for a specific bug, an invariant, or a "why this default and not
  the obvious one" — anything a careful reader could not derive from well-named code. A
  comment restating what the next line does is a smell, not a courtesy.
- Grade = % of decision-path modules carrying a `Depends on` line, banded 90/75/55/35%.

### 1.4 Typing

- Every new or edited function gets full parameter and return type hints.
- `Any` is the **honest** annotation for genuinely untyped externals (a third-party SDK
  object, an injected logger/client handle) — use it rather than leaving a param bare, but
  never as an escape hatch to dodge a knowable type.
- If any real caller can pass `None`, the annotation is `X | None` — don't lie to the type
  checker to make it stop complaining.
- Grade = average of (% returns annotated, % params annotated), banded 90/75/60/45%.
- Enforce with `mypy` on touched files at minimum — "zero NEW errors vs the pre-change
  baseline" is a workable bar for a codebase that didn't start fully typed.

### 1.5 Testing

- **Every new module must be imported by at least one test file**, detected by scanning test
  files for actual `import`/`from ... import`/`patch()` targets — not by filename convention
  (`test_<name>.py` existing proves nothing if it doesn't import the module). A module only
  reachable indirectly through another class's facade still needs its own direct test.
- Grade = % of decision-path modules with at least one test-side import, banded 80/65/50/35%
  (or read straight `coverage` line-% if you're running with instrumentation — same bands).
- New behavior → new test, in the *same* change, not a follow-up. This is the single rule
  most likely to be skipped under time pressure, and the one whose absence compounds fastest.

### 1.6 Lint

- Run `ruff check .` (fast enough to run on every save, not just CI) and grade the raw issue
  count, banded 0/5/20/50 issues. Zero is the only acceptable steady state — this is the one
  dimension with no legitimate reason to carry debt.
- A workable starting rule set for a fresh Python project (adjust `known-first-party` to your
  package names):

  ```toml
  [tool.ruff]
  target-version = "py312"   # match your actual interpreter
  line-length = 100

  [tool.ruff.lint]
  select = [
      "E", "W",   # pycodestyle
      "F",        # pyflakes — undefined names, unused imports
      "I",        # isort — import ordering
      "B",        # bugbear — common real bugs, not just style
      "UP",       # pyupgrade — modernize syntax
      "C4",       # comprehension style
      "TID",      # tidy imports (no relative-import spaghetti)
  ]
  ignore = [
      "E501",  # line length — let the formatter own this, don't fight it with noqa
  ]
  ```

  Add project-specific safety rules where they exist — Paramo enforces `DTZ005`/`DTZ011`
  (naive `datetime.now()`/`date.today()`) on production code only, because an un-timezoned
  wall-clock read is a real bug class there but a non-issue in throwaway scripts. Look for
  your own equivalent: a mistake class specific to your domain that a lint rule can catch
  mechanically instead of relying on review.

---

## 2. Build the scorecard, don't eyeball it

All six dimensions above should be computed by **one script** that a human or agent can run
in seconds and get one clear verdict from — not six separate manual checks that quietly stop
happening under deadline pressure. The shape that worked:

```
$ python scripts/dev/defensive_coding_report.py

╔══ DEFENSIVE CODING SCORECARD ══════════════════════════════════╗
  Complexity      [A]  3 decision-path fns ≥ CCN 15 (0.3%), worst 17
  Documentation   [A]  100% carry 'Depends on', 100% have a module docstring
  Typing          [A]  returns 96%, params 95%
  Testing         [A]  97% modules import-covered by tests, 3,437 test fns
  Modularity      [B]  4 fns nested ≥ depth 3 & CCN ≥ 10, 6 decision-path files > 500 LOC
  Lint            [A]  0 ruff issue(s)
  ──────────────────────────────────────────────────────────────
  OVERALL         [A]  GPA 3.83  (decision-path weighted)
╚════════════════════════════════════════════════════════════════╝
```

Implementation notes that matter more than they look like they should:

- Built on `radon` (complexity + maintainability index), `ruff` (lint), stdlib `ast` (docstring/
  type-hint/import scanning), optionally `coverage` for line-%. All read-only — the script
  never edits anything.
- Print **named offenders**, not just percentages — a grade with no drill-down gets ignored the
  first time it's inconvenient. Show file:line, the tier, and the actual metric for the worst
  N offenders per dimension.
- Support scoping to a subpackage (`--area torra/gates`) so a reviewer of one PR isn't
  wading through repo-wide output.
- **CI enforcement**: encode last-known-good grades as floors and fail the build if a new
  commit drops below them.

  ```python
  ENFORCE_FLOORS = {
      "Complexity": "A", "Documentation": "A", "Typing": "A",
      "Testing": "A", "Modularity": "B", "Lint": "A",
  }
  MIN_GPA = 3.80  # set just under your actual baseline GPA
  ```

  Run with `--enforce` in CI: exit non-zero if any graded dimension falls below its floor, or
  overall GPA drops below the minimum. This is what turns "no dimension may drop a letter
  grade" from a wish into a build failure.

- **Raise floors when you improve; never lower one to turn a red gate green.** If a gate is
  genuinely wrong (flagging something correct), fix the gate's *logic*, and say so explicitly
  in the commit — a silent threshold relaxation is indistinguishable from cheating the check,
  and six months later nobody will remember which one it was.

### 2.1 This torra's actual first-run baseline

The example above is Paramo's. This is `torra/`'s, measured with
`python3 scripts/dev/defensive_coding_report.py --verbose` right after the `gates.py`/
`core.py`/`telemetry.py` → package split (§1.2.1) and the docstring/typing pass:

```
Complexity      [B]  7 decision-path fn(s) >= CCN 15 (2.7%), 257 checked
Documentation   [A]  17/17 carry 'Depends on:' (100%), 17/17 have a module docstring
Typing          [A]  returns 100%, params 100%
Testing         [A]  14/17 import-covered by tests (82%)
Modularity      [C]  12 fn(s) nested >= depth 3 & CCN >= 10, 0 decision-path file(s) > 500 LOC
Lint            [A]  0 ruff issue(s)
OVERALL         [A]  GPA 3.50 (decision-path weighted)
```

`ENFORCE_FLOORS` in the script is set to this run, **not** an all-`A` aspiration, because the
Complexity and Modularity offenders are exactly the case §1.1 warns against reflexively
splitting: `SafetyClassifier._judge` (CCN 46, one flat `if` per shell program it recognizes),
`Clarifier.detect` (CCN 26, one flat `if` per ambiguity check), and `Torra.execute` (CCN 29,
the seven-stage step loop) are dispatch/guard-clause/orchestration functions that read *worse*
fragmented than flat. `telemetry/` sits at 82% test coverage rather than 100% because no test
file imports it directly yet (a pre-existing gap this split surfaced, not introduced by it).

If you touch one of those functions and can make it more literally read as a table (a
dict-dispatch, a rule list) without losing the WHY-comments explaining each branch, that is
worth doing then; do not fragment it just to move the number.

**Update, 2026-08-11: this baseline was superseded, deliberately.** All nine CCN>=15
offenders in the codebase at the time (`SafetyClassifier._judge` included) were decomposed
into focused per-rule/per-check/per-phase helpers -- `_judge` now dispatches through a small
table of `(predicate, handler)` pairs where each handler still holds exactly one program's own
logic, not a table encoding risk levels or a class per program; `detect` and `execute` were
split the same way. `ENFORCE_FLOORS` now requires Complexity **A** (0 functions >= CCN 15) and
the file-size half of Modularity is also clean (`gates/rollback.py` split into `rollback.py` +
`undo.py`, both under 500 LOC). Modularity's floor stays **B**: its separate nested-complexity
signal (depth >= 3 AND CCN >= 10) still has real offenders, several of them legitimate linear
fallback cascades (`detect_accelerator`, `split_segments`) that are exactly the case this
section already argued shouldn't be fragmented. The lesson from the original baseline --
measure before assuming a number is fixable without a real structural change -- held: it took
an explicit decision to accept that cost, not a quick refactor, to move Complexity to A.

Beyond the generic six dimensions, every real system has a handful of rules that are worth
more than all the others combined — the ones where a violation doesn't just look bad, it
*breaks the product's core promise*. For a trading system that's "no look-ahead bias" and
"the position cap can only shrink a size, never expand it." For this torra, the obvious
candidates are things like:

- The command the safety gate classified **is** the command that executes (no path where a
  proposal is re-parsed or re-derived between gate and shell).
- A destructive/irreversible action never runs without an explicit human tick.
- The model process never gets direct shell access — every action travels through the same
  gated code path, no matter which module initiated it.

**Write these down explicitly** (a short table: invariant → where it's checked → what
enforces it), and for every one that *can* be checked mechanically, write an executable guard
— a small, fast test that asserts the invariant by inspecting code or behavior (an AST scan
for a forbidden call, a test that asserts two code paths can't diverge, a runtime assertion).
Mark which invariants are CI-guarded and which are still "trust-based" (nothing catches
drift automatically) — that list of trust-based invariants is exactly where your review
attention should concentrate, because it's where a mistake won't announce itself.

---

## 4. Cheap navigation: one-line area maps

Every top-level package/directory gets a `README.md` that is **not** documentation of the
code — it's an index: what's here, the 3–6 key files with a one-line description each, and
where to look for a given task. The point is token/attention economy: an agent (or a new
engineer) reads one small file instead of grepping the whole tree.

- Keep entries to one line each; link to the file for detail, don't duplicate its content.
- New directory → new `README.md`, same change.
- If a file's role changes enough that its one-line description goes stale, fix it in the
  *same* change that changed the role — a map that lies is worse than no map, because it's
  trusted by default.
- A doc-audit test that checks every listed file still exists and every area still has a map
  is cheap to write and catches rot before a human notices.

---

## 5. Fact ownership: one place per class of fact

Pick, for every *class* of fact that changes over time (which strategy/feature is "live" right
now, what a config flag does, current test-coverage numbers, the current invariant list), a
**single canonical owner** — one file or one generated report — and make every other
reference a *link* to that owner instead of a second restatement. This is the single biggest
lever against documentation rot: a fact stated in exactly one place can't go stale in two
places and disagree with itself.

Encode this as a small table if the project is big enough to need it: fact class → canonical
owner → what verifies it (a test, a CI check, or "trust-based" if nothing does). Reach for
this table whenever you're about to write down a fact and aren't sure where it belongs.

---

## 6. The guard-gaming trap (read this twice)

Every automated guard above can be satisfied two ways, and the wrong way is always more
tempting under time pressure:

- ✅ **Fix the cause** — the actual code/doc/test the guard exists to protect.
- ❌ **Weaken the guard** — relax a threshold, add an exemption, mark something "frozen",
  delete or soften a failing test, narrow a scope constant. This turns the indicator green
  while the problem it caught **persists and becomes invisible** — arguably worse than never
  having the guard, because now there's false confidence attached to it.

The test: *does the change make the guard MORE accurate, or LESS accurate?* A guard is
sometimes genuinely wrong (a false positive — flagging something that actually is fine); fixing
that is legitimate. A guard that's *right* — correctly flagging something broken — being
weakened to make a build pass is not. When in doubt, treat a red gate as correct by default,
and make any relaxation a visible, commented, deliberate decision — never a quiet tweak buried
in an unrelated diff.

---

## 7. Ask-first vs. act-freely — draw this line explicitly

Not every change carries the same risk, and treating them identically either slows down safe
work or rushes dangerous work. Draw an explicit boundary once, in writing:

**Act freely**: reading any file, editing tests, fixing lint/type errors, adding config that's
additive and gated behind a flag/default that preserves old behavior.

**Ask first**: anything touching the safety-critical decision path itself (for this torra:
the gates, the classifier, anything that decides what's allowed to execute), schema/format
changes to persisted state, deleting files or branches, anything that changes what happens
without an explicit human approval step.

This list is short by design — if everything requires asking, nobody asks; if nothing does,
the one time it mattered gets missed.

---

## 8. Definition of Done (make this literal, not aspirational)

A change is not finished until **all** of the following hold, checked in the *same* change:

1. Relevant tests run and pass; new behavior has a new test.
2. Every touched module's one-line docstring + `Depends on` is still accurate.
3. Any area map (`README.md`) touched by the change (new/moved/renamed file) is updated.
4. Any single-owner status fact the change alters is updated at its one canonical location.
5. `ruff check` clean on touched files.
6. The defensive-coding scorecard held — no dimension dropped a letter grade; if one did, fix
   the regression in the same change (see §2's extraction pattern for Modularity specifically).

Treat this exactly like Paramo's own recent history: a real feature addition (a new sizing
mode + a full-universe data seam) pushed one file 6 lines over the 500-LOC modularity cap,
which alone dropped the overall GPA from 3.83 to 3.67 and the letter grade from A to B→C.
The fix was a 15-minute extraction of two standalone helper functions into a new collaborator
module — done in the same commit, verified back to baseline before merging. That's the loop
this whole document describes: a regression is caught by a number, not a feeling, and fixed
before it ships, not filed as follow-up debt.

---

## 9. Minimal bootstrap for a fresh repo

If this torra project doesn't have these yet, the fastest path to the same posture:

```bash
pip install ruff mypy pytest pytest-cov radon coverage
```

1. Add the `[tool.ruff]` block from §1.6 to `pyproject.toml`; run `ruff check . --fix` once to
   clear the initial backlog.
2. Write a scoring script modeled on §2 — start with just Complexity + Lint + Testing (the
   three cheapest to compute and highest-signal), add Documentation/Typing/Modularity once
   those habits are established.
3. Tier your files (§0) before you grade anything — this is the step people skip and then
   wonder why the grade feels unfair.
4. Run the script once, record the resulting grades as your `ENFORCE_FLOORS` baseline, wire
   `--enforce` into CI.
5. Write the area-map `README.md` files (§4) for existing top-level packages — retrofitting
   this is cheap and immediately pays for itself on the next context-constrained agent session.
6. Only then start tightening thresholds — raise a floor when you clear it, never invent a
   stricter one nobody's actually hit yet.

### 9.1 Done for this repo

All six steps above are complete for `torra/`:

1. `[tool.ruff]` and `[tool.mypy]` live in `pyproject.toml`; `ruff check torra/ tests/` and
   `mypy torra/` are both clean.
2. `scripts/dev/defensive_coding_report.py` implements all six dimensions, not just three.
3. Tiering is implemented in `tier_of()` in that script; see §0's "Implemented for this torra".
4. `ENFORCE_FLOORS`/`MIN_GPA` in that script are the real baseline from §2.1, and
   `.github/workflows/ci.yml` runs `pytest`, `mypy`, and the scorecard with `--enforce` on every
   push and pull request.
5. Every package (`torra/`, `torra/gates/`, `torra/core/`, `torra/telemetry/`) plus
   `tests/`, `examples/`, and `docs/` has an area-map `README.md`.
6. Next: if a future change clears a floor (Complexity/Modularity's file-size signal already
   did, see §2.1's update), raise that dimension's floor in the same change — don't leave it
   slack. Modularity's nested-complexity signal is the next candidate, if a future change
   happens to touch one of its remaining offenders and can genuinely simplify it.
