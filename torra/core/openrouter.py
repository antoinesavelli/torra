"""Talks to OpenRouter, an opt-in, paid, remote alternative to the local Ollama daemon.

Depends on: torra.core.tasks (OllamaError, reused rather than a new
exception type -- its docstring is already backend-generic), torra.core.ollama
(RecoveryOption), stdlib urllib (same no-external-HTTP-client rule as ollama.py).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from .ollama import RecoveryOption
from .tasks import OllamaError

__all__ = ["OpenRouterClient"]


class OpenRouterClient:
    """An OpenAI-compatible chat backend, reached over the network with an API key.

    Never selected automatically -- `torra/gates/router.py`'s `ModelRouter`
    has no reference to this class at all. Only used when an operator
    explicitly names a model with the `openrouter/` prefix
    (`Agent.resolve_backend`), because sending request text, clarifier gaps,
    and observed machine state to a third party is a real trust-boundary
    change from this torra's local-only default.
    """

    DEFAULT_HOST: str = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        host: str = DEFAULT_HOST,
        timeout: float = 120.0,
        transport: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
    ) -> None:
        """Configure the host, timeout, and an injectable transport.

        `api_key` explicit-overrides-env, same precedent as
        `Torra._resolve_knowledge_dir`: `None` reads `OPENROUTER_API_KEY`
        from the environment, an explicit value (including `""`) wins.
        """
        self.api_key = api_key if api_key is not None else os.environ.get(
            "OPENROUTER_API_KEY", "")
        self.host = host.rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport or self._http
        self.last_error: str = ""
        self.calls: int = 0
        self.total_seconds: float = 0.0
        #: Cumulative real token counts and USD cost, read from
        #: OpenRouter's own response `usage` object -- present by default
        #: on a non-streaming completion (this client never sets
        #: `stream: true`), confirmed against OpenRouter's own API
        #: reference, not assumed. `usage.cost` is USD directly, the same
        #: unit OpenRouter's own pricing pages quote (verified live this
        #: project's own real test calls, e.g. grok-4.6 at $2/$6 per M
        #: input/output tokens) -- not a separate "credit" currency.
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.cost_usd: float = 0.0

    # -- transport --------------------------------------------------------- #

    def _http(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST JSON to OpenRouter, bearer-authenticated."""
        url = f"{self.host}{path}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise OllamaError(f"HTTP {exc.code}: {self._http_error_detail(exc)}") from exc

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> str:
        """OpenRouter's own error message, falling back to the HTTP reason.

        Nested under `{"error": {"message": ...}}`, not Ollama's flat
        `{"error": "..."}` -- a different shape, same lesson as
        `OllamaClient._http_error_detail`: the generic HTTP reason is not
        the detail an operator needs to fix the request.
        """
        try:
            body = json.loads(exc.read().decode("utf-8"))
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or exc.reason)
            return str(err or exc.reason)
        except Exception:
            return exc.reason

    # -- generation -------------------------------------------------------- #

    def chat(
        self,
        model: str,
        messages: Sequence[dict[str, str]],
        json_mode: bool = False,
        temperature: float = 0.2,
        on_thinking: Callable[[str], None] | None = None,
    ) -> str:
        """Send a conversation and return the assistant's reply text.

        Not every model OpenRouter proxies honors structured JSON output --
        a bad model choice can surface as a plan-parsing `AgentError`
        rather than an obviously-OpenRouter error. Not solved here.

        `on_thinking` exists only for `ModelBackend` protocol parity with
        `OllamaClient` -- accepted and ignored. `Agent.converse` only ever
        passes a real callback for models its own `router.model_thinking_capable`
        marks capable, which is populated exclusively from local Ollama
        `/api/show` data, so a remote OpenRouter model is never asked.
        """
        if not self.api_key:
            raise OllamaError(
                "OPENROUTER_API_KEY is not set -- no key configured for openrouter/* models")

        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": float(temperature),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            data = self._transport("/chat/completions", payload, self.timeout)
            if not isinstance(data, dict):
                raise TypeError(f"expected a mapping, got {type(data).__name__}")
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise TypeError("response had no choices")
            content = ((choices[0].get("message") or {}).get("content") or "").strip()
            usage = data.get("usage") or {}
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)
            self.cost_usd += float(usage.get("cost") or 0.0)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise OllamaError(
                f"generation failed against {self.host}: {self.last_error}") from exc
        finally:
            self.total_seconds += time.perf_counter() - started
            self.calls += 1

        if not content:
            raise OllamaError(f"{model} returned an empty response")
        return content

    def recovery_options(self, current_model: str = "unknown") -> list[RecoveryOption]:
        """OpenRouter-shaped guidance -- "restart Ollama" would be exactly
        the wrong advice for an API-key or rate-limit failure here."""
        return [
            # Presence + length only: an operator (or an agent) following
            # `echo $OPENROUTER_API_KEY` put the full secret into terminal
            # scrollback and any transcript downstream of it.
            RecoveryOption(
                "check-key", "Check OPENROUTER_API_KEY",
                "Every openrouter/* request needs a valid key in the environment. Torra may also "
                "load it from its own .env, which this shell check cannot see.",
                'test -n "$OPENROUTER_API_KEY" && echo "OPENROUTER_API_KEY is set '
                '(${#OPENROUTER_API_KEY} chars)" || echo "OPENROUTER_API_KEY is MISSING"'),
            RecoveryOption(
                "check-status", "Check OpenRouter's own status/credits",
                f"{current_model} may be rate-limited, out of credits, or the "
                "provider behind it may be down.",
                "open https://openrouter.ai/activity"),
            RecoveryOption(
                "fallback-local", "Drop the /model override",
                "Clears the operator's explicit model choice and lets routing "
                "pick a local model again.",
                "/model"),
        ]

    def stats(self) -> dict[str, Any]:
        """Call counters, for the audit trail."""
        return {
            "host": self.host,
            "calls": self.calls,
            "total_seconds": round(self.total_seconds, 3),
            "mean_seconds": round(self.total_seconds / self.calls, 3) if self.calls else 0.0,
            "last_error": self.last_error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }
