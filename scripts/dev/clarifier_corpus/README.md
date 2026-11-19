# scripts/dev/clarifier_corpus/

The 2026-11-19 live measurement of gap 6.1 (does the clarifier stop ambiguous requests and let
specific ones through, against a real local model). Method and decisions:
[`docs/plans/P5_6_1_CLARIFIER_LIVE_PLAN.md`](../../../docs/plans/P5_6_1_CLARIFIER_LIVE_PLAN.md); the
result lives in `docs/STATUS.md`.

| File | Holds |
|---|---|
| `corpus.json` | The author's original 40 ambiguous (`A..`) and 30 specific (`S..`) requests, each with its label and why |
| `corpus_blind.json`, `blind_labels.json` | The same requests without labels, in shuffled order, and a second labeler's verdicts made without seeing the author's |
| `agreement.json` | Author and blind verdict per item; `agreed` is true when they match |
| `corpus_agreed.json` | `corpus.json` plus the `agreed` flag (and `dropped` reason); only agreed items (59: 31 ambiguous, 28 specific) are driven and counted |
| `results_r1.jsonl`, `results_r2.jsonl`, `results_r3.jsonl` | One line per request per round (3 rounds): outcome, intent, model, tail of the `harness.py` output. `S20` produced no telemetry record in any round |
| `probe_torra_plan_cli.jsonl` | Two requests sent through the `torra plan` CLI as a subprocess before the run, to see how it reports a plan |
| `numbers.json` | Output of `numbers.py`: FN/FP with Wilson 95% intervals (overall, per round, per model, per routing rule, per intent), per-item stability, item-level figures, the `CorpusExporter.drift` report, the pre-fix comparison |
| `evidence.txt` | The `torra evidence` aggregates at the time of the run; the lines that quote request text were removed (this repo is public) |
| `harness.py` | One in-process `torra plan` for a request; records the turn like a declined plan and never calls `execute()` |
| `drive.py` | Sends each agreed request through `harness.py` once, in shuffled batches of 10, waiting for a quiet Ollama gateway between batches |
| `label.py` | Labels the corpus requests in `~/.torra/telemetry/labels.jsonl` through the real `torra label` CLI |
| `numbers.py` | Reads the telemetry of the run window and the label store, prints the figures and writes `numbers.json` |

Reproduce `numbers.json` (read-only; needs the `~/.torra/telemetry` of the machine that ran it):

```
uv run python -I scripts/dev/clarifier_corpus/numbers.py            # the 2026-11-19 window
uv run python -I scripts/dev/clarifier_corpus/numbers.py --start EPOCH --end EPOCH
```

Expected headline: false negatives 7/93, false positives 0/81, `S20` the only count mismatch (no
record), drift report present (it needs every corpus request that has a record to be labeled).

**Warning.** `drive.py` writes real turns into `~/.torra/telemetry/tasks.jsonl` and calls a live
local model; `label.py` writes labels into `~/.torra/telemetry/labels.jsonl` (`--dry` only prints
the plan). Neither belongs in a test or CI; `numbers.py` writes only `numbers.json`.
