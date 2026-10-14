"""CI guard: agent-facing docs cite only sections, guard tests and paths that still exist.

Depends on: the root *.md, agents/*.md, torra/**/*.py and *.md, tests/**/*.py and *.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SECTION_REF = re.compile(r"HANDOFF(?:\.md)?`?\s?§\s?(\d+\.\d+)")
_HEADER = re.compile(r"^#{2,4}\s+(\d+(?:\.\d+)*)\.?\s", re.M)
# A name may start the next line ("> "-quoted) when `::` ends a wrapped line; a trailing
# `*` or `...` makes it a prefix wildcard; a `[param]` suffix is ignored.
_NAME = r"[ \t]*(?:\n[> \t]*)?(\w+)(?:\[[^\]\s]*\])?(\*|\.\.\.)?"
_TEST_REF = re.compile(rf"(?<![\w/.-])(?:tests/)?(test_\w+\.py)((?:::{_NAME})+)")
# A bare backticked name chained onto the citation before it (joined by "+", ",", ";", "and",
# line breaks, "> " quote markers or a parenthetical note) is cited from the same test file.
_JOIN = r"(?:[ \t+,;]|and\b|\n[> \t]*(?=[^\s>])|\([^()]*\))*"
_CHAINED = re.compile(rf"`?{_JOIN}`((?:test_|Test)\w+)(\*|\.\.\.)?`")
_DEFINED = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)", re.M)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _section_ref_sources() -> list[Path]:
    found = {*ROOT.glob("*.md"), *(ROOT / "agents").glob("*.md")}
    for sub in ("torra", "tests"):
        found.update(p for ext in ("py", "md") for p in (ROOT / sub).rglob(f"*.{ext}"))
    return sorted(found)


def test_every_handoff_section_citation_names_a_real_header() -> None:
    headers = set(_HEADER.findall((ROOT / "HANDOFF.md").read_text()))
    assert len(headers) > 50, "HANDOFF.md headers not found: _HEADER likely broke"
    stale, cited = [], 0
    for path in _section_ref_sources():
        text = path.read_text()
        for m in _SECTION_REF.finditer(text):
            cited += 1
            if m.group(1) not in headers:
                stale.append(f"{path.relative_to(ROOT)}:{_line(text, m.start())} -> §{m.group(1)}")
    assert cited > 100, "no HANDOFF section citations found: _SECTION_REF likely broke"
    assert not stale, "citations of a HANDOFF.md section with no header:\n" + "\n".join(stale)


def _cited_tests(text: str) -> Iterator[tuple[str, str, str, int]]:
    for m in _TEST_REF.finditer(text):
        for name, wildcard in re.findall(rf"::{_NAME}", m.group(2)):
            yield m.group(1), name, wildcard, m.start()
        pos = m.end()
        while chained := _CHAINED.match(text, pos):
            yield m.group(1), chained.group(1), chained.group(2) or "", chained.start(1)
            pos = chained.end()


def test_every_cited_guard_test_exists() -> None:
    docs = [ROOT / "HANDOFF.md", ROOT / "AGENTS.md", *sorted((ROOT / "agents").glob("*.md"))]
    defined: dict[str, set[str] | None] = {}
    stale, cited = [], 0
    for doc in docs:
        text = doc.read_text()
        for file, name, wildcard, pos in _cited_tests(text):
            cited += 1
            where = f"{doc.relative_to(ROOT)}:{_line(text, pos)}"
            if file not in defined:
                path = ROOT / "tests" / file
                defined[file] = set(_DEFINED.findall(path.read_text())) if path.is_file() else None
            names = defined[file]
            if names is None:
                stale.append(f"{where} -> tests/{file} does not exist")
            elif not (any(d.startswith(name) for d in names) if wildcard else name in names):
                stale.append(f"{where} -> {file}::{name}{wildcard} is not defined there")
    assert cited > 150, "too few guard-test citations found: _TEST_REF likely broke"
    assert not stale, "citations of a guard test that no longer exists:\n" + "\n".join(stale)


def test_every_path_in_the_agents_md_area_map_exists() -> None:
    text = (ROOT / "AGENTS.md").read_text()
    section = re.search(r"^## Area Maps.*?(?=^## )", text, re.M | re.S)
    assert section, "AGENTS.md has no Area Maps section: the test's regex likely broke"
    stale, paths = [], 0
    # A leading "/" is a slash command (`/grade`), never a path relative to the repo root.
    for m in re.finditer(r"`([^`\n]+)`", section.group(0)):
        token = m.group(1)
        is_path = "/" in token or token.endswith((".md", ".py", ".yaml"))
        if is_path and not token.startswith("/"):
            paths += 1
            if not (ROOT / token).exists():
                line = _line(text, section.start() + m.start())
                stale.append(f"AGENTS.md:{line} -> {token} does not exist")
    assert paths > 10, "no paths found in the Area Maps table: the test's regex likely broke"
    assert not stale, "Area Maps table points at missing paths:\n" + "\n".join(stale)
