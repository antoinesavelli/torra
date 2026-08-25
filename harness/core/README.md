# harness/core/

Task/step primitives, conflict detection, context compression, the Ollama
client, and machine observation. `__init__.py` re-exports every public name,
so `from harness.core import X` works exactly as it did when this was one file.

| File | Holds |
|---|---|
| `tasks.py` | `Step`, `TaskSpec`, `shell()` -- the task/step primitives; `Step.command` is what the safety gate classifies |
| `resume.py` | `ResumePoint`, one atomically-written file so a crash doesn't mean starting over |
| `conflict.py` | `ConflictDetector`, two-layer detection: cheap fingerprints to shortlist, SHA256 to confirm |
| `context.py` | `ContextCompressor`, keeps the conversation under a token budget |
| `ollama.py` | `OllamaClient`, talks to the local Ollama daemon and generates |
| `machine.py` | `Accelerator`/`MachineState`, what the box looks like and what the model will run on (depends on `ollama.py` for the `OllamaClient` type it attaches) |
| `session.py` | `SessionStore`, one directory per conversation under `workdir/sessions/` -- atomically-written metadata plus an append-only transcript, so a TUI tab's chat history survives a restart (depends on `context.py` for the `ContextStep`/`ContextCompressor` shape it persists and rehydrates) |

See [`HANDOFF.md` §2.1](../../HANDOFF.md) for why `Step.command` and `Step.run`
must always be built from the same string.
