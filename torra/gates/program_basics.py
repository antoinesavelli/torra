"""The trust levels `trust()` returns, the credential-path check and the subcommand finder.

Depends on: the standard library only (fnmatch, re). `known_programs` re-exports the trust levels
and `reads_credentials`.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence

READ_ONLY = "read_only"
BOUNDED_WRITE = "bounded_write"
PROTECTED_WRITE = "protected_write"

_CREDENTIAL_NAME = re.compile(
    r"^(?:\.env(?:\.(?!example$|sample$|template$|dist$)[\w.-]+)?|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|credentials|\.netrc|\.pgpass|\.git-credentials|\.npmrc|\.pypirc|g?shadow"
    r"|secrets?\.(?:ya?ml|json|toml)|ssh_host_\w+_key|.+\.(?:key|p12|pfx|kdbx)|secring\.gpg"
    r"|\.(?:bash|zsh|psql|mysql|python)_history|terraform\.tfstate(?:\.backup)?|wp-config\.php"
    r"|rclone\.conf|environ)$", re.IGNORECASE)
_SSH_PUBLIC = re.compile(r"^(?:.+\.pub|known_hosts.*|config|authorized_keys.*)$")
_CREDENTIAL_DIRS = ("/.aws/", "/.gnupg/", "/.kube/", "/.docker/", "/.azure/", "/.config/gcloud/",
                    "/.config/gh/", "/.config/rclone/", "/.password-store/")
#: Concrete names a glob like `.e*` or `id_*` could expand to.
_CREDENTIAL_LITERALS = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", ".netrc",
                        ".pgpass", ".git-credentials", ".npmrc", ".pypirc", "shadow", "gshadow",
                        ".bash_history", ".zsh_history", "terraform.tfstate", "wp-config.php",
                        "rclone.conf")


def _glob_could_match_a_credential(pattern: str) -> bool:
    """Whether a shell glob in a file name could expand to a credentials file. A pattern with fewer
    than two literal characters (`*`) is skipped, and `*`/`?` never match a leading dot."""
    if not any(c in pattern for c in "*?[") or len(re.sub(r"[*?\[\]]", "", pattern)) < 2:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) and (pattern.startswith(".") or name[0] != ".")
               for name in _CREDENTIAL_LITERALS)


def reads_credentials(path: str) -> bool:
    """True when `path` names a file that holds credentials: an env file, a private key, a cloud,
    git or shell-history file, a process environment, anything under ~/.aws, ~/.kube or ~/.gnupg.
    Reading one puts the secret in front of the model, and from there possibly a remote one."""
    if "=" in path and not path.startswith(("/", "~", ".")):
        path = path.split("=", 1)[1]  # dd if=.env
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if any(d in path for d in _CREDENTIAL_DIRS):
        return True
    if "/.ssh/" in path and not _SSH_PUBLIC.match(name):
        return True
    return bool(_CREDENTIAL_NAME.match(name)) or _glob_could_match_a_credential(name)


def _subcommand(argv: Sequence[str], value_flags: frozenset[str]) -> str:
    """The first operand that is not a flag's value; `-` when there is none."""
    skip = False
    for tok in argv[1:]:
        if skip:
            skip = False
        elif tok in value_flags:
            skip = True
        elif not tok.startswith("-"):
            return tok
    return "-"
