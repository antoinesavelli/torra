# Task playbooks

Step lists for the recurring kinds of change.

## Task Playbooks

### Extending command classification (a new evasion, a new wrapper)
1. Add the case to `tests/test_gates.py::TestSafety`'s corpus first — this is benchmark-driven
   work, same discipline as `docs/`: measure the gap before closing it.
2. Implement the resolution logic in `harness/gates/safety.py`.
3. Confirm 20/20 (or the new total) still passes with 0 false positives on the routine-command set.
4. Update `HANDOFF.md` §2.1 only if the change affects the `Step.command`/`Step.run` equivalence.

### Adding a new gate or extending an existing one
1. Read `harness/gates/README.md` and the relevant `HANDOFF.md` §2 entries first.
2. Ask before changing behavior on the safety-critical path (see When to Ask vs. Act).
3. Wire it through `pipeline.py`'s seven stages — a gate that doesn't run inside
   `Harness.execute`'s existing stage sequence is a second code path (see HANDOFF §7, question 2).
4. Add the test, add the `HANDOFF.md` §2 entry, update `gates/README.md`.

### Adding OS domain knowledge to `docs/`
1. Run or point to a `scripts/dev/model_bench/` benchmark showing the actual gap (wrong, vague,
   or hallucinated) — not a guess.
2. New theme → new file; don't grow an existing one past its natural topic boundary.
3. Update `docs/README.md`'s table, including the "Measured gap?" column, in the same change.
4. Do not wire it into `harness.agent.SYSTEM_PROMPT` in this same change — that's a separate,
   reviewed change with its own test.

### Fixing a live-wire bug (`AUDIT_FINDINGS.md` pattern)
1. Reproduce the finding, don't take the writeup on faith — code drifts between when a finding
   is written and when it's picked up.
2. Fix the cause; write the regression test that would have caught it.
3. Remove (don't just leave stale) the finding's entry in `AUDIT_FINDINGS.md` once fixed, or
   mark it resolved with the commit — a findings doc that never shrinks stops being read.
4. If the fix touches an existing `HANDOFF.md` §2 invariant, update that entry.

### Before adding any feature (from `HANDOFF.md` §7 — ask in this order)
1. Does an existing gate already cover this?
2. Does this add a second code path (a way to do something the model path can't, or vice versa)?
3. Can you measure it before you build it, the way the clarifier corpus was built first and
   the routing benchmark exists before routing rules were trusted?
4. Would this add an item to an open-ended "list of things to detect"? (See `HANDOFF.md` §3 for
   what's already been tried and failed at that shape.)

If a change survives all four: add its test under the `tests/` subsystem it belongs to, not a
new top-level phase file — the old six-phase layout is exactly the structure this package was
consolidated out of.
