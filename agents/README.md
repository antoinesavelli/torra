# agents/ — Area Map

Topic files that `AGENTS.md`'s read-first triggers point to. **Not** `docs/`: `docs/*.md` is OS knowledge fed to the planning
model (measured gaps only, `docs/README.md`); these are instructions for an agent editing this codebase. Keep the two apart.

| File | What it is |
|---|---|
| `LOOP.md` | The OBSERVE→CLOSE loop as this repo runs it, and the full Definition of Done. |
| `GUARDRAILS.md` | The OS-engineering guardrails with their reasons, and "fix the cause, not the check" for this repo's guards. |
| `PLAYBOOKS.md` | Classification, gates, `docs/` knowledge, live-wire bugs, new features. |
| `CONVENTIONS.md` | Full code conventions, the source-of-truth registry, where new information goes, tool-use patterns. |
| `knowledge_questions.yaml` | Questions a fresh agent must answer correctly, each with the facts expected and the docs that state them. `tests/test_knowledge_questions.py` checks every expected fact is in its sources. |
