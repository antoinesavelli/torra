#!/usr/bin/env python3
"""fucone reader for bench.py: exits non-zero unless $FUCONE_RUN_DIR/results.json is a complete battery.

Depends on: tasks.py (TASKS) and the stdlib; reads only, never contacts Ollama.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

from tasks import TASKS

REQUIRED_KEYS = {"model", "task_id", "ok", "intent_correct"}


def problem(rows: object) -> str:
    """Why `rows` is not a usable bench result, or "" when it is."""
    if not isinstance(rows, list) or not rows:
        return "results.json is not a non-empty list"
    if not all(isinstance(r, dict) and REQUIRED_KEYS <= r.keys() for r in rows):
        return f"a row lacks one of {sorted(REQUIRED_KEYS)}"
    short = {m: n for m, n in Counter(r["model"] for r in rows).items() if n != len(TASKS)}
    return f"models without all {len(TASKS)} tasks: {short}" if short else ""


def main() -> int:
    run_dir = os.environ.get("FUCONE_RUN_DIR")
    if not run_dir:
        print("FUCONE_RUN_DIR is not set", file=sys.stderr)
        return 1
    path = Path(run_dir) / "results.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"unreadable {path}: {exc}", file=sys.stderr)
        return 1
    why = problem(rows)
    print(why or f"ok: {len(rows)} rows", file=sys.stderr if why else sys.stdout)
    return 1 if why else 0


if __name__ == "__main__":
    raise SystemExit(main())
