# legacy/

The six-phase incremental build that `harness/` was consolidated from.

Kept for one reason: `README.md` and `HANDOFF.md` cite measurements taken against
this code, and a citation you cannot re-run is not a citation. If you want to
reproduce the 83% clarifier false negative rate, the 5,143x inotify speedup, or
the checkpoint byte figures, the code that produced them is here.

Nothing in the shipped `harness/` package imports from this directory. It is
frozen.

```bash
cd legacy
python3 -m pytest tests/ -q       # the original 121 cases, phase-organised
python3 clarifier_corpus.py       # the three-corpus clarifier scorecard
python3 break_it.py               # phase 1 adversarial probes
python3 break_it_v2.py            # ... through break_it_v6.py
python3 bench_checkpointing.py    # checkpoint overhead measurement
```

The phase reports that used to sit in the repository root were replaced by a
single `README.md` and `HANDOFF.md`. What they contained that still matters is
in section 3 of `HANDOFF.md`: the approaches that were tried and measured worse,
so nobody re-derives them.
