#!/usr/bin/env python3
"""Send each corpus request once through `harness.py` (plan, then reject) against a live local model, gently.

Usage: python3 -I drive.py CORPUS.json RESULTS.jsonl --python VENV_PYTHON --workdir PATH [--only ID,ID] [--dry]
PATH is the torra workdir the turns are written to (passed to harness.py; never ~/.torra).
"""

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

CWD = str(Path(__file__).resolve().parents[3])
STATUS_URL = "http://127.0.0.1:11434/pastore/status"
BATCH = 10
POLL_S = 60
MAX_WAIT_S = 3 * 3600
RUN_TIMEOUT_S = 900
HARNESS = str(Path(__file__).resolve().parent / "harness.py")
HARNESS_RE = re.compile(r"^HARNESS outcome=(\S*) intent=(\S*) model=(.*)$", re.M)


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def bench_lines():
    """Running benchmark processes, excluding pgrep wait-loops and this script itself."""
    try:
        out = subprocess.run(
            ["pgrep", "-af", r"m bench\."], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"pgrep failed ({exc}); treating as busy")
        return ["<pgrep failed>"]
    me = {str(os.getpid()), str(os.getppid())}
    keep = []
    for line in out.splitlines():
        if not line.strip():
            continue
        pid = line.split(None, 1)[0]
        if pid in me or "pgrep" in line or "while pgrep" in line:
            continue
        keep.append(line)
    return keep


def _all_zero(value):
    if isinstance(value, dict):
        return all(_all_zero(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return len(value) == 0 or all(_all_zero(v) for v in value)
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return False  # unknown shape: not provably idle


def gateway_idle():
    """Return (idle: bool, detail: str)."""
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            data = json.load(resp)
        ws = data["hosts"]["workstation"]
        in_flight = ws["in_flight"]
        queued = ws["queued"]
    except Exception as exc:  # any failure means wait
        return False, f"status call failed: {exc!r}"
    idle = _all_zero(in_flight) and _all_zero(queued)
    return idle, f"in_flight={in_flight!r} queued={queued!r}"


def wait_for_quiet(state, dry):
    if dry:
        b = bench_lines()
        idle, detail = gateway_idle()
        log(f"[dry] load check: bench_lines={len(b)} gateway_idle={idle} ({detail})")
        return
    while True:
        b = bench_lines()
        idle, detail = gateway_idle()
        if not b and idle:
            return
        if state["waited"] >= MAX_WAIT_S:
            log("gave up waiting for a quiet host after 3 hours")
            sys.exit(2)
        reasons = []
        if b:
            reasons.append(f"bench running: {b[0][:100]}")
        if not idle:
            reasons.append(detail)
        log("waiting: " + "; ".join(reasons))
        time.sleep(POLL_S)
        state["waited"] += POLL_S


def run_item(item, python, workdir, extra=()):
    start = time.time()
    rc = None
    out = b""
    proc = subprocess.Popen(
        [python, "-I", HARNESS, "--workdir", workdir, *extra, item["request"]],
        cwd=CWD,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=RUN_TIMEOUT_S)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.SubprocessError:
            out = b""
    return start, time.time(), rc, out.decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("results")
    ap.add_argument("--python", required=True, help="venv interpreter that runs harness.py")
    ap.add_argument("--workdir", required=True, help="torra workdir for the turns (never ~/.torra)")
    ap.add_argument("--only", default=None)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    default = (Path.home() / ".torra").resolve()  # torra.pipeline.DEFAULT_WORKDIR
    if workdir == default or default in workdir.parents:
        print(f"refusing: workdir {workdir} is or is inside {default}", file=sys.stderr)
        sys.exit(2)
    args.workdir = str(workdir)

    with open(args.corpus, encoding="utf-8") as fh:
        items = json.load(fh)
    items = [i for i in items if i.get("agreed", True) is True]
    random.Random(61).shuffle(items)
    if args.only:
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        items = [i for i in items if i["id"] in only]

    done = set()
    if os.path.exists(args.results):
        with open(args.results, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    continue
    pending = [i for i in items if i["id"] not in done]
    log(
        f"{len(items)} selected, {len(done & {i['id'] for i in items})} already done, {len(pending)} to run"
    )

    state = {"waited": 0}
    with open(args.results, "a", encoding="utf-8") as out:
        for b in range(0, len(pending), BATCH):
            batch = pending[b : b + BATCH]
            wait_for_quiet(state, args.dry)
            for item in batch:
                if args.dry:
                    start = end = time.time()
                    rc, text = -999, ""
                else:
                    start, end, rc, text = run_item(item, args.python, args.workdir)
                m = HARNESS_RE.findall(text)
                outcome, intent, model = (
                    (m[-1][0], m[-1][1], m[-1][2].strip()) if m else (None, None, None)
                )
                rec = {
                    "id": item["id"],
                    "label": item.get("label"),
                    "request": item["request"],
                    "start": start,
                    "end": end,
                    "rc": rc,
                    "outcome": outcome,
                    "intent": intent,
                    "model": model,
                    "tail": "\n".join(text.splitlines()[-25:]),
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                print(f"{item['id']} rc={rc} {end - start:.1f}s", flush=True)


if __name__ == "__main__":
    main()
