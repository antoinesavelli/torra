"""Fixes from the second independent review (HANDOFF.md §2.74).

Depends on: torra.agent (Agent, AgentError), torra.core (TaskSpec, TaskSpecError), torra.gates
(RiskLevel, SafetyClassifier), torra.telemetry.records (redact_secrets).
"""

from __future__ import annotations

import time

import pytest
from test_agent import build_agent

from torra.agent import Agent, AgentError
from torra.core import TaskSpec, TaskSpecError
from torra.gates import RiskLevel, SafetyClassifier
from torra.telemetry.records import redact_secrets

B = "x7Qk9" * 8  # a 40-character body that is not any real credential

MORE_SECRETS = [
    "export DATABASE_PASS=" + B, "credentials: " + B, "sk_live_" + B, "rk_live_" + B,
    "npm_" + B, "pypi-" + B, "glpat-" + B[:24], "SG." + B[:22] + "." + B[:22], "ASIA" + "Q7K9X2" * 3,
    "redis://:" + B + "@cache.internal:6379", "https://" + B + "@github.com/x/y.git",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\n" + B + "\n-----END PGP PRIVATE KEY BLOCK-----",
    "curl -u admin:" + B + " https://x", "curl --user admin:" + B + " https://x",
    "Authorization: Basic " + B, "mysql --password " + B + " db", "sshpass -p " + B + " ssh x",
    "machine h login u\npassword " + B, "DefaultEndpointsProtocol=https;AccountKey=" + B + ";",
    "https://hooks.slack.com/services/T000/B000/" + B, '{"auth": "' + B + '"}',
    "https://api.example.com/v1?key=" + B + "&x=1", "Cookie: session=" + B,
    "password：" + B, "{'password': '" + B + "'}", "psk=" + B,
]


@pytest.mark.parametrize("text", MORE_SECRETS, ids=[t[:26] for t in MORE_SECRETS])
def test_more_realistic_secrets_are_scrubbed(text: str) -> None:
    assert B[:20] not in redact_secrets(f"before {text}\nafter")


@pytest.mark.parametrize("text", ["password-" * 1000, "a.a." * 5000, "token=" * 3000, "sk-" * 4000,
                                  "A" * 50000, "key." * 5000])
def test_redaction_stays_fast_on_hostile_input(text: str) -> None:
    started = time.perf_counter()
    redact_secrets(text)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("command", [
    "dd if=.env of=/tmp/x", "cat .e*", "cat .en?", "cat .e[n]v", "while read l; do echo $l; done < .env",
    "python3 -c \"print(open('/srv/app/.env').read())\"", "cat ~/.config/gh/hosts.yml",
    "cat ~/.docker/config.json", "cat ~/.kube/config", "cat ~/.bash_history",
    "cat terraform.tfstate", "cat wp-config.php", "cat /proc/self/environ", "cat .ENV",
    "cat ~/.config/rclone/rclone.conf", "cat ~/.config/gcloud/credentials.db", "printenv",
])
def test_the_remaining_credential_reads_ask(command: str) -> None:
    verdict = SafetyClassifier().classify(command)
    assert verdict.needs_confirmation, (command, verdict.level)


@pytest.mark.parametrize("command", ["cat *", "grep x *", "wc -l *.py", "cat .gitignore", "cat .bashrc",
                                     "ls .e*", "printenv HOME"])
def test_neighbours_of_those_reads_still_run(command: str) -> None:
    assert not SafetyClassifier().classify(command).needs_confirmation, command


@pytest.mark.parametrize("task", [
    {"rollback": 5}, {"rollback": True}, {"rollback": "x"},
    {"steps": [{"id": "s", "run": "true", "estimated_seconds": 10**400}]},
    {"steps": [{"id": "s", "run": "true", "touches": ["a\x00b"]}]},
    {"steps": [{"id": "s", "run": "true", "touches": ["/" + "a" * 5000]}]},
    {"steps": [{"id": "s", "run": "true", "reads": [5]}]},
])
def test_hostile_task_fields_are_a_task_error_not_a_crash(task: dict) -> None:
    base = {"task_id": "t", "request": "r", "steps": [{"id": "s", "run": "true"}]}
    with pytest.raises(TaskSpecError):
        TaskSpec.validate({**base, **task})


@pytest.mark.parametrize("reply", [
    '{"intent": "plan", "task": {"task_id": "t", "request": "r", "steps": [{"id": "s", "run": "true"}], "rollback": 5}}',
    '{"intent": "plan", "task": {"task_id": "t", "request": "r", "steps": [{"id": "s", "run": "true", "touches": ["a\\u0000b"]}]}}',
    '{"intent": "answer", "message": ' + "1" + "0" * 5000 + "}",
    '{"x": ' + "1" * 5000 + "}",
])
def test_a_hostile_reply_never_escapes_as_an_unexpected_exception(reply: str) -> None:
    agent = build_agent(lambda p, pay, t: {"message": {"content": reply}})
    try:
        agent.converse("do something to /srv/data")
    except AgentError:
        pass


def test_a_huge_integer_is_an_agent_error() -> None:
    with pytest.raises(AgentError):
        Agent._extract_json('{"a": ' + "1" * 6000 + "}" + "x" * 0 + "")


@pytest.mark.parametrize("command", [
    "lsns", "swapon --show", "lvs", "pvs", "vgs", "zpool status", "zpool list", "rfkill list",
    "ethtool eth0", "acpi -V", "upower -i /org/x", "gsettings get org.gnome.desktop.interface gtk-theme",
    "wpctl status", "arecord -l", "hdparm -I /dev/sda", "glxinfo", "fc-list", "xrandr", "xrandr --query",
    "systemd-resolve --status", "go version", "java -version", "gcc --version",
    "python3 -m pip list", "sudo fdisk -l", "sudo parted -l", "fdisk -l /dev/sda", "parted /dev/sda print",
])
def test_routine_diagnostics_the_review_found_wrongly_asking_now_run(command: str) -> None:
    verdict = SafetyClassifier().classify(command)
    assert not verdict.needs_confirmation, (command, verdict.level, verdict.reasons)


@pytest.mark.parametrize("command", ["fdisk /dev/sda", "parted /dev/sda mklabel gpt", "parted /dev/sda rm 1",
                                     "sfdisk /dev/sda < layout", "swapon /dev/sda2", "zpool destroy tank",
                                     "ethtool -E eth0 magic 1", "hdparm --security-erase x /dev/sda",
                                     "xrandr --output HDMI-1 --off", "gsettings set a b c"])
def test_their_writing_siblings_still_ask(command: str) -> None:
    assert SafetyClassifier().classify(command).level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL), command
