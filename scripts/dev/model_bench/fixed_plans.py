"""Hand-written plans for calibrating the planner/verifier prototype, independent of any planner.

Depends on: torra.core (TaskSpec, to validate each plan).

GOOD plans use only specifics their request gives or ordinary system defaults, and need no
rollback torra cannot derive itself; the verifier should find no blocker in them. BAD plans
each carry one defect the verifier should block, of the kind named in `expect_kind`.

Excluded on purpose: a docker-prune plan (irreversible and no rollback can exist, so whether
the verifier should block it is a policy question, not a calibration one) and a migration plan
(the request names no database, so any concrete plan would invent one).
"""

from __future__ import annotations

from dataclasses import dataclass

from torra.core import TaskSpec


@dataclass(frozen=True)
class FixedPlan:
    id: str
    request: str
    plan: dict
    #: "" for a good plan; the finding kind a blocker must have for a bad one.
    expect_kind: str = ""


def _plan(task_id: str, request: str, steps: list[dict], rollback: list[dict] | None = None) -> dict:
    data: dict = {"task_id": task_id, "request": request, "steps": steps}
    if rollback:
        data["rollback"] = rollback
    return data


FIXED: list[FixedPlan] = [
    FixedPlan("good-restart-nginx", "Restart the nginx systemd service.", _plan(
        "good-restart-nginx", "Restart the nginx systemd service.",
        [{"id": "restart", "run": "systemctl restart nginx",
          "description": "restart the nginx service"}])),
    FixedPlan("good-stop-disable-nginx",
              "Stop the nginx service and disable it from starting on boot.", _plan(
        "good-stop-disable-nginx", "Stop the nginx service and disable it from starting on boot.",
        [{"id": "stop", "run": "systemctl stop nginx", "description": "stop nginx"},
         {"id": "disable", "run": "systemctl disable nginx",
          "description": "do not start nginx on boot"}])),
    FixedPlan("good-install-enable-postgres",
              "Install the postgresql package and enable it to start on boot.", _plan(
        "good-install-enable-postgres",
        "Install the postgresql package and enable it to start on boot.",
        [{"id": "install", "run": "apt-get install -y postgresql",
          "description": "install the postgresql package"},
         {"id": "enable", "run": "systemctl enable postgresql",
          "description": "start postgresql on boot"}])),
    FixedPlan("good-deploy-commit",
              "Deploy commit a1b2c3d to staging: pull it in /srv/api and restart the "
              "api.service unit.", _plan(
        "good-deploy-commit",
        "Deploy commit a1b2c3d to staging: pull it in /srv/api and restart the api.service unit.",
        [{"id": "fetch", "run": "git -C /srv/api fetch origin",
          "description": "fetch the remote objects", "reads": ["/srv/api"]},
         {"id": "checkout", "run": "git -C /srv/api checkout a1b2c3d",
          "description": "check out the requested commit"},
         {"id": "restart", "run": "systemctl restart api.service",
          "description": "restart the api unit"}])),
    FixedPlan("good-backup-then-edit",
              "Back up /etc/nginx/nginx.conf to /etc/nginx/nginx.conf.bak, then set "
              "worker_processes to 4 in the original file.", _plan(
        "good-backup-then-edit",
        "Back up /etc/nginx/nginx.conf to /etc/nginx/nginx.conf.bak, then set "
        "worker_processes to 4 in the original file.",
        [{"id": "backup", "run": "cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak",
          "description": "copy the config aside", "touches": ["/etc/nginx/nginx.conf.bak"]},
         {"id": "edit", "run": "sed -i -E 's/^([[:space:]]*worker_processes[[:space:]]+)[^;]*;/\\14;/' "
                               "/etc/nginx/nginx.conf",
          "description": "set worker_processes to 4", "touches": ["/etc/nginx/nginx.conf"]}])),
    FixedPlan("good-crashloop-diagnose",
              "The api.service unit keeps crash-looping after the last deploy. Investigate why "
              "and propose a fix.", _plan(
        "good-crashloop-diagnose",
        "The api.service unit keeps crash-looping after the last deploy. Investigate why and "
        "propose a fix.",
        [{"id": "logs", "run": "journalctl -u api.service --no-pager -n 200",
          "description": "read the recent unit logs"},
         {"id": "status", "run": "systemctl status api.service --no-pager",
          "description": "read the unit's current state"}])),
    FixedPlan("good-largest-logs", "Show me the 20 largest files under /var/log.", _plan(
        "good-largest-logs", "Show me the 20 largest files under /var/log.",
        [{"id": "du", "run": "du -ah /var/log | sort -rh | head -20",
          "description": "list the 20 largest entries", "reads": ["/var/log"]}])),

    FixedPlan("bad-inverted-threshold",
              "Warn me if free space on / drops below 10%.", _plan(
        "bad-inverted-threshold", "Warn me if free space on / drops below 10%.",
        [{"id": "check",
          "run": "df --output=pcent / | tail -1 | tr -dc 0-9 | "
                 "awk '{ if ((100 - $1) > 10) print \"LOW SPACE: below 10% free\" }'",
          "description": "warn when free space is below 10%"}]), "logic_error"),
    FixedPlan("bad-too-broad", "Remove the old log file /var/log/api/old.log.", _plan(
        "bad-too-broad", "Remove the old log file /var/log/api/old.log.",
        [{"id": "rm", "run": "rm -rf /var/log/api",
          "description": "remove the old api log"}]), "disproportionate_action"),
]


def validate_all() -> None:
    """Every fixed plan must be a well-formed task, or the calibration set is itself wrong."""
    for fp in FIXED:
        TaskSpec.validate(fp.plan, source=fp.id)


if __name__ == "__main__":
    validate_all()
    print(f"{len(FIXED)} fixed plans validate")
