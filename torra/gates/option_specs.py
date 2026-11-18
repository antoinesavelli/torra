"""The read-only options of each listed program, as `OptionSpec`s, and the few extra checks they need.

A spec lists only options that neither write a file, run a command, change system, device or kernel
state, nor print a secret; whatever is not listed asks the operator (`options.allowing`). Each block
cites the source it was written from. `smartctl`, `tree` and `rg` are not installed on this
workstation and have no man page here, so theirs come from the programs' published option lists; a
value-taking option marked as a plain flag only turns its value into an operand, which is harmless.

Depends on: torra.gates.options (`OptionSpec`, `Parsed`). Data and small module-level checks only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .options import OptionSpec, Parsed, allowing

#: A redirect stays in argv as words, and shlex cannot tell a quoted `'2>'` from a real `2>`. So only
#: self-contained words whose target is harmless are skipped (`2>&1`, `>/dev/null`, `&>/dev/null`).
#: A bare operator word (`>`, `2>>`) is never skipped, nor the word after it: that word could be a
#: real operand (`swapon --raw '2>' /dev/sda2`). A real redirect is judged by the classifier itself.
_HARMLESS_REDIRECT = re.compile(r"(\d*>>?|&>>?|\d*<)/dev/null|\d*>&\d+")


def _operands(parsed: Parsed) -> list[str]:
    """The operands, without the redirect words that name only `/dev/null` or a descriptor."""
    return [word for word in parsed.operands if not _HARMLESS_REDIRECT.fullmatch(word)]


def _n(words: str) -> frozenset[str]:
    """A set of long option names from a whitespace-separated string (names carry their `--`)."""
    return frozenset(words.split())


# sort (GNU coreutils 9.4) --help. Off: -o/--output (writes), --compress-program (runs a program),
# -T/--temporary-directory (writes temporaries where told).
SORT = OptionSpec(
    short="bdfgiMhnRrVcCmsuz",
    short_value="ktS",
    long=_n(
        """--ignore-leading-blanks --dictionary-order --ignore-case --general-numeric-sort
        --ignore-nonprinting --month-sort --human-numeric-sort --numeric-sort --random-sort
        --reverse --version-sort --merge --stable --unique --zero-terminated --debug --help --version"""
    ),
    long_value=_n(
        """--random-source --sort --batch-size --files0-from --key --field-separator
        --buffer-size --parallel"""
    ),
    long_optional=_n("--check"),
)

# date (GNU coreutils 9.4) --help. Off: -s/--set (sets the clock). A bare operand that is not
# `+FORMAT` is `MMDDhhmm`, which also sets the clock: see `date_prints`.
DATE = OptionSpec(
    short="Ru",
    short_value="dfr",
    short_optional="I",
    long=_n("--rfc-email --utc --universal --resolution --debug --help --version"),
    long_value=_n("--date --file --reference --rfc-3339"),
    long_optional=_n("--iso-8601"),
)


def date_prints(parsed: Parsed) -> bool:
    """Every operand is a `+FORMAT`: a bare `MMDDhhmm` operand would set the clock."""
    return all(word.startswith("+") for word in _operands(parsed))


# dmesg (util-linux 2.39.3) --help. Off: -C/-c (clear the ring buffer), -D/-E and -n (console state).
DMESG = OptionSpec(
    short="HJkPprSuwWxdeTthV",
    short_value="Ffls",
    short_optional="L",
    long=_n(
        """--human --json --kernel --nopager --force-prefix --raw --noescape --syslog --userspace
        --follow --follow-new --decode --show-delta --reltime --ctime --notime --help --version"""
    ),
    long_value=_n("--file --facility --level --buffer-size --time-format --since --until"),
    long_optional=_n("--color"),
)

# journalctl (systemd 255) --help and man. Digits are `-b -1` and `-n -5` style offsets. Off:
# --vacuum-*, --rotate, --flush, --sync, --relinquish-var, --smart-relinquish-var, --setup-keys,
# --update-catalog, --force, --interval (write or change the journal), --cursor-file (updates its
# file), --image and --image-policy (attach a disk image).
JOURNALCTL = OptionSpec(
    short="0123456789hefalqmkrNx",
    short_value="MDSUcutpgoF",
    short_optional="nb",
    long=_n(
        """--system --user --merge --list-boots --disk-usage --verify --header --list-catalog
        --dump-catalog --utc --catalog --no-hostname --no-full --full --all --follow --no-tail
        --truncate-newline --quiet --no-pager --pager-end --reverse --show-cursor --dmesg --fields
        --help --version"""
    ),
    long_value=_n(
        """--machine --directory --file --root --namespace --since --until --cursor --after-cursor
        --unit --user-unit --identifier --priority --facility --grep --output --output-fields
        --field --verify-key"""
    ),
    long_optional=_n("--boot --lines --case-sensitive"),
)

# dmidecode 3.5 --help. Off: --dump-bin (writes a file).
DMIDECODE = OptionSpec(
    short="hquV",
    short_value="dstH",
    long=_n("--help --quiet --no-quirks --dump --no-sysfs --version"),
    long_value=_n("--dev-mem --string --type --handle --from-dump --oem-string"),
)

# ss (iproute2 6.1.0) --help. Off: -K/--kill (closes sockets), -D/--diag (writes a file).
SS = OptionSpec(
    short="hVnraloemTpisbEZz460tMSudwxHO",
    short_value="NfAF",
    long=_n(
        """--help --version --numeric --resolve --all --listening --options --extended --memory
        --processes --threads --info --tipcinfo --summary --tos --cgroup --bpf --events --context
        --contexts --ipv4 --ipv6 --packet --tcp --mptcp --sctp --udp --dccp --raw --unix --tipc
        --vsock --xdp --no-header --oneline --inet-sockopt"""
    ),
    long_value=_n("--net --family --query --socket --filter"),
)

# nvidia-smi 580.178.04 -h. Not getopt: it has single-dash words such as -pm and -lgc, and
# sub-commands (dmon, mig, drain, ...). So the short letters here are plain flags whose values
# become operands, and no listed letter can spell a modification option (-pm -e -p -c -r -pl -ac
# -lgc ... all contain a letter that is not listed). Off: -f/--filename and --debug (write a file),
# --set-hostname and every modification option, -lms and -col (asks).
NVIDIA_SMI = OptionSpec(
    short="hLBquxlid",
    long=_n(
        """--help --version --list-gpus --list-excluded-gpus --query --unit --xml-format --dtd
        --help-query-gpu --help-query-supported-clocks --help-query-compute-apps
        --help-query-accounted-apps --help-query-retired-pages --help-query-remapped-rows"""
    ),
    long_value=_n(
        """--id --display --loop --loop-ms --query-gpu --query-supported-clocks
        --query-compute-apps --query-accounted-apps --query-retired-pages --query-remapped-rows
        --format"""
    ),
)
_NVIDIA_VALUE = re.compile(r"[0-9A-Z][0-9A-Za-z_,:.\-]*")
# `nvidia-smi dmon|pmon` (nvidia-smi dmon -h): scrolling monitors. Off: -f/--filename (writes).
NVIDIA_MONITOR = OptionSpec(
    short="h",
    short_value="idcso",
    long=_n("--help"),
    long_value=_n("--id --delay --count --select --options"),
)


def nvidia_values_only(parsed: Parsed) -> bool:
    """Operands are only values of `-i`/`-d`/`-l` (an id, a UUID, DISPLAY names, seconds).

    A lowercase word could be a sub-command (`mig`, `drain`, `daemon`), which nvidia-smi runs
    with options of its own."""
    return all(_NVIDIA_VALUE.fullmatch(word) for word in _operands(parsed))


def no_operands(parsed: Parsed) -> bool:
    """No operand: `hostname NAME` sets the name."""
    return not _operands(parsed)


# `nvidia-smi topo -m` (nvidia-smi topo -h): the GPU interconnect matrix. Other topo options ask.
NVIDIA_TOPO = OptionSpec(short="mh", long=_n("--matrix --help"))
_NVIDIA_MAIN = allowing(NVIDIA_SMI, nvidia_values_only)
_NVIDIA_TOPO = allowing(NVIDIA_TOPO, no_operands)
_NVIDIA_MONITORS = allowing(NVIDIA_MONITOR, no_operands)


def nvidia_smi(argv: Sequence[str]) -> str | None:
    """`dmon`, `pmon` and `topo` have their own options; every other word goes to the main spec."""
    if argv[1:2] in (["dmon"], ["pmon"]):
        return _NVIDIA_MONITORS([argv[0], *argv[2:]])
    if argv[1:2] == ["topo"]:
        return _NVIDIA_TOPO([argv[0], *argv[2:]])
    return _NVIDIA_MAIN(argv)


# smartctl (smartmontools 7.x) usage; not installed here. Off: -s/--set, -S/--smart, -o/--offlineauto,
# -t/--test, -X/--abort, -C/--captive, -e (all change drive state or start a test). `-l` and
# `--log` take a log name; the set-like values (`scterc,N`, `sataphy,reset`) are refused by
# `smartctl_logs`.
SMARTCTL = OptionSpec(
    short="hViaxHcAj",
    short_value="dTbrnfvFPBqlg",
    long=_n(
        """--help --usage --version --copyright --license --info --all --xall --health
        --capabilities --attributes --scan --scan-open"""
    ),
    long_value=_n(
        """--device --tolerance --badsum --report --nocheck --format --vendorattribute
        --firmwarebug --presets --drivedb --quietmode --log --get"""
    ),
    long_optional=_n("--json --identify"),
)
_SMARTCTL_LOG = re.compile(
    r"(error|xerror|selftest|xselftest|selective|directory|background|scttempsts|scttemphist"
    r"|scterc|devstat|defects|ssd|sasphy|sataphy|tapealert|farm)(,[0-9]+)?"
)


def smartctl_logs(parsed: Parsed) -> bool:
    """Every `-l`/`--log` value names a log to print, not a setting (`scterc,70,70`); `--identify`
    takes only the display letters `w n v b`."""
    logs = parsed.values("-l", "--log")
    shows = parsed.values("--identify")
    return all(v is not None and _SMARTCTL_LOG.fullmatch(v) for v in logs) and all(
        v is None or re.fullmatch(r"[wnvb]*", v) for v in shows
    )


# tree 2.x usage; not installed here. tree is not getopt: each value letter (-L -P -I) takes the NEXT
# word, never an attached value, and the cluster goes on (`-Lo 1 out` writes `out`). So the value
# letters are plain flags here, their values become operands, and every writer letter is unlisted.
# Off: -o/--output and -R (write files), -H and -T (HTML), --hintro and --houtro. The long options
# that take a value are plain flags too, so `--name=value` asks and `--name value` passes.
TREE = OptionSpec(
    short="adlfxqNQpugshDFvtcUriASnCXJLPI",
    long=_n(
        """--ignore-case --matchdirs --metafirst --prune --info --noreport --si --du --inodes
        --device --dirsfirst --filesfirst --gitignore --fromfile --fromtabfile --fflinks
        --nolinks --charset --filelimit --timefmt --sort --gitfile --infofile --help --version"""
    ),
)


def tree_no_dash_operand(parsed: Parsed) -> bool:
    """No operand starts with `-`: tree's value options swallow even `--` as their value and go on
    parsing, so `tree -P -- -o out` writes `out` while getopt would read `-o` as an operand."""
    return not any(o.startswith("-") for o in parsed.operands)


# sdiff (GNU diffutils 3.10) --help. Off: -o/--output (writes), --diff-program (runs a program).
SDIFF = OptionSpec(
    short="iEZbWBalstdHv",
    short_value="Iw",
    long=_n(
        """--ignore-case --ignore-tab-expansion --ignore-trailing-space --ignore-space-change
        --ignore-all-space --ignore-blank-lines --strip-trailing-cr --text --left-column
        --suppress-common-lines --expand-tabs --minimal --speed-large-files --help --version"""
    ),
    long_value=_n("--ignore-matching-lines --width --tabsize"),
)

# sensors 3.6.0 --help. Off: -s/--set (writes chip settings).
SENSORS = OptionSpec(
    short="hfAujv",
    short_value="c",
    long=_n("--help --fahrenheit --no-adapter --bus-list --version"),
    long_value=_n("--config-file"),
)

# file 5.45 --help. Off: -C/--compile (writes a compiled magic file), -z/-Z (run decompressors). `-c` only prints the parsed
# magic file named by -m.
FILE = OptionSpec(
    short="vbikLhnNp0rsSd",
    short_value="mefFP",
    long=_n(
        """--help --version --brief --checking-printout --mime
        --apple --extension --mime-type --mime-encoding --keep-going --list --dereference
        --no-dereference --no-buffer --no-pad --print0 --preserve-date --raw --special-files
        --no-sandbox --debug"""
    ),
    long_value=_n("--magic-file --exclude --exclude-quiet --files-from --separator --parameter"),
)

# bash `help hash`. Off: -p (makes a later command of that name run another program).
HASH = OptionSpec(short="lrdt")

# ripgrep 14 option list; not installed here. Off: --pre and --pre-glob (run a command),
# --hostname-bin (runs a program), -z/--search-zip (spawns decompressors).
RG = OptionSpec(
    short="FHILNPSUVabchilnopqsuvwx0.",
    short_value="ABCEMTdefgjmrt",
    long=_n(
        """--no-ignore --no-ignore-vcs --no-ignore-dot --no-ignore-global --no-ignore-parent
        --no-ignore-exclude --no-ignore-files --hidden --files --files-with-matches
        --files-without-match --count --count-matches --line-number --no-line-number
        --only-matching --ignore-case --case-sensitive --smart-case --word-regexp --line-regexp
        --fixed-strings --invert-match --multiline --multiline-dotall --pcre2 --follow --text
        --binary --unrestricted --heading --no-heading --with-filename --no-filename --column
        --vimgrep --json --pretty --quiet --no-messages --no-config --stats --trim --version --help
        --null --sort-files --one-file-system --crlf --no-unicode --no-mmap --mmap --passthru
        --auto-hybrid-regex --no-require-git --require-git --debug --trace --byte-offset
        --include-zero --type-list --no-encoding --null-data --line-buffered --block-buffered"""
    ),
    long_value=_n(
        """--after-context --before-context --context --encoding --max-columns --type-not
        --regexp --file --glob --iglob --threads --max-count --replace --type --max-depth
        --max-filesize --ignore-file --type-add --path-separator --sort --sortr --engine
        --colors --color --context-separator --field-context-separator --field-match-separator
        --dfa-size-limit --regex-size-limit"""
    ),
)

# ethtool 6.7 --help. One mode option, a device, then keyword words. Read modes only. Off: -s -A
# -C -G -K -E -r -p -t -N -U -X -f -W -L -Q, every --set-*/--change/--pause/--coalesce/--features
# /--offload/--reset/--cable-test*/--monitor/--per-queue, and --debug. -d/-w write a file when the
# keywords say `file F`/`data F`: see `ethtool_reads`.
ETHTOOL = OptionSpec(
    short="acgkidenuSTxPlmwIh",
    long=_n(
        """--show-pause --show-coalesce --show-ring --show-features --show-offload --driver
        --register-dump --eeprom-dump --statistics --phy-statistics --show-nfc --show-ntuple
        --show-time-stamping --show-rxfh-indir --show-rxfh --show-permaddr --get-dump
        --show-channels --show-priv-flags --dump-module-eeprom --module-info --show-eee
        --get-phy-tunable --get-tunable --show-fec --show-tunnels --show-module --get-plca-cfg
        --get-plca-status --show-mm --show-pse --help --version --json --include-statistics
        --all-groups --groups"""
    ),
    long_value=_n("--src"),
)
_ETHTOOL_FLAGS = frozenset({"--json", "-I", "--include-statistics", "-h", "--help", "--version"})
_ETHTOOL_WRITING_KEYWORDS = frozenset({"file", "data"})


def ethtool_reads(parsed: Parsed) -> bool:
    """No `file F`/`data F` keyword, and no keyword words at all without a mode option.

    `ethtool eth0 speed 10` (no mode) is the legacy spelling of `-s`; `-d eth0 file F` and
    `-w eth0 data F` write F."""
    words = _operands(parsed)
    if _ETHTOOL_WRITING_KEYWORDS & set(words):
        return False
    modes = [name for name, _ in parsed.options if name not in _ETHTOOL_FLAGS]
    return bool(modes) or len(words) <= 1


# ssh-keygen (OpenSSH) usage and man: `-l` prints a fingerprint. Off: every other mode.
SSH_KEYGEN = OptionSpec(short="lvB", short_value="fE")


def prints_fingerprint(parsed: Parsed) -> bool:
    """`-l` is present: without it, `-f` is the output file of key generation."""
    return bool(parsed.values("-l"))


# swapon (util-linux) --help. Off: everything that enables swap or sets its options.
SWAPON = OptionSpec(
    short="s",
    long=_n("--summary --noheadings --raw --bytes"),
    long_optional=_n("--show"),
)


def lists_swap(parsed: Parsed) -> bool:
    """At least one listing flag and no device operand (a device operand enables swap)."""
    return bool(parsed.options) and not _operands(parsed)


# hdparm (v9.65) --help. Off: every setter; a letter that takes a value is not listed.
HDPARM = OptionSpec(short="IitTv")

# xrandr --help. Off: --output and every mode, position, rotation and property setter.
XRANDR = OptionSpec(
    short="qv",
    long=_n(
        """--query --listmonitors --listactivemonitors --listproviders --prop --properties
        --verbose --current --version --help"""
    ),
)

# systemd-resolve / resolvectl: the two status reads.
SYSTEMD_RESOLVE = OptionSpec(long=_n("--status --statistics"))

# hostname (net-tools) --help. Off: -b and -F (set the name); an operand sets it too.
HOSTNAME = OptionSpec(
    short="adfAiIsyhV",
    long=_n(
        """--alias --domain --fqdn --long --all-fqdns --ip-address --all-ip-addresses --short --yp
        --nis --help --version"""
    ),
)


# mokutil --help (not installed here; the names are the ones the old table held). Off: every
# enrol, delete, import, reset and password option.
MOKUTIL = OptionSpec(
    long=_n(
        """--sb-state --list-enrolled --list-new --list-delete --db --dbx --kek --pk --mokx --help
        --version"""
    )
)

# ruff 0.16.3 check/format --help. `ruff check` writes fixes when the project config sets
# `fix = true`, so it needs `--no-fix`; plain `ruff format` rewrites files, so it needs `--check`
# or `--diff`. Off: --fix, --watch, --add-noqa, --output-file, --cache-dir, --config, --extension.
RUFF_CHECK = OptionSpec(
    short="qsnv",
    long=_n(
        """--no-fix --statistics --exit-zero --no-cache --quiet --verbose --silent --isolated
        --preview"""
    ),
    long_value=_n("--output-format --select --ignore --extend-select --target-version"),
)
RUFF_FORMAT = OptionSpec(
    short="qsnv",
    long=_n(
        """--check --diff --no-cache --quiet --verbose --silent --isolated --preview
        --exit-non-zero-on-format"""
    ),
    long_value=_n("--line-length --target-version"),
)


def ruff_no_fix(parsed: Parsed) -> bool:
    """`--no-fix` is given (an option, not the value of another)."""
    return bool(parsed.values("--no-fix"))


def ruff_checks_only(parsed: Parsed) -> bool:
    """`--check` or `--diff` is given."""
    return bool(parsed.values("--check", "--diff"))
