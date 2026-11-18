"""Per-program checks, part two: `curl`, `tar`, `sed` and `awk`/`gawk`/`mawk`, each an allow-list of options.

Each program has an `OptionSpec` of the options that cannot write a file, run a program or send local
data; any other option (an abbreviation of a long name included) is unknown and so asks. A value
that can still do harm (`curl -o`, `-w`, `-H`, `-X`; a sed or awk program text) is checked in a
module-level function.

Depends on: torra.gates.options (`OptionSpec`, `Parsed`, `parse`, `allowing`),
torra.gates.program_basics (trust levels), torra.gates.script_rules (sed and awk programs).
"""

from __future__ import annotations

from collections.abc import Sequence

from .options import OptionSpec, Parsed, allowing, parse
from .program_basics import BOUNDED_WRITE, READ_ONLY
from .script_rules import awk_program_is_safe, sed_script_is_simple


def _names(text: str) -> frozenset[str]:
    """`"a b"` as `{"--a", "--b"}`: long option names, written without their dashes."""
    return frozenset(f"--{name}" for name in text.split())


# curl 8.5.0, `curl --help all`. Left off, so they ask: everything that writes (-O -D -c --stderr
# --trace* --libcurl --etag-save --hsts --alt-svc --create-dirs --output-dir), uploads or sends
# data (-d -F -T --json --data* --form* -Q -t), reads a config or secret file (-K -b -n -E --key
# --netrc-file --variable --url-query), or chains requests (-: --next); and `--no-<name>`, of which
# only the ones real traffic uses are listed.
_CURL = OptionSpec(
    short="sSLIikvf46lN#p0123ZqVhjg",
    short_value="mAHruexXwoyYU",
    long=_names(
        "silent show-error location insecure verbose fail head include list-only compressed"
        " http1.0 http1.1 http2 http2-prior-knowledge http3 ipv4 ipv6 globoff no-buffer"
        " no-progress-meter no-keepalive no-sessionid progress-bar tcp-nodelay tcp-fastopen"
        " tlsv1 tlsv1.0 tlsv1.1 tlsv1.2 tlsv1.3 retry-connrefused retry-all-errors fail-early"
        " fail-with-body path-as-is raw tr-encoding ssl ssl-reqd ssl-no-revoke proxytunnel"
        " anyauth basic digest negotiate ntlm styled-output suppress-connect-headers disable"
        " disable-epsv disable-eprt ftp-pasv ignore-content-length junk-session-cookies"
        " parallel parallel-immediate version help ca-native cert-status doh-insecure"
        " proxy-insecure ssl-allow-beast disallow-username-in-url"
    ),
    long_value=_names(
        "max-time connect-timeout max-redirs max-filesize retry retry-delay retry-max-time"
        " limit-rate speed-limit speed-time user-agent header referer range user proxy"
        " proxy-user noproxy preproxy socks4 socks4a socks5 socks5-hostname proxy1.0 resolve"
        " connect-to interface local-port dns-servers doh-url unix-socket abstract-unix-socket"
        " cacert capath proto proto-redir proto-default tls-max ciphers tls13-ciphers curves"
        " expect100-timeout happy-eyeballs-timeout-ms keepalive-time oauth2-bearer url request"
        " write-out output parallel-max rate pinnedpubkey hostpubsha256 service-name delegation"
    ),
)


def _starts_with_at(values: list[str | None]) -> bool:
    """Whether any value names a file to read (`@file`)."""
    return any((v or "").startswith("@") for v in values)


def _curl_ok(parsed: Parsed) -> bool:
    """Every output goes to /dev/null, no format writes a file or reads one, no header file, only
    GET or HEAD."""
    formats = parsed.values("-w", "--write-out")
    methods = parsed.values("-X", "--request")
    return (
        all(v == "/dev/null" for v in parsed.values("-o", "--output"))
        and not any("%output{" in (v or "") for v in formats)
        and not _starts_with_at(formats + parsed.values("-H", "--header"))
        and all((v or "").upper() in ("GET", "HEAD") for v in methods)
    )


# GNU tar 1.35, `man tar`. Left off: modes other than -c -x -t (-A -r -u -d --delete), every option
# that runs a program (-I --to-command --checkpoint-action --info-script -F --new-volume-script
# --rsh-command --transform), writes a second file (--index-file -g --remove-files) or reaches
# outside the archive's own tree (-P --absolute-names).
_TAR = OptionSpec(
    short="ctxvzjJZapkmhOSWlBsiRno",
    short_value="fCTXbVNH",
    long=_names(
        "list create extract get verbose gzip gunzip ungzip bzip2 xz zstd lzma lzip lzop compress"
        " uncompress auto-compress no-auto-compress preserve-permissions same-permissions"
        " no-same-permissions keep-old-files skip-old-files keep-newer-files overwrite-dir"
        " no-overwrite-dir keep-directory-symlink no-same-owner same-owner numeric-owner touch"
        " dereference hard-dereference to-stdout sparse wildcards no-wildcards anchored"
        " no-anchored wildcards-match-slash no-wildcards-match-slash ignore-case no-ignore-case"
        " exclude-caches exclude-caches-all exclude-caches-under exclude-vcs exclude-vcs-ignores"
        " exclude-backups null no-null no-recursion recursion one-file-system ignore-zeros"
        " read-full-records same-order preserve-order show-transformed-names show-stored-names"
        " show-omitted-dirs ignore-failed-read verify check-links block-number full-time utc"
        " acls no-acls selinux no-selinux xattrs no-xattrs delay-directory-restore"
        " no-delay-directory-restore posix old-archive portability version help usage"
        " force-local verbatim-files-from no-verbatim-files-from unquote no-unquote seek no-seek"
        " clamp-mtime restrict"
    ),
    long_value=_names(
        "file directory files-from exclude exclude-from strip-components blocking-factor label"
        " newer newer-mtime after-date format mtime mode owner group sort quoting-style warning"
        " record-size"
    ),
    long_optional=_names("one-top-level occurrence checkpoint totals"),
)
_TAR_WRITES = frozenset({"-c", "-x", "--create", "--extract", "--get"})
_TAR_LISTS = frozenset({"-t", "--list"})


def _tar_argv(argv: Sequence[str]) -> list[str] | None:
    """`tar cfv a.tar x` as `tar -c -f -v a.tar x`: an old-style first word, one option per letter.

    Each letter that takes a value takes the next word, in order; a missing word becomes "" (tar
    itself refuses that). None when the first word holds anything but letters.
    """
    first = argv[1] if len(argv) > 1 else ""
    if first.startswith("-") or not first:
        return list(argv)
    if not first.isalpha():
        return None
    words = iter(argv[2:])
    out = [argv[0]]
    for letter in first:
        out.append(f"-{letter}")
        if letter in _TAR.short_value:
            out.append(next(words, ""))
    return [*out, *words]


def _tar(argv: Sequence[str]) -> str | None:
    """Create and extract are bounded writes, list reads; any other mode or option asks."""
    expanded = _tar_argv(argv)
    parsed = parse(expanded, _TAR) if expanded is not None else None
    if parsed is None:
        return None
    given = {name for name, _ in parsed.options}
    if given & _TAR_WRITES:
        return BOUNDED_WRITE
    return READ_ONLY if given & _TAR_LISTS else None


def _tar_paths(argv: Sequence[str]) -> list[str]:
    """Where tar writes: every `-C`/`--directory` and `--one-top-level=` value, and the archive
    (`-f`/`--file`) when creating. Empty when the options do not parse (the judge asks then)."""
    expanded = _tar_argv(argv)
    parsed = parse(expanded, _TAR) if expanded is not None else None
    if parsed is None:
        return []
    names = {"-C", "--directory", "--one-top-level"}
    if {"-c", "--create"} & {name for name, _ in parsed.options}:
        names |= {"-f", "--file"}
    return [v for v in parsed.values(*names) if v]


# GNU sed 4.9, `sed --help`. -f/--file stays off: a script file is unknown content.
_SED = OptionSpec(
    short="nErsuz",
    short_value="el",
    short_optional="i",
    long=_names(
        "quiet silent debug posix follow-symlinks sandbox separate unbuffered null-data"
        " regexp-extended"
    ),
    long_value=_names("expression line-length"),
    long_optional=_names("in-place"),
)


def _sed(argv: Sequence[str]) -> str | None:
    """Asks unless every script is one `sed_script_is_simple` accepts (each `-e` value, else the
    first operand); then in place is a bounded write and anything else read-only."""
    parsed = parse(argv, _SED)
    if parsed is None:
        return None
    scripts = [v for v in parsed.values("-e", "--expression") if v is not None]
    scripts = scripts or list(parsed.operands[:1])
    if not scripts or not all(sed_script_is_simple(s) for s in scripts):
        return None
    return BOUNDED_WRITE if parsed.values("-i", "--in-place") else READ_ONLY


# gawk 5.3 manual, "Options" (gawk is not installed), and `man mawk` (mawk 1.3.4, this box's awk).
# Left off: -f -i -l -E (they read program text from files) and -o -p -d -D (they write files), with
# their long forms. -W is allowed for a few exact words only: mawk's `-W exec FILE` runs FILE.
_AWK = OptionSpec(
    short="bcChMnNOPrsStV",
    short_value="FveW",
    short_optional="L",
    long=_names(
        "characters-as-bytes traditional copyright help bignum non-decimal-data use-lc-numeric"
        " optimize no-optimize posix re-interval sandbox lint-old version usage csv"
    ),
    long_value=_names("field-separator assign source"),
    long_optional=_names("lint"),
)


_AWK_WORDS = frozenset({"interactive", "version", "posix", "traditional", "usage", "help"})


def _awk_ok(parsed: Parsed) -> bool:
    """Every program text (`-e`/`--source` value or operand) passes `awk_program_is_safe`, and
    `-W` names one of a few harmless words (mawk would take an abbreviation; this does not)."""
    texts = [*parsed.values("-e", "--source"), *parsed.operands]
    return all(awk_program_is_safe(t) for t in texts if t is not None) and all(
        w in _AWK_WORDS for w in parsed.values("-W")
    )


_curl = allowing(_CURL, _curl_ok)
_awk = allowing(_AWK, _awk_ok)
