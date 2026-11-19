# Plan: gap 6.1, does the model still miss ambiguous requests after the routing fix

Status: done
Owner decisions pending: none (the gap closed under the pre-registered bar, so no prompt change is proposed)
Last verified: a5ab709 on 2026-11-19 (run, labels and numbers verified)
Executor: an Opus orchestrator session; Sonnet builds each unit, a fresh Opus verifies it

## 1. Goal

Answer `docs/STATUS.md` gap 6.1: on live traffic the model asked for only 18% of requests a human labeled ambiguous
(82% false negatives, 39 occurrences), and 38 of those 39 were logged before the VRAM-eviction routing fix
(`docs/INVARIANTS.md` §2.35, landed 2026-08-27 10:30). Measure the post-fix false-negative and false-positive rates
on a fresh corpus and record them in §6.1. Nothing in `torra/` changes in this plan.

## 2. Decisions and why

- **A fresh blind corpus, never `TestClarifier`'s.** `docs/agents/GUARDRAILS.md` names tuning against the
  clarifier's own corpus as a mistake already made once. The corpus author reads no file under `tests/` or
  `torra/gates/`, nor `docs/STATUS.md`. The verifier may read `TestClarifier` only to check that no corpus item
  repeats or paraphrases it.
- **Labels fixed before any run.** The author labels each item; a fresh Opus labels the same items without seeing
  the author's labels. Only items both call the same go into the run, so a label is never chosen after seeing what
  the model did.
- **Plan only, recorded the way a declined plan is.** `torra plan` cannot be the driver: a turn that ends in a plan
  finishes `AWAITING_APPROVAL` through `_finish(write_audit=False)`, which returns before the telemetry write, and
  `cmd_plan` exits without `reject()`. Only `ask`/`answer`/`refuse` turns would reach `tasks.jsonl`, so every false
  negative would be invisible and the rate would read 0% (found on the probe, 2026-11-19). The driver instead runs
  a scratch harness that does what `cmd_plan` does (`build_torra(DEFAULT_WORKDIR)`, `Torra.plan(request)`) and then
  calls `Torra.reject()` on a plan, the path the TUI takes when the operator declines. It never calls `execute()`;
  nothing runs. A clarifying question ends the turn with `intent=ask`, which is what `TaskRecord.clarifier_blocked`
  records and what `CorpusExporter.drift()` scores (`docs/INVARIANTS.md` §2.34). Side effect: these turns show as
  `rejected` in `torra evidence`'s health summary; they are told apart by request text and time window.
- **Scored the same way as the 82%.** False negative: an ambiguous-labeled occurrence whose record has
  `intent != "ask"`. False positive: a specific-labeled occurrence with `intent == "ask"`. Three occurrences per
  item (three rounds in the same fixed order): the probe showed the same request planned on one run and asked on
  the next, so one run per item would measure noise. Per-item stability (0 to 3 asks) is reported too.
- **Post-fix split by time.** `torra evidence` mixes the 39 pre-fix occurrences with this run. The post-fix number
  is `CorpusExporter.drift()` called on this run's records only (`at` inside the run window), the same code path.
- **Labeler provenance.** Labels are written with `USER=claude-p5-6.1` so `labels.jsonl` tells them apart from the
  owner's hand labels.
- **Decision rule, fixed before the run.** With n ≥ 30 ambiguous occurrences: false negatives ≤ 20% closes the
  gap; ≥ 50% means it persists; between is partial. The false-positive rate is reported beside it either way. Each
  rate is given with its n and a Wilson 95% interval.
- **Not comparable one to one.** The pre-fix 82% came from the owner's own requests; this corpus is written to look
  like them but is synthetic. §6.1 says so.

## 3. Ask-first boundaries

No change to `SYSTEM_PROMPT`, the clarifier, routing, or any file under `torra/` (R-2, R-3, R-9 in
`docs/DECISIONS.md`; `docs/INVARIANTS.md` §2.40). If the gap persists, the deliverable is the numbers plus one
proposed change with an A/B plan, brought to the owner. No push, no merge to `main`.

Live model etiquette: before each batch, check `curl -s http://127.0.0.1:11434/pastore/status` and for a running
Pruvera bench (`pgrep -af 'm bench\.'`, ignoring shell loops that only wait for one). If a bench runs or the
workstation host has work queued, wait; never evict a bench's model.

## 4. Units

### U1 corpus (Sonnet, blind) → verified by a fresh Opus

Write about 40 ambiguous and 30 specific requests an operator would type at this Pop!_OS workstation, as JSON
`[{"id", "request", "label": "ambiguous"|"specific", "why"}]` in the session scratchpad. Ambiguous means a careful
administrator could not act without asking: the target, the goal or the scope is missing, and plausible readings
lead to different actions. Specific means a careful administrator could plan it as written. Mix kinds: services,
packages, disks, network, users, GPU, containers, logs, boot, files.
Done: the file parses, ids are unique, no request text already appears in `~/.torra/telemetry/tasks.jsonl`.
Verify: a fresh Opus labels every item blind, then compares; it reports agreement and checks for overlap with
`tests/test_gates.py::TestClarifier`. Items the two label differently are dropped.

Result (2026-11-19): 40 ambiguous and 30 specific written. The blind second labels agree on 62 of 70 (Cohen's
kappa 0.77): all 30 specific, 32 of 40 ambiguous. The 8 disagreements are all author-ambiguous, verifier-specific
(A05, A09, A15, A16, A20, A31, A35, A39: a standard reading exists, or a read-only look settles it). Three items
closely paraphrase `TestClarifier` strings and are dropped (A02 "free up some disk", S03, S04). Run set: 31
ambiguous, 28 specific. The verifier's caveats: the blind file's ids began with A/S, so blinding was partial; the
specific side is mostly easy read-only requests, so the false-positive half is the easier test.

### U2 driver (Sonnet) → verified by a fresh Opus

A script in the scratchpad that runs each agreed item through the plan-then-reject harness (§2) in a fixed shuffled
order (seed 61), in batches of 10, doing the load check above before each batch, and writes one JSONL line per item
(id, start and end time, exit code, outcome, intent, model, the last lines of output). Probe on two items first.
Verify: the probe's two records exist in `tasks.jsonl` with `intent` set and nothing executed.

Probe (2026-11-19): through `torra plan`, S06 and A34 both planned and neither reached `tasks.jsonl` (the hole in §2).
Through the harness both were recorded: S06 `plan`, `rejected`, 0 steps run; A34 `ask`, `needs_answer`.

The full run goes in the background from the orchestrator.

### U3 labels and numbers (Sonnet) → verified by a fresh Opus

Feed the pre-decided labels through `torra label` (answering `k` for anything not in the corpus), run
`torra evidence`, and compute the run-window `drift()` with model and routing rule per occurrence.
Verify: a fresh Opus recomputes both rates from raw `tasks.jsonl` and `labels.jsonl` and must match.

### U4 records (orchestrator)

`docs/STATUS.md` §6.1 updated with the numbers; this plan's §5 filled in; the Definition of Done run; one commit
by explicit path on the worktree branch.

## 5. Result

Run 2026-11-19 at `a5ab709`, 15:05 to 15:37 local, through the pastore gateway with no bench running. 177 turns
(59 items, 3 rounds); 174 recorded. S20 ("pull the ollama model llama3.2:3b") crashed all 3 times in plan-time undo
capture (`PermissionError` on `/root/.ollama/models`, `docs/STATUS.md` 6.5), so it has no record and no label. Every
recorded turn ran 0 steps.

| Measure | Value |
|---|---|
| False negatives, run window (`drift()` on these records) | 7/93 = 7.5% (Wilson 95% 3.7-14.7%) |
| False positives, run window | 0/81 = 0% (0-4.5%) |
| Per round, false negatives | 1/31, 3/31, 3/31 |
| Ambiguous items asking 3/3, 2/3, 1/3, 0/3 | 27, 2 (A27, A33), 1 (A30), 1 (A38) |
| Pre-fix, owner labels (`at` before 2026-08-27 10:30) | 32/38 = 84% (70-93%); false positives 15/74 = 20% |
| Post-fix, owner labels | false negatives 2/5, false positives 0/19 |
| `torra evidence`, all labeled traffic blended | 30% false negatives (136), 9% false positives (174) |

Routing: 90 of the 93 ambiguous turns took the ambiguous rule (85 asked); 2 of the 3 on the default rule did not
ask. 15 turns were downgraded for VRAM, all on specific items, none of them asked.

Verdict under §2's rule: false negatives at most 20% with n of at least 30, so gap 6.1 is closed. The routing fix,
not a prompt change, is what moved it. No prompt or anchor change is proposed. Two side findings are in
`docs/STATUS.md` 6.5: `torra plan` drops plan turns from telemetry, and plan-time capture crashes on an unreadable
path.

## 6. Corpus

Written by a Sonnet agent that read no repo file; labels fixed before any run.

| id | request | label | in run |
|---|---|---|---|
| A01 | `restart the container` | ambiguous | yes |
| A02 | `free up some space` | ambiguous | no: TestClarifier paraphrase |
| A03 | `fix the gpu` | ambiguous | yes |
| A04 | `stop the timer` | ambiguous | yes |
| A05 | `clean up old kernels` | ambiguous | no: labels disagreed |
| A06 | `mount the new disk` | ambiguous | yes |
| A07 | `give him access to the data folder` | ambiguous | yes |
| A08 | `block that ip` | ambiguous | yes |
| A09 | `update everything` | ambiguous | no: labels disagreed |
| A10 | `delete the old backups` | ambiguous | yes |
| A11 | `ollama is slow, make it faster` | ambiguous | yes |
| A12 | `remove the unused models` | ambiguous | yes |
| A13 | `the network is flaky, sort it out` | ambiguous | yes |
| A14 | `open the port for the web app` | ambiguous | yes |
| A15 | `rotate the logs` | ambiguous | no: labels disagreed |
| A16 | `kill the process eating memory` | ambiguous | no: labels disagreed |
| A17 | `set up a backup` | ambiguous | yes |
| A18 | `disable the service i don't need` | ambiguous | yes |
| A19 | `make ssh more secure` | ambiguous | yes |
| A20 | `why did it reboot last night` | ambiguous | no: labels disagreed |
| A21 | `resize the partition` | ambiguous | yes |
| A22 | `copy the repos to the other machine` | ambiguous | yes |
| A23 | `remove docker stuff i'm not using` | ambiguous | yes |
| A24 | `downgrade the driver` | ambiguous | yes |
| A25 | `change the dns` | ambiguous | yes |
| A26 | `run it every night` | ambiguous | yes |
| A27 | `the disk is full` | ambiguous | yes |
| A28 | `add a swap file` | ambiguous | yes |
| A29 | `kill the old python processes` | ambiguous | yes |
| A30 | `make the ollama service start at boot with the larger model loaded` | ambiguous | yes |
| A31 | `turn off the firewall for a minute` | ambiguous | no: labels disagreed |
| A32 | `clear the cache` | ambiguous | yes |
| A33 | `reinstall the nvidia stuff` | ambiguous | yes |
| A34 | `stop docker from eating all my disk` | ambiguous | yes |
| A35 | `restart the failed unit` | ambiguous | no: labels disagreed |
| A36 | `move my home to the big drive` | ambiguous | yes |
| A37 | `set the permissions right on the shared folder` | ambiguous | yes |
| A38 | `purge the journal` | ambiguous | yes |
| A39 | `upgrade the kernel` | ambiguous | no: labels disagreed |
| A40 | `pull the latest model and get rid of the old one` | ambiguous | yes |
| S01 | `what's using port 8080` | specific | yes |
| S02 | `show disk usage for /mnt/ParamoStorage` | specific | yes |
| S03 | `restart ollama.service` | specific | no: TestClarifier paraphrase |
| S04 | `how much vram is free right now` | specific | no: TestClarifier paraphrase |
| S05 | `list the ollama models i have installed` | specific | yes |
| S06 | `tail the last 100 lines of the ollama service journal` | specific | yes |
| S07 | `which docker containers are running` | specific | yes |
| S08 | `is ufw active and what are the rules` | specific | yes |
| S09 | `show all systemd timers and when they next fire` | specific | yes |
| S10 | `install htop with apt` | specific | yes |
| S11 | `what kernel am i running` | specific | yes |
| S12 | `show me which apt packages are upgradable, don't install anything` | specific | yes |
| S13 | `git status on /mnt/ParamoStorage/repos/corsu-repos/torra` | specific | yes |
| S14 | `ping 192.168.1.1 five times` | specific | yes |
| S15 | `what dns servers is the system using` | specific | yes |
| S16 | `stop the docker container named grafana` | specific | yes |
| S17 | `top 10 biggest directories under /mnt/ParamoStorage, just list them` | specific | yes |
| S18 | `add a ufw rule allowing ssh from 192.168.1.0/24` | specific | yes |
| S19 | `show failed systemd units` | specific | yes |
| S20 | `pull the ollama model llama3.2:3b` | specific | yes |
| S21 | `when did the machine last boot` | specific | yes |
| S22 | `check the temperature and power draw of the gpu` | specific | yes |
| S23 | `list flatpak apps installed` | specific | yes |
| S24 | `disable the user timer backup-notes.timer` | specific | yes |
| S25 | `vacuum the journal down to 500M` | specific | yes |
| S26 | `how much free memory and swap is there` | specific | yes |
| S27 | `remove the docker image nginx:1.25 if no container uses it` | specific | yes |
| S28 | `show sshd config effective settings` | specific | yes |
| S29 | `run uv sync in the torra repo` | specific | yes |
| S30 | `what ports is ollama listening on` | specific | yes |
