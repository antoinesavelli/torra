# docs/agents/ — Area Map

Topic files that `AGENTS.md`'s read-first triggers point to. **Not** the OS reference files in `docs/` (measured gaps only, listed in `docs/README.md`): those are
meant for the planning model, these are instructions for an agent editing this codebase. Keep the two apart.

| File | What it is |
|---|---|
| `LOOP.md` | The OBSERVE→CLOSE loop as this repo runs it, and the full Definition of Done. |
| `GUARDRAILS.md` | The OS-engineering guardrails with their reasons, and "fix the cause, not the check" for this repo's guards. |
| `PLAYBOOKS.md` | Classification, gates, `docs/` knowledge, live-wire bugs, new features. |
| `CONVENTIONS.md` | Full code conventions, the source-of-truth registry, where new information goes, tool-use patterns. |
| `knowledge_questions.yaml` | Questions a fresh agent must answer correctly, each with the facts expected and the docs that state them. `tests/test_knowledge_questions.py` checks every expected fact is in its sources. |
