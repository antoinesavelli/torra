# torra/gates/

The five behavioural guardrails. Each submodule is self-contained -- no
cross-references between them, except `rollback.py` -> `undo.py`, one
gate's own two halves -- and `__init__.py` re-exports every public name, so
`from torra.gates import X` works exactly as it did when this was one file.

| File | Holds |
|---|---|
| `known_programs.py` | What torra can vouch for: read-only programs, bounded writers, and the per-program rules (`git`, `curl`, `python -c`) that decide which. Anything absent asks (`HANDOFF.md` §2.60) |
| `safety.py` | SAFETY_FIRST: `SafetyClassifier` resolves what a command will actually run, then classifies it |
| `state.py` | VERIFY_STATE: `StateVerifier`, preconditions rechecked between steps, TTL cached |
| `undo.py` | ROLLBACK_ON_FAIL, half one: `UndoGenerator` derives undo actions from what a step declares -- also `capture_remote_file`, the same real snapshot/restore bar over an optional executor SSH runner, for `remote_push` steps (`HANDOFF.md` §2.53) |
| `rollback.py` | ROLLBACK_ON_FAIL, half two: `VerifiedRollback` proves each undo actually worked |
| `clarifier.py` | ASK_CLARIFYING_QUESTIONS: `Clarifier` detects ambiguity by missing structure, not phrasing |
| `router.py` | Model routing: `ModelRouter` scores every rule instead of taking the first match -- also tracks `model_chat_capable`/`model_thinking_capable` from the daemon's own `/api/show` data (`HANDOFF.md` §2.31) |
| `decisions.py` | TRANSPARENCY: `DecisionLog`, every branch logged with its reasoning |

`rollback.py`/`undo.py` split out of one file once it crossed the 500 functional
LOC modularity cap -- see `DEFENSIVE_CODING_STANDARDS.md` §1.2 and §2.1's update.

See [`HANDOFF.md` §2](../../HANDOFF.md) for the invariants each gate protects.
