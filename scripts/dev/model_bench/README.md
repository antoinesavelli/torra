# scripts/dev/model_bench/

Benchmarks local Ollama models as the harness's planner, using the real
`Agent`/`SafetyClassifier`/`Clarifier`, not synthetic scoring. See
[`REPORT.md`](REPORT.md) for the last measured results and recommendation.

| File | Holds |
|---|---|
| `tasks.py` | The graduated difficulty test battery (13 tasks, tiers 1-8) with an explicit correctness rubric per task |
| `bench.py` | Runs the battery against one or more models, grading via the real gates; `python3 bench.py qwen3:32b devstral:24b qwen3.5:latest` |
| `planner_verifier.py` | Prototype: a fast model reviews a larger model's plan as a second opinion alongside the deterministic gates |
| `popos_probe.py` | Probes Pop!_OS/System76-specific factual knowledge, to decide whether reference docs are worth adding |
| `domain_exploration.py` | Open-ended, ungraded scenarios (user/permission mgmt, networking, backups, firewall, containers, git) for qualitative review -- extend `SCENARIOS` as new domains come up |
| `edge_cases.py` | Adversarial/boundary scenarios: prompt injection, embedded shell metacharacters, empty/garbage/contradictory input, non-English, signal-in-noise |
| `results.json`, `planner_verifier_results.json` | Raw output from the last run of each script |

Requires a running `ollama serve` reachable at `OLLAMA_HOST` (default
`http://127.0.0.1:11434`) with the target models already pulled.
