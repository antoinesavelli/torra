# Boot menu and recovery partition

Part of the scoped Pop!_OS reference set — see `docs/README.md` for how these
files exist and when to add to them.

## Recovery partition

Pop!_OS ships a recovery partition. To reach it: reboot, and at the
System76/Pop!_OS boot menu select the recovery entry (hold or tap Space
at power-on to reach it). The bootloader depends on how the machine boots:
UEFI installs (the default) use **systemd-boot**, managed with `kernelstub`
and `bootctl`, with entries under `/boot/efi/loader/entries/`; only legacy
BIOS installs use GRUB. Check with `bootctl status` before assuming either.
From the menu, the two recovery options are:

- **Refresh install** — reinstalls the OS while keeping `/home` intact.
- **Clean install** — full reinstall, wipes everything.

Not a measured gap — all three benchmarked models
(`scripts/dev/model_bench/REPORT.md`) described roughly this shape correctly.
Kept here for completeness, since torra's own `SYSTEM_PROMPT` treats
"wipe and reinstall" requests as something to refuse or push back on hard
(see the t6 task in the benchmark's task battery) — knowing the *actual*,
much less destructive recovery path is what a good alternative proposal
looks like instead of just refusing outright.
