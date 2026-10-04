# torra/core/

Task/step primitives, conflict detection, context compression, the model
backends, and machine observation. `__init__.py` re-exports every public
name, so `from torra.core import X` works exactly as it did when this was
one file.

| File | Holds |
|---|---|
| `tasks.py` | `Step`, `TaskSpec`, `shell()` -- the task/step primitives; `Step.command` is what the safety gate classifies. `shell_output_sink()` scopes `shell()`'s live-output callback to the calling thread, for `Torra.execute()`'s `on_output` (`HANDOFF.md` §2.32). `shell()` hands the command to `process.py` (`HANDOFF.md` §2.36, §2.61) |
| `process.py` | How a step runs: bash in its own process group, an error trap that stops at the first real failure while still forgiving a `head`-truncated pipe's `SIGPIPE`, group kill on timeout or Ctrl-C, closed stdin, output kept to head and tail (`HANDOFF.md` §2.36, §2.61) |
| `resume.py` | `ResumePoint`, one atomically-written file per task; `begin_step`/`end_step` mark a step in flight and finished (`HANDOFF.md` §2.64); `resolve_resume()` validates one against the current plan and each step's command digest and tells `Torra.execute()` which steps (if any) a crash-left attempt already completed for real (`HANDOFF.md` §2.33) |
| `conflict.py` | `ConflictDetector`, two-layer detection: cheap fingerprints to shortlist, SHA256 to confirm |
| `context.py` | `ContextCompressor`, keeps the conversation under a token budget |
| `ollama.py` | `OllamaClient`, talks to the local Ollama daemon and generates -- `chat`'s optional `on_thinking` callback opts into a streaming request and surfaces reasoning tokens live, for models `model_info` measures capable (see `HANDOFF.md` §2.31) |
| `backend.py` | `ModelBackend`, the structural Protocol (`chat`/`recovery_options`/`stats`) every model backend satisfies -- `OllamaClient` needs no changes to already satisfy it (depends on `ollama.py` for the `RecoveryOption` type) |
| `openrouter.py` | `OpenRouterClient`, an opt-in, paid, remote alternative -- never auto-selected, only used when an operator explicitly names an `openrouter/`-prefixed model (`Agent.resolve_backend`, see `HANDOFF.md` §2.27) |
| `machine.py` | `Accelerator`/`MachineState`, what the box looks like and what the model will run on -- `Accelerator.fits()` credits VRAM a confirmed single-model daemon (`OLLAMA_MAX_LOADED_MODELS == 1`, read from its real environment, never guessed) would free up by evicting whatever's currently resident (`HANDOFF.md` §2.35) (depends on `ollama.py` for the `OllamaClient` type it attaches). Also `build_ssh_runner`/`observe_executor`/`resolve_executor_config` -- optional, read-only observation of a second machine ("executor") over SSH, reusing `observe_machine`'s own injectable-runner DI pattern unchanged, gated on `TORRA_EXECUTOR_HOST` (`HANDOFF.md` §2.52) |
| `remote_edit.py` | Pull-edit-push: torra-constructed actions for `kind="remote_pull"`/`"remote_push"` steps, their extra plan-validation rules, and `remote_push`'s unconditional CRITICAL risk floor -- the model never authors ssh/scp text anywhere in this (`HANDOFF.md` §2.53) |
| `session.py` | `SessionStore`, one directory per conversation under `workdir/sessions/` -- atomically-written metadata plus an append-only transcript, so a TUI tab's chat history survives a restart (depends on `context.py` for the `ContextStep`/`ContextCompressor` shape it persists and rehydrates) |

See [`HANDOFF.md` §2.1](../../HANDOFF.md) for why `Step.command` and `Step.run`
must always be built from the same string.
