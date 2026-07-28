# Graphics switching (hybrid NVIDIA hardware)

Part of the scoped Pop!_OS reference set — see `docs/README.md` for how these
files exist and when to add to them.

## The System76-specific tool

**`system76-power graphics <mode>`**, where `<mode>` is `integrated`,
`nvidia`, or `hybrid`. Also exposed via Settings → Graphics in the GUI. This
is the native System76/Pop!_OS mechanism — not the generic Debian/Ubuntu
`nvidia-prime` / `prime-select` path, which predates it and isn't how
Pop!_OS itself manages the switch.

Related, read-only and safe to run unconditionally: `system76-power profile`
reports/sets the power profile (`battery`, `balanced`, `performance`) and is
a different feature from graphics mode, worth not conflating the two when a
request mentions "power" or "performance" rather than "graphics."

## Measured gap

This was the single most-missed fact in the benchmark
(`scripts/dev/model_bench/REPORT.md`) — **2 of 3 models got it wrong**:

- qwen3:8b named `nvidia-prime` (the generic, non-Pop!_OS-native mechanism).
- devstral named `pop-shell` — confusing graphics switching with the tiling
  window manager entirely (see `docs/desktop_environment.md`); Pop Shell has
  nothing to do with graphics.
- qwen3:32b was the only model to name `system76-power` correctly.

If a harness request mentions switching graphics modes, hybrid GPU behavior,
or "nvidia mode" on System76 hardware, a generated plan naming `nvidia-prime`
or `prime-select` should be treated as a likely miss worth double-checking
against this file before approval.
