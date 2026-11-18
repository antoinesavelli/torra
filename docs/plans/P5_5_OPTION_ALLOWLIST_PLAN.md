# Plan: P5.5, a listed program's options are allowed, not denied

Status: done
Owner decisions pending: none (O1 to O3 answered, G0 and G1 given, all 2026-11-18)
Last verified: 37b87f0 on 2026-11-18 (Phases 0 and 1; Phase 2 and 3 in the commit that closes this plan)
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
- `_curl` never reads `-w` (`program_checks.py:212-220`), and `-w '%output{F}'` writes F (confirmed on curl
  8.5.0). `-o /dev/null --output=F` also reads SAFE but is harmless: curl rejects `--output=F` (`b9939bd`).
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
parsed the way getopt parses them: clusters, attached values, `--name=value`, `--`, and full long names
only (amended in Phase 1 from "a unique prefix"; see its result). Any option outside the spec asks. This replaces
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

Every Bash command in the Claude Code transcripts (`~/.claude/projects/**/*.jsonl`, subagents included) plus every
step command torra planned (`plan.steps[].step.command` in `~/.torra/audit/*.audit.json`; `tasks.jsonl` holds only a
step count), deduplicated, classified at the base commit and again after each
phase. Report: counts per level, and every command whose level changed, grouped by program. Commands are
classified in-process only; nothing runs. Output stays in the scratchpad (it may hold private paths); only counts
and the program-level summary go into docs.

## 6. Phases and units

### Phase 0: corpus and baseline (no gate code touched)

- **U0 corpus** (Sonnet). `tests/test_known_programs.py` only. Add every P5.5 evidence command (the list under
  "Evidence" in the plan's P5.5 entry (since shrunk to a pointer; the cases are the P5.5 rows in `ASKS`), including the three `sudo` ones) to `ASKS`, and `curl -s -o /dev/null -w
  '%{http_code}' URL`, `tar -tf a.tar`, `awk '{print $1}' f`, `ethtool eth0`, `pip list` and `journalctl -n50 -u x
  --no-pager` to `READ_ONLY_COMMANDS`. Leave the `pip list --log=F` case marked `xfail(strict=True)` with reason
  "Phase 2"; every other new must-ask case fails today and the rest of the suite stays green. Check:
  `uv run pytest tests/test_known_programs.py -q` shows exactly the new must-ask cases failing.
- **U1 replay tool** (Sonnet, parallel with U0). `scripts/dev/replay_verdicts.py` plus
  `tests/test_replay_verdicts.py`. The tool takes `--out FILE` (JSONL of command and level) and `--diff A B`.
  It reads transcripts and audit files through injected paths, so the test uses fixtures, never `~`. Check: its test
  passes, and a baseline run at `6adef17` lands in the scratchpad with its counts reported.

**Phase 0 result (done 2026-11-18).** U0: 56 must-ask cases in `ASKS`, all `xfail(strict=True)`, 55 with reason
"P5.5 Phase 1" and `pip list --log=F` with "Phase 2"; beyond the evidence the verifier added `ethtool -w`/`--get-dump
eth0 data F` (writes F), the short `-r -X -U -Q` forms, curl `--write-out`, bare `gawk -p` and `env ethtool --flash`.
Five read-only cases added (`pip list` was already there). U1: `scripts/dev/replay_verdicts.py` (16 tests); one
classifier exception is recorded as level `error` instead of ending the run, warnings are ignored so `-W error`
cannot move a verdict, and `--diff` groups each move under every plain program name it resolved to. Suite 1970
passed before, 1991 passed and 56 xfailed after. Baseline at `9c3d823`: 138,841 distinct commands
(transcripts 138,661, audit 180, 95 malformed lines skipped), safe 79,188, caution 4,383, dangerous 53,516,
critical 1,754; two runs at one commit are byte-identical. Notes for later units: U3's ethtool spec cannot allow
`-w` outright, because `data FILE` is an operand (check the operands, or let bare `ethtool -w eth0` ask and
explain the move under O3). Found in passing, outside P5.5: `python3 -c` with a payload of about 7,000 chained unary
minus signs makes `ast.parse` in `_pure_python` (`program_checks.py:146`) raise `MemoryError`, which only
`SyntaxError`/`ValueError` are caught for; a gate fix, ask-first.

**Gate G0 (owner):** the baseline counts seen, then a yes to edit `torra/gates/**` (O1 to O3 are answered).

### Phase 1: one option parser, every flag judge on it

- **U2 parser** (Sonnet). New `torra/gates/options.py` plus `tests/test_options.py`. `OptionSpec` (short letters
  with value marks, long names with none, required or optional argument), `parse(argv, spec)`: the options and
  values in order plus the operands, or None on any unknown, ambiguous or malformed option. A factory
  `allowing(spec, value_ok=None, needs=None)` returns READ_ONLY or None, with `value_ok` a per-option value check
  (a module function, not a closure, per the CCN gotcha). Tests cover clusters, attached values (`-n50`),
  `--x=v`, `--x v`, an abbreviation refused, `--` and optional arguments (`gawk -p`,
  `--profile[=F]`). Check: `uv run pytest tests/test_options.py -q`, `uv run mypy`.
- **U3 table judges** (Sonnet, after U2). `program_tables.py` and `system_checks.py` only. Convert every
  `_avoiding`, `_positive` and `_listing_only` entry to an `allowing` spec of that program's read-only options
  (from `man`/`--help` on this box, cited in a comment once per program), then delete `_positive` and
  `_listing_only`. Check: the U0 cases for those programs pass, and the whole suite except the U4 cases stays
  green.
- **U4 curl, tar, sed, awk** (Sonnet, after U2, parallel with U3). `program_checks.py` only. The four judges move to
  `options.parse`. curl: the `-o /dev/null` exemption is kept as is, and a `-w`/`--write-out`
  value containing `%output{` asks. tar: `--index-file` and every unlisted long form ask. awk: `-W`, gawk's
  `-o -p -d` and their long forms ask, with long names matched in full only. sed: every `-e`/`--expression`
  spelling feeds the script check. Delete `_avoiding`, `_hits` and `_CURL_LONG_WRITES`/`_TAR_UNSAFE_LONG` once
  nothing imports them. Check: every U0 case passes except the Phase 2 xfail; `defensive_coding_report.py
  --enforce` holds.

Each unit's verifier is a fresh Opus given this section, the unit's diff, and the instruction to break it: getopt
spellings the unit did not list, `sudo` and `env` wrappers, quoting, and attached values.

**Integration (orchestrator):** full Definition of Done, then the replay against the baseline. Each move goes
through O3; widening a spec goes back to the unit's builder.

**Phase 1 result (done 2026-11-18).** U2 `torra/gates/options.py` (68 tests); U3 the 23 deny-list and letter-set
judges as specs in `torra/gates/option_specs.py`; U4 curl, tar, sed and awk in `torra/gates/program_options.py`.
`_avoiding`, `_positive`, `_listing_only`, `_CURL_LONG_WRITES` and `_TAR_UNSAFE_LONG` are deleted. All 55 Phase 1
xfails pass, the corpus gained 111 cases, and the suite is 2225 passed and 1 xfailed (Phase 2). Each unit was broken by its
verifier first, then fixed:
- *Long names match in full only* (O1 said "unique prefix"). The real program matches its whole option table
  first, so a prefix of a spec name can be the full name of an option the spec omits: `curl --output F` was
  accepted as the allowed `--output-dir`. Abbreviations now ask; real traffic has none.
- tree is not getopt (a value letter takes the next word and the cluster goes on: `tree -Lo 1 F` writes F) and its
  value options swallow `--`; smartctl's short `-j` takes no value (`-jt short` hid a self-test); a quoted `'2>'`
  operand hid the next word from swapon's operand check. All three were SAFE in the first build and now ask.
- Found in passing and fixed: tar `-C` attached, clustered or old-style and `--one-top-level=` now reach the
  system-path check; `sed -i` with an `e`/`w`/`r` script asks; `RISKY_ENV` gains `TAR_OPTIONS`, `TAPE`,
  `POSIXLY_CORRECT`, `SSLKEYLOGFILE`, `CURL_HOME`; `dmidecode --dump-bin` (the old list named a nonexistent
  `--dump-file`) and `date MMDDhhmm` (sets the clock) now ask.

Replay against the baseline, 138,841 commands present in both runs: 95 moved. SAFE to asking: 1, a `journalctl`
call with systemctl-only options (`--list-units --state=failed`) that journalctl rejects. Asking to SAFE: 78, all
read-only forms the old letter sets misread (`date -Is` 67, `hostname -I 2>/dev/null` 5, attached `awk -F` 3,
`swapon --show 2>&1`, `journalctl` 2). CAUTION to asking: 12, ten `sed -i` scripts `sed_script_is_simple` cannot
prove (`a`/`i` text, `{}` blocks, `r FILE`) and two `date >> file`. DANGEROUS to CAUTION: 4, `tar xf ARCHIVE -C
/tmp/...` no longer counts the archive it reads as a write target. O3 holds.

Open, for the owner (all pre-existing, none made worse here):
- argv keeps unquoted redirect words, and shlex has lost the quoting, so an operand check cannot tell `date >> f`
  from `date '>>' f`; the fix belongs in the resolver (drop real redirects from argv).
- an unresolved `$VAR`/`$(...)` word in argv can expand to an option (`curl ${O:--o} F URL` writes F).
- `awk_program_is_safe` misses a redirect after a regex holding `;` (`print $0 ~ /;/ > "F"`) and gawk `@include`.
- `sed -i` with `a`/`i`/`c` text now asks (10 real commands); widening `script_rules` would free them.
- `HOME=`/`XDG_CONFIG_HOME=` choose curl's `.curlrc`; chained relative tar `-C` (`-C /tmp -C ../etc`); curl to
  non-HTTP schemes and local data in a URL; `RIPGREP_CONFIG_PATH`, `SYSTEMD_PAGER`.
- the `python -c` `MemoryError` noted under Phase 0.

**Gate G1 (owner):** the replay's moves are seen; a yes for Phase 2.

### Phase 2: options around a subcommand

- **U5** (Sonnet). `known_programs.py` and `program_tables.py`. Each `SUBCOMMANDS` entry gets an option spec for
  its global and read-subcommand options; an unknown option asks. Spec sizes come from the replay's real usage.
  The U0 `pip --log` xfail now passes and becomes a plain case. Same verification and replay.

**Phase 2 result (done 2026-11-18, G1 given the same day).** Each `SUBCOMMANDS` row is `(read, write, OptionSpec,
lead)`, specs in `torra/gates/subcommand_specs.py`. A read subcommand is READ_ONLY only when the whole line
parses; a write subcommand is BOUNDED_WRITE only when every option before it is on the spec. The verifier broke it
three times, each fixed with its cases in `ASKS`:
- programs that take the first non-dash word as the command whatever a global option expects (flatpak; ollama's
  cobra lets an unknown flag eat the next word): `flatpak --arch uninstall list --all` was SAFE. A `lead` mode now
  allows only plain flags before the subcommand (flatpak, snap, gsettings, ufw, zpool, zfs, tmux, go), or none
  (ollama).
- nft joins its words and splits on `;` or a newline anywhere (`nft list tables ';flush' ruleset`); tmux chains on
  a word ending in `;`. A word holding `;`, or a newline with text around it, asks for every subcommand program.
- an off-spec value option before a write subcommand hid it (`systemctl --message reload poweroff` was CAUTION,
  pre-existing); a lone `-` operand, which apt, flatpak and ollama skip, read as "no subcommand" (`ollama - rm x`
  was SAFE in the first fix).
Suite 2388 passed, 0 xfailed. Replay against Phase 1, 139,271 commands: one move, SAFE to asking, `pip list
--python /bin/true` (pip runs that interpreter). Against the baseline: 96 moves, the Phase 1 set plus that one. Left
open: `python3 -m pip list --log F` goes through the `python` judge, not this table; `tmux capture-pane -S` and
`gsettings --schemadir` now ask (no traffic).

### Phase 3: records

- **U6 docs** (Sonnet). Add a `docs/INVARIANTS.md` entry for the new invariant ("a listed program's options are
  allowed by spec; an unknown option asks", guarded by `tests/test_options.py` and the U0 cases) within the byte
  ceiling, plus a `docs/DECISIONS.md` row for O1. Shrink P5.5 in `GRADE_TO_A_PLAN.md` to a pointer, and update the
  `docs/STATUS.md` line, the `torra/gates/README.md` row and the `PLAYBOOKS.md` file names (`safety_resolve.py`
  and `safety_judge.py` hold rules, but per-program flags now live in `options.py`). Check:
  `uv run pytest tests/test_knowledge_refs.py -q`.

**Phase 3 result (done 2026-11-18).** `docs/INVARIANTS.md` §2.93, `docs/DECISIONS.md` D-016, the P5.5 pointer in
`GRADE_TO_A_PLAN.md`, `docs/STATUS.md` (P5.5 fixed, the open items above as gaps), `PLAYBOOKS.md` and the
`torra/gates/README.md` rows. The next independent re-grade is Phase 5's.

## 7. Risks

- **Ticks on routine work** if a spec misses a common option: the replay and O3 are the guard.
- **getopt differences** between programs (mawk has no long options; `tar`'s old-style first word; `-W` in mawk
  takes a word, not letters): each spec is written against this box's binary, not a generic GNU assumption.
- **Wrong spec that allows a writing option**: the verifier's job; any option whose man page mentions a file it
  writes, a command it runs, or a device it changes stays off the spec.
