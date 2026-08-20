"""Talks to the local Ollama daemon, and generates rather than only health-checking it.

Depends on: harness.core.tasks (OllamaError), stdlib urllib (no external HTTP
client, so the whole agent stays testable without a running daemon).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .tasks import OllamaError

__all__ = ["OllamaStatus", "RecoveryOption", "OllamaClient"]


class OllamaStatus(str, Enum):
    """Result of a health probe."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecoveryOption:
    """One path out of a backend outage."""

    key: str
    title: str
    detail: str
    action: str

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


class OllamaClient:
    """Talks to the local Ollama daemon, and can fall back when it cannot.

    Six phases shipped a handler that only ever called `/api/tags`. It health
    checked a daemon nothing was talking to. This one generates, which is what
    makes the difference between a safety framework and an agent.
    """

    DEFAULT_HOST: str = "http://localhost:11434"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        timeout: float = 120.0,
        probe_timeout: float = 1.0,
        fallback_model: str = "qwen3.5:latest",
        transport: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
    ) -> None:
        """Configure the host, timeouts, fallback model, and an injectable transport.

        `fallback_model` defaults to the smallest model in the current
        four-model catalog (`HANDOFF.md` §2.9's pruning), not the
        cheapest-sounding name in the abstract -- "qwen3:8b" was this
        default until a full-file audit found it: that model was deleted
        along with the rest of the exploratory catalog, so both
        `Agent.converse`'s degraded-status fallback and the "Fall back to
        {model}" text in `recovery_options` were recommending a model that
        no longer exists. `Agent.converse` already has its own defense
        for exactly this (falls through to `available[0]` when even the
        fallback isn't pulled), so this wasn't a crash -- just consistently
        wrong advice to whoever read it.
        """
        self.host = host.rstrip("/")
        self.timeout = float(timeout)
        self.probe_timeout = float(probe_timeout)
        self.fallback_model = fallback_model
        self._transport = transport or self._http
        self.last_status: OllamaStatus = OllamaStatus.UNKNOWN
        self.last_error: str = ""
        self.consecutive_failures: int = 0
        self.calls: int = 0
        self.total_seconds: float = 0.0

    # -- transport --------------------------------------------------------- #

    def _http(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST JSON to the daemon, or GET when payload is empty."""
        url = f"{self.host}{path}"
        if payload:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json"})
        else:
            request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            # Found live: a bad or unpulled model name surfaced to the operator
            # as the generic "HTTP Error 404: Not Found" -- Ollama's own body
            # ({"error": "model '...' not found"}) was read by nobody and
            # discarded, which is exactly the detail needed to fix the request.
            raise OllamaError(f"HTTP {exc.code}: {self._http_error_detail(exc)}") from exc

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> str:
        """Ollama's own error message from the response body, falling back to the HTTP reason."""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            return str(body.get("error") or exc.reason)
        except Exception:
            return exc.reason

    # -- health ------------------------------------------------------------ #

    def health(self) -> OllamaStatus:
        """Probe once and record the result."""
        try:
            self._transport("/api/tags", {}, self.probe_timeout)
            self.consecutive_failures = 0
            self.last_status = OllamaStatus.HEALTHY
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.consecutive_failures += 1
            self.last_status = (OllamaStatus.DEGRADED
                                if self.consecutive_failures == 1
                                else OllamaStatus.UNREACHABLE)
        return self.last_status

    def models(self) -> list[str]:
        """Names of every model pulled locally. Empty on any failure."""
        try:
            data = self._transport("/api/tags", {}, self.probe_timeout)
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    def model_info(self, model: str) -> dict[str, Any]:
        """Real parameter size and chat capability for one pulled model.

        Guessing a size from the model name (a `\\d+b\\b` regex plus a small
        hardcoded table) is what routing used before this: it worked for
        "devstral" and broke silently on anything the table didn't know
        about -- "qwen3.5", "deepseek-r1-heavy", any future pull -- which
        the guesser reads as "0B, fits anywhere". Worse, an embedding-only
        model like "nomic-embed-text" has no chat capability at all
        regardless of size, and nothing about its name says so. `/api/show`
        reports both directly. Returns `{}` on any failure -- callers keep
        the name-based guess as a fallback for that case, same as an
        unreachable daemon already degrades every other probe here.
        """
        try:
            data = self._transport("/api/show", {"name": model}, self.probe_timeout)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {}
        raw_size = ((data.get("details") or {}).get("parameter_size") or "").strip().upper()
        match = re.match(r"^([\d.]+)\s*([BM])$", raw_size)
        params_b = 0.0
        if match:
            value = float(match.group(1))
            params_b = value if match.group(2) == "B" else value / 1000
        capabilities = data.get("capabilities")
        return {
            "params_b": params_b,
            "chat_capable": ("completion" in capabilities
                             if capabilities is not None else True),
        }

    def recovery_options(self, current_model: str = "unknown") -> list[RecoveryOption]:
        """The three paths out of an outage. Never touches the network."""
        return [
            RecoveryOption(
                "restart", "Restart Ollama and retry",
                "Bounces the systemd unit and waits for the health probe. "
                "The conversation and any resume point are kept. Whether "
                "Ollama runs as a user or system unit depends on how it "
                "was installed here -- found live: assuming --user gave "
                "advice that silently did nothing on a machine where "
                "Ollama is a system service.",
                "systemctl --user restart ollama "
                "|| sudo systemctl restart ollama"),
            RecoveryOption(
                "fallback", f"Fall back to {self.fallback_model}",
                f"{current_model} may not fit in available VRAM. The smaller "
                "model keeps the session moving with less reasoning depth.",
                f"use model {self.fallback_model}"),
            RecoveryOption(
                "manual", "Drop to manual mode",
                "Stops asking the model for plans. You write the task file "
                "yourself and every gate still applies.",
                "harness run <task.yaml>"),
        ]

    # -- generation -------------------------------------------------------- #

    def chat(
        self,
        model: str,
        messages: Sequence[dict[str, str]],
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> str:
        """Send a conversation and return the assistant's reply text."""
        # temperature defaults low: a model proposing shell commands should
        # not be creative. Raises OllamaError on transport failure or an
        # unusable response; the caller is expected to present recovery_options.
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": float(temperature)},
        }
        if json_mode:
            payload["format"] = "json"

        started = time.perf_counter()
        try:
            data = self._transport("/api/chat", payload, self.timeout)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_status = OllamaStatus.UNREACHABLE
            raise OllamaError(
                f"generation failed against {self.host}: {self.last_error}") from exc
        finally:
            self.total_seconds += time.perf_counter() - started
            self.calls += 1

        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            raise OllamaError(f"{model} returned an empty response")
        self.last_status = OllamaStatus.HEALTHY
        self.consecutive_failures = 0
        return content

    def stats(self) -> dict[str, Any]:
        """Call counters, for the audit trail."""
        return {
            "host": self.host,
            "status": self.last_status.value,
            "calls": self.calls,
            "total_seconds": round(self.total_seconds, 3),
            "mean_seconds": round(self.total_seconds / self.calls, 3) if self.calls else 0.0,
            "last_error": self.last_error,
        }
