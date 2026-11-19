#!/usr/bin/env python3
"""Read-only numbers for the P5 6.1 clarifier live run. Writes numbers.json next to it.

Usage: <venv python> -I numbers.py [--start EPOCH] [--end EPOCH]
Reads ~/.torra/telemetry (read-only); the defaults are the 2026-11-19 run window.
"""

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

W = str(Path(__file__).resolve().parents[3])
S = str(Path(__file__).resolve().parent)
RUN_START = 1795118738
RUN_END = 1795120673
LABELER = "claude-p5-6.1"
PREFIX_CUTOFF = 1787841000
sys.path.insert(0, W)

from torra.pipeline import DEFAULT_WORKDIR  # noqa: E402
from torra.telemetry.audit import CorpusExporter  # noqa: E402
from torra.telemetry.labels import LabelStore  # noqa: E402
from torra.telemetry.records import TaskLogger  # noqa: E402

OUT = {}


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def rate(k, n):
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def fmt(r):
    if not r["n"]:
        return "n/a (n=0)"
    lo, hi = r["wilson95"]
    return f"{r['k']}/{r['n']} = {r['rate']:.1%}  (Wilson 95%: {lo:.1%} - {hi:.1%})"


def fn_fp(records, label_of):
    """label_of(request) -> 'ambiguous' | 'specific' | None."""
    amb = [r for r in records if label_of(r.request) == "ambiguous"]
    spec = [r for r in records if label_of(r.request) == "specific"]
    return (
        rate(sum(1 for r in amb if not r.clarifier_blocked), len(amb)),
        rate(sum(1 for r in spec if r.clarifier_blocked), len(spec)),
    )


def breakdown(records, label_of, keyfn):
    groups = defaultdict(list)
    for r in records:
        groups[keyfn(r) or "(none)"].append(r)
    out = {}
    for key in sorted(groups):
        fn, fp = fn_fp(groups[key], label_of)
        out[key] = {"records": len(groups[key]), "fn": fn, "fp": fp}
    return out


def print_breakdown(title, data):
    print(f"\n-- {title}")
    for key, v in data.items():
        print(f"  {key}  [{v['records']} records]")
        print(f"      FN {fmt(v['fn'])}")
        print(f"      FP {fmt(v['fp'])}")


def window_records(all_records, start, end, corpus_label):
    """The run-window records whose request is in the corpus; warns about the rest."""
    window = [r for r in all_records if start <= r.at <= end]
    stray = [r for r in window if r.request not in corpus_label]
    if stray:
        print(f"WARNING: {len(stray)} run-window record(s) not in the corpus:")
        for r in stray:
            print(f"   at={r.at:.0f} {r.request!r}")
    OUT["stray_window_records"] = [{"at": r.at, "request": r.request} for r in stray]
    recs = [r for r in window if r.request in corpus_label]
    print(f"run-window corpus records: {len(recs)}")
    return recs


def load_results(recs, corpus_ids):
    """Read results_r*.jsonl and compare per-request record counts with result lines."""
    results = {}  # round -> list of dicts
    for n in (1, 2, 3):
        p = os.path.join(S, f"results_r{n}.jsonl")
        if os.path.exists(p):
            results[f"r{n}"] = [json.loads(x) for x in open(p) if x.strip()]
    res_count = Counter(x["request"] for lines in results.values() for x in lines)
    rec_count = Counter(r.request for r in recs)
    mism = {
        q: (rec_count.get(q, 0), res_count.get(q, 0))
        for q in set(rec_count) | set(res_count)
        if rec_count.get(q, 0) != res_count.get(q, 0)
    }
    if mism:
        print(f"WARNING: {len(mism)} request(s) whose record count != result-line count:")
        for q, (a, b) in sorted(mism.items(), key=lambda kv: corpus_ids.get(kv[0], "")):
            print(f"   {corpus_ids.get(q, '?')} {q!r}: {a} vs {b}")
    OUT["count_mismatches"] = {
        corpus_ids.get(q, q): {"records": a, "results": b} for q, (a, b) in mism.items()
    }
    print("result lines per round: " + ", ".join(f"{k}={len(v)}" for k, v in results.items()))
    OUT["result_lines"] = {k: len(v) for k, v in results.items()}
    return results, rec_count


def drift_section(corpus, recs, rec_count, stored, store):
    """CorpusExporter.drift, once every corpus request that has a record is labeled."""
    ran = [c for c in corpus if c["request"] in rec_count]
    stored_ok = [
        c
        for c in ran
        if (
            stored.get(c["request"]) is not None
            and stored[c["request"]].labeler == LABELER
            and stored[c["request"]].ambiguous == (c["label"] == "ambiguous")
        )
    ]
    print(
        f"\n== CorpusExporter.drift (labels.jsonl holds {len(stored_ok)}/{len(ran)} "
        f"corpus labels by {LABELER}, for the corpus requests with a run-window record)"
    )
    if len(stored_ok) != len(ran):
        print("  not yet labeled: run label.py first; skipping drift().")
        OUT["drift"] = None
        return
    rep = CorpusExporter(store).drift(recs)
    print(rep.render())
    OUT["drift"] = rep.to_dict()
    OUT["drift"]["rendered"] = rep.render()


def per_round_section(results, recs, label_of):
    """Match each result line to the unused record of the same request nearest its time."""
    used = set()
    round_of = {}
    for rnd, lines in results.items():
        for x in sorted(lines, key=lambda x: x["start"]):
            cands = [
                r
                for r in recs
                if r.request == x["request"]
                and id(r) not in used
                and x["start"] - 5 <= r.at <= x["end"] + 30
            ]
            if not cands:
                continue
            best = min(cands, key=lambda r: abs(r.at - x["end"]))
            used.add(id(best))
            round_of[id(best)] = rnd
    unmatched = [r for r in recs if id(r) not in used]
    if unmatched:
        print(
            f"\nWARNING: {len(unmatched)} corpus record(s) not matched to a result line "
            "(excluded from per-round)"
        )
    per_round = {}
    print("\n-- per round")
    for rnd in sorted(results):
        sub = [r for r in recs if round_of.get(id(r)) == rnd]
        f1, p1 = fn_fp(sub, label_of)
        per_round[rnd] = {"records": len(sub), "fn": f1, "fp": p1}
        print(f"  {rnd} [{len(sub)} records]\n      FN {fmt(f1)}\n      FP {fmt(p1)}")
    OUT["per_round"] = per_round
    OUT["unmatched_to_round"] = len(unmatched)


def stability_section(recs, corpus_label, corpus_ids):
    """Per-item asks out of runs, plus the item-level missed / asked-at-least-once figures."""
    asks = defaultdict(list)
    for r in recs:
        asks[r.request].append(bool(r.clarifier_blocked))
    stability = {}
    print("\n-- per-item stability (clarifier asks out of runs)")
    for label in ("ambiguous", "specific"):
        hist = Counter()
        items = {}
        for q, lab in corpus_label.items():
            if lab != label:
                continue
            runs = asks.get(q, [])
            items[corpus_ids[q]] = f"{sum(runs)}/{len(runs)}"
            hist[sum(runs)] += 1
        dist = {str(k): hist[k] for k in sorted(hist)}
        stability[label] = {"histogram_asks": dist, "items": items}
        print(
            f"  {label}: items by number of asks: "
            + ", ".join(f"{k} asks={v}" for k, v in dist.items())
        )
        print("    " + " ".join(f"{i}:{v}" for i, v in sorted(items.items())))
    OUT["stability"] = stability

    # item level: an item counts as missed (or asked) if ANY of its runs was
    amb_items = [q for q, lab in corpus_label.items() if lab == "ambiguous" and asks.get(q)]
    spec_items = [q for q, lab in corpus_label.items() if lab == "specific" and asks.get(q)]
    item_missed = rate(sum(1 for q in amb_items if not all(asks[q])), len(amb_items))
    item_asked = rate(sum(1 for q in spec_items if any(asks[q])), len(spec_items))
    print("\n-- item level (items with at least one record)")
    print(f"  ambiguous items missed at least once : {fmt(item_missed)}")
    print(f"  specific items asked at least once   : {fmt(item_asked)}")
    OUT["item_level"] = {
        "ambiguous_missed_at_least_once": item_missed,
        "specific_asked_at_least_once": item_asked,
    }


def prefix_section(all_records, stored):
    """FN/FP of the pre-fix records against the labels not written by this corpus."""
    print(
        f"\n== Pre-fix comparison (records at < {PREFIX_CUTOFF}, "
        f"labels in labels.jsonl not by {LABELER})"
    )
    old = {q: lab for q, lab in stored.items() if lab.labeler != LABELER}
    pre = [r for r in all_records if r.at < PREFIX_CUTOFF and r.request in old]
    amb = [r for r in pre if old[r.request].ambiguous]
    spec = [r for r in pre if not old[r.request].ambiguous]
    pfn = rate(sum(1 for r in amb if not r.clarifier_blocked), len(amb))
    pfp = rate(sum(1 for r in spec if r.clarifier_blocked), len(spec))
    print(f"  labels considered: {len(old)}; matching records: {len(pre)}")
    print(f"  FN {fmt(pfn)}")
    print(f"  FP {fmt(pfp)}")
    OUT["prefix"] = {"labels": len(old), "records": len(pre), "fn": pfn, "fp": pfp}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=RUN_START, help="run window start, epoch s")
    ap.add_argument("--end", type=float, default=RUN_END, help="run window end, epoch s")
    args = ap.parse_args()
    tz = ZoneInfo("America/New_York")
    cut = datetime.fromtimestamp(PREFIX_CUTOFF, tz).isoformat()
    cut_utc = datetime.fromtimestamp(PREFIX_CUTOFF, ZoneInfo("UTC")).isoformat()
    print(f"pre-fix cutoff {PREFIX_CUTOFF} = {cut}  (utc {cut_utc})")
    OUT["prefix_cutoff"] = {"epoch": PREFIX_CUTOFF, "america_new_york": cut}
    print(f"run window {args.start} .. {args.end}")
    OUT["run_window"] = {"start": args.start, "end": args.end}

    corpus = [c for c in json.load(open(os.path.join(S, "corpus_agreed.json"))) if c.get("agreed")]
    corpus_label = {c["request"]: c["label"] for c in corpus}
    corpus_ids = {c["request"]: c["id"] for c in corpus}
    print(
        f"agreed corpus: {len(corpus)} items "
        f"({sum(1 for c in corpus if c['label'] == 'ambiguous')} ambiguous, "
        f"{sum(1 for c in corpus if c['label'] == 'specific')} specific)"
    )

    workdir = DEFAULT_WORKDIR
    all_records = TaskLogger(workdir / "telemetry" / "tasks.jsonl").read()
    store = LabelStore(workdir / "telemetry" / "labels.jsonl")
    stored = store.all()

    recs = window_records(all_records, args.start, args.end, corpus_label)
    results, rec_count = load_results(recs, corpus_ids)

    label_of = corpus_label.get
    fn, fp = fn_fp(recs, label_of)
    print("\n== Headline (labels from the corpus itself)")
    print(f"  FN rate (ambiguous not blocked): {fmt(fn)}")
    print(f"  FP rate (specific blocked)    : {fmt(fp)}")
    OUT["headline"] = {"fn": fn, "fp": fp}

    drift_section(corpus, recs, rec_count, stored, store)

    for name, keyfn in (
        ("model", lambda r: r.model),
        ("routing_rule", lambda r: r.routing_rule),
        ("intent", lambda r: r.intent),
    ):
        data = breakdown(recs, label_of, keyfn)
        print_breakdown(f"by {name}", data)
        OUT[f"by_{name}"] = data

    per_round_section(results, recs, label_of)
    stability_section(recs, corpus_label, corpus_ids)
    prefix_section(all_records, stored)

    out_path = os.path.join(S, "numbers.json")
    with open(out_path, "w") as fh:
        json.dump(OUT, fh, indent=2, sort_keys=True, default=str)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
