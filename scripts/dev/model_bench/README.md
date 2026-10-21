# scripts/dev/model_bench/

Benchmarks local Ollama models as torra's planner, using the real
`Agent`/`SafetyClassifier`/`Clarifier`, not synthetic scoring. See
[`REPORT.md`](REPORT.md) for the last measured results and recommendation.

| File | Holds |
|---|---|
| `tasks.py` | The graduated difficulty test battery (13 tasks, tiers 1-8) with an explicit correctness rubric per task |
| `bench.py` | Runs the battery against one or more models, grading via the real gates; `python3 bench.py qwen3:32b devstral:24b qwen3.5:latest` |
| `planner_verifier.py` | Prototype: a second model reviews the planner's plan alongside the deterministic gates (default: `qwen3.5:9b` plans, `devstral:24b` verifies). The verifier returns structured findings; code decides the veto. `--control`, `--fixed`/`--fixed-only`, `--self-check`; `--planner-host`/`--verifier-host` for another machine |
| `fixed_plans.py` | Hand-written good and bad plans for calibrating the verifier independent of any planner (`planner_verifier.py --fixed-only`) |
| `popos_probe.py` | Probes Pop!_OS/System76-specific factual knowledge, to decide whether reference docs are worth adding |
| `domain_exploration.py` | Open-ended, ungraded scenarios (user/permission mgmt, networking, backups, firewall, containers, git) for qualitative review -- extend `SCENARIOS` as new domains come up |
| `edge_cases.py` | Adversarial/boundary scenarios: prompt injection, embedded shell metacharacters, empty/garbage/contradictory input, non-English, signal-in-noise |
| `results.json`, `planner_verifier_results.json` | Raw output from the last run of each script (`planner_verifier_results.json` is the 2026-08-20 run, before the structured-findings format) |
| `planner_verifier_results.<planner>__<verifier>.json`, `planner_verifier_fixed.json` | Raw output of `planner_verifier.py` per model pair, and of `--fixed-only` |

Requires a running `ollama serve` reachable at `OLLAMA_HOST` (default
`http://127.0.0.1:11434`) with the target models already pulled.
