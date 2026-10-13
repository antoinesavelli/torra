# Rename: harness → torra

Written 2026-10-02. Status (2026-10-13): **done.** Executed 2026-10-03: Phase 0 backups (`/mnt/ParamoStorage/archive/harness-pre-torra-rename.bundle`, `dot-harness-pre-torra.tgz`, both still kept), Phase 1 (`69b43a5`), Phase 2, Phase 4, Phase 5 (system-library `2a5ee05`, Paramo `15927dc2`). Since then: the old `harness-*` unit files are gone (`systemctl --user list-unit-files | grep harness` empty on 2026-10-13), and systemd user units, platform-routines and `~/.bashrc` name `harness` only in September logs. Phase 3 was superseded: the private repo was renamed (`paramotrading/harness` redirects to `paramotrading/torra`) but never got the Phase 1 commit; the code was published instead as the public `github.com/antoinesavelli/torra` on 2026-10-13, CI green at `2d5f82b`. Not part of the rename: the `torra-doc-audit` model write failures belong to that routine and are being fixed separately; the Corsican spelling check is the owner's and still open. Left as recorded: system-library `raw/`, `.ORIGINAL`, executor-machine docs, frozen `AIModels/agent-testing` fixtures, one owner quote. Original status was: **plan only, nothing executed.** The owner picked *torra* (Corsican: the
Genoese coastal watchtower, which watches, signals and decides who lands) for this repo, the local
model's system-engineering agent. This doc is the full-rename plan: package, CLI, directory, GitHub repo,
host wiring and cross-repo references.

Spelling note: the meaning is certain, the spelling isn't. Have a native speaker confirm *torra* before
Phase 3 (the GitHub rename is the first step outsiders can see).

---

## 1. Decisions for the owner (answer before Phase 1)

| # | Question | Recommendation |
|---|---|---|
| D1 | Rename internal identifiers (`Harness`, `HarnessError`, `HarnessApp`, `make_harness`, `build_harness`, local `harness` variables) or only outward names (package, CLI, paths)? | **DECIDED 2026-10-02 (owner): every mention.** "harness" is too generic across the machine, so after the rename the word must not appear in any live file. Then `grep -ri harness` means only history. |
| D2 | Keep a transitional `harness` CLI alias? | **No.** There's one user and one alias in `~/.bashrc`, updated in the same change. |
| D3 | Env vars `HARNESS_*` → `TORRA_*`: hard cut, or read the old name as a fallback? | **Hard cut.** The only setters are `~/.bashrc:574` and the repo `.env` (keys only, values untouched), both updated in the same phase. A fallback is speculative code. |
| D4 | Rename the GitHub repo `paramotrading/harness` → `paramotrading/torra`? | **Yes.** It's private, and GitHub redirects the old URL. |
| D5 | Historical records (CHANGELOGs, `quirks-resolved.md`, `raw/` snapshots, dated Paramo planning docs, `archive/harness_phases/`): rewrite? | **No.** They record what was true then. Only live references change. |
| D6 | Distribution name in `pyproject.toml` (`terminal-agent-harness`)? | **`torra`**, matching the package and CLI. |

**No allowlist (D1).** Mentions that mean this tool become `torra`. Generic English uses ("benchmark
harness" in `scripts/dev/model_bench/`, any "test harness" phrasing) are **reworded** to avoid the word
("benchmark runner", "test rig"). They are not renamed to `torra`, because they aren't the tool.

---

## 2. Inventory (measured 2026-10-02)

**In the repo**: 80 tracked files and 1,578 case-insensitive hits.
- Package dir `harness/` (subpackages `core/`, `gates/`, `telemetry/`), `pyproject.toml`
  (`name`, `[project.scripts] harness = "harness.cli:main"`, `packages.find include = ["harness*"]`),
  `terminal_agent_harness.egg-info/`.
- Identifiers: `harness` ×683, `make_harness` ×168, `Harness` ×138, `HarnessError` ×35, `HarnessApp` ×20,
  `build_harness` ×14, and smaller ones (`harness_a/_b`, `initial_harness`, `_build_session_harness`,
  `harness_kwargs`, test names).
- Env vars: `HARNESS_EXECUTOR_HOST` ×15, `HARNESS_KNOWLEDGE_DIR` ×11, `HARNESS_ROOT` ×3,
  `HARNESS_EXECUTOR_USER` ×3, `HARNESS_EXECUTOR_SSH_KEY` ×3.
- State dir default `Path.home() / ".harness"` (`harness/cli.py:49`, `harness/pipeline.py:86`,
  `harness/tui.py:807`). Shadow-dir prefix `harness-shadow-*`.
- `.claude/settings.json` ask-first patterns `Edit(/harness/gates/**)`, `Edit(/harness/pipeline.py)`,
  `Edit(/harness/core/tasks.py)`, which **must** move to `/torra/...` or the guard silently stops matching.
- `.github/workflows/ci.yml:39` (`mypy harness/`), README badge and install block (`~/harness`, `alias harness=…`),
  the `AGENTS.md` area-map paths, `tests/test_instruction_files.py` (asserts on instruction files).

**On the host**:
- `~/.harness/` (2.0 MB: `audit/`, `checkpoints/`, `resume/`, `sessions/`, 6 `harness-shadow-*` dirs).
- `~/.bashrc`: line 262 and 283 (`--harness` repo-jump shortcuts), line 574 (`alias harness=… HARNESS_KNOWLEDGE_DIR …`).
- systemd user units: `harness-doc-audit.{service,timer}` (next fire Mon 2026-10-05 06:53),
  `harness-defensive-scorecard.{service,timer}` (next fire Sun 2026-11-01). Both set
  `ROUTINE_REPO=/mnt/ParamoStorage/harness` and `OPENCODE_AGENT=harnessdocs|harnessscorecard`.
  `platform-routine-watchdog.service` names them in its Description.
- `~/.local/share/platform-routines/`: `prompts/harness-{doc-audit,defensive-scorecard}.{md,pre.sh,post.sh}`
  (6 files), `run.sh`, `platform-routine-watchdog.sh`. Logs stay as they are (history).
- Claude Code project memory `~/.claude/projects/-mnt-ParamoStorage-harness/`, which is keyed by path and
  is orphaned if the dir moves without it.
- `.venv/` has absolute paths baked into its shebangs, so it must be **recreated**, not moved.

**system-library (live references to update)**:
- `machines/workstation/model-routing.yaml` §"harness" (lines ~345–411: `tool: harness`, 10 `file:` paths).
- `machines/workstation/opencode/opencode.json` (agents `harnessdocs`, `harnessscorecard`).
- `machines/workstation/software.md` §1 (~lines 783–890), `shell-and-dotfiles.md`, `users-and-permissions.md`,
  `regenerate.sh:406–411`, `REGENERATE.md:489`.
- Skills mentioning harness: `skills/{handoff-prompt,local-model-research,local-model-subagent}/SKILL.md`.
  Per D1, hits that mean the tool become `torra` and generic uses are reworded.

**Paramo**: only dated planning docs (`docs/planning/AGENTS_MD_MIGRATION.md`,
`OPENCODE_AGENT_WORKFLOW.md`), left alone under D5. Plus the memory file
`user_corsican_component_names.md` (add **torra**).

---

## 3. Ask-first gates in this plan

The plan as a whole is approved by whoever approves this doc. These steps still get an explicit yes at
execution time, because the repo rules or doctrine name them:
1. **Phase 1** touches `harness/gates/**`, `pipeline.py` and `core/tasks.py` (the `.claude/settings.json`
   ask-first paths). The edits are mechanical import/identifier renames only, with no logic change.
2. **Phase 2** moves persisted state (`~/.harness` → `~/.torra`).
3. **Phase 3** is outward-facing (GitHub rename, push).
4. **Phase 4** changes automated behavior (systemd units) and deletes the old unit files.

---

## 4. Phases

### Phase 0: preconditions
- [ ] Peer WIP is settled. At writing time the tree had `M .gitignore`, `M README.md` and untracked
      `AUDIT_LOG.md`, `DEFENSIVE_SCORECARD_LOG.md`, `opencode.json`, none of which belong to this plan.
      Wait for their owner to commit, or confirm with the owner. Never sweep them into a rename commit.
- [ ] No `harness` TUI session running (`pgrep -af harness`), no Claude/opencode session cwd'd into the repo,
      no active `.claude/worktrees/*`.
- [ ] Not within an hour of a timer firing (`systemctl --user list-timers | grep harness`).
- [ ] Backup: `git -C /mnt/ParamoStorage/harness bundle create /mnt/ParamoStorage/archive/harness-pre-torra-rename.bundle --all`
      and `tar czf /mnt/ParamoStorage/archive/dot-harness-pre-torra.tgz -C ~ .harness`.
- [ ] Baseline: record the test count (README says 788) and `ruff`/`mypy` status from
      `.venv/bin/python -m pytest tests/ -q`.

### Phase 1: code rename, in place (one commit, repo still at the old path)
1. `git mv harness torra`. Remove `terminal_agent_harness.egg-info/` (build artifact, regenerated).
2. `pyproject.toml`: `name = "torra"`, `[project.scripts] torra = "torra.cli:main"`,
   `include = ["torra*"]`, and the header comment.
3. Imports: `harness.` → `torra.` / `from harness` → `from torra` across `torra/`, `tests/`, `scripts/`.
4. Identifiers (D1): `Harness`→`Torra`, `HarnessError`→`TorraError`, `HarnessApp`→`TorraApp`,
   `make_harness`→`make_torra`, `build_harness`→`build_torra`, `_build_session_harness`→`_build_session_torra`,
   locals and test names likewise (including partial-word hits such as `test_two_harnesses_…` and
   `harnesss_own_write`). Use regexes, then reword every remaining generic use (D1).
5. Env vars (D3): `HARNESS_*` → `TORRA_*`. Rename the keys in `.env` by hand **without printing values**
   (`sed -i 's/^HARNESS_/TORRA_/' .env`).
6. State dir default `~/.harness` → `~/.torra`, shadow prefix `harness-shadow-` → `torra-shadow-`.
7. `.claude/settings.json` ask-first paths → `/torra/...`. `ci.yml` → `mypy torra/`.
8. Docs: `README.md` (title, badge URL, install block, alias), `AGENTS.md` area-map paths, `agents/*.md`,
   `HANDOFF.md`, `AUDIT_FINDINGS.md`, `DEFENSIVE_CODING_STANDARDS.md`, the package READMEs and `CLAUDE_MIGRATION_ROADMAP.md`
   (live doc, renamed throughout, with a dated note line).
9. Verify: `pip install -e . --no-deps` in the existing venv, same test count as baseline,
   `ruff check .`, `mypy torra/`, and the defensive scorecard with no grade drop.
   Residual check: `git grep -in harness` returns **zero** hits (this plan doc excepted, until Phase 6).
10. Commit by explicit paths (`git mv` stages the rename, so review `git diff --cached --stat` in full).

### Phase 2: directory, venv, state, Claude memory
1. `mv /mnt/ParamoStorage/harness /mnt/ParamoStorage/torra`.
2. Recreate the venv: `rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements-lock.txt && .venv/bin/pip install -e . --no-deps`.
3. `mv ~/.harness ~/.torra`, then rename the shadow dirs (`harness-shadow-*` → `torra-shadow-*`), or delete them
   if they're stale (check age first; deleting needs an ask).
4. `mv ~/.claude/projects/-mnt-ParamoStorage-harness ~/.claude/projects/-mnt-ParamoStorage-torra`.
5. Verify: full test suite from the new path, `torra doctor`, and a session resume opens an existing session.

### Phase 3: GitHub
1. `gh repo rename torra -R paramotrading/harness`, then `git remote set-url origin https://github.com/paramotrading/torra.git`.
2. Push the Phase 1 commit. Check the badge URL resolves and CI runs. If Actions is still billing-blocked
   (as on Paramo), say so instead of reporting CI green.

### Phase 4: host wiring
1. `~/.bashrc`: lines 262/283 `--harness` → `--torra` (keep the help text at 277 in sync), line 574 →
   `alias torra="sudo -v && export TORRA_KNOWLEDGE_DIR=/mnt/ParamoStorage/system-library && cd /mnt/ParamoStorage/torra && source .venv/bin/activate && torra"`.
2. systemd: `systemctl --user disable --now harness-doc-audit.timer harness-defensive-scorecard.timer`,
   then write `torra-doc-audit.{service,timer}` and `torra-defensive-scorecard.{service,timer}` with the same
   schedules, `ROUTINE_REPO=/mnt/ParamoStorage/torra` and `OPENCODE_AGENT=torradocs|torrascorecard`. Update the watchdog
   Description, `daemon-reload` and `enable --now` the new timers, then remove the old unit files (ask).
3. platform-routines: rename the 6 `prompts/harness-*` files to `torra-*` and fix their contents. Update the
   routine names in `run.sh` and `platform-routine-watchdog.sh` (the watchdog must expect the new names, or
   it pages on missed runs).
4. Verify: `systemctl --user list-timers | grep torra` shows the same next-fire times as before.
   `systemctl --user start torra-doc-audit.service` and read its log through to the end. Run the watchdog once and check it's green.

### Phase 5: system-library and memory
1. Update the live references listed in §2 (`model-routing.yaml`, `opencode/opencode.json` agent names,
   `software.md`, `shell-and-dotfiles.md`, `users-and-permissions.md`, `regenerate.sh`, `REGENERATE.md`,
   the three skills where the hit means this tool). Leave CHANGELOG, `quirks-resolved.md` and `raw/` alone (D5).
2. Run `check.sh agents` in system-library, and dry-run the `regenerate.sh` section that reads the repo.
3. Memory: add **torra** to `user_corsican_component_names.md`.
4. Commit system-library by explicit paths.

### Phase 6: close
- Repo-wide residual sweep: `grep -rIn "ParamoStorage/harness\|~/harness\|harness-doc-audit\|harness-defensive" /mnt/ParamoStorage ~/.config ~/.local/share/platform-routines ~/.bashrc --exclude-dir=.git --exclude-dir=.venv --exclude-dir=archive`. Hits
  must be historical (D5) only. Then run a bare `grep -rIil harness` over the same scope, with no path patterns.
  Any live file it lists is a miss.
- After one clean firing of each routine, delete the backups only if the owner agrees.
- Rename this file's status line to **done**, or delete the file once the rename has held for a week.

---

## 5. Rollback

Every phase can be undone on its own, in reverse order: units (re-enable the `harness-*` timers, which stay
on disk until Phase 4's last step), `gh repo rename harness` (the old name is free again), move the dir and
`~/.torra` back, rebuild the venv, and `git revert` the Phase 1 commit. The bundle and the
`~/.harness` tarball from Phase 0 are the last resort.
