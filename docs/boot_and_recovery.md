# Boot menu and recovery partition

Part of the scoped Pop!_OS reference set — see `docs/README.md` for how these
files exist and when to add to them.

## Recovery partition

Pop!_OS ships a recovery partition. To reach it: reboot, and at the
System76/Pop!_OS boot menu (GRUB-based) select the recovery entry. From
there, the two options are:

- **Refresh install** — reinstalls the OS while keeping `/home` intact.
- **Clean install** — full reinstall, wipes everything.

Not a measured gap — all three benchmarked models
(`scripts/dev/model_bench/REPORT.md`) described roughly this shape correctly.
Kept here for completeness, since the harness's own `SYSTEM_PROMPT` treats
"wipe and reinstall" requests as something to refuse or push back on hard
(see the t6 task in the benchmark's task battery) — knowing the *actual*,
much less destructive recovery path is what a good alternative proposal
looks like instead of just refusing outright.
