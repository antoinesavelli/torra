# Package management, upgrades, and base system

Part of the scoped Pop!_OS reference set — see `docs/README.md` for how these
files exist and when to add to them.

## Package manager

`apt` (Pop!_OS is Ubuntu-based, Ubuntu is Debian-based). Not a measured gap —
all three benchmarked models (qwen3:8b, qwen3:32b, devstral) got this right
unprompted.

## Init system

`systemd`, same as Ubuntu. Also not a measured gap, kept for completeness
alongside the two facts above and below it in this file.

## OS-level upgrades: `pop-upgrade`

**`pop-upgrade`** is the Pop!_OS-specific CLI for OS-level upgrades, distinct
from `apt upgrade` (which only handles installed-package versions, not moving
to a new Pop!_OS release):

- `pop-upgrade update` — apply package updates
- `pop-upgrade release upgrade` — upgrade to the next Pop!_OS release
- `pop-upgrade release check` — check what's available without applying it

**Measured gap** (`scripts/dev/model_bench/REPORT.md`): qwen3:8b hallucinated
`popos-upgrade` — a plausible-sounding name that would fail if actually run.
qwen3:32b and devstral both named `pop-upgrade` correctly.
