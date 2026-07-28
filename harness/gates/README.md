# harness/gates/

The five behavioural guardrails. Each submodule is self-contained -- no
cross-references between them -- and `__init__.py` re-exports every public
name, so `from harness.gates import X` works exactly as it did when this was
one file.

| File | Holds |
|---|---|
| `safety.py` | SAFETY_FIRST: `SafetyClassifier` resolves what a command will actually run, then classifies it |
| `state.py` | VERIFY_STATE: `StateVerifier`, preconditions rechecked between steps, TTL cached |
| `rollback.py` | ROLLBACK_ON_FAIL: `UndoGenerator` derives undos, `VerifiedRollback` proves each one worked |
| `clarifier.py` | ASK_CLARIFYING_QUESTIONS: `Clarifier` detects ambiguity by missing structure, not phrasing |
| `router.py` | Model routing: `ModelRouter` scores every rule instead of taking the first match |
| `decisions.py` | TRANSPARENCY: `DecisionLog`, every branch logged with its reasoning |

See [`HANDOFF.md` §2](../../HANDOFF.md) for the invariants each gate protects.
