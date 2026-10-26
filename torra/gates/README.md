# torra/gates/

The five behavioural guardrails. Each submodule is self-contained -- no
cross-references between them, except `rollback.py` -> `undo.py`, one
gate's own two halves -- and `__init__.py` re-exports every public name, so
`from torra.gates import X` works exactly as it did when this was one file.

| File | Holds |
|---|---|
| `known_programs.py` | What torra can vouch for: read-only programs, bounded writers, and `trust()`, which looks a program up in the tables and decides which. Anything absent asks (`HANDOFF.md` §2.60); it re-exports the trust levels, `reads_credentials`, `PREDICATES` and `SUBCOMMANDS` |
| `program_tables.py` | `SUBCOMMANDS` and `PREDICATES`: per-program subcommand sets and the check each program gets |
| `program_checks.py` | The flag helpers and the per-program checks for `python -c`, `sed`, `awk`, `curl`, `tar`, `rsync` and `git` |
| `system_checks.py` | The per-program checks for network, packages, firewall, kernel, disks and the other system tools |
| `program_basics.py` | The trust levels, `reads_credentials` (which file names hold secrets) and the subcommand finder |
| `safety.py` | SAFETY_FIRST: `SafetyClassifier` resolves what a command will actually run, then classifies it; it composes the four files below and re-exports `RiskLevel`, `ResolvedCommand` and `Verdict` |
| `safety_resolve.py` | `CommandResolver`: quotes, `VAR=value`, wrappers, `sh -c`, `$(...)` and base64 resolved to concrete argv lists |
| `safety_judge.py` | `CommandJudge`: one resolved command's argv judged by the per-program rules, plus the regex backstop |
| `safety_types.py` | `RiskLevel`, `ResolvedCommand` and `Verdict` |
| `safety_tables.py` | The tables `SafetyClassifier` reads: risky programs, protected prefixes, wrappers, the backstop rows |
| `assignments.py` | Which variables a call assigns besides a leading `NAME=value` (`export`/`declare`, `read`, `printf -v`, namerefs, `for`, `${NAME:=x}`), so `safety.py` can note a changed `PATH`; a name built at run time asks (`HANDOFF.md` §2.83) |
| `state.py` | VERIFY_STATE: `StateVerifier`, preconditions rechecked between steps, TTL cached |
| `script_rules.py` | Recognising the sed and awk programs torra runs without a tick: a parser that accepts only simple sed commands, and an awk check that blanks string literals first (`HANDOFF.md` §2.72) |
| `service_state.py` | Reading and restoring systemd unit state for a step's undo: which units a `systemctl` step names, a unit's scope, active and enabled state, and the verbs that put both back (`HANDOFF.md` §2.63) |
| `undo.py` | ROLLBACK_ON_FAIL, half one: `UndoGenerator` derives undo actions from what a step declares -- also `capture_remote_file`, the same real snapshot/restore bar over an optional executor SSH runner, for `remote_push` steps (`HANDOFF.md` §2.53) |
| `rollback.py` | ROLLBACK_ON_FAIL, half two: `VerifiedRollback` proves each undo actually worked |
| `clarifier.py` | ASK_CLARIFYING_QUESTIONS: `Clarifier` detects ambiguity by missing structure, not phrasing |
| `router.py` | Model routing: `ModelRouter` scores every rule instead of taking the first match -- also tracks `model_chat_capable`/`model_thinking_capable` from the daemon's own `/api/show` data (`HANDOFF.md` §2.31) |
| `decisions.py` | TRANSPARENCY: `DecisionLog`, every branch logged with its reasoning |

`rollback.py`/`undo.py` split out of one file once it crossed the 500 functional
LOC modularity cap -- see `DEFENSIVE_CODING_STANDARDS.md` §1.2 and §2.1's update.

See [`HANDOFF.md` §2](../../HANDOFF.md) for the invariants each gate protects.
