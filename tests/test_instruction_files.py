"""CI guard: the instruction files are one owner (AGENTS.md) behind a one-line CLAUDE.md stub, and the read-first triggers resolve.

Depends on: AGENTS.md, CLAUDE.md, agents/*.md.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_MAX_ROOT_BYTES = 14400  # measured at the 2026-09-30 migration, ~15% slack; detail belongs in agents/ behind a trigger


def test_claude_md_is_the_one_line_import_stub() -> None:
    """Content in CLAUDE.md would silently fork the instructions from AGENTS.md."""
    assert (ROOT / "CLAUDE.md").read_text() == "@AGENTS.md\n"


def test_every_read_first_trigger_target_exists_and_is_indexed() -> None:
    text = (ROOT / "AGENTS.md").read_text()
    targets = sorted(set(re.findall(r"\]\((agents/[A-Z_]+\.md)\)", text)))
    assert targets, "no agents/ triggers found in AGENTS.md: regex likely broke"
    missing = [t for t in targets if not (ROOT / t).is_file()]
    assert not missing, f"AGENTS.md triggers point at missing files: {missing}"
    index = (ROOT / "agents" / "README.md").read_text()
    unindexed = [t for t in targets if Path(t).name not in index]
    assert not unindexed, f"agents/README.md does not list: {unindexed}"


def test_root_agents_md_stays_under_its_byte_budget() -> None:
    size = (ROOT / "AGENTS.md").stat().st_size
    assert size <= _MAX_ROOT_BYTES, (
        f"AGENTS.md is {size} bytes, over {_MAX_ROOT_BYTES}: move detail into agents/"
    )
