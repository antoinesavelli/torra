"""The tables `SafetyClassifier` reads: risky programs, protected paths, wrappers, the regex backstop.

Depends on: torra.gates.safety_types (RiskLevel). Data only, no logic.
"""

from __future__ import annotations

import re

from .safety_types import RiskLevel

PROGRAM_RISK: dict[str, tuple[RiskLevel, str]] = {
    "mkfs": (RiskLevel.CRITICAL, "filesystem format"),
    "shred": (RiskLevel.CRITICAL, "destructive overwrite"),
    "wipefs": (RiskLevel.CRITICAL, "filesystem signature wipe"),
    "fdisk": (RiskLevel.CRITICAL, "partition table edit"),
    "parted": (RiskLevel.CRITICAL, "partition table edit"),
    "sgdisk": (RiskLevel.CRITICAL, "partition table edit"),
    "sfdisk": (RiskLevel.CRITICAL, "partition table edit"),
    "blkdiscard": (RiskLevel.CRITICAL, "block device discard"),
    "mkswap": (RiskLevel.CRITICAL, "swap format"),
    "cryptsetup": (RiskLevel.CRITICAL, "disk encryption change"),
    "lvremove": (RiskLevel.CRITICAL, "logical volume removal"),
    "vgremove": (RiskLevel.CRITICAL, "volume group removal"),
    "pvremove": (RiskLevel.CRITICAL, "physical volume removal"),
    "reboot": (RiskLevel.DANGEROUS, "host restart"),
    "poweroff": (RiskLevel.DANGEROUS, "host power off"),
    "halt": (RiskLevel.DANGEROUS, "host halt"),
    "shutdown": (RiskLevel.DANGEROUS, "host shutdown"),
    "pkill": (RiskLevel.DANGEROUS, "broad process kill"),
    "killall": (RiskLevel.DANGEROUS, "broad process kill"),
    "userdel": (RiskLevel.DANGEROUS, "account removal"),
    "truncate": (RiskLevel.DANGEROUS, "file truncation"),
    "chmod": (RiskLevel.CAUTION, "permission change"),
    "chown": (RiskLevel.CAUTION, "ownership change"),
    "mv": (RiskLevel.CAUTION, "file move"),
    "docker": (RiskLevel.CAUTION, "container operation"),
}

PROTECTED_PREFIXES: tuple[str, ...] = (
    "/",
    "/etc",
    "/boot",
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
    "/var",
    "/sys",
    "/proc",
    "/dev",
    "/home",
    "/root",
    "/srv",
    "/opt",
)

#: Writing here can never lose data or damage the system -- these are stream
#: sinks and documented no-ops, not storage. `/dev` is a protected prefix
#: because `> /dev/sda` should stay CRITICAL, but that same prefix rule was
#: catching the extremely common `> /dev/null` output-suppression idiom and
#: escalating harmless, often read-only commands (e.g. `systemctl status
#: nginx > /dev/null`) to CRITICAL. Exempting these specific targets keeps
#: the block-device case caught without crying wolf on the common one.
SAFE_REDIRECT_TARGETS: frozenset[str] = frozenset(
    {
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/stdout",
        "/dev/stderr",
    }
)
#: Characters that end a shell word when unquoted (besides blanks).
WORD_ENDS = ";|&()<>"
#: `$'...'`, ANSI-C quoting: bash decodes the backslash escapes inside, so `$'\x2fetc'` is /etc.
ANSI_C_QUOTED = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
#: A `$` that starts an expansion (a name, `${`, `$(`, a special parameter), a backtick, or the placeholder
#: `_blank_substitutions` leaves for a `$(...)`; a lone `$` is literal.
EXPANSION = re.compile(r"\$[\w{(@*#?$!-]|`|SUBST")

#: xlsx/docx/pptx/odt/ods/odp are all zip containers -- `PROTECTED_PREFIXES`
#: only recognizes absolute system locations, so a step with a declared
#: `cwd` inside the user's own document folder (an entirely ordinary way to
#: write a task) edits with a bare relative filename and never trips it.
#: Found testing §2.54 for real: `zip -r budget.xlsx ...` with no leading
#: `/` classified SAFE, the exact repack-in-place shape that section closed
#: for absolute paths. A small, closed set of long-stable file formats, not
#: an open-ended list of things to detect -- these extensions haven't
#: changed in over a decade.
DOCUMENT_CONTAINER_EXTENSIONS: tuple[str, ...] = (
    ".xlsx",
    ".xlsm",
    ".docx",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
)

#: `su`'s `-c` payload is executed by a shell exactly like `sh -c`'s is, even
#: though `su` isn't itself an interpreter -- listing it here is what makes
#: `resolve()` recurse into it as shell syntax instead of leaving it as one
#: opaque, unjudged argv. Found live: a bare `su` was in neither WRAPPERS nor
#: here, so `su -c 'rm -rf /x'` resolved to program "su", no PROGRAM_RISK
#: entry, no `_judge` rule, and silently fell through to SAFE unless the
#: regex BACKSTOP happened to still see the literal text.
INTERPRETERS: tuple[str, ...] = (
    "sh",
    "bash",
    "zsh",
    "ksh",
    "dash",
    "su",
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
)

WRAPPERS: tuple[str, ...] = (
    "sudo",
    "doas",
    "env",
    "nohup",
    "nice",
    "ionice",
    "setsid",
    "time",
    "timeout",
    "stdbuf",
    "xargs",
)

#: Shell keywords that sit in front of a command (`do rm -rf /x`, `then cat f`) and end a segment
#: that is only loop or branch structure (`for x in a b`, `done`, `fi`). Judging them as programs
#: hid the real command behind them. `case` is deliberately absent: its arms share one segment
#: with the pattern, so it stays an unrecognised program and asks.
CONTROL_PREFIXES: frozenset[str] = frozenset(
    {"do", "then", "else", "elif", "while", "until", "if", "!", "{"}
)
STRUCTURE_ONLY: frozenset[str] = frozenset({"for", "done", "fi", "in", "}", ")"})

#: Assignments whose value changes what a later command runs (a search path, a pager, a git helper).
RISKY_ENV: frozenset[str] = frozenset(
    """
PATH LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT BASH_ENV ENV PAGER MANPAGER EDITOR VISUAL BROWSER SHELL
IFS PYTHONPATH PYTHONSTARTUP PYTHONHOME PERL5OPT PERL5LIB RUBYOPT NODE_OPTIONS SSH_ASKPASS
LESSOPEN LESSCLOSE PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_CONFIG_FILE
""".split()
)
#: Programs that report only names, sizes and existence, never a file's contents: pointing them at a
#: credentials file reveals no secret.
METADATA_ONLY: frozenset[str] = frozenset(
    {
        "ls",
        "dir",
        "vdir",
        "stat",
        "test",
        "[",
        "[[",
        "du",
        "df",
        "find",
        "realpath",
        "readlink",
        "basename",
        "dirname",
    }
)
#: Disk tools that are read-only when they only list or print (see `known_programs._disk_listing`).
LISTING_TOOLS: frozenset[str] = frozenset({"fdisk", "sfdisk", "parted", "sgdisk"})
#: Units whose restart or stop can cut the session torra is running in.
CRITICAL_UNITS: tuple[str, ...] = (
    "display-manager",
    "gdm",
    "lightdm",
    "sddm",
    "networkmanager",
    "network-manager",
    "systemd-logind",
    "ssh",
    "sshd",
    "dbus",
)

#: Package managers whose `install` runs code from whatever source it is pointed at. A plain
#: install from the manager's default index is the ordinary workflow (CAUTION), but an install
#: aimed at a non-default source -- a custom/plain-HTTP index, a URL, a VCS ref, a sideloaded
#: local archive via `--dangerous` -- is supply-chain / RCE territory and must ask for a tick.
#: Found by the 2026-10-24 Phase-5 blind red-team: `pip install --index-url http://<host> pkg`
#: classified CAUTION and so ran without confirmation (the one miss in 30).
PACKAGE_INSTALLERS: frozenset[str] = frozenset(
    {
        "pip",
        "pip3",
        "npm",
        "pnpm",
        "yarn",
        "gem",
        "cargo",
        "apt",
        "apt-get",
        "dnf",
        "pacman",
        "snap",
    }
)
#: An install subcommand (`npm i`, `yarn add`, `npm ci` included), told apart from a package
#: literally named "install" only by position -- here we only need to know one is present.
INSTALL_VERBS: frozenset[str] = frozenset({"install", "i", "add", "ci"})
#: Long flags that aim an install at a source other than the manager's default index/registry:
#: pip's index/find-links, a custom registry (npm/yarn/cargo), gem's/cargo's source, cargo's
#: `--git`, snap's sideload `--dangerous`. Their mere presence means "not the default source".
UNTRUSTED_SOURCE_FLAGS: frozenset[str] = frozenset(
    {
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "--registry",
        "--source",
        "--git",
        "--index",
        "--clear-sources",
        "--dangerous",
    }
)
#: An operand (or an attached flag value) that names a remote or VCS source rather than a package
#: name: an http/ftp/git/file URL, or a pip-style VCS ref such as `git+https://...`.
#: pip's short options that take a value (`pip install --help`, pip 26). In a cluster the first of
#: them takes the rest of the word or the next one, so `-Uqf ./w` and `-ihttps://x` name a source.
PIP_VALUE_SHORT = "Ccefirt"
#: pip's source options in full; its optparse also takes any unambiguous prefix (`--find`, `--extra-ind`).
PIP_SOURCE_LONG: tuple[str, ...] = ("--index-url", "--extra-index-url", "--find-links")
UNTRUSTED_SOURCE_OPERAND = re.compile(
    r"(?i)^(?:(?:git|hg|svn|bzr)\+[a-z]+://|(?:https?|ftps?|git|file)://)"
)

HEREDOC = re.compile(
    r"(?<!<)<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(?:.*?\n)?[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL
)

# Live wire: HANDOFF.md §5.2
#: Flags that consume the *next* token as their own value, per wrapper --
#: used so _strip doesn't mistake a flag's value for the start of the
#: wrapped command. Found live: `timeout -s KILL 5 kill -9 1` left "KILL"
#: (the value of `-s`, not a flag) unconsumed; _strip's duration-skip only
#: matches digits, so "KILL" became the resolved argv's program name --
#: never "kill" -- and `_judge_kill`'s PID-1 check never ran. There is no
#: regex BACKSTOP entry for "kill of PID 1" (that check is argv-only) to
#: catch it as a fallback the way rm/mkfs/dd are, so the whole command
#: classified SAFE outright for an entirely ordinary shell idiom, not a
#: contrived adversarial string.
WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "xargs": frozenset(
        {
            "-n",
            "--max-args",
            "-I",
            "-P",
            "--max-procs",
            "-L",
            "--max-lines",
            "-s",
            "--max-chars",
            "-a",
            "--arg-file",
            "-d",
            "--delimiter",
            "-E",
        }
    ),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt"}),
}

#: Regex backstop. Parsing can fail on exotic syntax, and when it does the
#: answer should be the old answer rather than silence.
BACKSTOP: list[tuple[str, RiskLevel, str]] = [
    (
        r"\brm\s+(-[a-zA-Z]*\s+)*-{1,2}[a-zA-Z]*[rR][a-zA-Z]*f|\brm\s+-{1,2}f[a-zA-Z]*[rR]",
        RiskLevel.CRITICAL,
        "recursive force delete",
    ),
    (r"\bmkfs(\.\w+)?\b", RiskLevel.CRITICAL, "filesystem format"),
    (r"\bdd\b[^|;&]*\bof=/dev/", RiskLevel.CRITICAL, "raw write to a block device"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;\s*:", RiskLevel.CRITICAL, "fork bomb"),
    (r"\bshutil\.rmtree\b", RiskLevel.CRITICAL, "python recursive tree delete"),
    (r"\b(reboot|poweroff|halt|shutdown)\b", RiskLevel.DANGEROUS, "host power state"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", RiskLevel.DANGEROUS, "service interruption"),
    (r"(?<![-\w])rm\b", RiskLevel.DANGEROUS, "file deletion"),  # not `docker run --rm`
    (
        r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+--force|checkout\s+--\s)",
        RiskLevel.DANGEROUS,
        "unrecoverable git operation",
    ),
    (r"\|\s*(sudo\s+)?(ba|z|k)?sh\b", RiskLevel.DANGEROUS, "stdin piped into a shell"),
    (r"\bsudo\b", RiskLevel.CAUTION, "privilege escalation"),
    (r"\bsystemctl\s+(start|restart|enable)\b", RiskLevel.CAUTION, "service state change"),
]
#: Rows that match a bare word anywhere, so a file name or a search term (`cat /var/run/reboot-required`)
#: trips them; they apply only when resolve() could not parse the line, as the argv rules judge the real
#: `reboot`, `shutdown`, `systemctl poweroff` and `rm` once it can (GRADE_TO_A_PLAN.md P5.3, HANDOFF.md §2.90).
UNPARSED_ONLY: frozenset[str] = frozenset({"host power state", "file deletion"})
