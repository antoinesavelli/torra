# Defensive-Coding Scorecard Drift Log

**Schedule:** Monthly (opencode routine `torra-defensive-scorecard`)
**What it checks:** runs `scripts/dev/defensive_coding_report.py --enforce` — the script's own
authoritative regression gate (fails if any dimension drops below its recorded last-known-good
grade, or overall GPA falls below `MIN_GPA`; see `DEFENSIVE_CODING_STANDARDS.md`). No LLM
judgment decides *whether* there was a regression — only *why*: when `--enforce` fails, an
opencode agent correlates the failure with recent commit history to point at a likely cause. A
clean `--enforce` run appends a bare pass entry with the scorecard attached, no agent turn.
**Append-only, newest run last** (a mechanical post-step does the actual write —
`torra-defensive-scorecard.post.sh` — never the agent itself).

---

### Run: 2026-09-04

**--enforce: no regression (all dimensions at or above their recorded floor).**

```
====================================================================
                     DEFENSIVE CODING SCORECARD                     
====================================================================
  Complexity     [A]  0 decision-path fn(s) >= CCN 15 (0.0%), 421 checked
  Documentation  [A]  29/29 carry 'Depends on:' (100%), 29/29 have a module docstring
  Typing         [A]  returns 100%, params 100%
  Testing        [A]  29/29 import-covered by tests (100%)
  Modularity     [B]  15 fn(s) nested >= depth 3 & CCN >= 10, 1 decision-path file(s) > 500 LOC
  Lint           [A]  0 ruff issue(s)
--------------------------------------------------------------------
  OVERALL        [A]  GPA 3.83 (decision-path weighted)
====================================================================
```

### Run: 2026-10-01

**--enforce: no regression (all dimensions at or above their recorded floor).**

```
====================================================================
                     DEFENSIVE CODING SCORECARD                     
====================================================================
  Complexity     [A]  0 decision-path fn(s) >= CCN 15 (0.0%), 421 checked
  Documentation  [A]  29/29 carry 'Depends on:' (100%), 29/29 have a module docstring
  Typing         [A]  returns 100%, params 100%
  Testing        [A]  29/29 import-covered by tests (100%)
  Modularity     [B]  15 fn(s) nested >= depth 3 & CCN >= 10, 1 decision-path file(s) > 500 LOC
  Lint           [A]  0 ruff issue(s)
--------------------------------------------------------------------
  OVERALL        [A]  GPA 3.83 (decision-path weighted)
====================================================================
```

### Run: 2026-10-03

**--enforce: no regression (all dimensions at or above their recorded floor).**

```
====================================================================
                     DEFENSIVE CODING SCORECARD                     
====================================================================
  Complexity     [A]  0 decision-path fn(s) >= CCN 15 (0.0%), 421 checked
  Documentation  [A]  29/29 carry 'Depends on:' (100%), 29/29 have a module docstring
  Typing         [A]  returns 100%, params 100%
  Testing        [A]  29/29 import-covered by tests (100%)
  Modularity     [B]  15 fn(s) nested >= depth 3 & CCN >= 10, 1 decision-path file(s) > 500 LOC
  Lint           [A]  0 ruff issue(s)
--------------------------------------------------------------------
  OVERALL        [A]  GPA 3.83 (decision-path weighted)
====================================================================
```
