# Pop Shell vs. COSMIC — two different things

Part of the scoped Pop!_OS reference set — see `docs/README.md` for how these
files exist and when to add to them.

## Pop Shell

A GNOME Shell extension: adds tiling window management on top of the (older,
GTK/GNOME-based) Pop!_OS desktop. It is **not a desktop environment on its
own** and has nothing to do with graphics switching (see `docs/graphics.md`
for the gap that confusion caused in one benchmarked model).

## COSMIC

System76's own desktop environment, written in **Rust** (using their `iced`
GUI toolkit), built to eventually replace the GNOME-based desktop as
Pop!_OS's default. It is a **separate project from Pop Shell**, not a
renaming or successor version of it — Pop Shell's tiling ideas influenced
COSMIC's design, but they are different codebases with different scopes
(COSMIC is a full desktop environment; Pop Shell is one extension for GNOME).

## Measured gaps

From `scripts/dev/model_bench/REPORT.md`:

- qwen3:8b stated COSMIC is "not written in Rust" — backwards; this is a
  confident, specific factual reversal, not a vague miss.
- qwen3:32b named the tiling feature "Tiling... part of GNOME" without
  naming Pop Shell specifically — vague enough to read as not actually
  knowing the feature's name.
- devstral got the general shape of COSMIC right but invented a wrong
  expansion for the acronym.

Both the Rust fact and the Pop Shell/COSMIC distinction are worth treating as
reliably-confused points rather than assuming a fluent-sounding answer is
correct.
