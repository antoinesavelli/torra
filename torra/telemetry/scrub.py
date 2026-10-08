"""Scrubbing every string inside a structure before it is written out.

The audit JSON is written from `RunResult.to_dict()`, which carries step output, error text and
the model's own words. Each string goes through `redact_secrets` on its own, so a pattern that
reaches to the end of a line cannot eat a JSON closing quote.

Depends on: torra.telemetry.records (redact_secrets).
"""

from __future__ import annotations

from typing import Any

from .records import redact_secrets

__all__ = ["redact_tree"]


def redact_tree(value: Any) -> Any:
    """A copy of `value` with every string, at any depth, run through `redact_secrets`."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: redact_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_tree(v) for v in value]
    return value
