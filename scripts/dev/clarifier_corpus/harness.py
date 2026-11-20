"""In-process `torra plan` that also records the plan turn like a declined plan.

Run: <venv python> -I harness.py --workdir PATH [--rule6 FILE] "<request>"   (never calls execute();
writes a real turn to PATH/telemetry/tasks.jsonl; PATH may not be ~/.torra or inside it). --rule6 swaps
SYSTEM_PROMPT's rule 6 (the text in ab/rule6_a.txt) for FILE's text, in this process only (the A/B).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from torra.cli import build_torra, print_plan  # noqa: E402
from torra.pipeline import DEFAULT_WORKDIR, Outcome  # noqa: E402
from torra.prompts import SYSTEM_PROMPT  # noqa: E402

RULE6_A = Path(__file__).resolve().parent / "ab" / "rule6_a.txt"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, help="torra workdir (never ~/.torra)")
    ap.add_argument("--rule6", default=None, help="file whose text replaces rule 6 (arm B)")
    ap.add_argument("request")
    args = ap.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    default = DEFAULT_WORKDIR.expanduser().resolve()
    if workdir == default or default in workdir.parents:
        print(f"refusing: workdir {workdir} is or is inside {default}", file=sys.stderr)
        return 2
    workdir.mkdir(parents=True, exist_ok=True)
    request = args.request
    torra = build_torra(workdir)
    if args.rule6:
        old = RULE6_A.read_text(encoding="utf-8")
        if SYSTEM_PROMPT.count(old) != 1 or torra.agent.system_prompt != SYSTEM_PROMPT:
            print(
                "refusing: rule 6 in SYSTEM_PROMPT no longer matches ab/rule6_a.txt",
                file=sys.stderr,
            )
            torra.close()
            return 2
        torra.agent.system_prompt = SYSTEM_PROMPT.replace(
            old, Path(args.rule6).read_text(encoding="utf-8")
        )
    try:
        result = torra.plan(request, model=None)
        if result.turn and result.turn.message:
            print(f"\n{result.turn.message}")
        if result.outcome is Outcome.NEEDS_ANSWER:
            print("\nquestions:")
            for question in result.turn.questions if result.turn else []:
                print(f"  ? {question}")
        elif result.outcome is Outcome.AWAITING_APPROVAL and result.plan is not None:
            print_plan(result.plan)
            result = torra.reject(result)  # writes the telemetry record; runs nothing
        else:
            print(f"\noutcome: {result.outcome.value}")
            if result.abort_reason:
                print(f"reason : {result.abort_reason}")
        turn = result.turn
        intent = turn.intent.value if turn else ""
        model = (turn.model if turn else "") or ""
        print(f"HARNESS outcome={result.outcome.value} intent={intent} model={model}")
    finally:
        torra.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
