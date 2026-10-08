"""
The five guardrails, reorganised by subsystem.

Every assertion here was earned by a defect found in six phases of validation.
The comments name which one, because a test whose reason is forgotten is a test
someone eventually deletes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from torra.core import Step, TaskSpec
from torra.gates import (
    Clarifier,
    DecisionLog,
    ModelRouter,
    Precondition,
    RiskLevel,
    SafetyClassifier,
    Snapshot,
    SnapshotKind,
    StateVerifier,
    UndoAction,
    UndoGenerator,
    VerifiedRollback,
    model_available,
)

# --------------------------------------------------------------------------- #
# SAFETY_FIRST
# --------------------------------------------------------------------------- #


class TestSafety:
    """Regex on raw text missed five of twelve evasions. This resolves first."""

    EVASIONS = [
        ("rm -fr /var/lib/x", "flag order swapped"),
        ("rm  -r  -f  /var/lib/x", "flags split"),
        ("/bin/rm -rf /var/lib/x", "absolute path"),
        ("R=rm; $R -rf /var/lib/x", "variable indirection"),
        ("sudo -- sh -c 'rm -rf /var/lib/x'", "sh -c wrapper"),
        ("sh -c \"sh -c 'rm -rf /srv'\"", "nested sh -c"),
        ("timeout 30 rm -rf /srv/data", "timeout wrapper"),
        ("nohup sudo mkfs.ext4 /dev/nvme0n1p3 &", "nohup and background"),
        ("env FOO=1 sudo rm -rf /opt/app", "env wrapper"),
        ("nice -n 5 dd if=/dev/zero of=/dev/sda", "nice wrapper"),
        ("find /var -delete", "find with -delete"),
        ("python3 -c \"import shutil;shutil.rmtree('/var/lib/x')\"", "python rmtree"),
        ("git checkout -- .", "discards uncommitted work"),
        ("> /etc/fstab", "truncate by redirect"),
        ("SYSTEMCTL=1 systemctl stop ollama", "env prefix"),
        ("chmod -R 777 /etc", "recursive chmod on /etc"),
        ("tee /etc/sudoers < evil", "tee into sudoers"),
        ("kill -9 1", "kill of PID 1"),
        ("apt-get purge --yes ollama", "package purge"),
        ("xargs rm -rf < targets.txt", "xargs indirection"),
        ("$(echo cm0gLXJmIC8=|base64 -d)", "base64 via command substitution"),
        ("eval \"$(echo cm0gLXJmIC8=|base64 -d)\"", "base64 via eval + substitution"),
        ("`echo cm0gLXJmIC8=|base64 -d`", "base64 via backtick substitution"),
        ("rm${IFS}-rf${IFS}/srv/data", "IFS-obfuscated rm -rf"),
        ("su -c 'rm -rf /var/lib/x' postgres", "su -c wrapper"),
        ("perl -e 'unlink(\"/etc/passwd\")'", "perl bareword unlink, no leading dot"),
        ("grep x /etc/fstab 2>/dev/null > /etc/fstab", "a real redirect after a /dev/null one"),
        ("echo ok > /dev/null; echo '' > /etc/fstab", "second statement truncates a system file"),
        ("tee /tmp/../etc/fstab", "dot-dot path that lands in /etc"),
        ("rsync -a ./payload antoine@executor:/etc/", "rsync to another machine"),
        ("rsync -a ./payload rsync://executor/etc/", "rsync daemon url"),
    ]

    ROUTINE = [
        "ls -la /etc", "cat /proc/meminfo", "df -h", "free -h", "uptime",
        "systemctl status ollama", "journalctl -u ollama -n 50",
        "git pull --ff-only", "git log --oneline -20", "git status",
        "docker ps", "docker logs api --tail 100", "python3 -m pytest tests/",
        "grep -r TODO src/", "echo hello", "ss -tlnp", "ps aux", "nvidia-smi",
        "lsblk", "curl -s http://localhost:11434/api/tags",
        "systemctl status nginx > /dev/null", "grep foo bar.txt 2>/dev/null",
        "echo \"today is $(date)\"", "tar czf backup-$(date +%Y%m%d).tar.gz /home/user/docs",
        "VERSION=$(git rev-parse --short HEAD)", "echo \"free: $(df -h / | tail -1)\"",
        "su - postgres",
        "ls > /dev/null 2>&1", "echo hi > /dev/null 2> /dev/null",
        "rsync -a ./src/ ./backup/", "cat /etc/hosts > /dev/null",
    ]

    def test_every_evasion_is_caught(self) -> None:
        """Every case in EVASIONS: the original 20, four found by an independent security
        audit (command-substitution and IFS obfuscation), and two found via
        live probing outside the documented corpus (su -c wrapping a shell
        payload; Perl's bareword unlink, which the old regex's required
        leading dot never matched)."""
        safety = SafetyClassifier()
        missed = [f"{label}: {cmd!r}" for cmd, label in self.EVASIONS
                  if not safety.classify(cmd).needs_confirmation]
        assert not missed, missed

    def test_command_substitution_and_ifs_evasions_are_critical_not_just_gated(
            self) -> None:
        """`needs_confirmation` alone doesn't distinguish CRITICAL from
        DANGEROUS -- these specific bugs were found live because a real
        `rm -rf /` classified as one severity too low (or not gated at
        all), not because it slipped past the gate outright. Pin the
        actual level so a regression back to "gated, but wrong severity"
        would still fail this test even though it would still pass the
        looser needs_confirmation check above."""
        safety = SafetyClassifier()
        for cmd in ("$(echo cm0gLXJmIC8=|base64 -d)",
                    "sh -c \"$(echo cm0gLXJmIC8=|base64 -d)\"",
                    "`echo cm0gLXJmIC8=|base64 -d`",
                    "rm${IFS}-rf${IFS}/", "rm${IFS}-rf${IFS}/home"):
            verdict = safety.classify(cmd)
            assert verdict.level is RiskLevel.CRITICAL, (cmd, verdict.level, verdict.reasons)

    def test_su_c_wrapping_a_nested_interpreter_call_is_still_caught(self) -> None:
        """A second, structural benefit of adding `su` to INTERPRETERS,
        beyond the direct `su -c 'rm -rf ...'` case already in EVASIONS:
        `su -c "python3 -c 'import os; os.system(...)'" user` used to be
        invisible to both _judge (su was an opaque, unjudged argv) and the
        regex BACKSTOP (which never checked for os.system inside a nested
        payload like this). Recursing into su's -c payload as shell syntax
        means the nested `python3 -c ...` is itself resolved and judged."""
        verdict = SafetyClassifier().classify(
            "su -c \"python3 -c 'import os; os.system(\\\"rm -rf /var/lib/x\\\")'\" postgres")
        assert verdict.needs_confirmation, verdict.reasons

    def test_no_false_positives_on_routine_work(self) -> None:
        """A classifier that interrupts normal work trains people to ignore it."""
        safety = SafetyClassifier()
        blocked = [c for c in self.ROUTINE if safety.classify(c).needs_confirmation]
        assert not blocked, blocked
        assert safety.parse_failures == []

    def test_dev_null_redirect_is_not_an_overwrite(self) -> None:
        """`> /dev/null` is the single most common output-suppression idiom in
        shell scripting. It was being misclassified CRITICAL because /dev is a
        protected prefix -- correct for `> /dev/sda`, wrong for a no-op sink
        that can never lose data. Found via a real torra exploration run
        that hit `systemctl status nginx > /dev/null` and got CRITICAL."""
        safety = SafetyClassifier()
        for suppressed in ("ls -la > /dev/null", "echo hi 2>/dev/null",
                           "grep foo bar.txt > /dev/null 2>&1"):
            verdict = safety.classify(suppressed)
            assert verdict.level is RiskLevel.SAFE, (suppressed, verdict.reasons)

    def test_a_real_device_redirect_still_critical(self) -> None:
        """The /dev/null exemption must not widen to all of /dev -- writing to
        an actual block device is exactly what the protected-prefix check
        exists to catch."""
        verdict = SafetyClassifier().classify("dd if=/dev/zero of=/dev/sda bs=1M")
        assert verdict.level is RiskLevel.CRITICAL

    def test_an_append_redirect_is_worded_as_an_append_not_an_overwrite(self) -> None:
        """Found in a full-file audit: the classification was already
        correct (CRITICAL either way for a protected path), but the reason
        text always said "overwrites" even for `>>`, which appends rather
        than truncates -- simply the wrong word for what the command does."""
        safety = SafetyClassifier()
        appended = safety.classify("echo x >> /etc/hosts")
        overwritten = safety.classify("echo x > /etc/hosts")

        assert appended.level is RiskLevel.CRITICAL
        assert overwritten.level is RiskLevel.CRITICAL
        assert any("appends to" in r for r in appended.reasons)
        assert not any("overwrites" in r for r in appended.reasons)
        assert any("overwrites" in r for r in overwritten.reasons)

    def test_an_interpreter_overwriting_a_file_it_also_read_is_not_silently_safe(
            self) -> None:
        """Found via scripts/dev/model_bench/domain_exploration.py's xlsx-edit
        scenario (2026-08-28, devstral:24b): asked to edit an existing
        spreadsheet, the model proposed one `python3 -c` step that loads
        /home/user/Documents/budget.xlsx, mutates it in memory, and saves back
        to that same path -- no backup made, no rollback declared -- and it
        classified SAFE. `_judge`/BACKSTOP only recognize shell-idiom
        overwrites (a `>` redirect onto a protected prefix) and named
        destructive programs (rm, dd, mkfs); an interpreter payload that reads
        a path and writes that same path back out is invisible to both, so a
        human never sees this in a TUI checkbox before it clobbers the user's
        only copy of the file. `needs_confirmation` -- not just `level is not
        SAFE` -- is the real bar: CAUTION renders no checkbox either (see
        `Torra`/`tui.py`'s `sp.needs_confirmation` gate), so this must reach
        DANGEROUS or CRITICAL to actually stop and ask. Not yet fixed --
        `SafetyClassifier` has no such rule yet, so this currently fails."""
        safety = SafetyClassifier()
        verdict = safety.classify(
            "python3 -c \"import openpyxl; "
            "wb = openpyxl.load_workbook('/home/user/Documents/budget.xlsx'); "
            "wb.active.append(['Total', 100]); "
            "wb.save('/home/user/Documents/budget.xlsx')\"")
        assert verdict.needs_confirmation, verdict.reasons

    def test_reading_the_same_path_twice_is_not_an_overwrite(self) -> None:
        """Found testing §2.51 for real against actual xlsx files: the
        repeated-literal check counted occurrences only, with no notion of
        which calls read versus write, so two independent read-only
        `load_workbook()` calls on the same path -- comparing a file to
        itself, or validating it before processing it, both completely
        ordinary -- classified DANGEROUS with zero writes anywhere. Fixed by
        requiring at least two distinct call shapes touching the literal (a
        read-named call followed by a differently-named call is the actual
        load-then-save signature; the same call name repeated twice is not)
        plus explicit mode-awareness for the one call where read/write share
        a name: `open(path, 'r')` vs `open(path, 'w')`."""
        safety = SafetyClassifier()
        verdict = safety.classify(
            "python3 -c \"import openpyxl; "
            "wb1 = openpyxl.load_workbook('/home/user/Documents/budget.xlsx'); "
            "wb2 = openpyxl.load_workbook('/home/user/Documents/budget.xlsx'); "
            "print(wb1.active.max_row, wb2.active.max_row)\"")
        assert not verdict.needs_confirmation, verdict.reasons

    def test_open_builtin_read_then_write_same_path_is_still_caught(self) -> None:
        """The call-shape distinction above can't use "different call name"
        for `open()`, since Python's own read and write paths share one
        name -- only the mode argument tells them apart. Without special-
        casing this, fixing the false positive above would silently reopen
        the exact hole §2.51 closed for the single most common I/O
        primitive in the language."""
        safety = SafetyClassifier()
        verdict = safety.classify(
            "python3 -c \"data = open('/home/user/Documents/budget.csv', 'r').read(); "
            "open('/home/user/Documents/budget.csv', 'w').write(data + ',Total,450')\"")
        assert verdict.needs_confirmation, verdict.reasons

    def test_open_builtin_read_twice_same_path_is_not_flagged(self) -> None:
        """Negative case for the fix above: two read-mode `open()` calls on
        the same path (default mode, and explicit 'r') must not be treated
        as a write just because `open` was the repeated call name."""
        safety = SafetyClassifier()
        verdict = safety.classify(
            "python3 -c \"a = open('/home/user/Documents/budget.csv').read(); "
            "b = open('/home/user/Documents/budget.csv', 'r').read(); print(a == b)\"")
        assert not verdict.needs_confirmation, verdict.reasons

    def test_zip_repack_over_a_relative_document_path_is_still_dangerous(self) -> None:
        """Found testing §2.54 for real: `_judge_tee_cp_mv_zip` reused the
        same `protected` flag cp/mv already used, which only recognizes
        absolute paths under PROTECTED_PREFIXES (system locations). A step
        with a declared `cwd` inside the user's own document folder --a
        completely ordinary way to write a task-- edits with a bare relative
        filename, so `zip -r budget.xlsx ...` sailed through as SAFE with no
        leading `/` in sight, the exact xlsx-repack shape §2.54 was supposed
        to close. `cp`/`mv` onto the same relative name are equally
        unprotected today (pre-existing, not new); this closes it for the
        specific risk category this investigation is about -- overwriting
        something that looks like an existing office document -- via a
        small, closed extension set, not a blanket escalation of every
        relative-path cp/mv/zip."""
        safety = SafetyClassifier()
        verdict = safety.classify("zip -r budget.xlsx xl/worksheets/sheet1.xml")
        assert verdict.level is RiskLevel.DANGEROUS, verdict.reasons

    def test_zip_to_a_relative_non_document_path_is_not_escalated(self) -> None:
        """Negative case: an ordinary relative-path zip that isn't building
        something shaped like an office document must stay unescalated --
        this is deliberately narrow, not a blanket "relative path" rule."""
        verdict = SafetyClassifier().classify("zip -r backup.zip staging/")
        assert verdict.level is RiskLevel.CAUTION, verdict.reasons
        assert not any("office document" in r for r in verdict.reasons), verdict.reasons

    def test_a_python_comparison_operator_is_not_mistaken_for_a_redirect(self) -> None:
        """Found testing xlsx commands for real: `_check_redirect`'s final
        catch-all regex scans raw command text for a bare `>` with no
        awareness of quoting, so an entirely ordinary Python comparison
        inside a `-c` payload (`if wb.active.max_row > 2:`) read as an
        "output redirect that overwrites" and escalated to CAUTION. Neither
        §2.51 nor §2.54 touched this -- pre-existing, found while testing
        realistic xlsx-editing scripts, which lean on comparisons like this
        constantly."""
        verdict = SafetyClassifier().classify(
            "python3 -c \"import openpyxl; "
            "wb = openpyxl.load_workbook('/home/user/Documents/budget.xlsx'); "
            "print(wb.active.max_row > 2)\"")
        assert not any("redirect" in r for r in verdict.reasons), verdict.reasons

    def test_a_real_redirect_next_to_a_quoted_string_is_still_caught(self) -> None:
        """Negative case for the fix above: masking quoted spans must not
        blind the check to a genuine, unquoted redirect that merely
        happens to sit next to a quoted argument on the same line."""
        verdict = SafetyClassifier().classify("echo 'hello' > /etc/fstab")
        assert verdict.level is RiskLevel.CRITICAL, verdict.reasons

    def test_base64_payloads_are_decoded(self) -> None:
        """An obfuscated payload piped to a shell is still what it decodes to."""
        import base64
        blob = base64.b64encode(b"rm -rf /var/lib/postgresql").decode()
        verdict = SafetyClassifier().classify(f"echo '{blob}' | base64 -d | sh")
        assert verdict.level is RiskLevel.CRITICAL
        assert any("base64 payload" in r for r in verdict.reasons)

    def test_an_unquoted_base64_payload_is_decoded_too(self) -> None:
        """Found live: the blob-extraction regex only matched a quoted
        string, but base64's own alphabet has no whitespace in it, so
        `echo BLOB | base64 -d | sh` (no quotes needed or used) is at
        least as natural to write as the quoted form -- and evaded
        decoding entirely, silently downgrading a CRITICAL recursive
        delete to a generic DANGEROUS "piped into a shell" backstop
        finding instead of the real verdict."""
        import base64
        blob = base64.b64encode(b"rm -rf /home").decode()
        verdict = SafetyClassifier().classify(f"echo {blob} | base64 -d | sh")
        assert verdict.level is RiskLevel.CRITICAL
        assert any("base64 payload" in r for r in verdict.reasons)

    def test_quoted_semicolons_do_not_split_the_payload(self) -> None:
        """A naive split on `;` tears `python3 -c "a;b"` apart, and that is
        exactly how a destructive call escapes classification."""
        safety = SafetyClassifier()
        verdict = safety.classify(
            "python3 -c \"import shutil;shutil.rmtree('/var/lib/x')\"")
        assert verdict.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL)
        assert safety.parse_failures == []

    def test_blocked_means_blocked_until_approved(self) -> None:
        """The verdict carries approval state; nothing runs off `level` alone."""
        verdict = SafetyClassifier().classify("rm -rf /var/lib/x")
        assert verdict.blocked is True

    def test_every_way_to_kill_pid_1_is_caught(self) -> None:
        """Found in a full-file audit: only the literal `-9` flag was
        recognized. `kill -s KILL 1`, `kill -SIGKILL 1`, `kill -s 9 1`,
        and plain `kill 1` (default SIGTERM) all classified SAFE."""
        safety = SafetyClassifier()
        for cmd in ("kill -9 1", "kill -s KILL 1", "kill -SIGKILL 1",
                   "kill -s 9 1", "kill 1", "kill -9 1 12345"):
            assert safety.classify(cmd).level is RiskLevel.CRITICAL, cmd

    def test_kill_of_pid_1_is_caught_even_behind_timeouts_value_taking_flag(
            self) -> None:
        """Found live, the most severe gap this project's own audits have
        caught: `timeout -s KILL 5 kill -9 1` classified SAFE outright.
        `_strip` popped "timeout" as a wrapper, then treated "-s" as a
        bare boolean flag and stopped -- leaving "KILL" (the *value* of
        `-s`, not a flag itself) as the next token. The duration-skip
        check only matches digits, so "KILL" was never consumed either,
        and it became the resolved argv's program name. `kill` never ran
        through `_judge_kill` at all, and there is no regex BACKSTOP entry
        for "kill of PID 1" (that check is argv-only) to catch it as a
        fallback the way rm/mkfs/dd are. `timeout -s KILL 5 kill -9 1` is
        an entirely ordinary shell idiom, not a contrived adversarial
        string."""
        safety = SafetyClassifier()
        for cmd in ("timeout -s KILL 5 kill -9 1",
                   "timeout --signal=KILL 5 kill -9 1",
                   "timeout -k 10 5 kill -9 1"):
            assert safety.classify(cmd).level is RiskLevel.CRITICAL, cmd

    def test_kill_of_a_different_pid_is_not_mistaken_for_pid_1(self) -> None:
        """`-s`/`-n`/`--signal` consume the *next* token as their argument,
        not a PID -- `kill -s 1 12345` sends SIGHUP to PID 12345 and never
        touches PID 1."""
        verdict = SafetyClassifier().classify("kill -s 1 12345")
        assert verdict.level is RiskLevel.CAUTION, verdict.reasons  # a bounded write, never CRITICAL

    def test_kill_list_mode_is_not_mistaken_for_a_kill(self) -> None:
        """`kill -l [1]` prints a signal name; it kills nothing."""
        verdict = SafetyClassifier().classify("kill -l 1")
        assert verdict.level is RiskLevel.SAFE
        verdict.approved = True
        assert verdict.blocked is False


class TestSafetyCoverageEdges:
    """The routine (non-evasion) side of resolve()/classify(): the "nothing
    to flag here" return in each _judge_* helper, and the failure paths
    around tokenizing/base64/substitution parsing that TestSafety's evasion
    corpus never exercises because every one of those cases is deliberately
    well-formed enough to succeed."""

    def test_an_unclosed_quote_is_a_recorded_parse_failure_not_a_crash(self) -> None:
        """`shlex.split` raises on an unterminated quote; `_tokenize` falls
        back to a whitespace split and records why, and classify() surfaces
        that as a NOTE rather than letting the exception propagate."""
        safety = SafetyClassifier()
        verdict = safety.classify('echo "unterminated')
        assert safety.parse_failures
        assert any("parse issue" in r for r in verdict.reasons)

    def test_a_charset_match_that_is_not_valid_base64_is_a_recorded_failure(
            self) -> None:
        """9 characters from base64's alphabet is not a valid base64 length
        (not a multiple of 4) -- `b64decode(validate=True)` raises, and
        that has to be caught per-candidate, not let one bad match abort
        decoding of a real payload elsewhere in the same command."""
        safety = SafetyClassifier()
        safety.classify("echo ABCDEFGHI | base64 -d | sh")
        assert any("base64 decode failed" in f for f in safety.parse_failures)

    def test_an_unmatched_command_substitution_paren_does_not_crash(self) -> None:
        verdict = SafetyClassifier().classify("echo $(echo hi")
        assert verdict.level is RiskLevel.SAFE

    def test_an_unmatched_backtick_does_not_crash(self) -> None:
        verdict = SafetyClassifier().classify("echo `unterminated")
        assert verdict.level is RiskLevel.SAFE

    def test_max_unwrap_depth_stops_recursion_instead_of_looping_forever(
            self) -> None:
        safety = SafetyClassifier(max_depth=-1)
        resolved = safety.resolve("rm -rf /")
        assert resolved == []
        assert any("max unwrap depth" in f for f in safety.parse_failures)

    def test_a_shell_dash_c_with_no_payload_resolves_without_crashing(self) -> None:
        """`argv.index("-c") + 1` is out of range when `-c` is the last
        token -- an empty payload is falsy, so nothing recurses, but the
        IndexError itself must not propagate."""
        verdict = SafetyClassifier().classify("sh -c")
        assert verdict.level is RiskLevel.DANGEROUS  # a bare shell is not known to be read-only

    def test_dd_without_a_block_device_target_is_not_flagged(self) -> None:
        verdict = SafetyClassifier().classify("dd if=/dev/zero of=/tmp/out.img bs=1M count=1")
        assert verdict.level is RiskLevel.CAUTION, verdict.reasons  # a file write, not a device write

    def test_find_without_delete_or_exec_is_not_flagged(self) -> None:
        verdict = SafetyClassifier().classify('find /tmp -name "*.tmp" -mtime +7')
        assert verdict.level is RiskLevel.SAFE

    def test_a_package_manager_verb_outside_the_known_lists_is_not_flagged(
            self) -> None:
        """`list` is neither a removal nor an install/upgrade verb."""
        verdict = SafetyClassifier().classify("apt list --installed")
        assert verdict.level is RiskLevel.SAFE

    def test_a_package_install_verb_is_flagged_as_a_package_change(self) -> None:
        verdict = SafetyClassifier().classify("apt install python3-pytest")
        assert verdict.level is RiskLevel.CAUTION
        assert any("package change" in r for r in verdict.reasons)

    def test_chmod_on_an_unprotected_path_is_not_escalated(self) -> None:
        """chmod carries a baseline CAUTION from PROGRAM_RISK regardless;
        this only checks that `_judge_chmod_chown`'s own protected-path
        escalation stays silent for an ordinary target."""
        verdict = SafetyClassifier().classify("chmod 644 /tmp/file.txt")
        assert verdict.level is RiskLevel.CAUTION
        assert not any("system path" in r for r in verdict.reasons)

    def test_mv_to_an_unprotected_path_is_not_escalated(self) -> None:
        verdict = SafetyClassifier().classify("mv /tmp/a /tmp/b")
        assert verdict.level is RiskLevel.CAUTION
        assert not any("writes to a system path" in r for r in verdict.reasons)

    def test_zip_repacking_over_an_existing_home_directory_file_is_dangerous(
            self) -> None:
        """Sibling gap to §2.51: xlsx/odt are both zip containers, and the
        other natural way to edit one -- extract, edit the inner XML, repack
        with `zip` -- was invisible to `_judge` entirely. `zip` had no
        PROGRAM_RISK entry and wasn't in any dispatch rule, so overwriting an
        existing document this way classified SAFE even though `cp`/`mv` onto
        the same path already classify DANGEROUS via the same protected-
        prefix check (`test_mv_to_an_unprotected_path_is_not_escalated`
        above proves that check exists for `mv`). `devstral:24b` proposed
        exactly this repack approach, unprompted, for an odt-edit scenario in
        domain_exploration.py. Not yet fixed -- `zip` isn't dispatched
        anywhere yet, so this currently fails."""
        verdict = SafetyClassifier().classify(
            "zip -r /home/user/Documents/contract.odt content.xml -x content.xml")
        assert verdict.level is RiskLevel.DANGEROUS
        assert any("writes to a system path" in r for r in verdict.reasons)

    def test_zip_to_an_unprotected_path_is_not_escalated(self) -> None:
        verdict = SafetyClassifier().classify("zip -r /tmp/backup.zip /tmp/staging")
        assert not any("writes to a system path" in r for r in verdict.reasons)

    def test_explain_renders_the_full_trace_including_an_unwrap_origin(self) -> None:
        safety = SafetyClassifier()
        text = safety.explain('sh -c "rm -rf /var/lib/x"')
        assert "command: sh -c" in text
        assert "verdict: critical" in text
        assert "resolved into" in text
        assert "<- sh -c" in text
        assert "reasons:" in text


# --------------------------------------------------------------------------- #
# VERIFY_STATE
# --------------------------------------------------------------------------- #


class TestVerifier:
    """Checking once before execution is checking the wrong moment."""

    def test_a_flip_mid_run_is_caught(self) -> None:
        """A service up at planning time and dead at step two used to be ignored."""
        state = {"up": True}
        verifier = StateVerifier()
        verifier.add(Precondition("svc", lambda: state["up"], "must stay up",
                                  ttl_seconds=0))

        assert verifier.run().passed is True
        assert verifier.guard() is None
        state["up"] = False
        veto = verifier.guard()

        assert veto is not None and "svc" in veto
        assert verifier.flips and verifier.flips[-1][1] == "pass -> fail"

    def test_ttl_absorbs_probe_storms(self) -> None:
        """Re-running an expensive probe before 200 steps is its own problem."""
        clock = {"now": 0.0}
        calls = {"n": 0}

        def probe() -> bool:
            calls["n"] += 1
            return True

        verifier = StateVerifier(clock=lambda: clock["now"])
        verifier.add(Precondition("slow", probe, "", ttl_seconds=30.0))
        verifier.run()
        for _ in range(20):
            verifier.guard()

        assert calls["n"] == 1
        assert verifier.cache_hits == 20
        clock["now"] = 31.0
        verifier.guard()
        assert calls["n"] == 2

    def test_a_raising_probe_counts_as_failed(self) -> None:
        """Swallowing a probe exception would report a false green."""
        verifier = StateVerifier()
        verifier.add(Precondition(
            "boom", lambda: (_ for _ in ()).throw(OSError("no /proc")), ""))
        report = verifier.run()
        assert report.passed is False
        assert "OSError" in report.results[0].error
        assert [r.name for r in report.failures] == ["boom"]

    def test_non_blocking_failures_do_not_stop_a_task(self) -> None:
        """Not every check is worth refusing over."""
        verifier = StateVerifier()
        verifier.add(Precondition("nice-to-have", lambda: False, "", blocking=False))
        assert verifier.run().passed is True

    def test_stats_reports_a_real_cache_hit_ratio(self) -> None:
        verifier = StateVerifier()
        verifier.add(Precondition("p", lambda: True, "", ttl_seconds=60))
        verifier.run()
        verifier.guard()
        verifier.guard()

        stats = verifier.stats()

        assert stats["probe_runs"] == 1
        assert stats["cache_hits"] == 2
        assert stats["cache_hit_ratio"] == round(2 / 3, 4)


class TestStateVerifierRealProbes:
    """disk_free_gb/tcp_reachable/service_active's own error handling --
    had no dedicated coverage; only the scope-injected tests below
    exercise service_active at all."""

    def test_disk_free_gb_returns_a_real_positive_number(self) -> None:
        assert StateVerifier.disk_free_gb("/") > 0

    def test_disk_free_gb_returns_minus_one_on_a_bad_path(self) -> None:
        assert StateVerifier.disk_free_gb("/no/such/path/at/all") == -1.0

    def test_tcp_reachable_is_true_for_a_real_listening_port(self) -> None:
        import socket as socket_module

        with socket_module.socket(socket_module.AF_INET,
                                  socket_module.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            assert StateVerifier.tcp_reachable("127.0.0.1", port) is True

    def test_tcp_reachable_is_false_when_nothing_is_listening(self) -> None:
        import socket as socket_module

        with socket_module.socket(socket_module.AF_INET,
                                  socket_module.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        # probe closed above, freeing the port -- nothing listens on it now
        assert StateVerifier.tcp_reachable(
            "127.0.0.1", free_port, timeout=0.5) is False

    def test_service_active_returns_false_on_a_probe_exception(
            self, monkeypatch) -> None:
        def broken_run(cmd, **kwargs):
            raise OSError("systemctl not found")

        monkeypatch.setattr(subprocess, "run", broken_run)
        assert StateVerifier.service_active("anything") is False


class TestSystemctlScope:
    """Found in a full-file audit: every systemctl call in this codebase
    hardcoded --user, and ollama.service is a system unit on the
    project's own reference machine -- `systemctl --user is-active
    ollama` reports "inactive" for a service that's actually running,
    because --user can't tell "not loaded here" apart from "loaded and
    genuinely inactive". `systemctl_scope` distinguishes them via
    LoadState instead of guessing."""

    def test_a_user_loaded_unit_is_detected_as_user_scope(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            class Result:
                stdout = "LoadState=loaded\n"
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert StateVerifier.systemctl_scope("some.service") == "user"

    def test_a_system_only_unit_falls_back_to_system_scope(self, monkeypatch) -> None:
        """--user reports "not-found" for a unit it doesn't know about --
        exactly ollama.service's situation on the reference machine."""
        def fake_run(cmd, **kwargs):
            class Result:
                stdout = "LoadState=not-found\n"
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert StateVerifier.systemctl_scope("ollama") == "system"

    def test_a_probe_failure_falls_back_to_system_scope(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            raise OSError("systemctl not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert StateVerifier.systemctl_scope("anything") == "system"

    def test_service_active_autodetects_scope_by_default(self, monkeypatch) -> None:
        """The actual live bug: `--user is-active ollama` reports inactive
        for a running system service. Autodetection must issue the
        system-scope is-active call instead, and see the real state."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            class Result:
                stdout = ("LoadState=not-found\n" if "show" in cmd
                          else "active\n" if "--user" not in cmd else "inactive\n")
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert StateVerifier.service_active("ollama") is True
        assert ["--user"] not in [c for c in calls if "is-active" in c and "--user" in c]

    def test_service_active_still_honors_an_explicit_scope(self, monkeypatch) -> None:
        """Passing user_scope explicitly must skip autodetection entirely --
        no LoadState probe issued, just the requested scope."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            class Result:
                stdout = "active\n"
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert StateVerifier.service_active("x", user_scope=False) is True
        assert all("show" not in c for c in calls), "explicit scope still probed LoadState"
        assert all("--user" not in c for c in calls)


# --------------------------------------------------------------------------- #
# ROLLBACK_ON_FAIL
# --------------------------------------------------------------------------- #


def _verify(action: UndoAction) -> bool:
    """Narrows `UndoAction.verify`'s `Callable[[], bool] | None` for tests
    that know a verifier was supplied and just want to call it."""
    assert action.verify is not None
    return action.verify()


class TestUndoGeneration:
    """Undo actions used to be caller supplied. With a model writing the steps,
    'whose author forgot' becomes the default case."""

    def test_a_file_is_restored_byte_for_byte(self, tmp_path: Path) -> None:
        target = tmp_path / "app.conf"
        target.write_text("original\n", encoding="utf-8")
        os.chmod(target, 0o640)

        generator = UndoGenerator(shadow_dir=tmp_path / "shadow")
        action = generator.for_step(Step("edit", touches=[str(target)]))[0]

        target.write_text("CLOBBERED\n", encoding="utf-8")
        os.chmod(target, 0o777)
        action.undo()

        assert target.read_text(encoding="utf-8") == "original\n"
        assert target.stat().st_mode & 0o7777 == 0o640
        assert _verify(action) is True

    def test_a_created_file_is_deleted_not_restored(self, tmp_path: Path) -> None:
        target = tmp_path / "new.conf"
        action = UndoGenerator(shadow_dir=tmp_path / "s").for_step(
            Step("create", touches=[str(target)]))[0]
        assert action.snapshot is not None
        assert action.snapshot.kind is SnapshotKind.FILE_ABSENT

        target.write_text("made by the step", encoding="utf-8")
        action.undo()
        assert not target.exists()

    def test_mkdir_undo_refuses_to_delete_a_filled_directory(self,
                                                             tmp_path: Path) -> None:
        """Deleting a directory someone filled is a second disaster."""
        target = tmp_path / "newdir"
        action = UndoGenerator(shadow_dir=tmp_path / "s").for_step(
            Step("make", command=f"mkdir -p {target}"))[0]

        target.mkdir()
        (target / "someone_elses.txt").write_text("important", encoding="utf-8")
        action.undo()
        assert target.exists() and (target / "someone_elses.txt").exists()

        (target / "someone_elses.txt").unlink()
        action.undo()
        assert not target.exists()

    def test_mkdir_undo_verify_is_false_when_the_directory_is_left_behind(
            self, tmp_path: Path) -> None:
        """Found live: verify() read `not target.exists() or
        any(target.iterdir())` -- when undo() correctly declines to rmdir a
        directory someone filled, "still exists AND has content" made
        verify() return True, so VerifiedRollback reported "undone" for a
        directory (and its real content) that was never removed. Undone
        must mean the prior state -- absent -- was actually confirmed."""
        target = tmp_path / "newdir"
        action = UndoGenerator(shadow_dir=tmp_path / "s").for_step(
            Step("make", command=f"mkdir -p {target}"))[0]

        target.mkdir()
        (target / "someone_elses.txt").write_text("important", encoding="utf-8")
        action.undo()
        assert _verify(action) is False, \
            "a directory left behind with real content must not verify as undone"

        (target / "someone_elses.txt").unlink()
        action.undo()
        assert _verify(action) is True

    def test_service_undo_uses_the_same_scope_it_was_captured_under(
            self, tmp_path: Path, monkeypatch) -> None:
        """Found in a full-file audit: capture_service's generated undo
        hardcoded --user regardless of the unit's actual scope. A system
        unit's undo would run against the wrong systemd instance --
        no error, just silently restoring nothing."""
        calls: list[list[str]] = []

        def fake_service_probe(unit: str) -> str:
            return "active"

        def fake_scope_probe(unit: str) -> str:
            return "system"

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            class Result:
                returncode = 0
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        generator = UndoGenerator(shadow_dir=tmp_path / "s",
                                  service_probe=fake_service_probe,
                                  enabled_probe=lambda unit: "enabled",
                                  scope_probe=fake_scope_probe)
        action = generator.capture_service("myservice")
        action.undo()

        assert calls, "undo never called subprocess.run"
        assert "--user" not in calls[0]
        assert calls[0][:2] == ["systemctl", "start"]

    def test_the_units_a_systemctl_step_acts_on_come_from_its_arguments(self) -> None:
        """`enable --now nginx` used to yield the unit `--now`."""
        from torra.gates.service_state import service_units
        assert service_units("sudo systemctl enable --now nginx") == ["nginx"]
        assert service_units("systemctl --user restart a.service b@1.service") == [
            "a.service", "b@1.service"]
        assert service_units("systemctl status nginx") == []
        assert service_units("systemctl stop x && systemctl disable y") == ["x", "y"]

    def _unit(self, tmp_path: Path, monkeypatch, active: str, enabled: str):
        state = {"active": active, "enabled": enabled}
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            verb = cmd[1 if "--user" not in cmd else 2]
            state["enabled"] = {"enable": "enabled", "disable": "disabled", "mask": "masked",
                                "unmask": "disabled"}.get(verb, state["enabled"])
            state["active"] = {"start": "active", "stop": "inactive"}.get(verb, state["active"])

            class Result:
                returncode = 0
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        generator = UndoGenerator(shadow_dir=tmp_path / "s",
                                  service_probe=lambda unit: state["active"],
                                  enabled_probe=lambda unit: state["enabled"],
                                  scope_probe=lambda unit: "system")
        return generator, state, calls

    def test_undoing_a_disable_re_enables_the_unit_and_verifies_both_facts(
            self, tmp_path: Path, monkeypatch) -> None:
        generator, state, calls = self._unit(tmp_path, monkeypatch, "active", "enabled")
        action = generator.capture_service("nginx")
        state.update(active="inactive", enabled="disabled")  # the step: disable --now

        action.undo()

        assert [c[1] for c in calls] == ["enable", "start"]
        assert state == {"active": "active", "enabled": "enabled"}
        assert action.verify is not None and action.verify() is True

    def test_an_undo_that_restores_only_the_running_state_is_not_called_verified(
            self, tmp_path: Path, monkeypatch) -> None:
        generator, state, _ = self._unit(tmp_path, monkeypatch, "active", "enabled")
        action = generator.capture_service("nginx")
        state.update(active="active", enabled="disabled")  # running again, still disabled

        assert action.verify is not None and action.verify() is False

    def test_undoing_a_mask_unmasks_before_restoring(self, tmp_path: Path, monkeypatch) -> None:
        generator, state, calls = self._unit(tmp_path, monkeypatch, "inactive", "disabled")
        action = generator.capture_service("nginx")
        state.update(active="inactive", enabled="masked")

        action.undo()

        assert [c[1] for c in calls] == ["unmask", "disable", "stop"]

    def test_package_undo_is_unsafe_by_design(self) -> None:
        """Removing packages cascades through dependencies, so it asks first."""
        installed = {"curl", "git"}
        generator = UndoGenerator(package_probe=lambda: set(installed))
        action = generator.for_step(Step("i", command="sudo apt install -y nginx"))[0]
        assert action.safe is False
        installed.add("nginx")
        assert _verify(action) is False

    def test_a_non_debian_package_manager_gets_no_undo(self) -> None:
        """Found in a full-file audit: the pattern used to also match dnf
        and pacman, but the generated undo always ran `apt-get remove`
        regardless -- a working-looking UndoAction that would fail (or
        remove the wrong packages) the moment it actually ran. Naming the
        step uncovered is honest; a broken undo that looks safe is not."""
        generator = UndoGenerator(package_probe=lambda: {"curl"})
        actions = generator.for_step(Step("i", command="sudo dnf install -y nginx"))
        assert actions == []

    def test_it_invents_nothing_it_cannot_invert(self, tmp_path: Path) -> None:
        """dd/mkfs against a raw device, and a migration tool's own upgrade,
        are named as uncovered rather than faked -- none of these declare
        `touches`, and a raw block device has no prior on-disk state a
        userspace file snapshot could restore from regardless. See
        test_dd_against_a_regular_declared_file_is_already_covered below
        for the case this does NOT describe."""
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        steps = [
            Step("dd", command="dd if=/dev/zero of=/dev/sda"),
            Step("mkfs", command="mkfs.ext4 /dev/nvme0n1p3"),
            Step("migrate", command="alembic upgrade head"),
        ]
        assert sum(len(generator.for_step(s)) for s in steps) == 0
        coverage = generator.coverage(steps)
        assert coverage["coverage_ratio"] == 0.0
        assert coverage["uncovered"] == ["dd", "mkfs", "migrate"]

    def test_dd_against_a_regular_declared_file_is_already_covered(
            self, tmp_path: Path) -> None:
        """HANDOFF.md §6.3: dd/mkfs writing to a regular file, not a raw
        device, need nothing special -- `touches` is the only thing that
        was ever required, the exact same mechanism `cp`/`sed -i`/any other
        file-modifying command already gets. Runs the real command and the
        real undo, not just checks an action object got built."""
        target = tmp_path / "disk.img"
        target.write_bytes(b"ORIGINAL" * 100)
        generator = UndoGenerator(shadow_dir=tmp_path / "shadow")
        step = Step("wipe", command=f"dd if=/dev/zero of={target} bs=800 count=1",
                   touches=[str(target)])

        actions = generator.for_step(step)
        assert len(actions) == 1
        coverage = generator.coverage([step])
        assert coverage["coverage_ratio"] == 1.0
        assert coverage["uncovered"] == []

        subprocess.run(["bash", "-c", step.command], check=True)
        assert target.read_bytes() != b"ORIGINAL" * 100

        actions[0].undo()
        assert target.read_bytes() == b"ORIGINAL" * 100

    def test_an_oversized_file_is_refused_loudly(self, tmp_path: Path) -> None:
        """An undo that cannot restore the file would be a lie."""
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 5000)
        generator = UndoGenerator(shadow_dir=tmp_path / "s", max_shadow_bytes=100)
        assert generator.for_step(Step("b", touches=[str(big)])) == []
        assert any("over max_shadow_bytes" in s for s in generator.skipped)

    def test_cleanup_empties_the_shadow_dir_but_keeps_it_usable(
            self, tmp_path: Path) -> None:
        """Found in a full-file audit: cleanup() had zero callers anywhere,
        so shadow directories accumulated forever (14 found on one real
        machine). Wiring it up has to leave the generator usable for the
        rest of the session -- _gate() snapshots at propose time on every
        plan(), not just executed ones, using the same UndoGenerator."""
        target = tmp_path / "app.conf"
        target.write_text("original\n", encoding="utf-8")
        generator = UndoGenerator(shadow_dir=tmp_path / "shadow")
        generator.for_step(Step("edit", touches=[str(target)]))
        assert any(generator.shadow_dir.iterdir())

        generator.cleanup()

        assert generator.shadow_dir.exists()
        assert not any(generator.shadow_dir.iterdir())
        # and it still works for the next plan in the session
        second = generator.for_step(Step("edit2", touches=[str(target)]))
        assert second and second[0].snapshot is not None

    def test_a_step_whose_snapshot_was_skipped_is_reported_uncovered(
            self, tmp_path: Path) -> None:
        """`touches` alone used to count as covered, even when the file was too big to
        snapshot or was a directory, so the plan advertised an undo that did not exist."""
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 100)
        small = tmp_path / "small.txt"
        small.write_text("ok", encoding="utf-8")
        generator = UndoGenerator(shadow_dir=tmp_path / "s", max_shadow_bytes=10)
        steps = [Step("big", touches=[str(big)]), Step("dir", touches=[str(tmp_path)]),
                 Step("small", touches=[str(small)])]
        for step in steps:
            generator.for_step(step)

        report = generator.coverage(steps)

        assert report["covered"] == ["small"]
        assert report["uncovered"] == ["big", "dir"]

    def test_a_dead_old_sibling_shadow_dir_is_reaped(self, tmp_path: Path) -> None:
        """A directory from a run that failed (so cleanup() never ran) must
        not accumulate forever either -- reaped once it is both stale by
        age and its pid is dead, never while a run could still be using it."""
        import os
        import time

        dead_pid = 999999
        while UndoGenerator._pid_alive(dead_pid) and dead_pid > 2:
            dead_pid -= 1
        stale = tmp_path / f"torra-shadow-{dead_pid}"
        stale.mkdir()
        (stale / "leftover.txt").write_text("x", encoding="utf-8")
        old = time.time() - UndoGenerator.STALE_SHADOW_SECONDS - 3600
        os.utime(stale, (old, old))

        UndoGenerator(shadow_dir=tmp_path / f"torra-shadow-{os.getpid()}")

        assert not stale.exists()

    def test_a_fresh_sibling_shadow_dir_is_not_reaped(self, tmp_path: Path) -> None:
        """Even with a dead pid, a directory younger than the staleness
        window is left alone -- age alone isn't enough to prove the run
        it belonged to is really over and done needing its snapshots."""
        import os

        dead_pid = 999999
        while UndoGenerator._pid_alive(dead_pid) and dead_pid > 2:
            dead_pid -= 1
        fresh = tmp_path / f"torra-shadow-{dead_pid}"
        fresh.mkdir()

        UndoGenerator(shadow_dir=tmp_path / f"torra-shadow-{os.getpid()}")

        assert fresh.exists()

    def test_a_live_pids_shadow_dir_is_never_reaped(self, tmp_path: Path) -> None:
        """Old by age but the owning process is still running: never touch it."""
        import os
        import time

        live = tmp_path / f"torra-shadow-{os.getpid()}-other"
        live.mkdir()
        old = time.time() - UndoGenerator.STALE_SHADOW_SECONDS - 3600
        os.utime(live, (old, old))

        UndoGenerator(shadow_dir=tmp_path / f"torra-shadow-{os.getpid()}")

        assert live.exists()

    def _aged(self, path: Path, seconds: float) -> None:
        import os
        import time
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_a_uuid_named_dir_whose_owner_file_names_a_live_pid_is_never_reaped(
            self, tmp_path: Path) -> None:
        """A TUI tab's shadow dir is named by session uuid; `torra-shadow-3f2a...`
        was read as pid 3 by the old pattern, so an idle tab could lose snapshots."""
        import os
        live = tmp_path / "torra-shadow-3f2a9c0d11"
        live.mkdir()
        (tmp_path / "torra-shadow-3f2a9c0d11.owner").write_text(str(os.getpid()), encoding="utf-8")
        self._aged(live, UndoGenerator.STALE_SHADOW_SECONDS + 3600)

        UndoGenerator(shadow_dir=tmp_path / f"torra-shadow-{os.getpid()}")

        assert live.exists()

    def test_a_uuid_named_dir_whose_owner_is_dead_is_reaped(self, tmp_path: Path) -> None:
        import os
        dead_pid = 999999
        while UndoGenerator._pid_alive(dead_pid) and dead_pid > 2:
            dead_pid -= 1
        stale = tmp_path / "torra-shadow-9e8d7c6b5a"
        stale.mkdir()
        (tmp_path / "torra-shadow-9e8d7c6b5a.owner").write_text(str(dead_pid), encoding="utf-8")
        self._aged(stale, UndoGenerator.STALE_SHADOW_SECONDS + 3600)

        UndoGenerator(shadow_dir=tmp_path / f"torra-shadow-{os.getpid()}")

        assert not stale.exists()

    def test_an_ownerless_uuid_dir_is_left_a_week_then_reaped(self, tmp_path: Path) -> None:
        import os
        young = tmp_path / "torra-shadow-aaaa1111"
        old = tmp_path / "torra-shadow-bbbb2222"
        for d in (young, old):
            d.mkdir()
        self._aged(young, UndoGenerator.STALE_SHADOW_SECONDS + 3600)
        self._aged(old, UndoGenerator.LEGACY_SHADOW_SECONDS + 3600)

        UndoGenerator(shadow_dir=tmp_path / f"torra-shadow-{os.getpid()}")

        assert young.exists() and not old.exists()

    def test_every_shadow_dir_records_its_owner_even_after_cleanup(self, tmp_path: Path) -> None:
        import os
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        owner = tmp_path / "s.owner"
        assert owner.read_text(encoding="utf-8") == str(os.getpid())

        generator.cleanup()

        assert owner.read_text(encoding="utf-8") == str(os.getpid())
        assert not any((tmp_path / "s").iterdir())


class TestCaptureRemoteFile:
    """UndoGenerator.capture_remote_file(): the direct remote mirror of
    capture_file, over a fake executor_runner -- no test here touches a
    live network (HANDOFF.md §2.53)."""

    def test_none_when_no_executor_runner_is_configured(self, tmp_path: Path) -> None:
        generator = UndoGenerator(shadow_dir=tmp_path / "shadow")
        assert generator.capture_remote_file("/etc/nginx/nginx.conf") is None
        assert any("no executor configured" in s for s in generator.skipped)

    def test_present_case_snapshots_and_restores_byte_for_byte(
            self, tmp_path: Path) -> None:
        remote: dict[str, str] = {"/etc/nginx/nginx.conf": "original\n"}

        def fake_runner(command: str, cwd: str | None, timeout: float | None) -> str:
            if command.startswith("test -e"):
                return "yes" if "/etc/nginx/nginx.conf" in remote else "no"
            if command.startswith("base64 -w0"):
                import base64 as b64
                return b64.b64encode(remote["/etc/nginx/nginx.conf"].encode("utf-8")).decode()
            if "base64 -d" in command:
                import base64
                encoded = command.split("echo ", 1)[1].split(" |", 1)[0].strip("'")
                remote["/etc/nginx/nginx.conf"] = base64.b64decode(encoded).decode("utf-8")
                return ""
            raise AssertionError(f"unexpected command: {command}")

        generator = UndoGenerator(shadow_dir=tmp_path / "shadow", executor_runner=fake_runner)
        action = generator.capture_remote_file("/etc/nginx/nginx.conf", "push")
        assert action is not None
        assert action.snapshot is not None
        assert action.snapshot.kind is SnapshotKind.FILE_CONTENT

        remote["/etc/nginx/nginx.conf"] = "CLOBBERED\n"
        action.undo()
        assert remote["/etc/nginx/nginx.conf"] == "original\n"
        assert _verify(action) is True

    def test_restore_keeps_a_trailing_newline_and_bytes_that_are_not_utf8(
            self, tmp_path: Path) -> None:
        """The real runner strips its output, so a `cat` read dropped the trailing newline and
        the restore still verified, against content that was already wrong."""
        import base64
        path = "/etc/app/blob.bin"
        original = b"line one\nline two\n\xff\xfe\x00tail\n"
        remote = {path: original}

        def fake_runner(command: str, cwd: str | None, timeout: float | None) -> str:
            if command.startswith("test -e"):
                return "yes"
            if command.startswith("base64 -w0"):
                return base64.b64encode(remote[path]).decode().strip()
            if "base64 -d" in command:
                encoded = command.split("echo ", 1)[1].split(" |", 1)[0].strip("'")
                remote[path] = base64.b64decode(encoded)
                return ""
            raise AssertionError(f"unexpected command: {command}")

        generator = UndoGenerator(shadow_dir=tmp_path / "shadow", executor_runner=fake_runner)
        action = generator.capture_remote_file(path, "push")
        assert action is not None
        remote[path] = b"CLOBBERED"

        action.undo()

        assert remote[path] == original
        assert _verify(action) is True

    def test_absent_case_undo_deletes_what_the_push_created(self, tmp_path: Path) -> None:
        remote: dict[str, str] = {}

        def fake_runner(command: str, cwd: str | None, timeout: float | None) -> str:
            if command.startswith("test -e"):
                return "yes" if "/tmp/new.conf" in remote else "no"
            if command.startswith("rm -f"):
                remote.pop("/tmp/new.conf", None)
                return ""
            raise AssertionError(f"unexpected command: {command}")

        generator = UndoGenerator(shadow_dir=tmp_path / "shadow", executor_runner=fake_runner)
        action = generator.capture_remote_file("/tmp/new.conf", "push")
        assert action is not None
        assert action.snapshot is not None
        assert action.snapshot.kind is SnapshotKind.FILE_ABSENT

        remote["/tmp/new.conf"] = "made by the push"
        action.undo()
        assert "/tmp/new.conf" not in remote
        assert _verify(action) is True

    def test_none_when_the_runner_fails_outright_not_a_broken_undo(
            self, tmp_path: Path) -> None:
        """Never a fabricated or best-effort undo -- a push step this
        happens to is honestly 'uncovered', the same as dd/mkfs."""
        def always_fails(command: str, cwd: str | None, timeout: float | None) -> str:
            raise RuntimeError("host unreachable")

        generator = UndoGenerator(shadow_dir=tmp_path / "shadow", executor_runner=always_fails)
        assert generator.capture_remote_file("/etc/nginx/nginx.conf") is None
        assert any("existence check failed" in s for s in generator.skipped)

    def test_for_step_wires_remote_push_kind_to_capture_remote_file(
            self, tmp_path: Path) -> None:
        def fake_runner(command: str, cwd: str | None, timeout: float | None) -> str:
            if command.startswith("test -e"):
                return "no"
            if command.startswith("rm -f"):
                return ""
            raise AssertionError(f"unexpected command: {command}")

        generator = UndoGenerator(shadow_dir=tmp_path / "shadow", executor_runner=fake_runner)
        step = Step("push", kind="remote_push", remote_path="/etc/nginx/nginx.conf")
        actions = generator.for_step(step)
        assert len(actions) == 1
        assert actions[0].snapshot is not None
        assert actions[0].snapshot.target == "/etc/nginx/nginx.conf"

    def test_for_step_does_nothing_remote_for_an_ordinary_step(self, tmp_path: Path) -> None:
        def must_not_be_called(command: str, cwd: str | None, timeout: float | None) -> str:
            raise AssertionError("an ordinary step must never touch the executor runner")

        generator = UndoGenerator(shadow_dir=tmp_path / "shadow",
                                  executor_runner=must_not_be_called)
        actions = generator.for_step(Step("edit", command="echo hi"))
        assert actions == []


class TestUndoGeneratorRealProbesAndFailureEdges:
    """The rest of undo.py that TestUndoGeneration doesn't reach: the real
    (non-injected) systemctl/dpkg probes every UndoGenerator falls back to
    when no fake is passed in, and the failure edges (unreadable file,
    oversized shadow, reflink unsupported) that only fire when something
    actually goes wrong. undo.py was the second-worst-covered file in the
    tree (79%) with almost none of this exercised."""

    def test_the_real_systemctl_scope_probe_does_not_crash(self) -> None:
        """Exercises the real subprocess.run path (every other scope test
        injects scope_probe), against a unit this project's own docs
        already establish is a real system-scope service on this machine."""
        assert UndoGenerator._systemctl_scope("ollama.service") in ("user", "system")

    def test_the_real_systemctl_state_probe_does_not_crash(self, tmp_path: Path) -> None:
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        state = generator._systemctl_state("ollama.service")
        assert state and state != "unknown"

    def test_the_real_systemctl_probes_do_not_crash_on_a_nonexistent_unit(self) -> None:
        """A unit name guaranteed not to exist still gets a real answer
        from systemctl (typically "inactive" -- systemctl is-active does
        not distinguish "never existed" from "loaded and stopped", which
        is exactly the ambiguity _systemctl_scope's own docstring
        describes), not a crash."""
        assert UndoGenerator._systemctl_scope("definitely-not-a-real-unit.service") \
            in ("user", "system")
        generator = UndoGenerator(shadow_dir=Path(tempfile.mkdtemp()))
        state = generator._systemctl_state("definitely-not-a-real-unit.service")
        assert state, "must return a real status string, not raise"

    def test_the_real_systemctl_state_probe_degrades_to_unknown_on_a_real_failure(
            self, monkeypatch) -> None:
        """"unknown" is the fallback for when systemctl itself cannot be
        invoked at all (binary missing, timeout) -- not for an ordinary
        "unit doesn't exist" miss, which still returns a real status."""
        generator = UndoGenerator(shadow_dir=Path(tempfile.mkdtemp()))

        def broken_run(cmd, **kwargs):
            raise FileNotFoundError("systemctl not found")

        monkeypatch.setattr(subprocess, "run", broken_run)
        assert generator._systemctl_state("anything") == "unknown"

    def test_the_real_dpkg_probe_returns_a_real_package_set(self) -> None:
        packages = UndoGenerator._dpkg_set()
        assert len(packages) > 10, "a real Pop!_OS install has far more than 10 packages"
        assert all(isinstance(p, str) and p for p in packages)

    def test_digest_of_a_missing_file_is_none(self, tmp_path: Path) -> None:
        assert UndoGenerator.digest(tmp_path / "nope") is None

    def test_digest_of_an_unreadable_file_is_none(self, tmp_path: Path) -> None:
        target = tmp_path / "locked.bin"
        target.write_bytes(b"secret")
        target.chmod(0o000)
        try:
            result = UndoGenerator.digest(target)
        finally:
            target.chmod(0o644)
        assert result is None

    def test_reflink_is_not_probed_twice(self, tmp_path: Path) -> None:
        """The result is cached on first probe -- a second call must not
        shell out again."""
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        first = generator._probe_reflink()
        calls = []
        real_run = subprocess.run

        def spy(*args, **kwargs):
            calls.append(args)
            return real_run(*args, **kwargs)

        subprocess.run = spy
        try:
            second = generator._probe_reflink()
        finally:
            subprocess.run = real_run
        assert first == second
        assert calls == [], "a cached probe result must not shell out again"

    def test_reflink_probe_is_false_when_cp_is_unavailable(
            self, tmp_path: Path, monkeypatch) -> None:
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert generator._probe_reflink() is False

    def test_copy_falls_back_to_a_byte_copy_when_reflink_is_unsupported(
            self, tmp_path: Path) -> None:
        source = tmp_path / "src.txt"
        source.write_text("content", encoding="utf-8")
        dest = tmp_path / "dst.txt"
        generator = UndoGenerator(shadow_dir=tmp_path / "s", prefer_reflink=False)

        how = generator._copy(source, dest)

        assert how == "copy"
        assert generator.byte_copies == 1
        assert dest.read_text(encoding="utf-8") == "content"

    def test_copy_uses_reflink_when_the_probe_reports_it_works(
            self, tmp_path: Path, monkeypatch) -> None:
        """This filesystem (ext4, per HANDOFF §6.5) doesn't really support
        --reflink=always -- forcing the probe result is what exercises the
        success branch _copy would take on btrfs/xfs without needing one."""
        source = tmp_path / "src.txt"
        source.write_text("content", encoding="utf-8")
        dest = tmp_path / "dst.txt"
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        generator.reflink_supported = True

        def fake_run(cmd, **kwargs):
            assert cmd[:2] == ["cp", "--reflink=always"]
            dest.write_bytes(source.read_bytes())
            class Result:
                returncode = 0
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        how = generator._copy(source, dest)

        assert how == "reflink"
        assert generator.reflink_copies == 1

    def test_capture_file_records_an_error_when_the_shadow_copy_fails(
            self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "app.conf"
        target.write_text("x", encoding="utf-8")
        generator = UndoGenerator(shadow_dir=tmp_path / "s")

        def broken_copy(self, source, destination):
            raise OSError("simulated copy failure")

        monkeypatch.setattr(UndoGenerator, "_copy", broken_copy)
        assert generator.capture_file(target) is None
        assert any("shadow copy failed" in s for s in generator.skipped)

    def test_capture_mkdir_declines_when_the_directory_already_exists(
            self, tmp_path: Path) -> None:
        """Only worth an undo when the step is the one creating it -- a
        directory that already exists is not this step's to remove."""
        target = tmp_path / "already-there"
        target.mkdir()
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        assert generator.capture_mkdir(str(target)) is None

    def test_for_step_derives_a_service_undo_from_a_systemctl_command(
            self, tmp_path: Path) -> None:
        """Found gap: TestUndoGeneration's service test calls
        capture_service() directly -- nothing exercised SERVICE_PATTERN's
        match inside for_step() itself."""
        generator = UndoGenerator(shadow_dir=tmp_path / "s",
                                  service_probe=lambda unit: "active",
                                  scope_probe=lambda unit: "system")
        actions = generator.for_step(
            Step("restart", command="sudo systemctl restart nginx"))
        assert len(actions) == 1
        snapshot = actions[0].snapshot
        assert snapshot is not None
        assert snapshot.target == "nginx"

    def test_for_step_marks_a_package_removal_as_uncovered_not_inverted(
            self, tmp_path: Path) -> None:
        """apt install gets an undo (test_package_undo_is_unsafe_by_design);
        apt remove/purge cannot be safely auto-inverted (what was removed
        may have had its own config already gone) and must not pretend
        otherwise -- named uncovered instead."""
        generator = UndoGenerator(shadow_dir=tmp_path / "s",
                                  package_probe=lambda: {"curl"})
        actions = generator.for_step(
            Step("cleanup", command="sudo apt remove --yes nginx"))
        assert actions == []
        assert any("cannot be auto-inverted" in s for s in generator.skipped)

    def test_a_captured_packages_snapshot_serializes_its_payload_sorted(
            self) -> None:
        generator = UndoGenerator(package_probe=lambda: {"zeta", "alpha", "mid"})
        action = generator.capture_packages("nginx")
        assert action.snapshot is not None
        assert action.snapshot.to_dict()["payload"] == ["alpha", "mid", "zeta"]


class TestUndoGeneratorRemainingEdges:
    """What's left after TestUndoGeneratorRealProbesAndFailureEdges (91%):
    Snapshot.to_dict()'s own set-payload branch (capture_packages always
    hands it an already-sorted list, so nothing else reaches it directly),
    _pid_alive's PermissionError/OSError arms, a real permission failure
    while scanning for sibling shadow dirs, the "user"-scope return of
    _systemctl_scope (every other test's real unit resolves to "system"),
    the real dpkg/reflink probes' own except branches, _copy's reflink
    subprocess-raises fallback (distinct from the already-covered
    returncode-!=0 fallback), undo_packages' closure actually running
    (only ever built, never invoked, before this), and stats().

    Two branches stay uncovered, matching the same call made one round
    earlier for capture_file's stat-after-exists TOCTOU (see the "79% ->
    91%" commit): _reap_stale_shadows' `entry.stat()` at the tail of its
    loop can only fail if the entry vanishes between the `is_dir()` check
    a few lines above and the `stat()` call itself (pathlib's own is_dir()
    already swallows ENOENT/ELOOP, so a dangling symlink or a real
    permission wall doesn't reach this line) -- a genuine race, not
    reachable without monkeypatching Path itself, which the project
    already ruled out after it broke pytest's own internals once.
    """

    def test_snapshot_to_dict_sorts_a_set_payload(self) -> None:
        snap = Snapshot(SnapshotKind.PACKAGE_SET, "pkgs",
                        payload={"zeta", "alpha", "mid"})
        assert snap.to_dict()["payload"] == ["alpha", "mid", "zeta"]

    def test_pid_alive_treats_permission_denied_as_alive(self, monkeypatch) -> None:
        """A pid we're not allowed to signal still definitely exists."""
        def fake_kill(pid: int, sig: int) -> None:
            raise PermissionError("not ours to signal")

        monkeypatch.setattr(os, "kill", fake_kill)
        assert UndoGenerator._pid_alive(1) is True

    def test_pid_alive_treats_other_os_errors_as_dead(self, monkeypatch) -> None:
        def fake_kill(pid: int, sig: int) -> None:
            raise OSError("some other kill failure")

        monkeypatch.setattr(os, "kill", fake_kill)
        assert UndoGenerator._pid_alive(123456) is False

    def test_reap_stale_shadows_returns_quietly_when_the_parent_cannot_be_listed(
            self, tmp_path: Path) -> None:
        """A real permission failure (not mocked) on the directory
        _reap_stale_shadows scans -- must skip reaping this round, not
        crash __init__. is_dir() on `parent` itself only needs traversal
        permission on parent's own ancestors (which tmp_path still grants),
        so it stays True; iterdir() needs read+execute on `parent` itself,
        which the chmod below removes."""
        parent = tmp_path / "restricted"
        parent.mkdir()
        generator = UndoGenerator(shadow_dir=parent / "s")
        os.chmod(parent, 0o000)
        try:
            generator._reap_stale_shadows()
        finally:
            os.chmod(parent, 0o755)

    def test_the_real_systemctl_scope_probe_recognizes_user_scope(
            self, monkeypatch) -> None:
        """test_the_real_systemctl_scope_probe_does_not_crash only ever
        resolves this machine's real units, which are all "system" --
        nothing exercises the "user" return itself."""
        def fake_run(cmd, **kwargs):
            assert cmd[:2] == ["systemctl", "--user"]
            class Result:
                stdout = "LoadState=loaded\n"
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert UndoGenerator._systemctl_scope("some.service") == "user"

    def test_the_real_dpkg_probe_returns_empty_on_a_real_failure(
            self, monkeypatch) -> None:
        def broken_run(cmd, **kwargs):
            raise FileNotFoundError("dpkg-query not found")

        monkeypatch.setattr(subprocess, "run", broken_run)
        assert UndoGenerator._dpkg_set() == set()

    def test_reflink_probe_is_false_when_the_probe_subprocess_itself_fails(
            self, tmp_path: Path, monkeypatch) -> None:
        """Distinct from test_reflink_probe_is_false_when_cp_is_unavailable:
        `cp` is genuinely on PATH here, it's the probe invocation itself
        that blows up (killed, vanished mid-run, etc.)."""
        generator = UndoGenerator(shadow_dir=tmp_path / "s")

        def broken_run(cmd, **kwargs):
            raise OSError("cp vanished mid-probe")

        monkeypatch.setattr(subprocess, "run", broken_run)
        assert generator._probe_reflink() is False

    def test_copy_falls_back_to_a_byte_copy_when_the_reflink_subprocess_itself_fails(
            self, tmp_path: Path, monkeypatch) -> None:
        """Distinct from test_copy_falls_back_to_a_byte_copy_when_reflink_is_unsupported
        (probe says no) and the returncode-!=0 path exercised by the
        success test's mirror image: here the reflink subprocess call
        raises outright, and _copy must still swallow it and finish the
        copy as a byte copy."""
        source = tmp_path / "src.txt"
        source.write_text("content", encoding="utf-8")
        dest = tmp_path / "dst.txt"
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        generator.reflink_supported = True

        def broken_run(cmd, **kwargs):
            raise OSError("cp vanished mid-copy")

        monkeypatch.setattr(subprocess, "run", broken_run)
        how = generator._copy(source, dest)

        assert how == "copy"
        assert generator.byte_copies == 1
        assert dest.read_text(encoding="utf-8") == "content"
        assert any("reflink copy of" in s for s in generator.skipped)

    def test_undo_packages_removes_only_the_packages_added_since_the_snapshot(
            self, tmp_path: Path, monkeypatch) -> None:
        """Every existing capture_packages test only ever builds the
        UndoAction -- nothing calls .undo() itself, so the closure that
        diffs against the snapshot and issues the real removal never ran."""
        calls = {"n": 0}

        def probe() -> set[str]:
            calls["n"] += 1
            return {"curl"} if calls["n"] == 1 else {"curl", "nginx", "nginx-common"}

        generator = UndoGenerator(shadow_dir=tmp_path / "s", package_probe=probe)
        action = generator.capture_packages("nginx nginx-common")

        recorded: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            recorded.append(cmd)
            class Result:
                returncode = 0
            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        action.undo()

        assert recorded and recorded[0][:3] == ["apt-get", "remove", "--yes"]
        assert set(recorded[0][3:]) == {"nginx", "nginx-common"}

    def test_undo_packages_issues_no_removal_when_nothing_new_was_added(
            self, tmp_path: Path, monkeypatch) -> None:
        generator = UndoGenerator(shadow_dir=tmp_path / "s",
                                  package_probe=lambda: {"curl"})
        action = generator.capture_packages("nginx")

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        action.undo()

        assert calls == []

    def test_stats_reports_the_copy_mode_counters(self, tmp_path: Path) -> None:
        generator = UndoGenerator(shadow_dir=tmp_path / "s")
        stats = generator.stats()
        assert stats["shadow_dir"] == str(generator.shadow_dir)
        assert stats["snapshots"] == 0
        assert stats["reflink_copies"] == 0
        assert stats["byte_copies"] == 0


class TestVerifiedRollback:
    """`undone` used to mean only that the callable did not raise."""

    def test_a_no_op_undo_is_not_reported_as_undone(self) -> None:
        rollback = VerifiedRollback()
        rollback.add("pretend to restore", lambda: None, verify=lambda: False)
        report = rollback.rollback()

        assert report.ok is False
        assert report.results[0].status == "unverified"
        assert report.results[0].verified is False

    def test_lifo_order(self) -> None:
        order: list[str] = []

        def record(n: str) -> Callable[[], None]:
            return lambda: order.append(n)

        rollback = VerifiedRollback()
        for name in ("bottom", "middle", "top"):
            rollback.add(name, record(name), verify=lambda: True)
        rollback.rollback()
        assert order == ["top", "middle", "bottom"]

    def test_drop_where_removes_only_the_selected_actions_and_keeps_the_order(self) -> None:
        order = []
        rollback = VerifiedRollback()
        for name in ("bottom", "drop-me", "top"):
            rollback.add(name, lambda n=name: order.append(n))

        dropped = rollback.drop_where(lambda action: action.description == "drop-me")

        assert [a.description for a in dropped] == ["drop-me"]
        assert rollback.depth == 2
        rollback.rollback()
        assert order == ["top", "bottom"]

    def test_an_unverified_undo_halts_the_sweep(self) -> None:
        """Unwinding past a step that did not revert is not recovery."""
        order = []
        rollback = VerifiedRollback(stop_on_unverified=True)
        rollback.add("bottom", lambda: order.append("bottom"), verify=lambda: True)
        rollback.add("broken", lambda: order.append("broken"), verify=lambda: False)
        rollback.add("top", lambda: order.append("top"), verify=lambda: True)

        report = rollback.rollback()
        assert order == ["top", "broken"]
        assert report.ok is False
        assert rollback.depth == 1

    def test_a_missing_verifier_is_honest_about_it(self) -> None:
        rollback = VerifiedRollback()
        rollback.add("legacy", lambda: None)
        report = rollback.rollback()
        assert report.results[0].verified is None
        assert "nothing was checked" in report.results[0].error
        assert report.ok is True

    def test_a_raising_verifier_counts_as_unverified(self) -> None:
        rollback = VerifiedRollback(stop_on_unverified=False)
        rollback.add("r", lambda: None,
                     verify=lambda: (_ for _ in ()).throw(OSError("cannot stat")))
        report = rollback.rollback()
        assert report.results[0].status == "unverified"
        assert "OSError" in report.results[0].error

    def test_declining_an_unsafe_undo_touches_nothing(self) -> None:
        order = []
        rollback = VerifiedRollback(confirm_unsafe=lambda a: False)
        rollback.add("safe one", lambda: order.append("safe"), verify=lambda: True)
        rollback.add("drop the table", lambda: order.append("drop"), safe=False)

        report = rollback.rollback()
        assert order == []
        assert len(report.cancelled) == 1
        assert rollback.depth == 2, "the declined action and its dependents survive"

    def test_peek_and_actions_on_an_empty_stack(self) -> None:
        rollback = VerifiedRollback()
        assert rollback.peek() is None
        assert rollback.actions() == []

    def test_peek_and_actions_reflect_the_pushed_order(self) -> None:
        rollback = VerifiedRollback()
        rollback.add("bottom", lambda: None)
        rollback.add("top", lambda: None)
        top = rollback.peek()
        assert top is not None
        assert top.description == "top"
        assert [a.description for a in rollback.actions()] == ["bottom", "top"]

    def test_a_raising_confirm_callback_is_treated_as_a_decline(self) -> None:
        def broken_confirm(action):
            raise RuntimeError("simulated confirm crash")

        rollback = VerifiedRollback(confirm_unsafe=broken_confirm)
        rollback.add("drop the table", lambda: None, safe=False)

        report = rollback.rollback()
        assert report.results[0].status == "cancelled"
        assert "confirm raised RuntimeError" in report.results[0].error

    def test_an_approved_unsafe_undo_actually_runs(self) -> None:
        order = []
        rollback = VerifiedRollback(confirm_unsafe=lambda a: True)
        rollback.add("drop the table", lambda: order.append("dropped"),
                     safe=False, verify=lambda: True)

        report = rollback.rollback()
        assert order == ["dropped"]
        assert report.results[0].status == "undone"

    def test_a_raising_undo_is_reported_as_failed(self) -> None:
        def broken_undo():
            raise OSError("disk full")

        rollback = VerifiedRollback()
        rollback.add("r", broken_undo)
        report = rollback.rollback()

        assert report.results[0].status == "failed"
        assert "OSError" in report.results[0].error
        assert report.ok is False

    def test_stop_on_cancel_false_continues_past_a_declined_action(self) -> None:
        order = []
        rollback = VerifiedRollback(confirm_unsafe=lambda a: False, stop_on_cancel=False)
        rollback.add("bottom", lambda: order.append("bottom"), verify=lambda: True)
        rollback.add("drop the table", lambda: None, safe=False)

        report = rollback.rollback()
        assert order == ["bottom"], "the sweep continued past the cancelled action"
        assert len(report.cancelled) == 1
        assert len(report.undone) == 1
        assert rollback.depth == 0

    def test_rollback_actions_reports_the_command_for_classification(self) -> None:
        """`rollback_actions()` used to return only (description, undo, safe),
        which is exactly why HANDOFF.md §6.4's gap was possible: nothing
        downstream could classify a command it was never given. `command` is
        now part of the tuple so a caller can. This class only tests the raw
        primitive; `Torra.execute` is where the command is actually
        classified and a declared `safe: true` gets overridden -- see
        `tests/test_pipeline.py::test_a_declared_safe_rollback_is_still_classified`
        for that end-to-end coverage."""
        target = tempfile.mkdtemp()
        spec = TaskSpec.validate({
            "task_id": "t", "request": "r",
            "steps": [{"id": "s1", "run": "true"}],
            "rollback": [{"run": f"rm -rf {target}", "description": "cleanup"}],
        })
        actions = spec.rollback_actions()
        assert len(actions) == 1
        description, command, undo, declared_safe = actions[0]
        assert command == f"rm -rf {target}"
        assert SafetyClassifier().classify(command).level is RiskLevel.CRITICAL
        assert declared_safe is True, "the declaration omitted safe, defaulting True"

    def test_rollback_actions_honours_a_declared_cwd_and_timeout(self) -> None:
        """Found in a full-file audit: this used to always call the runner
        with (command, None, None), silently dropping a declared cwd or
        timeout with no error -- a rollback command relying on a relative
        path would run from the wrong directory."""
        seen = {}

        def runner(command, cwd, timeout):
            seen["cwd"], seen["timeout"] = cwd, timeout
            return "ok"

        spec = TaskSpec.validate({
            "task_id": "t", "request": "r",
            "steps": [{"id": "s1", "run": "true"}],
            "rollback": [{"run": "git reset --hard", "cwd": "/srv/app", "timeout": 30}],
        })
        _description, _command, undo, _safe = spec.rollback_actions(runner=runner)[0]
        undo()

        assert seen == {"cwd": "/srv/app", "timeout": 30}


# --------------------------------------------------------------------------- #
# ASK_CLARIFYING_QUESTIONS
# --------------------------------------------------------------------------- #


class TestClarifier:
    """Two closed vocabularies failed measurement before this worked.

    Phrasing lists scored 83% false negatives on a blind corpus. Verb lists
    scored 50%: the same failure one level up. Anchor presence is verb-agnostic.
    """

    AMBIGUOUS = [
        "fix it", "clean up this", "make the api faster", "delete all the logs",
        "restart the server", "roll back the deployment", "deploy",
        "reset the postgres volume", "free up some disk", "tidy up the box",
        "quiesce the writes", "drain the queue", "throttle the neighbour",
        "consolidate the dirs", "reticulate the splines", "unwedge the thing",
        "get the api ready for production", "migrate to the new setup",
    ]

    SPECIFIC = [
        "restart ollama.service on popos-01 and confirm systemctl reports active",
        "tail the last 200 lines of /var/log/syslog",
        "remove /tmp/build-cache-2024 and nothing else",
        "deploy commit a1b2c3d to staging",
        "list every container using more than 2 GB of memory",
        "delete every file in /tmp/torra-shadow older than 7 days",
        "install python3-pytest with apt",
        "set the ollama systemd unit to restart on failure",
        "remove docker images older than 30 days",
        "run nvidia-smi and report the free VRAM",
    ]

    def test_zero_false_negatives(self) -> None:
        clarifier = Clarifier()
        missed = [r for r in self.AMBIGUOUS if not clarifier.detect(r)]
        assert not missed, missed

    def test_low_false_positives(self) -> None:
        clarifier = Clarifier()
        blocked = [(r, [a.code for a in clarifier.detect(r)])
                   for r in self.SPECIFIC if clarifier.detect(r)]
        assert len(blocked) <= 1, blocked

    def test_it_is_verb_agnostic(self) -> None:
        """Invented jargon cannot slip past, because there is no verb list."""
        clarifier = Clarifier()
        for request in ("defenestrate the daemon", "frobnicate the widget",
                        "yeet the thing"):
            codes = {a.code for a in clarifier.detect(request)}
            assert "no_target" in codes, f"{request!r} -> {codes}"

    def test_structure_beats_phrasing(self) -> None:
        """Casual wording with a real path is executable. Politeness is not."""
        clarifier = Clarifier()
        assert clarifier.detect("nuke /tmp/build-cache-2024") == []
        assert clarifier.detect("yeet the /srv/api/old.log file") == []
        assert clarifier.detect("please could you possibly restart the server")

    def test_destructive_needs_a_target_or_a_filter(self) -> None:
        clarifier = Clarifier()
        assert any(a.code == "no_backup_plan"
                   for a in clarifier.detect("wipe the staging data"))
        assert clarifier.detect("remove docker images older than 30 days") == []
        assert Clarifier(strict_destructive=True).detect(
            "remove /tmp/build-cache-2024 and nothing else")

    def test_deploy_without_an_environment_is_flagged(self) -> None:
        assert any(a.code == "no_environment"
                   for a in Clarifier().detect("deploy the api"))

    def test_deploy_to_localhost_is_not_flagged(self) -> None:
        """Found in a full-file audit: the anchor pattern recognizes
        "localhost" as a named environment, but this check used to look
        for "local" instead -- a word-boundary mismatch ("local" never
        matches inside "localhost"), so the clarifier both found the
        anchor and complained no environment was named, for the same
        request."""
        codes = {a.code for a in Clarifier().detect("deploy the api localhost")}
        assert "no_environment" not in codes

    def test_deploy_to_fix_something_is_still_flagged(self) -> None:
        """Found live: `_check_no_environment`'s fallback pattern was
        `\\b(to|into|on)\\s+\\S+`, meant to catch "deploy to <environment>"
        phrasing the fixed word list doesn't cover -- but it matches "to"
        followed by *any* word, environment or not. "deploy the api to fix
        the bug" and "...to handle more load" both matched "to fix"/"to
        handle" and were silently treated as having named an environment,
        even though neither names one at all."""
        clarifier = Clarifier()
        for request in ("deploy the api to fix the bug",
                        "deploy the api to handle more load"):
            codes = {a.code for a in clarifier.detect(request)}
            assert "no_environment" in codes, request

    def test_extra_anchors_teach_it_a_local_scheme(self) -> None:
        assert Clarifier().detect("restart the frobnitz gateway")
        taught = Clarifier(extra_anchors=[(r"\bfrobnitz\s+\w+\b", "frobnitz id")])
        assert taught.detect("restart the frobnitz gateway") == []

    def test_clarify_reports_needs_clarification_questions_and_serializes(self) -> None:
        clarification = Clarifier().clarify("fix it")
        assert clarification.needs_clarification is True
        assert clarification.questions()
        d = clarification.to_dict()
        assert d["request"] == "fix it"
        assert d["ambiguities"][0]["code"]

    def test_clarify_on_a_specific_request_needs_no_clarification(self) -> None:
        clarification = Clarifier().clarify("remove docker images older than 30 days")
        assert clarification.needs_clarification is False
        assert clarification.questions() == []

    def test_a_request_with_no_dangling_pronoun_is_not_flagged(self) -> None:
        """`_has_antecedent`'s own no-match short circuit -- most specific
        requests never even mention "it"/"this"/"that"."""
        codes = {a.code for a in Clarifier().detect(
            "remove docker images older than 30 days")}
        assert "dangling_pronoun" not in codes

    def test_has_antecedent_defaults_true_with_no_pronoun_present(self) -> None:
        """`_has_antecedent`'s own `if not match: return True` -- its one
        caller (`_check_dangling_pronoun`) already short-circuits on the
        identical regex before ever reaching this, so the only way to
        exercise this defensive default is calling the helper directly."""
        assert Clarifier()._has_antecedent("remove docker images") is True

    def test_open_timing_with_no_concrete_schedule_is_flagged(self) -> None:
        codes = {a.code for a in Clarifier().detect(
            "restart ollama.service on popos-01 sometime soon")}
        assert "no_schedule" in codes

    def test_detect_on_an_empty_request_reports_it_as_empty(self) -> None:
        ambiguities = Clarifier().detect("   ")
        assert len(ambiguities) == 1
        assert ambiguities[0].code == "empty"

    def test_explain_renders_anchors_filter_and_verdict_for_a_blocked_request(
            self) -> None:
        text = Clarifier().explain("fix it")
        assert "anchors:" in text
        assert "filter:" in text
        assert "BLOCKED" in text

    def test_explain_reports_executable_for_a_specific_request(self) -> None:
        text = Clarifier().explain("remove docker images older than 30 days")
        assert "executable" in text


# --------------------------------------------------------------------------- #
# Routing and transparency
# --------------------------------------------------------------------------- #


class TestRouting:
    """First-match routing cannot express 'both matched and debug wins here'."""

    CONTRACT = [
        ("deploy the api container to staging", "devstral:24b"),
        ("release the new systemd unit", "devstral:24b"),
        ("debug why nginx keeps crashing", "gpt-oss:20b"),
        ("find the root cause of this traceback", "gpt-oss:20b"),
        ("list the running containers", "devstral:24b"),
        ("debug the failing deploy to production", "gpt-oss:20b"),
        ("deploy the fix for the crash", "gpt-oss:20b"),
        ("check why the release is broken", "gpt-oss:20b"),
        ("review the deploy script before we ship it", "gpt-oss:20b"),
        ("refactor the auth module", "devstral-small-2:24b"),
    ]

    def test_the_routing_contract(self) -> None:
        router = ModelRouter()
        wrong = [(r, router.select(r).model, want)
                 for r, want in self.CONTRACT if router.select(r).model != want]
        assert not wrong, wrong

    def test_the_earliest_rule_owns_the_intent(self) -> None:
        """"review the deploy script" beat term frequency twice before this."""
        router = ModelRouter()
        assert router.select("review the deploy script before we ship it").rule \
            == "analyze"
        assert router.select("deploy the reviewed script").rule == "deploy"

    def test_it_never_routes_to_an_absent_model(self) -> None:
        """Devstral is both the "deploy" rule's target and the class default,
        so this only proves something without an accelerator set: `runnable()`
        alone doesn't know about `available` at all, and used to let an
        absent model through as if it were a safe, ready-to-call choice."""
        limited = ModelRouter(available=["qwen3:32b", "qwen3:8b"])
        choice = limited.select("deploy the api to staging")
        assert choice.model == "qwen3:32b", choice
        assert "not pulled locally" in choice.reason

    def test_an_unmatched_request_defaults_to_the_measured_reliable_model(self) -> None:
        """A phrasing that matches none of the keyword rules used to silently
        land on qwen3:8b -- the one model with a measured hard JSON-schema
        failure in scripts/dev/model_bench/REPORT.md, found live when a real
        user asked "what files take up lots of space" and hit exactly that
        failure. An unmatched request has no other signal to route on, so it
        should default to the model measured most reliable, not the cheapest."""
        router = ModelRouter()
        choice = router.select("what files take up lots of space")
        assert choice.rule == "default"
        assert choice.model == "devstral:24b"

    def test_an_ambiguous_unmatched_request_routes_to_the_asking_model(self) -> None:
        """Found live 2026-08-17: "storage issues on paramostorage" matched no
        rule and Clarifier flagged it ambiguous (no target/anchor). Routed to
        devstral (the class default), it proposed a destructive-shaped cleanup
        plan instead of asking what was wrong; the identical prompt routed to
        qwen3:32b asked the right clarifying question. `ambiguous=True` is how
        the caller (Agent.converse) passes Clarifier's own verdict in.
        qwen3:32b was retired in the 2026-08-28 Qwen-removal swap and
        AMBIGUOUS_FALLBACK_MODEL now points at its replacement,
        gpt-oss:20b -- this only proves the routing plumbing still
        reaches whatever that constant names, not that the replacement
        asks the same right question (unverified, see router.py's comment)."""
        router = ModelRouter()
        choice = router.select("storage issues on paramostorage", ambiguous=True)
        assert choice.rule == "default"
        assert choice.model == "gpt-oss:20b"

    def test_an_ambiguous_request_prefers_the_default_over_an_arbitrary_fit(self) -> None:
        """Found live 2026-08-17: qwen3:32b (32.8B real params) needs ~23.5GB
        and this card only ever has ~23.3-23.8GB free even fully idle, so it
        loses `fits()` almost every time -- and the generic downgrade
        fallback (largest model that happens to fit) landed on
        qwen3-coder:30b, which fabricated a disk-usage answer for the exact
        same ambiguous request. When AMBIGUOUS_FALLBACK_MODEL doesn't fit,
        this must prefer the measured-reliable default_model over whatever
        else happens to be big and available, not the model that just
        proved itself unreliable for this kind of request. Re-parametrized
        2026-08-28 onto the post-Qwen-removal names (gpt-oss:20b /
        devstral-small-2:24b) -- it exercises the same mechanism, not a
        re-verification of the original qwen3:32b/qwen3-coder:30b finding."""
        class EverythingButAmbiguousFallbackFits:
            """Both devstral and the larger devstral-small-2 fit -- proves this
            deliberately skips _pick_replacement()'s "largest fitting"
            choice (which would pick devstral-small-2 here) for default_model."""

            gpu = True
            free_vram_mb = 20000

            def fits(self, model: str) -> bool:
                return model != "gpt-oss:20b"

        router = ModelRouter(
            available=["gpt-oss:20b", "devstral:24b", "devstral-small-2:24b"],
            accelerator=EverythingButAmbiguousFallbackFits())
        choice = router.select("storage issues on paramostorage", ambiguous=True)
        assert choice.model == "devstral:24b"

    def test_ambiguity_never_overrides_an_explicit_rule_match(self) -> None:
        """Ambiguity is only a signal for the *unmatched* fallback path -- a
        rule match (e.g. "deploy") is a stronger, more specific signal and
        must win regardless of how `ambiguous` is set."""
        router = ModelRouter()
        choice = router.select("deploy the api container to staging", ambiguous=True)
        assert choice.rule == "deploy"
        assert choice.model == "devstral:24b"

    def test_confidence_reflects_the_margin(self) -> None:
        router = ModelRouter()
        clear = router.select("deploy the api container to staging")
        contested = router.select("debug the failing deploy to production")
        assert clear.confidence > contested.confidence

    def test_an_untagged_pull_still_counts_as_available(self) -> None:
        """`ollama pull devstral` registers as 'devstral:latest'; a bare-name
        rule must still match it, or every rule for an untagged pull is
        silently treated as unavailable and downgraded. (ROUTING_RULES
        itself no longer has a bare-name target -- every entry was tagged
        precisely when the model catalog was pruned to four models -- so
        this exercises the normalization directly with its own rule.)"""
        router = ModelRouter(
            rules=[("deploy", r"\bdeploy\b", "devstral", "n/a")],
            available=["devstral:latest", "qwen3:32b"])
        assert router.select("deploy the api to staging").model == "devstral"

    def test_a_downgrade_never_re_offers_the_model_that_just_failed(self) -> None:
        """Found live in a real audit log (disk-usage-analysis): an unmatched
        request defaults to devstral, VRAM was too tight for anything in
        `available` to pass `runnable()`, and the old last-resort fallback
        was `self.default_model` -- which *is* devstral, so the reported
        "downgraded from devstral because ..." resolved right back to
        devstral. With nothing runnable, the least-bad choice is the
        smallest model actually pulled, not a repeat of the failed one."""
        class NothingFits:
            gpu = True
            free_vram_mb = 5660

            def fits(self, model: str) -> bool:
                return False

        router = ModelRouter(
            available=["devstral", "qwen3:32b", "qwen3:8b"],
            accelerator=NothingFits())
        choice = router.select("what files take up lots of space")
        assert choice.model != "devstral"
        assert choice.model == "qwen3:8b"

    def test_a_downgrade_never_lands_on_an_embedding_model(self) -> None:
        """Found live: a freshly-pulled catalog with a much wider spread of
        model sizes and one real embedding model (nomic-embed-text). Its
        name carries no parameter count, so the old name-guessing
        `_params_b` read it as "0B" -- indistinguishable from "definitely
        tiny" -- and every real chat model was too big for the free VRAM
        at that moment, so the downgrade fallback's `min(key=_params_b)`
        picked the embedding model as the "smallest, safest" choice. Ollama
        can't hold a chat/plan conversation on an embedding-only model;
        routing a real request onto it is a hang or a broken response, not
        a working answer at reduced quality."""
        class TightVram:
            gpu = True
            free_vram_mb = 3000

            def fits(self, model: str) -> bool:
                return False

        available = ["devstral", "qwen3:32b", "qwen3:8b", "nomic-embed-text:latest"]
        router = ModelRouter(
            available=available, accelerator=TightVram(),
            model_params_b={"nomic-embed-text:latest": 0.137, "qwen3:8b": 8.0},
            model_chat_capable={"nomic-embed-text:latest": False})

        choice = router.select("what files take up lots of space")

        assert choice.model != "nomic-embed-text:latest"
        assert choice.model == "qwen3:8b"

    def test_score_never_routes_a_rule_onto_a_non_chat_model(self) -> None:
        router = ModelRouter(
            rules=[("quick", r"\bcheck\b", "nomic-embed-text:latest", "n/a")],
            available=["nomic-embed-text:latest"],
            model_chat_capable={"nomic-embed-text:latest": False})
        assert router.score("check the status") == []


class TestModelParamsAndChatCapable:
    """The daemon's own `/api/show` data, consulted before the name-guessing
    fallback that silently mis-sizes any model whose name carries no digit."""

    def test_a_known_size_wins_over_the_name_guess(self) -> None:
        router = ModelRouter(model_params_b={"qwen3.5:latest": 27.0})
        assert router._params_b("qwen3.5:latest") == 27.0

    def test_a_zero_reported_size_falls_back_to_the_name_guess(self) -> None:
        """`/api/show` failed for this one model (empty dict, `.get` misses)
        -- must not be read as 'known to be 0B', or a model this large
        would look free to route onto a CPU or an undersized card."""
        router = ModelRouter(model_params_b={})
        assert router._params_b("devstral") == 24.0

    def test_bare_name_is_normalized_before_the_lookup(self) -> None:
        """`available` and `/api/show` results are keyed by the daemon's own
        tag ('devstral:latest'); a rule referencing the bare name must
        still hit the same entry."""
        router = ModelRouter(model_params_b={"devstral:latest": 24.0})
        assert router._params_b("devstral") == 24.0

    def test_chat_capable_defaults_true_with_no_data(self) -> None:
        assert ModelRouter()._chat_capable("anything") is True

    def test_chat_capable_defaults_true_for_an_unlisted_model(self) -> None:
        """Missing from the dict means /api/show never ran for it (or
        failed), not that it was checked and found incapable."""
        router = ModelRouter(model_chat_capable={"nomic-embed-text:latest": False})
        assert router._chat_capable("devstral") is True

    def test_chat_capable_is_false_when_reported_false(self) -> None:
        router = ModelRouter(model_chat_capable={"nomic-embed-text:latest": False})
        assert router._chat_capable("nomic-embed-text") is False

    def test_thinking_capable_defaults_false_with_no_data(self) -> None:
        """Opposite default from chat_capable -- not knowing must mean
        "don't ask this model to stream reasoning," not "assume it can"."""
        assert ModelRouter()._thinking_capable("anything") is False

    def test_thinking_capable_defaults_false_for_an_unlisted_model(self) -> None:
        router = ModelRouter(model_thinking_capable={"deepseek-r1:latest": True})
        assert router._thinking_capable("devstral") is False

    def test_thinking_capable_is_true_when_reported_true(self) -> None:
        router = ModelRouter(model_thinking_capable={"deepseek-r1:latest": True})
        assert router._thinking_capable("deepseek-r1") is True


class TestModelAvailable:
    """The tag-normalization helper `Agent.converse` and `ModelRouter.score`
    both depend on, so a bare pull name isn't silently treated as absent."""

    def test_bare_name_matches_its_latest_tag(self) -> None:
        assert model_available("devstral", ["devstral:latest", "qwen3:8b"])

    def test_exact_tag_still_matches(self) -> None:
        assert model_available("qwen3:32b", ["qwen3:32b", "qwen3:8b"])

    def test_a_genuinely_absent_model_does_not_match(self) -> None:
        assert not model_available("phi3", ["devstral:latest", "qwen3:8b"])

    def test_different_tags_of_the_same_base_do_not_conflate(self) -> None:
        """Requesting qwen3:32b must not be satisfied by qwen3:8b being present."""
        assert not model_available("qwen3:32b", ["qwen3:8b"])


class TestDecisionLog:
    """Reasoning is required, and a missing one is visible rather than blank."""

    def test_empty_reasoning_is_flagged(self) -> None:
        log = DecisionLog()
        log.log("stage", "did a thing", "")
        assert log.violations()

    def test_a_broken_sink_does_not_break_the_run(self) -> None:
        """The TUI subscribes to this. A UI crash must not kill a deploy."""
        def exploding(_record) -> None:
            raise RuntimeError("ui died")

        log = DecisionLog(sink=exploding)
        log.log("stage", "decision", "reasoning")
        assert len(log.records) == 1


class TestHardwareAwareRouting:
    """Routing a 32b model onto a CPU is technically correct and useless.

    The router is the only place that knows both which model a task wants and
    what the machine can actually run, so it is where the two get reconciled.
    """

    AVAILABLE = ["gpt-oss:20b", "qwen3:8b", "devstral:24b", "phi3"]

    def _router(self, accelerator):
        from torra.gates import ModelRouter
        return ModelRouter(available=self.AVAILABLE, accelerator=accelerator)

    def test_a_big_gpu_gets_the_big_model(self) -> None:
        from torra.core import Accelerator
        router = self._router(Accelerator("cuda", "RTX 4090", 24564, 23000))
        assert router.select("debug why nginx keeps crashing").model == "gpt-oss:20b"
        assert router.select("deploy the api to staging").model == "devstral:24b"

    def test_cpu_only_never_loads_a_32b_model(self) -> None:
        """The reported symptom: three times the thinking time, on CPU."""
        from torra.core import Accelerator
        router = self._router(Accelerator(kind="cpu", detail="no nvidia-smi"))
        choice = router.select("debug why nginx keeps crashing")

        assert choice.model == "qwen3:8b"
        assert "no GPU" in choice.reason, "the swap must be visible, not silent"
        assert router.downgrades == [("debug", "gpt-oss:20b", "qwen3:8b")]

    def test_a_small_card_gets_a_model_that_fits(self) -> None:
        from torra.core import Accelerator
        router = self._router(Accelerator("cuda", "GTX 1650", 4096, 3500))
        assert router.select("debug why nginx keeps crashing").model == "phi3"
        assert router.select("deploy the api to staging").model == "phi3", (
            "devstral is 24b and would not fit, despite its name saying nothing"
        )

    def test_a_nameless_size_is_looked_up_not_assumed(self) -> None:
        """`devstral` parses as 0b and would look like it fits anywhere."""
        from torra.core import Accelerator
        router = self._router(Accelerator("cuda", "GTX 1650", 4096, 3500))
        assert router._params_b("devstral") == 24.0
        assert router.runnable("devstral") is False
        assert router.runnable("phi3") is True

    def test_no_accelerator_information_changes_nothing(self) -> None:
        """Routing must behave exactly as before when hardware is unknown."""
        from torra.gates import ModelRouter
        plain = ModelRouter(available=self.AVAILABLE)
        assert plain.select("debug why nginx keeps crashing").model == "gpt-oss:20b"
        assert plain.downgrades == []

    def test_model_choice_to_dict_is_serializable(self) -> None:
        choice = ModelRouter().select("debug why nginx keeps crashing")
        d = choice.to_dict()
        assert d["model"] == "gpt-oss:20b"
        assert d["rule"] == "debug"

    def test_explain_reports_no_rule_matched_for_an_unmatched_request(self) -> None:
        router = ModelRouter()
        text = router.explain("xyzzy plugh quux")
        assert "no rule matched" in text
        assert router.default_model in text

    def test_explain_reports_the_score_table_for_a_matched_request(self) -> None:
        router = ModelRouter()
        text = router.explain("debug why nginx keeps crashing")
        assert "debug" in text
        assert "gpt-oss:20b" in text


class TestServiceStateHelpers:
    """HANDOFF.md §2.63: the verbs that put a unit's enablement and running state back."""

    def test_restore_verbs_cover_each_before_and_now_combination(self) -> None:
        from torra.gates.service_state import restore_verbs
        assert restore_verbs("disabled", "active", "enabled") == ["enable", "start"]
        assert restore_verbs("enabled", "inactive", "disabled") == ["disable", "stop"]
        assert restore_verbs("masked", "active", "enabled") == ["unmask", "enable", "start"]
        assert restore_verbs("enabled", "inactive", "masked") == ["stop", "mask"]
        assert restore_verbs("masked", "inactive", "masked") == ["stop"]
        assert restore_verbs("static", "active", "static") == ["start"]
        assert restore_verbs("enabled", "active", "enabled") == ["start"]
        assert restore_verbs("masked", "inactive", "static") == ["unmask", "stop"]
        assert restore_verbs("masked", "active", "indirect") == ["unmask", "start"]

    def test_the_query_runs_in_the_units_own_scope(self, monkeypatch) -> None:
        from torra.gates.service_state import systemctl_query
        seen: list[list[str]] = []

        class Out:
            stdout = "active\n"

        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (seen.append(cmd), Out())[1])

        assert systemctl_query("is-active", "u.service", "user") == "active"
        assert systemctl_query("is-enabled", "u.service", "system") == "active"
        assert seen == [["systemctl", "--user", "is-active", "u.service"],
                        ["systemctl", "is-enabled", "u.service"]]

    def test_an_undo_is_not_verified_when_the_state_could_never_be_read(
            self, tmp_path: Path, monkeypatch) -> None:
        """`unknown == unknown` used to pass, so a failed undo read as verified."""
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: None)
        generator = UndoGenerator(shadow_dir=tmp_path / "s", service_probe=lambda u: "unknown",
                                  enabled_probe=lambda u: "unknown", scope_probe=lambda u: "system")
        action = generator.capture_service("nginx")

        assert action.verify is not None and action.verify() is False
