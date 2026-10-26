"""The chat-backend interface every model backend must satisfy.

Depends on: torra.core.ollama (RecoveryOption -- not moved here, to avoid
touching every existing import of it for a rename that adds no value).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .ollama import RecoveryOption

__all__ = ["ModelBackend", "resolve_backend"]


@runtime_checkable
class ModelBackend(Protocol):
    """A place a chat completion can come from -- the local Ollama daemon,
    or an explicitly opted-into alternative like OpenRouter.

    Structural (`Protocol`), not inherited: `OllamaClient` satisfies this
    with zero changes to `ollama.py`. Scoped to exactly what `Agent`/
    `Torra` call -- no `health()`/`models()`/`model_info()`, since those
    are local-daemon/VRAM-routing concepts an opt-in remote backend has no
    equivalent for.
    """

    def chat(
        self,
        model: str,
        messages: Sequence[dict[str, str]],
        json_mode: bool = False,
        temperature: float = 0.2,
        on_thinking: Callable[[str], None] | None = None,
    ) -> str:
        """Send a conversation and return the assistant's reply text."""
        ...

    def recovery_options(self, current_model: str = "unknown") -> list[RecoveryOption]:
        """The paths out of this backend's own kind of outage."""
        ...

    def stats(self) -> dict[str, Any]:
        """Call counters, for the audit trail."""
        ...


def resolve_backend(
    model: str, backends: Mapping[str, ModelBackend], default: ModelBackend
) -> tuple[ModelBackend, str]:
    """The backend for `model`, plus `model` with its routing prefix
    stripped -- OpenRouter IDs are already provider-prefixed
    ("anthropic/..."), so a registered "openrouter/..." prefix only picks
    the backend; the full string is what a caller should keep for audit
    transparency (`Agent` keeps it as `turn.model`). Falls through to
    `default` unchanged for anything else -- `Agent.router` never returns
    a prefixed model itself, so this is the only place a request's backend
    can be anything but the local daemon, and only when explicitly asked."""
    for prefix, backend in backends.items():
        marker = f"{prefix}/"
        if model.startswith(marker):
            return backend, model[len(marker) :]
    return default, model
