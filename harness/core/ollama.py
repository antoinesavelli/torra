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
        fallback_model: str = "gpt-oss:20b",
        transport: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
        stream_transport: Callable[
            [str, dict[str, Any], float, Callable[[str], None]], dict[str, Any]] | None = None,
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

        Changed from "qwen3.5:latest" to "gpt-oss:20b" on 2026-08-28 (the
        operator's own request: no Chinese-origin models) -- same role,
        smallest of the current catalog, not yet re-verified against the
        specific failure qwen3.5:latest was found to have on "quick"-style
        requests (see `ROUTING_RULES`' own comment in `harness/gates/router.py`).
        """
        self.host = host.rstrip("/")
        self.timeout = float(timeout)
        self.probe_timeout = float(probe_timeout)
        self.fallback_model = fallback_model
        self._transport = transport or self._http
        self._stream_transport = stream_transport or self._http_stream
        self.last_status: OllamaStatus = OllamaStatus.UNKNOWN
        self.last_error: str = ""
        self.consecutive_failures: int = 0
        self.calls: int = 0
        self.total_seconds: float = 0.0
        #: Cumulative real token counts, read from Ollama's own response --
        #: `prompt_eval_count`/`eval_count`, present in both the plain and
        #: streaming shapes (`_http_stream` folds the final NDJSON line,
        #: which carries them, into what it returns). Confirmed live
        #: against a real daemon, not assumed present in every reply --
        #: an old Ollama version or a scripted test transport that omits
        #: them just contributes 0, never raises.
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

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

    def _http_stream(
        self, path: str, payload: dict[str, Any], timeout: float,
        on_thinking: Callable[[str], None],
    ) -> dict[str, Any]:
        """POST with `stream: true` and call `on_thinking` per reasoning
        delta as it arrives, off the same NDJSON response body `/api/chat`
        returns in streaming mode -- one JSON object per line, the last
        carrying `"done": true`. Returns the same `{"message": {"content":
        ...}}` shape `_http` returns, content re-assembled from every
        line's delta, so `chat()`'s existing shape validation and
        `.get("message", {}).get("content")` read need no branch for
        which transport produced the dict.
        """
        url = f"{self.host}{path}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        content_parts: list[str] = []
        final: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    message = chunk.get("message") or {}
                    thinking = message.get("thinking") or ""
                    if thinking:
                        on_thinking(thinking)
                    content = message.get("content") or ""
                    if content:
                        content_parts.append(content)
                    if chunk.get("done"):
                        final = chunk
        except urllib.error.HTTPError as exc:
            raise OllamaError(f"HTTP {exc.code}: {self._http_error_detail(exc)}") from exc
        final_message = dict(final.get("message") or {})
        final_message["content"] = "".join(content_parts)
        final["message"] = final_message
        return final

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
        # The whole parse, not just the transport call, is in this try:
        # a daemon that returns valid JSON in an unexpected shape (a list,
        # or "capabilities" as something non-iterable) used to raise an
        # uncaught TypeError/AttributeError here -- past this method's own
        # "returns {} on any failure" contract, and past Harness.plan()'s
        # narrow except clause, so the operator got a raw traceback instead
        # of the intended backend-down handling. Found live.
        try:
            data = self._transport("/api/show", {"name": model}, self.probe_timeout)
            if not isinstance(data, dict):
                raise TypeError(f"expected a mapping, got {type(data).__name__}")
            raw_size = ((data.get("details") or {}).get("parameter_size") or "").strip().upper()
            match = re.match(r"^([\d.]+)\s*([BM])$", raw_size)
            params_b = 0.0
            if match:
                value = float(match.group(1))
                params_b = value if match.group(2) == "B" else value / 1000
            capabilities = data.get("capabilities")
            model_details = data.get("model_info")
            # The real key is family-prefixed ("qwen35.context_length",
            # "llama.context_length", ...) and the family varies per model,
            # so this looks for whichever key actually ends in
            # ".context_length" instead of guessing the family name --
            # confirmed live against a real daemon (qwen3.5 reported
            # "qwen35.context_length": 262144), not assumed from docs.
            context_length = 0
            if isinstance(model_details, dict):
                for key, value in model_details.items():
                    if key.endswith(".context_length") and isinstance(value, int):
                        context_length = value
                        break
            return {
                "params_b": params_b,
                "chat_capable": ("completion" in capabilities
                                 if capabilities is not None else True),
                # Unlike chat_capable, this defaults to False when
                # capabilities is unknown -- a model that might not expose
                # reasoning tokens should silently not be asked to stream
                # them, not be assumed to.
                "thinking_capable": ("thinking" in capabilities
                                     if capabilities is not None else False),
                "context_length": context_length,
            }
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {}

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
        on_thinking: Callable[[str], None] | None = None,
    ) -> str:
        """Send a conversation and return the assistant's reply text.

        `on_thinking`, when given, switches the request to `stream: true`
        and `think: true` and is called with each reasoning delta as it
        arrives -- opt-in, so every existing caller that doesn't pass it
        keeps the original single-shot, non-streaming request unchanged.
        A model with no reasoning capability just never calls it back
        (Ollama returns no `thinking` field), which is the whole point:
        the caller decides whether to ask by checking `model_info`'s
        `thinking_capable` first, not by this method guessing.
        """
        # temperature defaults low: a model proposing shell commands should
        # not be creative. Raises OllamaError on transport failure or an
        # unusable response; the caller is expected to present recovery_options.
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": on_thinking is not None,
            "options": {"temperature": float(temperature)},
        }
        if json_mode:
            payload["format"] = "json"
        if on_thinking is not None:
            payload["think"] = True

        started = time.perf_counter()
        try:
            data = (self._stream_transport("/api/chat", payload, self.timeout, on_thinking)
                   if on_thinking is not None
                   else self._transport("/api/chat", payload, self.timeout))
            # Shape validation lives inside this try too, not after it: a
            # daemon returning valid JSON in an unexpected shape (a list,
            # or "message" as something with no .get) used to raise an
            # uncaught TypeError/AttributeError past this method's own
            # "raises OllamaError on ... an unusable response" contract.
            # Found live.
            if not isinstance(data, dict):
                raise TypeError(f"expected a mapping, got {type(data).__name__}")
            content = ((data.get("message") or {}).get("content") or "").strip()
            self.prompt_tokens += int(data.get("prompt_eval_count") or 0)
            self.completion_tokens += int(data.get("eval_count") or 0)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_status = OllamaStatus.UNREACHABLE
            raise OllamaError(
                f"generation failed against {self.host}: {self.last_error}") from exc
        finally:
            self.total_seconds += time.perf_counter() - started
            self.calls += 1

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
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
