# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/). This
project has one maintainer and no external consumers of the package as an
API, so entries favor "what changed and why it matters to the operator"
over strict semver-contract language.

## [7.1.0] - 2026-08-25

### Added
- Multi-tab TUI sessions: every conversation is now an independent tab
  (`ctrl+t` new, `ctrl+w` close, `ctrl+tab`/`ctrl+shift+tab` switch), each
  with its own `Harness`/`Agent`/context, so running a plan in one tab
  never blocks or interrupts another.
- Persisted chat history: every message is written to
  `workdir/sessions/<id>/` as it happens (`harness/core/session.py`), so a
  crash or restart no longer loses a conversation. `ctrl+o` opens a picker
  to reopen a past session, replaying its transcript and rehydrating the
  model's own context exactly as it was.
- `ctrl+l` (clear chat) now leaves a `[cleared by operator]` marker in the
  persisted transcript instead of erasing history.

### Fixed
- `UndoGenerator`'s default shadow directory was keyed only by
  `(workdir, pid)` -- two `Harness` instances sharing a `workdir` in one
  process (exactly what tabs are) could wipe each other's rollback
  snapshots mid-run. See `HANDOFF.md` §2.23.
- `Agent`'s fallback `task_id` restarted at 0 per instance, so two
  sessions' first turns both defaulted to `task_id="agent-1"`,
  deterministically overwriting each other's audit file. See
  `HANDOFF.md` §2.23.
- A session name built from arbitrary operator text (e.g. `"list files in
  [/mnt/x]"`) crashed the app: Textual's `Tab`/`TabPane` parse a plain
  `str` as their own markup language. See `HANDOFF.md` §2.25.

## [7.0.0] - 2026-08-24

First tagged release. Matches the version already recorded in
`pyproject.toml`, which had been bumped without a corresponding tag; this
entry is the retroactive anchor for it, on the commit where a full
hardening pass most recently landed.

### Added
- `harness --version`, single-sourced from the installed package.

### Fixed
- `SafetyClassifier`: closed a command-substitution/`$IFS` bypass and a
  `su -c`/Perl bareword-`unlink` evasion gap (26/26 evasion corpus, 0
  false positives).
- All pre-existing `mypy` errors in the test suite.
- `ModelRouter`: recovered a measured "quick" routing fix (`devstral` over
  `qwen3.5`, based on live-tested accuracy).

### Changed
- Test coverage raised from ~88% to 99% project-wide; 20/25 source files
  at 100%.
- Documentation audit: fixed stale numbers throughout, and one real
  functional bug in `examples/`.
- Repo made private; two abandoned worktrees' work recovered and merged,
  the orphans removed.

[7.1.0]: https://github.com/antoinesavelli/harness/compare/v7.0.0...v7.1.0
[7.0.0]: https://github.com/antoinesavelli/harness/releases/tag/v7.0.0
