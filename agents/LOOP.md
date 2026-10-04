# The execution loop and the full Definition of Done

Detail behind the short Definition of Done in `AGENTS.md`.

## Execution Architecture

Every task follows this loop — no shortcuts. Notice it's the same shape torra enforces
on its own planning model (`converse → observe → propose → gate → present → execute →
report`): don't hold the code to a lower discipline than the code holds its model to.

```
OBSERVE → MODEL → DECIDE → ACT → VERIFY → CLOSE
```

1. **OBSERVE** — Read current state before acting: the relevant `README.md`, `HANDOFF.md` §2
   for any invariant the area touches, and the actual code — never edit from memory of a
   prior session or from `CORE_RULES.yaml`'s stale numbers.
2. **MODEL** — Identify blast radius. Is this on the safety-critical path (gates, the
   classifier, `Step.command`/`Step.run` construction, anything between "model proposes" and
   "shell executes") or peripheral (`cli.py`, `tui.py`, a docs file)? That answer decides how
   much rigor and how much asking-first this needs.
3. **DECIDE** — State the plan in one sentence. Ask if it touches the safety-critical path or
   persisted-state format; proceed if it's reversible and outside that path.
4. **ACT** — Smallest edit that satisfies the task. Parallel tool calls when independent.
   **Before defaulting to editing it yourself: is this mechanical/atomic-diff-sized, not a
   judgment call?** If so, check whether the on-disk local model should do it instead — see
   `system-library/AGENT_DOCTRINE.md`'s "Before you ACT: could the on-disk local model do this
   instead?" section and the `local-model-subagent` skill. This repo has no repo-specific coding
   agent of its own in `opencode.json` (just `instructions` pointing here), so that means the
   global `build`/`quick` agent names, not a Paramo-style named one.
5. **VERIFY** — Run the relevant tests; never skip. If the change touches classification,
   routing, or rollback, also re-read the specific `HANDOFF.md` §2 entry it's near and confirm
   the invariant the entry describes still holds, not just that tests are green (a test can be
   accidentally weakened in the same change that breaks what it guards).
6. **CLOSE** — Report what changed and what's next. No trailing narration.

### Definition of Done (applies to every change)

Not complete until all hold, in the *same* change:

1. **Tests run and pass.** `python3 -m pytest tests/ -q` — every test must pass and
   the count never drops. A change that adds behavior without adding a test
   for it isn't done; a change that *removes* a test to make the suite pass is very likely
   deleting the thing HANDOFF.md §2 told you not to delete — stop and re-read that section
   before doing it.
2. **Per-file docstring current.** One line + a `Depends on:` line for every touched module
   (see `DEFENSIVE_CODING_STANDARDS.md` §1.3). If you changed what a file does or imports, fix it.
3. **Area map current.** New/moved/renamed file → update that directory's `README.md` in the
   same change. New top-level directory → add a `README.md` and a row to the Area Maps table
   in `AGENTS.md`.
4. **`HANDOFF.md` §2 current** if the change touches an existing invariant, or adds a new one
   worth the same treatment (what breaks, why it's not obvious, which test guards it). A new
   safety-relevant invariant with no entry here is invisible to the next agent who needs it.
5. **Lint and types clean.** `ruff check .` on touched files; `mypy <touched files>` with zero
   new errors vs. the pre-change baseline.
6. **Defensive-coding scorecard held.** `python3 scripts/dev/defensive_coding_report.py` — no
   graded dimension drops a letter grade from the current baseline in `DEFENSIVE_CODING_STANDARDS.md`
   §2.1 (`ENFORCE_FLOORS` in the script is the enforced version of that baseline). If one drops,
   fix the regression in the same change — see that doc §2's extraction pattern for Modularity.
7. **`CORE_RULES.yaml` is not updated as a substitute for step 2.** If a default value changes,
   the docstring/`Depends on` and the actual code are what's authoritative; touching the YAML
   snapshot is optional cleanup, never a stand-in for documenting the real change.
8. **No version bumps, tags, or a `CHANGELOG.md` entry.** This is a solo, private tool with
   no external consumers of the package as an API — `pyproject.toml`'s `version` stays fixed
   at `0.0.0` (present only because PEP 621 requires the field), there is no `torra --version`
   flag, and there are no git tags to keep current. `git log`/GitHub's own commit and compare
   views are the record of what changed and when; `HANDOFF.md` §2 is the record of why. Don't
   reintroduce any of this without the user asking for it back explicitly.
