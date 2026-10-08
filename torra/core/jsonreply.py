"""Pulling one JSON object out of a model reply without trusting the reply.

A reply can be fenced, preceded by prose, nested deeper than Python will parse, or built to make
a scan slow. Everything here is bounded: only the first `MAX_SCAN` characters are searched, only
the first `MAX_STARTS` opening braces are tried, and a parser limit is a `ReplyError`, never a
crash.

Depends on: the standard library only (json, re).
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["ReplyError", "extract_object", "as_strings"]

MAX_SCAN = 200_000
MAX_STARTS = 25


class ReplyError(ValueError):
    """The reply holds no usable JSON object."""


def _matching_brace(text: str, start: int) -> int | None:
    """Index of the `}` closing the `{` at `start`, skipping braces inside string literals."""
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise
    except (RecursionError, ValueError) as exc:  # nesting, or an integer past the parser's limit
        raise json.JSONDecodeError("unusable JSON", text, 0) from exc


def _find_object(text: str) -> dict[str, Any] | None:
    """The first brace-balanced substring that also parses as a JSON object: preamble like
    `${VAR}` before the real reply otherwise fools a find-first/find-last pair."""
    pos = 0
    for _ in range(MAX_STARTS):
        start = text.find("{", pos)
        if start < 0:
            return None
        end = _matching_brace(text, start)
        if end is not None:
            try:
                data = _loads(text[start:end + 1])
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                return data
        pos = start + 1
    return None


def extract_object(text: str) -> dict[str, Any]:
    """The JSON object in a model reply, or `ReplyError`."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()[:MAX_SCAN],
                     flags=re.MULTILINE).strip()
    try:
        data = _loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    found = _find_object(cleaned)
    if found is None:
        raise ReplyError("model returned no JSON object")
    return found


def as_strings(value: Any) -> list[str]:
    """`value` as a list of strings; anything that is not a list (a number, a string) is empty."""
    return [str(v) for v in value] if isinstance(value, list) else []
