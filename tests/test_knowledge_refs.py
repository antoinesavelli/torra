"""CI guard: agent-facing docs cite only what still exists, docs/INVARIANTS.md and docs/LIVE_WIRES.md stay short, and plans open with their status.

Depends on: docs/INVARIANTS.md, docs/LIVE_WIRES.md, AGENTS.md, docs/agents/*.md, docs/plans/*.md, the
root *.md, docs/**/*.md, torra/**/*.py and *.md, tests/**/*.py and *.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A citation names its file, then "§N.M" (the file may end a line, with the "§" opening the
# next, after a "#" or ">" marker); numbers listed straight after ("§2.62, §2.74", "§2.78-2.81",
# "`§2.1`/`§2.2`", "and §2.3") are each checked against that same file.
_SECTION_REF = re.compile(
    r"(INVARIANTS|LIVE_WIRES)(?:\.md)?`?[ \t]*(?:\n[ \t]*(?:[#>][ \t]*)*)?`?§\s?(\d+\.\d+)`?"
    r"((?:[ \t]*(?:[,/;-]|–|\band\b)[ \t]*`?§?[ \t]*\d+\.\d+`?)*)"
)
_TAIL_NUMBER = re.compile(r"\d+\.\d+")
_REF_FILES = {"INVARIANTS": "docs/INVARIANTS.md", "LIVE_WIRES": "docs/LIVE_WIRES.md"}
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
# Caps on the two files' growth: a new §2/§5 entry is short and each file is bounded.
# The last entry per section that is exempt, because it holds history (plan item P2.4, K6).
_EXEMPT_UP_TO = {"2": 82, "5": 25}
_NEW_ENTRY_MAX_LINES = 10
# Each file's size + 10 % on 2026-10-27 when HANDOFF.md retired; raising either is the owner's
# call -- when it fails, shrink an entry the change supersedes.
_INVARIANTS_MAX_BYTES = 120_170
_LIVE_WIRES_MAX_BYTES = 19_597
_ENTRY_HEADER = re.compile(r"^#{2,4}\s+([25])\.(\d+)(?:\.\d+)*\.?\s")
_ANY_HEADER = re.compile(r"^#{1,6}\s")
_BLANK = re.compile(r"^[>\s]*$")


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _section_ref_sources() -> list[Path]:
    found = {*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")}
    for sub in ("torra", "tests"):
        found.update(p for ext in ("py", "md") for p in (ROOT / sub).rglob(f"*.{ext}"))
    return sorted(found)


def test_every_invariant_and_live_wire_citation_names_a_real_header() -> None:
    headers = {
        name: set(_HEADER.findall((ROOT / rel).read_text())) for name, rel in _REF_FILES.items()
    }
    for name, found in headers.items():
        assert len(found) > (50 if name == "INVARIANTS" else 10), (
            f"{_REF_FILES[name]} headers not found: _HEADER likely broke"
        )
    stale, cited = [], 0
    for path in _section_ref_sources():
        text = path.read_text()
        for m in _SECTION_REF.finditer(text):
            numbers = [m.group(2), *_TAIL_NUMBER.findall(m.group(3))]
            cited += len(numbers)
            stale += [
                f"{path.relative_to(ROOT)}:{_line(text, m.start())} -> "
                f"{_REF_FILES[m.group(1)]} §{n}"
                for n in numbers
                if n not in headers[m.group(1)]
            ]
    assert cited > 100, (
        "no INVARIANTS/LIVE_WIRES section citations found: _SECTION_REF likely broke"
    )
    assert not stale, "citations of a section with no header in the file they name:\n" + "\n".join(
        stale
    )


def _cited_tests(text: str) -> Iterator[tuple[str, str, str, int]]:
    for m in _TEST_REF.finditer(text):
        for name, wildcard in re.findall(rf"::{_NAME}", m.group(2)):
            yield m.group(1), name, wildcard, m.start()
        pos = m.end()
        while chained := _CHAINED.match(text, pos):
            yield m.group(1), chained.group(1), chained.group(2) or "", chained.start(1)
            pos = chained.end()


def test_every_cited_guard_test_exists() -> None:
    docs = [
        ROOT / "docs" / "INVARIANTS.md",
        ROOT / "docs" / "LIVE_WIRES.md",
        ROOT / "AGENTS.md",
        *sorted((ROOT / "docs" / "agents").glob("*.md")),
    ]
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


def _entries() -> Iterator[tuple[str, int, str, str, int, int]]:
    """Yield (file, line, header, section, number, non-blank body lines) for each §2.N and §5.N entry.

    An entry runs from its header to the next header outside a code fence; the header line
    itself is not counted, and neither is a blank or bare ">" line.
    """
    for rel in _REF_FILES.values():
        lines = (ROOT / rel).read_text().splitlines()
        starts, fenced = [], False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                fenced = not fenced
            elif not fenced and _ANY_HEADER.match(line):
                starts.append(i)
        for start, end in zip(starts, [*starts[1:], len(lines)], strict=True):
            if m := _ENTRY_HEADER.match(lines[start]):
                body = sum(1 for text in lines[start + 1 : end] if not _BLANK.match(text))
                yield rel, start + 1, lines[start], m.group(1), int(m.group(2)), body


def test_a_new_invariant_or_live_wire_entry_stays_short() -> None:
    entries = list(_entries())
    assert len(entries) > 100, "too few §2/§5 entries found: _ENTRY_HEADER likely broke"
    long = [
        f"{rel}:{line} -> {header!r} has {body} non-blank lines"
        for rel, line, header, section, number, body in entries
        if number > _EXEMPT_UP_TO[section] and body > _NEW_ENTRY_MAX_LINES
    ]
    assert not long, (
        f"new entries (past §2.{_EXEMPT_UP_TO['2']} and §5.{_EXEMPT_UP_TO['5']}) are capped at "
        f"{_NEW_ENTRY_MAX_LINES} non-blank lines under their header, the header line itself "
        "not counted. Shorten each to its rule, its guard and the shortest why; history "
        "belongs in git:\n" + "\n".join(long)
    )


def test_invariants_and_live_wires_stay_under_their_size_ceilings() -> None:
    ceilings = {
        _REF_FILES["INVARIANTS"]: ("_INVARIANTS_MAX_BYTES", _INVARIANTS_MAX_BYTES),
        _REF_FILES["LIVE_WIRES"]: ("_LIVE_WIRES_MAX_BYTES", _LIVE_WIRES_MAX_BYTES),
    }
    over = []
    for rel, (const, ceiling) in ceilings.items():
        size = (ROOT / rel).stat().st_size
        if size > ceiling:
            over.append(f"{rel} is {size} bytes, {size - ceiling} over {const} ({ceiling})")
    assert not over, (
        "\n".join(over) + "\nShrink an entry this change supersedes. Raising a ceiling is the "
        "owner's call, not the change's."
    )


# docs/agents/CONVENTIONS.md, "A multi-step plan": the three lines every plan opens with.
_PLAN_HEADER = (
    re.compile(r"^Status: (active|done)$", re.M),
    re.compile(r"^Owner decisions pending: \S", re.M),
    re.compile(r"^Last verified: [0-9a-f]{7,40} on \d{4}-\d{2}-\d{2}\b", re.M),
)


def test_every_plan_opens_with_its_status_header() -> None:
    """A plan with no status or verified-at sha reads as live long after it went stale."""
    missing = []
    plans = sorted((ROOT / "docs" / "plans").glob("*.md"))
    assert plans, "no plans found under docs/plans: the glob likely broke"
    for plan in plans:
        head = "\n".join(plan.read_text().splitlines()[:12])
        missing += [f"{plan.name}: {p.pattern}" for p in _PLAN_HEADER if not p.search(head)]
    assert not missing, (
        "plan header lines missing from the first 12 lines (docs/agents/CONVENTIONS.md, "
        "'A multi-step plan'):\n" + "\n".join(missing)
    )
