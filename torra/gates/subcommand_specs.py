"""The options around a subcommand: one `OptionSpec` per `SUBCOMMANDS` program.

`known_programs._lookup` calls a program read-only only when its whole command line parses under the
program's spec AND the first operand is one of its read subcommands. So a spec lists the program's
global options plus the options its READ subcommands accept (one union per program), and only those
that neither write a file, run a command, change state, nor read a config that can do those. Anything
else asks. A write subcommand (`systemctl reload`, `pip install`) is found whatever the options.

Sources are this workstation's binaries (`--help`, `<sub> --help`, `man`), cited once per program.
`zpool`, `zfs`, `tmux` and `go` are not installed here, so theirs come from the programs' published
usage and stay small. Value-taking options are marked as such because a wrong mark shifts which word
is the subcommand. A union can mark one letter only one way: where two read subcommands disagree
(`tmux -c`, `-f`, `-S`, `-T`, `-C`, `-v`) the letter stays off.

Depends on: torra.gates.options (`OptionSpec`). Data only.
"""

from __future__ import annotations

from .options import OptionSpec

#: How a program finds its subcommand. `LEAD_ANY`: getopt, so a value option may come before it.
#: `LEAD_FLAGS`: the first word that is no `-option`, so only flag options may come before it (what
#: flatpak, snap, ollama, tmux, gsettings, ufw, zpool and zfs do). `LEAD_NONE`: ollama's cobra, where
#: a flag its root command does not know takes the next word, so no option may come before it.
LEAD_ANY = "any"
LEAD_FLAGS = "flags"
LEAD_NONE = "none"


def _n(words: str) -> frozenset[str]:
    """A set of long option names from a whitespace-separated string (names carry their `--`)."""
    return frozenset(words.split())


# systemctl (systemd 255) --help. Off: -H/--host (opens an ssh), -M/--machine, --root, --image,
# --image-policy (another root or a disk image), --now --force --runtime --global --no-block
# --no-reload --wait --signal --kill-whom --kill-value --what --job-mode --check-inhibitors -i
# --firmware-setup --boot-loader-* --when --drop-in --preset-mode --dry-run --no-wall (all steer a
# change: a write subcommand is found without them, a read subcommand never needs them).
SYSTEMCTL = OptionSpec(
    short="halrq",
    short_value="tpPno",
    long=_n(
        """--help --version --system --user --failed --all --full --recursive --reverse
        --with-dependencies --show-types --value --quiet --no-warn --no-pager --no-legend
        --no-ask-password --plain"""
    ),
    long_value=_n("--type --state --property --lines --output --timestamp --legend"),
)

# loginctl (systemd 255) --help. Off: -H, -M, -s/--signal, --kill-whom (signals), as for systemctl.
LOGINCTL = OptionSpec(
    short="hal",
    short_value="pPno",
    long=_n("--help --version --no-pager --no-legend --no-ask-password --all --value --full"),
    long_value=_n("--property --lines --output"),
)

# timedatectl (systemd 255) --help. Off: -H, -M, --adjust-system-clock (changes the RTC mode) and
# --monitor (does not return).
TIMEDATECTL = OptionSpec(
    short="ha",
    short_value="p",
    long=_n("--help --version --no-pager --no-ask-password --all --value"),
    long_value=_n("--property"),
)

# localectl (systemd 255) --help. Off: -H, -M, --no-convert (only set-keymap reads it).
LOCALECTL = OptionSpec(short="h", long=_n("--help --version --no-pager --no-ask-password"))

# hostnamectl (systemd 255) --help. `--static`/`--transient`/`--pretty` pick which name `status`
# prints; with a `set-hostname` they pick which one is set, but that verb is not a read subcommand.
# Off: -H, -M.
HOSTNAMECTL = OptionSpec(
    short="h",
    long=_n("--help --version --no-ask-password --transient --static --pretty"),
    long_value=_n("--json"),
)

# networkctl (systemd 255) --help. Off: -H does not exist here, --no-reload and --drop-in (edit).
NETWORKCTL = OptionSpec(
    short="hasl",
    short_value="n",
    long=_n("--help --version --no-pager --no-legend --all --stats --full"),
    long_value=_n("--lines --json"),
)

# resolvectl (systemd 255) --help. `-p` is --protocol here, not --property. Every query switch takes
# a BOOL or a name; `--raw` alone prints the answer as binary on stdout.
RESOLVECTL = OptionSpec(
    short="h46j",
    short_value="iptc",
    long=_n("--help --version --no-pager"),
    long_value=_n(
        """--interface --protocol --type --class --service-address --service-txt --cname --validate
        --synthesize --cache --stale-data --zone --trust-anchor --network --search --legend --json"""
    ),
    long_optional=_n("--raw"),
)

# systemd-analyze (systemd 255) --help. Off: -H, -M, --root, --image, --image-policy (another root,
# a disk image), --generators (runs unit generators), --profile, --security-policy, --threshold,
# --offline, --recursive-errors (read files or unclear argument kinds, no traffic).
SYSTEMD_ANALYZE = OptionSpec(
    short="hq",
    long=_n(
        """--help --version --quiet --system --user --global --no-pager --no-legend --order
        --require --table --tldr"""
    ),
    long_value=_n("--json --from-pattern --to-pattern --fuzz --iterations --base-time"),
    long_optional=_n("--man"),
)

# pip 24.0 --help, pip list/show/freeze/inspect --help. Off: --log, --log-file (append a file),
# --cache-dir (writes the cache where told), --python (runs that interpreter), --keyring-provider
# (runs `keyring`), --proxy --cert --client-cert --trusted-host (network and TLS trust), --index-url
# -i --extra-index-url --find-links (where to ask), -r/--requirement (reads a file),
# --exists-action --use-feature --use-deprecated. `-f` is `show --files` (a flag) but `list -f URL`
# (find-links, a value): marked a flag, so a URL after it reads as an operand; both only read.
PIP = OptionSpec(
    short="hvVqouelf",
    long=_n(
        """--help --debug --isolated --require-virtualenv --verbose --version --quiet --no-input
        --no-cache-dir --disable-pip-version-check --no-color --no-python-version-warning
        --outdated --uptodate --editable --local --user --pre --not-required --exclude-editable
        --include-editable --files --all"""
    ),
    long_value=_n("--path --format --exclude --timeout --retries"),
)

# apt 2.8.4 --help, apt list/search --help (probed with `apt list`). Off: -o/--option and
# -c/--config-file (`APT::Update::Pre-Invoke` and friends run commands), -t, -d, -s, -y. `-a` is `--all-versions` (`apt list -a bash`, `apt -a=amd64 list` is
# refused by apt); `-q` also takes `=N`.
APT = OptionSpec(
    short="hva",
    short_optional="q",
    long=_n(
        """--help --version --installed --upgradable --all-versions --manual-installed
        --names-only --full"""
    ),
    long_optional=_n("--quiet"),
)

# apt-get 2.8.4: only the bare program (help, version) counts as read-only, and `-c`, `-o` are off.
APT_GET = OptionSpec(short="hv", long=_n("--help --version"))

# snap 2.78 `snap help <sub>` (go-flags: an optional value only attaches with `=`). Off: nothing
# else is listed for the read subcommands.
SNAP = OptionSpec(
    short="hug",
    long=_n("--help --version --all --verbose --abs-time --narrow --private --user --global"),
    long_optional=_n("--color --unicode --section"),
)

# Flatpak 1.18.1 `flatpak <sub> --help` (GOption; it takes the first non-flag word as its command).
# Off: --print-updated-env, --print-system-only,
# --gl-drivers (not needed).
FLATPAK = OptionSpec(
    short="hudavjrcosmMel",
    long=_n(
        """--help --version --default-arch --supported-arches --installations --user --system
        --show-details --runtime --app --all --json --verbose --ostree-verbose --updates --cached
        --sideloaded --show-ref --show-commit --show-origin --show-size --show-metadata
        --show-runtime --show-sdk --show-permissions --show-extensions --show-location"""
    ),
    long_value=_n("--installation --arch --columns --app-runtime --file-access"),
)

# ufw 0.36 `ufw --help`: not getopt (`ufw [--dry-run] COMMAND`), so no option is listed and any
# option asks. `status`, `show` and `version` take words (`verbose`, `numbered`, `raw`).
UFW = OptionSpec()

# nft 1.0.9 `nft --help`. Off: -f/--file (applies a file), -D, -i, -I, -c, -o, -e, -d.
NFT = OptionSpec(
    short="hvVastSNunypTj",
    long=_n(
        """--help --version --handle --stateless --terse --service --reversedns --guid --numeric
        --numeric-priority --numeric-protocol --numeric-time --json"""
    ),
)

# zpool, OpenZFS (not installed here; usage of `zpool status|list|get|history|iostat`). Off: -c
# (runs a script from ZPOOL_SCRIPTS_PATH on status and iostat). Options only follow the subcommand.
ZPOOL = OptionSpec(
    short="DegiHLlnpPqrstvwxyj",
    short_value="oT",
    long=_n("--json-int --json-flat-vdevs"),
)

# zfs, OpenZFS (not installed here; usage of `zfs list|get`).
ZFS = OptionSpec(short="Hprj", short_value="dostS", long=_n("--json-int"))

# rfkill (util-linux 2.39.3) --help. `block`, `unblock` and `toggle` are not read subcommands.
RFKILL = OptionSpec(
    short="JnrhV",
    short_value="o",
    long=_n("--json --noheadings --output-all --raw --help --version"),
    long_value=_n("--output"),
)

# go: not getopt (Go's `flag` package: `-m` and `-v` after `go version`), no `-C dir` either, so no
# option is listed and any option asks. Not installed here.
GO = OptionSpec()

# wpctl (WirePlumber) `wpctl --help`, `wpctl status|inspect --help` (GOption).
WPCTL = OptionSpec(short="hknra", long=_n("--help --nick --name --referenced --associated"))

# gsettings (GLib) `gsettings --help`: `gsettings [--schemadir DIR] COMMAND`, not getopt, and a value
# option before the command is not allowed here (`LEAD_FLAGS`), so `--schemadir` asks.
GSETTINGS = OptionSpec()

# ollama 0.33.1 `ollama --help`, `ollama show --help` (cobra). Off: nothing else is listed for
# list, ps, show and help.
OLLAMA = OptionSpec(
    short="hv",
    long=_n(
        """--help --version --verbose --nowordwrap --license --modelfile --parameters --system
        --template"""
    ),
)

# borg 1.2.8 `borg --help`, `borg list --help`. Off: --rsh (the command used to connect),
# --remote-path (the borg executable run remotely), --debug-profile (writes a file), --umask,
# --remote-ratelimit --upload-ratelimit --remote-buffer --upload-buffer, --bypass-lock,
# --debug-topic, --exclude-from, --patterns-from (files).
BORG = OptionSpec(
    short="Vhvp",
    short_value="Pae",
    long=_n(
        """--version --help --critical --error --warning --info --verbose --debug --progress --iec
        --log-json --show-version --show-rc --consider-part-files --consider-checkpoints --short
        --json --json-lines"""
    ),
    long_value=_n(
        """--lock-wait --prefix --glob-archives --sort-by --first --last --exclude --pattern
        --format"""
    ),
)

# amixer (alsa-utils) --help. Off: -s/--stdin (executes commands from stdin, `sset` included),
# -D/--device (an ALSA device name resolved through asoundrc).
AMIXER = OptionSpec(
    short="hdnviqRM",
    short_value="ca",
    long=_n("--help --debug --nocheck --version --quiet --inactive --raw-volume --mapped-volume"),
    long_value=_n("--card --abstract"),
)

# bluetoothctl 5.72 --help. Off: --agent (registers an agent), --endpoints, --monitor and
# --init-script (runs commands from a file).
BLUETOOTHCTL = OptionSpec(long=_n("--help --version"), long_value=_n("--timeout"))

# tmux (not installed here; tmux(1) command list). Off: -c (runs a shell command), -f (config file,
# runs commands), -L and -S (pick the server socket), -C (control mode), -v (writes log files),
# -T, -D, and `-I` (sends stdin into a pane), `-b` (names a paste buffer to write), `-S`
# (capture-pane start line, the same letter as the socket): none of these is needed by `ls`.
TMUX = OptionSpec(short="aAeghHJpqsw", short_value="EFt")
