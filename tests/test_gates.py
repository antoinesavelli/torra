"""
The five guardrails, reorganised by subsystem.

Every assertion here was earned by a defect found in six phases of validation.
The comments name which one, because a test whose reason is forgotten is a test
someone eventually deletes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from harness.core import Step, TaskSpec
from harness.gates import (
    Clarifier,
    DecisionLog,
    ModelRouter,
    Precondition,
    RiskLevel,
    SafetyClassifier,
    SnapshotKind,
    StateVerifier,
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
    ]

    ROUTINE = [
        "ls -la /etc", "cat /proc/meminfo", "df -h", "free -h", "uptime",
        "systemctl status ollama", "journalctl -u ollama -n 50",
        "git pull --ff-only", "git log --oneline -20", "git status",
        "docker ps", "docker logs api --tail 100", "python3 -m pytest tests/",
        "grep -r TODO src/", "echo hello", "ss -tlnp", "ps aux", "nvidia-smi",
        "lsblk", "curl -s http://localhost:11434/api/tags",
        "systemctl status nginx > /dev/null", "grep foo bar.txt 2>/dev/null",
    ]

    def test_every_evasion_is_caught(self) -> None:
        """20/20, including eight the phase 1 set never contained."""
        safety = SafetyClassifier()
        missed = [f"{label}: {cmd!r}" for cmd, label in self.EVASIONS
                  if not safety.classify(cmd).needs_confirmation]
        assert not missed, missed

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
        that can never lose data. Found via a real harness exploration run
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

    def test_base64_payloads_are_decoded(self) -> None:
        """An obfuscated payload piped to a shell is still what it decodes to."""
        import base64
        blob = base64.b64encode(b"rm -rf /var/lib/postgresql").decode()
        verdict = SafetyClassifier().classify(f"echo '{blob}' | base64 -d | sh")
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
        verdict.approved = True
        assert verdict.blocked is False


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

    def test_non_blocking_failures_do_not_stop_a_task(self) -> None:
        """Not every check is worth refusing over."""
        verifier = StateVerifier()
        verifier.add(Precondition("nice-to-have", lambda: False, "", blocking=False))
        assert verifier.run().passed is True


# --------------------------------------------------------------------------- #
# ROLLBACK_ON_FAIL
# --------------------------------------------------------------------------- #


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
        assert action.verify() is True

    def test_a_created_file_is_deleted_not_restored(self, tmp_path: Path) -> None:
        target = tmp_path / "new.conf"
        action = UndoGenerator(shadow_dir=tmp_path / "s").for_step(
            Step("create", touches=[str(target)]))[0]
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

    def test_package_undo_is_unsafe_by_design(self) -> None:
        """Removing packages cascades through dependencies, so it asks first."""
        installed = {"curl", "git"}
        generator = UndoGenerator(package_probe=lambda: set(installed))
        action = generator.for_step(Step("i", command="sudo apt install -y nginx"))[0]
        assert action.safe is False
        installed.add("nginx")
        assert action.verify() is False

    def test_it_invents_nothing_it_cannot_invert(self, tmp_path: Path) -> None:
        """dd, mkfs and migrations are named as uncovered rather than faked."""
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

    def test_an_oversized_file_is_refused_loudly(self, tmp_path: Path) -> None:
        """An undo that cannot restore the file would be a lie."""
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 5000)
        generator = UndoGenerator(shadow_dir=tmp_path / "s", max_shadow_bytes=100)
        assert generator.for_step(Step("b", touches=[str(big)])) == []
        assert any("over max_shadow_bytes" in s for s in generator.skipped)


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
        order = []
        rollback = VerifiedRollback()
        for name in ("bottom", "middle", "top"):
            rollback.add(name, lambda n=name: order.append(n), verify=lambda: True)
        rollback.rollback()
        assert order == ["top", "middle", "bottom"]

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

    def test_rollback_actions_reports_the_command_for_classification(self) -> None:
        """`rollback_actions()` used to return only (description, undo, safe),
        which is exactly why HANDOFF.md §6.4's gap was possible: nothing
        downstream could classify a command it was never given. `command` is
        now part of the tuple so a caller can. This class only tests the raw
        primitive; `Harness.execute` is where the command is actually
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
        "delete every file in /tmp/harness-shadow older than 7 days",
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

    def test_extra_anchors_teach_it_a_local_scheme(self) -> None:
        assert Clarifier().detect("restart the frobnitz gateway")
        taught = Clarifier(extra_anchors=[(r"\bfrobnitz\s+\w+\b", "frobnitz id")])
        assert taught.detect("restart the frobnitz gateway") == []


# --------------------------------------------------------------------------- #
# Routing and transparency
# --------------------------------------------------------------------------- #


class TestRouting:
    """First-match routing cannot express 'both matched and debug wins here'."""

    CONTRACT = [
        ("deploy the api container to staging", "devstral"),
        ("release the new systemd unit", "devstral"),
        ("debug why nginx keeps crashing", "qwen3:32b"),
        ("find the root cause of this traceback", "qwen3:32b"),
        ("list the running containers", "qwen3:8b"),
        ("debug the failing deploy to production", "qwen3:32b"),
        ("deploy the fix for the crash", "qwen3:32b"),
        ("check why the release is broken", "qwen3:32b"),
        ("review the deploy script before we ship it", "qwen3:32b"),
        ("refactor the auth module", "devstral"),
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
        assert choice.model == "devstral"

    def test_confidence_reflects_the_margin(self) -> None:
        router = ModelRouter()
        clear = router.select("deploy the api container to staging")
        contested = router.select("debug the failing deploy to production")
        assert clear.confidence > contested.confidence

    def test_an_untagged_pull_still_counts_as_available(self) -> None:
        """`ollama pull devstral` registers as 'devstral:latest'; a bare-name
        rule must still match it, or every rule for an untagged pull is
        silently treated as unavailable and downgraded."""
        router = ModelRouter(available=["devstral:latest", "qwen3:8b"])
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

    AVAILABLE = ["qwen3:32b", "qwen3:8b", "devstral", "phi3"]

    def _router(self, accelerator):
        from harness.gates import ModelRouter
        return ModelRouter(available=self.AVAILABLE, accelerator=accelerator)

    def test_a_big_gpu_gets_the_big_model(self) -> None:
        from harness.core import Accelerator
        router = self._router(Accelerator("cuda", "RTX 4090", 24564, 23000))
        assert router.select("debug why nginx keeps crashing").model == "qwen3:32b"
        assert router.select("deploy the api to staging").model == "devstral"

    def test_cpu_only_never_loads_a_32b_model(self) -> None:
        """The reported symptom: three times the thinking time, on CPU."""
        from harness.core import Accelerator
        router = self._router(Accelerator(kind="cpu", detail="no nvidia-smi"))
        choice = router.select("debug why nginx keeps crashing")

        assert choice.model == "qwen3:8b"
        assert "no GPU" in choice.reason, "the swap must be visible, not silent"
        assert router.downgrades == [("debug", "qwen3:32b", "qwen3:8b")]

    def test_a_small_card_gets_a_model_that_fits(self) -> None:
        from harness.core import Accelerator
        router = self._router(Accelerator("cuda", "GTX 1650", 4096, 3500))
        assert router.select("debug why nginx keeps crashing").model == "phi3"
        assert router.select("deploy the api to staging").model == "phi3", (
            "devstral is 24b and would not fit, despite its name saying nothing"
        )

    def test_a_nameless_size_is_looked_up_not_assumed(self) -> None:
        """`devstral` parses as 0b and would look like it fits anywhere."""
        from harness.core import Accelerator
        router = self._router(Accelerator("cuda", "GTX 1650", 4096, 3500))
        assert router._params_b("devstral") == 24.0
        assert router.runnable("devstral") is False
        assert router.runnable("phi3") is True

    def test_no_accelerator_information_changes_nothing(self) -> None:
        """Routing must behave exactly as before when hardware is unknown."""
        from harness.gates import ModelRouter
        plain = ModelRouter(available=self.AVAILABLE)
        assert plain.select("debug why nginx keeps crashing").model == "qwen3:32b"
        assert plain.downgrades == []
