# torra TODO

## Planner/verifier prototype (`scripts/dev/model_bench/planner_verifier.py`)

State as of 2026-10-14: 9b planner (`qwen3.5:9b`) with a 24b verifier (`devstral:24b`); structured
findings, veto decided in code. One run: fixed set 7/7 good passed and 2/2 bad blocked, live clean
plans 1/8 vetoed, planted path caught 5/8. The git-recoverable exemption was added after the fixed
set showed a wrong veto, so that 7/7 is partly tuned. Committed 2026-10-21 (`61b6886`).

- [ ] Repeat `--fixed-only` 3 to 5 times and report the spread of the 7/7 and 2/2.
- [x] Fix the `invented_specific` false positive on description text: a quote that appears only in
      a step's `description`, not in its `run`/`touches`/`reads`, should not block (seen with
      "Pop!_OS repositories" on `t4-install-and-enable`). Done: `executable_text`, pinned by
      `--self-check`; not yet re-measured on a live run.
- [ ] Look at the 3 missed planted-path controls (docker-prune, install-and-enable, timer); two
      reviews timed out or named other things.
- [ ] Add a short note on the swapped roles, structured findings and fixed set to
      `scripts/dev/model_bench/REPORT.md`, replacing "Torra has no LLM-based verifier today".
- [x] Commit `planner_verifier.py`, `fixed_plans.py` and the results JSONs by explicit path.
      Done: `61b6886` (with the `bench.py` host argument it needs), docs in `50dafc6`.

## Both projects on the workstation

- [ ] Optional: set `OLLAMA_MAX_LOADED_MODELS=2` (`sudo systemctl edit ollama`, then restart) so a
      24b and an 8b stay resident together (about 20 GB of the 4090's 24 GB). Not needed for the
      prototype, which plans every task first and verifies afterwards. Restart only when no
      pruvera trial is running.

## pruvera (after the live trial finishes)

- [ ] A per-role model through the sandbox's inference filter, so a 24b can supervise an 8b in a
      trial. This is a harness change: pre-register it through `bench.experiment`, land it per the
      bench-edits memory (git-archive copy, one guarded commit), and check whether it widens what a
      local model may do unsupervised (the rule-change gate).

## Executor (8 GB) as the 8b planner host

- 2026-10-15: `bench.make_metrics_transport` hardcoded the workstation host, so an earlier "executor"
  run never reached executor (the 404s were `llama3.1:8b` missing on the workstation); fixed with a
  `host` argument. First real run, `llama3.1:8b` on executor with `devstral:24b` on the workstation:
  10 plans, 5 vetoed (most on names the 8b planner invented, e.g. `postgresql-13`, `/srv/api/.env`),
  planted path caught as a blocker in 2 of 10 (the verifier usually filed it as `style`, minor).
  One run, and `planner_verifier.py` was being edited by the other chips during it.
- [ ] Re-run once the chips land, and check the vetoed plans by hand: are the invented names real
      planner hallucinations (then the vetoes are right) or defaults the check should allow.
- [ ] Decide what to do about the verifier calling a planted path `style`: promote by quote check
      regardless of kind, or leave it.
- [ ] Executor has only `hermes3:8b` and `llama3.1:8b`; try `hermes3:8b` as the planner too.
- [ ] `bin/executor-ollama-tunnel down` when finished; it is currently up.
