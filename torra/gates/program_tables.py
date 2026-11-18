"""The tables `known_programs.trust()` looks programs up in: subcommand sets and per-program checks.

Depends on: torra.gates.program_checks and torra.gates.system_checks (the checks),
torra.gates.program_basics (trust levels). Data only, no logic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .option_specs import (
    DATE,
    DMESG,
    DMIDECODE,
    ETHTOOL,
    FILE,
    HASH,
    HDPARM,
    HOSTNAME,
    JOURNALCTL,
    MOKUTIL,
    RG,
    SDIFF,
    SENSORS,
    SMARTCTL,
    SORT,
    SS,
    SSH_KEYGEN,
    SWAPON,
    SYSTEMD_RESOLVE,
    TREE,
    XRANDR,
    date_prints,
    ethtool_reads,
    lists_swap,
    no_operands,
    nvidia_smi,
    prints_fingerprint,
    smartctl_logs,
    tree_no_dash_operand,
)
from .options import allowing
from .program_basics import BOUNDED_WRITE, READ_ONLY
from .program_checks import _git, _only_version, _operands, _python, _short
from .program_options import _awk, _curl, _sed, _tar
from .system_checks import (
    _command,
    _crontab,
    _disk_listing,
    _dpkg,
    _ip,
    _iptables,
    _nmcli,
    _pactl,
    _printenv,
    _rsync_checked,
    _ruff,
    _su,
    _sysctl,
    _torra,
)

_FIND_WRITES = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)


def _sub(read: str, write: str = "", value_flags: str = "") -> tuple[frozenset[str], ...]:
    """Subcommand tables: read-only names, bounded-write names, flags that take a value.
    A lone `-` in `read` means the program with no subcommand at all is read-only too."""
    return frozenset(read.split()), frozenset(write.split()), frozenset(value_flags.split())


SUBCOMMANDS: dict[str, tuple[frozenset[str], ...]] = {
    "systemctl": _sub(
        "- status is-active is-enabled is-failed show list-units list-unit-files list-timers "
        "list-sockets list-dependencies list-jobs list-machines cat get-default help",
        "reload reload-or-restart try-restart reset-failed daemon-reexec",
        "-H -M -t --type -p --property -n --lines -o --output -s --signal -a --state",
    ),
    "apt": _sub("list search show policy depends rdepends showpkg madison changelog -"),
    "apt-get": _sub("-"),
    "snap": _sub("list info find version services changes connections interfaces -"),
    "flatpak": _sub("list info search remote-ls -"),
    "pip": _sub("list show freeze check inspect -", "install"),
    "pip3": _sub("list show freeze check inspect -", "install"),
    "loginctl": _sub(
        "- list-sessions list-users list-seats show-session show-user show-seat "
        "session-status user-status seat-status status"
    ),
    "timedatectl": _sub("- status show list-timezones show-timesync timesync-status"),
    "localectl": _sub("- status list-locales list-keymaps"),
    "hostnamectl": _sub("- status"),
    "networkctl": _sub("- list status lldp"),
    "resolvectl": _sub("- status query statistics"),
    "ufw": _sub("status show version"),
    "nft": _sub("list"),
    "zpool": _sub("status list get history iostat version"),
    "zfs": _sub("list get"),
    "rfkill": _sub("list"),
    "go": _sub("version"),
    "wpctl": _sub("status inspect get-volume"),
    "gsettings": _sub(
        "get list-keys list-schemas list-recursively range describe list-children writable"
    ),
    "ollama": _sub("- list ps show help"),
    "borg": _sub("list info version"),
    "amixer": _sub("- get sget scontents controls scontrols info"),
    "bluetoothctl": _sub("show devices info paired-devices list version"),
    "systemd-analyze": _sub(
        "- blame critical-chain time verify security calendar timespan "
        "cat-config unit-paths plot dot"
    ),
    "tmux": _sub(
        "ls list-sessions list-windows list-panes list-clients list-commands "
        "show-options show-environment show-messages has-session capture-pane "
        "display-message"
    ),
}

PREDICATES: dict[str, Callable[[Sequence[str]], str | None]] = {
    "sort": allowing(SORT),
    "date": allowing(DATE, date_prints),
    "dmesg": allowing(DMESG),
    "journalctl": allowing(JOURNALCTL),
    "dmidecode": allowing(DMIDECODE),
    "ss": allowing(SS),
    "nvidia-smi": nvidia_smi,
    "smartctl": allowing(SMARTCTL, smartctl_logs),
    "kill": lambda argv: (
        READ_ONLY if {"-l", "-L", "--list", "--table"} & set(argv[1:]) else BOUNDED_WRITE
    ),
    "uniq": lambda argv: READ_ONLY if len(_operands(argv)) <= 1 else None,
    "find": lambda argv: None if _FIND_WRITES & set(argv[1:]) else READ_ONLY,
    "dd": lambda argv: BOUNDED_WRITE if any(a.startswith("of=") for a in argv[1:]) else READ_ONLY,
    "mount": lambda argv: None if _operands(argv) else READ_ONLY,
    "ip": lambda argv: _ip(argv),
    "sed": _sed,
    "awk": _awk,
    "gawk": _awk,
    "mawk": _awk,
    "curl": _curl,
    "tar": _tar,
    "rsync": _rsync_checked,
    "git": _git,
    "su": _su,
    "tree": allowing(TREE, tree_no_dash_operand),
    "sdiff": allowing(SDIFF),
    "sensors": allowing(SENSORS),
    "file": allowing(FILE),
    "hash": allowing(HASH),  # `hash -p PROG ls` makes a later `ls` run PROG
    "rg": allowing(RG),
    "xxd": lambda argv: None if len(_operands(argv)) > 1 or "r" in _short(argv) else READ_ONLY,
    "crontab": _crontab,
    "dpkg": _dpkg,
    "iptables": _iptables,
    "ip6tables": _iptables,
    "sysctl": _sysctl,
    "command": _command,
    "python": _python,
    "perl": _only_version,
    "ruby": _only_version,
    "node": _only_version,
    "bash": _only_version,
    "sh": _only_version,
    "zsh": _only_version,
    "dash": _only_version,
    "ksh": _only_version,
    "pactl": _pactl,
    "nmcli": _nmcli,
    "aplay": lambda argv: READ_ONLY if {"-l", "-L"} & set(argv[1:]) else None,
    "ssh-keygen": allowing(SSH_KEYGEN, prints_fingerprint),
    "swapon": allowing(SWAPON, lists_swap),
    "ethtool": allowing(ETHTOOL, ethtool_reads),
    "hdparm": allowing(HDPARM),
    "xrandr": allowing(XRANDR),
    "systemd-resolve": allowing(SYSTEMD_RESOLVE),
    "arecord": lambda argv: READ_ONLY if {"-l", "-L"} & set(argv[1:]) else None,
    "fdisk": _disk_listing,
    "sfdisk": _disk_listing,
    "parted": _disk_listing,
    "sgdisk": _disk_listing,
    "printenv": _printenv,
    "hostname": allowing(HOSTNAME, no_operands),  # an operand, `-F` or `-b` sets the host name
    "ruff": _ruff,
    "torra": _torra,
    "gcc": _only_version,
    "g++": _only_version,
    "cc": _only_version,
    "clang": _only_version,
    "rustc": _only_version,
    "cargo": _only_version,
    "java": _only_version,
    "mokutil": allowing(MOKUTIL),
}
