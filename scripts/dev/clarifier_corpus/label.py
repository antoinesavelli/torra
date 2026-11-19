#!/usr/bin/env python3
"""Label the agreed corpus requests through the real `torra label` CLI.

Usage: <venv python> -I label.py [--dry]
Writes ~/.torra/telemetry/labels.jsonl (unless --dry).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

W = str(Path(__file__).resolve().parents[3])
S = str(Path(__file__).resolve().parent)
LABELER = "claude-p5-6.1"
sys.path.insert(0, W)

from torra.pipeline import DEFAULT_WORKDIR  # noqa: E402
from torra.telemetry.labels import LabelStore  # noqa: E402
from torra.telemetry.records import TaskLogger  # noqa: E402


def one_line(text):
    return " ".join(str(text).split())


def plan_answers(pending, by_request):
    """stdin answers for `torra label`, and request -> is-ambiguous for the corpus items."""
    answers = []  # (request, stdin text)
    expected = {}
    for request in pending:
        item = by_request.get(request)
        if item is None:
            answers.append((request, "k\n"))
        elif item["label"] == "ambiguous":
            why = one_line(f"p5-6.1 corpus {item['id']}: {item['why']}")
            answers.append((request, f"a\n{why}\n"))
            expected[request] = True
        else:
            answers.append((request, "s\n"))
            expected[request] = False
    return answers, expected


def check_labels(before, after, expected):
    """Problems found comparing the label store before and after the run."""
    problems = []
    for request, amb in expected.items():
        lab = after.get(request)
        if lab is None:
            problems.append(f"missing label: {request!r}")
        elif lab.ambiguous != amb or lab.labeler != LABELER:
            problems.append(
                f"wrong label: {request!r} -> ambiguous={lab.ambiguous} labeler={lab.labeler!r}"
            )
    extra = set(after) - set(before) - set(expected)
    if extra:
        problems.append(f"unexpected labels added: {sorted(extra)}")
    changed = [r for r in before if after.get(r) != before[r]]
    if changed:
        problems.append(f"pre-existing labels changed: {changed}")
    return problems


def main():
    dry = "--dry" in sys.argv[1:]
    corpus = [c for c in json.load(open(os.path.join(S, "corpus_agreed.json"))) if c.get("agreed")]
    by_request = {c["request"]: c for c in corpus}
    if len(by_request) != len(corpus):
        print("WARNING: duplicate request texts in the agreed corpus")

    workdir = DEFAULT_WORKDIR
    logger = TaskLogger(workdir / "telemetry" / "tasks.jsonl")
    store = LabelStore(workdir / "telemetry" / "labels.jsonl")
    before = store.all()
    records = logger.read()
    pending = store.unlabeled(records)

    answers, expected = plan_answers(pending, by_request)

    recorded = {r.request for r in records}
    not_pending = [c for c in corpus if c["request"] not in expected]
    print(f"pending requests      : {len(pending)}")
    print(f"  corpus items        : {len(expected)} of {len(corpus)} agreed")
    print(f"  other (will skip k) : {len(pending) - len(expected)}")
    for c in not_pending:
        state = (
            "ALREADY LABELED"
            if c["request"] in before
            else ("no record in tasks.jsonl" if c["request"] not in recorded else "?")
        )
        print(f"  corpus item not pending: {c['id']} {c['request']!r}: {state}")
    if dry:
        print("first 5 planned answers:")
        for request, text in answers[:5]:
            print(f"  {request!r} -> {text!r}")
        print("--dry: nothing run")
        return 0
    if not pending:
        print("nothing pending")
        return 1

    stdin = "".join(text for _r, text in answers)
    code = f"import sys; sys.path.insert(0, {W!r}); from torra.cli import main; sys.exit(main())"
    env = dict(os.environ, USER=LABELER)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code, "label", "--limit", str(len(pending))],
        input=stdin,
        text=True,
        capture_output=True,
        cwd=W,
        env=env,
        timeout=300,
    )
    print(f"torra label rc={proc.returncode}")
    print("stdout tail:\n" + "\n".join(proc.stdout.splitlines()[-4:]))
    if proc.stderr.strip():
        print("stderr:\n" + proc.stderr[-1000:])

    after = LabelStore(workdir / "telemetry" / "labels.jsonl").all()
    problems = check_labels(before, after, expected)
    added = set(after) - set(before)
    n_amb = sum(1 for v in expected.values() if v)
    print(
        f"labels before={len(before)} after={len(after)} added={len(added)} "
        f"(expected {len(expected)}: {n_amb} ambiguous, {len(expected) - n_amb} specific)"
    )
    if problems:
        print("ASSERTION FAILED:")
        for p in problems:
            print("  " + p)
        return 1
    print("OK: every pending agreed corpus request has the expected label from", LABELER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
