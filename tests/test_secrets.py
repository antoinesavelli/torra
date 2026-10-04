"""Secret handling: what redact_secrets scrubs and which credential files need a tick to read.

Depends on: torra.telemetry.records (redact_secrets), torra.gates (RiskLevel, SafetyClassifier).
"""

from __future__ import annotations

import pytest

from torra.gates import RiskLevel, SafetyClassifier
from torra.telemetry.records import redact_secrets

#: (text, the secret body that must not survive). Built at test time so no real key sits in the repo.
SECRETS = [
    ("sk-or-v1-" + "a1" * 20, "a1" * 20),
    ("sk-proj-" + "b2" * 15, "b2" * 15),
    ("sk-ant-api03-" + "c3" * 20, "c3" * 20),
    ("sk-" + "d4" * 15, "d4" * 15),
    ("Authorization: Bearer " + "e5" * 15, "e5" * 15),
    ("OPENROUTER_API_KEY=" + "f6" * 15, "f6" * 15),
    ("export DJANGO_SECRET_KEY='" + "g7" * 15 + "'", "g7" * 15),
    ("AUTH_TOKEN: " + "h8" * 15, "h8" * 15),
    ('{"api_key": "' + "i9" * 15 + '"}', "i9" * 15),
    ('{"client_secret":"' + "j0" * 15 + '", "ok": 1}', "j0" * 15),
    ('{"password": "' + "k1" * 8 + '"}', "k1" * 8),
    ("aws_secret_access_key = " + "l2" * 20, "l2" * 20),
    ("ghp_" + "m3" * 20, "m3" * 20),
    ("github_pat_" + "n4" * 20, "n4" * 20),
    ("xoxb-" + "123456789012-" + "o5" * 10, "o5" * 10),
    ("AIza" + "p6" * 20, "p6" * 20),
    ("hf_" + "q7" * 20, "q7" * 20),
    ("AKIA" + "R8" * 8, "R8" * 8),
    ("postgres://app:" + "s9" * 8 + "@db.internal:5432/app", "s9" * 8),
    ("https://user:" + "t0" * 8 + "@example.com/x", "t0" * 8),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "u1" * 30 + "\n-----END OPENSSH PRIVATE KEY-----", "u1" * 30),
    ("-----BEGIN RSA PRIVATE KEY-----\n" + "v2" * 30 + "\n(truncated", "v2" * 30),
]


@pytest.mark.parametrize(("text", "body"), SECRETS, ids=[t[:24] for t, _ in SECRETS])
def test_a_realistic_secret_does_not_survive_redaction(text: str, body: str) -> None:
    assert body not in redact_secrets(f"before {text}\nafter")


def test_ordinary_text_is_left_alone() -> None:
    plain = "step 3 failed: exit 2 while reading /var/log/syslog; token count 40; the key is in the lock"
    assert redact_secrets(plain) == plain


@pytest.mark.parametrize("command", [
    "cat .env", "cat /srv/app/.env", "grep KEY .env.local", "head -3 ~/.aws/credentials",
    "cat ~/.ssh/id_rsa", "cat ~/.ssh/id_ed25519", "less /etc/shadow", "cat ~/.netrc",
    "cat ~/.git-credentials", "tail ~/.gnupg/secring.gpg", "cat ~/.pgpass",
    "cat /etc/ssh/ssh_host_ed25519_key", "wc -c ~/.ssh/id_rsa", "sort ~/.config/app/secrets.yaml",
])
def test_reading_a_file_that_holds_credentials_needs_a_tick(command: str) -> None:
    verdict = SafetyClassifier().classify(command)
    assert verdict.needs_confirmation, (command, verdict.level)
    assert any("credentials" in r for r in verdict.reasons)


@pytest.mark.parametrize("command", [
    "cat .env.example", "cat .env.sample", "cat ~/.ssh/id_rsa.pub", "cat ~/.ssh/known_hosts",
    "cat ~/.ssh/config", "ssh-keygen -lf ~/.ssh/id_ed25519.pub", "ls ~/.ssh", "ls -la ~/.aws",
    "cat environment.md", "grep token notes.txt", "cat /etc/passwd",
    "ls -la ~/.ssh/", "ls ~/.aws", "ls -la /home/*/.netrc", "[ -f /srv/app/.env ]",
    "test -e ~/.ssh/id_rsa", "stat ~/.ssh/id_rsa", "du -h ~/.aws", "find ~/.ssh -name 'id_*'",
])
def test_neighbouring_files_that_hold_no_credentials_still_run(command: str) -> None:
    assert SafetyClassifier().classify(command).level is not RiskLevel.DANGEROUS, command
