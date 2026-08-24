# examples/

Runnable task files, no model involved.

| File | Holds |
|---|---|
| `deploy-api.yaml` | A concrete, well-anchored deploy task -- passes the clarifier, gates classify each step |
| `vague.yaml` | Deliberately underspecified, to watch the clarifier stop it before anything runs |

```bash
harness check examples/deploy-api.yaml   # classify without running
harness check examples/vague.yaml        # the clarifier blocks it here; `run` skips the
                                          # clarifier entirely and would just execute it
```
