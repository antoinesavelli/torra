# Plan: P5.5, a listed program's options are allowed, not denied

Status: active
Owner decisions pending: none (O1 to O3 answered 2026-11-18, each the recommended option); G0's yes to edit `torra/gates/**` is still to come
Last verified: 6adef17 on 2026-11-18
Executor: an Opus orchestrator session; Sonnet builds each unit, a fresh Opus verifies it

## 1. Goal

Close P5.5 (`docs/plans/GRADE_TO_A_PLAN.md`, Phase 5 result of 2026-11-18). Today a per-program judge calls a listed
program read-only unless a flag from a short-letter set or a long-name list shows (`_avoiding`,
`torra/gates/program_checks.py:94`). Long forms, getopt abbreviations, attached values and options hidden inside
another option's value slip past it, so `curl -w '%output{~/.bashrc}x'`, `tar -tf a.tar --index-file=F`,
`awk -W exec F`, `gawk -oF`, `ethtool --flash`, `pip list --log=F` and `sed --e='1w F'` run with no tick.

Done when every P5.5 evidence command asks, routine read-only work stays SAFE (measured on real traffic, section 5),
and the fix is structural: the next unlisted option asks instead of running.

## 2. Real state (read 2026-11-18 at `6adef17`)

- `known_programs.trust()` calls `_lookup`: `READ_ONLY_PROGRAMS` (whatever the arguments), then `PREDICATES`, then
  `SUBCOMMANDS`, then `WRITER_PROGRAMS`; no answer means ask (§2.60).
- **Three ways to judge flags, all letter-set based:** `_avoiding` (deny list, 15 programs in
  `program_tables.py:112`), `_positive` and `_listing_only` (allow lists, `system_checks.py:93`, `:155`). All three
  read `_short()`, which splits **every** letter of a cluster, so an attached value (`-n50`, `-o/x`) gets read as
  flags. That is why an allowlist cannot simply reuse `_short()`: it would ask on `journalctl -n50`.
- `_curl` exempts `-o /dev/null` by deleting every `-o`/`--output` once the separate-token ones all name
  `/dev/null`, so `-o /dev/null --output=F` passes (`program_checks.py:212-220`). `-w` is never read.
- `_awk` denies `fiEl` and four exact long names; mawk's `-W` and gawk's `-o -p -d` and their long forms are not
  known (`:206`). `_sed_scripts` takes an exact `--expression=` only (`:191`).
- `SUBCOMMANDS` judges only the subcommand, never the options around it: `pip list --log=F` is SAFE.
- The Claude Code hook asks only at CRITICAL (D-005), so a SAFE command that becomes DANGEROUS here costs a torra
  tick, not a Claude Code prompt. The cost of a false positive falls on `torra ask` and `torra run` plans.
- Scorecard: radon adds a closure's CCN to its factory, and a decision-path file over 500 functional LOC drops
  Modularity to C (memory: scorecard gotchas). `program_checks.py` is 356 lines; new parsing goes in a new module.

## 3. Decisions for the owner

Answered by the owner on 2026-11-18: O1 allow list, O2 flag judges first, O3 no unexplained moves.

**O1. Allow list, or patch the listed holes.** *Recommended: allow list.* Each listed program gets a spec of its
known read-only options (short letters, marked if they take a value, and long names with their argument kind),
parsed the way getopt parses them: clusters, attached values, `--name=value`, `--`, and a long abbreviation
accepted only when it is a unique prefix of a known name. Any option outside the spec asks. This replaces
`_avoiding`, `_positive` and `_listing_only` with one judge, so it deletes code too. Patching only the evidence
commands is smaller but is the open-ended detect-list shape `docs/DECISIONS.md` rejects (R-2, R-3, R-11): the
next red-team finds the next option.

**O2. How far Phase 1 reaches.** *Recommended:* the 15 `_avoiding` programs, the `_positive`/`_listing_only`
ones, and `curl`, `tar`, `sed`, `awk`/`gawk`/`mawk`. `SUBCOMMANDS` (the `pip --log` case: options around a
subcommand) is Phase 2, behind its own gate, because systemctl, apt and snap carry many options and their spec
should be sized from the replay first. `READ_ONLY_PROGRAMS` stays as is (the 2026-10-13 pass checked it against
each program's write options).

**O3. False-positive bar.** *Recommended:* the replay (section 5) moves no command from SAFE to asking unless the
move is one of P5.5's write or run forms; any other move is fixed by widening that program's spec before the
phase closes. The alternative is a numeric budget (for example under 0.5% of replayed SAFE commands).

## 4. Ask-first boundaries

Editing `torra/gates/**` is ask-first (`AGENTS.md`); the owner's yes at G0 covers Phases 0 and 1 only, and G1
covers Phase 2. Never: soften a test in the must-ask lists, lower a coverage or scorecard floor, push. No change to
`Step.command`/`Step.run`, `pipeline.py`, persisted formats, or the hook's thresholds.

## 5. Measurement: the replay

Every Bash command in the Claude Code transcripts (`~/.claude/projects/*/*.jsonl`) plus every `proposed_steps`
command in `~/.torra/telemetry/tasks.jsonl`, deduplicated, classified at the base commit and again after each
phase. Report: counts per level, and every command whose level changed, grouped by program. Commands are
classified in-process only; nothing runs. Output stays in the scratchpad (it may hold private paths); only counts
and the program-level summary go into docs.

## 6. Phases and units

### Phase 0: corpus and baseline (no gate code touched)

- **U0 corpus** (Sonnet). `tests/test_known_programs.py` only. Add every P5.5 evidence command (the list under
  "Evidence" in the plan's P5.5 entry, including the three `sudo` ones) to `ASKS`, and `curl -s -o /dev/null -w
  '%{http_code}' URL`, `tar -tf a.tar`, `awk '{print $1}' f`, `ethtool eth0`, `pip list` and `journalctl -n50 -u x
  --no-pager` to `READ_ONLY_COMMANDS`. Leave the `pip list --log=F` case marked `xfail(strict=True)` with reason
  "Phase 2"; every other new must-ask case fails today and the rest of the suite stays green. Check:
  `uv run pytest tests/test_known_programs.py -q` shows exactly the new must-ask cases failing.
- **U1 replay tool** (Sonnet, parallel with U0). `scripts/dev/replay_verdicts.py` plus
  `tests/test_replay_verdicts.py`. The tool takes `--out FILE` (JSONL of command and level) and `--diff A B`.
  It reads transcripts and telemetry through injected paths, so the test uses fixtures, never `~`. Check: its test
  passes, and a baseline run at `6adef17` lands in the scratchpad with its counts reported.

**Gate G0 (owner):** the baseline counts seen, then a yes to edit `torra/gates/**` (O1 to O3 are answered).

### Phase 1: one option parser, every flag judge on it

- **U2 parser** (Sonnet). New `torra/gates/options.py` plus `tests/test_options.py`. `OptionSpec` (short letters
  with value marks, long names with none, required or optional argument), `parse(argv, spec)`: the options and
  values in order plus the operands, or None on any unknown, ambiguous or malformed option. A factory
  `allowing(spec, value_ok=None, needs=None)` returns READ_ONLY or None, with `value_ok` a per-option value check
  (a module function, not a closure, per the CCN gotcha). Tests cover clusters, attached values (`-n50`),
  `--x=v`, `--x v`, the unique-prefix rule, an ambiguous prefix, `--` and optional arguments (`gawk -p`,
  `--profile[=F]`). Check: `uv run pytest tests/test_options.py -q`, `uv run mypy`.
- **U3 table judges** (Sonnet, after U2). `program_tables.py` and `system_checks.py` only. Convert every
  `_avoiding`, `_positive` and `_listing_only` entry to an `allowing` spec of that program's read-only options
  (from `man`/`--help` on this box, cited in a comment once per program), then delete `_positive` and
  `_listing_only`. Check: the U0 cases for those programs pass, and the whole suite except the U4 cases stays
  green.
- **U4 curl, tar, sed, awk** (Sonnet, after U2, parallel with U3). `program_checks.py` only. The four judges move to
  `options.parse`. curl: every `-o` spelling must name `/dev/null` for the exemption, and a `-w`/`--write-out`
  value containing `%output{` asks. tar: `--index-file` and every unlisted long form ask. awk: `-W`, gawk's
  `-o -p -d` and their long forms ask, with long names matched under the prefix rule. sed: every `-e`/`--expression`
  spelling feeds the script check. Delete `_avoiding`, `_hits` and `_CURL_LONG_WRITES`/`_TAR_UNSAFE_LONG` once
  nothing imports them. Check: every U0 case passes except the Phase 2 xfail; `defensive_coding_report.py
  --enforce` holds.

Each unit's verifier is a fresh Opus given this section, the unit's diff, and the instruction to break it: getopt
spellings the unit did not list, `sudo` and `env` wrappers, quoting, and attached values.

**Integration (orchestrator):** full Definition of Done, then the replay against the baseline. Each move goes
through O3; widening a spec goes back to the unit's builder.

**Gate G1 (owner):** the replay's moves are seen; a yes for Phase 2.

### Phase 2: options around a subcommand

- **U5** (Sonnet). `known_programs.py` and `program_tables.py`. Each `SUBCOMMANDS` entry gets an option spec for
  its global and read-subcommand options; an unknown option asks. Spec sizes come from the replay's real usage.
  The U0 `pip --log` xfail now passes and becomes a plain case. Same verification and replay.

### Phase 3: records

- **U6 docs** (Sonnet). Add a `docs/INVARIANTS.md` entry for the new invariant ("a listed program's options are
  allowed by spec; an unknown option asks", guarded by `tests/test_options.py` and the U0 cases) within the byte
  ceiling, plus a `docs/DECISIONS.md` row for O1. Shrink P5.5 in `GRADE_TO_A_PLAN.md` to a pointer, and update the
  `docs/STATUS.md` line, the `torra/gates/README.md` row and the `PLAYBOOKS.md` file names (`safety_resolve.py`
  and `safety_judge.py` hold rules, but per-program flags now live in `options.py`). Check:
  `uv run pytest tests/test_knowledge_refs.py -q`.

Then this plan's status becomes done; the next independent re-grade is Phase 5's.

## 7. Risks

- **Ticks on routine work** if a spec misses a common option: the replay and O3 are the guard.
- **getopt differences** between programs (mawk has no long options; `tar`'s old-style first word; `-W` in mawk
  takes a word, not letters): each spec is written against this box's binary, not a generic GNU assumption.
- **Wrong spec that allows a writing option**: the verifier's job; any option whose man page mentions a file it
  writes, a command it runs, or a device it changes stays off the spec.
