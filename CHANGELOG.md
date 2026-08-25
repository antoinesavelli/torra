# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/). This
project has one maintainer and no external consumers of the package as an
API, so entries favor "what changed and why it matters to the operator"
over strict semver-contract language.

## [7.2.0] - 2026-08-25

### Added
- A real model-backend abstraction (`harness/core/backend.py`'s `ModelBackend`
  Protocol) instead of the model client being one concrete class referenced
  by type everywhere -- raises Extensibility/API design from B+.
- **OpenRouter support**, the first alternate backend behind that
  abstraction: opt-in only (`ModelRouter` has no reference to it and cannot
  select one automatically), activated by naming an `openrouter/<model-id>`
  model explicitly (`harness ask --model openrouter/...`, or `/model
  openrouter/...` in the TUI). API key from `OPENROUTER_API_KEY` only --
  never a CLI arg, never persisted to disk. Registered unconditionally
  (key or no key) so an unconfigured request fails with a specific "key not
  set" error instead of silently falling through to an unrelated local
  model -- found by manual testing before release, see `HANDOFF.md` §2.27.
- `harness tui` now goes through `cli.py`'s `build_harness()` instead of
  building its own `Harness` independently -- fixes a real gap found during
  design review: the TUI previously skipped both the disk-free precondition
  and (now) OpenRouter registration that `harness ask`/`harness plan` always
  had. See `HANDOFF.md` §2.27.

## [7.1.1] - 2026-08-25

A full-codebase audit run immediately after 7.1.0 shipped found five real
bugs in the multi-tab/session feature, all reproduced directly before being
fixed -- see `HANDOFF.md` §2.26 for the full story on each.

### Fixed
- Two tabs' first non-plan turn (answer/ask/refuse) could land on the same
  `turn-N.audit.json`, one silently overwriting the other's audit record.
  Reopening a session had the same collision even for plan-type turns,
  since a fresh `Agent`'s turn counter restarts at 0.
- A long-lived tab's audit record for turn *N* silently accumulated every
  earlier turn's decisions too (`DecisionLog` was never cleared between
  turns on a reused `Harness`), growing without bound for the tab's life.
- Reopening a long session sent one unbounded, over-budget prompt to the
  model before compression ever ran on the reloaded history.
- `SessionStore.rename()` could raise on an ordinary disk hiccup, unlike
  every other write path in the class -- and it sits on the most common
  call site there is, the first message of every new tab.
- `execute_plan()`/`plan_request()`'s busy-flag guard still had a real
  (if narrower) double-dispatch race window: the check was synchronous,
  but the flip to `busy=True` happened inside the worker thread after a
  round trip. Both now flip it synchronously, before the worker is even
  dispatched.

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

[7.2.0]: https://github.com/antoinesavelli/harness/compare/v7.1.1...v7.2.0
[7.1.1]: https://github.com/antoinesavelli/harness/compare/v7.1.0...v7.1.1
[7.1.0]: https://github.com/antoinesavelli/harness/compare/v7.0.0...v7.1.0
[7.0.0]: https://github.com/antoinesavelli/harness/releases/tag/v7.0.0
