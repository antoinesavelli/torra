# docs/

Reference material for the harness's planning model, split by theme. Scoped
to *measured* knowledge gaps, not a general Pop!_OS/System76 manual —
`scripts/dev/model_bench/` is what measures those gaps, and
`scripts/dev/model_bench/REPORT.md` is the evidence trail for every fact
below. Blanket documentation would mostly restate what the benchmarked models
(qwen3:8b, qwen3:32b, devstral) already got right unprompted; these files
exist for the specific things they got wrong, vague, or hallucinated.

| File | Covers | Measured gap? |
|---|---|---|
| `package_management.md` | apt, `pop-upgrade`, systemd | `pop-upgrade` name hallucinated by one model |
| `graphics.md` | `system76-power` hybrid graphics switching | yes — wrong in 2 of 3 models |
| `desktop_environment.md` | Pop Shell vs. COSMIC, COSMIC/Rust | yes — wrong or vague in 2 of 3 models |
| `boot_and_recovery.md` | Recovery partition, boot menu | no, kept for completeness |

**Not yet wired into `harness.agent.SYSTEM_PROMPT`.** Injecting this content
into the live model-facing prompt is a change to the harness's core loop and
belongs in its own reviewed change with its own test, not silently bundled
into adding a docs file.

## Adding to this set

Add a fact only when a benchmark run in `scripts/dev/model_bench/` actually
measured it wrong, vague, or hallucinated — the same discipline
`HANDOFF.md` §3 applies to the clarifier's vocabulary applies here: a
docs file that grows from someone's guess about what might be missing, rather
than a measured gap, is the same mistake in a new location. Start a new
themed file rather than growing one past a natural topic boundary; update
this table in the same change.
