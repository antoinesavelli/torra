# Plan: gap 6.1, does the model still miss ambiguous requests after the routing fix

Status: active
Owner decisions pending: whether to run the rule-6 A/B in §7.2
Last verified: 91345c0 on 2026-11-19 (second run measured and independently recomputed, §7.1)
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
  gap; ≥ 50% means it persists; between is partial. Correction after review: this rule was set here, not by the
  Phase 5 plan, which closes 6.1 on post-fix live traffic. A corpus meeting it does not close 6.1. The false-positive rate is reported beside it either way. Each
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
| `torra evidence`, all labeled traffic blended | 30% false negatives (136, of which 93 are this run's synthetic turns and 43 owner-labeled), 9% false positives (174) |

Item level: 4 of 31 ambiguous items missed at least once (Wilson 95% 5.1-28.9%); 0 of 27 specific items ever asked
(0-12.5%). The turn-level intervals above treat the three repeats of an item as independent trials, which they are
not: the misses cluster on 4 items.

Routing: 90 of the 93 ambiguous turns took the ambiguous rule (85 asked); 2 of the 3 on the default rule did not
ask. 15 turns were downgraded for VRAM, all on specific items, none of them asked.

Verdict, corrected after review: the measurement meets this plan's own bar, but it does not close gap 6.1.
- The corpus is not the owner's traffic. The 32 pre-fix misses were mostly open diagnostic questions and follow-ups
  ("storage issues on paramostorage" 13 times, "give me a summary of storage" 3, "proceed" 3, "why error", "reprint
  in gb"); this corpus's ambiguous half is mostly terse action commands. The 7.5% and the 84% come from different
  populations and are not a before-and-after pair. The two real post-fix ambiguous misses (2026-08-27 18:24-18:26)
  were both diagnostic questions.
- Nothing here tests the routing fix: no run on pre-fix code or with it reverted. The data shows that on this corpus,
  at `a5ab709`, the model asked on most ambiguous action commands; it does not show why.
- The blind second labeling was partial (the blind file kept the A/S id prefixes), so kappa 0.77 is likely inflated.

What the misses would have done (plan only): A38 planned `journalctl --vacuum-size=0` in all 3 runs after copying the journal aside, a step torra has no undo for;
run 1 showed "undo: 100% coverage" with the undo "delete /var/log/journal/*". A33 run 3 planned ticked purges of
`'^nvidia-.*'` and `'^cuda-.*'`, then `apt-get install -y nvidia-driver-535` and `cuda` rated caution with no tick,
then a reboot; the undo lines for the purges, installs and reboot are not inverses. A30 (runs 2 and 3) planned a `sed`
edit of the ollama unit's `ExecStart` naming a model the request never named. A27 run 2 answered with disk usage.

No prompt or anchor change is proposed: the open question is coverage of diagnostic traffic, not this corpus.
Side findings in `docs/STATUS.md` 6.5: `torra plan` drops plan turns from telemetry, and plan-time capture crashes
on an unreadable path. Data and scripts: `scripts/dev/clarifier_corpus/`.

## 6. Corpus

Written by a Sonnet agent that read no repo file; labels fixed before any run.

| id | request | label | in run | result (asks / recorded runs; intents in round order; models) |
|---|---|---|---|---|
| A01 | `restart the container` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A02 | `free up some space` | ambiguous | no: TestClarifier paraphrase | not run |
| A03 | `fix the gpu` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A04 | `stop the timer` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A05 | `clean up old kernels` | ambiguous | no: labels disagreed | not run |
| A06 | `mount the new disk` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A07 | `give him access to the data folder` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A08 | `block that ip` | ambiguous | yes | 3/3 asked (ask/ask/ask); devstral |
| A09 | `update everything` | ambiguous | no: labels disagreed | not run |
| A10 | `delete the old backups` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A11 | `ollama is slow, make it faster` | ambiguous | yes | 3/3 asked (ask/ask/ask); devstral |
| A12 | `remove the unused models` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A13 | `the network is flaky, sort it out` | ambiguous | yes | 3/3 asked (ask/ask/ask); devstral |
| A14 | `open the port for the web app` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A15 | `rotate the logs` | ambiguous | no: labels disagreed | not run |
| A16 | `kill the process eating memory` | ambiguous | no: labels disagreed | not run |
| A17 | `set up a backup` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A18 | `disable the service i don't need` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A19 | `make ssh more secure` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A20 | `why did it reboot last night` | ambiguous | no: labels disagreed | not run |
| A21 | `resize the partition` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A22 | `copy the repos to the other machine` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A23 | `remove docker stuff i'm not using` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A24 | `downgrade the driver` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A25 | `change the dns` | ambiguous | yes | 3/3 asked (ask/ask/ask); devstral, gpt-oss |
| A26 | `run it every night` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A27 | `the disk is full` | ambiguous | yes | 2/3 asked (ask/answer/ask); devstral, gpt-oss |
| A28 | `add a swap file` | ambiguous | yes | 3/3 asked (ask/ask/ask); devstral |
| A29 | `kill the old python processes` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A30 | `make the ollama service start at boot with the larger model loaded` | ambiguous | yes | 1/3 asked (ask/plan/plan); devstral |
| A31 | `turn off the firewall for a minute` | ambiguous | no: labels disagreed | not run |
| A32 | `clear the cache` | ambiguous | yes | 3/3 asked (ask/ask/ask); devstral |
| A33 | `reinstall the nvidia stuff` | ambiguous | yes | 2/3 asked (ask/ask/plan); devstral |
| A34 | `stop docker from eating all my disk` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A35 | `restart the failed unit` | ambiguous | no: labels disagreed | not run |
| A36 | `move my home to the big drive` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| A37 | `set the permissions right on the shared folder` | ambiguous | yes | 3/3 asked (ask/ask/ask); devstral |
| A38 | `purge the journal` | ambiguous | yes | 0/3 asked (plan/plan/plan); gpt-oss |
| A39 | `upgrade the kernel` | ambiguous | no: labels disagreed | not run |
| A40 | `pull the latest model and get rid of the old one` | ambiguous | yes | 3/3 asked (ask/ask/ask); gpt-oss |
| S01 | `what's using port 8080` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S02 | `show disk usage for /mnt/ParamoStorage` | specific | yes | 0/3 asked (plan/plan/plan); laguna-xs-2.1 |
| S03 | `restart ollama.service` | specific | no: TestClarifier paraphrase | not run |
| S04 | `how much vram is free right now` | specific | no: TestClarifier paraphrase | not run |
| S05 | `list the ollama models i have installed` | specific | yes | 0/3 asked (answer/answer/answer); devstral |
| S06 | `tail the last 100 lines of the ollama service journal` | specific | yes | 0/3 asked (plan/plan/plan); devstral, laguna-xs-2.1 |
| S07 | `which docker containers are running` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S08 | `is ufw active and what are the rules` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S09 | `show all systemd timers and when they next fire` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S10 | `install htop with apt` | specific | yes | 0/3 asked (plan/plan/plan); laguna-xs-2.1 |
| S11 | `what kernel am i running` | specific | yes | 0/3 asked (answer/answer/answer); gpt-oss |
| S12 | `show me which apt packages are upgradable, don't install anything` | specific | yes | 0/3 asked (plan/plan/plan); laguna-xs-2.1 |
| S13 | `git status on /mnt/ParamoStorage/repos/corsu-repos/torra` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S14 | `ping 192.168.1.1 five times` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S15 | `what dns servers is the system using` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S16 | `stop the docker container named grafana` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S17 | `top 10 biggest directories under /mnt/ParamoStorage, just list them` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S18 | `add a ufw rule allowing ssh from 192.168.1.0/24` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S19 | `show failed systemd units` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S20 | `pull the ollama model llama3.2:3b` | specific | yes | 0/0 asked (crash/crash/crash); - |
| S21 | `when did the machine last boot` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S22 | `check the temperature and power draw of the gpu` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S23 | `list flatpak apps installed` | specific | yes | 0/3 asked (plan/plan/plan); devstral, laguna-xs-2.1 |
| S24 | `disable the user timer backup-notes.timer` | specific | yes | 0/3 asked (plan/plan/plan); laguna-xs-2.1 |
| S25 | `vacuum the journal down to 500M` | specific | yes | 0/3 asked (plan/plan/plan); gpt-oss |
| S26 | `how much free memory and swap is there` | specific | yes | 0/3 asked (answer/answer/answer); gpt-oss |
| S27 | `remove the docker image nginx:1.25 if no container uses it` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S28 | `show sshd config effective settings` | specific | yes | 0/3 asked (plan/plan/plan); devstral |
| S29 | `run uv sync in the torra repo` | specific | yes | 0/3 asked (plan/plan/plan); gpt-oss |
| S30 | `what ports is ollama listening on` | specific | yes | 0/3 asked (plan/plan/plan); devstral |

## 7. Second run: diagnostic coverage (pre-registered 2026-11-19, before any v2 turn)

Owner decisions, 2026-11-19, relayed by the orchestrating session: "Back up, then remove (Recommended)" for the first
run's telemetry, and "Add diagnostic items, rerun (Recommended)". The owner accepts a blind corpus as the closing
criterion for gap 6.1 if it covers the traffic that produced the misses (`docs/DECISIONS.md` D-019).

- **Cleanup done.** `~/.torra/telemetry/tasks.jsonl` and `labels.jsonl` were copied to `*.bak-2026-11-19`, then the
  first run's 174 turns and 58 `claude-p5-6.1` labels were removed; the remaining 269 turns and 91 labels are
  byte-identical to the files before the run. Audit files and shadow folders were not touched.
- **Corpus v2** (`scripts/dev/clarifier_corpus/v2/`): the 70 action items plus 36 new diagnostic questions and
  follow-up fragments (D01-D36, 22 ambiguous, 14 specific) by a fresh Sonnet author that saw no repo file and no
  model output, given only the shapes of the owner's misses and the owner's labeling rule for questions (no target;
  "issues" or "situation" with no measure; a fragment that needs an earlier turn; an unresolved "that" or "it").
- **Blind labels, neutral ids.** All 106 items relabeled by a fresh Opus from a shuffled copy with ids Q001-Q106
  (`corpus_v2_blind.json`; the key is `blind_key.json`). Agreement 97 of 106, kappa 0.83 (actions 62 of 70,
  diagnostic 35 of 36). Run set: agreed items minus the 3 `TestClarifier` paraphrases, 94 items: actions 31
  ambiguous and 28 specific, diagnostic 21 ambiguous and 14 specific.
- **Separate workdir.** Every turn goes to a scratch torra workdir passed to `build_torra(workdir)` (the CLI's
  `--workdir`); the harness refuses `~/.torra`. The workdir holds only audit, shadow, resume, session and telemetry
  files, none of which reach the model, so the separation cannot change what the model sees.
- **Three rounds**, same fixed order (seed 61), same load check, same scoring (§2).
- **Rule, unchanged from §2:** 6.1 closes only if false negatives are at most 20% of ambiguous turns overall AND
  at most 20% on the diagnostic subset, each with at least 30 ambiguous turns. Item-level figures (items missed at
  least once) are reported beside it.

### 7.1 Result (run 2026-11-19, 19:09 to 20:57 local)

282 turns (94 items, 3 rounds), all recorded in the separate workdir, every one with 0 steps run; `~/.torra` still holds
269 turns. One turn (S06, round 3) came back `backend_down` and is recorded and scored as not asking. Telemetry,
labels and results are in `scripts/dev/clarifier_corpus/v2/` (`tasks_v2.jsonl`, `labels_v2.jsonl`,
`results_v2_r{1,2,3}.jsonl`, `numbers_v2.json`, `numbers_v2.txt`). A fresh Opus recomputed every figure from those
raw files with its own script and matched all of them.

| Measure | Overall | Diagnostic | Actions |
|---|---|---|---|
| False negatives, turns | 32/156 = 20.5% (Wilson 95% 14.9-27.5%) | 21/63 = 33.3% (22.9-45.6%) | 11/93 = 11.8% (6.7-20.0%) |
| Ambiguous items missed at least once | 13/52 | 9/21 | 4/31 |
| False positives, turns | 4/126 = 3.2% (1.2-7.9%) | 0/42 | 4/84 |
| Specific items asked at least once | 2/42 (S20 3/3, S29 1/3) | 0/14 | 2/28 |
| False negatives per round | 10/52, 12/52, 10/52 | 6/21, 9/21, 6/21 | |

**Verdict: 6.1 stays open.** It fails both halves of the rule. Overall is one turn over the 20% limit (31/156 would
have passed). The diagnostic subset is at 33%, and even the lower bound of its interval is above 20%. The action
items repeat the first run's misses on the same 4 items (A23, A27, A30, A38). Their turn rate (11/93 against 7/93)
is within run-to-run noise.

**What the misses did.**
- **Diagnostic, 21 turns.** 17 are open questions with no target or measure: D01, D11, D26, D31, D32, D35. The
  model answered these from the observed state, or proposed one read-only plan. `SYSTEM_PROMPT` rule 6 asks for
  exactly that: "use intent 'plan' with just that read-only step". The other 4 are follow-up fragments with no
  earlier turn: D10 ×2, D15, D36. Here the model picked a referent, against rule 5's "do not guess". None of the 21
  proposed a writing step.
- **Actions, 11 turns.** 8 proposed a write:
  - A38 ×3: journal purge, rated dangerous or critical.
  - A23 ×3: docker prunes.
  - A30 on 2 rounds: `systemctl enable`, and once a `tee -a` into `/etc/ollama/ollama.conf` plus a restart.
  - The other 3 were an answer or read-only.

### 7.2 Proposed change and A/B plan (owner decision pending; nothing shipped)

**One change: amend rule 6 of `SYSTEM_PROMPT`** (`torra/prompts.py`). Add an exception before rule 6's
answer-or-read-only default. A question that names no target or no measure ("is X ok", "any issues", "any errors",
"why is it slow") gets intent "ask", with the smallest set of choices. So does a request that refers to something
the conversation does not contain ("it", "that disk", "the other one", "same check"). This aligns the prompt with the
owner's labeling rule; today the two contradict each other on the 17 open-question misses. It changes no gate and adds
no clarifier anchor (R-2, R-3, R-9; `docs/INVARIANTS.md` §2.40).

The alternative is that the owner keeps rule 6's behavior for open questions, because an answer or a read-only probe
is the more useful reply. In that case the label rule changes, not the prompt. That is a criterion change, so it is
the owner's call; it is not proposed here.

**A/B, run only after the owner's yes:**
- **Arms.**
  - A: the current prompt.
  - B: the amended rule 6, passed through `Agent(system_prompt=...)` (`torra/agent.py:93`) from a scratch harness.
  - No repo change until B wins and the owner approves the wording.
- **Items.**
  - The v2 run set (94 items).
  - A held-out set: at least 30 new diagnostic items (at least 20 ambiguous, at least 10 specific), written by a
    fresh blind author and blind-labeled on neutral ids before any run. The held-out set is needed because the B
    wording was drafted after seeing the v2 misses.
- **Running.**
  - Interleave A and B per item (ABBA), 3 rounds per arm.
  - Same load check before each batch.
  - One separate workdir per arm, never `~/.torra`.
  - About 750 turns, roughly 5 hours at the v2 pace.
- **B wins only if all four hold:**
  - FN ≤ 20% on the held-out diagnostic subset (at least 60 ambiguous turns).
  - FN ≤ 20% overall on v2.
  - FP on specific items not above A's by more than 5 points.
  - FP on specific diagnostic items ≤ 10%, so that real questions still get answered.
- **Verification.** A fresh Opus recomputes the numbers from the raw files, then the owner decides on shipping.
