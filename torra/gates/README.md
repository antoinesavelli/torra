# torra/gates/

The five behavioural guardrails. Each submodule is self-contained -- no
cross-references between them, except `rollback.py` -> `undo.py`, one
gate's own two halves -- and `__init__.py` re-exports every public name, so
`from torra.gates import X` works exactly as it did when this was one file.

| File | Holds |
|---|---|
| `known_programs.py` | What torra can vouch for: read-only programs, bounded writers, and `trust()`, which looks a program up in the tables and decides which. Anything absent asks (`docs/INVARIANTS.md` §2.60); it re-exports the trust levels, `reads_credentials`, `PREDICATES` and `SUBCOMMANDS` |
| `program_tables.py` | `SUBCOMMANDS` and `PREDICATES`: per-program subcommand sets (read, bounded write, option spec) and the check each program gets |
| `subcommand_specs.py` | The `OptionSpec` of each `SUBCOMMANDS` program (`systemctl`, `pip`, `apt`, `borg`, `tmux` and the rest): its global options plus those its read subcommands accept, each cited to the `--help` or man page it was written from. `known_programs` calls a subcommand read-only only when the whole command line parses under the spec (`--log`, `--rsh`, `-f`, `-o` and the other writing or running options are left off) |
| `program_checks.py` | The flag helpers and the per-program checks for `python -c`, `rsync` and `git` |
| `program_options.py` | The `curl`, `tar`, `sed` and `awk`/`gawk`/`mawk` judges: each an `OptionSpec` of the program's non-writing options (from its own `--help` or man page) on `options.parse`, plus the value checks (`curl -o`, `-w`, `-H`, `-X`; sed and awk program text) |
| `system_checks.py` | The per-program checks for network, packages, firewall, kernel, disks and the other system tools |
| `options.py` | `parse` and `OptionSpec`: a command line split into options and operands the way GNU getopt_long splits it (clusters, attached and separate values, full long names only, `--`), and `allowing`, the judge that is READ_ONLY only when every option is in the program's spec |
| `option_specs.py` | The `OptionSpec` of each listed read-only program (`sort`, `date`, `journalctl`, `ss`, `nvidia-smi`, `ethtool`, `rg`, `ruff` and the rest of the old deny-list set), each cited to the man page or `--help` it was written from, plus the few module-level extra checks (`date` operands, `ethtool` `file`/`data` keywords, `nvidia-smi` operands) |
| `program_basics.py` | The trust levels, `reads_credentials` (which file names hold secrets) and the subcommand finder |
| `safety.py` | SAFETY_FIRST: `SafetyClassifier` resolves what a command will actually run, then classifies it; it composes the four files below and re-exports `RiskLevel`, `ResolvedCommand` and `Verdict` |
| `safety_resolve.py` | `CommandResolver`: quotes, `VAR=value`, wrappers, `sh -c`, `$(...)` and base64 resolved to concrete argv lists |
| `safety_judge.py` | `CommandJudge`: one resolved command's argv judged by the per-program rules, plus the regex backstop |
| `safety_types.py` | `RiskLevel`, `ResolvedCommand` and `Verdict` |
| `safety_tables.py` | The tables `SafetyClassifier` reads: risky programs, protected prefixes, wrappers, the backstop rows |
| `assignments.py` | Which variables a call assigns besides a leading `NAME=value` (`export`/`declare`, `read`, `printf -v`, namerefs, `for`, `${NAME:=x}`), so `safety.py` can note a changed `PATH`; a name built at run time asks (`docs/INVARIANTS.md` §2.83) |
| `state.py` | VERIFY_STATE: `StateVerifier`, preconditions rechecked between steps, TTL cached |
| `script_rules.py` | Recognising the sed and awk programs torra runs without a tick: a parser that accepts only simple sed commands, and an awk check that blanks string literals first (`docs/INVARIANTS.md` §2.72) |
| `service_state.py` | Reading and restoring systemd unit state for a step's undo: which units a `systemctl` step names, a unit's scope, active and enabled state, and the verbs that put both back (`docs/INVARIANTS.md` §2.63) |
| `undo.py` | ROLLBACK_ON_FAIL, half one: `UndoGenerator` derives undo actions from what a step declares -- also `capture_remote_file`, the same real snapshot/restore bar over an optional executor SSH runner, for `remote_push` steps (`docs/INVARIANTS.md` §2.53) |
| `undo_types.py` | `SnapshotKind`, `Snapshot` and `UndoAction`; `undo.py` re-exports them |
| `undo_shadow.py` | `ShadowStore`: the shadow directory a generator snapshots into, reaping dead runs' leftovers, file hashing |
| `undo_remote.py` | `RemoteCapture`: `capture_remote_file`, the snapshot and restore of a file on the executor |
| `rollback.py` | ROLLBACK_ON_FAIL, half two: `VerifiedRollback` proves each undo actually worked |
| `clarifier.py` | ASK_CLARIFYING_QUESTIONS: `Clarifier` detects ambiguity by missing structure, not phrasing |
| `router.py` | Model routing: `ModelRouter` scores every rule instead of taking the first match -- also tracks `model_chat_capable`/`model_thinking_capable` from the daemon's own `/api/show` data (`docs/INVARIANTS.md` §2.31) |
| `decisions.py` | TRANSPARENCY: `DecisionLog`, every branch logged with its reasoning |

`rollback.py`/`undo.py` split out of one file once it crossed the 500 functional
LOC modularity cap -- see `docs/agents/DEFENSIVE_CODING_STANDARDS.md` §1.2 and §2.1's update.

See [`docs/INVARIANTS.md`](../../docs/INVARIANTS.md) for the invariants each gate protects.
