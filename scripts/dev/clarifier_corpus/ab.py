#!/usr/bin/env python3
"""Rule-6 A/B: send each item through harness.py once per arm per round, interleaved ABBA, gently.

Usage: python3 -I ab.py CORPUS.json [CORPUS.json ...] --out DIR --python VENV_PYTHON --workdir-a PATH
       --workdir-b PATH [--rounds 3] [--dry]
Arm A is the current SYSTEM_PROMPT; arm B swaps rule 6 for ab/rule6_b.txt (harness.py --rule6). Items run in
the seed-61 order of drive.py; even positions run A then B, odd positions B then A. Results go to
DIR/results_{A,B}_r{N}.jsonl, one line per item, resumable by (arm, round, id).
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir

import drive  # noqa: E402

RULE6_B = str(Path(__file__).resolve().parent / "ab" / "rule6_b.txt")


def load_items(paths):
    items = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            items += [
                i for i in json.load(fh) if i.get("agreed", True) is True and not i.get("dropped")
            ]
    random.Random(61).shuffle(items)
    return items


def done_ids(path):
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as fh:
        return {json.loads(line)["id"] for line in fh if line.strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--workdir-a", required=True)
    ap.add_argument("--workdir-b", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    default = (Path.home() / ".torra").resolve()
    workdirs = {}
    for arm, wd in (("A", args.workdir_a), ("B", args.workdir_b)):
        w = Path(wd).expanduser().resolve()
        if w == default or default in w.parents:
            print(f"refusing: workdir {w} is or is inside {default}", file=sys.stderr)
            sys.exit(2)
        workdirs[arm] = str(w)
    if workdirs["A"] == workdirs["B"]:
        print("refusing: the two arms need separate workdirs", file=sys.stderr)
        sys.exit(2)
    extra = {"A": (), "B": ("--rule6", RULE6_B)}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    items = load_items(args.corpus)
    state = {"waited": 0}
    for rnd in range(1, args.rounds + 1):
        paths = {arm: out / f"results_{arm}_r{rnd}.jsonl" for arm in "AB"}
        done = {arm: done_ids(paths[arm]) for arm in "AB"}
        pending = [
            (k, i)
            for k, i in enumerate(items)
            if i["id"] not in done["A"] or i["id"] not in done["B"]
        ]
        drive.log(f"round {rnd}: {len(items)} items, {len(pending)} with an arm to run")
        for b in range(0, len(pending), drive.BATCH):
            drive.wait_for_quiet(state, args.dry)
            for k, item in pending[b : b + drive.BATCH]:
                for arm in "AB" if k % 2 == 0 else "BA":
                    if item["id"] in done[arm]:
                        continue
                    if args.dry:
                        start = end = time.time()
                        rc, text = -999, ""
                    else:
                        start, end, rc, text = drive.run_item(
                            item, args.python, workdirs[arm], extra[arm]
                        )
                    m = drive.HARNESS_RE.findall(text)
                    outcome, intent, model = (
                        (m[-1][0], m[-1][1], m[-1][2].strip()) if m else (None, None, None)
                    )
                    rec = {
                        "id": item["id"],
                        "arm": arm,
                        "round": rnd,
                        "label": item.get("label"),
                        "set": item.get("set"),
                        "request": item["request"],
                        "start": start,
                        "end": end,
                        "rc": rc,
                        "outcome": outcome,
                        "intent": intent,
                        "model": model,
                        "tail": "\n".join(text.splitlines()[-25:]),
                    }
                    with open(paths[arm], "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    drive.log(
                        f"r{rnd} {arm} {item['id']} -> {outcome}/{intent} ({model}) {end - start:.1f}s"
                    )


if __name__ == "__main__":
    main()
