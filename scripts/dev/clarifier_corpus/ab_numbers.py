#!/usr/bin/env python3
"""Score the rule-6 A/B (`ab.py`) per arm against the pre-registered rule in the plan's §7.3.

Usage: python3 -I ab_numbers.py --results DIR --workdir-a PATH --workdir-b PATH --out FILE.json
A turn asked iff its telemetry record (same request, `at` inside the result line's start/end) has
clarifier_blocked true. Every result line must match exactly one record in its arm's workdir. Records
with no result line (turns ab.py discarded: backend_down, killed, retried) are counted, never scored.
"""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * max(0.0, c - h), 1), round(100 * min(1.0, c + h), 1)]


def rate(k, n):
    return {"k": k, "n": n, "pct": round(100 * k / n, 1) if n else None, "wilson95": wilson(k, n)}


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def subset(line):
    return "heldout" if line["set"] == "heldout" else f"v2_{line['set']}"


def score_arm(arm, recs, lines, errors):
    """Turn- and item-level FN/FP for one arm; appends unmatched lines or records to errors."""
    used = set()
    turns = defaultdict(lambda: {"amb": 0, "fn": 0, "spec": 0, "fp": 0})
    items = defaultdict(lambda: defaultdict(list))
    for x in lines:
        hits = [
            i
            for i, r in enumerate(recs)
            if r["request"] == x["request"] and x["start"] <= r["at"] <= x["end"] and i not in used
        ]
        if len(hits) != 1:
            errors.append(f"{arm} r{x['round']} {x['id']}: {len(hits)} telemetry records")
            continue
        used.add(hits[0])
        asked = bool(recs[hits[0]]["clarifier_blocked"])
        for key in (subset(x), "v2_all" if x["set"] != "heldout" else None, "spec_diagnostic"):
            if key is None or (
                key == "spec_diagnostic" and (x["set"] == "actions" or x["label"] != "specific")
            ):
                continue
            t = turns[key]
            if x["label"] == "ambiguous":
                t["amb"] += 1
                t["fn"] += not asked
            else:
                t["spec"] += 1
                t["fp"] += asked
        items[subset(x)][x["id"]].append((x["label"], asked))
    unscored = Counter(r.get("status") for i, r in enumerate(recs) if i not in used)
    arm_out = {
        "result_lines": len(lines),
        "telemetry_records": len(recs),
        "unscored_records_by_status": dict(unscored),
    }
    if any(x.get("outcome") == "backend_down" or x.get("rc") != 0 for x in lines):
        errors.append(f"{arm}: a result line that is not scorable")
    for key, t in sorted(turns.items()):
        arm_out[key] = {"fn": rate(t["fn"], t["amb"]), "fp": rate(t["fp"], t["spec"])}
    for key, its in sorted(items.items()):
        amb = [v for v in its.values() if v[0][0] == "ambiguous"]
        spec = [v for v in its.values() if v[0][0] == "specific"]
        arm_out[key]["items_missed_once"] = (
            f"{sum(any(not a for _, a in v) for v in amb)}/{len(amb)}"
        )
        arm_out[key]["items_asked_once"] = f"{sum(any(a for _, a in v) for v in spec)}/{len(spec)}"
    return arm_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--workdir-a", required=True)
    ap.add_argument("--workdir-b", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = {"arms": {}}
    errors = []
    for arm, wd in (("A", args.workdir_a), ("B", args.workdir_b)):
        recs = load_jsonl(Path(wd) / "telemetry" / "tasks.jsonl")
        lines = [
            x
            for p in sorted(Path(args.results).glob(f"results_{arm}_r*.jsonl"))
            for x in load_jsonl(p)
        ]
        out["arms"][arm] = score_arm(arm, recs, lines, errors)
    a, b = out["arms"]["A"], out["arms"]["B"]

    def pct(arm, key, kind):
        return arm[key][kind]["pct"]

    checks = {
        "heldout FN <= 20%": pct(b, "heldout", "fn") <= 20,
        "v2 overall FN <= 20%": pct(b, "v2_all", "fn") <= 20,
        "v2 FP not > A + 5pt": pct(b, "v2_all", "fp") <= pct(a, "v2_all", "fp") + 5,
        "heldout FP not > A + 5pt": pct(b, "heldout", "fp") <= pct(a, "heldout", "fp") + 5,
        "specific diagnostic FP <= 10%": pct(b, "spec_diagnostic", "fp") <= 10,
    }
    out["rule_B"] = checks
    out["B_wins"] = all(checks.values())
    out["errors"] = errors
    Path(args.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    for arm in "AB":
        print(f"== arm {arm}")
        for key, v in out["arms"][arm].items():
            if isinstance(v, dict) and "fn" in v:
                print(
                    f"  {key:16} FN {v['fn']['k']}/{v['fn']['n']} = {v['fn']['pct']}% {v['fn']['wilson95']}"
                    f"   FP {v['fp']['k']}/{v['fp']['n']} = {v['fp']['pct']}%"
                    f"   items {v.get('items_missed_once', '-')} missed, {v.get('items_asked_once', '-')} asked"
                )
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"B wins: {out['B_wins']}")
    for e in errors:
        print("ERROR", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
