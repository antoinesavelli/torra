"""In-process `torra plan` that also records the plan turn like a declined plan.

Run: <venv python> -I harness.py "<request>"   (never calls execute(); writes a real turn to ~/.torra).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from torra.cli import build_torra, print_plan  # noqa: E402
from torra.pipeline import DEFAULT_WORKDIR, Outcome  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: harness.py REQUEST", file=sys.stderr)
        return 64
    request = sys.argv[1]
    torra = build_torra(DEFAULT_WORKDIR)
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
