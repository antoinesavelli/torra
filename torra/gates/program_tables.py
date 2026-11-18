"""The tables `known_programs.trust()` looks programs up in: subcommand sets and per-program checks.

Depends on: torra.gates.program_checks and torra.gates.system_checks (the checks),
torra.gates.subcommand_specs (the options around a subcommand), torra.gates.program_basics
(trust levels). Data only, no logic.
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
from .options import OptionSpec, allowing
from .program_basics import BOUNDED_WRITE, READ_ONLY
from .program_checks import _git, _only_version, _operands, _python, _short
from .program_options import _awk, _curl, _sed, _tar
from .subcommand_specs import (
    AMIXER,
    APT,
    APT_GET,
    BLUETOOTHCTL,
    BORG,
    FLATPAK,
    GO,
    GSETTINGS,
    HOSTNAMECTL,
    LEAD_ANY,
    LEAD_FLAGS,
    LEAD_NONE,
    LOCALECTL,
    LOGINCTL,
    NETWORKCTL,
    NFT,
    OLLAMA,
    PIP,
    RESOLVECTL,
    RFKILL,
    SNAP,
    SYSTEMCTL,
    SYSTEMD_ANALYZE,
    TIMEDATECTL,
    TMUX,
    UFW,
    WPCTL,
    ZFS,
    ZPOOL,
)
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


#: Read-only subcommand names, bounded-write subcommand names, the program's options, and how it
#: finds its subcommand (`LEAD_*`).
SubcommandRow = tuple[frozenset[str], frozenset[str], OptionSpec, str]


def _sub(read: str, spec: OptionSpec, write: str = "", lead: str = LEAD_ANY) -> SubcommandRow:
    """Subcommand tables: read-only names, bounded-write names, the program's `OptionSpec`.
    A lone `-` in `read` means the program with no subcommand at all is read-only too."""
    return frozenset(read.split()), frozenset(write.split()), spec, lead


SUBCOMMANDS: dict[str, SubcommandRow] = {
    "systemctl": _sub(
        "- status is-active is-enabled is-failed show list-units list-unit-files list-timers "
        "list-sockets list-dependencies list-jobs list-machines cat get-default help",
        SYSTEMCTL,
        "reload reload-or-restart try-restart reset-failed daemon-reexec",
    ),
    "apt": _sub("list search show policy depends rdepends showpkg madison changelog -", APT),
    "apt-get": _sub("-", APT_GET),
    "snap": _sub(
        "list info find version services changes connections interfaces -", SNAP, lead=LEAD_FLAGS
    ),
    "flatpak": _sub("list info search remote-ls -", FLATPAK, lead=LEAD_FLAGS),
    "pip": _sub("list show freeze check inspect -", PIP, "install"),
    "pip3": _sub("list show freeze check inspect -", PIP, "install"),
    "loginctl": _sub(
        "- list-sessions list-users list-seats show-session show-user show-seat "
        "session-status user-status seat-status status",
        LOGINCTL,
    ),
    "timedatectl": _sub("- status show list-timezones show-timesync timesync-status", TIMEDATECTL),
    "localectl": _sub("- status list-locales list-keymaps", LOCALECTL),
    "hostnamectl": _sub("- status", HOSTNAMECTL),
    "networkctl": _sub("- list status lldp", NETWORKCTL),
    "resolvectl": _sub("- status query statistics", RESOLVECTL),
    "ufw": _sub("status show version", UFW, lead=LEAD_FLAGS),
    "nft": _sub("list", NFT),
    "zpool": _sub("status list get history iostat version", ZPOOL, lead=LEAD_FLAGS),
    "zfs": _sub("list get", ZFS, lead=LEAD_FLAGS),
    "rfkill": _sub("list", RFKILL),
    "go": _sub("version", GO, lead=LEAD_FLAGS),
    "wpctl": _sub("status inspect get-volume", WPCTL),
    "gsettings": _sub(
        "get list-keys list-schemas list-recursively range describe list-children writable",
        GSETTINGS,
    ),
    "ollama": _sub("- list ps show help", OLLAMA, lead=LEAD_NONE),
    "borg": _sub("list info version", BORG),
    "amixer": _sub("- get sget scontents controls scontrols info", AMIXER),
    "bluetoothctl": _sub("show devices info paired-devices list version", BLUETOOTHCTL),
    "systemd-analyze": _sub(
        "- blame critical-chain time verify security calendar timespan "
        "cat-config unit-paths plot dot",
        SYSTEMD_ANALYZE,
    ),
    "tmux": _sub(
        "ls list-sessions list-windows list-panes list-clients list-commands "
        "show-options show-environment show-messages has-session capture-pane "
        "display-message",
        TMUX,
        lead=LEAD_FLAGS,
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
