"""CI guard: agents/knowledge_questions.yaml is well-formed, gradeable, and still true to its docs.

Depends on: agents/knowledge_questions.yaml and the docs each question names as sources.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
_REQUIRED = ("id", "question", "expected", "must_contain", "sources")


def _questions() -> list[dict[str, Any]]:
    data = yaml.safe_load((ROOT / "agents" / "knowledge_questions.yaml").read_text("utf-8"))
    assert isinstance(data, list), "knowledge_questions.yaml must be a YAML list"
    return data


def _search(rx: str, text: str) -> bool:
    return re.search(rx, text, re.I) is not None


def test_questions_are_well_formed_and_gradeable() -> None:
    questions = _questions()
    ids = [q.get("id") for q in questions]
    assert 12 <= len(questions) <= 15, f"expected 12 to 15 questions, found {len(questions)}"
    assert len(ids) == len(set(ids)), f"duplicate question ids in {ids}"
    errors: list[str] = []
    for q in questions:
        missing = [f for f in _REQUIRED if not q.get(f)]
        if missing:
            errors.append(f"[{q.get('id')}] missing or empty: {missing}")
            continue
        must_not = q.get("must_not_contain", [])
        for rx in [*q["must_contain"], *must_not]:
            if "**" in rx or "`" in rx or "\n" in rx:
                errors.append(f"[{q['id']}] /{rx!r}/ holds markup or a line break")
            try:
                re.compile(rx)
            except re.error as exc:
                errors.append(f"[{q['id']}] /{rx}/ does not compile: {exc}")
        errors += [
            f"[{q['id']}] question leaks /{r}/"
            for r in q["must_contain"]
            if _search(r, q["question"])
        ]
        errors += [
            f"[{q['id']}] expected misses /{r}/"
            for r in q["must_contain"]
            if not _search(r, q["expected"])
        ]
        errors += [
            f"[{q['id']}] expected trips /{r}/" for r in must_not if _search(r, q["expected"])
        ]
    assert not errors, "\n".join(errors)


def test_every_expected_fact_is_still_in_its_sources() -> None:
    errors: list[str] = []
    for q in _questions():
        paths = [ROOT / src for src in q["sources"]]
        absent = [src for src, p in zip(q["sources"], paths, strict=True) if not p.is_file()]
        if absent:
            errors.append(f"[{q['id']}] source does not exist: {absent}")
            continue
        corpus = "\n".join(p.read_text("utf-8") for p in paths)
        errors += [
            f"[{q['id']}] /{rx}/ not found in {q['sources']}: the doc or the answer drifted"
            for rx in q["must_contain"]
            if not _search(rx, corpus)
        ]
    assert not errors, "\n".join(errors)
