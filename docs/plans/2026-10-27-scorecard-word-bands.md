# Plan: the defensive-coding scorecard reports word bands, not A–F letters

Status: done
Owner decisions pending: none
Last verified: 33a934a on 2026-10-27
Spec decided by the owner on 2026-10-27 · Executor: an Opus orchestrator session

## Goal

The defensive-coding scorecard (`scripts/dev/defensive_coding_report.py`) prints A–F per dimension and a GPA,
the same vocabulary as the `/letter-grade` skill, and the owner keeps confusing the two. Done means the
scorecard prints word bands (`strong` / `ok` / `weak` / `poor` / `failing`) and a band mean, no A–F label and no
"GPA" anywhere in its output, with every threshold, exit code and flag unchanged. Paramo's canonical copy gets
the same spec in another session so both outputs match.

## Decisions (owner's, not re-decided here)

1. A → `strong`, B → `ok`, C → `weak`, D → `poor`, F → `failing`. Thresholds unchanged; only labels change.
   Functions and values are renamed away from "grade"/"letter" where cheap.
2. radon CC/MI ranks, wherever shown, map A → strong … D → poor, E/F → failing. torra's script shows no radon
   rank (it prints raw CCN numbers), so this item has nothing to change here.
3. Overall line: `OVERALL  <band>  (<n> strong, <n> ok, ...)  band mean 3.83 (floor 3.80)`. Band values
   4/3/2/1/0, overall band is the band nearest the mean as today, zero counts left out. `MIN_GPA` becomes
   `MIN_BAND_MEAN`, value 3.80.
4. `--enforce` exactly as strict as today: floors are band names, a dimension fails when its band value is below
   its floor's, the overall check compares the band mean with `MIN_BAND_MEAN`, messages use band words.
5. Past drift-log entries stay as written; one dated line records the mapping.

Format choice (this plan's, shared with Paramo's session through the notes folder): a dimension row is
`  {name:<14} {band:<8} {headline}` (no brackets); the overall row uses the same columns.

## Ask-first boundaries

The change stays inside the scorecard script, its test file and docs. Nothing under `torra/`, no CI change, no
floor lowered. The routine config for `torra-defensive-scorecard` lives outside this repo and is not edited.
A peer session (doc-arch) has uncommitted staged edits in several of the same files; only this plan's hunks
are committed, and the peer's staged hunks are left staged.

## Units

### U1: script and its tests
Files: `scripts/dev/defensive_coding_report.py`, `tests/test_defensive_coding_report.py`.
- Bands per decisions 1, 3, 4; `ENFORCE_FLOORS` values become band names; `MIN_BAND_MEAN = 3.80`.
- Enforcement moves into a pure function (`enforce_failures(results, band_mean)`) so it is testable.
- New tests: a dimension one band under its floor fails; a band mean under `MIN_BAND_MEAN` fails; the
  current floors pass; the overall line format (counts, zero counts omitted); `band()` threshold edges.
Done: `uv run pytest tests/test_defensive_coding_report.py -q` passes; the script's output has no `[A-F]`
label and no "GPA"; `--enforce` exits 0 on the current tree.

### U2: docs and drift log
Files: `docs/agents/DEFENSIVE_CODING_STANDARDS.md`, `docs/agents/LOOP.md`, `docs/agents/CONVENTIONS.md`,
`docs/STATUS.md`, `DEFENSIVE_SCORECARD_LOG.md`.
- Every reference to this scorecard's letters or GPA becomes bands / band mean; "drop a letter grade" becomes
  "drop a band". Drift-log past entries untouched; header updated; one dated mapping line added.
Done: `grep -n "GPA\|letter grade\|\[A\]\|\[B\]" <files>` finds only past drift-log entries.

### Orchestrator
`docs/DECISIONS.md` row, full checks, commit by explicit path (own hunks only).

## Checks

```
uv run pytest -q
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python scripts/dev/defensive_coding_report.py --verbose --enforce   # exit 0, as before
uv run python scripts/dev/defensive_coding_report.py | grep -nE '\[[A-F]\]|GPA'   # no match
```
