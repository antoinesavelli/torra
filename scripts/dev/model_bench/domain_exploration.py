#!/usr/bin/env python3
"""Explore devstral (the recommended default) across domains the graded battery doesn't cover.

Depends on: bench.py (transport/metrics), torra.agent, torra.core, torra.gates. Unlike
tasks.py/bench.py, these scenarios have no single objectively-correct answer to grade against
-- this prints full detail (message, steps, risk levels, rollback) for manual review rather
than an automatic pass/fail, the same way the disk-cleanup scenario in
scripts/dev/model_bench/REPORT.md was reviewed. Extend SCENARIOS as new domains come up.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import OLLAMA_HOST, make_metrics_transport  # noqa: E402

from torra.agent import Agent, AgentError, Intent  # noqa: E402
from torra.core import OllamaClient, OllamaError, observe_machine  # noqa: E402
from torra.gates import Clarifier, SafetyClassifier  # noqa: E402

MODEL = "devstral:24b"


@dataclass
class Scenario:
    """One open-ended, real-world request with no single correct answer to grade against."""

    id: str
    domain: str
    request: str


SCENARIOS: list[Scenario] = [
    Scenario("user-mgmt", "user/permission management",
             "Add a new user 'developer' with sudo access and set up SSH key "
             "authentication for them."),
    Scenario("network-diag", "network diagnostics",
             "This machine can't reach the internet. Figure out why."),
    Scenario("backup-cron", "backup scheduling",
             "Set up a nightly backup of /srv/api to /mnt/backups, keeping 14 "
             "days of history."),
    Scenario("log-rotation", "log rotation",
             "Configure log rotation for /var/log/myapp.log: rotate weekly, "
             "keep 4 weeks."),
    Scenario("security-updates", "package security updates",
             "Check for and apply security updates only, not a full system upgrade."),
    Scenario("firewall", "firewall/security hardening",
             "Open port 8080 for the new service, but make sure everything "
             "else stays closed."),
    Scenario("container-cpu", "container troubleshooting",
             "A docker container called 'worker' is pegging the CPU at 100%. "
             "Investigate and fix it."),
    Scenario("git-history", "version control",
             "Squash the last 5 commits on the current branch into one, with "
             "a clean commit message."),
    Scenario("xlsx-edit", "office document editing",
             "Add a 'Total' row at the bottom of the 'Marketing' column in "
             "/home/user/Documents/budget.xlsx and save it."),
    Scenario("odt-edit", "office document editing",
             "Replace every occurrence of 'Acme Corp' with 'Globex Inc' in "
             "/home/user/Documents/contract.odt."),
]


def main() -> int:
    metrics: list = []
    ollama = OllamaClient(host=OLLAMA_HOST, timeout=180.0,
                          transport=make_metrics_transport(metrics))
    state = observe_machine(ollama=ollama)
    agent = Agent(ollama=ollama, clarifier=Clarifier(), safety=SafetyClassifier(),
                  observer=lambda: state)
    agent.observe()

    print(f"model={MODEL}\n")
    for scenario in SCENARIOS:
        agent.reset()
        print(f"{'=' * 70}\n{scenario.id}  [{scenario.domain}]\n{'=' * 70}")
        print(f"request: {scenario.request}")
        try:
            turn = agent.converse(scenario.request, model=MODEL)
        except (AgentError, OllamaError) as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}\n")
            continue

        print(f"intent: {turn.intent.value}  (repairs: {turn.repairs})")
        print(f"message: {turn.message}")
        if turn.questions:
            print(f"questions: {turn.questions}")
        if turn.intent is Intent.PLAN and turn.spec is not None:
            print(f"rollback: {turn.spec.rollback}")
            safety = SafetyClassifier()
            for s in turn.spec.to_steps(dry_run=True):
                verdict = safety.classify(s.command)
                print(f"  [{verdict.level.value:10}] {s.command}  (touches={s.touches})")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
