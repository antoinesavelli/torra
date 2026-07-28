# tests/

76 cases, organised by subsystem. Nothing here needs a live Ollama, systemd,
or network -- every external dependency is an injected callable (see
`conftest.py`), which is also how it's wired to the real thing in `harness/`.

| File | Holds |
|---|---|
| `conftest.py` | Shared fixtures: `scripted()` drives a multi-turn conversation, including the repair loop, without a daemon |
| `test_gates.py` | The five guardrails: safety evasions, precondition TTL/flips, undo generation, verified rollback, clarifier accuracy, routing |
| `test_agent.py` | The model loop: plan validation, repair, conversation compression, backend errors |
| `test_pipeline.py` | The seven-stage `Harness`: approval flow, dry-run, precondition re-checks, external-edit detection |
| `test_tui.py` | Terminal rendering: verdict display, checkbox logic |

```bash
python3 -m pytest tests/ -q              # must stay 76 passed
```
